"""Execute authoritative roster reconciliation against deterministic browser state."""

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
const region=js.slice(js.indexOf('function _resetSessionRuntimeState'),js.indexOf('async function loadAll(',js.indexOf('function _resetSessionRuntimeState')));
const scenario=process.argv[2];
const old={name:'same',incarnation:'runtime-old',logical_incarnation:'logical-a',activity_status:'idle',messages:[]};
const replacementLogical=scenario==='logical-replacement'?'logical-b':'logical-a';
const next={name:'same',incarnation:'runtime-new',logical_incarnation:replacementLogical,activity_status:'unknown',messages:[]};
const response=(status,data)=>({ok:status>=200&&status<300,status,json:async()=>data});
let fetchCalls=0,replaced=0,stopped=0;
async function fetchStub(){
  fetchCalls++;
  if(scenario==='transient-error')return response(503,{authoritative:false,sessions:[]});
  return response(200,{authoritative:true,generation:'next',sessions:[next]});
}
const context=vm.createContext({
  console,Promise,Map,Set,Array,String,Math,fetch:fetchStub,BASE:'',
  sessions:[old],selectedSession:'same',_sessionRosterGeneration:'old',_sessionRosterRequest:0,_sessionRosterPromise:null,
  _autopushRevision:new Map(),_autopushPending:new Set(),
  rawState:{same:{polling:true}},rawCache:{same:{text:'old terminal'}},chatMessages:{same:[{role:'user',text:'keep'}]},
  lastStatus:{same:'idle'},activeTabs:{same:'raw'},lastSubmittedDraft:{same:{text:'keep'}},
  _completionWatch:{same:true},_completedUnread:{same:true},_idleNudgeAdhdPending:{same:true},
  _uploadTab:{same:'chat'},_bracketedPaste:{same:true},_sessionClientEpoch:{same:0},_uploadRequests:{},
  draftText:{'chat-same':'unsent'},_composerAttachments:{'chat-same':[{}]},_composerUploadTasks:{},
  _recording:{},_mediaRec:{},_audioChunks:{},
  mainEl:{replaceChildren(){replaced++}},
  stopRawPolling(){stopped++},stopWatchdogPolling(){},saveDrafts(){},saveRawCache(){},
  renderNav(){},renderDetail(){},_syncIdleNudgeTimer(){},mergeChatMessages(){},
});
vm.runInContext(region,context);
(async()=>{
  const applied=await context.reconcileSessionRoster();
  process.stdout.write(JSON.stringify({
    applied,fetchCalls,replaced,stopped,
    sessions:context.sessions.map(row=>row.name),
    draft:context.draftText['chat-same']||'',
    attachments:(context._composerAttachments['chat-same']||[]).length,
    chats:(context.chatMessages.same||[]).length,
    raw:!!context.rawState.same,
    epoch:context._sessionClientEpoch.same||0,
  }));
})().catch(error=>{console.error(error);process.exit(1)});
"""


def _run(scenario: str) -> dict:
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(APP), scenario],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_non_authoritative_inventory_preserves_everything():
    state = _run("transient-error")
    assert state == {
        "applied": False,
        "fetchCalls": 1,
        "replaced": 0,
        "stopped": 0,
        "sessions": ["same"],
        "draft": "unsent",
        "attachments": 1,
        "chats": 1,
        "raw": True,
        "epoch": 0,
    }


def test_runtime_recovery_preserves_logical_state_but_resets_terminal():
    state = _run("runtime-recovery")
    assert state["applied"] is True
    assert state["draft"] == "unsent"
    assert state["attachments"] == 1
    assert state["chats"] == 1
    assert state["raw"] is False
    assert state["epoch"] == 0
    assert state["replaced"] == 1


def test_logical_replacement_purges_old_session_state():
    state = _run("logical-replacement")
    assert state["applied"] is True
    assert state["draft"] == ""
    assert state["attachments"] == 0
    assert state["chats"] == 0
    assert state["raw"] is False
    assert state["epoch"] == 1
    assert state["replaced"] == 1
