"""Prompt-cache expiry: the countdown, the blink, and Basic+ keep-alive.

Anthropic's prompt cache is ephemeral, 5 minutes by default and 1 hour when the
request asks for it, and Claude Code buys the 1-hour one. Two properties decide
the design and are pinned here:

  * a cache READ refreshes the timer, so the deadline is measured from the last
    turn rather than from the start of the session;
  * the TTL is READ off the transcript (usage.cache_creation splits the write
    into ephemeral_1h_input_tokens / ephemeral_5m_input_tokens), never assumed,
    so a session that drops to the 5-minute cache is tracked without a code
    change.

There is no API that answers "is this prefix still cached". The one honest
measurement is whether the last turn read from cache, and that is reported
separately from the countdown.
"""
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import app  # noqa: E402

APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")


# ── the server's view ───────────────────────────────────────────────────────

def test_the_four_autopush_modes_in_order():
    assert app.AUTOPUSH_MODES == ("off", "basic", "basicplus", "full")
    assert app.AUTOPUSH_DEFAULT == "basic", "Basic+ must not become the default"


def test_basicplus_is_not_off_and_not_full():
    """The existing gates are `== "off"` and `!= "full"`, so Basic+ has to fall
    through to Basic's behaviour and must not arm the composing autopilot."""
    assert "basicplus" != "off"
    src = APP.read_text()
    assert 'if _get_autopush_mode(name) != "full":' in src, (
        "the autopilot gate changed shape; re-check that Basic+ does not arm it")


def test_deadline_is_last_turn_plus_ttl():
    assert app._cache_deadline({"cache_ttl": 3600, "last_turn_end": 1000}) == 4600
    assert app._cache_deadline({"cache_ttl": 300, "last_turn_end": 1000}) == 1300


@pytest.mark.parametrize("sess", [
    {},                                            # nothing known yet
    {"cache_ttl": 3600},                           # no turn recorded
    {"last_turn_end": 1000},                       # no cache write seen
    {"cache_ttl": 0, "last_turn_end": 0},
    {"cache_ttl": "x", "last_turn_end": 1000},     # junk
])
def test_an_unknown_deadline_is_zero_not_a_guess(sess):
    """0 means "say nothing". A guessed deadline would blink at the wrong time
    and, in Basic+, push on a made-up clock."""
    assert app._cache_deadline(sess) == 0.0


def test_the_warning_lead_is_ten_minutes_and_the_prompt_is_fixed():
    assert app.CACHE_WARN_LEAD == 600
    assert app.CACHE_KEEPALIVE_PROMPT == (
        "Continue. re-verify your own work end to end, then finish whatever is still left.")


def test_the_keepalive_never_composes_a_message():
    """The whole safety argument for Basic+ over Full is that it sends one fixed
    sentence. If the loop ever reaches the model, that argument is gone."""
    src = APP.read_text()
    start = src.index("async def _cache_keepalive_loop")
    end = src.index("\ndef ", start)
    body = src[start:end]
    for banned in ("llm_call", "_SIMPLE_WATCHDOG_SYSTEM_PROMPT", "_parse_autopilot_decision"):
        assert banned not in body, f"the keep-alive loop must not use {banned}"
    assert "CACHE_KEEPALIVE_PROMPT" in body


def test_the_keepalive_reuses_every_auto_typing_guard():
    src = APP.read_text()
    start = src.index("async def _cache_keepalive_loop")
    end = src.index("\ndef ", start)
    body = src[start:end]
    for guard in ("_detect_interactive_prompt", "_looks_like_bare_shell",
                  "_looks_like_fresh_claude_session", "_has_pending_user_input",
                  "_async_is_claude_running"):
        assert guard in body, f"the keep-alive loop must honour {guard}"
    assert 'activity.get("status") != "idle"' in body, "must never type into a busy session"


def test_a_short_cache_is_left_alone():
    """On a 5-minute cache a 10-minute lead is already past, so the nudge would
    be continuous. Those sessions are skipped rather than hammered."""
    assert app.CACHE_KEEPALIVE_MIN_TTL > app.CACHE_WARN_LEAD - 1
    assert 300 < app.CACHE_KEEPALIVE_MIN_TTL


def test_keepalive_active_reports_only_while_the_nudge_is_running():
    app._cache_keepalive_state.pop("s1", None)
    assert app._cache_keepalive_active("s1") is False
    app._cache_keepalive_state["s1"] = {"fired_for": 123.0}
    assert app._cache_keepalive_active("s1") is True
    app._cache_keepalive_state["s1"] = {}      # cleared when the turn lands
    assert app._cache_keepalive_active("s1") is False
    app._cache_keepalive_state.pop("s1", None)


# ── reading the TTL and the cache read off a real transcript ────────────────

def _facts(entries):
    return app._scan_assistant_tail([json.dumps(e) for e in entries])


def _assistant(ts, usage):
    return {"type": "assistant", "timestamp": ts, "message": {"model": "claude-opus-5", "usage": usage}}


def test_a_one_hour_write_is_read_as_3600():
    f = _facts([_assistant("2026-09-04T21:00:00.000Z", {
        "input_tokens": 2, "cache_read_input_tokens": 35686,
        "cache_creation_input_tokens": 4648,
        "cache_creation": {"ephemeral_1h_input_tokens": 4648, "ephemeral_5m_input_tokens": 0}})])
    assert f["ttl"] == 3600
    assert f["cache_read"] == 35686


def test_a_five_minute_write_is_read_as_300():
    f = _facts([_assistant("2026-09-04T21:00:00.000Z", {
        "input_tokens": 2, "cache_read_input_tokens": 10,
        "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 900}})])
    assert f["ttl"] == 300, "a session on the short cache must not be tracked as 1h"


def test_cache_read_comes_from_the_NEWEST_turn_only():
    """An older turn's cache read says nothing about now. Newest entry wins."""
    f = _facts([
        _assistant("2026-09-04T20:00:00.000Z", {"cache_read_input_tokens": 999,
                   "cache_creation": {"ephemeral_1h_input_tokens": 10, "ephemeral_5m_input_tokens": 0}}),
        _assistant("2026-09-04T21:00:00.000Z", {"cache_read_input_tokens": 111,
                   "cache_creation": {"ephemeral_1h_input_tokens": 10, "ephemeral_5m_input_tokens": 0}}),
    ])   # file order: oldest first, exactly as the transcript is written
    assert f["cache_read"] == 111


def test_a_turn_that_only_read_still_yields_a_ttl_from_further_back():
    """A read creates nothing, so the TTL is only stated on turns that wrote."""
    f = _facts([
        _assistant("2026-09-04T20:00:00.000Z", {"cache_read_input_tokens": 5,
                   "cache_creation": {"ephemeral_1h_input_tokens": 4000, "ephemeral_5m_input_tokens": 0}}),
        _assistant("2026-09-04T21:00:00.000Z", {"cache_read_input_tokens": 35686,
                   "cache_creation": {"ephemeral_1h_input_tokens": 0, "ephemeral_5m_input_tokens": 0}}),
    ])
    assert f["ttl"] == 3600
    assert f["cache_read"] == 35686


def test_a_transcript_with_no_cache_at_all_reports_no_ttl():
    f = _facts([_assistant("2026-09-04T21:00:00.000Z", {"input_tokens": 100, "output_tokens": 5})])
    assert f["ttl"] == 0
    assert f["cache_read"] == 0


# ── the page's view ─────────────────────────────────────────────────────────

pytestmark_node = pytest.mark.skipif(NODE is None, reason="node is not installed")
_TOP = re.compile(r"^(?:const|let|var|function|//)")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _js(seeds):
    lines = APP.read_text().split("\n")
    starts, bodies = {}, {}
    for i, line in enumerate(lines):
        m = re.match(r"(?:const|function)\s+([A-Za-z_$][\w$]*)", line)
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
        if n in seen or n not in bodies:
            continue
        seen.add(n)
        for ident in _IDENT.findall(bodies[n]):
            if ident in bodies and ident not in seen:
                need.append(ident)
    src = "\n".join(bodies[n] for n in sorted(seen, key=lambda n: starts[n]))
    return src.replace("__CACHE_WARN_LEAD__", str(app.CACHE_WARN_LEAD))


def run_js(sessions, expr, pre=""):
    """`pre` runs after the fixture is defined, so a test can anchor a timestamp
    to node's own clock rather than to a made-up epoch."""
    script = ("const sessions=" + json.dumps(sessions) + ";\n" + pre + "\n"
              + _js(["_pillCacheClasses", "_cacheSecondsLeft"]) + "\n"
              + "console.log(JSON.stringify(" + expr + "));")
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def _sess(name="s", **kw):
    base = {"name": name, "activity_status": "idle", "cache_ttl": 3600}
    base.update(kw)
    return base


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_page_and_the_server_share_one_definition_of_the_lead():
    """Substituted, not duplicated: if these drift the blink stops matching the push."""
    src = APP.read_text()
    assert "__CACHE_WARN_LEAD__" in src
    assert 'out.replace("__CACHE_WARN_LEAD__", str(CACHE_WARN_LEAD))' in src


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("mins_idle,blinks", [
    (0, False), (30, False), (49, False), (51, True), (59, True), (61, False),
])
def test_it_blinks_only_inside_the_last_ten_minutes(mins_idle, blinks):
    """Not before (nothing to hurry for) and not after (nothing left to save)."""
    s = _sess(last_turn_end=None)          # filled in by node against its own clock
    got = run_js([s], "_pillCacheClasses('s','idle')",
                 pre=f"sessions[0].last_turn_end=Date.now()/1000-{mins_idle*60};")
    assert (" expiring" in got) is blinks, f"{mins_idle}m idle -> {got!r}"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_session_with_no_measured_ttl_never_blinks():
    got = run_js([_sess(cache_ttl=0, last_turn_end=1)], "_pillCacheClasses('s','idle')")
    assert got == ""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_a_busy_session_does_not_blink():
    """Working refreshes the cache on every request; blinking at it is noise."""
    s = _sess(activity_status="busy", last_turn_end=1)  # long cold; busy still must not blink
    assert " expiring" not in run_js([s], "_pillCacheClasses('s','busy')")


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_keepalive_run_is_amber_and_not_blinking():
    s = _sess(activity_status="busy", cache_keepalive=True, last_turn_end=1)
    got = run_js([s], "_pillCacheClasses('s','busy')")
    assert " keepcache" in got
    assert " expiring" not in got


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_an_ordinary_busy_session_is_not_amber():
    s = _sess(activity_status="busy", cache_keepalive=False, last_turn_end=1)
    assert run_js([s], "_pillCacheClasses('s','busy')") == ""


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_seconds_left_is_null_when_unknown_and_zero_when_cold():
    """null and 0 must not be conflated: one means "say nothing"."""
    assert run_js([_sess(cache_ttl=0)], "_cacheSecondsLeft('s')") is None
    assert run_js([_sess(last_turn_end=None)], "_cacheSecondsLeft('s')") is None
    cold = run_js([_sess(last_turn_end=None)], "_cacheSecondsLeft('s')",
                  pre="sessions[0].last_turn_end=Date.now()/1000-99999;")
    assert cold == 0


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_fourth_button_exists_and_is_labelled_basic_plus():
    src = APP.read_text()
    assert "b('basicplus','Basic +')" in src
    assert ".autopush-seg button.ap-basicplus.active" in src, "no active style for the 4th segment"
    seg = src[src.index("'<div class=\"autopush-seg'"):]
    seg = seg[:seg.index("</div>")]
    assert seg.index("basicplus") < seg.index("'Full'"), "Basic + must sit between Basic and Full"


# ── the idle strip's own blink (a second, independent code path) ────────────
# _pillCacheClasses decides the pill; _paintIdleSince decides the "idle 22m"
# text and its class. Widening one window without the other is a real defect and
# the pill tests above cannot see it, so this drives the strip directly against
# a DOM stub.

def run_paint(sess, expr="[el.className,el.textContent]"):
    el = ("{className:'',textContent:'',title:''}")
    script = (
        "const sessions=[" + json.dumps(sess) + "];\n"
        "sessions[0].last_turn_end=Date.now()/1000-(" + str(sess.pop("_ago", 0)) + ");\n"
        "const el=" + el + ";\n"
        "function _sessionBusy(){return " + ("true" if sess.get("_busy") else "false") + "}\n"
        "const document={getElementById:function(){return el}};\n"
        + _js(["_paintIdleSince"]) + "\n"
        "_paintIdleSince('s');\n"
        "console.log(JSON.stringify(" + expr + "));")
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("mins,blinks", [
    (10, False), (49, False), (51, True), (59, True), (61, False),
])
def test_the_idle_strip_blinks_on_the_same_window_as_the_pill(mins, blinks):
    cls, _ = run_paint({"name": "s", "cache_ttl": 3600, "_ago": mins * 60})
    assert ("expiring" in cls) is blinks, f"{mins}m -> {cls!r}"


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_strip_counts_down_only_once_it_matters():
    _, txt = run_paint({"name": "s", "cache_ttl": 3600, "_ago": 30 * 60})
    assert "cold in" not in txt, "a countdown 30 minutes out is just noise"
    _, txt = run_paint({"name": "s", "cache_ttl": 3600, "_ago": 55 * 60})
    assert "cold in" in txt, "inside the lead it should say how long is left"
    _, txt = run_paint({"name": "s", "cache_ttl": 3600, "_ago": 70 * 60})
    assert "cache cold" in txt and "cold in" not in txt


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_strip_says_nothing_about_cache_without_a_measured_ttl():
    cls, txt = run_paint({"name": "s", "cache_ttl": 0, "_ago": 55 * 60})
    assert "expiring" not in cls
    assert "cold" not in txt


def test_the_keepalive_refuses_a_transcript_it_cannot_prove_is_ours():
    """Several sessions in one working directory share a transcript, and
    detect_sure says so. Showing a neighbour's countdown is cosmetic; typing on
    it is not."""
    src = APP.read_text()
    start = src.index("async def _cache_keepalive_loop")
    end = src.index("\ndef ", start)
    body = src[start:end]
    assert 'fields.get("detect_sure", True)' in body, (
        "the keep-alive must skip a session whose transcript is a guess")


def test_the_two_states_do_not_share_a_colour():
    """Green blinking means "come and type"; amber steady means "it is handling
    it". One colour for both would defeat the point of having two states."""
    src = APP.read_text()
    expiring = src[src.index(".status-pill.idle.expiring{"):]
    expiring = expiring[:expiring.index(".tl-since.expiring")]
    keep = src[src.index(".status-pill.busy.keepcache{"):]
    keep = keep[:keep.index("\n\n")] if "\n\n" in keep[:400] else keep[:400]
    assert "#3fb950" in expiring or "#56d364" in expiring, "the pre-cold blink must stay green"
    assert "#d29922" in keep or "#e3b341" in keep, "the keep-alive state must be amber"
    assert "#d29922" not in expiring and "#e3b341" not in expiring, \
        "the blink must not borrow the keep-alive's amber"
