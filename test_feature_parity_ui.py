"""Exercise the merged dashboard controls without accounts, browser, or network."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest


APP = Path(__file__).with_name("app.py")
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

DRIVER = r"""
const fs=require('fs'),vm=require('vm');
const src=fs.readFileSync(process.argv[1],'utf8'),code=JSON.parse(process.argv[2]);
const section=(a,b)=>{const start=src.indexOf(a),end=src.indexOf(b,start);if(start<0||end<0)throw Error(a);return src.slice(start,end)};
const elements=new Map(),requests=[],timers=new Map(),paints=[],raw={};
let nextTimer=0,loads=0,renames=0,closed=0;
function element(id){
  if(elements.has(id))return elements.get(id);
  const el={id,style:{},dataset:{},attributes:{},className:'',value:'',textContent:'',innerHTML:'',
    children:[],scrollTop:0,scrollHeight:1000,clientHeight:200,offsetTop:0,disabled:false,
    appendChild(child){this.children.push(child);return child},
    setAttribute(k,v){this.attributes[k]=String(v)},getAttribute(k){return this.attributes[k]},
    focus(){},select(){},querySelectorAll(){return []},getBoundingClientRect(){return {top:this.offsetTop}},
  };
  el.classList={add(...s){el.className=[...new Set(el.className.split(/\s+/).concat(s))].filter(Boolean).join(' ')},
    remove(...s){el.className=el.className.split(/\s+/).filter(c=>!s.includes(c)).join(' ')},
    contains(s){return el.className.split(/\s+/).includes(s)},toggle(s,on){if(on===undefined)on=!this.contains(s);on?this.add(s):this.remove(s);return on}};
  elements.set(id,el);return el;
}
for(const id of ['modal-content','modal-overlay','settings-content','users-content','nav-browser-badge',
  'nav-server-stats','nav-cpu-summary','nav-cpu-summary-value','nav-cpu-summary-fill',
  'nav-ram-summary','nav-ram-summary-value','nav-ram-summary-fill'])element(id);
const noop=()=>{},escape=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const context=vm.createContext({console,Promise,JSON,Number,String,Math,Date,Set,Map,AbortController,
  BASE:'',MEMBER_SIMPLE:false,selectedSession:'alpha',activeTabs:{alpha:'chat'},_sessionClientEpoch:{},
  sessions:[{name:'alpha',logical_incarnation:'one',tab_label:'Alpha'}],
  _currentUser:{id:'owner1',role:'admin'},_settingsActiveTab:'preferences',_browserBadgeRequest:null,
  _browserAuth:null,statusInfoEl:{textContent:''},_uploadTab:{},
  document:{hidden:false,getElementById:id=>elements.get(id)||null,createElement:()=>element('created-'+elements.size)},
  localStorage:{getItem:()=>null,setItem:noop},
  setTimeout:(fn,ms)=>{const id=++nextTimer;timers.set(id,{fn,ms});return id},clearTimeout:id=>timers.delete(id),setInterval:()=>1,
  esc:escape,_escTermHtml:s=>escape(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;'),
  sessionTabLabel:s=>s.tab_label||s.name,_paintSessionTabLabel:()=>renames++,
  _sessionLogicalIncarnation:name=>(context.sessions.find(s=>s.name===name)||{}).logical_incarnation||'',
  getRawState:name=>raw[name]||(raw[name]={fullText:'',firstLoad:false,userRows:[]}),
  _setRawScroll:(el,top)=>{el.scrollTop=top},renderRawText:(name,force)=>paints.push([name,force]),
  switchTab:(name,tab)=>{context.activeTabs[name]=tab},startRawPolling:()=>loads++,
  renderSavedKeys:name=>paints.push(['saved',name]),renderUsersAdmin:()=>paints.push(['users']),
  _savedRow:()=>element('row-'+elements.size),_savedCopyBtn:value=>({copied:value}),
  loadAll:async()=>loads++,closeModal:()=>closed++,refreshCodexAlertBadge:noop,_fmtResetTime:()=> 'in 2h',
  fetch:(url,options)=>new Promise((resolve,reject)=>requests.push({url,options,resolve,reject})),
});
for(const [a,b] of [
  ['function jumpToLastUserMessage(name){','function rerenderAllRaw(){'],
  ['function readTerminal(action,name){','const CLEAN_VIEW_ON='],
  ['function buildKeyBar(name,tab){','// ── Saved project keys'],
  ['const _savedProjectItems={};','function renderSavedKeys(name,sessionObj){'],
  ['function _savedCredRow(c){','function _savedFileRow(p,disambiguate,sessionName){'],
  ['function showRenameSession(name){','function _paintSessionTabLabel(name){'],
  ['let _preferencesRevision=0;','function renderSettingsContent(){'],
  ["let _groupsCache=[];",'function _groupOpts(selected){'],
  ['async function loadUsersAdmin(){','function filterUsers(value){'],
  ['// --- Inline server stats in nav header ---','// --- Open Codex health alerts'],
  ['function _applyUsageStyle(fillEl,pctNum){','function _selectUsageCapWindow'],
  ['async function refreshBrowserAuthBadge(){','function onBrowserBadgeClick(){'],
])vm.runInContext(section(a,b).replace(/^refreshNavStats\(\);$/gm,''),context);
context.test={element,raw,requests,paints,timers,counts:()=>({loads,renames,closed}),
  installSwitch(){
    Object.assign(context,{mainEl:{querySelectorAll:()=>[],querySelector:()=>null},rawCache:{alpha:{scrollTop:240}},
      captureComposerFocus:noop,saveChatViewState:noop,_updateSessionComposerView:noop,stopAllRawPolling:noop,
      stopStatsPolling:noop,stopAllWatchdogPolling:noop,restoreComposerFocus:noop,refreshActiveChat:noop});
    context.renderRawText=(name,force)=>{paints.push([name,force]);element('raw-live-'+name)._lineMode=true};
    vm.runInContext(section('function switchTab(name,tab){','// ── Tab-more dropdown'),context);
  },
  respond:(i,data,status=200)=>requests[i].resolve({ok:status>=200&&status<300,json:async()=>data}),
  flush:()=>new Promise(resolve=>setImmediate(resolve)),
};
Promise.resolve(vm.runInContext('(async()=>{'+code+'})()',context))
  .then(result=>process.stdout.write(JSON.stringify(result)))
  .catch(error=>{console.error(error);process.exitCode=1});
"""


def run_js(code):
    result = subprocess.run([NODE, "-e", DRIVER, str(APP), json.dumps(code)],
                            capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.mark.parametrize("member", [False, True])
def test_single_drawer_has_reading_controls_saved_items_and_upload(member):
    html = run_js(f"MEMBER_SIMPLE={str(member).lower()};return buildKeyBar('alpha','chat');")
    assert html.count('id="freeze-btn-alpha"') == 1
    assert html.count('id="keysaved-chat-alpha"') == 1
    assert "My last message" in html and "Latest output" in html
    assert 'id="dropzone-chat-alpha"' in html
    if not member:
        assert "Clear Input" in html and "/plan" in html and "/status" in html


def test_frozen_snapshot_does_not_follow_new_output_and_latest_catches_up():
    state = run_js(r"""
      const raw=test.element('raw-alpha'),st=getRawState('alpha');st.fullText='Original reply';
      toggleRawFreeze('alpha');st.fullText='Original reply\nNew reply';
      const held=_rawVisibleText(st);jumpToLive('alpha');
      return {held,latest:_rawVisibleText(st),frozen:st.frozen,top:raw.scrollTop,paints:test.paints};
    """)
    assert state["held"] == "Original reply"
    assert state["latest"] == "Original reply\nNew reply"
    assert state["frozen"] is False and state["top"] == 1000
    assert ["alpha", True] in state["paints"]


def test_reading_controls_wait_for_first_snapshot_and_preserve_chat_draft():
    state = run_js("""
      const st=getRawState('alpha');st.firstLoad=true;
      const input=test.element('cmd-chat-alpha');input.value='Unsent draft';
      readTerminal('freeze','alpha');const before={view:activeTabs.alpha,pending:st.pendingReadAction,frozen:!!st.frozen};
      st.fullText='Fresh output';st.firstLoad=false;readTerminal(st.pendingReadAction,'alpha');
      return {before,held:_rawVisibleText(st),frozen:st.frozen,draft:input.value,loads:test.counts().loads};
    """)
    assert state["before"] == {"view": "raw", "pending": "freeze", "frozen": False}
    assert state["frozen"] is True and state["held"] == "Fresh output"
    assert state["draft"] == "Unsent draft" and state["loads"] == 1


def test_last_message_cycles_live_then_saved_history_without_sending_keys():
    state = run_js("""
      const pane=test.element('raw-alpha'),live=test.element('raw-live-alpha'),saved=test.element('saved-user');
      const old=test.element('old-user'),latest=test.element('new-user');
      saved.offsetTop=40;old.offsetTop=180;latest.offsetTop=340;
      live.children=[old,latest];pane.querySelectorAll=()=>[saved];
      getRawState('alpha').userRows=[0,1];
      const labels=[];for(let i=0;i<4;i++){jumpToLastUserMessage('alpha');labels.push(statusInfoEl.textContent)}
      return {labels,requests:test.requests.length,scrolled:getRawState('alpha').userScrolledUp};
    """)
    assert [label.split(".")[0] for label in state["labels"]] == [
        "Your message 3 of 3", "Your message 2 of 3", "Your message 1 of 3", "Your message 3 of 3"]
    assert state["requests"] == 0 and state["scrolled"] is True


def test_frozen_snapshot_paints_when_returning_via_chat_before_socket_reply():
    state = run_js("""
      test.installSwitch();const pane=test.element('raw-alpha');pane.textContent='Loading Codex...';
      const draft=test.element('cmd-chat-alpha');draft.value='Keep this draft';
      const st=getRawState('alpha');st.fullText='New background output';st.frozenText='Original held output';st.frozen=true;
      activeTabs.alpha='chat';switchTab('alpha','raw');
      return {paints:test.paints,top:pane.scrollTop,snapshot:_rawVisibleText(st),draft:draft.value,view:activeTabs.alpha};
    """)
    assert ["alpha", True] in state["paints"]
    assert state["top"] == 240 and state["snapshot"] == "Original held output"
    assert state["draft"] == "Keep this draft" and state["view"] == "raw"


def test_saved_credentials_are_rendered_as_text_not_html():
    state = run_js("""
      const row=_savedCredRow({label:'<img src=x>',value:'<script>synthetic</script>'});
      return row.children.map(child=>({text:child.textContent||'',html:child.innerHTML||''}));
    """)
    assert state[0] == {"text": "<img src=x>:", "html": ""}
    assert state[1] == {"text": "<script>synthetic</script>", "html": ""}


def test_saved_items_reject_bad_shapes_and_unsafe_links():
    state = run_js("""
      const pending=refreshSavedProjectItems('alpha');
      test.respond(0,{urls:['javascript:alert(1)','https://example.com/report',null],
        files:['/safe/report.md','javascript:alert(1)',9],creds:[null,{label:'<img>',value:'synthetic-only'},{value:9}]});
      await pending;return {items:_savedProjectItems.alpha,request:test.requests[0].url,paints:test.paints};
    """)
    assert state["items"] == {"urls": ["https://example.com/report"], "files": ["/safe/report.md"],
                              "creds": [{"label": "<img>", "value": "synthetic-only"}]}
    assert state["request"].endswith("/alpha/saved-items")
    assert state["paints"] == [["saved", "alpha"]]


def test_old_saved_items_request_cannot_clear_or_populate_replacement_request():
    state = run_js("""
      const old=refreshSavedProjectItems('alpha');const oldSignal=test.requests[0].options.signal;
      _clearSavedProjectState('alpha');_sessionClientEpoch.alpha=1;sessions[0].logical_incarnation='two';
      const next=refreshSavedProjectItems('alpha');const current=_savedProjectRequests.alpha;
      test.respond(0,{urls:['https://old.example'],creds:[],files:[]});await old;
      const preserved=_savedProjectRequests.alpha===current,empty=!_savedProjectItems.alpha;
      test.respond(1,{urls:['https://new.example'],creds:[],files:[]});await next;
      return {aborted:oldSignal.aborted,preserved,empty,items:_savedProjectItems.alpha};
    """)
    assert state["aborted"] and state["preserved"] and state["empty"]
    assert state["items"]["urls"] == ["https://new.example"]


def test_saved_items_are_throttled_and_not_fetched_for_hidden_or_missing_session():
    count = run_js("""
      document.hidden=true;await refreshSavedProjectItems('alpha');document.hidden=false;
      await refreshSavedProjectItems('missing');const first=refreshSavedProjectItems('alpha');
      await refreshSavedProjectItems('alpha');test.respond(0,{urls:[],creds:[],files:[]});await first;
      await refreshSavedProjectItems('alpha');return test.requests.length;
    """)
    assert count == 1


def test_rename_is_attribute_escaped_and_late_response_does_not_rename_replacement():
    state = run_js("""
      sessions[0].tab_label='name" autofocus onfocus="bad';showRenameSession('alpha');
      const html=test.element('modal-content').innerHTML;
      test.element('session-rename-input').value='New display name';test.element('session-rename-save');test.element('session-rename-error');
      const pending=renameSession('alpha');_sessionClientEpoch.alpha=1;sessions[0].tab_label='Replacement';
      test.respond(0,{ok:true,name:'alpha',tab_label:'New display name'});await pending;
      return {html,label:sessions[0].tab_label,count:test.counts().renames,body:JSON.parse(test.requests[0].options.body)};
    """)
    assert '&quot; autofocus onfocus=&quot;bad' in state["html"]
    assert 'value="name" autofocus' not in state["html"]
    assert state["label"] == "Replacement" and state["count"] == 0
    assert state["body"] == {"name": "New display name"}


def test_preferences_ignore_old_load_and_do_not_claim_wrong_default():
    state = run_js("""
      const old=loadPreferences(),current=loadPreferences();
      test.respond(1,{auto_session_names:true});await current;
      test.respond(0,{auto_session_names:false});await old;
      return test.element('settings-content').innerHTML;
    """)
    assert 'id="auto-session-names" checked' in state
    assert "Off by default" not in state


def test_users_usage_period_ignores_old_filter_response():
    state = run_js("""
      const old=loadUsersAdmin(),latest=setUsersUsagePeriod('today');
      test.respond(2,{users:[{id:'latest'}]});test.respond(3,{groups:[]});await latest;
      test.respond(0,{users:[{id:'old'}]});test.respond(1,{groups:[]});await old;
      await setUsersUsagePeriod('invalid');return {users:_usersCache,period:_usersUsagePeriod,requests:test.requests.map(r=>r.url)};
    """)
    assert state["users"] == [{"id": "latest"}] and state["period"] == "today"
    assert state["requests"][2].endswith("usage_period=today")
    assert len(state["requests"]) == 4


def test_browser_badge_uses_member_endpoint_and_active_lease_state():
    state = run_js("""
      _currentUser={id:'member1',role:'member'};const p=refreshBrowserAuthBadge();
      test.respond(0,{browser:{connected:true,active_leases:1}});await p;
      return {url:test.requests[0].url,cls:test.element('nav-browser-badge').className,label:test.element('nav-browser-badge').title};
    """)
    assert state == {"url": "/api/my/browser", "cls": "working", "label": "Browser working now"}


def test_browser_badge_ignores_old_owner_and_reports_unavailable_honestly():
    state = run_js("""
      const old=refreshBrowserAuthBadge();_currentUser={id:'member2',role:'member'};
      const current=refreshBrowserAuthBadge();test.respond(1,{},503);await current;
      test.respond(0,{sessions:[{id:'default',running:true}]});await old;
      const el=test.element('nav-browser-badge');return {label:el.title,cls:el.className,aborted:test.requests[0].options.signal.aborted};
    """)
    assert state == {"label": "Browser status unavailable", "cls": "unknown", "aborted": True}


def test_cpu_and_ram_header_show_real_values_then_stale_status():
    state = run_js("""
      let p=refreshNavStats();test.respond(0,{cpu_percent:61,memory:{total_mb:1000,used_mb:800}});await p;
      const first=[test.element('nav-cpu-summary-value').textContent,test.element('nav-ram-summary-value').textContent];
      p=refreshNavStats();test.respond(1,{},503);await p;
      return {first,stale:test.element('nav-cpu-summary-value').textContent,ram:test.element('nav-ram-summary-value').textContent};
    """)
    assert state["first"] == ["61%", "80%"]
    assert state["stale"] == "61% (stale)" and state["ram"] == "—"


def test_plan_bars_track_accessible_values_and_disappear_without_plan():
    state = run_js("""
      for(const id of ['plan-primary','plan-primary-label','plan-primary-value','plan-primary-meter','plan-primary-fill'])test.element(id);
      _setUsageWindow('primary',{label:'5 hours',utilization:41,resets_at:'later'});
      const meter={...test.element('plan-primary-meter').attributes};_setUsageWindow('primary',null);
      return {meter,hidden:test.element('plan-primary').style.display};
    """)
    assert state["meter"]["aria-valuenow"] == "41"
    assert state["hidden"] == "none"


def test_markup_preserves_mobile_plus_voice_shared_status_and_correct_asset_path():
    source = APP.read_text()
    html = re.search(r'^HTML_PAGE = r"""([\s\S]*?)^"""', source, re.M)[1]
    assert html.count('id="nav-browser-badge"') == 1
    assert 'type="button" title="Browser not connected"' in html
    assert 'class="nav-new-mobile-btn"' in html
    assert '.nav-plan-bars{display:none}' in html
    assert 'max-height:calc(5.6em + 24px)' in html
    assert "if(_currentUser) startBrowserAuthPolling();" in html
    assert 'id="session-composer-${s.name}"' in html
    assert 'id="term-live-${s.name}"' in html
    assert 'id="voice-mode-open-${s.name}"' in html
    assert 'src="__ROOT_PATH__/voice-mode.js"' in html
    assert "const tab=activeTabs[s.name]||'chat';" in html
    assert "if(st.frozen&&(!live||live._lineMode!==true))" in html
    assert "readTerminal(st.pendingReadAction,name)" in html
    assert "name_generated:!requested" in html
