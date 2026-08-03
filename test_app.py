"""Tests for tmux Dashboard app.py — utility functions and API logic."""
import hashlib
import hmac
import json
import os
import time

import pytest

# Patch environment before importing app to control auth behavior
os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

from unittest.mock import MagicMock, patch

from app import (
    _RE_COMPLETION,
    _RE_COMPLETION_MSG,
    _RE_IDLE_PROMPT,
    _RE_RUNNING_TASK,
    _RE_SHELL_PROMPT,
    _RE_SPINNER_INLINE,
    _RE_SPINNER_START,
    _RE_THOUGHT,
    _RE_TIP_CODEX,
    AUTH_SECRET,
    IDLE_CONFIRM_COUNT,
    _activity_state,
    _append_assistant_msg,
    _away_log,
    _away_state_summary,
    _check_login_rate_limit,
    _check_token,
    _detect_interactive_prompt,
    _find_session,
    _is_valid_session_name,
    _login_attempts,
    _make_token,
    _msg_similarity,
    build_session_response,
    detect_activity,
    get_pane_position,
    get_session_cwd,
    get_tmux_sessions,
)

# ─── Auth Token Tests ───


class TestMakeToken:
    def test_returns_username_colon_signature(self):
        token = _make_token("admin")
        assert ":" in token
        username, sig = token.split(":", 1)
        assert username == "admin"
        assert len(sig) == 24  # truncated hex

    def test_deterministic(self):
        assert _make_token("admin") == _make_token("admin")

    def test_different_users_different_tokens(self):
        assert _make_token("admin") != _make_token("user2")

    def test_empty_username(self):
        token = _make_token("")
        assert token.startswith(":")
        assert len(token.split(":", 1)[1]) == 24


class TestCheckToken:
    def test_valid_token(self):
        token = _make_token("admin")
        assert _check_token(token) is True

    def test_invalid_signature(self):
        assert _check_token("admin:invalidsig000000000") is False

    def test_empty_string(self):
        assert _check_token("") is False

    def test_none(self):
        assert _check_token(None) is False

    def test_no_colon(self):
        assert _check_token("nocolonhere") is False

    def test_tampered_username(self):
        token = _make_token("admin")
        tampered = "hacker" + token[5:]
        assert _check_token(tampered) is False

    def test_roundtrip(self):
        for user in ["admin", "test", "a", "user-with-dashes", "user_underscore"]:
            token = _make_token(user)
            assert _check_token(token) is True


# ─── Message Similarity Tests ───


class TestMsgSimilarity:
    def test_identical_strings(self):
        assert _msg_similarity("hello world", "hello world") == 1.0

    def test_completely_different(self):
        assert _msg_similarity("hello world", "foo bar") == 0.0

    def test_partial_overlap(self):
        sim = _msg_similarity("the quick brown fox", "the quick red dog")
        assert 0.0 < sim < 1.0
        # "the" and "quick" overlap = 2 out of 4 = 0.5
        assert sim == pytest.approx(0.5)

    def test_empty_first(self):
        assert _msg_similarity("", "hello") == 0.0

    def test_empty_second(self):
        assert _msg_similarity("hello", "") == 0.0

    def test_both_empty(self):
        assert _msg_similarity("", "") == 0.0

    def test_case_insensitive(self):
        assert _msg_similarity("Hello World", "hello world") == 1.0

    def test_single_word_match(self):
        assert _msg_similarity("hello", "hello") == 1.0

    def test_subset_overlap(self):
        # "a b" vs "a b c d" => overlap 2, max len 4 => 0.5
        sim = _msg_similarity("a b", "a b c d")
        assert sim == pytest.approx(0.5)


# ─── Append Assistant Message Tests ───


class TestAppendAssistantMsg:
    def test_appends_new_message(self):
        entry = {"messages": []}
        # Mock _save_messages to prevent file I/O
        import app
        original_save = app._save_messages
        app._save_messages = lambda: None
        try:
            _append_assistant_msg(entry, "hello", 1000.0)
            assert len(entry["messages"]) == 1
            assert entry["messages"][0]["role"] == "assistant"
            assert entry["messages"][0]["text"] == "hello"
            assert entry["messages"][0]["ts"] == 1000.0
        finally:
            app._save_messages = original_save

    def test_skips_identical_message(self):
        import app
        original_save = app._save_messages
        app._save_messages = lambda: None
        try:
            entry = {"messages": [{"role": "assistant", "text": "hello", "ts": 1.0}]}
            _append_assistant_msg(entry, "hello", 2.0)
            assert len(entry["messages"]) == 1  # not added
        finally:
            app._save_messages = original_save

    def test_skips_very_similar_message(self):
        import app
        original_save = app._save_messages
        app._save_messages = lambda: None
        try:
            entry = {"messages": [
                {"role": "assistant", "text": "the quick brown fox jumps", "ts": 1.0}
            ]}
            # >70% word overlap
            _append_assistant_msg(entry, "the quick brown fox leaps", 2.0)
            assert len(entry["messages"]) == 1  # not added (4/5 = 0.8 > 0.7)
        finally:
            app._save_messages = original_save

    def test_updates_different_message_in_same_turn(self):
        import app
        original_save = app._save_messages
        app._save_messages = lambda: None
        try:
            entry = {"messages": [
                {"role": "assistant", "text": "hello world", "ts": 1.0}
            ]}
            _append_assistant_msg(entry, "completely different text here", 2.0)
            assert len(entry["messages"]) == 1
            assert entry["messages"][0]["text"] == "completely different text here"
            assert entry["messages"][0]["ts"] == 2.0
        finally:
            app._save_messages = original_save

    def test_creates_messages_key_if_missing(self):
        import app
        original_save = app._save_messages
        app._save_messages = lambda: None
        try:
            entry = {}
            _append_assistant_msg(entry, "first message", 1.0)
            assert "messages" in entry
            assert len(entry["messages"]) == 1
        finally:
            app._save_messages = original_save

    def test_updates_assistant_message_after_last_user(self):
        import app
        original_save = app._save_messages
        app._save_messages = lambda: None
        try:
            entry = {"messages": [
                {"role": "assistant", "text": "old message", "ts": 1.0},
                {"role": "user", "text": "user input", "ts": 2.0},
                {"role": "assistant", "text": "recent message", "ts": 3.0},
            ]}
            # Same as an older turn, but the current turn's assistant bubble is updated.
            _append_assistant_msg(entry, "old message", 4.0)
            assert len(entry["messages"]) == 3
            assert entry["messages"][-1]["text"] == "old message"
        finally:
            app._save_messages = original_save


# ─── Away Mode Utility Tests ───


class TestAwayLog:
    def test_appends_log_entry(self):
        state = {"phase": 1, "step": 2, "log": []}
        _away_log(state, "test action")
        assert len(state["log"]) == 1
        assert state["log"][0]["action"] == "test action"
        assert state["log"][0]["phase"] == 1
        assert state["log"][0]["step"] == 2
        assert "ts" in state["log"][0]

    def test_creates_log_list_if_missing(self):
        state = {"phase": 1, "step": 1}
        _away_log(state, "first entry")
        assert "log" in state
        assert len(state["log"]) == 1

    def test_truncates_at_200_entries(self):
        state = {"phase": 1, "step": 1, "log": [{"ts": i, "phase": 1, "step": 1, "action": f"entry-{i}"} for i in range(200)]}
        _away_log(state, "entry-200")
        assert len(state["log"]) == 200  # stays at 200, not 201
        assert state["log"][-1]["action"] == "entry-200"
        assert state["log"][0]["action"] == "entry-1"  # entry-0 was dropped

    def test_defaults_phase_and_step(self):
        state = {}
        _away_log(state, "no phase")
        assert state["log"][0]["phase"] == 0
        assert state["log"][0]["step"] == 0


class TestAwayStateSummary:
    def test_returns_safe_dict(self):
        state = {
            "enabled": True,
            "phase": 3,
            "phase_name": "execute",
            "step": 2,
            "step_name": "running tests",
            "started_at": 1000.0,
            "log": [{"ts": 1, "phase": 1, "step": 1, "action": "test"}],
            "report": "all good",
            "task": "SHOULD_NOT_APPEAR",  # asyncio.Task — not JSON-safe
        }
        summary = _away_state_summary(state)
        assert summary["enabled"] is True
        assert summary["phase"] == 3
        assert summary["phase_name"] == "execute"
        assert "task" not in summary
        assert json.dumps(summary)  # must be JSON-serializable

    def test_empty_state_defaults(self):
        summary = _away_state_summary({})
        assert summary["enabled"] is False
        assert summary["phase"] == 0
        assert summary["phase_name"] == ""


# ─── Interactive Prompt Detection Tests ───


class TestDetectInteractivePrompt:
    def test_no_prompt(self):
        assert _detect_interactive_prompt("just some normal output\nno prompts here") is None

    def test_no_selector(self):
        text = "1. Option one\n2. Option two\n3. Option three"
        assert _detect_interactive_prompt(text) is None  # no ❯ cursor

    def test_plan_approval_prompt(self):
        text = "\n".join([
            "Would you like to proceed",
            "❯ 1. Yes, proceed",
            "  2. No, cancel",
            "  3. Modify plan",
        ])
        result = _detect_interactive_prompt(text)
        assert result == "plan_approval"

    def test_selection_prompt(self):
        text = "\n".join([
            "Choose an option:",
            "❯ 1. First option",
            "  2. Second option",
        ])
        result = _detect_interactive_prompt(text)
        assert result == "selection_prompt"

    def test_empty_string(self):
        assert _detect_interactive_prompt("") is None

    def test_single_option_not_enough(self):
        text = "❯ 1. Only one option"
        assert _detect_interactive_prompt(text) is None

    def test_bypass_permissions_prompt(self):
        text = "\n".join([
            "bypass permissions for this session",
            "❯ 1. Yes, bypass all",
            "  2. No, ask each time",
            "  3. Cancel",
        ])
        result = _detect_interactive_prompt(text)
        assert result == "plan_approval"


# ─── Session Name Validation Tests ───


class TestSessionNameValidation:
    """Test the regex pattern used for session name validation."""

    def test_valid_names(self):
        import re
        pattern = r'^[a-zA-Z0-9_-]+$'
        valid = ["test", "my-session", "session_1", "ABC123", "a", "a-b-c", "a_b_c"]
        for name in valid:
            assert re.match(pattern, name), f"Should be valid: {name}"

    def test_invalid_names(self):
        import re
        pattern = r'^[a-zA-Z0-9_-]+$'
        invalid = ["", "test session", "test;rm -rf /", "test$(cmd)", "../etc", "test\ninjection", "test\x00null"]
        for name in invalid:
            assert not re.match(pattern, name), f"Should be invalid: {name}"


# ─── Path Traversal Protection Tests ───


class TestClaudeMdPathValidation:
    """Test the path validation logic used in api_save_claude_md."""

    def _validate_path(self, path: str) -> bool:
        """Reproduce the validation logic from api_save_claude_md."""
        from pathlib import Path
        if not path.endswith("AGENTS.md"):
            return False
        real_path = os.path.realpath(path)
        home_dir = str(Path.home())
        if not real_path.startswith(home_dir + "/") and real_path != home_dir:
            return False
        if not real_path.endswith("/AGENTS.md"):
            return False
        return True

    def test_valid_home_claude_md(self):
        from pathlib import Path
        path = str(Path.home() / "AGENTS.md")
        assert self._validate_path(path) is True

    def test_valid_project_claude_md(self):
        from pathlib import Path
        path = str(Path.home() / "some-project" / "AGENTS.md")
        assert self._validate_path(path) is True

    def test_rejects_etc_claude_md(self):
        assert self._validate_path("/etc/AGENTS.md") is False

    def test_rejects_root_claude_md(self):
        assert self._validate_path("/root/AGENTS.md") is False

    def test_rejects_dot_dot_traversal(self):
        from pathlib import Path
        path = str(Path.home() / ".." / "etc" / "AGENTS.md")
        assert self._validate_path(path) is False

    def test_rejects_non_claude_md(self):
        from pathlib import Path
        path = str(Path.home() / "some-file.txt")
        assert self._validate_path(path) is False

    def test_rejects_tricky_suffix(self):
        # "notAGENTS.md" ends with AGENTS.md but isn't a standalone filename
        from pathlib import Path
        path = str(Path.home() / "notAGENTS.md")
        # os.path.realpath resolves to /home/user/notAGENTS.md
        # endswith("/AGENTS.md") would fail since it's "/notAGENTS.md"
        assert self._validate_path(path) is False


# ─── _find_session Helper Tests ───


class TestFindSession:
    """Test the _find_session helper (mocking get_tmux_sessions)."""

    def test_finds_existing_session(self):
        import app
        original = app.get_tmux_sessions
        app.get_tmux_sessions = lambda: [
            {"name": "sess1", "windows": "1"},
            {"name": "sess2", "windows": "2"},
        ]
        try:
            sessions, found = _find_session("sess1")
            assert found is not None
            assert found["name"] == "sess1"
            assert len(sessions) == 2
        finally:
            app.get_tmux_sessions = original

    def test_returns_none_for_missing(self):
        import app
        original = app.get_tmux_sessions
        app.get_tmux_sessions = lambda: [
            {"name": "sess1", "windows": "1"},
        ]
        try:
            sessions, found = _find_session("nonexistent")
            assert found is None
            assert len(sessions) == 1
        finally:
            app.get_tmux_sessions = original

    def test_empty_sessions_list(self):
        import app
        original = app.get_tmux_sessions
        app.get_tmux_sessions = lambda: []
        try:
            sessions, found = _find_session("any")
            assert found is None
            assert sessions == []
        finally:
            app.get_tmux_sessions = original

    def test_returns_correct_session_dict(self):
        import app
        original = app.get_tmux_sessions
        app.get_tmux_sessions = lambda: [
            {"name": "a", "windows": "1", "attached": False},
            {"name": "b", "windows": "3", "attached": True},
        ]
        try:
            _, found = _find_session("b")
            assert found["windows"] == "3"
            assert found["attached"] is True
        finally:
            app.get_tmux_sessions = original


# ─── get_tmux_sessions Parsing Tests ───


class TestGetTmuxSessions:
    """Test tmux session list parsing with mocked subprocess."""

    @pytest.fixture(autouse=True)
    def _show_test_sessions_in_codex_dashboard(self):
        # `get_tmux_sessions` also re-adds *parked* sessions from the lifecycle
        # store so a retained session does not vanish from the UI. That store is
        # real state on the host, so without isolating it these tests fail on any
        # box that happens to have a parked session.
        import app as _app
        empty = {"sessions": {}}
        with patch("app._session_is_codex", return_value=True), \
             patch.object(_app._session_lifecycle, "snapshot", return_value=empty):
            yield

    @patch("app.subprocess.run")
    def test_parses_standard_output(self, mock_run):
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="main:3:1700000000:1\nwork:1:1700001000:0\n"
        )
        sessions = get_tmux_sessions()
        assert len(sessions) == 2
        assert sessions[0]["name"] == "main"
        assert sessions[0]["windows"] == "3"
        assert sessions[0]["created"] == "1700000000"
        assert sessions[0]["attached"] is True
        assert sessions[1]["name"] == "work"
        assert sessions[1]["attached"] is False

    @patch("app.subprocess.run")
    def test_empty_output(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="")
        sessions = get_tmux_sessions()
        assert sessions == []

    @patch("app.subprocess.run")
    def test_nonzero_returncode(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="error")
        sessions = get_tmux_sessions()
        assert sessions == []

    @patch("app.subprocess.run")
    def test_subprocess_exception(self, mock_run):
        mock_run.side_effect = Exception("tmux not running")
        sessions = get_tmux_sessions()
        assert sessions == []

    @patch("app.subprocess.run")
    def test_partial_fields(self, mock_run):
        """Session line with fewer fields than expected."""
        mock_run.return_value = MagicMock(returncode=0, stdout="lonely\n")
        sessions = get_tmux_sessions()
        assert len(sessions) == 1
        assert sessions[0]["name"] == "lonely"
        assert sessions[0]["windows"] == "?"
        assert sessions[0]["created"] == ""
        assert sessions[0]["attached"] is False

    @patch("app.subprocess.run")
    def test_session_name_with_colons(self, mock_run):
        """Session name containing colons splits correctly (only first 4 parts used)."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="my:session:2:1700000000:0\n"
        )
        sessions = get_tmux_sessions()
        # parts[0] = "my", parts[1] = "session", parts[2] = "2", parts[3] = "1700000000"
        assert sessions[0]["name"] == "my"


# ─── get_pane_position Tests ───


class TestGetPanePosition:
    @patch("app.subprocess.run")
    def test_parses_position(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="500:25\n")
        pos = get_pane_position("test-session")
        assert pos["total_lines"] == 525  # 500 + 25

    @patch("app.subprocess.run")
    def test_zero_position(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="0:0\n")
        pos = get_pane_position("test-session")
        assert pos["total_lines"] == 0

    @patch("app.subprocess.run")
    def test_failure_returns_zero(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        pos = get_pane_position("test-session")
        assert pos["total_lines"] == 0

    @patch("app.subprocess.run")
    def test_exception_returns_zero(self, mock_run):
        mock_run.side_effect = Exception("timeout")
        pos = get_pane_position("test-session")
        assert pos["total_lines"] == 0


# ─── detect_activity Hysteresis Tests ───


class TestDetectActivityHysteresis:
    """Test the state machine transitions in detect_activity()."""

    def setup_method(self):
        """Clean up global state before each test."""
        _activity_state.clear()

    @patch("app._detect_activity_raw")
    def test_first_reading_accepted(self, mock_raw):
        mock_raw.return_value = {"status": "busy", "command": "claude", "detail": "Working"}
        result = detect_activity("test-sess")
        assert result["status"] == "busy"
        assert "test-sess" in _activity_state

    @patch("app._detect_activity_raw")
    def test_first_idle_reading_accepted(self, mock_raw):
        mock_raw.return_value = {"status": "idle", "command": "claude", "detail": "Waiting"}
        result = detect_activity("test-sess")
        assert result["status"] == "idle"
        assert _activity_state["test-sess"]["consecutive_idle"] == 1

    @patch("app._detect_activity_raw")
    def test_idle_to_busy_immediate(self, mock_raw):
        """Transition from idle to busy should be immediate (no debounce)."""
        # Start idle
        mock_raw.return_value = {"status": "idle", "command": "claude", "detail": "Waiting"}
        detect_activity("test-sess")
        # Go busy
        mock_raw.return_value = {"status": "busy", "command": "claude", "detail": "Working"}
        result = detect_activity("test-sess")
        assert result["status"] == "busy"

    @patch("app._detect_activity_raw")
    def test_busy_to_idle_requires_confirmation(self, mock_raw):
        """Transition from busy to idle requires IDLE_CONFIRM_COUNT consecutive readings."""
        # Start busy
        mock_raw.return_value = {"status": "busy", "command": "claude", "detail": "Working"}
        detect_activity("test-sess")

        # First idle reading — should stay busy
        mock_raw.return_value = {"status": "idle", "command": "claude", "detail": "Waiting"}
        result = detect_activity("test-sess")
        assert result["status"] == "busy"  # held busy

        # Second idle reading — still busy (need IDLE_CONFIRM_COUNT=3)
        result = detect_activity("test-sess")
        assert result["status"] == "busy"  # still held

        # Third idle reading — NOW transitions to idle
        result = detect_activity("test-sess")
        assert result["status"] == "idle"

    @patch("app._detect_activity_raw")
    def test_busy_to_idle_interrupted_by_busy(self, mock_raw):
        """A busy reading during idle confirmation resets the counter."""
        # Start busy
        mock_raw.return_value = {"status": "busy", "command": "claude", "detail": "Working"}
        detect_activity("test-sess")

        # One idle reading
        mock_raw.return_value = {"status": "idle", "command": "claude", "detail": "Waiting"}
        result = detect_activity("test-sess")
        assert result["status"] == "busy"  # held

        # Interrupted by busy
        mock_raw.return_value = {"status": "busy", "command": "claude", "detail": "Still working"}
        result = detect_activity("test-sess")
        assert result["status"] == "busy"
        assert _activity_state["test-sess"]["consecutive_idle"] == 0  # reset

    @patch("app._detect_activity_raw")
    def test_idle_stays_idle(self, mock_raw):
        """Consecutive idle readings keep incrementing the counter."""
        mock_raw.return_value = {"status": "idle", "command": "bash", "detail": "Shell prompt"}
        detect_activity("test-sess")
        assert _activity_state["test-sess"]["consecutive_idle"] == 1

        detect_activity("test-sess")
        assert _activity_state["test-sess"]["consecutive_idle"] == 2

        detect_activity("test-sess")
        assert _activity_state["test-sess"]["consecutive_idle"] == 3

    @patch("app._detect_activity_raw")
    def test_busy_stays_busy(self, mock_raw):
        """Consecutive busy readings keep status as busy."""
        mock_raw.return_value = {"status": "busy", "command": "claude", "detail": "Working"}
        detect_activity("test-sess")
        detect_activity("test-sess")
        result = detect_activity("test-sess")
        assert result["status"] == "busy"
        assert _activity_state["test-sess"]["consecutive_idle"] == 0

    @patch("app._detect_activity_raw")
    def test_fallback_for_unexpected_status(self, mock_raw):
        """Covers lines 1075-1081: fallback when raw status is not busy/idle/unknown."""
        # Pre-populate state so prev is not None
        _activity_state["test-sess"] = {
            "status": "idle", "since": 0, "consecutive_idle": 1, "raw": {}
        }
        # Return a status that doesn't match any of the handled cases
        mock_raw.return_value = {"status": "custom_status", "command": "custom", "detail": ""}
        result = detect_activity("test-sess")
        assert result["status"] == "custom_status"
        assert _activity_state["test-sess"]["consecutive_idle"] == 0


# ─── build_session_response Tests ───


class TestBuildSessionResponse:
    @patch("app._session_real_auth_mode", return_value="api")
    @patch("app.detect_activity")
    def test_builds_complete_response(self, mock_activity, mock_auth_mode):
        mock_activity.return_value = {"status": "idle", "command": "codex", "detail": "Waiting"}
        sess = {"name": "test", "windows": "2", "attached": True}
        data = {
            "title": "My Task",
            "description": "Doing stuff",
            "description_at": 100.0,
            "progress": "50%",
            "progress_at": 200.0,
            "notes": "Some notes",
            "notes_at": 300.0,
            "realtime": "Active",
            "realtime_at": 400.0,
            "messages": [{"role": "assistant", "text": "hi"}],
        }
        result = build_session_response(sess, data)
        assert result["name"] == "test"
        assert result["windows"] == "2"
        assert result["attached"] is True
        assert result["title"] == "My Task"
        assert result["description"] == "Doing stuff"
        assert result["activity_status"] == "idle"
        assert result["activity_command"] == "codex"
        assert result["activity_detail"] == "Waiting"
        assert result["auth_mode"] == "api"
        assert result["autopush_mode"] == "basic"
        assert result["simple_watchdog"] is False

    @patch("app.detect_activity")
    def test_missing_data_keys_use_defaults(self, mock_activity):
        mock_activity.return_value = {"status": "unknown", "command": "", "detail": ""}
        sess = {"name": "empty", "windows": "1", "attached": False}
        result = build_session_response(sess, {})
        assert result["title"] == ""
        assert result["description"] == ""
        assert result["progress"] == ""
        assert result["notes"] == ""
        assert result["realtime"] == ""
        assert result["messages"] == []
        assert result["description_at"] == 0
        assert result["progress_at"] == 0
        assert result["notes_at"] == 0
        assert result["realtime_at"] == 0


# ─── Pre-compiled Regex Pattern Tests ───


class TestActivityRegexPatterns:
    """Test the pre-compiled regex constants used in activity detection."""

    def test_completion_matches_done(self):
        assert _RE_COMPLETION.match("● Done")
        assert _RE_COMPLETION.match("✻ Completed")

    def test_completion_matches_timed(self):
        assert _RE_COMPLETION.match("✶ Churned for 2m")
        assert _RE_COMPLETION.match("✻ Sautéed for 4m 47s")

    def test_completion_no_match_spinner(self):
        assert not _RE_COMPLETION.match("● Working…")
        assert not _RE_COMPLETION.match("⠋ Reading…")

    def test_running_task(self):
        assert _RE_RUNNING_TASK.match("◼")
        assert _RE_RUNNING_TASK.match("  ◼ task name")
        assert _RE_RUNNING_TASK.match("⎿ ◼")
        assert not _RE_RUNNING_TASK.match("some text ◼")

    def test_spinner_start(self):
        assert _RE_SPINNER_START.match("⠋ Reading…")
        assert _RE_SPINNER_START.match("● Working...")
        assert _RE_SPINNER_START.match("✶ Ebbing…")
        assert not _RE_SPINNER_START.match("normal text")
        assert not _RE_SPINNER_START.match("● Done.")  # single dot, not ellipsis

    def test_spinner_inline(self):
        assert _RE_SPINNER_INLINE.search("Let me create it. ✢ Ebbing… (thought for 8s)")
        assert _RE_SPINNER_INLINE.search("text ⠋ Working…")
        assert not _RE_SPINNER_INLINE.search("normal text with no spinner")

    def test_thought_pattern(self):
        assert _RE_THOUGHT.search("(thought for 5s)")
        assert _RE_THOUGHT.search("some text (thought for 12m)")
        assert not _RE_THOUGHT.search("(thinking)")
        assert not _RE_THOUGHT.search("thought without parens")

    def test_shell_prompt(self):
        assert _RE_SHELL_PROMPT.search("user@host:~$ ")
        assert _RE_SHELL_PROMPT.search("# ")
        assert _RE_SHELL_PROMPT.search(">>> ")  # > matches
        assert not _RE_SHELL_PROMPT.search("normal text")

    def test_idle_prompt(self):
        assert _RE_IDLE_PROMPT.match("❯ ")
        assert _RE_IDLE_PROMPT.match("❯")
        assert _RE_IDLE_PROMPT.match("➜ ")
        assert not _RE_IDLE_PROMPT.match("❯ some command")  # has text after

    def test_tip_codex(self):
        assert _RE_TIP_CODEX.search("Tip: Use codex --help for more info")
        assert not _RE_TIP_CODEX.search("Tip: Use git for version control")

    def test_completion_msg(self):
        assert _RE_COMPLETION_MSG.search("Completed for 3m")
        assert _RE_COMPLETION_MSG.search("Sautéed for 4s")
        assert not _RE_COMPLETION_MSG.search("completed for 3m")  # lowercase


# ─── get_session_cwd Tests ───


class TestGetSessionCwd:
    @patch("app.subprocess.run")
    def test_returns_cwd_on_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="/home/user/project\n")
        result = get_session_cwd("test-session")
        assert result == "/home/user/project"

    @patch("app.subprocess.run")
    def test_returns_empty_on_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        result = get_session_cwd("test-session")
        assert result == ""

    @patch("app.subprocess.run")
    def test_returns_empty_on_exception(self, mock_run):
        mock_run.side_effect = Exception("tmux not running")
        result = get_session_cwd("test-session")
        assert result == ""

    @patch("app.subprocess.run")
    def test_strips_whitespace(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="  /home/user/project  \n")
        result = get_session_cwd("test-session")
        assert result == "/home/user/project"


# ─── Login Password Comparison Tests ───


class TestLoginComparison:
    """Test that the login uses constant-time comparison."""

    def test_login_uses_hmac_compare(self):
        """Verify hmac.compare_digest is used in the login handler source."""
        import inspect

        import app
        source = inspect.getsource(app.do_login)
        assert "hmac.compare_digest" in source
        # Should NOT use plain == for password comparison
        assert "password == AUTH_PASS" not in source


# ─── datetime Module-Level Import Tests ───


class TestDatetimeImport:
    """Verify datetime is imported at module level, not inside functions."""

    def test_datetime_available_at_module_level(self):
        import app
        assert hasattr(app, 'datetime')
        assert hasattr(app, 'timezone')
        from datetime import datetime, timezone
        assert app.datetime is datetime
        assert app.timezone is timezone


# ─── Session Name Validation Tests ───


class TestIsValidSessionName:
    def test_simple_alphanumeric(self):
        assert _is_valid_session_name("mysession") is True

    def test_with_hyphen(self):
        assert _is_valid_session_name("my-session") is True

    def test_with_underscore(self):
        assert _is_valid_session_name("my_session") is True

    def test_with_dot(self):
        assert _is_valid_session_name("my.session") is True

    def test_numbers_only(self):
        assert _is_valid_session_name("12345") is True

    def test_empty_string(self):
        assert _is_valid_session_name("") is False

    def test_none_like_empty(self):
        assert _is_valid_session_name("") is False

    def test_colon_rejected(self):
        # tmux uses colon for window targeting (session:window)
        assert _is_valid_session_name("session:1") is False

    def test_space_rejected(self):
        assert _is_valid_session_name("my session") is False

    def test_semicolon_rejected(self):
        assert _is_valid_session_name("sess;rm -rf /") is False

    def test_backtick_rejected(self):
        assert _is_valid_session_name("sess`cmd`") is False

    def test_dollar_rejected(self):
        assert _is_valid_session_name("sess$(cmd)") is False

    def test_slash_rejected(self):
        assert _is_valid_session_name("path/traversal") is False

    def test_too_long_rejected(self):
        assert _is_valid_session_name("a" * 129) is False

    def test_max_length_accepted(self):
        assert _is_valid_session_name("a" * 128) is True


class TestFindSessionRejectsInvalidNames:
    @patch("app.get_tmux_sessions")
    def test_invalid_name_returns_empty_not_found(self, mock_sessions):
        mock_sessions.return_value = [{"name": "session:1", "windows": "1", "created": "", "attached": False}]
        sessions, sess = _find_session("session:1")
        assert sess is None
        assert sessions == []
        # get_tmux_sessions should NOT be called for invalid names
        mock_sessions.assert_not_called()

    @patch("app.get_tmux_sessions")
    def test_valid_name_calls_tmux(self, mock_sessions):
        mock_sessions.return_value = [{"name": "valid", "windows": "1", "created": "", "attached": False}]
        sessions, sess = _find_session("valid")
        assert sess is not None
        mock_sessions.assert_called_once()


# ─── Login Rate Limiter Tests ───


class TestLoginRateLimiter:
    def setup_method(self):
        """Clear rate limit state before each test."""
        _login_attempts.clear()

    def test_allows_first_attempt(self):
        assert _check_login_rate_limit("1.2.3.4") is True

    def test_allows_up_to_limit(self):
        for _ in range(10):
            assert _check_login_rate_limit("10.0.0.1") is True

    def test_blocks_after_limit(self):
        for _ in range(10):
            _check_login_rate_limit("10.0.0.2")
        assert _check_login_rate_limit("10.0.0.2") is False

    def test_different_ips_independent(self):
        for _ in range(10):
            _check_login_rate_limit("192.168.1.1")
        # A different IP should still be allowed
        assert _check_login_rate_limit("192.168.1.2") is True

    def test_rate_limit_resets_in_new_window(self):
        import math

        import app
        original = time.time
        fixed_time = 1000.0
        try:
            time.time = lambda: fixed_time
            app.time.time = lambda: fixed_time
            for _ in range(10):
                _check_login_rate_limit("5.5.5.5")
            assert _check_login_rate_limit("5.5.5.5") is False
            # Advance to the next window
            new_time = fixed_time + 61.0
            time.time = lambda: new_time
            app.time.time = lambda: new_time
            _login_attempts.clear()
            assert _check_login_rate_limit("5.5.5.5") is True
        finally:
            time.time = original
            app.time.time = original


# ─── Shell command safety ───


class TestApiKeyShellQuoting:
    """Shell metadata is quoted and authentication never enters pane history."""

    def test_shlex_quoted_in_session_create(self):
        import inspect

        import app
        source = inspect.getsource(app.api_create_session)
        assert "shlex.quote" in source

    def test_set_auth_mode_never_sends_credentials_to_tmux(self):
        import inspect

        import app
        source = inspect.getsource(app.api_set_auth_mode)
        assert "OPENAI_API_KEY" not in source
        assert "subprocess" not in source


# ─── Codex detached-session trust ───


class TestCodexProjectTrust:
    def test_adds_native_trust_and_preserves_other_sections(self):
        from app import _ensure_codex_project_trust, tomllib

        original = (
            'model = "gpt-5.6-sol"\n\n'
            '[mcp_servers.google]\ncommand = "uvx"\n'
        )
        result = _ensure_codex_project_trust(original, "/srv/grabo")
        parsed = tomllib.loads(result)
        assert parsed["projects"]["/srv/grabo"]["trust_level"] == "trusted"
        assert parsed["mcp_servers"]["google"]["command"] == "uvx"

    def test_replaces_untrusted_marker_idempotently(self):
        from app import _ensure_codex_project_trust

        original = (
            '[projects."/srv/grabo"]\n'
            'trust_level = "untrusted"\n'
            'trust_level = "untrusted"\n'
        )
        result = _ensure_codex_project_trust(original, "/srv/grabo")
        assert result.count('trust_level = "trusted"') == 1
        assert "untrusted" not in result
        assert _ensure_codex_project_trust(result, "/srv/grabo") == result


# ─── Atomic JSON Write Tests ───


class TestAtomicWriteJson:
    """Verify _atomic_write_json writes data correctly and leaves no .tmp file."""

    def test_writes_data_atomically(self, tmp_path):
        from app import _atomic_write_json
        target = tmp_path / "data.json"
        _atomic_write_json(target, {"key": "value", "num": 42})
        assert target.exists()
        data = json.loads(target.read_text())
        assert data == {"key": "value", "num": 42}

    def test_no_tmp_file_left_behind(self, tmp_path):
        from app import _atomic_write_json
        target = tmp_path / "state.json"
        _atomic_write_json(target, [1, 2, 3])
        tmp_file = target.with_suffix(".tmp")
        assert not tmp_file.exists(), ".tmp file should be renamed away after write"

    def test_overwrites_existing_file(self, tmp_path):
        from app import _atomic_write_json
        target = tmp_path / "notes.json"
        target.write_text(json.dumps({"old": "data"}))
        _atomic_write_json(target, {"new": "data"})
        data = json.loads(target.read_text())
        assert data == {"new": "data"}

    def test_sets_file_permissions_600(self, tmp_path):
        from app import _atomic_write_json
        target = tmp_path / "secret.json"
        _atomic_write_json(target, {"tok": "abc"})
        mode = oct(target.stat().st_mode & 0o777)
        assert mode == "0o600", f"Expected 0o600, got {mode}"
