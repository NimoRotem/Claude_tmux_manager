/* GPT-Live audio stays in WebRTC. Only authenticated application controls use our socket. */
(() => {
  'use strict';
  const base = typeof BASE === 'string' ? BASE : '';
  const css = document.createElement('style');
  css.textContent = `
.voice-panel{position:fixed;right:22px;bottom:22px;width:min(410px,calc(100vw - 28px));height:min(560px,calc(100dvh - 28px));box-sizing:border-box;display:flex;flex-direction:column;overflow:hidden;z-index:15000;padding:20px;border:1px solid #596484;border-radius:16px;background:#161c29;color:#e6edf3;box-shadow:0 14px 60px #0009;font:14px/1.5 system-ui,sans-serif}
.voice-content{flex:1;min-height:0;overflow:auto;overscroll-behavior:contain;scrollbar-width:thin;scrollbar-color:#58627a transparent}.voice-footer{flex-shrink:0}.voice-panel h2{font-size:18px;margin:0 0 3px}.voice-panel p{margin:8px 0}.voice-subtle{font-size:12px;color:#adb7ca}.voice-row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:14px}
.voice-panel button,.voice-panel select{font:inherit;border:1px solid #4a5570;background:#27324a;color:#e6edf3;border-radius:8px;padding:9px 12px;min-height:42px;cursor:pointer}.voice-panel button:disabled{opacity:.5;cursor:default}.voice-panel button:focus-visible{outline:2px solid #a6bcff;outline-offset:3px}
.voice-panel .voice-primary{background:#4657bc;border-color:#7182ee}.voice-panel .voice-end{margin-left:auto;background:#3f2630}.voice-dot{display:inline-block;width:8px;height:8px;border-radius:50%;background:#8b949e;margin-right:7px}.voice-dot.live{background:#67dea3;box-shadow:0 0 0 5px #67dea319}
.voice-caption{padding:12px;border-radius:9px;background:#101521;flex-shrink:0}.voice-caption-text{height:63px;line-height:21px;overflow:hidden;white-space:pre-wrap;overflow-wrap:anywhere;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical}.voice-panel .voice-mic{width:42px;height:42px;padding:9px;display:inline-flex;align-items:center;justify-content:center;color:#adb7ca;background:#27324a;border-color:#4a5570;border-radius:50%;flex-shrink:0}.voice-mic svg{width:22px;height:22px;pointer-events:none}.voice-mic[data-hot=true]{color:#161c29;background:#e6edf3;border-color:#e6edf3}.voice-mic .voice-mic-slash{display:none}.voice-mic[data-hot=false] .voice-mic-slash{display:block}.voice-panel [hidden]{display:none!important}.voice-mic-state{min-height:36px;line-height:18px;overflow-wrap:anywhere}.voice-guard{padding:9px 12px;background:#303043;border-left:3px solid #c0a0ff;border-radius:4px;font-size:12px}.voice-hold{touch-action:none;user-select:none}.voice-hold[aria-pressed=true]{background:#175943}
@media(max-width:600px){.voice-panel{position:fixed;right:12px;bottom:12px;width:calc(100% - 24px);height:min(500px,calc(100dvh - 24px));padding:12px}.voice-mode-row{flex-wrap:nowrap}.voice-mode-row select{flex:1;min-width:0}.voice-mode-row label{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}}
`;
  css.textContent += `.voice-panel .voice-speaker{width:42px;height:42px;padding:9px;display:inline-flex;align-items:center;justify-content:center;flex-shrink:0;color:#dce4ff;background:#283352}.voice-speaker svg{width:22px;height:22px;pointer-events:none}.voice-speaker[aria-pressed=false]{background:transparent;color:#adb7ca;border-style:dashed}.voice-speaker .voice-speaker-slash{display:none}.voice-speaker[aria-pressed=false] .voice-speaker-slash{display:block}.voice-speaker[aria-pressed=false] .voice-speaker-waves{display:none}.voice-speaker .voice-speaker-stack{display:none}.voice-speaker[data-scope=all] .voice-speaker-stack{display:block}.voice-speaker[data-scope=all] .voice-speaker-single,.voice-speaker[data-scope=all] .voice-speaker-waves{display:none}`;
  css.textContent += `.voice-mode-row{flex-wrap:nowrap}.voice-mode-row select{flex:1;min-width:0}.voice-mode-row label{position:absolute;width:1px;height:1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap}`;
  document.head.append(css);
  let panel, name = '', pc, stream, audio, events, socket, timer, startupTimer;
  let micLease = null, returnFocus = null, binding = null;
  const mic = () => window.dashboardMicrophone;
  let connected = false, starting = false, ending = false, mode = 'continuous', muted = false;
  let updateScope = 'current', preferencePending = false;
  let supervision = false, checkin = false, advancedPending = false, advancedTimer, held = false;
  const updateScopes = ['off', 'current', 'all'];
  const updateLabels = {off:'Off', current:'On', all:'All Sessions'};
  let playbackReady = false, greetingId = '', greetingTimer;
  let epoch = 0, lastSpeaker = '', caption = '', off = false, capturing = false;
  const controlKeys = new Set();
  const heldInputs = new Set();
  const node = id => document.getElementById('voice-' + id);
  const send = data => { if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify(data)); };
  let activityPulseTimer, lastActivityPulse = 0;
  function userActivity() {
    if(!connected || ending) return;
    const pulse = () => {lastActivityPulse=Date.now();send({type:'user_activity'});};
    if(Date.now()-lastActivityPulse >= 1000) pulse();
    clearTimeout(activityPulseTimer); activityPulseTimer=setTimeout(pulse,250);
  }
  const diagnostic = (leg, reason) => send({type:'diagnostic',leg,reason});
  const status = text => { if(node('status')) {node('status').textContent = text;node('status').title = text;} };
  const guard = text => { if(node('guard')) node('guard').textContent = text; };
  function paint() {
    if(typeof window._syncVoiceModeButtons === 'function') window._syncVoiceModeButtons();
    if(!panel) return;
    node('dot').classList.toggle('live', connected);
    node('start').hidden = connected;
    node('start').disabled = connected || starting || ending;
    node('start').textContent = starting ? 'Connecting…' : 'Connect';
    node('end').disabled = ending || (!starting && !connected && !binding);
    node('mute').disabled = !connected;
    const micLabel = !connected ? 'Microphone off' : muted ? 'Unmute microphone' : 'Mute microphone';
    node('mute').setAttribute('aria-label', micLabel);
    node('mute').setAttribute('aria-pressed', String(muted));
    node('mute').title = micLabel + (connected && mode === 'continuous' ? ' (Control toggles)' : '');
    node('mute').dataset.hot = String(capturing);
    node('speaker').disabled = starting || ending || preferencePending;
    node('speaker').setAttribute('aria-busy', String(preferencePending));
    node('speaker').dataset.scope = updateScope;
    node('speaker').setAttribute('aria-pressed', updateScope === 'all' ? 'mixed' : String(updateScope === 'current'));
    const nextScope = updateScopes[(updateScopes.indexOf(updateScope)+1)%updateScopes.length];
    node('speaker').setAttribute('aria-label', 'Work updates: '+updateLabels[updateScope]+'. Switch to '+updateLabels[nextScope]);
    node('speaker').title = {off:'Off: no proactive updates. Replies stay on.', current:'On: direction and approval requests from the attached session.', all:'All Sessions: direction and approval requests from your sessions. Voice stays attached.'}[updateScope];
    node('mic-state').textContent = !connected ? 'Microphone off.' : muted ? 'Microphone muted. Unmute to talk.' :
      capturing ? (mode === 'push' ? 'Talking. Release to mute.' : 'Listening. Press Control to mute.') : 'Hold Control (⌃ on Mac) or the button to talk.';
    node('hold').hidden = mode !== 'push';
    node('hold').disabled = !connected || muted;
    node('hold').setAttribute('aria-pressed', String(capturing && mode === 'push'));
    node('pause').disabled = !connected;
    node('mode').disabled = starting;
    for(const option of ['supervision','checkin']) {
      const control=node(option);
      if(control){control.checked=option==='supervision'?supervision:checkin;
        control.disabled=!connected||!binding?.root||advancedPending||ending;}
    }
    if(node('release-hold'))node('release-hold').hidden=!connected||!held;
  }
  function advanced(option, enabled) {
    if(!connected||!binding?.root||advancedPending||ending)return;
    const disclosure=option==='supervision'?(enabled?
      'Enable managed pause controls? This may interrupt and restart the coding worker in this exact conversation. It suspends automatic dashboard prompts. Explicit pauses survive End and disconnect. Reconnect and use Release existing tool hold to release them. Hooks cover supported local tools, not every possible action.':
      'Disable managed controls and release any explicit tool hold for this conversation? This does not send new work or override other approvals.'):
      (enabled?'Enable 3-minute pre-test check-ins for this connection? A scoped instruction will ask for feature tweaks before testing. After that exact question and 3 minutes without input, already-requested testing and the normal workflow may continue only under existing rules and approvals. This is not deployment consent.':
      'Disable the pre-test inactivity timer? Silence will no longer trigger a continuation.');
    paint();
    if(!window.confirm(disclosure))return;
    advancedPending=true;paint();
    send({type:'advanced',option,enabled,consent:true,restart_acknowledged:option==='supervision'&&enabled,
      nonce:binding.nonce,generation:binding.generation,root:binding.root});
    const run=epoch;
    clearTimeout(advancedTimer);advancedTimer=setTimeout(()=>{if(run===epoch){advancedPending=false;paint();
      status('Advanced control result is unconfirmed. Reconnect before trying again; existing explicit holds remain.');}},90000);
  }
  function capture(enabled) {
    capturing = Boolean(enabled && connected && !ending && !muted && !document.hidden);
    enabled = capturing;
    stream?.getAudioTracks().forEach(track => { track.enabled = enabled; });
    if(events?.readyState === 'open') events.send(JSON.stringify({
      type: enabled ? 'session.input_audio.unmute' : 'session.input_audio.mute',
      event_id: 'mic_' + Math.random().toString(36).slice(2)
    }));
    paint();
  }
  function holdInput(input) {
    if(!connected || ending || muted || mode !== 'push' || document.hidden || heldInputs.has(input)) return;
    heldInputs.add(input);
    if(heldInputs.size === 1) capture(true);
  }
  function releaseInput(input) {
    if(heldInputs.delete(input) && heldInputs.size === 0 && mode === 'push') capture(false);
  }
  function release() {
    heldInputs.clear();
    if(mode === 'push') capture(false);
  }
  function setMode() {
    heldInputs.clear();
    const nextMode = node('mode').value;
    // Selecting push-to-talk explicitly arms a new hold, even after continuous mute.
    // No audio starts until a fresh hold; keep mute when switching to continuous.
    if(nextMode === 'push' && mode !== nextMode) muted = false;
    mode = nextMode;
    capture(connected && mode === 'continuous' && !muted);
    paint();

  }
  function greetWhenReady() {
    if(!connected || !playbackReady || greetingId || events?.readyState !== 'open') return;
    greetingId = 'greeting_' + Math.random().toString(36).slice(2);
    // Session-scoped startup instruction, never a coding request or a work update.
    events.send(JSON.stringify({type:'session.instructions.append', event_id:greetingId, delegation_id:null,
      content:'The user has just connected voice mode and audio playback is ready. Greet them now in English with exactly: "Hi, how can I help?" Say it once, without waiting for them to speak, then pause and listen. This requested opening greeting also applies when work updates are off. Do not call tools, resume work, or add a status report for this greeting. Keep all existing session instructions.'}));
    greetingTimer = setTimeout(() => status('The opening greeting could not be confirmed. You can still talk.'), 10000);
  }
  async function enableSound(run) {
    try {
      await audio.play();
      if(run !== epoch) return;
      playbackReady=true; node('sound').hidden=true; greetWhenReady();
    } catch {
      if(run !== epoch) return;
      playbackReady=false; status('Tap Enable sound to hear the assistant.'); node('sound').hidden=false;
    }
  }
  function cleanup() {
    clearTimeout(advancedTimer);advancedPending=false;supervision=false;checkin=false;
    clearTimeout(activityPulseTimer);
    playbackReady=false; greetingId=''; clearTimeout(greetingTimer);
    if(node('sound')) node('sound').hidden=true;
    preferencePending=false;
    heldInputs.clear();controlKeys.clear();capturing=false;
    clearInterval(timer); clearTimeout(startupTimer);
    if(micLease)mic()?.release(micLease);else stream?.getTracks().forEach(t => t.stop());
    stream = null; micLease = null; binding = null;
    if(events) { events.onclose = null; events.close(); events = null; }
    if(pc) { pc.onconnectionstatechange = null; pc.close(); pc = null; }
    if(audio) { audio.pause(); audio.srcObject = null; audio.remove(); audio = null; }
    if(socket) { socket.onclose = null; socket.close(); socket = null; }
    connected = false; starting = false; caption = ''; lastSpeaker = '';

    paint();
  }
  function failed(text) {
    ++epoch; cleanup(); status(text);
    guard('Voice is disconnected. Existing explicit coding holds remain; reconnect to release them deliberately.');
  }
  async function start() {
    if(starting || connected || ending) return;
    if(!navigator.mediaDevices?.getUserMedia || !window.RTCPeerConnection) {
      status('This browser needs microphone access and WebRTC support.'); return;
    }
    const lease = mic()?.reserve('voice', name);
    if(!lease) {status('Finish the current recording or transcription before connecting Voice Mode.');return;}
    micLease = lease;
    starting = true; ending = false; off = false; paint();
    binding = null;
    const run = ++epoch;
    status('Allow microphone access to begin.');
    try {
      const acquired = await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,autoGainControl:true}});
      if(run !== epoch) { acquired.getTracks().forEach(t => t.stop()); return; }
      if(!mic().accept(lease, acquired)) {if(run===epoch)failed('This session changed. Reopen Voice Mode to connect.');return;}
      stream = acquired; stream.getAudioTracks().forEach(t => {t.enabled=false;});
      const peer = new RTCPeerConnection(); pc = peer;
      audio = document.createElement('audio'); audio.autoplay = true; audio.setAttribute('playsinline','');
      panel.append(audio);
      pc.ontrack = event => {
        if(run !== epoch) return;
        audio.srcObject = event.streams[0]; enableSound(run);
      };
      stream.getTracks().forEach(t => pc.addTrack(t, stream));
      events = pc.createDataChannel('oai-events');
      events.onmessage = event => {
        if(run !== epoch) return;
        let data; try { data=JSON.parse(event.data); } catch {return;}
        if(data.type === 'session.started') {
          if(connected) return;
          clearTimeout(startupTimer); connected=true; starting=false;
          capture(mode === 'continuous' && !muted); paint(); send({type:'ready'});
          status(node('sound').hidden ? 'Ready. Tell me what you want to work on.' : 'Tap Enable sound to hear the assistant.');
          greetWhenReady();
        } else if(greetingId && (data.client_event_id || data.error?.client_event_id || data.error?.event_id) === greetingId && ['session.instructions.appended', 'error'].includes(data.type)) {
          clearTimeout(greetingTimer);
          if(data.type === 'error') status('The opening greeting could not start. You can still talk.');
        } else if(data.type === 'session.input_transcript.delta') {
          // User speech is handled by the voice service; never put dictation in this panel.
          lastSpeaker = 'You';
        } else if(data.type === 'session.output_transcript.delta') {
          if(lastSpeaker !== 'Assistant') caption = '';
          lastSpeaker = 'Assistant';
          caption = (caption + (data.delta || '')).slice(-360);
          status(caption);
        } else if(data.type === 'error' && !['updates_', 'approval_', 'greeting_'].some(prefix => (data.client_event_id || data.error?.client_event_id || data.error?.event_id || '').startsWith(prefix))) {
          // A rejected audio command must not leave the UI claiming a hot mic.
          heldInputs.clear();muted=true;capturing=false;
          stream?.getAudioTracks().forEach(track=>{track.enabled=false;});paint();
          status('Voice could not apply the audio control. Unmute to retry, or reconnect.');
        } else if(data.type === 'session.closed' && !ending) {
          failed('The voice conversation ended. Reconnect or end voice mode.');
        }
      };
      events.onclose = () => { if(!ending && run === epoch) {diagnostic('data_channel','closed');failed('The voice connection was lost.');} };
      pc.onconnectionstatechange = () => {
        if(pc && run === epoch && ['connected','disconnected','failed'].includes(pc.connectionState)) diagnostic('webrtc',pc.connectionState);
        if(pc && pc.connectionState === 'failed' && !ending && run === epoch) failed('Microphone connection failed.');
      };
      const offer=await peer.createOffer();
      if(run !== epoch || !mic().valid(lease)) return;
      await peer.setLocalDescription(offer);
      if(run !== epoch || !mic().valid(lease)) return;
      const url=new URL(base+'/api/sessions/'+encodeURIComponent(name)+'/voice',location.href);
      url.protocol=location.protocol==='https:'?'wss:':'ws:';
      const ws = new WebSocket(url.href); socket=ws;
      socket.onopen=()=> {if(run!==epoch||!mic().valid(lease)){ws.close();return;}send({type:'start',sdp:offer.sdp,proactive_updates:updateScope!=='off',update_scope:updateScope}); timer=setInterval(()=>send({type:'heartbeat'}),10000);};
      socket.onmessage=async event=> {
        if(run !== epoch) return;
        let data; try {data=JSON.parse(event.data);} catch {return;}
        if(data.type==='binding' || data.type==='answer') {
          if(typeof data.nonce==='string'&&data.nonce&&typeof data.generation==='string'&&data.generation){
            if(binding&&(binding.nonce!==data.nonce||binding.generation!==data.generation)){failed('Voice connection identity changed. Reconnect to continue.');return;}
            if(binding?.root&&data.root&&binding.root!==data.root){failed('The coding conversation changed. Reconnect voice.');return;}
            binding={nonce:data.nonce,generation:data.generation,root:data.root||binding?.root||''};
            if(typeof data.held==='boolean')held=data.held;
          }else if(data.type==='binding'){failed('Voice service returned an invalid connection identity.');return;}
          if(data.type==='binding'){paint();return;}
          try {await peer.setRemoteDescription({type:'answer',sdp:data.sdp});}
          catch {if(run===epoch)failed('Could not establish the microphone connection.');}
        } else if(data.type==='preferences') {
          if(updateScopes.includes(data.update_scope)) updateScope=data.update_scope;
          else if(typeof data.proactive_updates === 'boolean') updateScope=data.proactive_updates?'current':'off';
          preferencePending=false;paint();
          if(data.error)status(data.error);
        } else if(data.type==='advanced') {
          clearTimeout(advancedTimer);advancedPending=false;supervision=data.supervision===true;
          checkin=data.checkin===true;held=data.held===true;paint();
          guard(data.error||('Managed pause controls: '+(supervision?'on':'off')+'. Pre-test timer: '+(checkin?'on':'off')+'.'+(held?' An explicit tool hold remains.':'')));
        } else if(data.type==='error') failed(data.message);
        else if(data.type==='feature_checkin') {guard(data.message);}
        else if(data.type==='target_confirmation') {guard(data.message);status('Confirmation for '+data.spoken_name+'. Voice stays attached to the current session.');}
        else if(data.type==='target_action') {guard(data.message);status(data.message);}
        else if(data.type==='ready') guard('Talk through ideas. Coding actions happen only when you ask for them.');
        else if(data.message) status(data.message);
      };
      socket.onclose=()=> {if(!ending && run===epoch) failed('Voice disconnected. Reconnect to continue.');};
      socket.onerror=()=> {if(run===epoch) status('Could not reach the voice service.');};
      startupTimer=setTimeout(()=>{if(run===epoch&&!connected)failed('Voice connection timed out. Please retry.');},90000);
    } catch(error) {
      if(run !== epoch) return;
      cleanup(); status(error.name==='NotAllowedError'?'Microphone access was denied. Enable it in browser settings and retry.':'Could not start voice: '+error.message);
    }
  }
  async function stop(closePanel=false) {
    if(ending)return;
    ending=true; const run=++epoch, session=name, ownedBinding=binding; capture(false);
    send({type:'stop'});
    if(events?.readyState==='open') events.send(JSON.stringify({type:'session.close'}));
    // Stop capture and playback immediately, even if the server is unreachable.
    cleanup();
    off=true;status('Voice mode is off.');guard('Your coding session is unchanged.');
    if(closePanel)removePanel();
    // Before a binding arrives, closing our socket is the only scoped action.
    // Never send a name-only stop that could affect another tab or incarnation.
    if(!ownedBinding){ending=false;paint();return;}
    const controller=new AbortController();
    let deadline;
    try {
      const response=await Promise.race([
        fetch(base+'/api/sessions/'+encodeURIComponent(session)+'/voice/stop',{
          method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(ownedBinding),signal:controller.signal,
        }),
        new Promise((resolve,reject)=>{deadline=setTimeout(()=>{controller.abort();reject(new Error('Server confirmation timed out.'));},8000);}),
      ]);
      if(run!==epoch)return;
      if(!response.ok) throw new Error('Could not confirm the voice connection stopped on the server.');
      off=true; cleanup(); status('Voice mode is off.'); guard('Normal session controls apply.');
    } catch(error) {if(run!==epoch)return;cleanup();status(error.message);guard('Microphone is off. Server confirmation is unavailable.');}
    finally{clearTimeout(deadline);}
    if(run===epoch){ending=false;paint();}
  }
  function removePanel() {
    if(panel)panel.remove(); panel=null;name='';
    if(returnFocus?.isConnected)returnFocus.focus({preventScroll:true});
    returnFocus=null;
    if(typeof window._syncVoiceModeButtons === 'function')window._syncVoiceModeButtons();
  }
  function invalidate(session) {
    if(session!==name)return;
    ending=true;++epoch;cleanup();removePanel();ending=false;
  }
  async function open(session) {
    if(panel) {panel.hidden=false; panel.focus(); if(session!==name) status('Voice is attached to '+name+'. You can ask about your other sessions without switching voice.'); return;}
    ++epoch;name=session;off=false;ending=false;binding=null;held=false;supervision=false;checkin=false;
    returnFocus=document.activeElement;
    panel=document.createElement('section'); panel.className='voice-panel'; panel.tabIndex=-1;
    panel.setAttribute('role','dialog');panel.setAttribute('aria-label','Voice Mode');panel.id='voice-mode-panel';
    panel.innerHTML=`<h2><span class="voice-dot" id="voice-dot"></span>Voice mode</h2>
      <div class="voice-subtle" id="voice-session"></div>
      <div class="voice-caption"><div class="voice-caption-text" id="voice-status" role="status">Talk through ideas and steer your coding session.</div></div>
      <div class="voice-content">
      <p class="voice-subtle">Voice stays attached here. Use the speaker button for Off, On, or All Sessions updates.</p>
      <div class="voice-row voice-mode-row"><label for="voice-mode">Microphone</label><select id="voice-mode"><option value="continuous">Continuous listening</option><option value="push">Push to talk</option></select><button id="voice-mute" class="voice-mic" type="button" aria-label="Mute microphone" aria-pressed="false" disabled><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/><path class="voice-mic-slash" d="M3 3l18 18"/></svg></button></div>
      <div class="voice-row"><button id="voice-start" class="voice-primary">Connect</button><button id="voice-hold" class="voice-hold" hidden aria-pressed="false" title="Hold the Control key (⌃ on Mac), or hold this button, to talk">Hold Control to talk</button><button id="voice-sound" hidden>Enable sound</button></div>
      <p class="voice-subtle voice-mic-state" id="voice-mic-state" role="status"></p>
      <div class="voice-guard" id="voice-guard">Talk through ideas. Coding actions happen only when you ask for them.</div>
      <details class="voice-subtle"><summary>Advanced controls · off by default</summary>
      <p><label><input type="checkbox" id="voice-supervision" disabled> Managed pause controls</label><br>May restart this coding worker. End and disconnect keep explicit tool holds. Reconnect, then use Release existing tool hold. Disabling also releases it. Hooks cover supported local tools, not all actions.</p>
      <p><label><input type="checkbox" id="voice-checkin" disabled> 3-minute pre-test check-in</label><br>Only after the exact feature-tweak question. Input resets the timer; holds cancel it. Existing rules and approvals still apply. No new deployment permission.</p>
      <button id="voice-release-hold" hidden type="button">Release existing tool hold</button></details>
      </div><div class="voice-row voice-footer"><button id="voice-pause" aria-label="Interrupt coding" disabled><span aria-hidden="true">⏸</span> Interrupt coding</button><button id="voice-end" class="voice-end">End voice</button><button id="voice-close" aria-label="Close Voice Mode">Close</button></div>`;
    const main = document.getElementById('main');
    if(main) main.before(panel); else document.body.append(panel);
    const speaker=document.createElement('button');
    speaker.id='voice-speaker';speaker.className='voice-speaker';speaker.type='button';
    speaker.innerHTML='<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false"><path class="voice-speaker-single" d="M11 4 6 8H3v8h3l5 4z"/><g class="voice-speaker-stack"><path d="M9 9 5 12H2v6h3l4 3zM17 3l-4 3h-3v3m7-6v12l-5-4m8-4a6 6 0 0 1 0 8"/></g><path class="voice-speaker-waves" d="M15 8a6 6 0 0 1 0 8m3-11a10 10 0 0 1 0 14"/><path class="voice-speaker-slash" d="m16 9 6 6m0-6-6 6"/></svg>';
    node('mute').after(speaker);
    node('session').textContent='Attached to '+session;
    node('mode').value=mode;
    node('mode').onchange=setMode;
    node('start').onclick=start;
    node('mute').onclick=toggleMute;
    node('speaker').onclick=()=>{
      if(starting || ending || preferencePending) return;
      if(connected && socket?.readyState !== WebSocket.OPEN) return;
      updateScope=updateScopes[(updateScopes.indexOf(updateScope)+1)%updateScopes.length];
      if(connected){preferencePending=true;send({type:'proactive_updates',enabled:updateScope!=='off',update_scope:updateScope});}
      paint();
    };
    node('sound').onclick=()=>{if(audio)enableSound(epoch);};
    node('pause').onclick=()=>send({type:'pause'});
    node('supervision').onchange=event=>advanced('supervision',event.target.checked);
    node('checkin').onchange=event=>advanced('checkin',event.target.checked);
    node('release-hold').onclick=()=>advanced('supervision',false);
    node('end').onclick=()=>stop(); node('close').onclick=()=>stop(true);
    const hold=node('hold');
    hold.onpointerdown=event=>{
      if(!connected||ending||muted||mode!=='push')return;
      if(event.button !== 0)return;
      event.preventDefault();
      holdInput('pointer:'+event.pointerId);
      // A capture failure must not swallow the press. Window release is the fallback.
      try {hold.setPointerCapture(event.pointerId);} catch {}
    };
    const releasePointer=event=>releaseInput('pointer:'+event.pointerId);
    hold.onpointerup=releasePointer;hold.onpointercancel=releasePointer;hold.onlostpointercapture=releasePointer;
    hold.onkeydown=event=>{if([' ','Enter'].includes(event.key)&&!event.repeat){event.preventDefault();holdInput('button:'+event.key);}};
    hold.onkeyup=event=>{if([' ','Enter'].includes(event.key)){event.preventDefault();releaseInput('button:'+event.key);}};
    hold.onblur=()=>{releaseInput('button: ');releaseInput('button:Enter');};
    paint();panel.focus();
    const openedPanel=panel,openedSession=session;
    try {
      const response=await fetch(base+'/api/sessions/'+encodeURIComponent(session)+'/voice/status');
      const data=await response.json();
      if(panel!==openedPanel||name!==openedSession)return;
      if(data.error)status(data.error);
      else if(data.active)guard('Voice is already active. This panel can only stop its own connection.');
    }catch{if(panel===openedPanel&&name===openedSession)status('Could not check session status.');}
  }
  function toggleMute() {
    if(!connected || ending) return;
    heldInputs.clear();muted=!muted;
    capture(mode === 'continuous' && !muted);
  }
  function leavePage() {
    controlKeys.clear();release();
    if((connected || starting) && mode === 'continuous') {muted=true;capture(false);}
  }
  // Capture phase keeps Control working while the terminal or composer has focus.
  // Use the actual Control key on every platform, including macOS.
  window.addEventListener('pointerdown',userActivity,true);
  window.addEventListener('keydown',userActivity,true);
  window.addEventListener('keydown',event=>{
    if(event.key==='Escape'&&panel){event.preventDefault();stop(true);return;}
    if(event.key !== 'Control' || event.repeat) return;
    const key = event.code || 'Control';
    if(controlKeys.has(key)) return;
    controlKeys.add(key);
    if(!connected || ending || document.hidden) return;
    if(mode === 'push') holdInput(key);
    else toggleMute();
  },true);
  window.addEventListener('keyup',event=>{
    if(event.key === 'Control') {
      const key = event.code || 'Control';controlKeys.delete(key);releaseInput(key);
    }
    if([' ','Enter'].includes(event.key)) releaseInput('button:'+event.key);
  },true);
  window.addEventListener('pointerup',event=>releaseInput('pointer:'+event.pointerId),true);
  window.addEventListener('pointercancel',event=>releaseInput('pointer:'+event.pointerId),true);
  window.addEventListener('blur',leavePage);
  document.addEventListener('visibilitychange',()=>{diagnostic('page',document.hidden?'hidden':'visible');if(document.hidden)leavePage();});
  window.addEventListener('pagehide',()=>{diagnostic('page','pagehide');ending=true;++epoch;capture(false);cleanup();removePanel();ending=false;});
  window.voiceMode={open,close:()=>stop(true),invalidate,state:()=>({open:!!panel,session:name,connected,starting})};
})();
