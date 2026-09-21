"""Live voice mode: listen, send, read the reply back, listen again.

The mic button was always one-way (record a clip, transcribe it into the box).
Live voice closes the loop so a session can be driven from a phone with the
screen away. These tests cover the half that can be tested without a microphone:
what gets spoken, what the server sends back, and the state machine's rules
about when the mic is open.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest
from fastapi.testclient import TestClient

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import app  # noqa: E402

APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")


@pytest.fixture
def client():
    c = TestClient(app.app)
    c.cookies.set("tmux_auth", app._make_token("admin"))
    return c


# ── what a reply sounds like ────────────────────────────────────────────────

def test_markdown_is_turned_into_something_worth_hearing():
    said = app._plain_for_speech(
        "# Done\n\nAdded a **CSV export** on `/orders`.\n\n- one\n- two\n\n"
        "```python\nprint(1)\n```\n\nSee https://example.com/a/very/long/path for the file."
    )
    assert "**" not in said and "`" not in said and "#" not in said
    assert "https://" not in said, "a URL read out character by character is noise"
    assert "code block" in said
    assert "Added a CSV export on /orders" in said


def test_speech_is_capped_so_one_reply_cannot_talk_for_ten_minutes():
    assert len(app._plain_for_speech("word " * 5000)) <= app._TTS_MAX_CHARS


def test_the_endpoint_returns_mp3(client):
    spoken = {}

    def fake_create(model, voice, input, response_format):
        spoken.update(model=model, voice=voice, input=input, fmt=response_format)
        return MagicMock(read=lambda: b"ID3fake-mp3-bytes")

    fake = MagicMock()
    fake.audio.speech.create.side_effect = lambda **kw: fake_create(**kw)
    with patch.object(app.openai, "OpenAI", return_value=fake):
        r = client.post("/api/tts", json={"text": "Added the **export**."})
    assert r.status_code == 200
    assert r.headers["content-type"] == "audio/mpeg"
    assert r.content == b"ID3fake-mp3-bytes"
    assert spoken["fmt"] == "mp3" and spoken["input"] == "Added the export."


def test_an_empty_text_is_refused(client):
    assert client.post("/api/tts", json={"text": "   "}).status_code == 400


def test_a_made_up_voice_name_cannot_be_injected(client):
    fake = MagicMock()
    fake.audio.speech.create.return_value = MagicMock(read=lambda: b"x")
    with patch.object(app.openai, "OpenAI", return_value=fake):
        client.post("/api/tts", json={"text": "hello", "voice": "../../etc/passwd"})
    assert fake.audio.speech.create.call_args.kwargs["voice"] == app.TTS_VOICE


def test_speech_needs_a_key(client, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    assert client.post("/api/tts", json={"text": "hello"}).status_code == 503


def test_the_route_is_behind_the_dashboard_login():
    """Speech costs money, so an anonymous call must reach the login page, not
    the model. The dashboard answers an unauthenticated request with that page
    rather than a 401, so the body is what proves it."""
    anon = TestClient(app.app)
    r = anon.post("/api/tts", json={"text": "hello"}, follow_redirects=False)
    assert "text/html" in r.headers.get("content-type", "")
    assert "Enter credentials" in r.text


# ── the page's state machine ────────────────────────────────────────────────

pytestmark_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
_TOP = re.compile(r"^(?:const|let|var|function|//)")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _js(seeds, provided=()):
    lines = APP.read_text().split("\n")
    starts, bodies = {}, {}
    for i, line in enumerate(lines):
        m = re.match(r"(?:const|function|async function)\s+([A-Za-z_$][\w$]*)", line)
        if not m or m.group(1) in starts:
            continue
        j = i + 1
        while j < len(lines) and not _TOP.match(lines[j]):
            j += 1
        starts[m.group(1)] = i
        bodies[m.group(1)] = "\n".join(lines[i:j]).rstrip()
    need, seen = list(seeds), set()
    while need:
        n = need.pop()
        if n in seen or n not in bodies or n in provided:
            continue
        seen.add(n)
        for ident in _IDENT.findall(bodies[n]):
            if ident in bodies and ident not in seen:
                need.append(ident)
    return "\n".join(bodies[n] for n in sorted(seen, key=lambda n: starts[n]))


def node(seeds, body, pre=""):
    provided = set(re.findall(r"(?:const|let|var|function)\s+([A-Za-z_$][\w$]*)", pre))
    script = pre + "\n" + _js(seeds, provided) + "\n" + body
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


VOICE_PRE = """
const BASE='';
const chatMessages={s:[]};
const lastStatus={s:'idle'};
const sent=[],toasts=[],bubbles=[];
function showToast(m){toasts.push(m)}
function appendChatBubble(n,r,t){bubbles.push([n,r,t])}
function setOptimisticBusy(){}
function unlockCompletionAudio(){}
function _getCompletionAudioContext(){return null}
function renderVoiceBar(){}
function _voiceSet(){}
function refreshOne(){return Promise.resolve()}
function _voiceListen(){_voice.phase='listening'}
const document={getElementById:()=>null,querySelectorAll:()=>[]};
const window={};
let fetchReply={ok:true,json:()=>Promise.resolve({text:'add the missing tests'})};
function fetch(url,opts){
  if(url.indexOf('/api/transcribe')>=0)return Promise.resolve(fetchReply);
  sent.push(JSON.parse(opts.body).command);
  return Promise.resolve({ok:true,json:()=>Promise.resolve({ok:true})});
}
class Blob{constructor(parts,opts){this.size=1;this.type=(opts&&opts.type)||''}}
class FormData{append(k,v,name){this.name=name}}
"""


@pytestmark_node
def test_what_you_said_is_sent_to_the_session_and_shown_in_chat():
    js = """
      _voice.phase='listening';_voice.name='s';
      _voiceSend(new Blob([],{type:'audio/webm'})).then(()=>
        console.log(JSON.stringify({sent,bubbles,phase:_voice.phase,awaiting:_voice.awaiting})));
    """
    out = node(["_voiceSend", "_voice"], js, pre=VOICE_PRE)
    assert out["sent"] == ["add the missing tests"]
    assert out["bubbles"] == [["s", "user", "add the missing tests"]]
    assert out["awaiting"] is True
    assert out["phase"] == "listening", "it goes straight back to listening"


@pytestmark_node
@pytest.mark.parametrize("said,stops", [
    ("stop voice mode", True), ("Stop live voice.", True), ("end voice", True),
    ("stop the tests from running", False), ("exit code 1 again", False),
])
def test_saying_stop_voice_mode_ends_it_and_nothing_else_does(said, stops):
    js = """
      _voice.phase='listening';_voice.name='s';
      fetchReply={ok:true,json:()=>Promise.resolve({text:%s})};
      _voiceSend(new Blob([],{type:'audio/webm'})).then(()=>
        console.log(JSON.stringify({sent,phase:_voice.phase})));
    """ % json.dumps(said)
    out = node(["_voiceSend", "_voice", "stopLiveVoice"], js, pre=VOICE_PRE)
    assert (out["phase"] == "off") is stops
    assert (out["sent"] == []) is stops


@pytestmark_node
def test_silence_that_transcribes_to_nothing_is_not_sent():
    js = """
      _voice.phase='listening';_voice.name='s';
      fetchReply={ok:true,json:()=>Promise.resolve({text:'  .  '})};
      _voiceSend(new Blob([],{type:'audio/webm'})).then(()=>
        console.log(JSON.stringify({sent,phase:_voice.phase})));
    """
    out = node(["_voiceSend", "_voice"], js, pre=VOICE_PRE)
    assert out["sent"] == [] and out["phase"] == "listening"


@pytestmark_node
def test_the_clip_is_named_for_what_the_browser_recorded():
    """iOS records mp4; whisper reads the extension, so it must not say webm."""
    js = """
      _voice.name='s';
      const types=['audio/webm;codecs=opus','audio/mp4','audio/ogg;codecs=opus'];
      (async()=>{
        for(const t of types){
          _voice.phase='listening';
          await _voiceSend(new Blob([],{type:t}));
        }
        console.log(JSON.stringify(clipNames));
      })();
    """
    pre = VOICE_PRE.replace("class FormData{append(k,v,name){this.name=name}}",
                            "const clipNames=[];class FormData{append(k,v,name){clipNames.push(name)}}")
    assert node(["_voiceSend", "_voice"], js, pre=pre) == ["voice.webm", "voice.m4a", "voice.ogg"]


@pytestmark_node
@pytest.mark.parametrize("ms,heard,stops", [
    (500, True, False),      # still talking
    (1600, True, True),      # a pause long enough to be the end of a turn
    (1600, False, False),    # nothing heard yet: keep listening, upload nothing
])
def test_a_turn_ends_after_a_pause_not_a_timer(ms, heard, stops):
    pre = VOICE_PRE + """
      const stopped=[];
      function _voiceStopClip(drop){stopped.push(!!drop);_voice.phase='sending'}
    """
    js = """
      _voice.phase='listening';_voice.name='s';_voice.analyser=null;
      _voice.heard=%s;_voice.startedAt=Date.now()-%d;_voice.speechAt=%s;
      _voiceTick();
      console.log(JSON.stringify({stopped}));
    """ % ("true" if heard else "false", ms, ("Date.now()-%d" % ms) if heard else "0")
    out = node(["_voiceTick", "_voice"], js, pre=pre)
    assert (out["stopped"] == [False]) is stops


@pytestmark_node
def test_a_long_silence_throws_the_clip_away_rather_than_uploading_it():
    pre = VOICE_PRE + """
      const stopped=[];
      function _voiceStopClip(drop){stopped.push(!!drop);_voice.phase='sending'}
      function _voiceLevel(){return 0}
    """
    js = """
      _voice.phase='listening';_voice.name='s';_voice.analyser={};
      _voice.heard=false;_voice.startedAt=Date.now()-31000;_voice.speechAt=0;
      _voiceTick();
      console.log(JSON.stringify({stopped}));
    """
    assert node(["_voiceTick", "_voice"], js, pre=pre)["stopped"] == [True]


def test_the_mic_is_closed_while_the_reply_is_read_out():
    src = APP.read_text()
    speak = src[src.index("async function _voiceSpeak(text){"):]
    speak = speak[:speak.index("\n}")]
    assert "_voice.dropClip=true;" in speak
    assert "_voice.rec.stop()" in speak
    assert speak.index("_voiceSet('speaking'") < speak.index("/api/tts")
    assert "_voiceListen()" in speak, "and it listens again when the reply ends"


def test_the_browsers_own_voice_is_the_fallback():
    src = APP.read_text()
    speak = src[src.index("async function _voiceSpeak(text){"):]
    speak = speak[:speak.index("\n}")]
    assert "speechSynthesis" in speak and "SpeechSynthesisUtterance" in speak


def test_leaving_the_chat_tab_or_the_session_turns_the_mic_off():
    src = APP.read_text()
    switch = src[src.index("function switchTab(name,tab,updateRoute=true){"):]
    switch = switch[:switch.index("\n}")]
    assert "stopLiveVoice" in switch
    select = src[src.index("function selectSession(name,updateRoute=true){"):]
    select = select[:select.index("\n}")]
    assert "stopLiveVoice" in select
    assert "if(typeof _voice==='object'&&_voice&&_voice.phase!=='off')return false;" in src, \
        "a page mid-conversation must not reload itself under the user"


def test_the_chat_composer_carries_the_toggle():
    src = APP.read_text()
    assert 'id="cmd-voice-${s.name}"' in src and 'onclick="toggleLiveVoice(' in src
    assert 'id="voice-bar-${s.name}"' in src
