"""Run the actual Chat UI JavaScript with synthetic, account-free transcripts."""

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
const src=fs.readFileSync(process.argv[1],'utf8'),input=JSON.parse(process.argv[2]);
const section=(a,b)=>src.slice(src.indexOf(a),src.indexOf(b,src.indexOf(a)));
const escape=s=>String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
const attr=s=>escape(s).replace(/"/g,'&quot;').replace(/'/g,'&#39;');
let markup='',paints=0,fetches=0,updates=0,now=1000000,incarnation='one';
const chat={scrollTop:30,scrollHeight:1000,clientHeight:200,
  get innerHTML(){return markup},set innerHTML(v){markup=v;paints++},
  querySelector(){return null},appendChild(){}};
const context=vm.createContext({console,Promise,JSON,Set,Map,Math,
  Date:{now:()=>now},BASE:'',MEMBER_SIMPLE:false,selectedSession:'demo',
  activeTabs:{demo:'chat'},chatMessages:{demo:input.messages||[]},
  sessions:[{name:'demo',logical_incarnation:'one'}],
  esc:escape,_escTermHtml:attr,fmtTime:ts=>'12:00',
  document:{hidden:false,getElementById:id=>id==='chat-demo'?chat:null},
  _sessionLogicalIncarnation:()=>incarnation,updateCard:()=>updates++,
  fetch:async()=>{fetches++;return {ok:true,json:async()=>({messages:[]})}},
});
vm.runInContext(section('/* ── Chat bubbles','function saveRawCache'),context);
vm.runInContext(section('function mergeChatMessages','function autoGrow'),context);
vm.runInContext(section('const _chatRefreshState={};','async function refreshFull'),context);
context.test={chat,setNow:v=>now=v,setIncarnation:v=>incarnation=v,
  counts:()=>({paints,fetches,updates}),markup:()=>markup};
Promise.resolve(vm.runInContext(input.code,context)).then(result=>
  process.stdout.write(JSON.stringify(result))
).catch(error=>{console.error(error);process.exitCode=1});
"""


def run_js(code, messages=None):
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(APP), json.dumps({"code": code, "messages": messages or []})],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_assistant_defaults_to_short_summary_with_expandable_code_free_details():
    state = run_js(r"""
      const m={role:'assistant',id:'reply-1',kind:'final',ts:100,
        text:'Button restored. Mobile layout checked.',
        full:'Button restored. Mobile layout checked. '+('Extra explanation. '.repeat(50))+
          '\n```python\ndef leak():\n    return secret\n```\nThe checks passed.'};
      ({html:chatBubbleInner(m,'demo'),brief:_chatBrief(m.full)})
    """)
    assert 'chat-preview">Button restored. Mobile layout checked.' in state["html"]
    assert 'class="chat-body chat-expanded" hidden' in state["html"]
    assert 'aria-expanded="false"' in state["html"] and "Read more" in state["html"]
    assert "The checks passed." in state["html"]
    assert "def leak" not in state["html"] and "return secret" not in state["html"]
    assert len(state["brief"]) <= 361


def test_short_reply_has_no_read_more_and_user_message_is_not_summarized():
    state = run_js("""
      ({reply:chatBubbleInner({role:'assistant',text:'Fixed. Tests pass.'},'demo'),
        user:chatBubbleInner({role:'user',text:'Please keep `sample_code` unchanged.'},'demo')})
    """)
    assert "Read more" not in state["reply"]
    assert "<code>sample_code</code>" in state["user"]


@pytest.mark.parametrize("code", [
    "const token = 'secret';", "def leak():", "import secret", "git push origin main",
    '  "secret": "value",', "secret = 'value'", "console.log(secret);",
    "    return True", '<div class="code">Secret</div>',
    "~~~python\nsecret = 'value'\n~~~", "```\nsecret = 'value'\n```",
])
def test_legacy_assistant_code_is_not_shown_even_when_expanded(code):
    text = json.dumps("Fixed the layout.\n" + code + "\nTests pass.")
    result = run_js(f"_chatPlainProse({text})")
    assert result == "Fixed the layout.\nTests pass."


def test_summary_retains_urls_and_limits_sentences():
    result = run_js("_chatBrief('Open https://example.com/report. Result ready. Tests pass. More words follow.')")
    assert result == "Open https://example.com/report. Result ready. Tests pass…"


def test_html_message_ids_and_link_labels_are_escaped_and_unsafe_links_rejected():
    state = run_js("""
      chatMessages.demo=[{role:'assistant',id:'x" onmouseover="alert(1)',
        text:'<img src=x onerror=alert(1)> Fixed.',
        links:[{kind:'url',href:'javascript:alert(1)',label:'bad'},
          {kind:'url',href:'https://example.com/',label:'<img src=x>'},
          {kind:'file',path:'~/report.md',label:'Report'}]}];
      renderChatBubbles('demo')
    """)
    assert "<img" not in state
    assert 'data-message-key="[&quot;demo&quot;' in state
    assert '&quot; onmouseover=' in state
    assert '" onmouseover="' not in state
    assert "javascript:" not in state
    assert 'href="https://example.com/"' in state
    assert '/file?path=~%2Freport.md&amp;session=demo' in state


def test_malformed_saved_links_do_not_hide_the_assistant_reply():
    state = run_js("""
      ({array:chatBubbleInner({role:'assistant',text:'Fixed.',links:[null,{href:{}}]},'demo'),
        object:chatBubbleInner({role:'assistant',text:'Fixed.',links:{}},'demo')})
    """)
    assert "Fixed." in state["array"] and "Fixed." in state["object"]


def test_read_more_toggle_survives_repainting_and_can_collapse():
    state = run_js("""
      const m={role:'assistant',id:'reply',text:'Done.',full:'Done. '+('More detail. '.repeat(10))};
      const preview={hidden:false},details={hidden:true},attrs={'aria-expanded':'false'};
      const bubble={dataset:{messageKey:_chatMessageKey('demo',m)},
        querySelector:s=>s==='.chat-preview'?preview:details};
      const button={closest:()=>bubble,getAttribute:k=>attrs[k],
        setAttribute:(k,v)=>attrs[k]=v,textContent:'Read more'};
      toggleChatMessage(button);
      const expanded=chatBubbleInner(m,'demo');
      const first={preview:preview.hidden,details:details.hidden,text:button.textContent};
      toggleChatMessage(button);
      ({first,expanded,collapsed:chatBubbleInner(m,'demo'),text:button.textContent})
    """)
    assert state["first"] == {"preview": True, "details": False, "text": "Read less"}
    assert 'aria-expanded="true"' in state["expanded"]
    assert 'class="chat-body chat-expanded" hidden' in state["collapsed"]
    assert state["text"] == "Read more"


def test_full_timeline_preserves_two_progress_updates_and_final_without_duplicates():
    state = run_js("""
      const history=[{role:'user',id:'u1',text:'Build it',ts:1},
        {role:'assistant',id:'p1',turn_id:'t1',kind:'progress',text:'Layout done.',ts:1201},
        {role:'assistant',id:'p2',turn_id:'t1',kind:'progress',text:'Tests running.',ts:2401},
        {role:'assistant',id:'f1',turn_id:'t1',kind:'final',text:'Ready.',ts:3601},
        {role:'user',id:'u2',text:'Another request',ts:3700},
        {role:'assistant',id:'f2',turn_id:'t2',kind:'final',text:'Ready.',ts:3800}];
      reconcileAssistantSummary('demo',history);
      const first=test.counts().paints;
      reconcileAssistantSummary('demo',history);
      ({messages:chatMessages.demo,html:test.markup(),first,paints:test.counts().paints})
    """)
    assert [m["id"] for m in state["messages"]] == ["u1", "p1", "p2", "f1", "u2", "f2"]
    assert state["html"].count(">Still working<") == 2
    assert state["html"].count(">Reply<") == 2
    assert state["first"] == state["paints"] == 1


def test_changed_server_summary_replaces_old_copy_but_preserves_pending_user():
    state = run_js("""
      selectedSession=null;
      chatMessages.demo=[{role:'assistant',id:'f1',text:'Old wording',ts:100}];
      appendChatBubble('demo','user','Next request',200);
      mergeChatMessages('demo',[{role:'assistant',id:'f1',text:'New wording',ts:100}]);
      const pending=chatMessages.demo.map(m=>m.text);
      mergeChatMessages('demo',[{role:'assistant',id:'f1',text:'New wording',ts:100},
        {role:'user',id:'u2',text:'Next request',ts:201}]);
      ({pending,messages:chatMessages.demo})
    """)
    assert state["pending"] == ["New wording", "Next request"]
    assert len(state["messages"]) == 2
    assert "_optimistic" not in state["messages"][1]


def test_repeated_prompt_on_different_turn_is_not_deduplicated():
    state = run_js("""
      selectedSession=null;
      appendChatBubble('demo','user','Again',200);
      mergeChatMessages('demo',[{role:'user',id:'u1',text:'Again',ts:100}]);
      chatMessages.demo
    """)
    assert len(state) == 2


def test_local_upload_failure_notice_survives_server_history_refresh():
    state = run_js("""
      selectedSession=null;
      appendChatBubble('demo','assistant','Upload failed: network error',200);
      mergeChatMessages('demo',[{role:'user',id:'u1',text:'Hello',ts:100}]);
      chatMessages.demo
    """)
    assert [m["text"] for m in state] == ["Hello", "Upload failed: network error"]


def test_inserted_older_assistant_reply_keeps_optimistic_user_and_correct_order():
    state = run_js("""
      selectedSession=null;
      appendChatBubble('demo','user','Latest request',200);
      mergeChatMessages('demo',[{role:'user',id:'u1',text:'Earlier request',ts:100},
        {role:'assistant',id:'f1',text:'Earlier answer',ts:150}]);
      chatMessages.demo
    """)
    assert [m["text"] for m in state] == ["Earlier request", "Earlier answer", "Latest request"]


def test_saved_chat_scroll_is_restored_after_detail_dom_rebuild():
    state = run_js("""
      document.getElementById=id=>id==='chat-demo'?test.chat:
        id==='tab-chat-demo'?{classList:{contains:()=>true}}:null;
      test.chat.scrollTop=135;saveChatViewState();
      test.chat.scrollTop=0;restoreChatViewState('demo',test.chat);
      ({top:test.chat.scrollTop})
    """)
    assert state["top"] == 135


def test_reconciliation_restores_keyboard_focus_to_same_read_more_button():
    state = run_js("""
      const attrs={dataset:{messageKey:'reply-key'}};
      let focused=false;
      document.activeElement={classList:{contains:()=>true},closest:()=>attrs};
      test.chat.contains=()=>true;
      test.chat.querySelectorAll=()=>[{closest:()=>attrs,focus:opts=>focused=opts.preventScroll}];
      reconcileAssistantSummary('demo',[{role:'assistant',id:'r1',text:'Ready',ts:100}]);
      ({focused})
    """)
    assert state["focused"] is True


def test_background_update_preserves_reader_scroll_position():
    state = run_js("""
      test.chat.scrollTop=125;
      reconcileAssistantSummary('demo',[{role:'assistant',id:'f1',text:'Ready',ts:1}]);
      const held=test.chat.scrollTop;
      test.chat.scrollTop=790;
      reconcileAssistantSummary('demo',[{role:'assistant',id:'f1',text:'Ready now',ts:1}]);
      ({held,followed:test.chat.scrollTop})
    """)
    assert state == {"held": 125, "followed": 1000}


def test_backfilled_earlier_reply_keeps_visible_message_anchored():
    state = run_js("""
      test.chat.scrollTop=125;
      test.chat.getBoundingClientRect=()=>({top:100});
      const bubble={dataset:{messageKey:'existing'},
        getBoundingClientRect:()=>({top:test.counts().paints?280:80,bottom:350})};
      test.chat.querySelectorAll=()=>[bubble];
      reconcileAssistantSummary('demo',[{role:'assistant',id:'old',text:'Older reply',ts:1}]);
      ({top:test.chat.scrollTop})
    """)
    assert state["top"] == 325


@pytest.mark.parametrize("simple", [False, True])
@pytest.mark.parametrize("selected", [None, "chat", "raw", "info"])
def test_initial_view_defaults_to_chat_and_preserves_explicit_choice(simple, selected):
    render_detail = APP.read_text().split("function renderDetail(){", 1)[1]
    expression = re.search(r"const tab=(.+);", render_detail).group(1)
    state = run_js(f"""
      MEMBER_SIMPLE={json.dumps(simple)};
      activeTabs.demo={json.dumps(selected)};
      const s=sessions[0];
      ({expression})
    """)
    assert state == (selected or "chat")


@pytest.mark.parametrize("simple", [False, True])
def test_default_chat_refreshes_before_any_view_is_selected(simple):
    state = run_js(f"""
      (async()=>{{
        MEMBER_SIMPLE={json.dumps(simple)};
        delete activeTabs.demo;
        await refreshActiveChat();
        return test.counts();
      }})()
    """)
    assert state["fetches"] == state["updates"] == 1


def test_active_chat_refreshes_without_status_changes_and_is_throttled():
    state = run_js("""
      (async()=>{
        await refreshActiveChat();await refreshActiveChat();
        test.setNow(1030000);await refreshActiveChat();
        document.hidden=true;await refreshActiveChat(true);
        document.hidden=false;activeTabs.demo='raw';await refreshActiveChat(true);
        return test.counts();
      })()
    """)
    assert state["fetches"] == state["updates"] == 2


def test_chat_refresh_ignores_response_from_replaced_session():
    state = run_js("""
      (async()=>{
        let finish;fetch=()=>new Promise(resolve=>{finish=resolve});
        const pending=refreshActiveChat();
        test.setIncarnation('two');
        finish({ok:true,json:async()=>({messages:[{role:'assistant',text:'Old session'}]})});
        await pending;return test.counts();
      })()
    """)
    assert state["updates"] == 0


def test_chat_refresh_has_incarnation_and_visibility_guards_in_live_wiring():
    source = APP.read_text()
    assert "if(tab==='chat')refreshActiveChat();" in source
    assert "refreshActiveChat(true);" in source
    assert "await reconcileSessionRoster();\n  refreshActiveChat();" in source
