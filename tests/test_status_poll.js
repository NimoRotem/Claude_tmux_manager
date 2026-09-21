// Behavioural test for the dashboard's status-poll recovery, run against the
// REAL slice of page JS pulled out of the served page (not a re-typed copy).
const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const src = fs.readFileSync(process.argv[2] || __dirname + '/../app.py', 'utf8');
// The dashboard page is the largest inline script in app.py; the login page
// and the mobile shim are the other two.
const js = (src.match(/<script>[\s\S]*?<\/script>/g) || []).map(b => b.slice(8, -9)).sort((a, b) => b.length - a.length)[0];

const start = js.indexOf('let _pollInFlight=false;');
const tailMark = js.indexOf('if(_authPollCount%3===0)refreshNavStats();', start);
assert.ok(start > 0 && tailMark > start, 'could not locate the poll block');
const end = js.indexOf('\n}', tailMark) + 2;
const slice = js.slice(start, end);
assert.ok(slice.includes('_STATUS_URLS'), 'slice missing the URL list');
assert.ok(slice.includes('_recoverStatusFromSessions'), 'slice missing the recovery path');

// ---- harness ----------------------------------------------------------------
let now = 1000000;
const timers = [];
function fakeSetTimeout(fn, ms) { timers.push({ at: now + (ms || 0), fn }); return timers.length; }
function fakeClearTimeout(id) { if (timers[id - 1]) timers[id - 1].fn = null; }
function advance(ms) {
  const target = now + ms;
  for (;;) {
    const due = timers.filter(t => t.fn && t.at <= target).sort((a, b) => a.at - b.at)[0];
    if (!due) break;
    now = due.at;
    const fn = due.fn; due.fn = null;
    fn();
  }
  now = target;
}
const flush = () => new Promise(r => setImmediate(r));

const cls = new Set();
const statusInfoEl = {
  _text: '',
  set textContent(v) { this._text = v; },
  get textContent() { return this._text; },
  classList: {
    toggle: (c, on) => { on ? cls.add(c) : cls.delete(c); },
    remove: c => cls.delete(c),
    add: c => cls.add(c),
  },
};

const painted = [];          // every updateStatusPill call
let fetchLog = [];           // every url fetch() was asked for
let behaviour = {};          // url -> 'ok' | 'reject' | 'hang'
const STATUSES = () => [{ name: 'a', activity_status: 'idle', activity_detail: '' }];

function fakeFetch(url, opts) {
  fetchLog.push(url);
  const mode = behaviour[url] || 'ok';
  if (mode === 'reject') return Promise.reject(new TypeError('Failed to fetch'));
  if (mode === 'hang') {
    return new Promise((_res, rej) => {
      if (opts && opts.signal) opts.signal.addEventListener('abort', () => rej(new Error('AbortError')));
    });
  }
  return Promise.resolve({
    ok: true,
    headers: { get: () => 'build-1' },
    json: async () => STATUSES(),
  });
}

const ctx = {
  BASE: '',
  statusInfoEl,
  sessions: [{ name: 'a', activity_status: 'busy', activity_detail: 'Working' }],
  lastStatus: { a: 'busy' },
  selectedSession: 'a',
  _authPollCount: 0,
  fetch: fakeFetch,
  AbortController,
  setTimeout: fakeSetTimeout,
  clearTimeout: fakeClearTimeout,
  Date: { now: () => now },
  Math,
  console,
  _checkBuild() {},
  refreshOne() {},
  trackSessionStatus() {},
  updateStatusPill(name, status) { painted.push([name, status]); },
  updateFavicon() {},
  _paintModelBadge() {},
  _paintEffortBadge() {},
  _paintContext() {},
  _paintIdleSince() {},
  renderNav() {},
  checkClaudeAuth() {},
  refreshNavStats() {},
};
vm.createContext(ctx);
vm.runInContext(slice, ctx);
// A top-level `let` is a lexical binding, not a property of the context object,
// so it is invisible as ctx.<name>. Read and write it through the script scope.
const ev = expr => vm.runInContext(expr, ctx);

// ---- cases ------------------------------------------------------------------
(async () => {
  // 1. Happy path: the primary URL is used and the pill is painted.
  await ctx.pollStatus(); await flush();
  assert.deepStrictEqual(fetchLog, ['/api/status'], 'first poll should use /api/status');
  assert.deepStrictEqual(painted[0], ['a', 'idle'], 'busy -> idle should paint');
  assert.ok(!cls.has('poll-stalled'), 'a good poll must not show the stall banner');
  console.log('ok  1  healthy poll uses /api/status and paints the new status');

  // 2. The primary URL is silently dropped, as it was on builder5: three misses
  //    and the page must move to the twin and recover on its own.
  behaviour['/api/status'] = 'reject';
  fetchLog = [];
  for (let i = 0; i < 3; i++) { await ctx.pollStatus(); await flush(); advance(2000); await flush(); }
  const used = fetchLog.filter(u => u === '/api/activity').length;
  assert.ok(used >= 1, 'after three misses the poll must try /api/activity, saw: ' + fetchLog.join(','));
  const before = painted.length;
  await ctx.pollStatus(); await flush();
  assert.ok(painted.length > before, 'the twin URL must paint statuses');
  console.log('ok  2  three dropped /api/status calls fail over to /api/activity');

  // 3. A request that never answers must not hold the in-flight latch for ever.
  behaviour['/api/status'] = 'hang'; behaviour['/api/activity'] = 'hang';
  ev('_statusUrlIdx=0;_pollFails=0;');
  const p = ctx.pollStatus();
  await flush();
  assert.strictEqual(ev('_pollInFlight'), true, 'latch should be held while in flight');
  advance(8001); await flush(); await p;
  assert.strictEqual(ev('_pollInFlight'), false, 'the abort must release the latch');
  console.log('ok  3  a hung poll is aborted at 8s and releases the latch');

  // 4. A page that has not had a good poll for a while says so, out loud.
  now += 40000;
  ev('_pollFails=4;');
  ctx._paintPollHealth();
  assert.ok(cls.has('poll-stalled'), 'the stall class must be set');
  assert.ok(/stalled/.test(statusInfoEl.textContent), 'header must name the stall: ' + statusInfoEl.textContent);
  console.log('ok  4  a stalled poll is announced in the header ("' + statusInfoEl.textContent + '")');

  // 5. Last-resort recovery off the session list, and the header stops crying wolf.
  behaviour = {};
  fetchLog = [];
  await ctx._recoverStatusFromSessions(); await flush();
  assert.deepStrictEqual(fetchLog, ['/api/sessions-fast'], 'recovery must use the session list');
  assert.strictEqual(statusInfoEl.textContent, 'Live status on the backup path');
  console.log('ok  5  recovery falls back to /api/sessions-fast and says which path it is on');

  console.log('\nall 5 passed');
})().catch(e => { console.error('FAIL:', e.message); process.exit(1); });
