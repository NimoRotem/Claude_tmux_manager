"""Run the dashboard's idle-nudge state machine in JavaScript."""

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
  lastSubmittedDraft:{},
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

context.setIdleNudgeMode('adhd');
context.trackSessionStatus('beta','busy');
context.trackSessionStatus('beta','idle');
context._acknowledgeCompletion('beta');
const adhdAfterView={timers:timers.size,names:names('adhd')};
context._clearIdleNudgeForNewWork('beta');
const adhdAfterNewMessage={timers:timers.size,names:names('adhd')};
context.trackSessionStatus('beta','busy');
context.trackSessionStatus('beta','idle');
context._acknowledgeCompletion('beta');
context.trackSessionStatus('beta','busy');
const adhdAfterWork={timers:timers.size,names:names('adhd')};

process.stdout.write(JSON.stringify({
  lightScheduled,lightDelay,lightRepeated,chimes,lightAfterView,
  lightAfterNewMessage,lightAfterAllMessages,adhdAfterView,
  adhdAfterNewMessage,adhdAfterWork,savedMode:storage.get('idleNudgeMode'),
}));
"""


def test_light_and_adhd_clear_on_their_distinct_conditions():
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
    assert state["adhdAfterView"] == {"timers": 1, "names": ["beta"]}
    assert state["adhdAfterNewMessage"] == {"timers": 0, "names": []}
    assert state["adhdAfterWork"] == {"timers": 0, "names": []}
    assert state["savedMode"] == "adhd"
