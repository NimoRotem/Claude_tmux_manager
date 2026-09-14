"""Saved-history paging and short-scroll intent against the shipped JavaScript."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

DRIVER = r"""
const fs=require('fs'),vm=require('vm'),input=JSON.parse(process.argv[2]);
const source=fs.readFileSync(process.argv[1],'utf8'),quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const region=(start,end)=>js.slice(js.indexOf(start),js.indexOf(end,js.indexOf(start)));
const noop=()=>{},requests=[],listeners={};let resolvePage,clean=true,incarnation='original';
class Element {
  constructor(tag='div'){this.tagName=tag;this.children=[];this.parentNode=null;this.className='';this.style={};this.disabled=false;this.hidden=false;this.scrollTop=0;this.clientHeight=120;this.id='';this._text='';this.classList={add:noop};}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(row=>row!==this);this.parentNode=null}
  append(...items){items.forEach(item=>this.appendChild(item))}
  appendChild(item){return this.insertBefore(item,null)}
  insertBefore(item,before){if(item.tagName==='fragment'){[...item.children].forEach(child=>this.insertBefore(child,before));return item}item.remove();const index=before?this.children.indexOf(before):this.children.length;this.children.splice(index,0,item);item.parentNode=this;return item}
  contains(item){return this===item||this.children.some(child=>child.contains(item))}
  addEventListener(type,callback,options){(listeners[type]||=[]).push({callback,options})}
  get firstChild(){return this.children[0]||null}
  get scrollHeight(){return this.id==='raw-demo'?300+this.countEntries()*20:0}
  countEntries(){return (this.className.startsWith('terminal-history-entry ')?1:0)+this.children.reduce((sum,child)=>sum+child.countEntries(),0)}
  set textContent(value){this._text=String(value);this.children.forEach(child=>child.parentNode=null);this.children=[]}
  get textContent(){return this._text}
}
const raw=new Element();raw.id='raw-demo';let mounted=true;
const states={demo:{userScrolledUp:false,frozen:false}},document={
  getElementById:id=>mounted&&id==='raw-demo'?raw:null,
  createElement:tag=>new Element(tag),createDocumentFragment:()=>new Element('fragment'),
};
const context=vm.createContext({console,Promise,Number,Math,Array,BASE:'',document,
  getRawState:name=>states[name],getCleanViewPref:()=>clean,_sessionLogicalIncarnation:()=>incarnation,
  _linkifyTerminalText:value=>String(value),
  fetch:async(url,options)=>{requests.push({url,options});const page=input.deferred?await new Promise(resolve=>resolvePage=resolve):input.pages.shift();if(!page)throw Error('No scripted page');return {ok:page.status===undefined||page.status===200,status:page.status||200,json:async()=>page}},
});
vm.runInContext(region('function _setRawScroll(','function renderRawText('),context);
vm.runInContext(region('function _ensureRawScrollTracking(','function applyRawPayload('),context);
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function snapshot(){const h=states.demo.history||{},parts=raw._terminalParts;return {entries:h.entries||[],cursor:h.cursor,loading:h.loading,atStart:h.atStart,error:h.error,top:raw.scrollTop,
  button:parts?.button.textContent,disabled:parts?.button.disabled,note:parts?.note.textContent,requests:requests.length,userScrolledUp:states.demo.userScrolledUp,renderedEntries:raw.countEntries()}}
(async()=>{
  if(input.action==='scroll'){
    Object.defineProperty(raw,'scrollHeight',{value:input.height||120});raw.scrollTop=input.top||0;
    states.demo.frozen=!!input.frozen;context.loadTerminalHistory=()=>requests.push({name:'demo'});
    context._ensureRawScrollTracking(raw,states.demo,'demo');context._ensureRawScrollTracking(raw,states.demo,'demo');
    for(const event of input.events||[])for(const row of listeners[event.type]||[])row.callback(event);
    return {state:snapshot(),listeners:Object.fromEntries(Object.entries(listeners).map(([type,rows])=>[type,rows.map(row=>row.options)]))};
  }
  context._terminalParts('demo');raw.scrollTop=input.top||0;
  const pending=context.loadTerminalHistory('demo');await settle();
  const during=snapshot();
  if(input.deferred){
    if(input.change==='incarnation')incarnation='replacement';
    if(input.change==='mode'){clean=false;context._terminalParts('demo')}
    if(input.change==='removed')mounted=false;
    if(input.change==='duplicate')await context.loadTerminalHistory('demo');
    resolvePage(input.pages.shift());
  }
  await pending;
  if(input.again)await context.loadTerminalHistory('demo');
  return {during,state:snapshot(),requests};
})().then(result=>process.stdout.write(JSON.stringify(result))).catch(error=>{console.error(error);process.exit(1)});
"""


def run_history(**scenario):
    result = subprocess.run([NODE, "-e", DRIVER, str(APP), json.dumps(scenario)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def entry(text):
    return {"kind": "assistant", "text": text, "id": text}


def test_empty_filtered_pages_advance_until_visible_history_arrives():
    result = run_history(pages=[{"entries": [], "cursor": "next-1"},
                                {"entries": [], "cursor": "next-2"},
                                {"entries": [entry("Original reply")], "cursor": "next-3"}])
    assert [row["text"] for row in result["state"]["entries"]] == ["Original reply"]
    assert [row["url"] for row in result["requests"]] == [
        "/api/sessions/demo/terminal-history?tools=false",
        "/api/sessions/demo/terminal-history?tools=false&cursor=next-1",
        "/api/sessions/demo/terminal-history?tools=false&cursor=next-2",
    ]
    assert all(row["options"]["cache"] == "no-store" for row in result["requests"])
    assert not result["state"]["loading"]


def test_empty_final_page_marks_beginning_without_refetching():
    result = run_history(pages=[{"entries": [], "cursor": "", "at_start": True}], again=True)
    assert result["state"]["entries"] == []
    assert result["state"]["requests"] == 1
    assert result["state"]["button"] == "Beginning of session"
    assert result["state"]["disabled"]
    assert result["state"]["note"] == "No saved conversation messages yet."


@pytest.mark.parametrize("pages", [[{"entries": [], "cursor": ""}],
                                  [{"entries": [], "cursor": "same"}, {"entries": [], "cursor": "same"}]])
def test_empty_history_page_without_cursor_progress_is_retryable_not_infinite(pages):
    result = run_history(pages=pages)
    assert "did not advance" in result["state"]["error"]
    assert result["state"]["button"] == "Retry earlier history"
    assert not result["state"]["loading"]
    assert not result["state"]["atStart"]


def test_older_history_prepend_keeps_reader_anchor_and_chronological_order():
    result = run_history(top=50, again=True,
                         pages=[{"entries": [entry("Recent")], "cursor": "older"},
                                {"entries": [entry("Original")], "at_start": True}])
    assert [row["text"] for row in result["state"]["entries"]] == ["Original", "Recent"]
    assert result["state"]["top"] == 90
    assert result["state"]["userScrolledUp"]


def test_concurrent_history_loads_share_the_pending_request():
    result = run_history(deferred=True, change="duplicate", pages=[{"entries": [entry("Once")]}])
    assert result["during"]["loading"]
    assert result["state"]["requests"] == 1
    assert len(result["state"]["entries"]) == 1


def test_changed_history_root_clears_old_entries_and_cursor_before_retry():
    result = run_history(again=True, pages=[{"entries": [entry("Previous root")], "cursor": "old-cursor"},
                                           {"status": 409, "error": "Session history changed"}])
    assert result["state"]["entries"] == []
    assert result["state"]["renderedEntries"] == 0
    assert result["state"]["cursor"] == ""
    assert result["state"]["button"] == "Retry earlier history"
    assert not result["state"]["loading"]


@pytest.mark.parametrize("change", ["incarnation", "mode", "removed"])
def test_late_history_response_does_not_cross_session_or_view_binding(change):
    result = run_history(deferred=True, change=change, pages=[{"entries": [entry("Old private reply")]}])
    assert result["state"]["entries"] == []
    assert not result["state"]["loading"]


@pytest.mark.parametrize("events", [[{"type": "wheel", "deltaY": -1}],
                                   [{"type": "touchstart", "touches": [{"clientY": 10}]},
                                    {"type": "touchmove", "touches": [{"clientY": 30}]}]])
def test_short_terminal_without_overflow_can_load_older_history(events):
    result = run_history(action="scroll", height=120, events=events)
    assert result["state"]["requests"] == 1
    assert all(len(rows) == 1 for rows in result["listeners"].values())
    assert all(row["passive"] for rows in result["listeners"].values() for row in rows)


@pytest.mark.parametrize("options", [{"frozen": True}, {"top": 150}, {"events": [{"type": "wheel", "deltaY": 1}]}])
def test_history_scroll_intent_respects_freeze_position_and_direction(options):
    result = run_history(action="scroll", **{"events": [{"type": "wheel", "deltaY": -1}], **options})
    assert result["state"]["requests"] == 0


def test_scroll_position_updates_live_follow_state_and_loads_near_top():
    result = run_history(action="scroll", height=500, top=20, events=[{"type": "scroll"}])
    assert result["state"]["userScrolledUp"]
    assert result["state"]["requests"] == 1
