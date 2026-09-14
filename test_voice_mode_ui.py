"""Voice Mode lifecycle probes with fake media, sockets and a deterministic DOM."""

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from test_shared_composer_ui import MARKUP_DRIVER, MarkupElements


ROOT = Path(__file__).parent
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


DRIVER = r"""
const fs=require('fs'),vm=require('vm'),scenario=process.argv[3];
const source=fs.readFileSync(process.argv[1]+'/app.py','utf8'),quotes='"'.repeat(3);
const html=source.match(new RegExp('^HTML_PAGE = r'+quotes+'([\\s\\S]*?)^'+quotes,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const media=[],sockets=[],pcs=[],recorders=[],requests=[],alerts=[],dispatch=[],confirmations=[],timers=new Map(),listeners={};
let timerId=0,resolveStop,resolveTranscript,pendingTranscript;
const noop=()=>{},all=[];
class Element {
  constructor(tag='div'){this.tagName=tag.toUpperCase();this.children=[];this.parentNode=null;this.dataset={};this.attrs={};this.style={};this.id='';this.className='';this.value='';this.hidden=false;this.disabled=false;this.textContent='';this.isConnected=false;
    this.classList={contains:name=>this.className.split(' ').includes(name),add:(...names)=>{this.className=[...new Set([...this.className.split(' ').filter(Boolean),...names])].join(' ')},remove:(...names)=>{this.className=this.className.split(' ').filter(name=>!names.includes(name)).join(' ')},toggle:(name,on)=>{on?this.classList.add(name):this.classList.remove(name)}};all.push(this)}
  connected(value){this.isConnected=value;for(const child of this.children)child.connected(value)}
  append(...nodes){for(const node of nodes){node.remove();node.parentNode=this;node.connected(this.isConnected);this.children.push(node)}}
  appendChild(node){this.append(node);return node}
  remove(){if(this.parentNode)this.parentNode.children=this.parentNode.children.filter(node=>node!==this);this.parentNode=null;this.connected(false)}
  before(node){this.parentNode.append(node)}
  after(node){this.parentNode.append(node)}
  focus(){document.activeElement=this}
  setAttribute(name,value){this.attrs[name]=String(value);if(name==='id')this.id=value}
  getAttribute(name){return this.attrs[name]||null}
  addEventListener(type,callback){this['on'+type]=callback}
  querySelectorAll(selector){return this.children.flatMap(child=>[...(selector==='[id]'&&child.id?[child]:[]),...child.querySelectorAll(selector)])}
  play(){return Promise.resolve()}
  pause(){}
  set innerHTML(value){this._html=value;this.children=[];
    for(const match of value.matchAll(/<([a-z][\w-]*)\b([^>]*)>/gi)){
      const id=match[2].match(/\bid="([^"]+)"/);if(!id)continue;
      const child=new Element(match[1]);child.id=id[1];child.hidden=/\bhidden\b/.test(match[2]);child.disabled=/\bdisabled\b/.test(match[2]);
      for(const attr of match[2].matchAll(/([\w-]+)="([^"]*)"/g))child.setAttribute(attr[1],attr[2]);
      this.append(child);
    }}
  get innerHTML(){return this._html||''}
}
const body=new Element('body'),head=new Element('head');body.isConnected=head.isConnected=true;
const document={body,head,hidden:false,activeElement:null,createElement:tag=>new Element(tag),
  getElementById:id=>all.find(node=>node.id===id&&node.isConnected)||null,
  querySelectorAll:selector=>all.filter(node=>node.isConnected&&selector.includes('voice-mode-open-')&&node.id.startsWith('voice-mode-open-')),
  addEventListener:(type,callback)=>{(listeners['document:'+type]||=[]).push(callback)}};
for(const id of ['main','cmd-chat-alpha','cmd-send-chat-alpha','voice-mode-open-alpha','cmd-chat-beta','cmd-send-chat-beta','voice-mode-open-beta']){
  const node=new Element(id.startsWith('cmd-chat')?'textarea':'button');node.id=id;body.append(node);
}
document.getElementById('cmd-chat-alpha').value='Unsent private draft';
document.getElementById('cmd-chat-alpha').focus();
for(const name of ['alpha','beta'])document.getElementById('voice-mode-open-'+name).setAttribute('aria-label','Start Voice Mode');
const track=()=>({enabled:true,stops:0,stop(){this.stops++;this.enabled=false}});
const makeStream=()=>{const t=track();return {track:t,getTracks:()=>[t],getAudioTracks:()=>[t]}};
class Socket {static OPEN=1;constructor(url){this.url=url;this.readyState=0;this.sent=[];this.closed=0;sockets.push(this)}send(data){this.sent.push(JSON.parse(data))}close(){this.closed++;this.readyState=3}}
class Peer {constructor(){this.closed=0;this.connectionState='new';pcs.push(this)}addTrack(){}createDataChannel(){return this.events={readyState:'open',sent:[],closed:0,send(data){this.sent.push(JSON.parse(data))},close(){this.closed++;this.readyState='closed'}}}async createOffer(){if(scenario==='offer-failure')throw new Error('offer failed');return {sdp:'offer'}}async setLocalDescription(){}async setRemoteDescription(){}close(){this.closed++;this.connectionState='closed'}}
const window={RTCPeerConnection:Peer,confirm:message=>{confirmations.push(message);return scenario!=='advanced-decline'},addEventListener:(type,callback)=>{(listeners[type]||=[]).push(callback)}};
const context=vm.createContext({console,Promise,Map,Set,WeakMap,Math,Number,String,Array,Date,JSON,URL,Blob,FormData,AbortController,
  window,document,location:{href:'https://codex.lisa.my/',protocol:'https:'},BASE:'',
  sessions:[{name:'alpha',logical_incarnation:'one'},{name:'beta',logical_incarnation:'two'}],selectedSession:'alpha',
  _sessionClientEpoch:{},_composerAttachments:{},_composerUploadTasks:{},activeTabs:{alpha:'chat'},
  _sessionLogicalIncarnation:name=>name==='alpha'?'one':'two',_sessionComposerView:name=>context.activeTabs[name]||'chat',
  _sessionComposerKey:(name,source)=>'chat-'+name,
  _composerElement:id=>document.getElementById(id),autoGrow:noop,sendChat:name=>dispatch.push(['chat',name]),sendCmd:name=>dispatch.push(['raw',name]),
  navigator:{mediaDevices:{getUserMedia:options=>new Promise((resolve,reject)=>media.push({resolve,reject,options}))}},
  RTCPeerConnection:Peer,WebSocket:Socket,MediaRecorder:class {constructor(stream){this.stream=stream;this.state='inactive';recorders.push(this)}start(){this.state='recording'}stop(){this.state='inactive';pendingTranscript=this.onstop?.()}},
  setTimeout:callback=>{timers.set(++timerId,callback);return timerId},clearTimeout:id=>timers.delete(id),
  setInterval:callback=>{timers.set(++timerId,callback);return timerId},clearInterval:id=>timers.delete(id),
  alert:value=>alerts.push(value),fetch:async(url,options)=>{requests.push({url,options});if(url.endsWith('/transcribe'))return new Promise(resolve=>{resolveTranscript=resolve});if(url.endsWith('/voice/stop')&&['delayed-stop','delayed-stop-reopen','timeout-stop'].includes(scenario))return new Promise(resolve=>{resolveStop=resolve});return {ok:true,json:async()=>({active:scenario==='observer-close'})}},
});
const start=js.indexOf('const _COMPOSER_SEND_SVG='),end=js.indexOf('async function sendChat(',start);
vm.runInContext(js.slice(start,end),context);
vm.runInContext(fs.readFileSync(process.argv[1]+'/voice-mode.js','utf8'),context);
const node=id=>document.getElementById('voice-'+id),settle=()=>new Promise(resolve=>setImmediate(resolve));
const emit=async(type,values={})=>{for(const callback of listeners[type]||[])await callback({key:'',preventDefault:noop,...values});await settle()};
function snapshot(){return {voice:window.voiceMode.state(),media:media.length,peers:pcs.length,sockets:sockets.length,
  stopped:media.filter(item=>item.stream).map(item=>item.stream.track.stops),
  enabled:media.filter(item=>item.stream).map(item=>item.stream.track.enabled),
  peerClosed:pcs.map(item=>item.closed),socketClosed:sockets.map(item=>item.closed),
  channelClosed:pcs.map(item=>item.events?.closed||0),busy:window.dashboardMicrophone.busy(),
  draft:document.getElementById('cmd-chat-alpha').value,status:node('status')?.textContent||'',timers:timers.size,
  focused:document.activeElement?.id,button:document.getElementById('cmd-send-chat-alpha').getAttribute('aria-label'),
  waveform:document.getElementById('voice-mode-open-alpha').getAttribute('aria-label'),
  advanced:['supervision','checkin'].map(option=>({option,checked:!!node(option)?.checked,disabled:!!node(option)?.disabled})),
  releaseHidden:node('release-hold')?.hidden}}
async function grant(index=0){const item=media[index];item.stream=makeStream();item.resolve(item.stream);await settle()}
(async()=>{
  const states=[snapshot()];
  await window.voiceMode.open('alpha');states.push(snapshot());
  if(scenario==='open')return {states,requests,alerts};
  if(scenario==='observer-close'){await window.voiceMode.close();states.push(snapshot());return {states,requests,alerts};}
  if(scenario==='composer-actions'){
    const input=document.getElementById('cmd-chat-alpha');
    for(const view of ['chat','raw','chat']){
      context.activeTabs.alpha=view;
      for(const text of ['', '   ', 'typed content']){input.value=text;context.updateComposerBtn('chat-alpha');states.push(snapshot())}
      context.composerAction('chat-alpha');
    }
    input.value='';context._composerAttachments['chat-alpha']=[{path:'/test/image.png'}];context.updateComposerBtn('chat-alpha');states.push(snapshot());
    return {states,requests,alerts,dispatch};
  }
  if(scenario==='dictation-first'){
    const recording=context.toggleRecording('chat-alpha');await settle();context.toggleRecording('chat-alpha');
    await node('start').onclick();states.push(snapshot());await grant();await recording;
    window.dashboardMicrophone.cancelSession('alpha');states.push(snapshot());return {states,requests,alerts};
  }
  if(scenario.startsWith('dictation-pagehide')||scenario==='transcription-owner'){
    const recording=context.toggleRecording('chat-alpha');await settle();
    if(scenario==='dictation-pagehide-permission'){await emit('pagehide');await grant();await recording;states.push(snapshot());return {states,requests,alerts};}
    await grant();await recording;
    if(scenario==='dictation-pagehide-recording'){await emit('pagehide');states.push(snapshot());return {states,requests,alerts};}
    recorders[0].ondataavailable({data:new Blob(['spoken'])});recorders[0].stop();await settle();
    await node('start').onclick();states.push(snapshot());
    resolveTranscript({ok:true,json:async()=>({text:'Voice transcript'})});await pendingTranscript;states.push(snapshot());
    return {states,requests,alerts};
  }
  const pending=node('start').onclick();await settle();states.push(snapshot());
  if(scenario==='voice-first'){await context.toggleRecording('chat-beta');states.push(snapshot())}
  if(['close-permission','pagehide-permission','invalidate-permission','epoch-permission'].includes(scenario)){
    if(scenario==='close-permission')await window.voiceMode.close();
    else if(scenario==='pagehide-permission')await emit('pagehide');
    else{context._sessionClientEpoch.alpha=1;if(scenario!=='epoch-permission')window.voiceMode.invalidate('alpha')}
    states.push(snapshot());await grant();await pending;states.push(snapshot());return {states,requests,alerts};
  }
  if(scenario==='denied'){media[0].reject(Object.assign(new Error('denied'),{name:'NotAllowedError'}));await pending;states.push(snapshot());return {states,requests,alerts}}
  await grant();await pending;states.push(snapshot());
  if(scenario==='offer-failure')return {states,requests,alerts};
  const socket=sockets[0];socket.readyState=Socket.OPEN;socket.onopen();
  if(scenario!=='unbound-close')await socket.onmessage({data:JSON.stringify({type:'binding',nonce:'our-connection',generation:'our-generation',root:scenario==='advanced-rootless'?'':'root-alpha',held:scenario==='advanced-release'})});
  if(scenario==='changed-binding'){
    await socket.onmessage({data:JSON.stringify({type:'binding',nonce:'different-connection',generation:'our-generation'})});
    states.push(snapshot());return {states,requests,alerts};
  }
  pcs[0].events.onmessage({data:JSON.stringify({type:'session.started'})});states.push(snapshot());
  if(scenario.startsWith('advanced-')){
    const option=scenario==='advanced-checkin'?'checkin':'supervision';
    if(scenario==='advanced-release')node('release-hold').onclick();
    else node(option).onchange({target:{checked:true}});
    states.push(snapshot());
    if(['advanced-supervision','advanced-checkin','advanced-release'].includes(scenario)){
      await socket.onmessage({data:JSON.stringify({type:'advanced',supervision:scenario==='advanced-supervision',checkin:scenario==='advanced-checkin',held:false})});
      states.push(snapshot());
    }
    if(scenario==='advanced-timeout'){for(const callback of [...timers.values()])callback();states.push(snapshot())}
    await window.voiceMode.close();states.push(snapshot());
    await window.voiceMode.open('alpha');states.push(snapshot());
    return {states,requests,alerts,confirmations,sent:socket.sent};
  }
  if(scenario.startsWith('nested-')){
    pcs[0].ontrack({streams:[media[0].stream]});await settle();
    const greeting=pcs[0].events.sent.find(event=>event.type==='session.instructions.append');
    const eventId=scenario==='nested-greeting-error'?greeting.event_id:
      scenario==='nested-updates-error'?'updates_test':scenario==='nested-approval-error'?'approval_test':'mic_test';
    pcs[0].events.onmessage({data:JSON.stringify({type:'error',error:{client_event_id:eventId,message:'mock protocol error'}})});
    states.push(snapshot());await window.voiceMode.close();states.push(snapshot());return {states,requests,alerts};
  }
  if(scenario==='keyboard'){
    node('mode').value='push';node('mode').onchange();states.push(snapshot());
    await emit('keydown',{key:'Control',code:'ControlLeft'});states.push(snapshot());
    await emit('keyup',{key:'Control',code:'ControlLeft'});states.push(snapshot());
    node('hold').onkeydown({key:'Enter',repeat:false,preventDefault:noop});states.push(snapshot());
    node('hold').onkeyup({key:'Enter',preventDefault:noop});states.push(snapshot());
    node('hold').onpointerdown({button:0,pointerId:1,preventDefault:noop});states.push(snapshot());
    await emit('pointercancel',{pointerId:1});states.push(snapshot());
    node('mode').value='continuous';node('mode').onchange();states.push(snapshot());
    document.hidden=true;await emit('document:visibilitychange');states.push(snapshot());
    document.hidden=false;await emit('document:visibilitychange');states.push(snapshot());
    await window.voiceMode.close();states.push(snapshot());return {states,requests,alerts};
  }
  if(scenario==='pagehide')await emit('pagehide');
  else if(scenario==='invalidate'){context._sessionClientEpoch.alpha=1;window.voiceMode.invalidate('alpha')}
  else if(scenario==='channel-close'){pcs[0].events.onclose()}
  else if(['delayed-stop','delayed-stop-reopen','timeout-stop'].includes(scenario)){
    const stopped=window.voiceMode.close();await settle();states.push(snapshot());
    if(scenario==='delayed-stop-reopen'){await window.voiceMode.open('beta');states.push(snapshot())}
    if(scenario==='timeout-stop'){for(const callback of [...timers.values()])callback()}
    else resolveStop({ok:true});
    await stopped;
  }else await window.voiceMode.close();
  states.push(snapshot());return {states,requests,alerts,sent:socket.sent};
})().then(value=>process.stdout.write(JSON.stringify(value))).catch(error=>{console.error(error);process.exit(1)});
"""


def run_voice(scenario):
    result = subprocess.run([NODE, "-e", DRIVER, str(ROOT), "unused", scenario], capture_output=True,
                            text=True, timeout=15)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_loading_and_opening_voice_controls_never_request_microphone():
    result = run_voice("open")
    assert [row["media"] for row in result["states"]] == [0, 0]
    assert result["states"][-1]["voice"]["open"] is True
    assert result["states"][-1]["draft"] == "Unsent private draft"
    assert not result["states"][-1]["busy"]


def test_connect_keeps_microphone_muted_until_voice_session_is_ready():
    result = run_voice("connect")
    assert result["states"][3]["enabled"] == [False]
    assert result["states"][4]["enabled"] == [True]
    assert all(count >= 1 for count in result["states"][-1]["stopped"])
    assert not result["states"][-1]["busy"]
    assert result["states"][-1]["draft"] == "Unsent private draft"
    assert result["states"][-1]["focused"] == "cmd-chat-alpha"


@pytest.mark.parametrize("scenario", ["close-permission", "pagehide-permission", "invalidate-permission", "epoch-permission"])
def test_cancelled_permission_request_cannot_start_late_audio_or_connections(scenario):
    final = run_voice(scenario)["states"][-1]
    assert final["media"] == 1
    assert len(final["stopped"]) == 1 and final["stopped"][0] >= 1
    assert final["peers"] == final["sockets"] == 0
    assert not final["busy"]


def test_duplicate_dictation_and_voice_connect_share_one_pending_microphone_owner():
    result = run_voice("dictation-first")
    assert result["states"][2]["media"] == 1
    assert result["states"][2]["peers"] == 0
    assert len(result["states"][-1]["stopped"]) == 1 and result["states"][-1]["stopped"][0] >= 1
    assert not result["states"][-1]["busy"]


def test_live_voice_connect_blocks_dictation_in_another_session():
    result = run_voice("voice-first")
    assert result["states"][3]["media"] == 1
    assert result["states"][3]["busy"]


@pytest.mark.parametrize("scenario", ["dictation-pagehide-permission", "dictation-pagehide-recording"])
def test_pagehide_cancels_pending_or_active_dictation_without_transcription(scenario):
    result = run_voice(scenario)
    assert result["states"][-1]["media"] == 1
    assert result["states"][-1]["stopped"][0] >= 1
    assert not result["states"][-1]["busy"]
    assert not any(row["url"].endswith("/transcribe") for row in result["requests"])


def test_transcription_retains_microphone_reservation_until_draft_callback_finishes():
    result = run_voice("transcription-owner")
    assert result["states"][2]["media"] == 1
    assert result["states"][2]["stopped"][0] >= 1
    assert result["states"][2]["busy"]
    assert result["states"][2]["peers"] == 0
    assert result["states"][-1]["draft"] == "Unsent private draft Voice transcript"
    assert result["states"][-1]["button"] == "Send message"
    assert not result["states"][-1]["busy"]


@pytest.mark.parametrize("scenario", ["pagehide", "invalidate", "channel-close", "delayed-stop", "timeout-stop"])
def test_disconnect_paths_release_tracks_connections_and_timers(scenario):
    result = run_voice(scenario)
    final = result["states"][-1]
    assert len(final["stopped"]) == 1 and final["stopped"][0] >= 1
    assert final["peerClosed"] == final["socketClosed"] == final["channelClosed"] == [1]
    assert not final["busy"]
    assert final["timers"] == 0
    if scenario == "delayed-stop":
        assert result["states"][-2]["stopped"][0] >= 1, "hardware must stop before server acknowledgement"
        assert not result["states"][-2]["voice"]["open"], "closing must not wait for server acknowledgement"


@pytest.mark.parametrize("scenario", ["observer-close", "unbound-close"])
def test_closing_without_own_connection_binding_never_posts_name_only_stop(scenario):
    result = run_voice(scenario)
    assert not any(row["url"].endswith("/voice/stop") for row in result["requests"])
    assert not result["states"][-1]["voice"]["open"]


def test_owned_stop_includes_nonce_and_generation_and_cannot_close_reopened_panel():
    result = run_voice("delayed-stop-reopen")
    stops = [row for row in result["requests"] if row["url"].endswith("/voice/stop")]
    assert len(stops) == 1
    assert stops[0]["url"] == "/api/sessions/alpha/voice/stop"
    assert json.loads(stops[0]["options"]["body"]) == {"nonce": "our-connection", "generation": "our-generation", "root": "root-alpha"}
    assert result["states"][-1]["voice"]["open"]
    assert result["states"][-1]["voice"]["session"] == "beta"
    assert result["states"][-1]["media"] == 1
    assert not result["states"][-1]["busy"]


def test_changed_socket_binding_fails_closed_and_releases_media():
    result = run_voice("changed-binding")
    assert not result["states"][-1]["voice"]["connected"]
    assert not result["states"][-1]["busy"]
    assert result["states"][-1]["stopped"][0] >= 1
    assert not any(row["url"].endswith("/voice/stop") for row in result["requests"])


@pytest.mark.parametrize("scenario", ["denied", "offer-failure"])
def test_start_failure_releases_microphone_ownership_for_retry(scenario):
    final = run_voice(scenario)["states"][-1]
    assert not final["voice"]["starting"]
    assert not final["busy"]
    if scenario == "offer-failure":
        assert len(final["stopped"]) == 1 and final["stopped"][0] >= 1


def test_typed_content_changes_only_mic_to_send_in_chat_and_terminal():
    result = run_voice("composer-actions")
    assert result["dispatch"] == [["chat", "alpha"], ["raw", "alpha"], ["chat", "alpha"]]
    assert [row["button"] for row in result["states"][2:]] == [
        "Record voice message", "Record voice message", "Send message",
        "Record voice message", "Record voice message", "Send message",
        "Record voice message", "Record voice message", "Send message", "Send message",
    ]
    assert all(row["waveform"] == "Start Voice Mode" for row in result["states"])
    assert all(row["media"] == 0 for row in result["states"])


def test_control_enter_and_mobile_pointer_hold_release_audio_without_resuming_after_hidden():
    result = run_voice("keyboard")
    assert [row["enabled"] for row in result["states"][5:]] == [
        [False], [True], [False], [True], [False], [True], [False], [True],
        [False], [False], [False],
    ]
    assert not result["states"][-1]["busy"]


@pytest.mark.parametrize("scenario", ["nested-greeting-error", "nested-updates-error", "nested-approval-error"])
def test_nested_non_audio_error_identity_does_not_mute_live_microphone(scenario):
    result = run_voice(scenario)
    after_error = result["states"][-2]
    assert after_error["voice"]["connected"]
    assert after_error["enabled"] == [True]
    assert "audio control" not in after_error["status"]
    if scenario == "nested-greeting-error":
        assert after_error["status"] == "The opening greeting could not start. You can still talk."


def test_nested_audio_error_identity_still_mutes_until_explicit_retry():
    after_error = run_voice("nested-audio-error")["states"][-2]
    assert after_error["voice"]["connected"]
    assert after_error["enabled"] == [False]
    assert "audio control" in after_error["status"]


@pytest.mark.parametrize("option", ["supervision", "checkin"])
def test_advanced_voice_requires_confirmation_and_exact_root_before_acknowledged_enable(option):
    result = run_voice("advanced-" + option)
    assert all(not control["checked"] and control["disabled"] for control in result["states"][1]["advanced"])
    assert all(not control["checked"] and control["disabled"] for control in result["states"][3]["advanced"])
    assert len(result["confirmations"]) == 1
    advanced = [frame for frame in result["sent"] if frame["type"] == "advanced"]
    assert advanced == [{"type": "advanced", "option": option, "enabled": True, "consent": True,
                         "restart_acknowledged": option == "supervision", "nonce": "our-connection",
                         "generation": "our-generation", "root": "root-alpha"}]
    assert all(not control["checked"] and control["disabled"] for control in result["states"][5]["advanced"])
    enabled = {control["option"] for control in result["states"][6]["advanced"] if control["checked"]}
    assert enabled == {option}
    assert all(not control["checked"] and control["disabled"] for control in result["states"][-1]["advanced"])
    assert result["states"][-1]["draft"] == "Unsent private draft"
    assert result["states"][-1]["stopped"][0] >= 1


@pytest.mark.parametrize("scenario", ["advanced-decline", "advanced-rootless"])
def test_advanced_voice_never_sends_without_consent_and_bound_root(scenario):
    result = run_voice(scenario)
    assert not any(frame["type"] == "advanced" for frame in result["sent"])
    assert all(not control["checked"] for control in result["states"][5]["advanced"])
    assert len(result["confirmations"]) == (1 if scenario == "advanced-decline" else 0)


def test_existing_tool_hold_can_be_released_without_enabling_restart():
    result = run_voice("advanced-release")
    assert result["states"][4]["releaseHidden"] is False
    advanced = next(frame for frame in result["sent"] if frame["type"] == "advanced")
    assert advanced["option"] == "supervision" and advanced["enabled"] is False
    assert advanced["restart_acknowledged"] is False
    assert advanced["root"] == "root-alpha"
    assert result["states"][6]["releaseHidden"] is True


def test_unconfirmed_advanced_result_does_not_enable_controls_or_leak_audio_after_reopen():
    result = run_voice("advanced-timeout")
    assert "unconfirmed" in result["states"][6]["status"]
    assert all(not control["checked"] for control in result["states"][6]["advanced"])
    assert all(not control["checked"] and control["disabled"] for control in result["states"][-1]["advanced"])
    assert result["states"][-1]["stopped"][0] >= 1
    assert result["states"][-1]["draft"] == "Unsent private draft"


@pytest.mark.parametrize("view", ["chat", "raw"])
def test_both_reading_modes_have_distinct_accessible_mic_and_waveform_buttons(view):
    result = subprocess.run([NODE, "-e", MARKUP_DRIVER, str(ROOT / "app.py"), view], capture_output=True,
                            text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    tree = MarkupElements()
    tree.feed(result.stdout)
    for node_id, label in [("cmd-send-chat-alpha", "Record voice message"), ("voice-mode-open-alpha", "Start Voice Mode")]:
        matches = [row for row in tree.nodes if row[1].get("id") == node_id]
        assert len(matches) == 1
        assert matches[0][0] == "button"
        assert matches[0][1]["aria-label"] == label
        assert "session-composer-alpha" in matches[0][2]
    wave = next(row for row in tree.nodes if row[1].get("id") == "voice-mode-open-alpha")
    assert wave[1]["type"] == "button"
    assert wave[1]["aria-haspopup"] == "dialog"
    assert "openVoiceMode('alpha')" in wave[1]["onclick"]


def test_waveform_is_touch_sized_without_overriding_global_composer_layout():
    source = (ROOT / "app.py").read_text()
    rule = re.search(r"\.composer-voice\{([^}]+)\}", source)
    assert rule
    assert any(f"{prop}:48px" in rule.group(1) or f"{prop}:44px" in rule.group(1) for prop in ["width", "min-width"])
    assert any(f"{prop}:48px" in rule.group(1) or f"{prop}:44px" in rule.group(1) for prop in ["height", "min-height"])
    assert ".cmd-bar{gap:5px}" not in (ROOT / "voice-mode.js").read_text()
