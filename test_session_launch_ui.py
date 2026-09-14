"""Execute session creation and message navigation in a small DOM harness."""
import json
import subprocess
from pathlib import Path

APP = Path(__file__).with_name('app.py')
DRIVER = r'''
const fs=require('fs'),vm=require('vm'),source=fs.readFileSync(process.argv[1],'utf8');
const input=JSON.parse(process.argv[2]);
const nodes={};const requests=[];const noop=()=>{};
const node=()=>({innerHTML:'',textContent:'',value:'',style:{},classList:{add:noop,remove:noop,toggle:noop},focus:noop});
for(const id of ['modal-overlay','modal-content','new-session-name','session-create-error','new-session-model','new-session-effort','new-session-nofb'])nodes[id]=node();
nodes['new-session-name'].value=input.name||'';
nodes['new-session-model'].value=input.model||'gpt-6-astra';
nodes['new-session-effort'].value=input.effort||'max';
nodes['new-session-nofb'].checked=!!input.no_fallback;
const st={userRows:[1,3],userScrolledUp:false};
nodes['raw-alpha']={scrollTop:0,scrollHeight:900,children:[0,1,2,3].map(i=>({offsetTop:i*100,classList:{add:noop,remove:noop}}))};
const context=vm.createContext({console,Promise,Math,Date,Uint8Array,BASE:'',crypto:{getRandomValues:a=>a.fill(2)},
 document:{getElementById:id=>id.startsWith('session-create-progress-')?node():nodes[id]||null},
 selectedSession:'old',sessions:[{name:'old'}],_sessionCreateRun:0,esc:String,
 setTimeout:fn=>fn(),statusInfoEl:node(),getRawState:()=>st,_setRawScroll:(el,y)=>el.scrollTop=y,
 selectSession:name=>context.selectedSession=name,
 loadAll:async()=>{context.selectedSession='old';context.sessions.push({name:'created'})},closeModal:noop,
 fetch:async(url,options)=>{if(url.endsWith('/create')){requests.push(JSON.parse(options.body));return {ok:!input.conflict,status:input.conflict?409:200,json:async()=>input.conflict?{code:'name_conflict',error:'Already exists'}:{ok:true,name:'created'}}}return {ok:true,json:async()=>({authoritative:true,sessions:[{name:'created',runtime_state:'active'}]})}}
});
vm.runInContext(source.slice(source.indexOf('let MODEL_CHOICES='),source.indexOf('function modelChoiceLabel(')),context);
(async()=>{
 if(input.action==='create'){
  vm.runInContext(source.slice(source.indexOf('let _sessionCreatePending='),source.indexOf('// ── Connections (Drive')),context);
  await context.createSession();
 }else if(input.action==='form'){
  vm.runInContext(source.slice(source.indexOf('let _sessionCreatePending='),source.indexOf('// ── Connections (Drive')),context);
  context.showCreateModal();
 }else if(input.action==='jump'){
  vm.runInContext(source.slice(source.indexOf('function jumpToLastUserMessage('),source.indexOf('function rerenderAllRaw(')),context);
  context.jumpToLastUserMessage('alpha');const first=nodes['raw-alpha'].scrollTop;
  context.jumpToLastUserMessage('alpha');const second=nodes['raw-alpha'].scrollTop;
  context.jumpToLive('alpha');context.jumpResult={first,second,last:nodes['raw-alpha'].scrollTop,following:!st.userScrolledUp};
 }
 process.stdout.write(JSON.stringify({requests,selected:context.selectedSession,html:nodes['modal-content'].innerHTML,error:nodes['session-create-error'].textContent,efforts:nodes['new-session-effort'].innerHTML,jump:context.jumpResult}));
})().catch(e=>{console.error(e);process.exit(1)});
'''

def run(**scenario):
    result = subprocess.run(['node','-e',DRIVER,str(APP),json.dumps(scenario)],capture_output=True,text=True,timeout=10)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_plus_opens_blank_name_form_without_creating():
    state=run(action='form')
    assert 'id="new-session-name"' in state['html']
    assert state['requests'] == []


def test_typed_name_is_used_and_created_session_opens_after_roster_refresh():
    state=run(action='create',name='my-project')
    assert state['requests'] == [{'name':'my-project','name_generated':False,'model':'gpt-6-astra','effort':'max','no_fallback':False}]
    assert state['selected'] == 'created'


def test_only_empty_submission_generates_a_name():
    state=run(action='create',name='   ')
    assert len(state['requests'][0]['name']) == 8
    assert state['requests'][0]['name_generated'] is True


def test_generated_name_collision_retries_keep_automatic_naming_enabled():
    state=run(action='create',name='',conflict=True)
    assert len(state['requests']) == 5
    assert all(row['name_generated'] is True and len(row['name']) == 8 for row in state['requests'])
    assert state['selected'] == 'old'


def test_named_collision_never_falls_back_to_random_name():
    state=run(action='create',name='taken',conflict=True)
    assert state['requests'] == [{'name':'taken','name_generated':False,'model':'gpt-6-astra','effort':'max','no_fallback':False}]
    assert state['selected'] == 'old'


def test_session_routes_are_addressable_and_preserve_browser_navigation():
    script = r'''
const fs=require('fs'),vm=require('vm'),s=fs.readFileSync(process.argv[1],'utf8');
const updates=[],listeners={};
const context=vm.createContext({location:{hash:'#/session/my%20project',pathname:'/',search:''},
 history:{pushState:(_a,_b,url)=>updates.push(url)},window:{addEventListener:(name,fn)=>listeners[name]=fn},
 sessions:[{name:'my project'}],selectedSession:'old',stopAllRawPolling:()=>{},_acknowledgeCompletion:()=>{},
 navEl:{querySelectorAll:()=>[]},document:{getElementById:()=>null},renderDetail:()=>{}});
vm.runInContext(s.slice(s.indexOf('function _sessionRouteName('),s.indexOf('function switchTab(')),context);
const parsed=context._sessionRouteName();context.selectSession('my project');
context.selectedSession='old';listeners.popstate();
process.stdout.write(JSON.stringify({parsed,updates,selected:context.selectedSession}));
'''
    response=subprocess.run(['node','-e',script,str(APP)],capture_output=True,text=True)
    assert response.returncode == 0, response.stderr
    assert json.loads(response.stdout) == {'parsed':'my project','updates':[],'selected':'my project'}


def test_creation_form_defaults_and_effort_cap():
    state=run(action='form')
    assert '<option value="gpt-6-astra" selected>' in state['html']
    assert '<option value="max" selected>Max</option>' in state['efforts']
    assert '<option value="ultra" selected>' not in state['efforts']
    assert 'id="new-session-nofb" type="checkbox"  ' in state['html']
    limited=run(action='form',model='gpt-5.5')
    assert '<option value="xhigh" selected>' in limited['efforts']
    assert 'value="ultra"' not in limited['efforts']


def test_custom_model_effort_and_opt_out_are_submitted():
    state=run(action='create',name='custom',model='gpt-5.6-luna',effort='high',no_fallback=True)
    assert state['requests']==[{'name':'custom','name_generated':False,'model':'gpt-5.6-luna','effort':'high','no_fallback':True}]
    assert state['selected']=='created'


def test_failed_creation_preserves_selected_model_effort_and_opt_out():
    state=run(action='create',name='taken',model='gpt-5.6-luna',effort='high',no_fallback=True,conflict=True)
    assert '<option value="gpt-5.6-luna" selected>' in state['html']
    assert '<option value="high" selected>' in state['efforts']
    assert 'id="new-session-nofb" type="checkbox" checked' in state['html']
