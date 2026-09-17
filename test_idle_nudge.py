"""Run the dashboard idle-nudge state machine in JavaScript."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


DRIVER = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const region=js.slice(js.indexOf('const lastStatus={}'),js.indexOf('// Local chat messages mirror'));
let nextTimer=1,chimes=0;
const timers=new Map(),storage=new Map();
const noop=()=>{};
const context=vm.createContext({
  console,
  sessions:[{name:'alpha'},{name:'beta'}],
  localStorage:{
    getItem:key=>storage.has(key)?storage.get(key):null,
    setItem:(key,value)=>storage.set(key,value),
  },
  document:{addEventListener:noop,getElementById:()=>null,querySelectorAll:()=>[]},
  window:{addEventListener:noop},
  esc:value=>String(value),
  setTimeout:(callback,delay)=>{
    const id=nextTimer++;
    timers.set(id,{callback,delay});
    return id;
  },
  clearTimeout:id=>timers.delete(id),
});
vm.runInContext(region,context);
context.playCompletionChime=()=>{chimes++};
const names=mode=>vm.runInContext(`_idleNudgeNames('${mode}')`,context);
const fireNext=()=>{
  const next=timers.entries().next().value;
  if(!next)throw new Error('expected a pending idle-nudge timer');
  const [id,timer]=next;
  timers.delete(id);
  timer.callback();
  return timer.delay;
};

context.setIdleNudgeMode('light');
context.trackSessionStatus('alpha','busy');
context.trackSessionStatus('alpha','idle');
const lightScheduled=timers.size;
const lightDelay=fireNext();
const lightRepeated=timers.size;
context._acknowledgeCompletion('alpha');
const lightAfterView={timers:timers.size,names:names('light')};

context.trackSessionStatus('alpha','busy');
context.trackSessionStatus('alpha','idle');
context.trackSessionStatus('beta','busy');
context.trackSessionStatus('beta','idle');
context._clearIdleNudgeForNewWork('alpha');
const lightAfterNewMessage={timers:timers.size,names:names('light')};
context._clearIdleNudgeForNewWork('beta');
const lightAfterAllMessages={timers:timers.size,names:names('light')};

context.setIdleNudgeMode('high');
context.trackSessionStatus('beta','busy');
context.trackSessionStatus('beta','idle');
context._acknowledgeCompletion('beta');
const highAfterView={timers:timers.size,names:names('high')};
context._clearIdleNudgeForNewWork('beta');
const highAfterNewMessage={timers:timers.size,names:names('high')};
context.trackSessionStatus('beta','busy');
context.trackSessionStatus('beta','idle');
context._acknowledgeCompletion('beta');
context.trackSessionStatus('beta','busy');
const highAfterWork={timers:timers.size,names:names('high')};

process.stdout.write(JSON.stringify({
  lightScheduled,lightDelay,lightRepeated,chimes,lightAfterView,
  lightAfterNewMessage,lightAfterAllMessages,highAfterView,
  highAfterNewMessage,highAfterWork,savedMode:storage.get('idleNudgeMode'),
}));
"""

PARSE_DRIVER = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const scriptPattern=new RegExp('<script[^>]*>([\\s\\S]*?)</script>','g');
const scripts=[...html.matchAll(scriptPattern)].map(match=>match[1]);
scripts.forEach((script,index)=>new vm.Script(script,{filename:'dashboard-inline-'+index+'.js'}));
process.stdout.write(String(scripts.length));
"""


def test_dashboard_inline_javascript_parses():
    result = subprocess.run(
        [NODE, "-e", PARSE_DRIVER, str(APP)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout == "1"


def test_light_and_high_clear_on_their_distinct_conditions():
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(APP)],
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    state = json.loads(result.stdout)
    assert state["lightScheduled"] == 1
    assert state["lightDelay"] == 20_000
    assert state["lightRepeated"] == 1
    assert state["chimes"] == 1
    assert state["lightAfterView"] == {"timers": 0, "names": []}
    assert state["lightAfterNewMessage"] == {"timers": 1, "names": ["beta"]}
    assert state["lightAfterAllMessages"] == {"timers": 0, "names": []}
    assert state["highAfterView"] == {"timers": 1, "names": ["beta"]}
    assert state["highAfterNewMessage"] == {"timers": 0, "names": []}
    assert state["highAfterWork"] == {"timers": 0, "names": []}
    assert state["savedMode"] == "high"


MODES_DRIVER = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8');
const quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const region=js.slice(js.indexOf('const lastStatus={}'),js.indexOf('// Local chat messages mirror'));
function run(saved,setup){
  const storage=new Map();
  if(saved!==null)storage.set('idleNudgeMode',saved);
  const noop=()=>{};
  const out={chimes:0,beeps:[],toasts:0};
  const ctx=vm.createContext({
    console,sessions:[{name:'alpha',last_turn_end:0,cache_ttl:3600,activity_status:'idle'}],
    localStorage:{getItem:k=>storage.has(k)?storage.get(k):null,setItem:(k,v)=>storage.set(k,v)},
    document:{addEventListener:noop,getElementById:()=>null,querySelectorAll:()=>[]},
    window:{addEventListener:noop},esc:v=>String(v),setTimeout:()=>1,clearTimeout:noop,
    CACHE_ALERT_LEAD:900,CACHE_PUSH_LEAD:600,sessionLabel:s=>s.name,_fmtAgo:x=>String(Math.round(x)),
    showToast:()=>{out.toasts++},
  });
  vm.runInContext(region,ctx);
  ctx.playCompletionChime=()=>{out.chimes++};
  ctx.playCacheAlertBeeps=n=>{out.beeps.push(n)};
  if(setup)setup(ctx,out);
  out.mode=ctx.getIdleNudgeMode();
  return out;
}
const idleAt=mins=>ctx=>{ctx.sessions[0].last_turn_end=Date.now()/1000-mins*60;vm.runInContext("lastStatus.alpha='idle'",ctx);};
const results={
  fresh:run(null).mode,
  legacy:run('adhd').mode,
  junk:run('loud').mode,
  lightBeeps:run('light',(c,o)=>{idleAt(46)(c);c._checkCacheExpiry();c._checkCacheExpiry();}).beeps,
  highBeeps:run('high',(c,o)=>{idleAt(46)(c);c._checkCacheExpiry();}).beeps,
  offBeeps:run('off',(c,o)=>{idleAt(46)(c);c._checkCacheExpiry();}),
  tooEarly:run('light',(c,o)=>{idleAt(40)(c);c._checkCacheExpiry();}).beeps,
  alreadyCold:run('light',(c,o)=>{idleAt(61)(c);c._checkCacheExpiry();}).beeps,
  offIsSilent:run('off',(c,o)=>{vm.runInContext('_completionWatch.alpha=true',c);
    c.trackSessionStatus('alpha','busy');c.trackSessionStatus('alpha','idle');}).chimes,
  lightChimes:run('light',(c,o)=>{vm.runInContext('_completionWatch.alpha=true',c);
    c.trackSessionStatus('alpha','busy');c.trackSessionStatus('alpha','idle');}).chimes,
};
process.stdout.write(JSON.stringify(results));
"""


def _modes():
    result = subprocess.run([NODE, "-e", MODES_DRIVER, str(APP)],
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_light_is_the_default_and_adhd_is_now_high():
    r = _modes()
    assert r["fresh"] == "light"
    assert r["legacy"] == "high", "a browser that saved ADHD keeps its behaviour as High"
    assert r["junk"] == "light"


def test_the_cache_warning_is_four_beeps_on_light_and_eight_on_high():
    r = _modes()
    assert r["lightBeeps"] == [4], "once per turn, not once per tick"
    assert r["highBeeps"] == [8]


def test_the_cache_warning_fires_fifteen_minutes_out_and_not_after_cold():
    r = _modes()
    assert r["tooEarly"] == []
    assert r["alreadyCold"] == []


def test_off_means_completely_silent():
    r = _modes()
    assert r["offBeeps"]["beeps"] == [] and r["offBeeps"]["toasts"] == 0
    assert r["offIsSilent"] == 0, "Off must not even play the one completion chime"
    assert r["lightChimes"] == 1


def test_the_selector_offers_off_light_high():
    src = APP.read_text()
    assert "b('off','Off')+b('light','Light')+b('high','High')" in src
    assert "ADHD" not in src[src.index("function idleNudgeSeg"):src.index("function syncIdleNudgeUI")]
