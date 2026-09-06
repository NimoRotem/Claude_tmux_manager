"""Exercise the shipped telemetry/cache state machine with a synthetic clock."""

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
const region=js.slice(js.indexOf('function _nowMs()'),js.indexOf('// ── Freeze'));
const storage=new Map();
let now=10000,beeps=[];
const base={name:'alpha',owner:'admin',logical_incarnation:'logical-a',metrics_thread_id:'thread-a',
  activity_status:'idle',last_turn_end:10000,last_turn_seconds:392,cache_last_activity:10000,
  cache_ttl:1800,cache_model:'gpt-6-astra',cache_read_tokens:0,last_input_tokens:12000,
  context_tokens:12500,context_limit:1000000,detect_sure:true,
  session_total_tokens:1234567,session_input_tokens:1234000,session_output_tokens:567,
  avg_tps:14.175,tps_completed_turns:2,tps_output_tokens:567,tps_active_seconds:40};
function makeElement(){
  const el={textContent:'',title:'',className:''};
  el.classList={toggle(cls,enabled){
    const classes=new Set(el.className.split(' ').filter(Boolean));
    enabled?classes.add(cls):classes.delete(cls);
    el.className=[...classes].join(' ');
  }};
  return el;
}
function setup(options={}){
  const elements={};
  for(const prefix of ['term-live-','status-','tl-verb-','tl-time-','tl-tok-','tl-since-','tl-total-','tl-ctx-','tl-tps-','tl-note-']){
    elements[prefix+'alpha']=makeElement();
  }
  let ticker;
  const rawState={alpha:{live:{sec:1,seen:true,done:true}}};
  const context=vm.createContext({
    Date:class extends Date{static now(){return now*1000}},
    Math,Number,String,JSON,Set,Promise,
    sessions:[{...base,...options.session}],selectedSession:'alpha',rawState,
    CACHE_WARN_LEAD:600,_currentUser:{id:'admin'},
    localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value)},
    document:{getElementById:id=>elements[id]||null},
    window:{performance:{now:()=>now*1000}},performance:{now:()=>now*1000},
    navigator:options.navigator||{},
    getRawState:name=>rawState[name],
    setInterval:callback=>{ticker=callback;return 1},
    setTimeout:()=>1,
    _completionAudioPrimed:options.primed!==false,
    _completionAudioCtx:{state:options.audioState||'running',currentTime:0,destination:{},
      createGain:()=>({gain:{setValueAtTime(){}},connect(){},disconnect(){}})},
    _glassPartial:(ctx,master,freq,start,duration,volume)=>beeps.push({freq,start,duration,volume}),
  });
  vm.runInContext(region,context);
  return {context,elements,tick:()=>ticker()};
}
const snapshot=elements=>Object.fromEntries(Object.entries(elements).map(([id,el])=>
  [id,{text:el.textContent,title:el.title,classes:el.className}]));
const scenario=process.argv[2];
(async()=>{
  if(scenario==='timeline'){
    const {context:c,elements,tick}=setup();
    const states=[];
    for(const offset of [0,1199,1200,1500,1800,1900]){
      now=10000+offset;
      c.updateLiveBar('alpha');
      tick();
      states.push({offset,state:c._cacheTelemetryState(c.sessions[0]),
        status:elements['tl-verb-alpha'].textContent,
        since:elements['tl-since-alpha'].textContent,
        pill:elements['status-alpha'].className,bar:elements['term-live-alpha'].className,
        beeps:beeps.length});
    }
    return {states,beeps};
  }
  if(scenario==='dedupe-and-new-work'){
    now=11200;
    const first=setup();
    first.context.updateLiveBar('alpha');
    first.context.updateLiveBar('alpha');
    const afterPolls=beeps.length;
    const reload=setup();
    reload.context.updateLiveBar('alpha');
    const afterReload=beeps.length;
    reload.context.sessions[0].activity_status='busy';
    reload.context.updateLiveBar('alpha');
    const busy=snapshot(reload.elements);
    reload.context.sessions[0].activity_status='idle';
    reload.context.updateLiveBar('alpha');
    const afterSameEpoch=beeps.length;
    Object.assign(reload.context.sessions[0],{cache_last_activity:11200,last_turn_end:11230});
    now=12400;
    reload.context.updateLiveBar('alpha');
    const afterNewEpoch=beeps.length;
    reload.context.sessions[0].metrics_thread_id='thread-b';
    reload.context.updateLiveBar('alpha');
    return {afterPolls,afterReload,busy,afterSameEpoch,afterNewEpoch,afterNewThread:beeps.length};
  }
  if(scenario==='audio-activation'){
    now=11200;
    const {context:c,elements}=setup({primed:false});
    c.updateLiveBar('alpha');
    const locked={beeps:beeps.length,records:storage.size,bar:elements['term-live-alpha'].className};
    c._completionAudioPrimed=true;
    c._completionAudioCtx.state='suspended';
    c.updateLiveBar('alpha');
    const suspended={beeps:beeps.length,records:storage.size};
    c._completionAudioCtx.state='running';
    c.updateLiveBar('alpha');
    c.updateLiveBar('alpha');
    return {locked,suspended,unlocked:{beeps:beeps.length,records:storage.size}};
  }
  if(scenario==='evidence-and-busy'){
    now=11200;
    const {context:c}=setup();
    const rows=[
      {label:'no-request',cache_last_activity:0},
      {label:'no-ttl',cache_ttl:0},
      {label:'short-prompt',cache_read_tokens:0,last_input_tokens:500,context_tokens:50000},
      {label:'unknown-input',cache_read_tokens:0,last_input_tokens:null},
      {label:'uncertain-identity',detect_sure:false},
      {label:'unknown-status',activity_status:'unknown'},
      {label:'busy',activity_status:'busy'},
      {label:'confirmed-cache-hit',cache_read_tokens:128,last_input_tokens:null},
      {label:'early-request-late-completion',cache_last_activity:9900,last_turn_end:11195},
      {label:'legacy-below-minimum',cache_model:'gpt-5.5',cache_min_tokens:2048,last_input_tokens:1500},
      {label:'legacy-without-minimum',cache_model:'gpt-5.5',last_input_tokens:1500},
      {label:'modern-without-minimum',cache_model:'gpt-6-astra',last_input_tokens:1500},
      {label:'backend-minimum-wins',cache_model:'gpt-6-astra',cache_min_tokens:2048,last_input_tokens:1500},
      {label:'unknown-model-without-minimum',cache_model:'custom-model',last_input_tokens:12000},
    ];
    return rows.map(row=>({label:row.label,state:c._cacheTelemetryState({...base,...row})}));
  }
  if(scenario==='metrics'){
    const {context:c,elements}=setup();
    c.updateLiveBar('alpha');
    const measured=snapshot(elements);
    Object.assign(c.sessions[0],{session_total_tokens:null,context_tokens:null,context_limit:null,avg_tps:null});
    c.updateLiveBar('alpha');
    const missing=snapshot(elements);
    Object.assign(c.sessions[0],{session_total_tokens:0,context_tokens:11000,context_limit:10000,avg_tps:0});
    c.updateLiveBar('alpha');
    const overflow=snapshot(elements);
    return {measured,missing,overflow};
  }
  if(scenario==='separate-duration-and-idle'){
    const {context:c,elements,tick}=setup();
    c.updateLiveBar('alpha');
    const initial=snapshot(elements);
    now=10250;
    tick();
    const afterTime=snapshot(elements);
    c.updateLiveBar('alpha');
    const afterPoll=snapshot(elements);
    now=13700;
    tick();
    const afterHour=snapshot(elements);
    c.sessions[0].last_turn_seconds=null;
    c.updateLiveBar('alpha');
    const unknown=snapshot(elements);
    c.sessions[0].activity_status='busy';
    Object.assign(c.rawState.alpha.live,{sec:8,done:false,at:now*1000,verb:'Working'});
    c.updateLiveBar('alpha');
    const busy=snapshot(elements);
    now+=10;
    tick();
    return {initial,afterTime,afterPoll,afterHour,unknown,busy,busyLater:snapshot(elements)};
  }
  if(scenario==='missing-completion'){
    const {context:c,elements}=setup({session:{last_turn_end:0,last_turn_seconds:null}});
    now=11200;
    c.updateLiveBar('alpha');
    const warning=snapshot(elements);
    now=11800;
    c.updateLiveBar('alpha');
    return {warning,cold:snapshot(elements),beeps:beeps.length};
  }
  if(scenario==='cross-tab-lock'){
    now=11200;
    let tail=Promise.resolve();
    const navigator={locks:{request:(key,callback)=>{tail=tail.then(callback);return tail}}};
    const a=setup({navigator}),b=setup({navigator});
    a.context.updateLiveBar('alpha');
    b.context.updateLiveBar('alpha');
    await tail;
    await Promise.resolve();
    const acrossTabs=beeps.length;
    a.context.sessions[0].cache_last_activity=11200;
    now=12400;
    a.context.updateLiveBar('alpha');
    a.context.sessions[0].activity_status='busy';
    await tail;
    return {acrossTabs,afterBusyWhileWaiting:beeps.length};
  }
  throw new Error('unknown scenario');
})().then(result=>process.stdout.write(JSON.stringify(result))).catch(error=>{console.error(error);process.exit(1)});
"""


def run_scenario(scenario):
    result = subprocess.run(
        [NODE, "-e", DRIVER, str(APP), scenario],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def test_idle_clock_warning_threshold_and_cold_transition():
    result = run_scenario("timeline")
    rows = result["states"]
    assert [row["state"]["phase"] for row in rows] == [
        "warm", "warm", "warning", "warning", "cold", "cold",
    ]
    assert [row["beeps"] for row in rows] == [0, 0, 2, 2, 2, 2]
    assert rows[1]["status"] == "Idle 19m 59s"
    assert rows[1]["since"] == ""
    assert "expiring" in rows[2]["pill"]
    assert "cache-expiring" in rows[2]["bar"]
    assert "cache is cold (estimated)" in rows[4]["since"]
    assert "expiring" not in rows[4]["pill"]
    assert "cache-expiring" not in rows[4]["bar"]
    assert result["beeps"][1]["start"] - result["beeps"][0]["start"] == pytest.approx(0.24)


def test_cache_chime_survives_polls_reload_and_resets_for_new_requests():
    result = run_scenario("dedupe-and-new-work")
    assert result["afterPolls"] == result["afterReload"] == result["afterSameEpoch"] == 2
    assert result["afterNewEpoch"] == 4
    assert result["afterNewThread"] == 6
    assert result["busy"]["tl-since-alpha"]["text"] == ""
    assert "cache-expiring" not in result["busy"]["term-live-alpha"]["classes"]


def test_cache_audio_waits_for_activation_without_consuming_alert():
    result = run_scenario("audio-activation")
    assert result["locked"]["beeps"] == result["locked"]["records"] == 0
    assert "cache-expiring" in result["locked"]["bar"]
    assert result["suspended"] == {"beeps": 0, "records": 0}
    assert result["unlocked"] == {"beeps": 2, "records": 1}


def test_no_cache_warning_without_cacheable_request_or_while_busy():
    rows = run_scenario("evidence-and-busy")
    assert [row["state"]["phase"] for row in rows[:7]] == ["unknown"] * 6 + ["busy"]
    assert rows[7]["state"]["phase"] == "warning"
    assert rows[8]["state"]["left"] == 500
    assert rows[8]["state"]["idle"] == 5
    assert [row["state"]["phase"] for row in rows[9:]] == [
        "unknown", "unknown", "warning", "unknown", "unknown",
    ]


def test_metrics_show_real_totals_context_window_and_honest_tps():
    result = run_scenario("metrics")
    measured = result["measured"]
    assert measured["tl-time-alpha"]["text"] == "last turn 6m 32s"
    assert measured["tl-total-alpha"]["text"] == "session 1.2M tokens"
    assert "1,234,567 cumulative input + output" in measured["tl-total-alpha"]["title"]
    assert measured["tl-ctx-alpha"]["text"] == "ctx 13k / 1.0M · 1%"
    assert measured["tl-tps-alpha"]["text"] == "avg 14.2 tps"
    assert "tool execution and waiting" in measured["tl-tps-alpha"]["title"]
    assert "does not establish rate limiting" in measured["tl-tps-alpha"]["title"]
    assert result["missing"]["tl-total-alpha"]["text"] == "session — tokens"
    assert result["missing"]["tl-ctx-alpha"]["text"] == "ctx —"
    assert result["missing"]["tl-tps-alpha"]["text"] == "avg — tps"
    assert result["overflow"]["tl-total-alpha"]["text"] == "session 0 tokens"
    assert result["overflow"]["tl-ctx-alpha"]["text"] == "ctx 11k / 10k · 110%"
    assert "crit" in result["overflow"]["tl-ctx-alpha"]["classes"]
    assert result["overflow"]["tl-tps-alpha"]["text"] == "avg 0.0 tps"


def test_tabs_share_alert_claim_and_busy_transition_cancels_queued_sound():
    result = run_scenario("cross-tab-lock")
    assert result == {"acrossTabs": 2, "afterBusyWhileWaiting": 2}


def test_known_cache_deadline_is_shown_without_a_completion_timestamp():
    result = run_scenario("missing-completion")
    assert result["warning"]["tl-verb-alpha"]["text"] == "Idle —"
    assert result["warning"]["tl-time-alpha"]["text"] == "last turn —"
    assert result["warning"]["tl-since-alpha"]["text"] == "cache may cool in 10m"
    assert result["cold"]["tl-since-alpha"]["text"] == "cache is cold (estimated)"
    assert "completion time is unavailable" in result["cold"]["tl-verb-alpha"]["title"]
    assert "cache-expiring" not in result["cold"]["term-live-alpha"]["classes"]
    assert result["beeps"] == 2


def test_last_turn_duration_stays_fixed_while_only_idle_counter_ticks():
    result = run_scenario("separate-duration-and-idle")
    assert result["initial"]["tl-verb-alpha"]["text"] == "Idle 0s"
    for stage in ["initial", "afterTime", "afterPoll", "afterHour"]:
        assert result[stage]["tl-time-alpha"]["text"] == "last turn 6m 32s"
    assert result["afterTime"]["tl-verb-alpha"]["text"] == "Idle 4m 10s"
    assert result["afterPoll"]["tl-verb-alpha"]["text"] == "Idle 4m 10s"
    assert result["afterTime"]["tl-since-alpha"]["text"] == ""
    assert result["afterHour"]["tl-verb-alpha"]["text"] == "Idle 1h 01m 40s"
    assert result["unknown"]["tl-time-alpha"]["text"] == "last turn —"
    assert result["busy"]["tl-verb-alpha"]["text"] == "Working"
    assert result["busy"]["tl-time-alpha"]["text"] == "8s"
    assert result["busyLater"]["tl-time-alpha"]["text"] == "18s"
    assert result["busyLater"]["tl-since-alpha"]["text"] == ""


def test_reduced_motion_keeps_the_warning_visible_without_flashing():
    source = APP.read_text()
    reduced = source.split("@media (prefers-reduced-motion:reduce){", 1)[1].split("/* Working", 1)[0]
    assert ".term-live.idle.cache-expiring .tl-verb{animation:none}" in reduced
    assert "outline:1px solid #56d364" in reduced
