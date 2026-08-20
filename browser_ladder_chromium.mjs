#!/usr/bin/env node
// L2/L3 of the browser ladder: a real Chromium, launched for one page and killed.
//
// L2 is this browser on the box's own IP. L3 is the identical browser with
// `proxy` set, which is the only difference between the two rungs: the engine
// is not the variable there, the egress is. L4 is somewhere else entirely: the
// resident headed Chrome with a profile and a residential exit, driven over CDP
// by the Python side.
//
// Everything here is ephemeral by construction. The browser is launched on the
// way in and closed in a finally, and the process exits even if a page left
// timers running, because the thing this ladder is for is having no Chromium
// resident when nobody asked for one.
//
// L4 comes through here too, with `cdp` set instead of `proxy`: then we attach
// to the resident headed Chrome rather than launching anything, use its profile
// and its residential exit, and close only the tab we opened. Killing that
// browser is never this script's business.
//
// stdin  {url, proxy, cdp, ua, timeoutMs, challengeWaitMs, shotPath, domPath,
//         blockMedia, viewport, executablePath, headless, extraHeaders, locale, timezone}
// stdout {ok, status, url, title, html, text, console, requests, challenge, ms, ...}

import { existsSync, readdirSync, writeFileSync } from 'node:fs';
import { chromium } from 'playwright-core';

const started = Date.now();
let finished = false;
let browser = null;

let attached = false;   // true at L4: someone else owns this browser's lifetime
let openedPage = null;
let mode = 'cdp';       // headless | headed | cdp: what the viewer shows
let stealth = '';       // how the fingerprint shim got in, if it did
let uaUsed = '';
let realistic = null;   // {ua, major} for a launched browser; null when attached

function emit(payload, code = 0) {
  if (finished) return;
  finished = true;
  payload.ms = Date.now() - started;
  payload.engine = attached ? 'chromium-cdp' : 'chromium';
  try { process.stdout.write(JSON.stringify(payload)); } catch { /* stdout gone */ }
  // close() races the exit deliberately: the answer is already on stdout, and a
  // page that will not let go must not keep a 300 MB browser alive. When we are
  // attached to the resident browser, only our own tab goes.
  try {
    if (attached) { openedPage?.close(); browser?.close(); }
    else { browser?.close({ reason: 'ladder step finished' }); }
  } catch { /* dying anyway */ }
  setTimeout(() => process.exit(code), 400).unref?.();
}

let input;
try {
  input = JSON.parse(await new Promise((res, rej) => {
    let buf = '';
    process.stdin.setEncoding('utf8');
    process.stdin.on('data', c => { buf += c; });
    process.stdin.on('end', () => res(buf));
    process.stdin.on('error', rej);
  }));
} catch (e) {
  emit({ ok: false, why: 'no input on stdin' }, 2);
}
if (!input?.url) emit({ ok: false, why: 'no url' }, 2);

const timeoutMs = Math.max(5000, Number(input.timeoutMs) || 45000);
const challengeWaitMs = Math.max(0, Number(input.challengeWaitMs ?? 12000));

// Killed from outside (the ladder's own deadline, or the box's resource guard):
// still return the partial answer rather than nothing.
for (const sig of ['SIGTERM', 'SIGINT', 'SIGHUP']) {
  process.on(sig, () => emit({ ok: false, why: `killed by ${sig}`, killed: true }));
}
setTimeout(() => emit({ ok: false, why: `no answer within ${timeoutMs}ms`, timeout: true }),
           timeoutMs + 5000).unref?.();

// Newest Playwright Chromium on the box. The full build, not headless_shell:
// the shell has no GPU stack and no extension support, and both of those are
// visible from the page, which is the opposite of what a rung whose whole job
// is "look like a browser" wants.
function findChromium() {
  if (input.executablePath) return input.executablePath;
  const base = `${process.env.HOME || '/home/nimrod_rotem'}/.cache/ms-playwright`;
  let best = null, bestN = -1;
  try {
    for (const d of readdirSync(base)) {
      const m = /^chromium-(\d+)$/.exec(d);
      if (!m) continue;
      const n = Number(m[1]);
      if (n > bestN) { bestN = n; best = `${base}/${d}/chrome-linux64/chrome`; }
    }
  } catch { /* fall through to Playwright's own lookup */ }
  return best;
}

// Deliberately NOT here, and why: the same reasoning as chrome-common.sh, which
// launches the resident browser:
//   --no-sandbox                             the sandbox works on this kernel
//   --disable-gpu                            no WebGL at all is a louder tell
//                                            than SwiftShader's
//   --disable-blink-features=AutomationControlled
//                                            only matters with --enable-automation,
//                                            which Playwright does not pass, and
//                                            the flag is itself a tell
const ARGS = [
  '--no-first-run',
  '--no-default-browser-check',
  '--password-store=basic',
  '--lang=en-US',
  '--use-gl=angle',
  '--use-angle=swiftshader',
  '--enable-unsafe-swiftshader',
  '--autoplay-policy=user-gesture-required',
  '--disable-background-timer-throttling',
  '--disable-backgrounding-occluded-windows',
];

// A challenge page is not a failure to be retried blindly: it is a page that may
// well finish on its own if the browser is given a few seconds to run the
// challenge's own JavaScript. What we wait on has to be the interstitial itself,
// though: a protected site serves its vendor's script on every page, so
// waiting on "the word datadome appears" means sitting out twelve seconds on
// every page of every DataDome customer, having already been served the article.
// Hence two tiers: `strong` decides the wait, `weak` is reported for the viewer
// and judged on the Python side against the status and how much text there is.
const CHALLENGE_JS = `(() => {
  const h = (document.documentElement && document.documentElement.outerHTML) || '';
  const t = document.title || '';
  const hit = (s) => h.indexOf(s) !== -1;
  const strong = [], weak = [];
  if (/just a moment/i.test(t) || hit('cf_chl_opt') || hit('__cf_chl') ||
      /checking (if )?(the security of )?your (browser|connection)/i.test(h) ||
      /enable javascript and cookies to continue/i.test(h)) strong.push('cloudflare');
  if (/incapsula incident id/i.test(h)) strong.push('imperva');
  if (hit('geo.captcha-delivery.com')) strong.push('datadome');
  if (hit('px-captcha') || hit('/px/captcha')) strong.push('perimeterx');
  if (/reference #[0-9a-f.]{10,}/i.test(h) && /access denied/i.test(h)) strong.push('akamai');
  if (/verify you are (a )?human/i.test(h)) strong.push('captcha-wall');
  if (hit('datadome')) weak.push('datadome');
  if (hit('awswaf')) weak.push('awswaf');
  if (hit('_pxhd') || hit('perimeterx')) weak.push('perimeterx');
  if (hit('kpsdk')) weak.push('kasada');
  if (hit('_Incapsula_Resource')) weak.push('imperva');
  if (hit('cf-turnstile')) weak.push('cloudflare');
  if (hit('g-recaptcha') || hit('h-captcha')) weak.push('captcha-widget');
  return { strong, weak };
})()`;

// Playwright's headless Chromium announces itself as "HeadlessChrome" in both
// the User-Agent and the Sec-CH-UA client hints, and its CDP pipe sets
// navigator.webdriver. Either one is a free, decisive answer to "is this a
// bot", which would make L2 fail challenges for reasons that have nothing to do
// with the rung's actual capability, and send pages up to the paid rungs for
// no reason. Both are fixed below: the UA through a CDP override (so the JS
// value and the client hints stay consistent with each other, which setting
// only the context userAgent does not), and webdriver plus the SwiftShader GPU
// string through the same stealth shim the resident browser loads as an
// extension. One source of truth, two ways of injecting it.
function realisticUA(version) {
  const major = String(version || '').split('.')[0] || '140';
  return {
    major,
    ua: `Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) ` +
        `Chrome/${major}.0.0.0 Safari/537.36`,
  };
}

// The context's own userAgent option fixes the request header. The CDP override
// fixes navigator.userAgent, navigator.userAgentData and the Sec-CH-UA hints,
// which the context option does NOT touch: a page that reads userAgentData
// would otherwise still be told "HeadlessChrome" by a browser whose headers say
// Chrome, and a disagreement between the two is a louder signal than either
// value on its own. The session is deliberately never detached: detaching
// reverts the override, which is exactly how the first version of this still
// sent HeadlessChrome on the wire.
async function deheadless(page, context, version, ua, major) {
  try {
    const cdp = await context.newCDPSession(page);
    await cdp.send('Emulation.setUserAgentOverride', {
      userAgent: ua,
      acceptLanguage: 'en-US,en;q=0.9',
      platform: 'Linux x86_64',
      userAgentMetadata: {
        brands: [
          { brand: 'Chromium', version: major },
          { brand: 'Google Chrome', version: major },
          { brand: 'Not=A?Brand', version: '24' },
        ],
        fullVersion: String(version || `${major}.0.0.0`),
        platform: 'Linux', platformVersion: '6.5.0',
        architecture: 'x86', bitness: '64', model: '', mobile: false,
      },
    });
    return ua;
  } catch {
    return '';   // an override we could not install is not a reason to give up
  }
}

const consoleLog = [];
const requests = [];
let mainStatus = 0, mainHeaders = {}, redirects = [];

let page = null;
try {
  let context;
  if (input.cdp) {
    // L4. The profile, the cookies, the residential exit and the window a human
    // can take over are all already there; we borrow a tab.
    attached = true;
    browser = await chromium.connectOverCDP(input.cdp, { timeout: 20000 });
    context = browser.contexts()[0];
    if (!context) throw new Error('the resident browser has no browser context');
  } else {
    // Headed is not a luxury rung here: on a display we already run, a real
    // window costs a little more RAM and removes a whole class of headless-only
    // tells at once. The caller asks for it when the egress is already being
    // changed (L3), so the expensive rungs differ from L2 in more than the IP.
    const headed = input.headless === false && !!input.display;
    browser = await chromium.launch({
      headless: !headed,
      executablePath: findChromium() || undefined,
      args: ARGS,
      proxy: input.proxy ? { server: input.proxy } : undefined,
      env: headed ? { ...process.env, DISPLAY: input.display } : undefined,
      timeout: 45000,
    });
    realistic = realisticUA(browser.version?.());
    context = await browser.newContext({
      viewport: input.viewport || { width: 1920, height: 1080 },
      userAgent: input.ua || realistic.ua,
      locale: input.locale || 'en-US',
      timezoneId: input.timezone || undefined,
      extraHTTPHeaders: input.extraHeaders || undefined,
      // A device pixel ratio of 1 on a 1920x1080 desktop is the ordinary case;
      // Playwright's default is already that, but say so rather than inherit it.
      deviceScaleFactor: 1,
    });
    mode = headed ? 'headed' : 'headless';
  }

  // The same shim the resident browser gets as an extension, injected before any
  // page script runs. Kept in one file so the two browsers cannot drift apart.
  // Never injected when attached: that browser already loads it as an extension,
  // and a second copy would patch the patched functions.
  if (attached) {
    stealth = 'the resident browser loads it as an extension';
  } else if (input.stealthPath && existsSync(input.stealthPath)) {
    try {
      await context.addInitScript({ path: input.stealthPath });
      stealth = 'injected before page scripts';
    } catch { /* best effort */ }
  }

  page = await context.newPage();
  openedPage = page;
  page.setDefaultTimeout(timeoutMs);
  if (!attached && realistic) {
    uaUsed = await deheadless(page, context, browser.version?.(),
                              input.ua || realistic.ua, realistic.major);
  }

  if (input.blockMedia !== false) {
    // Video and fonts are pure cost on a rung whose output is one screenshot and
    // the DOM. Images stay: a page that renders without them often looks broken
    // enough to read as a failure in the viewer.
    await page.route('**/*', route => {
      const t = route.request().resourceType();
      if (t === 'media' || t === 'font') return route.abort();
      return route.continue();
    });
  }

  page.on('console', m => {
    if (consoleLog.length < 200) {
      consoleLog.push({ level: m.type(), text: (m.text() || '').slice(0, 500) });
    }
  });
  page.on('pageerror', e => {
    if (consoleLog.length < 200) {
      consoleLog.push({ level: 'pageerror', text: String(e?.message || e).slice(0, 500) });
    }
  });
  page.on('response', r => {
    if (requests.length < 300) {
      requests.push({
        url: r.url().slice(0, 400),
        status: r.status(),
        type: r.request().resourceType(),
        method: r.request().method(),
      });
    }
  });

  const resp = await page.goto(input.url, {
    waitUntil: 'domcontentloaded',
    timeout: Math.min(timeoutMs, 40000),
  });
  if (resp) {
    mainStatus = resp.status();
    try { mainHeaders = resp.headers(); } catch { mainHeaders = {}; }
    try {
      let r = resp.request().redirectedFrom();
      while (r && redirects.length < 10) { redirects.unshift(r.url()); r = r.redirectedFrom(); }
    } catch { /* redirect chain is nice to have, not required */ }
  }

  const empty = { strong: [], weak: [] };
  let marks = await page.evaluate(CHALLENGE_JS).catch(() => empty);
  let waited = 0;
  if (marks.strong.length && challengeWaitMs > 0) {
    // Sit through it. A Cloudflare managed challenge resolves itself in a real
    // browser in a few seconds; the ones that never resolve are exactly the
    // ones worth escalating for, and this is how we tell them apart.
    const until = Date.now() + challengeWaitMs;
    while (Date.now() < until) {
      await page.waitForTimeout(1000);
      marks = await page.evaluate(CHALLENGE_JS).catch(() => marks);
      if (!marks.strong.length) break;
    }
    waited = Date.now() - (until - challengeWaitMs);
    if (!marks.strong.length) {
      try { await page.waitForLoadState('networkidle', { timeout: 8000 }); } catch { /* good enough */ }
    }
  }
  const challenge = [...new Set([...marks.strong, ...marks.weak])];

  // Give a normal page a moment to finish its first data fetch. networkidle is
  // the wrong default (an analytics beacon or a websocket keeps it from ever
  // firing), so it is a bounded wait, not a requirement.
  try { await page.waitForLoadState('networkidle', { timeout: 5000 }); } catch { /* fine */ }

  const html = await page.content().catch(() => '');
  const title = await page.title().catch(() => '');
  const text = await page.evaluate(`(() => {
    const c = document.body ? document.body.cloneNode(true) : null;
    if (!c) return '';
    for (const el of [...c.querySelectorAll('script,style,noscript,template')]) el.remove();
    return (c.innerText || c.textContent || '').replace(/[ \\t]+/g, ' ')
      .replace(/\\n\\s*\\n\\s*\\n+/g, '\\n\\n').trim();
  })()`).catch(() => '');
  const links = await page.evaluate(`([...document.querySelectorAll('a[href]')].slice(0,300)
      .map(a => ({text:(a.textContent||'').trim().slice(0,120), href:a.href||''}))
      .filter(l => l.href && !l.href.startsWith('javascript:')))`).catch(() => []);

  if (input.domPath && html) {
    try { writeFileSync(input.domPath, html); } catch { /* disk full is the caller's problem */ }
  }
  if (input.shotPath) {
    try {
      await page.screenshot({ path: input.shotPath, type: 'jpeg', quality: 55, fullPage: false });
    } catch { /* a page that will not paint still has a DOM worth returning */ }
  }

  emit({
    ok: true,
    status: mainStatus,
    url: page.url(),
    title: (title || '').slice(0, 300),
    html: html.slice(0, 2_000_000),
    text: (text || '').slice(0, 400_000),
    text_len: (text || '').length,
    links,
    headers: mainHeaders,
    redirects,
    console: consoleLog,
    requests,
    challenge,
    challenge_strong: marks.strong,
    challenge_waited_ms: waited,
    proxied: !!input.proxy,
    mode,
    stealth,
    ua: uaUsed,
  });
} catch (e) {
  emit({
    ok: false,
    why: String(e?.message || e).split('\n')[0].slice(0, 400),
    status: mainStatus,
    console: consoleLog,
    requests,
  });
} finally {
  // Attached: close our tab and drop the CDP socket, nothing else. Launched:
  // the browser existed only for this page, so it goes with it.
  try {
    if (attached) { await page?.close(); await browser?.close(); }
    else { await browser?.close({ reason: 'ladder step finished' }); }
  } catch { /* already gone */ }
}
