#!/usr/bin/env node
// L1 of the browser ladder: the light rung.
//
// A DOM and a JavaScript engine, and nothing else. No compositor, no GPU, no
// network stack of its own beyond fetch/XHR: happy-dom parses the HTML L0
// already downloaded, runs the page's scripts against a real DOM, and hands
// back what the document looked like once the scripts settled. Measured on this
// box at 40-70 MB against 250-400 MB for a Chromium, which is the whole reason
// the rung exists: most "the page is empty without JS" sites are a shell plus
// one fetch of JSON, and that does not need a browser.
//
// What it deliberately cannot do: negotiate a TLS/bot challenge. There is no
// real browser here to fingerprint, so anything that asks for one has to go up
// to L2. That is a feature of the ladder, not a defect of this rung.
//
// stdin  {url, html, ua, timeoutMs, domPath, maxBytes}
// stdout {ok, title, html, text, links, console, requests, ms, engine, why}
//
// Untrusted JS runs in here, so the caller launches us with a stripped
// environment (no API keys in process.env) and a small heap cap. Everything is
// best effort: a page that throws still returns whatever DOM it managed.

import { readFileSync, writeFileSync } from 'node:fs';

const started = Date.now();
let finished = false;

function emit(payload, code = 0) {
  if (finished) return;
  finished = true;
  payload.ms = Date.now() - started;
  payload.engine = 'happy-dom';
  try { process.stdout.write(JSON.stringify(payload)); } catch { /* stdout gone */ }
  // A page can leave timers and open sockets behind that would keep node alive
  // long past the answer being ready. We have what we came for; exit hard.
  process.exit(code);
}

function readStdin() {
  try { return JSON.parse(readFileSync(0, 'utf8')); } catch (e) { return null; }
}

const input = readStdin();
if (!input || !input.url) emit({ ok: false, why: 'no input on stdin' }, 2);

const timeoutMs = Math.max(2000, Number(input.timeoutMs) || 20000);
const maxBytes = Math.max(50_000, Number(input.maxBytes) || 4_000_000);

// Hard ceiling. happy-dom's waitUntilComplete() waits on the page's own
// promises, and a page with a polling loop never completes: so the wall clock,
// not the page, decides when we are done.
const guard = setTimeout(() => {
  try {
    const snap = snapshot('timeout');
    snap.why = `page still busy after ${timeoutMs}ms: returning what it had rendered`;
    emit(snap);
  } catch (e) {
    emit({ ok: false, why: `timeout after ${timeoutMs}ms` });
  }
}, timeoutMs);
guard.unref?.();

const consoleLog = [];
const requests = [];
const pushConsole = (level, args) => {
  if (consoleLog.length >= 200) return;
  try {
    consoleLog.push({
      level,
      text: args.map(a => {
        if (typeof a === 'string') return a;
        try { return JSON.stringify(a); } catch { return String(a); }
      }).join(' ').slice(0, 500),
    });
  } catch { /* a console arg whose toString throws is not our problem */ }
};
const pageConsole = {
  log: (...a) => pushConsole('log', a),
  info: (...a) => pushConsole('info', a),
  warn: (...a) => pushConsole('warn', a),
  error: (...a) => pushConsole('error', a),
  debug: (...a) => pushConsole('debug', a),
  trace: () => {}, dir: (...a) => pushConsole('dir', a), table: () => {},
  group: () => {}, groupEnd: () => {}, groupCollapsed: () => {},
  time: () => {}, timeEnd: () => {}, timeLog: () => {}, count: () => {},
  countReset: () => {}, assert: () => {}, clear: () => {},
};

let window = null;

function textOf(doc) {
  try {
    // Script and style text is not page text, and leaving it in is what makes an
    // empty SPA shell look like a full page to the "did we get content?" check.
    const clone = doc.cloneNode(true);
    for (const el of [...clone.querySelectorAll('script,style,noscript,template')]) el.remove();
    return (clone.body?.textContent || '').replace(/[ \t\r\f\v]+/g, ' ')
      .replace(/\n\s*\n\s*\n+/g, '\n\n').trim();
  } catch {
    return '';
  }
}

function snapshot(reason) {
  const doc = window?.document;
  if (!doc) return { ok: false, why: reason || 'no document' };
  let html = '';
  try { html = doc.documentElement?.outerHTML || ''; } catch { html = ''; }
  if (html.length > maxBytes) html = html.slice(0, maxBytes);
  const text = textOf(doc);
  let links = [];
  try {
    links = [...doc.querySelectorAll('a[href]')].slice(0, 300).map(a => ({
      text: (a.textContent || '').trim().slice(0, 120),
      href: a.getAttribute('href') || '',
    })).filter(l => l.href && !l.href.startsWith('javascript:'));
  } catch { /* detached DOM */ }
  if (input.domPath && html) {
    try { writeFileSync(input.domPath, html); } catch { /* disk full is the caller's problem */ }
  }
  return {
    ok: true,
    status: 200,          // L0 already established the transport status
    title: (doc.title || '').slice(0, 300),
    html,
    text: text.slice(0, 400_000),
    text_len: text.length,
    links,
    console: consoleLog,
    requests: requests.slice(0, 200),
    why: reason || '',
  };
}

try {
  const { Window } = await import('happy-dom');
  window = new Window({
    url: input.url,
    width: 1920,
    height: 1080,
    console: pageConsole,
    settings: {
      // happy-dom 20 made script evaluation opt-IN. Without this line the rung
      // silently returns the shell it was handed and every SPA reads as "L1
      // could not render it", which sends a page a DOM would have finished all
      // the way up to a 300 MB Chromium. It cost an afternoon; leave it on.
      enableJavaScriptEvaluation: true,
      disableJavaScriptFileLoading: false,
      // CSS files buy nothing here: there is no layout and no paint, and each
      // one is a round trip. Scripts are the entire point, so they stay on.
      disableCSSFileLoading: true,
      enableImageFileLoading: false,
      disableComputedStyleRendering: true,
      handleDisabledFileLoadingAsSuccess: true,
      errorCapture: 'processLevel',
      // We know it is not a security boundary: that is why the caller strips
      // the environment before launching us. Saying so once is enough.
      suppressInsecureJavaScriptEnvironmentWarning: true,
      suppressCodeGenerationFromStringsWarning: true,
      fetch: { disableSameOriginPolicy: true },
      navigator: input.ua ? { userAgent: input.ua } : {},
      // A page that polls must not decide how long this rung runs.
      timer: { maxTimeout: Math.min(timeoutMs, 10000), maxIntervalTime: 5000,
               maxIntervalIterations: 20, preventTimerLoops: true },
    },
  });

  // Everything the page asks for after load is the interesting part of this
  // rung: it is what tells the human (and the classifier) whether the shell
  // filled itself in from an API or just sat there.
  const nativeFetch = window.fetch.bind(window);
  window.fetch = async (resource, init) => {
    const url = String(resource?.url || resource || '');
    const row = { url: url.slice(0, 400), method: (init?.method || 'GET').toUpperCase(), status: 0 };
    if (requests.length < 200) requests.push(row);
    try {
      const res = await nativeFetch(resource, init);
      row.status = res.status;
      return res;
    } catch (e) {
      row.status = -1;
      row.error = String(e?.message || e).slice(0, 200);
      throw e;
    }
  };

  window.document.write(input.html || '');
  window.document.close();

  await window.happyDOM.waitUntilComplete();
  clearTimeout(guard);
  const out = snapshot('');
  try { await window.happyDOM.close(); } catch { /* already torn down */ }
  emit(out);
} catch (e) {
  clearTimeout(guard);
  // A thrown page script is not a failed rung: the DOM up to that point is
  // often exactly what we wanted. Return it and let the classifier judge.
  const partial = window ? snapshot('page script threw') : { ok: false };
  partial.error = String(e?.message || e).slice(0, 400);
  if (!partial.ok) partial.why = partial.error;
  emit(partial);
}
