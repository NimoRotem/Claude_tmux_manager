"""What the dashboard types on a person's behalf must never end their session.

2026-09-17, builder2: Claude Code's advance notice "Your login expires in 2 days
· run /login to renew" read as "logged out". The login watchdog pressed Escape,
typed /exit and relaunched three healthy sessions, and each relaunch died on a
transcript that held no turns ("No conversation found"), leaving a bare shell.
These tests pin each link of that chain, plus the menu guard that keeps every
auto-push mode from choosing exit, log out or a purchase.
"""
import os
import time
from pathlib import Path

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import app  # noqa: E402


# The screen calandar was showing when it was /exit'ed, byte for byte.
FRESH_SESSION_WITH_RENEWAL_NOTICE = """\
nimrod_rotem@builder2:~/tmux-dashboard-original$ source ~/.tmux-dashboard/launch/calandar.sh
[Nimo/calandar] Max plan · Opus 5 1M · Xhigh
 ▐▛███▛█   Claude Code v2.1.274
▝▜██████▀  Opus 5 (1M context) with xhigh effort · Claude Max
  ▝▝ ▝▝    ~/tmux-dashboard-original

⚠ Your login expires in 2 days · run /login to renew

────────────────────────────────────────────────────────────────────────────────
❯
────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle)
"""


# ── the renewal notice is not a logout ──────────────────────────────────────

def test_the_renewal_notice_does_not_read_as_logged_out():
    assert not app._screen_needs_login(FRESH_SESSION_WITH_RENEWAL_NOTICE)
    assert app._classify_login_watchdog_screen(FRESH_SESSION_WITH_RENEWAL_NOTICE) == (False, False)


@pytest.mark.parametrize("line", [
    "Please run /login · API Error: 401 OAuth token has expired",
    "Invalid API key · Please run /login",
    "OAuth token expired. Run /login to continue",
    "Login required",
])
def test_a_real_logout_still_reads_as_logged_out(line):
    screen = FRESH_SESSION_WITH_RENEWAL_NOTICE.replace("\n❯\n", "\n" + line + "\n❯\n")
    assert line in screen
    assert app._screen_needs_login(screen)


def test_the_notice_is_stripped_only_from_its_own_line():
    text = "⚠ Your login expires in 2 days · run /login to renew\nPlease run /login"
    assert app._screen_needs_login(text)


def test_a_login_a_person_opened_is_left_alone_far_longer_than_before():
    assert app._LOGIN_FLOW_STALE_AFTER >= 600
    assert app._LOGIN_FLOW_HUMAN_GRACE >= 1800
    app._human_input_at.pop("someone", None)
    assert app._seconds_since_human_input("someone") == float("inf")
    app._note_human_input("someone")
    assert app._seconds_since_human_input("someone") < 5


def test_the_send_and_key_routes_record_human_input():
    src = Path(app.__file__).read_text()
    send = src[src.index("async def api_send_command"):src.index("async def api_interrupt_session")]
    keys = src[src.index("async def api_send_keys"):src.index("class BracketedPasteBody")]
    assert "_note_human_input(session_name)" in send
    assert "_note_human_input(session_name)" in keys


# ── a transcript with no turns is not resumable ─────────────────────────────

@pytest.fixture
def transcripts(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_find_session_jsonl_files",
                        lambda name: [str(p) for p in tmp_path.glob("*.jsonl")])
    return tmp_path


UID = "1566299a-d6f2-4ad3-be82-78bdaa0c04dc"
STUB = ('{"type":"mode","mode":"normal","sessionId":"%s"}\n'
        '{"type":"permission-mode","permissionMode":"bypassPermissions","sessionId":"%s"}\n'
        % (UID, UID))


def test_missing_stub_and_saved_are_told_apart(transcripts):
    assert app._conversation_state("s", UID) == "missing"
    (transcripts / (UID + ".jsonl")).write_text(STUB)
    assert app._conversation_state("s", UID) == "stub"
    assert not app._conversation_exists("s", UID)
    (transcripts / (UID + ".jsonl")).write_text(
        STUB + '{"type":"user","message":{"role":"user","content":"hi"}}\n')
    assert app._conversation_state("s", UID) == "saved"
    assert app._conversation_exists("s", UID)


def test_a_stub_is_set_aside_so_session_id_can_start_on_it(transcripts):
    stub = transcripts / (UID + ".jsonl")
    stub.write_text(STUB)
    assert app._fresh_or_resume_flag("s", UID) == "--session-id " + UID
    assert not stub.exists(), "--session-id refuses an id whose file exists"
    kept = list(transcripts.glob(UID + ".jsonl.no-turns-*"))
    assert len(kept) == 1 and kept[0].read_text() == STUB, "renamed, never deleted"


def test_a_saved_conversation_resumes(transcripts):
    (transcripts / (UID + ".jsonl")).write_text(
        '{"type":"assistant","message":{"role":"assistant","content":[]}}\n')
    assert app._fresh_or_resume_flag("s", UID) == "--resume " + UID


def test_an_id_with_no_file_starts_fresh_on_the_same_id(transcripts):
    assert app._fresh_or_resume_flag("s", UID) == "--session-id " + UID


# ── menus: never exit, log out or spend ─────────────────────────────────────

BYPASS_WARNING = [(1, "No, exit"), (2, "Yes, I accept")]


def test_the_highlighted_no_exit_is_refused_for_the_other_option():
    assert app._safe_menu_choice(BYPASS_WARNING, 1) == 2
    assert app._safe_menu_choice(BYPASS_WARNING, None) == 2


def test_an_allowed_pick_is_kept():
    menu = [(1, "Yes"), (2, "Yes, and don't ask again for this command"), (3, "No, and tell Claude what to do differently")]
    assert app._safe_menu_choice(menu, 2) == 2


@pytest.mark.parametrize("label", [
    "Exit", "Quit", "Log out", "Logout", "Sign out", "No, exit", "Exit and fix manually",
    "End session", "Upgrade to Max", "Buy extra usage", "Add funds", "Switch to extra usage",
])
def test_ending_and_spending_options_are_forbidden(label):
    assert app._menu_option_forbidden(label)


@pytest.mark.parametrize("label", [
    "Yes, proceed", "Yes, and auto-accept edits", "No, keep planning", "Resume from summary",
    "Claude account with subscription", "Stop and wait for limit to reset", "Try again",
])
def test_ordinary_options_are_allowed(label):
    assert not app._menu_option_forbidden(label)


def test_a_limit_menu_waits_rather_than_buying():
    menu = [(1, "Stop and wait for limit to reset"), (2, "Add funds to continue with extra usage")]
    assert app._safe_menu_choice(menu, 2) == 1


def test_a_menu_that_only_offers_ways_out_is_left_for_a_human():
    assert app._safe_menu_choice([(1, "Exit"), (2, "Log out")], 1) is None


def test_the_picker_prompt_names_the_rule_too():
    assert "NEVER pick an option that exits" in app._MENU_PICK_SYSTEM_PROMPT


# ── typed text: never a command that ends the session ───────────────────────

@pytest.mark.parametrize("text", ["/exit", " /quit", "/logout", "/login", "/clear", "/compact now"])
def test_session_ending_commands_are_recognised(text):
    assert app._ends_session(text)


@pytest.mark.parametrize("text", ["Continue with the tests", "exit code 1 means failure, fix it",
                                  "Run the /api/exit route test"])
def test_ordinary_messages_are_not(text):
    assert not app._ends_session(text)


def test_the_typing_helper_refuses_them(monkeypatch):
    import asyncio
    calls = []
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: calls.append(a))
    assert asyncio.run(app._simple_watchdog_send_text("s", "/exit")) is False
    assert calls == []
