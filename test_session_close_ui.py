"""Execute the direct session close UI against deterministic fetch states."""

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
let polls=0,loads=0,closes=0;
const response=(statusCode,data)=>({ok:statusCode>=200&&statusCode<300,status:statusCode,json:async()=>data});
async function fetchStub(url,opts){
  if(opts&&opts.method==='DELETE'){
    if(scenario==='initial-503')return response(503,{error:'close controller unavailable'});
    if(scenario==='initial-response-loss')throw new Error('connection reset');
    if(scenario==='invalid-success')return response(200,{});
    if(scenario==='wrong-session')return response(200,{ok:true,killed:'another-session'});
    if(scenario==='legacy-job')return response(202,{ok:true,job:{id:'old-job'}});
    if(scenario==='dismissed')context._closeSessionRun++;
    return response(200,{ok:true,killed:'demo'});
  }
  polls++;
  throw new Error('Close must not poll for a summary');
}

const context=vm.createContext({
  console,Promise,Math,BASE:'',selectedSession:'demo',chatMessages:{demo:[]},
  _closeSessionRun:0,fetch:fetchStub,setTimeout:fn=>fn(),
  esc:value=>String(value),loadAll:async()=>{loads++},closeModal:()=>{closes++;context._closeSessionRun++},
  document:{getElementById:id=>{
    if(id.startsWith('session-close-job-'))return modal.innerHTML.includes(id)?{}:null;
    if(id.startsWith('session-close-status-'))return status;
    return elements[id]||null;
  }},
});
vm.runInContext(region,context);
context.showDeleteModal('demo');
const confirmation=modal.innerHTML;
context.deleteSession('demo').then(()=>process.stdout.write(JSON.stringify({
  html:modal.innerHTML,confirmation,polls,loads,closes,status:status.textContent,
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


def test_close_confirmation_has_no_summary_or_save_step():
    state = _run("success")
    assert "Close session demo?" in state["confirmation"]
    assert "Close session" in state["confirmation"]
    assert "Cancel" in state["confirmation"]
    assert "summar" not in state["confirmation"].lower()
    assert "TECHNICAL_SPEC" not in state["confirmation"]


def test_confirmed_close_dismisses_dialog_and_refreshes_without_polling():
    state = _run("success")
    assert state["polls"] == 0
    assert state["loads"] == 1
    assert state["closes"] == 1
    assert state["selectedSession"] is None
    assert state["chatPreserved"] is False


@pytest.mark.parametrize("scenario", ["initial-503", "initial-response-loss", "invalid-success", "wrong-session", "legacy-job"])
def test_unconfirmed_close_preserves_local_state_and_offers_refresh(scenario):
    state = _run(scenario)
    assert state["polls"] == 0
    assert state["loads"] == 1, "An uncertain DELETE must refresh the actual roster without retrying deletion"
    assert state["closes"] == 0
    assert state["selectedSession"] == "demo"
    assert state["chatPreserved"] is True
    assert "Could not confirm session close" in state["html"]
    assert "Refresh tabs" in state["html"]
    assert ">Retry<" not in state["html"]


def test_completed_request_does_not_dismiss_a_newer_modal():
    state = _run("dismissed")
    assert state["closes"] == 0
    assert state["loads"] == 1
    assert state["chatPreserved"] is False
