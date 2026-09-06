"""Execute the knowledge-preserving close UI against deterministic fetch states."""

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
const region=js.slice(js.indexOf('function showDeleteModal'),js.indexOf('// ── Codex Auth'));
const modal={innerHTML:''};
const overlay={classList:{add(){},remove(){}}};
const status={textContent:''};
const elements={'modal-content':modal,'modal-overlay':overlay};
const scenario=process.argv[2];
let polls=0,loads=0;
const response=(statusCode,data)=>({ok:statusCode>=200&&statusCode<300,status:statusCode,json:async()=>data});
async function fetchStub(url,opts){
  if(opts&&opts.method==='DELETE'){
    if(scenario==='initial-503')return response(503,{error:'close controller unavailable'});
    if(scenario==='initial-response-loss')throw new Error('connection reset');
    if(scenario==='initial-verified-open')return response(409,{error:'close blocked',tab_state:'open'});
    return response(202,{job:{id:'close_abcdefghijklmnop',status:'running',phase:'capturing'}});
  }
  polls++;
  if(scenario==='transient-success'){
    if(polls<=6)throw new Error('network down');
    return response(200,{job:{id:'close_abcdefghijklmnop',status:'completed',phase:'complete',tab_state:'closed',spec_file:'TECHNICAL_SPEC.md'}});
  }
  if(scenario==='no-conversation')return response(200,{job:{id:'close_abcdefghijklmnop',status:'completed',phase:'complete',tab_state:'closed',archived:false}});
  if(scenario==='lost-status')return response(404,{error:'Close job not found'});
  return response(200,{job:{id:'close_abcdefghijklmnop',status:'failed',phase:'failed',tab_state:'open',error:'disk full'}});
}
const context=vm.createContext({
  console,Promise,Math,BASE:'',selectedSession:'demo',chatMessages:{demo:[]},
  _closeSessionRun:0,fetch:fetchStub,setTimeout:fn=>fn(),
  esc:value=>String(value),loadAll:async()=>{loads++},
  document:{getElementById:id=>{
    if(id.startsWith('session-close-job-'))return modal.innerHTML.includes(id)?{}:null;
    if(id.startsWith('session-close-status-'))return status;
    return elements[id]||null;
  }},
});
vm.runInContext(region,context);
context.deleteSession('demo').then(()=>process.stdout.write(JSON.stringify({
  html:modal.innerHTML,polls,loads,status:status.textContent,
  selectedSession:context.selectedSession,
  chatPreserved:Object.prototype.hasOwnProperty.call(context.chatMessages,'demo'),
})));
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


def test_transient_poll_failures_keep_tracking_until_close_completes():
    state = _run("transient-success")
    assert state["polls"] == 7
    assert state["loads"] == 1
    assert "Session closed safely" in state["html"]


def test_completed_close_without_conversation_does_not_claim_spec_write():
    state = _run("no-conversation")
    assert state["loads"] == 1
    assert "Session closed safely" in state["html"]
    assert "nothing to archive" in state["html"]
    assert "technical handoff was saved" not in state["html"]
    assert "TECHNICAL_SPEC.md" not in state["html"]


def test_unknown_status_never_claims_tab_is_open_or_offers_retry():
    state = _run("lost-status")
    assert "Close status needs confirmation" in state["html"]
    assert "Nothing was killed" not in state["html"]
    assert ">Retry<" not in state["html"]


@pytest.mark.parametrize("scenario", ["initial-503", "initial-response-loss"])
def test_unconfirmed_initial_delete_preserves_tabs_without_offering_retry(scenario):
    state = _run(scenario)
    assert state["polls"] == 0
    assert state["loads"] == 0
    assert state["selectedSession"] == "demo"
    assert state["chatPreserved"] is True
    assert "Close status needs confirmation" in state["html"]
    assert "verified open" not in state["html"]
    assert ">Retry<" not in state["html"]


def test_explicit_initial_verified_open_failure_offers_retry():
    state = _run("initial-verified-open")
    assert state["loads"] == 0
    assert state["selectedSession"] == "demo"
    assert state["chatPreserved"] is True
    assert "Session left open" in state["html"]
    assert "verified open" in state["html"]
    assert ">Retry<" in state["html"]


def test_verified_open_failure_offers_retry():
    state = _run("failed-open")
    assert "Session left open" in state["html"]
    assert "verified open" in state["html"]
    assert ">Retry<" in state["html"]
