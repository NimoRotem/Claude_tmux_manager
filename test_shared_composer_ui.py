"""Run shared Chat/Terminal composer transactions against a deterministic DOM."""

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
const source=fs.readFileSync(process.argv[1],'utf8'),input=JSON.parse(process.argv[2]);
const quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const region=(start,end)=>{
  const a=js.indexOf(start),b=js.indexOf(end,a);
  if(a<0||b<0)throw new Error('Missing shipped region '+start);
  return js.slice(a,b);
};
const noop=()=>{},alerts=[],requests=[],paints=[],dispatch=[];
let doc,resolveRequest,resolveUpload,recorder,pendingStop;
class Element {
  constructor(id='',classes=''){
    this.id=id;this.className=classes;this.children=[];this.parentNode=null;
    this.style={};this.dataset={};this.attributes={};this.value='';this.textContent='';
    this.hidden=false;this.disabled=false;this.selectionStart=0;this.selectionEnd=0;this.selectionDirection='none';
    this.classList={
      contains:cls=>this.className.split(/\s+/).includes(cls),
      add:(...items)=>{this.className=[...new Set([...this.className.split(/\s+/).filter(Boolean),...items])].join(' ')},
      remove:(...items)=>{this.className=this.className.split(/\s+/).filter(cls=>!items.includes(cls)).join(' ')},
      toggle:(cls,on)=>{if(on===undefined)on=!this.classList.contains(cls);on?this.classList.add(cls):this.classList.remove(cls);return on},
    };
  }
  get isConnected(){return this===main||!!this.parentNode&&this.parentNode.isConnected}
  appendChild(child){child.remove();this.children.push(child);child.parentNode=this;return child}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(row=>row!==this);this.parentNode=null}
  replaceWith(other){const parent=this.parentNode;if(!parent)return;const index=parent.children.indexOf(this);other.remove();parent.children[index]=other;other.parentNode=parent;this.parentNode=null}
  replaceChild(other,previous){previous.replaceWith(other);return previous}
  contains(child){return this===child||this.children.some(row=>row.contains(child))}
  matches(selector){
    if(selector[0]==='#')return this.id===selector.slice(1);
    if(selector==='[id]')return !!this.id;
    if(selector==='[id^="session-composer-"]')return this.id.startsWith('session-composer-');
    if(selector[0]==='.')return this.classList.contains(selector.slice(1).split('[')[0]);
    return false;
  }
  querySelectorAll(selector){return this.children.flatMap(row=>[...(row.matches(selector)?[row]:[]),...row.querySelectorAll(selector)])}
  querySelector(selector){return this.querySelectorAll(selector)[0]||null}
  setAttribute(key,value){this.attributes[key]=String(value)}
  getAttribute(key){return this.attributes[key]||null}
  focus(){doc.activeElement=this}
  setSelectionRange(start,end,direction='none'){this.selectionStart=start;this.selectionEnd=end;this.selectionDirection=direction}
  get innerHTML(){return this._html||''}
  set innerHTML(value){this._html=value;this.children.forEach(child=>{child.parentNode=null});this.children=[]}
}
const main=new Element('main');
doc={activeElement:null,hidden:false,getElementById:id=>main.querySelector('#'+id),
  querySelectorAll:selector=>main.querySelectorAll(selector),createElement:()=>new Element()};
const sessions=['alpha','beta'].map(name=>({name,activity_status:'idle',messages:[],logical_incarnation:'logical-'+name}));
const context=vm.createContext({
  console,Promise,Math,Number,String,JSON,Map,Set,WeakMap,Date,Blob,FormData,
  sessions,selectedSession:'alpha',activeTabs:{alpha:input.start||'chat',beta:'chat'},mainEl:main,
  document:doc,window:{},BASE:'',_currentUser:null,MEMBER_SIMPLE:true,
  draftText:{},_composerAttachments:{},_composerUploadTasks:{},_uploadTab:{},
  _sessionClientEpoch:{},_recording:{},_mediaRec:{},_audioChunks:{},
  getRawState:()=>({live:{},fullText:''}),autoGrow:noop,
  renderComposerAttachments:(name,tab)=>paints.push([name,tab]),
  saveChatViewState:noop,restoreChatViewState:noop,renderChatBubbles:()=>'',refreshActiveChat:noop,
  stopAllRawPolling:noop,stopStatsPolling:noop,stopAllWatchdogPolling:noop,startRawPolling:noop,
  updateFreezeUi:noop,startStatsPolling:noop,startWatchdogPolling:noop,loadWatchdogStatus:noop,loadAccountSkills:noop,
  updateLiveBar:name=>paints.push(['status',name]),_sessionLogicalIncarnation:name=>'logical-'+name,
  _awaitComposerUploads:async key=>{if(context._composerUploadTasks[key])await context._composerUploadTasks[key]},
  _commandWithComposerAttachments:(text,attachments)=>text+(attachments.length?' Attached image: '+attachments[0].path:''),
  _clearComposerAttachments:(name,tab)=>{delete context._composerAttachments[tab+'-'+name]},
  appendChatBubble:noop,setOptimisticBusy:noop,scheduleBusyVerification:noop,
  alert:message=>alerts.push(message),setTimeout:()=>1,clearTimeout:noop,
  fetch:(url,options)=>{requests.push({url,options});return new Promise(resolve=>{resolveRequest=resolve})},
  navigator:{mediaDevices:{getUserMedia:async()=>({getTracks:()=>[{stop:noop}]})}},
  MediaRecorder:class {
    constructor(stream){this.stream=stream;this.state='inactive';this.mimeType='audio/webm';recorder=this}
    start(){this.state='recording'}
    stop(){this.state='inactive';pendingStop=this.onstop()}
  },
  _clipboardImages:()=>[{name:'pasted.png',size:3,type:'image/png'}],_clipboardImageFile:file=>file,
  _clipboardImagePreview:async()=> 'data:image/png;base64,AAAA',
  _uploadOneFile:()=>new Promise(resolve=>{resolveUpload=resolve}),
});
vm.runInContext(region('const draftText={};','function updateFavicon('),context);
vm.runInContext(region('const _COMPOSER_SEND_SVG=','async function sendChat('),context);
vm.runInContext(region('function handleSessionComposerKey(','// --- WhatsApp-style composer'),context);
vm.runInContext(region('function switchTab(','// ── Tab-more dropdown'),context);
vm.runInContext(region('async function sendChat(','function setOptimisticBusy('),context);
vm.runInContext(region('async function sendCmd(','// ── Raw Output Streaming'),context);
vm.runInContext(region('function handleComposerPaste(','function handleDrop('),context);
function makePage(name){
  main.innerHTML='';
  const tabBar=main.appendChild(new Element('','tab-bar'));
  tabBar.appendChild(new Element('','tab tab-view-trigger'));
  tabBar.appendChild(new Element('','tab tab-more-trigger'));
  tabBar.appendChild(new Element('tab-view-label-'+name));
  for(const tab of ['chat','raw','info','skills'])main.appendChild(new Element('tab-'+tab+'-'+name,'tab-content'));
  const shared=main.appendChild(new Element('session-composer-'+name,'session-composer'));
  shared.dataset.session=name;
  shared.appendChild(new Element('term-live-'+name,'term-live'));
  shared.appendChild(new Element('composer-attachments-chat-'+name,'composer-attachments'));
  shared.appendChild(new Element('cmd-chat-'+name,'cmd-input'));
  shared.appendChild(new Element('cmd-send-chat-'+name,'composer-action'));
  shared.appendChild(new Element('upload-'+name));
  return shared;
}
function mount(name,tab){context.selectedSession=name;context.activeTabs[name]=tab;makePage(name);if(!context._mountSessionComposer(name,tab))context.restoreDrafts()}
function rebuild(name=context.selectedSession){
  const focus=context.captureComposerFocus();context.saveDrafts();context._saveSessionComposerNodes();
  mount(name,context.activeTabs[name]||'chat');context.restoreComposerFocus(focus);
}
function state(){const name=context.selectedSession,el=doc.getElementById('cmd-chat-'+name),shared=doc.getElementById('session-composer-'+name);return {
  name,view:context.activeTabs[name],text:el.value,selection:[el.selectionStart,el.selectionEnd,el.selectionDirection],
  focused:doc.activeElement&&doc.activeElement.id,disabled:el.disabled,hidden:shared.hidden,
  attachments:(context._composerAttachments['chat-'+name]||[]).map(a=>a.name),
  button:doc.getElementById('cmd-send-chat-'+name).attributes['aria-label']||'',
}}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
(async()=>{
  mount('alpha',input.start||'chat');
  const original=doc.getElementById('cmd-chat-alpha'),shared=doc.getElementById('session-composer-alpha');
  original.value=input.text===undefined?'Draft to keep':input.text;original.focus();original.setSelectionRange(2,5,'backward');
  if(input.attachment)context._composerAttachments['chat-alpha']=[{name:'pasted.png',path:'/safe/pasted.png',previewUrl:'data:image/png;base64,AAAA'}];
  context.updateComposerBtn('chat-alpha');
  const states=[state()];
  if(input.action==='default-dispatch'){
    delete context.activeTabs.alpha;shared.dataset.view='';context.MEMBER_SIMPLE=!!input.member;
    context.handleChatKey=()=>dispatch.push('chat-key');context.handleRawKey=()=>dispatch.push('raw-key');
    context.sendChat=()=>dispatch.push('chat-send');context.sendCmd=()=>dispatch.push('raw-send');
    context.handleSessionComposerKey({},'alpha');context.composerAction('chat-alpha');
  }else if(input.action==='stale-send'){
    let releaseUpload;
    context._awaitComposerUploads=()=>new Promise(resolve=>{releaseUpload=resolve});
    const pending=input.start==='raw'?context.sendCmd('alpha','raw'):context.sendChat('alpha');
    await settle();context._sessionLogicalIncarnation=()=> 'replacement';releaseUpload();await pending;states.push(state());
  }else if(input.action==='callback'){
    let pending;
    if(input.callback==='paste'){
      context.handleComposerPaste({preventDefault:noop},'alpha','chat');await settle();
    }else if(input.callback==='voice'){
      await context.toggleRecording('chat-alpha');recorder.ondataavailable({data:new Blob(['voice'])});recorder.stop();await settle();
    }else{pending=input.start==='raw'?context.sendCmd('alpha','raw'):context.sendChat('alpha');await settle()}
    context.switchTab('alpha',input.start==='raw'?'chat':'raw');rebuild();states.push(state());
    if(input.otherSession){context.saveDrafts();context._saveSessionComposerNodes();mount('beta','chat');const beta=doc.getElementById('cmd-chat-beta');beta.value='Beta stays private';beta.focus()}
    if(input.callback==='paste'){resolveUpload({path:'/safe/pasted.png'});await context._composerUploadTasks['chat-alpha'];await settle()}
    else{resolveRequest({ok:input.ok!==false,json:async()=>input.callback==='voice'?{text:'Voice transcript'}:input.ok===false?{error:'Send failed'}:{ok:true}});if(pending)await pending;if(pendingStop)await pendingStop}
    if(input.otherSession){states.push(state());context.saveDrafts();context._saveSessionComposerNodes();mount('alpha',context.activeTabs.alpha)}
    states.push(state());
  }else{
    for(const action of input.actions||[{switch:'raw'},{rebuild:true},{switch:'chat'}]){
      if('text' in action){doc.getElementById('cmd-chat-'+context.selectedSession).value=action.text;context.updateComposerBtn('chat-'+context.selectedSession)}
      if(action.switch)context.switchTab(context.selectedSession,action.switch);
      if(action.rebuild)rebuild();
      if(action.session){context.saveDrafts();context._saveSessionComposerNodes();mount(action.session,context.activeTabs[action.session]||'chat')}
      states.push(state());
    }
  }
  return {states,alerts,dispatch,requests:requests.map(r=>r.url),paints,
    sameInput:doc.getElementById('cmd-chat-alpha')===original,
    sameShared:doc.getElementById('session-composer-alpha')===shared};
})().then(value=>process.stdout.write(JSON.stringify(value))).catch(error=>{console.error(error);process.exit(1)});
"""


def run_composer(**scenario):
    result = subprocess.run([NODE, "-e", DRIVER, str(APP), json.dumps(scenario)],
                            capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("start,target", [("chat", "raw"), ("raw", "chat")])
@pytest.mark.parametrize("text", ["Unsent draft\nSecond line", "same same same", "", "   "])
def test_switching_views_keeps_the_same_draft_node_and_caret(start, target, text):
    result = run_composer(start=start, text=text, actions=[{"switch": target}, {"rebuild": True}, {"switch": start}])
    assert all(row["text"] == text for row in result["states"])
    assert all(row["selection"] == [2, 5, "backward"] for row in result["states"] if len(text) >= 5)
    assert result["sameInput"] and result["sameShared"]
    assert all(not row["hidden"] for row in result["states"])


def test_cleared_draft_is_not_resurrected_by_switch_or_refresh():
    result = run_composer(actions=[{"switch": "raw"}, {"text": ""}, {"rebuild": True}, {"switch": "chat"}])
    assert result["states"][-1]["text"] == ""
    assert result["states"][-1]["button"] == "Record voice message"


def test_session_switches_keep_drafts_and_attachments_separate():
    result = run_composer(text="Alpha draft", attachment=True,
                          actions=[{"switch": "raw"}, {"session": "beta"}, {"text": "Beta draft"},
                                   {"session": "alpha"}, {"switch": "chat"}])
    assert result["states"][2]["text"] == ""
    assert result["states"][2]["attachments"] == []
    assert result["states"][3]["text"] == "Beta draft"
    assert result["states"][-1]["text"] == "Alpha draft"
    assert result["states"][-1]["attachments"] == ["pasted.png"]
    assert result["sameInput"] and result["sameShared"]


@pytest.mark.parametrize("start", ["chat", "raw"])
@pytest.mark.parametrize("ok", [True, False])
def test_pending_send_finishes_in_same_composer_after_view_switch(start, ok):
    result = run_composer(action="callback", callback="send", start=start, ok=ok, attachment=True)
    assert result["sameInput"] and result["sameShared"]
    assert result["states"][1]["disabled"] is True
    assert result["states"][-1]["disabled"] is False
    assert result["states"][-1]["text"] == ("" if ok else "Draft to keep")
    assert result["states"][-1]["attachments"] == ([] if ok else ["pasted.png"])
    assert result["alerts"] == ([] if ok else ["Send failed"])


def test_pending_pasted_image_completes_after_view_switch_and_refresh():
    result = run_composer(action="callback", callback="paste")
    assert result["sameInput"] and result["sameShared"]
    assert result["states"][-1]["attachments"] == ["pasted.png"]
    assert result["states"][-1]["text"] == "Draft to keep"
    assert result["states"][-1]["button"] == "Send message"


def test_pending_voice_transcript_joins_same_draft_after_view_switch_and_refresh():
    result = run_composer(action="callback", callback="voice")
    assert result["sameInput"] and result["sameShared"]
    assert result["states"][-1]["text"] == "Draft to keep Voice transcript"
    assert result["states"][-1]["button"] == "Send message"
    assert result["alerts"] == []


@pytest.mark.parametrize("callback,expected", [("send", ""), ("paste", "Draft to keep"), ("voice", "Draft to keep Voice transcript")])
def test_async_callback_updates_detached_original_composer_without_touching_other_session(callback, expected):
    result = run_composer(action="callback", callback=callback, otherSession=True)
    assert result["states"][-2]["name"] == "beta"
    assert result["states"][-2]["text"] == "Beta stays private"
    assert result["states"][-2]["attachments"] == []
    assert result["states"][-2]["focused"] == "cmd-chat-beta"
    assert result["states"][-1]["text"] == expected
    assert result["sameInput"] and result["sameShared"]


@pytest.mark.parametrize("start", ["chat", "raw"])
def test_upload_wait_cannot_send_old_draft_to_replacement_session(start):
    result = run_composer(action="stale-send", start=start)
    assert result["requests"] == []
    assert result["states"][-1]["text"] == "Draft to keep"
    assert result["states"][-1]["disabled"] is False
    assert result["alerts"] == ["Session changed before send completed."]


@pytest.mark.parametrize("member", [False, True])
def test_shared_composer_defaults_to_chat_dispatch_for_admin_and_member(member):
    result = run_composer(action="default-dispatch", member=member)
    assert result["dispatch"] == ["chat-key", "chat-send"]


MARKUP_DRIVER = r"""
const fs=require('fs'),vm=require('vm');
const source=fs.readFileSync(process.argv[1],'utf8'),quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const start=js.indexOf('  mainEl.innerHTML=`',js.indexOf('function renderDetail()'));
const end=js.indexOf('  // Reattach the actual composer',start);
const context=vm.createContext({
  s:{name:'alpha',activity_status:'idle'},tab:process.argv[2],mainEl:{innerHTML:''},
  _currentUser:null,MEMBER_SIMPLE:true,esc:value=>String(value||''),
  modelBadgeLabel:()=> 'GPT-6 Astra',effortBadgeLabel:()=> 'max',
  autopushSeg:()=>'',autopushDesc:()=>'',idleNudgeSeg:()=>'',getCleanViewPref:()=>true,
  CLEAN_VIEW_ON:'On',CLEAN_VIEW_OFF:'Off',statusLabel:()=> 'Idle',
  renderChatBubbles:()=>'',getTerminalHeight:()=>'',buildKeyBar:()=>'',timeAgo:()=>'',
  _COMPOSER_MIC_SVG:'',_COMPOSER_VOICE_SVG:'',
});
if(start<0||end<0)throw new Error('Missing renderDetail template boundary');
vm.runInContext(js.slice(start,end),context);
process.stdout.write(context.mainEl.innerHTML);
"""


class MarkupElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.stack = []
        self.nodes = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.nodes.append((tag, attrs, [node[1].get("id", "") for node in self.stack]))
        if tag not in {"input", "br", "img", "hr", "meta", "link"}:
            self.stack.append((tag, attrs))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break


@pytest.mark.parametrize("view", ["chat", "raw", "info"])
def test_one_shared_status_strip_and_composer_live_outside_both_reading_views(view):
    rendered = subprocess.run([NODE, "-e", MARKUP_DRIVER, str(APP), view],
                              capture_output=True, text=True, timeout=10)
    assert rendered.returncode == 0, rendered.stderr
    tree = MarkupElements()
    tree.feed(rendered.stdout)
    shared = [row for row in tree.nodes if row[1].get("id") == "session-composer-alpha"]
    assert len(shared) == 1
    assert ("hidden" in shared[0][1]) == (view == "info")
    assert "tab-chat-alpha" not in shared[0][2]
    assert "tab-raw-alpha" not in shared[0][2]
    inputs = [row for row in tree.nodes if "cmd-input" in row[1].get("class", "").split()]
    assert len(inputs) == 1
    assert inputs[0][1]["id"] == "cmd-chat-alpha"
    assert "handleSessionComposerKey" in inputs[0][1]["onkeydown"]
    for node_id in ["cmd-chat-alpha", "composer-attachments-chat-alpha", "upload-alpha", "term-live-alpha",
                    "tl-verb-alpha", "tl-time-alpha", "tl-total-alpha", "tl-ctx-alpha", "tl-tps-alpha"]:
        matches = [row for row in tree.nodes if row[1].get("id") == node_id]
        assert len(matches) == 1, node_id
        assert "session-composer-alpha" in matches[0][2], node_id
