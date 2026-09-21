// Drives the REAL dashboard page script in node against a stub DOM and a stub
// API, and asserts the thing that was broken: a session going busy -> idle must
// reach the pill, the session object and the nav dot off the ten-second poll
// alone, with no page reload.
const fs = require('fs');

const nodes = new Map();          // id -> recorded state
function rec(id) {
  if (!nodes.has(id)) nodes.set(id, {id, className:'', innerHTML:'', textContent:'', title:'', style:{}});
  return nodes.get(id);
}
function mkEl(id) {
  const state = id ? rec(id) : {id:'', className:'', innerHTML:'', textContent:'', title:'', style:{}};
  const kids = [];
  const self = new Proxy(function(){}, {
    get(t, k) {
      if (k in state) return state[k];
      if (k === 'classList') return {
        add(c){ state.className = (state.className+' '+c).trim() },
        remove(c){ state.className = state.className.split(/\s+/).filter(x=>x&&x!==c).join(' ') },
        toggle(c, on){ on ? this.add(c) : this.remove(c) },
        contains(c){ return state.className.split(/\s+/).includes(c) },
      };
      if (k === 'dataset') return {};
      if (k === 'children' || k === 'childNodes') return kids;
      if (k === 'appendChild') return n => { kids.push(n); return n };
      if (k === 'removeChild') return n => n;
      if (k === 'remove') return () => { state.removed = true };
      // A class selector really searches the children (that is how the chat
      // typing line is found, and null there is meaningful). Anything else is a
      // structural lookup the page expects to succeed, so hand back a stub.
      if (k === 'querySelector') return sel =>
        String(sel).startsWith('.')
          ? (kids.find(c => (c.className||'').split(/\s+/).includes(String(sel).slice(1))) || null)
          : mkEl();
      if (k === 'querySelectorAll') return () => [];
      if (k === 'closest') return () => null;
      if (k === 'addEventListener' || k === 'removeEventListener') return () => {};
      if (k === 'parentNode' || k === 'parentElement' || k === 'nextSibling') return null;
      if (k === 'scrollHeight' || k === 'scrollTop' || k === 'offsetWidth') return 0;
      if (k === 'hidden' || k === 'checked' || k === 'disabled') return false;
      if (typeof k === 'symbol') return undefined;
      return mkEl();
    },
    set(t, k, v) {
      state[k] = v;
      if (k === 'textContent') state.innerHTML = String(v)
        .replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      if (k === 'innerHTML') state.textContent = String(v).replace(/<[^>]*>/g,'');
      return true;
    },
    apply() { return mkEl() },
  });
  return self;
}

// ── the API this page will see ────────────────────────────────────────────────
let PHASE = 'busy';
let REFRESH_BROKEN = false;
const SESSIONS = [
  {name:'alpha', display_name:'alpha', windows:1, attached:false, cwd:'/tmp', messages:[],
   activity_status:'busy', activity_detail:'Working', title:'', model:'opus', effort:'high'},
  {name:'beta',  display_name:'beta',  windows:1, attached:false, cwd:'/tmp', messages:[],
   activity_status:'idle', activity_detail:'', title:'', model:'opus', effort:'high'},
];
let GAMMA='busy';
let statusFor = () => SESSIONS.map(s => ({
  name: s.name, display_name: s.display_name,
  activity_status: s.name === 'alpha' ? PHASE : 'idle',
  activity_detail: s.name === 'alpha' && PHASE === 'busy' ? 'Working' : '',
  model: s.model, effort: s.effort, cache_keepalive:false, autopush_mode:'off',
  context_tokens: 1000, context_limit: 200000, cache_ttl: 3600, last_turn_end: 0,
}));
const USAGE = {fetched_at: Date.now()/1000, account:{email:'nimo@grabo.com', plan:'Max 20x'},
               five_hour:{utilization:100, resets_at:new Date(Date.now()+36e5).toISOString()},
               seven_day:{utilization:29,  resets_at:new Date(Date.now()+4.2*864e5).toISOString()}};
const calls = [];
global.fetch = async (url) => {
  calls.push(String(url));
  const u = String(url);
  let body = {};
  if (u.includes('/refresh')) {
    if (REFRESH_BROKEN) throw new Error('network blip');
    const nm = u.split('/api/sessions/')[1].split('/')[0];
    const base = SESSIONS.find(x => x.name === nm);
    const cur  = statusFor().find(x => x.name === nm);
    body = Object.assign({}, base, cur);
  }
  else if (u.includes('/api/sessions-fast') || u.includes('/api/sessions')) body = SESSIONS;
  else if (u.includes('/api/status')) body = statusFor();
  else if (u.includes('/api/usage/limits')) body = USAGE;
  else if (u.includes('/api/me')) body = {username:'Nimo', simple:false, team_mode:false};
  else if (u.includes('/api/auth')) body = {loggedIn:true, email:'nimo@grabo.com',
                                            subscriptionType:'max', plan:'Max 20x'};
  else if (u.match(/\/api\/(projects|users|browser|login-health|stats|skills)/)) body =
    u.includes('login-health') ? {account:{}, stale_count:0, sessions:[]} : [];
  return {ok:true, status:200, json:async()=>body, text:async()=>JSON.stringify(body),
          headers:{get:()=>null}};
};

// ── DOM / browser stubs ───────────────────────────────────────────────────────
global.window = global;
global.document = new Proxy({
  getElementById: id => mkEl(id),
  querySelector: () => null, querySelectorAll: () => [],
  createElement: () => mkEl(), createTextNode: () => mkEl(),
  addEventListener(){}, removeEventListener(){},
  body: mkEl(), head: mkEl(), documentElement: mkEl(),
  cookie:'', title:'', readyState:'complete', visibilityState:'visible', hidden:false,
}, {get(t,k){ return k in t ? t[k] : mkEl() }});
const store = {getItem:()=>null,setItem(){},removeItem(){},clear(){},length:0};
global.localStorage = global.sessionStorage = store;
global.navigator = {userAgent:'node', language:'en-US', clipboard:{writeText:async()=>{}},
                    mediaDevices:{getUserMedia:async()=>{throw new Error('no')}}, onLine:true};
global.location = {href:'http://x/', origin:'http://x', hash:'', pathname:'/', search:'',
                   protocol:'http:', host:'x', reload(){}, replace(){}, assign(){}};
global.history = {replaceState(){}, pushState(){}, back(){}};
global.WebSocket = global.EventSource = function(){ return new Proxy({},{get:()=>()=>{}}) };
global.Audio = function(){ return {play:async()=>{}, pause(){}, addEventListener(){}} };
global.AudioContext = global.webkitAudioContext = function(){
  return new Proxy({},{get:()=>()=>new Proxy({},{get:()=>()=>{}})}) };
global.Notification = function(){}; global.Notification.permission='default';
global.matchMedia = () => ({matches:false, addListener(){}, addEventListener(){}, media:''});
global.requestAnimationFrame = cb => setTimeout(cb,0); global.cancelAnimationFrame = ()=>{};
global.getComputedStyle = () => new Proxy({},{get:()=>''});
global.alert=()=>{}; global.confirm=()=>false; global.prompt=()=>null;
global.scrollTo=()=>{}; global.open=()=>null;
global.ResizeObserver = global.MutationObserver = global.IntersectionObserver =
  function(){ return {observe(){},disconnect(){},unobserve(){}} };
global.addEventListener=()=>{}; global.removeEventListener=()=>{};
global.innerWidth=1280; global.innerHeight=800; global.devicePixelRatio=1;
global.speechSynthesis={speak(){},cancel(){},getVoices:()=>[]};
const realST = setTimeout;
global.setTimeout = (fn,ms) => { const t = realST(()=>{},0); t.unref&&t.unref(); return t; };
const realSI = setInterval;
global.setInterval = () => 0; global.clearInterval = ()=>{}; global.clearTimeout = ()=>{};

const keepAlive = setInterval(()=>{}, 1000);
let bootErr = null;
process.on('uncaughtException', e => { bootErr = e });
const vm = require('vm');
try { vm.runInThisContext(fs.readFileSync(process.argv[2],'utf8')); } catch (e) { bootErr = e }

const fail = [];
const check = (name, cond, detail) => {
  console.log((cond ? '  PASS  ' : '  FAIL  ') + name + (detail ? '   [' + detail + ']' : ''));
  if (!cond) fail.push(name);
};

realST(async () => {
 try{
  if (bootErr) { console.log('BOOT FAILED:\n' + (bootErr.stack||bootErr)); process.exit(1) }
  console.log('page script booted clean\n');

  const G = expr => vm.runInThisContext(expr);
  // renderDetail paints the panel as one innerHTML string on #main, so assert on
  // the markup it produced, not on a node that string never created.
  const pillInMarkup = name => {
    const m = String(rec('main').innerHTML || '')
      .match(new RegExp('class="status-pill ([^"]*)"[^>]*id="status-' + name + '"'));
    return m ? m[1] : '(pill not in markup)';
  };

  vm.runInThisContext("selectedSession='alpha'");
  await loadAll();                       // first paint: alpha is busy
  await new Promise(r => realST(r, 60));

  console.log('--- after load, alpha busy ---');
  check('sessions[alpha].activity_status === busy',
        G("sessions").find(s=>s.name==='alpha').activity_status === 'busy');
  check('pill shows busy', /\bbusy\b/.test(rec('status-alpha').className),
        rec('status-alpha').className);

  // The turn ends. Only the ten-second poll runs — no reload, no refreshOne call
  // the user makes by hand.
  PHASE = 'idle';
  await pollStatus();
  await new Promise(r => realST(r, 80));

  console.log('\n--- after one poll, alpha idle (THE BUG) ---');
  const a = G("sessions").find(s=>s.name==='alpha');
  check('poll wrote the fresh status back onto the session object',
        a.activity_status === 'idle', 'activity_status=' + a.activity_status);
  check('lastStatus agrees', G("lastStatus")['alpha'] === 'idle', 'lastStatus=' + G("lastStatus")['alpha']);
  check('pill class lost busy, gained idle',
        /\bidle\b/.test(rec('status-alpha').className) &&
        !/\bbusy\b/.test(rec('status-alpha').className), rec('status-alpha').className);
  check('nav dot lost busy',
        !/\bbusy\b/.test(rec('nav-dot-alpha').className), rec('nav-dot-alpha').className);

  // Re-rendering (switching tab / session) must not resurrect the old status.
  renderDetail();
  await new Promise(r => realST(r, 40));
  console.log('\n--- after a re-render (what switching tabs does) ---');
  check('renderDetail() rebuilt the pill markup as idle, not busy',
        /\bidle\b/.test(pillInMarkup('alpha')) && !/\bbusy\b/.test(pillInMarkup('alpha')),
        pillInMarkup('alpha'));

  // The reported path: the session that went idle was NOT the one on screen, and
  // renderNav repaints every tab's dot from the session objects. Switching to
  // that tab is what showed a red "Working" for a turn that was long over.
  console.log('\n--- the other tab: renderNav + switching to a session that went idle ---');
  renderNav();
  await new Promise(r => realST(r, 40));
  check('renderNav painted alpha\'s tab dot idle, not busy',
        !/\bbusy\b/.test(rec('nav-dot-alpha').className), rec('nav-dot-alpha').className);
  vm.runInThisContext("selectedSession='beta'"); renderDetail();
  await new Promise(r => realST(r, 40));
  vm.runInThisContext("selectedSession='alpha'"); renderDetail();
  await new Promise(r => realST(r, 40));
  check('switching back to alpha renders idle, not a red Working',
        /\bidle\b/.test(pillInMarkup('alpha')) && !/\bbusy\b/.test(pillInMarkup('alpha')),
        pillInMarkup('alpha'));

  // ── scenario 2 ────────────────────────────────────────────────────────────
  console.log('\n--- gamma goes idle while its /refresh call fails ---');
  SESSIONS.push({name:'gamma', display_name:'gamma', windows:1, attached:false, cwd:'/tmp',
                 messages:[], activity_status:'busy', activity_detail:'Working', title:'',
                 model:'opus', effort:'high'});
  const statusSrc = statusFor;
  GAMMA = 'busy';
  statusFor = () => SESSIONS.map(x => ({
    name:x.name, display_name:x.display_name,
    activity_status: x.name==='gamma' ? GAMMA : (x.name==='alpha' ? PHASE : 'idle'),
    activity_detail:'', model:x.model, effort:x.effort, cache_keepalive:false,
    autopush_mode:'off', context_tokens:1000, context_limit:200000, cache_ttl:3600,
    last_turn_end:0 }));
  vm.runInThisContext("selectedSession='beta'");
  await loadAll(); await new Promise(r => realST(r, 60));
  GAMMA = 'idle'; REFRESH_BROKEN = true;
  await pollStatus(); await new Promise(r => realST(r, 80));
  const g = G("sessions").find(x=>x.name==='gamma');
  check('gamma\'s session object went idle even though /refresh failed',
        g.activity_status === 'idle', 'activity_status=' + g.activity_status);
  vm.runInThisContext("selectedSession='gamma'"); renderDetail();
  await new Promise(r => realST(r, 40));
  check('switching to gamma renders idle, not a stuck red Working',
        /\bidle\b/.test(pillInMarkup('gamma')) && !/\bbusy\b/.test(pillInMarkup('gamma')),
        pillInMarkup('gamma'));
  REFRESH_BROKEN = false;

  console.log('\n--- usage bars ---');
  await refreshUsageLimits();
  await new Promise(r => realST(r, 40));
  check('7d percent painted in the header', rec('nav-usage-7d-pct').textContent === '29%',
        rec('nav-usage-7d-pct').textContent);
  check('7d reset line reads "Reset: 4d"', rec('nav-usage-7d-reset').textContent === 'Reset: 4d',
        rec('nav-usage-7d-reset').textContent);
  check('no 5h widget left in the header', !nodes.has('nav-usage-5h-pct'));
  renderAuthPanel();
  await new Promise(r => realST(r, 40));
  check('opening the account panel paints the 5h bar at 100%',
        rec('auth-usage-5h-pct').textContent === '100%', rec('auth-usage-5h-pct').textContent);
  check('5h row carries its own reset countdown',
        /^Reset: /.test(rec('auth-usage-5h-reset').textContent),
        rec('auth-usage-5h-reset').textContent);

  console.log('\n' + (fail.length ? fail.length + ' CHECK(S) FAILED' : 'all checks passed'));
  process.exit(fail.length ? 1 : 0);
 }catch(e){ console.log('TEST ERROR:\n'+(e.stack||e)); process.exit(1) }
}, 300);
