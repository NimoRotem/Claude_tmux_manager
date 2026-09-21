"""A session is only idle once it has stopped working, not once it looks it.

The page chimes, marks a tab finished and starts the prompt-cache countdown off
the status this decides, so calling a gap between tool calls "idle" is what
"it keeps beeping at me" is made of. Three things have to agree now: the pane
reads idle several times in a row, it has read that way for long enough, and
the session's own transcript has stopped growing (which is how a turn waiting on
its own sub-agents is told from one that has finished).
"""
import os

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import app  # noqa: E402


# ── the pane: a turn blocked on its own sub-agents ──────────────────────────

WAITING_PANE = """\
● I'll search the five directories in parallel.

● 4 background agents launched (↓ to manage)
   ├ Search group A
   └ Search group B

✻ Waiting for 4 background agents to finish

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

# The same words, but as the agent's own prose. It stays on screen for the rest
# of the session and must NOT pin it on "working".
PROSE_PANE = WAITING_PANE.replace("✻ Waiting for 4 background agents to finish",
                                  "● Waiting for the four background agents to finish scanning\n"
                                  "  the portfolio sites before I move on.")

IDLE_PANE = """\
● Done. The export is in place.

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


def _classify(name, text, changing=True):
    """Classify a pane. `changing` decides whether the pane is treated as alive:
    a real spinner animates, so a frozen one is a leftover."""
    app._pane_stability.pop(name, None)
    if changing:
        return app._classify_pane(name, text, "claude")
    app._pane_stability[name] = (
        __import__("hashlib").md5(text.encode()).hexdigest(),
        app.time.time() - 120, 5)
    return app._classify_pane(name, text, "claude")


def test_a_turn_waiting_on_its_subagents_is_busy():
    got = _classify("s1", WAITING_PANE)
    assert got["status"] == "busy"
    assert got["detail"] == "Waiting for agents"


def test_the_same_words_in_the_agents_own_prose_are_not():
    assert _classify("s2", PROSE_PANE)["status"] == "idle"


def test_a_frozen_waiting_line_is_a_leftover_not_work():
    assert _classify("s3", WAITING_PANE, changing=False)["status"] == "idle"


def test_a_finished_turn_still_reads_idle():
    assert _classify("s4", IDLE_PANE)["status"] == "idle"


@pytest.mark.parametrize("line", [
    "✻ Waiting for 4 background agents to finish",
    "· Waiting for the remaining tasks",
    "✽ Waiting on 2 subagents",
    "Waiting for completion of 3 agents",
])
def test_the_status_line_shapes_it_has_to_catch(line):
    assert (app._RE_WAITING_ON_WORK.match(line)
            or app._AGENTS_RUNNING_RE.search(line)), line


@pytest.mark.parametrize("line", [
    "● Waiting for the four background agents to finish scanning",
    "  I'm waiting for your answer on the pricing question",
])
def test_prose_is_not_a_status_line(line):
    assert not app._RE_WAITING_ON_WORK.match(line)


# ── the debounce: time and the transcript, not a count of polls ─────────────

@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1_000_000.0}
    monkeypatch.setattr(app.time, "time", lambda: now["t"])
    return now


@pytest.fixture
def fake_pane(monkeypatch):
    state = {"status": "busy"}
    monkeypatch.setattr(app, "_detect_activity_raw",
                        lambda name: {"status": state["status"], "command": "claude", "detail": ""})
    return state


@pytest.fixture
def quiet(monkeypatch):
    seconds = {"v": 999.0}
    monkeypatch.setattr(app, "_transcript_quiet_seconds", lambda name: seconds["v"])
    return seconds


def _poll(name="sess"):
    return app.detect_activity(name)["status"]


def test_three_quick_polls_do_not_make_it_idle(clock, fake_pane, quiet):
    app._activity_state.pop("sess", None)
    assert _poll() == "busy"
    fake_pane["status"] = "idle"
    for _ in range(6):                      # six polls inside five seconds
        clock["t"] += 0.8
        assert _poll() == "busy", "a count of polls is not a measure of time"
    clock["t"] += app.IDLE_CONFIRM_SECONDS
    assert _poll() == "idle"


def test_a_growing_transcript_keeps_it_busy_however_long_the_pane_looks_idle(
        clock, fake_pane, quiet):
    app._activity_state.pop("sess", None)
    _poll()
    fake_pane["status"] = "idle"
    quiet["v"] = 1.0                        # a sub-agent is still writing
    for _ in range(10):
        clock["t"] += 10
        assert _poll() == "busy"
    quiet["v"] = app.IDLE_TRANSCRIPT_QUIET + 1
    clock["t"] += app.IDLE_CONFIRM_SECONDS + 1
    assert _poll() == "busy", "the clock restarted when the transcript grew"
    clock["t"] += 5
    assert _poll() == "busy", "and the readings start again from one"
    clock["t"] += 5
    assert _poll() == "idle"


def test_work_starting_again_is_accepted_at_once(clock, fake_pane, quiet):
    app._activity_state.pop("sess", None)
    fake_pane["status"] = "idle"
    _poll()
    clock["t"] += 60
    assert _poll() == "idle"
    fake_pane["status"] = "busy"
    assert _poll() == "busy", "busy is never debounced"


def test_a_session_with_no_readable_transcript_still_settles(clock, fake_pane, monkeypatch):
    monkeypatch.setattr(app, "_transcript_quiet_seconds", lambda name: None)
    app._activity_state.pop("sess", None)
    _poll()
    fake_pane["status"] = "idle"
    for _ in range(2):
        clock["t"] += app.IDLE_CONFIRM_SECONDS
        assert _poll() == "busy"
    clock["t"] += app.IDLE_CONFIRM_SECONDS
    assert _poll() == "idle"


def test_the_three_thresholds_are_what_the_page_depends_on():
    assert app.IDLE_CONFIRM_COUNT >= 3
    assert app.IDLE_CONFIRM_SECONDS >= 10
    assert app.IDLE_TRANSCRIPT_QUIET >= 15


def test_the_transcript_read_is_this_sessions_own(monkeypatch, tmp_path):
    """A neighbour's transcript in the same working directory would report their
    work as this session's, so only the id the pane was launched on counts."""
    convo = "1566299a-d6f2-4ad3-be82-78bdaa0c04dc"
    mine = tmp_path / (convo + ".jsonl")
    mine.write_text("{}\n")
    theirs = tmp_path / "8c5096da-fb45-45fb-8a83-2fdb688178f2.jsonl"
    theirs.write_text("{}\n")
    monkeypatch.setattr(app, "_session_convo", lambda name: convo)
    monkeypatch.setattr(app, "_find_session_jsonl_files",
                        lambda name: [str(theirs), str(mine)])
    app._transcript_path_cache.pop("sess", None)
    assert app._own_transcript_path("sess") == str(mine)
    assert app._transcript_quiet_seconds("sess") < 5

    monkeypatch.setattr(app, "_session_convo", lambda name: "")
    app._transcript_path_cache.pop("sess", None)
    assert app._own_transcript_path("sess") == ""
    assert app._transcript_quiet_seconds("sess") is None
