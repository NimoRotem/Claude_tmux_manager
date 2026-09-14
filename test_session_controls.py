"""Exercise model/effort controls without importing app or touching live sessions."""

import json
import shutil
import subprocess
from html.parser import HTMLParser
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
const region=js.slice(js.indexOf('function formatModelName('),js.indexOf('function statusLabel('));
const input=JSON.parse(process.argv[2]);
const nodes=new Map(),menus=[],requests=[],alerts=[];
let focused='',moreStates=[],keyboardPrevented=0;
const session=Object.assign({name:'alpha',model:'gpt-6-astra',effort:'xhigh'},input.session||{});
const noop=()=>{};
const esc=value=>String(value||'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
for(const id of ['model-badge-alpha','effort-badge-alpha','more-model-alpha','more-effort-alpha']){
  nodes.set(id,{innerHTML:'',textContent:'',classList:{toggle:noop}});
}
const context=vm.createContext({
  console,Promise,Math,Date,BASE:'',sessions:[session],esc,
  window:{innerWidth:1200,innerHeight:800},
  document:{
    getElementById:id=>nodes.get(id)||null,
    addEventListener:noop,removeEventListener:noop,
    createElement:()=>({dataset:{},style:{},events:{},offsetWidth:240,offsetHeight:260,contains:()=>false,querySelector:()=>null,addEventListener(name,fn){this.events[name]=fn},remove(){this.removed=true}}),
    body:{appendChild:node=>menus.push(node)},
  },
  setTimeout:fn=>fn(),statusInfoEl:{textContent:''},
  alert:value=>alerts.push(value),confirm:()=>!!input.restart,
  fetch:async(url,options)=>{
    if(url==='/api/models')return {ok:false};
    requests.push({url,body:JSON.parse(options.body)});
    const response=(input.responses||[])[requests.length-1]||{ok:true,body:{ok:true}};
    if(response.throw)throw new Error(response.throw);
    return {ok:response.ok,status:response.status||200,json:async()=>response.body};
  },
});
vm.runInContext(region,context);
const anchor={getBoundingClientRect:()=>({bottom:40,right:800}),focus:()=>{focused='effort-control'}};
(async()=>{
  if(input.action==='menus'){
    context.openModelMenu('alpha',anchor);
    context.openEffortMenu('alpha',anchor);
  }else if(input.action==='effort'){
    await context.setSessionEffort('alpha',input.effort||'high');
  }else if(input.action==='model'){
    await context.setSessionModel('alpha',input.model||'gpt-5.5');
  }else if(input.action==='picker_keyboard'){
    context.openEffortMenu('alpha',anchor);
    menus[menus.length-1].events.keydown({key:'Escape',stopPropagation:noop});
  }else if(input.action==='more_keyboard'){
    let open=false;
    const attrs={};
    nodes.set('tab-more-toggle',{setAttribute:(key,value)=>{attrs[key]=value},focus:()=>{focused='more-trigger'}});
    nodes.set('tab-more-menu',{classList:{contains:()=>open,toggle:(_name,value)=>{open=value},remove:()=>{open=false}}});
    context.closeViewMenu=noop;
    vm.runInContext(js.slice(js.indexOf('function toggleTabMore('),js.indexOf('// ── View dropdown')),context);
    const snapshot=()=>moreStates.push({open,expanded:attrs['aria-expanded'],focused});
    context.toggleTabMore({stopPropagation:noop});
    snapshot();
    context.handleTabMoreKeydown({key:'Tab',preventDefault:()=>{keyboardPrevented++},stopPropagation:noop});
    snapshot();
    context.handleTabMoreKeydown({key:'Escape',preventDefault:()=>{keyboardPrevented++},stopPropagation:noop});
    snapshot();
    context.toggleTabMore({stopPropagation:noop});
    context.closeTabMore();
    snapshot();
  }else if(input.action==='header'){
    const start=js.indexOf('  mainEl.innerHTML=`',js.indexOf('function renderDetail()'));
    const end=js.indexOf('    <div class="tab-content',start);
    Object.assign(context,{
      s:session,tab:'raw',mainEl:{innerHTML:''},
      _currentUser:{username:'Nimo',team_mode:true},location:{origin:'https://codex.lisa.my'},
      MEMBER_SIMPLE:false,autopushSeg:()=>'',idleNudgeSeg:()=>'',
      getCleanViewPref:()=>true,CLEAN_VIEW_ON:'On',CLEAN_VIEW_OFF:'Off',statusLabel:()=> 'Idle',
    });
    vm.runInContext(js.slice(start,end)+'`;',context);
  }
  context._paintModelBadge('alpha');
  process.stdout.write(JSON.stringify({
    session,requests,alerts,menus:menus.map(m=>({html:m.innerHTML,kind:m.dataset.kind,removed:!!m.removed})),
    effortBadge:nodes.get('effort-badge-alpha').innerHTML,
    moreEffort:nodes.get('more-effort-alpha').textContent,
    modelBadge:nodes.get('model-badge-alpha').innerHTML,
    header:context.mainEl&&context.mainEl.innerHTML,
    message:context.statusInfoEl.textContent,
    focused,moreStates,keyboardPrevented,
  }));
})().catch(error=>{console.error(error);process.exit(1)});
"""


def run_controls(**scenario):
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(APP), json.dumps(scenario)],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_effort_is_a_separate_model_aware_control():
    state = run_controls(action="menus", session={"effort": "ultra"})
    model_menu, effort_menu = state["menus"]
    assert model_menu["removed"] is True
    assert "setSessionEffort" not in model_menu["html"]
    assert effort_menu["kind"] == "effort"
    assert "setSessionModel" not in effort_menu["html"]
    assert "setSessionEffort('alpha','ultra')" in effort_menu["html"]
    assert "setSessionEffort('alpha','none')" not in effort_menu["html"]
    assert '<span>Ultra</span><span>✓</span>' in effort_menu["html"]
    assert state["effortBadge"].startswith("Effort ultra ")
    assert state["moreEffort"] == "ultra ▾"


def test_pending_model_determines_supported_efforts():
    state = run_controls(
        action="menus", session={"model_pending": "gpt-5.5", "effort": "xhigh"}
    )
    effort_menu = state["menus"][-1]["html"]
    assert "setSessionEffort('alpha','xhigh')" in effort_menu
    assert "setSessionEffort('alpha','max')" not in effort_menu
    assert "setSessionEffort('alpha','ultra')" not in effort_menu


def test_unrecognized_models_do_not_offer_unverified_efforts():
    state = run_controls(action="menus", session={"model": "unknown-model"})
    effort_menu = state["menus"][-1]["html"]
    assert "Reasoning levels unavailable" in effort_menu
    assert "setSessionEffort(" not in effort_menu


def test_effort_save_repaints_both_controls_without_restarting():
    state = run_controls(
        action="effort",
        responses=[{"ok": True, "body": {"codex_was_running": True}}],
    )
    assert state["requests"] == [
        {"url": "/api/sessions/alpha/effort", "body": {"effort": "high", "restart": False}}
    ]
    assert state["session"]["effort"] == "high"
    assert state["effortBadge"].startswith("Effort high ")
    assert state["moreEffort"] == "high ▾"
    assert state["alerts"] == []


def test_rejected_effort_save_keeps_previous_selection_and_reports_error():
    state = run_controls(
        action="effort",
        responses=[{"ok": False, "status": 400, "body": {"error": "Unsupported effort"}}],
    )
    assert state["session"]["effort"] == "xhigh"
    assert state["effortBadge"].startswith("Effort xhigh ")
    assert state["alerts"] == ["Effort switch failed: Unsupported effort"]


def test_failed_restart_preserves_the_successfully_saved_effort():
    state = run_controls(
        action="effort",
        restart=True,
        responses=[
            {"ok": True, "body": {"codex_was_running": True}},
            {"ok": False, "body": {"error": "Restart unavailable"}},
        ],
    )
    assert state["requests"][-1]["body"]["restart"] is True
    assert state["session"]["effort"] == "high"
    assert state["effortBadge"].startswith("Effort high ")
    assert state["alerts"] == ["Effort saved, but restart failed: Restart unavailable"]


def test_model_switch_repaints_server_selected_supported_effort():
    state = run_controls(
        action="model",
        session={"effort": "ultra"},
        responses=[{"ok": True, "body": {"codex_was_running": False, "effort": "xhigh"}}],
    )
    assert state["session"]["model"] == "gpt-5.5"
    assert state["session"]["effort"] == "xhigh"
    assert state["effortBadge"].startswith("Effort xhigh ")
    assert state["moreEffort"] == "xhigh ▾"


def test_confirmed_model_restart_sets_active_model_and_clears_pending():
    state = run_controls(
        action="model", model="gpt-5.6-terra", restart=True,
        session={"model": "gpt-6-astra", "effort": "ultra"},
        responses=[
            {"ok": True, "body": {"ok": True, "codex_was_running": True, "effort": "max", "restarted": False}},
            {"ok": True, "body": {"ok": True, "codex_was_running": True, "effort": "max", "restarted": True}},
        ],
    )
    assert state["requests"] == [
        {"url": "/api/sessions/alpha/model", "body": {"model": "gpt-5.6-terra", "restart": False}},
        {"url": "/api/sessions/alpha/model", "body": {"model": "gpt-5.6-terra", "restart": True}},
    ]
    assert state["session"]["model"] == "gpt-5.6-terra"
    assert state["session"]["model_pending"] == ""
    assert state["session"]["effort"] == "max"
    assert state["effortBadge"].startswith("Effort max ")
    assert state["alerts"] == []
    assert state["message"] == "Model → GPT-5.6 Terra · restarted"


@pytest.mark.parametrize(("restart_response", "reason"), [
    ({"ok": False, "status": 409, "body": {"saved": True, "error": "Session is busy. Try again."}},
     "Session is busy. Try again."),
    ({"ok": True, "body": {"ok": True, "saved": True, "restarted": False}},
     "Codex did not restart"),
    ({"ok": False, "status": 500, "body": {"saved": True, "error": "Restart unavailable"}},
     "Restart unavailable"),
    ({"throw": "Network connection lost"}, "Network connection lost"),
], ids=["busy-saved-409", "restart-unconfirmed-200", "restart-500", "restart-network-loss"])
def test_model_restart_failure_retains_saved_pending_model_and_effort(restart_response, reason):
    state = run_controls(
        action="model", model="gpt-5.6-terra", restart=True,
        session={"model": "gpt-6-astra", "effort": "ultra"},
        responses=[
            {"ok": True, "body": {"ok": True, "codex_was_running": True, "effort": "max"}},
            restart_response,
        ],
    )
    assert len(state["requests"]) == 2
    assert state["requests"][-1]["body"] == {"model": "gpt-5.6-terra", "restart": True}
    assert state["session"]["model"] == "gpt-6-astra", "An unconfirmed restart must not claim the new model is active"
    assert state["session"]["model_pending"] == "gpt-5.6-terra"
    assert state["session"]["effort"] == "max", "Keep the reasoning level returned by the successful save"
    assert state["effortBadge"].startswith("Effort max ")
    assert "GPT-5.6 Terra…" in state["modelBadge"]
    assert state["alerts"] == ["Model saved, but restart failed: " + reason]
    assert "restarted" not in state["message"]


def test_declining_model_restart_saves_once_and_keeps_selection_pending():
    state = run_controls(
        action="model", model="gpt-5.6-terra", restart=False,
        session={"model": "gpt-6-astra", "effort": "ultra"},
        responses=[{"ok": True, "body": {"ok": True, "codex_was_running": True, "effort": "max"}}],
    )
    assert state["requests"] == [
        {"url": "/api/sessions/alpha/model", "body": {"model": "gpt-5.6-terra", "restart": False}}
    ]
    assert state["session"]["model"] == "gpt-6-astra"
    assert state["session"]["model_pending"] == "gpt-5.6-terra"
    assert state["session"]["effort"] == "max"
    assert state["alerts"] == []
    assert state["message"] == "Model → GPT-5.6 Terra · saved"


def test_project_link_is_in_more_and_effort_sits_next_to_model():
    state = run_controls(action="header", session={"name": "alpha", "owner": "Nimo"})
    header = state["header"]
    more, badges = header.split('<div class="detail-badges">', 1)
    assert 'class="tab-more-item tab-more-project-link"' in more
    assert 'href="https://codex.lisa.my/Nimo/alpha"' in more
    assert 'id="more-effort-alpha"' in more
    assert 'id="model-badge-alpha"' in badges
    assert badges.index('id="model-badge-alpha"') < badges.index('id="effort-badge-alpha"')
    assert "https://codex.lisa.my/Nimo/alpha" not in badges


def test_more_and_mobile_selectors_are_keyboard_operable_native_buttons():
    class Elements(HTMLParser):
        def __init__(self):
            super().__init__()
            self.by_id = {}

        def handle_starttag(self, tag, attrs):
            attrs = dict(attrs)
            if "id" in attrs:
                self.by_id[attrs["id"]] = (tag, attrs)

    elements = Elements()
    elements.feed(run_controls(action="header")["header"])
    for element_id in ("tab-more-toggle", "more-model-alpha", "more-effort-alpha"):
        tag, attrs = elements.by_id[element_id]
        assert tag == "button", f"{element_id} must accept keyboard activation natively"
        assert attrs["type"] == "button"
        assert attrs.get("aria-label")
    _, more_attrs = elements.by_id["tab-more-toggle"]
    assert more_attrs["aria-controls"] == "tab-more-menu"
    assert more_attrs["aria-expanded"] == "false"


def test_more_keyboard_close_updates_expanded_state_and_restores_focus():
    state = run_controls(action="more_keyboard")
    assert state["moreStates"] == [
        {"open": True, "expanded": "true", "focused": ""},
        {"open": True, "expanded": "true", "focused": ""},
        {"open": False, "expanded": "false", "focused": "more-trigger"},
        {"open": False, "expanded": "false", "focused": "more-trigger"},
    ]
    assert state["keyboardPrevented"] == 1, "Tab navigation must remain available"


def test_effort_picker_escape_returns_focus_to_the_mobile_control():
    state = run_controls(action="picker_keyboard")
    assert state["menus"][-1]["removed"] is True
    assert state["focused"] == "effort-control"
