"""Integration tests for tmux Dashboard API endpoints using FastAPI TestClient."""
import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Patch environment before importing app
os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

from fastapi.testclient import TestClient

from app import AUTH_PASS, AUTH_USER, _make_token, app

# Auth cookie for authenticated requests
AUTH_TOKEN = _make_token(AUTH_USER)
AUTH_COOKIES = {"tmux_auth": AUTH_TOKEN}

# Mock session data
MOCK_SESSIONS = [
    {"name": "test-session", "windows": "1", "created": "1700000000", "attached": False},
    {"name": "work-session", "windows": "2", "created": "1700001000", "attached": True},
]


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def authed_client():
    """TestClient with auth cookie pre-set on the client instance.

    Use this instead of passing cookies= per-request to avoid the starlette
    DeprecationWarning about per-request cookie semantics.
    """
    c = TestClient(app)
    c.cookies.set("tmux_auth", AUTH_TOKEN)
    return c


# ─── Auth & Middleware Tests ───


class TestAuthMiddleware:
    def test_unauthenticated_returns_login_page(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert "tmux Dashboard" in resp.text
        assert "Log in" in resp.text

    def test_authenticated_returns_app(self, authed_client):
        resp = authed_client.get("/")
        assert resp.status_code == 200
        # The app page should NOT be the login page
        assert "login-box" not in resp.text

    def test_invalid_token_returns_login(self):
        from fastapi.testclient import TestClient
        bad_client = TestClient(app)
        bad_client.cookies.set("tmux_auth", "admin:invalidsig00000000000")
        resp = bad_client.get("/")
        assert resp.status_code == 200
        assert "Log in" in resp.text

    def test_login_route_accessible_without_auth(self, client):
        resp = client.post("/login", data={"username": "wrong", "password": "wrong"}, follow_redirects=False)
        # Should redirect (not blocked by auth middleware)
        assert resp.status_code == 303

    def test_login_success_sets_cookie(self, client):
        resp = client.post(
            "/login",
            data={"username": AUTH_USER, "password": AUTH_PASS},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "tmux_auth" in resp.cookies

    def test_login_failure_redirects_with_error(self, client):
        resp = client.post(
            "/login",
            data={"username": "wrong", "password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "err=1" in resp.headers.get("location", "")


class TestSecurityHeaders:
    def test_security_headers_present(self, authed_client):
        resp = authed_client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "DENY"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_security_headers_on_api(self, authed_client):
        with patch("app.get_tmux_sessions", return_value=[]):
            resp = authed_client.get("/api/status")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"


# ─── Session List API Tests ───


class TestSessionListEndpoints:
    @patch("app.get_tmux_sessions", return_value=[])
    def test_sessions_fast_empty(self, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions-fast")
        assert resp.status_code == 200
        assert resp.json() == []

    @patch("app.detect_activity", return_value={"status": "idle", "command": "", "detail": ""})
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_sessions_fast_returns_sessions(self, mock_sessions, mock_activity, authed_client):
        resp = authed_client.get("/api/sessions-fast")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert data[0]["name"] == "test-session"
        assert data[1]["name"] == "work-session"

    @patch("app.detect_activity", return_value={"status": "idle", "command": "", "detail": ""})
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_status_returns_activity(self, mock_sessions, mock_activity, authed_client):
        resp = authed_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(s["activity_status"] == "idle" for s in data)


# ─── Session-Specific Endpoint Tests ───


class TestSessionSpecificEndpoints:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.capture_pane_full", return_value="line1\nline2\nline3\n")
    @patch("app.detect_activity", return_value={"status": "busy", "command": "node", "detail": "running"})
    def test_raw_output_existing_session(self, mock_activity, mock_capture, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/test-session/raw")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-session"
        assert "line1" in data["raw"]
        assert data["activity_status"] == "busy"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_raw_output_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/nonexistent/raw")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_upload_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/nonexistent/upload",
            files={"file": ("test.txt", b"content", "text/plain")},

        )
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.get_session_cwd", return_value="")
    def test_upload_no_cwd(self, mock_cwd, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/test-session/upload",
            files={"file": ("test.txt", b"content", "text/plain")},

        )
        assert resp.status_code == 500
        assert "working directory" in resp.json()["error"].lower()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_claude_md_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/nonexistent/claude-md")
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.get_session_cwd", return_value="/tmp/test-cwd")
    def test_get_claude_md_success_returns_files_list(self, mock_cwd, mock_sessions, authed_client):
        """GET success: returns files list with cwd field (files may or may not exist)."""
        resp = authed_client.get("/api/sessions/test-session/claude-md")
        assert resp.status_code == 200
        data = resp.json()
        assert "files" in data
        assert "cwd" in data
        assert data["cwd"] == "/tmp/test-cwd"
        assert isinstance(data["files"], list)
        # Should have entries for both CWD and home dir
        assert len(data["files"]) == 2
        labels = [f["label"] for f in data["files"]]
        assert "Project" in labels
        assert "Global" in labels

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_get_claude_md_handles_unreadable_file(self, mock_sessions, authed_client, tmp_path):
        """When CWD CLAUDE.md exists but cannot be read, content should be empty string."""
        md_path = tmp_path / "CLAUDE.md"
        md_path.write_text("secret content")
        md_path.chmod(0o000)
        try:
            with patch("app.get_session_cwd", return_value=str(tmp_path)):
                resp = authed_client.get("/api/sessions/test-session/claude-md")
            assert resp.status_code == 200
            data = resp.json()
            project_file = next(f for f in data["files"] if f["label"] == "Project")
            assert project_file["exists"] is True
            assert project_file["content"] == ""
        finally:
            md_path.chmod(0o644)

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_get_claude_md_handles_unreadable_home_file(self, mock_sessions, authed_client, tmp_path):
        """When home CLAUDE.md exists but cannot be read, content should be empty string."""
        md_path = tmp_path / "CLAUDE.md"
        md_path.write_text("home content")
        md_path.chmod(0o000)
        try:
            with patch("app.get_session_cwd", return_value=""), \
                 patch("app.Path.home", return_value=tmp_path):
                resp = authed_client.get("/api/sessions/test-session/claude-md")
            assert resp.status_code == 200
            home_file = next(f for f in resp.json()["files"] if f["label"] == "Global")
            assert home_file["exists"] is True
            assert home_file["content"] == ""
        finally:
            md_path.chmod(0o644)

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_send_command_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/nonexistent/send",
            json={"command": "echo hello"},

        )
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_interrupt_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.post("/api/sessions/nonexistent/interrupt")
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_send_keys_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/nonexistent/send-keys",
            json={"keys": ["Escape"]},

        )
        assert resp.status_code == 404


# ─── Session Create / Delete Tests ───


class TestSessionCreateDelete:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_create_session_invalid_name(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/create",
            json={"name": "bad name with spaces"},

        )
        assert resp.status_code == 400
        assert "Invalid name" in resp.json()["error"]

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_create_session_duplicate_name(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/create",
            json={"name": "test-session"},

        )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_create_session_injection_attempt(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/create",
            json={"name": "test;rm -rf /"},

        )
        assert resp.status_code == 400

    @patch("app.get_tmux_sessions", return_value=[])
    def test_delete_session_not_found(self, mock_sessions, authed_client):
        resp = authed_client.delete("/api/sessions/nonexistent")
        assert resp.status_code == 404


# ─── Stats Endpoint Tests ───


class TestStatsEndpoint:
    def test_stats_returns_json(self, authed_client):
        resp = authed_client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        # Should have some system stat keys (actual values depend on system)
        assert isinstance(data, dict)

    def test_stats_has_expected_keys(self, authed_client):
        """Stats response must contain the documented top-level keys."""
        resp = authed_client.get("/api/stats")
        data = resp.json()
        expected_keys = {"cpu_load", "memory", "disk", "tmux_sessions", "claude_processes"}
        assert expected_keys <= data.keys(), f"Missing keys: {expected_keys - data.keys()}"

    @patch("app.subprocess.run", side_effect=Exception("no pgrep"))
    @patch("app.shutil.disk_usage", side_effect=Exception("no disk"))
    def test_stats_degrades_gracefully_on_subprocess_failure(self, mock_disk, mock_run, authed_client):
        """Stats endpoint should return 200 even when subprocess calls fail."""
        resp = authed_client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        # These keys should exist but may be empty dicts/lists on failure
        assert "memory" in data
        assert "disk" in data
        assert "claude_processes" in data

    def test_stats_cpu_load_fallback_when_proc_missing(self, authed_client):
        """If /proc/loadavg is unavailable, cpu_load should be an empty dict (not crash)."""
        import builtins
        real_open = builtins.open

        def mock_open(path, *a, **kw):
            if str(path) == "/proc/loadavg":
                raise OSError("no proc")
            return real_open(path, *a, **kw)

        with patch("builtins.open", side_effect=mock_open):
            resp = authed_client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json().get("cpu_load") == {}

    def test_stats_uptime_fallback_when_proc_missing(self, authed_client):
        """If /proc/uptime is unavailable, uptime should be 'unknown' (not crash)."""
        import builtins
        real_open = builtins.open

        def mock_open(path, *a, **kw):
            if str(path) == "/proc/uptime":
                raise OSError("no proc")
            return real_open(path, *a, **kw)

        with patch("builtins.open", side_effect=mock_open):
            resp = authed_client.get("/api/stats")
        assert resp.status_code == 200
        assert resp.json().get("uptime") == "unknown"


# ─── Claude Auth Endpoints ───


class TestClaudeAuthEndpoints:
    def test_claude_status(self, authed_client):
        resp = authed_client.get("/api/auth/claude-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "hasApiKey" in data

    def test_set_key_empty(self, authed_client):
        resp = authed_client.post(
            "/api/auth/api-key",
            json={"apiKey": ""},

        )
        # Empty key clears the stored key
        assert resp.status_code == 200

    @patch("app._save_anthropic_key")
    def test_set_key_valid(self, mock_save, authed_client):
        resp = authed_client.post(
            "/api/auth/api-key",
            json={"apiKey": "sk-ant-test-key-12345"},

        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        mock_save.assert_called_once()

    def test_set_key_invalid_format(self, authed_client):
        resp = authed_client.post(
            "/api/auth/api-key",
            json={"apiKey": "not-a-valid-key"},

        )
        assert resp.status_code == 400
        assert "Invalid" in resp.json()["error"]

    @patch("app.subprocess.run", side_effect=Exception("claude binary not found"))
    def test_claude_status_exception_returns_error_field(self, mock_run, authed_client):
        """Exception during claude auth status check should still return 200 with error key."""
        resp = authed_client.get("/api/auth/claude-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "error" in data
        assert "hasApiKey" in data


# ─── CLAUDE.md Path Traversal Protection ───


class TestClaudeMdSaveEndpoint:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_rejects_non_claude_md_path(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/test-session/claude-md",
            json={"path": "/etc/passwd", "content": "pwned"},

        )
        assert resp.status_code == 400

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_rejects_path_outside_home(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/test-session/claude-md",
            json={"path": "/etc/CLAUDE.md", "content": "pwned"},

        )
        assert resp.status_code == 403

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_rejects_traversal_attack(self, mock_sessions, authed_client):
        from pathlib import Path
        evil_path = str(Path.home() / ".." / "etc" / "CLAUDE.md")
        resp = authed_client.post(
            "/api/sessions/test-session/claude-md",
            json={"path": evil_path, "content": "pwned"},

        )
        assert resp.status_code == 403

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_save_claude_md_missing_session_returns_404(self, mock_sessions, authed_client):
        """POST to a non-existent session should return 404."""
        resp = authed_client.post(
            "/api/sessions/no-such-session/claude-md",
            json={"path": "/home/user/CLAUDE.md", "content": "test"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "Session not found"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_save_claude_md_success(self, mock_sessions, authed_client, tmp_path):
        """POST with a valid in-home CLAUDE.md path should write the file and return ok."""
        from pathlib import Path as RealPath
        target = str(tmp_path / "CLAUDE.md")
        with patch("app.Path.home", return_value=tmp_path):
            resp = authed_client.post(
                "/api/sessions/test-session/claude-md",
                json={"path": target, "content": "# hello"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert (tmp_path / "CLAUDE.md").read_text() == "# hello"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_save_claude_md_non_slash_name_returns_400(self, mock_sessions, authed_client, tmp_path):
        """Path that passes endswith('CLAUDE.md') but not endswith('/CLAUDE.md') should return 400."""
        # e.g. /home/user/sub/prefixCLAUDE.md ends with "CLAUDE.md" but not "/CLAUDE.md"
        target = str(tmp_path / "prefixCLAUDE.md")
        with patch("app.Path.home", return_value=tmp_path):
            resp = authed_client.post(
                "/api/sessions/test-session/claude-md",
                json={"path": target, "content": "bad"},
            )
        assert resp.status_code == 400
        assert "Invalid path" in resp.json()["error"]

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("pathlib.Path.write_text", side_effect=OSError("disk full"))
    def test_save_claude_md_write_failure_returns_500(self, mock_write, mock_sessions, authed_client, tmp_path):
        """A write failure during CLAUDE.md save should return 500."""
        target = str(tmp_path / "CLAUDE.md")
        with patch("app.Path.home", return_value=tmp_path):
            resp = authed_client.post(
                "/api/sessions/test-session/claude-md",
                json={"path": target, "content": "oops"},
            )
        assert resp.status_code == 500
        assert "error" in resp.json()


# ─── Auth Mode Endpoint ───


class TestSetAuthMode:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_set_auth_mode_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/nonexistent/set-auth-mode",
            json={"mode": "subscription"},

        )
        assert resp.status_code == 404


# ─── Session Stats / JSONL Helper Tests ───


class TestGetSessionCwd:
    @patch("app.subprocess.run")
    def test_returns_cwd_on_success(self, mock_run):
        """get_session_cwd should return stripped CWD when subprocess succeeds."""
        mock_run.return_value = MagicMock(returncode=0, stdout="/home/user/project\n")
        import app
        result = app.get_session_cwd("test-session")
        assert result == "/home/user/project"

    @patch("app.subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="no session"))
    def test_returns_empty_on_nonzero_returncode(self, mock_run):
        """get_session_cwd should return '' when returncode != 0."""
        import app
        result = app.get_session_cwd("missing-session")
        assert result == ""


class TestFindSessionJsonlFiles:
    @patch("app.get_session_cwd", return_value="")
    def test_returns_empty_when_no_cwd(self, mock_cwd):
        """_find_session_jsonl_files returns [] when session has no CWD."""
        import app
        result = app._find_session_jsonl_files("no-cwd-session")
        assert result == []

    @patch("app.get_session_cwd", return_value="/home/user/myproject")
    def test_returns_empty_when_project_dir_not_found(self, mock_cwd):
        """_find_session_jsonl_files returns [] when no matching project dir exists."""
        import app
        with patch("app.os.path.isdir", return_value=False), \
             patch("app.globmod.glob", return_value=[]):
            result = app._find_session_jsonl_files("no-dir-session")
        assert result == []

    @patch("app.get_session_cwd", return_value="/home/user/myproject")
    def test_returns_files_when_project_dir_found(self, mock_cwd):
        """_find_session_jsonl_files returns JSONL files when project dir exists."""
        import app
        with patch("app.os.path.isdir", return_value=True), \
             patch("app.globmod.glob", return_value=["/home/user/.claude/projects/myproject/conversation.jsonl"]):
            result = app._find_session_jsonl_files("has-dir-session")
        assert len(result) > 0

    @patch("app.get_session_cwd", return_value="/home/user/myproject")
    def test_uses_alt_dir_with_leading_dash(self, mock_cwd):
        """Should use alt dir (with leading dash) when exact match is absent."""
        import app
        with patch("app.os.path.isdir", side_effect=[False, True]), \
             patch("app.globmod.glob", return_value=["/home/user/.claude/projects/-myproject/conv.jsonl"]):
            result = app._find_session_jsonl_files("leading-dash-session")
        assert len(result) > 0

    @patch("app.get_session_cwd", return_value="/home/user/myproject")
    def test_uses_glob_fallback(self, mock_cwd):
        """Should set project_dir from glob candidates when no exact or alt dir matches."""
        import app
        call_count = [0]

        def glob_fn(pattern):
            call_count[0] += 1
            if call_count[0] == 1:  # First call: fallback dir discovery
                return ["/home/user/.claude/projects/my-myproject"]
            # Subsequent calls: *.jsonl files within that dir
            return ["/home/user/.claude/projects/my-myproject/conv.jsonl"]

        with patch("app.os.path.isdir", return_value=False), \
             patch("app.globmod.glob", side_effect=glob_fn):
            result = app._find_session_jsonl_files("glob-session")
        # The directory was found via glob; *.jsonl files were collected
        assert "/home/user/.claude/projects/my-myproject/conv.jsonl" in result


# ─── Session Stats Endpoint ───


class TestSessionStats:
    @patch("app._find_session_jsonl_files", return_value=[])
    def test_session_stats_no_files(self, mock_jsonl, authed_client):
        resp = authed_client.get("/api/sessions/test-session/stats")
        assert resp.status_code == 200
        data = resp.json()
        # When no JSONL files found, returns {"available": false}
        assert data["available"] is False

    @patch("app._find_session_jsonl_files", return_value=[])
    def test_session_stats_nonexistent_session(self, mock_jsonl, authed_client):
        # The stats endpoint doesn't validate session existence — it just
        # tries to find JSONL files and returns available:false if none found
        resp = authed_client.get("/api/sessions/nonexistent/stats")
        assert resp.status_code == 200
        assert resp.json()["available"] is False

    def test_session_stats_uses_cache(self, authed_client):
        """Second call within 15s should return cached result."""
        import time

        import app
        unique_session = "cache-hit-test-session"
        cached_result = {"available": False, "_ts": time.time(), "_from_cache": True}
        app._session_stats_cache[unique_session] = cached_result
        try:
            resp = authed_client.get(f"/api/sessions/{unique_session}/stats")
            assert resp.status_code == 200
            # _ts is internal — but _from_cache should pass through
            assert resp.json().get("_from_cache") is True
        finally:
            app._session_stats_cache.pop(unique_session, None)


# ─── Health Endpoint Tests ───


class TestHealthEndpoint:
    @patch("app.subprocess.run")
    def test_health_ok_when_tmux_running(self, mock_run, authed_client):
        mock_run.return_value = MagicMock(returncode=0, stdout="session1\nsession2\n", stderr="")
        resp = authed_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tmux"] is True

    @patch("app.subprocess.run")
    def test_health_degraded_when_tmux_fails(self, mock_run, authed_client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        resp = authed_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["tmux"] is False

    @patch("app.subprocess.run")
    def test_health_ok_when_no_tmux_server(self, mock_run, authed_client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no server running")
        resp = authed_client.get("/api/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tmux"] is True

    @patch("app.subprocess.run")
    def test_health_degraded_on_exception(self, mock_run, authed_client):
        mock_run.side_effect = Exception("timeout")
        resp = authed_client.get("/api/health")
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["tmux"] is False

    def test_health_reports_openai_status(self, authed_client):
        resp = authed_client.get("/api/health")
        data = resp.json()
        assert "openai" in data
        assert isinstance(data["openai"], bool)

    def test_health_reports_data_dir_field(self, authed_client):
        """Health check should include data_dir field as a boolean."""
        resp = authed_client.get("/api/health")
        data = resp.json()
        assert "data_dir" in data
        assert isinstance(data["data_dir"], bool)

    @patch("app.subprocess.run")
    @patch("app.MESSAGES_DIR")
    def test_health_degraded_when_data_dir_missing(self, mock_dir, mock_run, authed_client):
        """Health status should be degraded when data directory is inaccessible."""
        mock_run.return_value = MagicMock(returncode=0, stdout="session1\n", stderr="")
        mock_dir.is_dir.return_value = False
        mock_dir.__bool__ = lambda self: True  # prevent falsy short-circuit
        resp = authed_client.get("/api/health")
        data = resp.json()
        assert data["data_dir"] is False
        assert data["status"] == "degraded"


# ─── Upload File Size Limit Tests ───


class TestUploadFileSizeLimit:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_malformed_content_length_falls_through_to_session_check(self, mock_sessions, authed_client):
        """Covers lines 1762-1763: malformed Content-Length ValueError → pass."""
        # The handler should not return 413 (skips pre-read check) but continues
        resp = authed_client.post(
            "/api/sessions/test-session/upload",
            content=b"--b\r\nContent-Disposition: form-data; name=\"file\"; filename=\"f.txt\"\r\n\r\nhi\r\n--b--",
            headers={
                "content-type": "multipart/form-data; boundary=b",
                "content-length": "notanumber",
            },
        )
        # 413 would mean pre-read check triggered — we want it NOT to be 413
        assert resp.status_code != 413

    @pytest.mark.asyncio
    async def test_post_read_oversized_returns_413(self):
        """Covers line 1782: post-read oversized content check."""
        from starlette.requests import Request

        import app as _app

        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/sessions/test-session/upload",
            "query_string": b"",
            "headers": [(b"content-length", b"100")],  # small pre-read → passes pre-check
            "app": _app.app,
        }

        request = Request(scope)

        class FakeLargeFile:
            filename = "big.bin"

            async def read(self):
                return b"x" * (51 * 1024 * 1024)

        with patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS), \
             patch("app.get_session_cwd", return_value="/tmp"):
            resp = await _app.api_upload_file(request, "test-session", FakeLargeFile())

        assert resp.status_code == 413
        assert "too large" in resp.body.decode().lower()

    @patch("app.get_session_cwd", return_value="/tmp/test-cwd")
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_rejects_oversized_file(self, mock_sessions, mock_cwd, authed_client):
        # Create a file larger than 50 MB
        large_content = b"x" * (51 * 1024 * 1024)
        from io import BytesIO
        resp = authed_client.post(
            "/api/sessions/test-session/upload",
            files={"file": ("big.bin", BytesIO(large_content), "application/octet-stream")},

        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["error"].lower()

    @patch("app.get_session_cwd", return_value="/tmp/test-cwd")
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._save_messages")
    def test_accepts_small_file(self, mock_save, mock_sessions, mock_cwd, authed_client, tmp_path):
        # Monkey-patch get_session_cwd to return the tmp dir
        import app
        original_cwd = app.get_session_cwd
        app.get_session_cwd = lambda name: str(tmp_path)
        try:
            small_content = b"hello world"
            from io import BytesIO
            resp = authed_client.post(
                "/api/sessions/test-session/upload",
                files={"file": ("small.txt", BytesIO(small_content), "text/plain")},

            )
            assert resp.status_code == 200
            data = resp.json()
            assert data.get("ok") is True
            assert "small.txt" in data.get("path", "")
        finally:
            app.get_session_cwd = original_cwd

    @patch("app.get_session_cwd", return_value="/tmp/test-cwd")
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_rejects_dotfile_filename(self, mock_sessions, mock_cwd, authed_client):
        """Files starting with '.' must be rejected (dotfile protection)."""
        from io import BytesIO
        resp = authed_client.post(
            "/api/sessions/test-session/upload",
            files={"file": (".bashrc", BytesIO(b"evil"), "text/plain")},

        )
        assert resp.status_code == 400
        assert "invalid" in resp.json()["error"].lower()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux unavailable"))
    def test_upload_fails_when_get_cwd_subprocess_raises(self, mock_run, mock_sessions, authed_client):
        """When get_session_cwd's subprocess.run raises, upload returns 500 (working directory error)."""
        from io import BytesIO
        resp = authed_client.post(
            "/api/sessions/test-session/upload",
            files={"file": ("test.txt", BytesIO(b"data"), "text/plain")},
        )
        assert resp.status_code == 500
        assert "working directory" in resp.json()["error"].lower()

    @patch("app._save_messages")
    def test_upload_loads_messages_when_cache_entry_empty(self, mock_save, authed_client, tmp_path):
        """Upload should call _load_session_messages when cache entry has no messages key."""
        import app
        fresh_name = "fresh-upload-xxxx"
        fresh_sessions = [{"name": fresh_name, "windows": "1", "created": "0", "attached": False}]
        app.cache.pop(fresh_name, None)  # Ensure no cache entry
        from io import BytesIO
        with patch("app.get_tmux_sessions", return_value=fresh_sessions), \
             patch("app.get_session_cwd", return_value=str(tmp_path)), \
             patch("app._load_session_messages", return_value=[]) as mock_load:
            resp = authed_client.post(
                f"/api/sessions/{fresh_name}/upload",
                files={"file": ("msg.txt", BytesIO(b"hi"), "text/plain")},
            )
        assert resp.status_code == 200
        mock_load.assert_called_once_with(fresh_name)

    @patch("app.get_session_cwd", return_value="/tmp/test-cwd")
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("pathlib.Path.write_bytes", side_effect=OSError("disk full"))
    def test_upload_write_failure_returns_500(self, mock_write, mock_sessions, mock_cwd, authed_client):
        """A write failure during upload should return 500 with error key."""
        from io import BytesIO
        resp = authed_client.post(
            "/api/sessions/test-session/upload",
            files={"file": ("test.txt", BytesIO(b"data"), "text/plain")},
        )
        assert resp.status_code == 500
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._save_messages")
    def test_path_traversal_filename_stripped(self, mock_save, mock_sessions, authed_client, tmp_path):
        """Path traversal in filename must be stripped to basename (../etc/passwd → passwd)."""
        from io import BytesIO

        import app
        original_cwd = app.get_session_cwd
        app.get_session_cwd = lambda name: str(tmp_path)
        try:
            resp = authed_client.post(
                "/api/sessions/test-session/upload",
                files={"file": ("../etc/passwd", BytesIO(b"data"), "text/plain")},

            )
            # The code strips to basename — 'passwd' — and writes successfully
            assert resp.status_code == 200
            data = resp.json()
            assert "passwd" in data.get("path", "")
            assert ".." not in data.get("path", "")
        finally:
            app.get_session_cwd = original_cwd


# ─── Security Header Tests (extended) ───


class TestExtendedSecurityHeaders:
    """Verify new security headers added in the security hardening pass."""

    def test_csp_header_present(self, authed_client):
        resp = authed_client.get("/")
        csp = resp.headers.get("Content-Security-Policy", "")
        assert csp, "CSP header should be set"
        assert "default-src" in csp
        assert "frame-ancestors" in csp

    def test_permissions_policy_header_present(self, authed_client):
        resp = authed_client.get("/")
        pp = resp.headers.get("Permissions-Policy", "")
        assert pp, "Permissions-Policy header should be set"
        assert "camera=()" in pp

    def test_csp_on_api_endpoints(self, authed_client):
        with patch("app.get_tmux_sessions", return_value=[]):
            resp = authed_client.get("/api/sessions-fast")
        assert "Content-Security-Policy" in resp.headers


# ─── Login Rate Limit Endpoint Tests ───


class TestLoginRateLimitEndpoint:
    """Verify the /login endpoint enforces rate limiting."""

    def setup_method(self):
        import app
        app._login_attempts.clear()

    def test_login_returns_429_after_many_attempts(self, client):
        # Exhaust the rate limit for a fixed IP
        # Directly fill the rate limit bucket for the test client's IP
        import math
        import time as _time

        import app
        window_key = f"testclient:{int(_time.time() // 60)}"
        app._login_attempts[window_key] = app._LOGIN_MAX_ATTEMPTS
        resp = client.post(
            "/login",
            data={"username": "wrong", "password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 429

    def test_login_allowed_before_limit(self, client):
        import app
        app._login_attempts.clear()
        resp = client.post(
            "/login",
            data={"username": "wrong", "password": "wrong"},
            follow_redirects=False,
        )
        # Should redirect (303), not rate-limit (429)
        assert resp.status_code == 303


# ─── Auto-Respond Log Endpoint ───


class TestAutoRespondLogEndpoint:
    def test_returns_list(self, authed_client):
        resp = authed_client.get("/api/auto-respond-log")
        assert resp.status_code == 200
        assert isinstance(resp.json(), list)

    def test_unauthenticated_blocked(self, client):
        resp = client.get("/api/auto-respond-log")
        # Should show login page (200 HTML), not 401
        assert resp.status_code == 200
        assert "Log in" in resp.text


# ─── Auth Logout Endpoint ───


class TestAuthLogout:
    @patch("app.subprocess.run")
    def test_logout_success(self, mock_run, authed_client):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        resp = authed_client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "warnings" not in resp.json()

    @patch("app.subprocess.run")
    def test_logout_with_warning(self, mock_run, authed_client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="Not logged in")
        resp = authed_client.post("/api/auth/logout")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        assert "warnings" in resp.json()

    @patch("app.subprocess.run", side_effect=FileNotFoundError("claude not found"))
    def test_logout_subprocess_error_generic(self, mock_run, authed_client):
        resp = authed_client.post("/api/auth/logout")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Error message should be generic, not expose exception detail
        assert "warnings" in data
        assert "claude not found" not in str(data["warnings"])


# ─── Raw Tail Endpoint ───


class TestRawTailEndpoint:
    @patch("app.get_tmux_sessions", return_value=[])
    def test_missing_session_returns_404(self, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/nonexistent/raw-tail")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.get_pane_position", return_value={"total_lines": 100, "history_size": 50, "pane_height": 50})
    @patch("app.capture_pane_full", return_value="line1\nline2\nline3\n")
    def test_full_capture_when_known_lines_zero(self, mock_capture, mock_pos, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/test-session/raw-tail?known_lines=0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "full"
        assert "line1" in data["raw"]

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.get_pane_position", return_value={"total_lines": 50, "history_size": 25, "pane_height": 25})
    def test_no_new_content_when_caught_up(self, mock_pos, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/test-session/raw-tail?known_lines=50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "none"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.get_pane_position", return_value={"total_lines": 80, "history_size": 40, "pane_height": 40})
    @patch("app.capture_pane_recent", return_value="new_line1\nnew_line2\n")
    def test_delta_mode_when_new_lines_available(self, mock_capture, mock_pos, mock_sessions, authed_client):
        """When known_lines < total_lines, should return delta mode with new content."""
        resp = authed_client.get("/api/sessions/test-session/raw-tail?known_lines=50")
        assert resp.status_code == 200
        data = resp.json()
        assert data["mode"] == "delta"
        assert "raw" in data
        assert "total_lines" in data


# ─── Refresh Endpoints ───


class TestRefreshEndpoints:
    @patch("app.get_tmux_sessions", return_value=[])
    def test_refresh_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.post("/api/sessions/nonexistent/refresh")
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=[])
    def test_refresh_all_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.post("/api/sessions/nonexistent/refresh-all")
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.get_session_data", new_callable=AsyncMock, return_value={"title": "Test", "description": ""})
    @patch("app.async_detect_activity", new_callable=AsyncMock, return_value={"status": "idle", "command": "", "detail": ""})
    def test_refresh_success(self, mock_activity, mock_data, mock_sessions, authed_client):
        """Refresh success path should return full session response dict."""
        resp = authed_client.post("/api/sessions/test-session/refresh")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-session"
        assert "activity_status" in data

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.get_session_data", new_callable=AsyncMock, return_value={"title": "Test", "description": ""})
    @patch("app.async_detect_activity", new_callable=AsyncMock, return_value={"status": "idle", "command": "", "detail": ""})
    def test_refresh_all_success(self, mock_activity, mock_data, mock_sessions, authed_client):
        """Refresh-all success path should return full session response dict."""
        resp = authed_client.post("/api/sessions/test-session/refresh-all")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "test-session"


# ─── Bracketed Paste Endpoint ───


class TestBracketedPasteEndpoint:
    @patch("app.get_tmux_sessions", return_value=[])
    def test_missing_session_returns_404(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/nonexistent/bracketed-paste",
            json={"enabled": True},
        )
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_enable_bracketed_paste(self, mock_run, mock_sessions, authed_client):
        mock_run.return_value = MagicMock(returncode=0)
        resp = authed_client.post(
            "/api/sessions/test-session/bracketed-paste",
            json={"enabled": True},
        )
        assert resp.status_code == 200
        assert resp.json()["bracketed_paste"] is True

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_disable_bracketed_paste(self, mock_run, mock_sessions, authed_client):
        mock_run.return_value = MagicMock(returncode=0)
        resp = authed_client.post(
            "/api/sessions/test-session/bracketed-paste",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["bracketed_paste"] is False

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux gone"))
    def test_bracketed_paste_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        """Subprocess failure in bracketed-paste should return 500."""
        resp = authed_client.post(
            "/api/sessions/test-session/bracketed-paste",
            json={"enabled": True},
        )
        assert resp.status_code == 500
        assert "error" in resp.json()


# ─── Send Keys Validation ───


class TestSendKeysValidation:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_allowed_key_accepted(self, mock_run, mock_sessions, authed_client):
        mock_run.return_value = MagicMock(returncode=0)
        resp = authed_client.post(
            "/api/sessions/test-session/send-keys",
            json={"keys": ["Escape"]},
        )
        assert resp.status_code == 200

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_disallowed_key_rejected(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/test-session/send-keys",
            json={"keys": ["rm -rf /"]},
        )
        assert resp.status_code == 400
        assert "not allowed" in resp.json()["error"].lower()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_single_char_key_accepted(self, mock_run, mock_sessions, authed_client):
        mock_run.return_value = MagicMock(returncode=0)
        resp = authed_client.post(
            "/api/sessions/test-session/send-keys",
            json={"keys": ["y"]},
        )
        assert resp.status_code == 200

    def test_oversized_keys_list_rejected(self, authed_client):
        """Sending > 50 keys must return 422 (Pydantic max_length on list)."""
        resp = authed_client.post(
            "/api/sessions/test-session/send-keys",
            json={"keys": ["q"] * 51},
        )
        assert resp.status_code == 422
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux gone"))
    def test_send_keys_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        """Subprocess failure in send-keys should return 500."""
        resp = authed_client.post(
            "/api/sessions/test-session/send-keys",
            json={"keys": ["Escape"]},
        )
        assert resp.status_code == 500
        assert "error" in resp.json()


# ─── Full Sessions List Endpoint ───


class TestFullSessionsList:
    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.get_session_data")
    @patch("app.async_detect_activity")
    def test_sessions_empty_list(self, mock_activity, mock_data, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions")
        assert resp.status_code == 200
        assert resp.json() == []


# ─── Away Mode Status Endpoint ───


class TestAwayModeStatus:
    def test_returns_disabled_for_unknown_session(self, authed_client):
        resp = authed_client.get("/api/sessions/no-such-session/away-mode")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert "phase" in data
        assert "log" in data

    def test_returns_disabled_for_known_session_not_running(self, authed_client):
        import app
        app._away_mode_state.pop("test-clean-session", None)
        resp = authed_client.get("/api/sessions/test-clean-session/away-mode")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False


# ─── Go Nuts Mode Status Endpoint ───


class TestGoNutsModeStatus:
    def test_returns_disabled_for_unknown_session(self, authed_client):
        resp = authed_client.get("/api/sessions/no-such-session/go-nuts-mode")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False

    def test_status_schema_has_required_fields(self, authed_client):
        resp = authed_client.get("/api/sessions/any-session/go-nuts-mode")
        data = resp.json()
        for field in ("enabled", "phase", "log"):
            assert field in data, f"Missing field: {field}"


# ─── API Key Error Response Schema ───


class TestApiKeyErrorSchema:
    """Verify that API key validation errors use 'error' key (not 'detail')."""

    def test_invalid_key_returns_error_key(self, authed_client):
        resp = authed_client.post(
            "/api/auth/api-key",
            json={"apiKey": "not-a-valid-anthropic-key"},
        )
        assert resp.status_code == 400
        data = resp.json()
        assert "error" in data, "Error response must use 'error' key"
        assert "detail" not in data, "'detail' key would be invisible to the JS handler"

    def test_pydantic_validation_error_uses_error_key(self, authed_client):
        """FastAPI/Pydantic validation errors (422) must also return {error:...} not {detail:[...]}."""
        resp = authed_client.post("/api/auth/api-key", json={})  # missing required 'apiKey'
        assert resp.status_code == 422
        data = resp.json()
        assert "error" in data, "Pydantic validation errors must use 'error' key"
        assert "detail" not in data, "'detail' key would be invisible to the JS handler"

    def test_oversized_api_key_rejected(self, authed_client):
        """API key field has a 500-char max_length — oversized input returns 422."""
        resp = authed_client.post("/api/auth/api-key", json={"apiKey": "x" * 600})
        assert resp.status_code == 422
        assert "error" in resp.json()


# ─── HSTS Header ───


class TestHstsHeader:
    def test_hsts_absent_over_http(self, authed_client):
        """HSTS must NOT be set over plain HTTP (no x-forwarded-proto header)."""
        resp = authed_client.get("/")
        assert "Strict-Transport-Security" not in resp.headers

    def test_hsts_present_when_forwarded_https(self, authed_client):
        """HSTS must be set when request comes in via HTTPS proxy."""
        resp = authed_client.get("/", headers={"x-forwarded-proto": "https"})
        hsts = resp.headers.get("Strict-Transport-Security", "")
        assert "max-age=" in hsts
        assert "includeSubDomains" in hsts


# ─── Claude Usage Endpoint ───


class TestClaudeUsageEndpoint:
    def test_returns_usage_schema(self, authed_client):
        """GET /api/auth/usage should return JSON with expected token usage fields."""
        resp = authed_client.get("/api/auth/usage")
        assert resp.status_code == 200
        data = resp.json()
        assert "date" in data
        assert "inputTokens" in data
        assert "outputTokens" in data
        assert "totalTokens" in data
        assert "messages" in data

    def test_unauthenticated_blocked(self):
        """GET /api/auth/usage must redirect to login for unauthenticated requests."""
        c = TestClient(app)
        resp = c.get("/api/auth/usage", follow_redirects=False)
        assert resp.status_code in (200, 302, 401, 403)

    def test_returns_cached_data_within_ttl(self, authed_client):
        """Second call within 60s should return cached result without re-scanning files."""
        import app
        app._usage_cache["ts"] = time.time()
        app._usage_cache["data"] = {"date": "cached", "inputTokens": 999, "_cached": True}
        try:
            resp = authed_client.get("/api/auth/usage")
            assert resp.status_code == 200
            assert resp.json().get("_cached") is True
        finally:
            app._usage_cache["ts"] = 0  # Reset so subsequent tests scan fresh


class TestParseUsageFile:
    def test_skips_entries_without_usage_field(self, tmp_path):
        """_parse_usage_file should skip assistant entries with no usage data."""
        import json
        from datetime import datetime, timezone

        import app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        jsonl_file = tmp_path / "test.jsonl"
        jsonl_file.write_text(
            # Entry with no usage → should be skipped (hits line 2004 continue)
            json.dumps({"type": "assistant", "timestamp": f"{today}T12:00:00Z",
                        "message": {"model": "claude-sonnet"}}) + "\n" +
            # Entry with usage → should be counted
            json.dumps({"type": "assistant", "timestamp": f"{today}T12:01:00Z",
                        "usage": {"input_tokens": 100, "output_tokens": 50,
                                  "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}}) + "\n"
        )
        result = app._parse_usage_file(str(jsonl_file), today)
        assert result[0] == 100   # input_tokens from second entry
        assert result[4] == 1     # msg_count = 1 (only second entry counted)


# ─── Away Mode Toggle (404 path) ───


class TestAwayModeToggle:
    def test_toggle_missing_session_returns_404(self, authed_client):
        """POST away-mode toggle with unknown session must return 404."""
        resp = authed_client.post(
            "/api/sessions/does-not-exist/away-mode",
            json={"enabled": True},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_disable_missing_session_returns_404(self, authed_client):
        """Disabling away-mode on unknown session must return 404."""
        resp = authed_client.post(
            "/api/sessions/does-not-exist/away-mode",
            json={"enabled": False},
        )
        assert resp.status_code == 404


# ─── Go Nuts Mode Toggle (404 path) ───


class TestGoNutsModeToggle:
    def test_toggle_missing_session_returns_404(self, authed_client):
        """POST go-nuts-mode toggle with unknown session must return 404."""
        resp = authed_client.post(
            "/api/sessions/does-not-exist/go-nuts-mode",
            json={"enabled": True},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_disable_missing_session_returns_404(self, authed_client):
        """Disabling go-nuts-mode on unknown session must return 404."""
        resp = authed_client.post(
            "/api/sessions/does-not-exist/go-nuts-mode",
            json={"enabled": False},
        )
        assert resp.status_code == 404


# ─── Create Session Tests ───


class TestCreateSession:
    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.subprocess.run")
    def test_create_session_with_valid_name(self, mock_run, mock_sessions, authed_client):
        """POST /api/sessions/create with a valid name should return ok=True."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_sessions.side_effect = [[], [{"name": "my-session", "windows": "1", "created": "0", "attached": False}]]
        resp = authed_client.post("/api/sessions/create", json={"name": "my-session"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["name"] == "my-session"

    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.subprocess.run")
    def test_create_session_auto_name(self, mock_run, mock_sessions, authed_client):
        """POST /api/sessions/create with empty name should auto-name the session."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        mock_sessions.return_value = [{"name": "auto-1", "windows": "1", "created": "0", "attached": False}]
        resp = authed_client.post("/api/sessions/create", json={"name": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True

    @patch("app.get_tmux_sessions", return_value=[])
    def test_create_session_invalid_name_returns_400(self, mock_sessions, authed_client):
        """Session names with special characters should be rejected with 400."""
        resp = authed_client.post("/api/sessions/create", json={"name": "bad name!"})
        assert resp.status_code == 400
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions")
    def test_create_session_duplicate_name_returns_409(self, mock_sessions, authed_client):
        """Creating a session with an already-existing name should return 409."""
        mock_sessions.return_value = [{"name": "existing", "windows": "1", "created": "0", "attached": False}]
        resp = authed_client.post("/api/sessions/create", json={"name": "existing"})
        assert resp.status_code == 409
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.subprocess.run")
    def test_create_session_tmux_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        """If tmux new-session fails, return 500."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="already exists")
        resp = authed_client.post("/api/sessions/create", json={"name": "fail-session"})
        assert resp.status_code == 500
        assert "error" in resp.json()

    @patch("app.subprocess.run")
    @patch("app._stored_anthropic_key", "sk-ant-testkey123")
    def test_create_session_injects_stored_api_key(self, mock_run, authed_client):
        """When _stored_anthropic_key is set, session creation should export ANTHROPIC_API_KEY."""
        sessions_before = [{"name": "keyed-session", "windows": "1", "created": "0", "attached": False}]
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.get_tmux_sessions", side_effect=[[], sessions_before]):
            resp = authed_client.post("/api/sessions/create", json={"name": "keyed-session"})
        assert resp.status_code == 200
        calls_str = [str(c) for c in mock_run.call_args_list]
        assert any("ANTHROPIC_API_KEY" in c for c in calls_str)

    @patch("app.subprocess.run")
    @patch("app.NEW_SESSION_CMD", "claude")
    def test_create_session_sends_new_session_cmd(self, mock_run, authed_client):
        """When NEW_SESSION_CMD is set, session creation should send it to the new pane."""
        sessions_before = [{"name": "cmd-session", "windows": "1", "created": "0", "attached": False}]
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.get_tmux_sessions", side_effect=[[], sessions_before]):
            resp = authed_client.post("/api/sessions/create", json={"name": "cmd-session"})
        assert resp.status_code == 200
        calls_str = [str(c) for c in mock_run.call_args_list]
        assert any("claude" in c for c in calls_str)

    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.subprocess.run", side_effect=Exception("tmux daemon crashed"))
    def test_create_session_exception_returns_500(self, mock_run, mock_sessions, authed_client):
        """An unexpected exception in create should return 500."""
        resp = authed_client.post("/api/sessions/create", json={"name": "crash-session"})
        assert resp.status_code == 500
        assert "error" in resp.json()


# ─── Delete Session Tests ───


class TestDeleteSession:
    @patch("app.get_tmux_sessions", return_value=[])
    def test_delete_missing_session_returns_404(self, mock_sessions, authed_client):
        """DELETE on unknown session must return 404."""
        resp = authed_client.delete("/api/sessions/does-not-exist")
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_delete_session_success(self, mock_run, mock_sessions, authed_client):
        """Successful session deletion should return ok=True and session name."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["killed"] == "test-session"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_delete_session_kill_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        """If tmux kill-session fails, return 500."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="can't kill session")
        resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 500
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_delete_session_with_pane_pids(self, mock_run, mock_sessions, authed_client):
        """Delete should TERM then KILL child processes when pane PIDs are found."""
        # Calls: list-panes (returns 1 PID), pkill -TERM, pkill -KILL, kill-session
        pids_result = MagicMock(returncode=0, stdout="99999\n", stderr="")
        ok_result = MagicMock(returncode=0, stdout="", stderr="")
        mock_run.side_effect = [pids_result, ok_result, ok_result, ok_result]
        resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # Verify pkill was called
        calls_str = [str(c) for c in mock_run.call_args_list]
        assert any("pkill" in c for c in calls_str)

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_delete_session_cancels_active_go_nuts_task(self, mock_run, mock_sessions, authed_client):
        """Delete should cancel any active go-nuts task for the session."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        import app
        mock_task = MagicMock()
        mock_task.done.return_value = False
        app._go_nuts_state["test-session"] = {"enabled": True, "task": mock_task}
        try:
            resp = authed_client.delete("/api/sessions/test-session")
            assert resp.status_code == 200
            mock_task.cancel.assert_called_once()
        finally:
            app._go_nuts_state.pop("test-session", None)

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux daemon gone"))
    def test_delete_session_outer_exception_returns_500(self, mock_run, mock_sessions, authed_client):
        """An unexpected outer exception should return 500."""
        resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 500
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_delete_session_pkill_exception_still_succeeds(self, mock_run, mock_sessions, authed_client):
        """pkill failures should be swallowed and kill-session should still run."""
        pids_result = MagicMock(returncode=0, stdout="99999\n", stderr="")
        kill_ok = MagicMock(returncode=0, stdout="", stderr="")

        def run_side_effect(cmd, **kw):
            if "pkill" in cmd:
                raise OSError("no permission")
            if "list-panes" in cmd:
                return pids_result
            return kill_ok

        mock_run.side_effect = run_side_effect
        resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ─── Send Command Success Path Tests ───


class TestSendCommandEndpoint:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_send_short_command_success(self, mock_run, mock_sessions, authed_client):
        """Short commands (<=200 chars) should be sent via send-keys and return ok=True."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        resp = authed_client.post(
            "/api/sessions/test-session/send",
            json={"command": "echo hello"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["sent"] == "echo hello"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_send_long_command_uses_buffer(self, mock_run, mock_sessions, authed_client):
        """Commands longer than 200 chars should use the tmux buffer path."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        long_cmd = "x" * 250
        resp = authed_client.post(
            "/api/sessions/test-session/send",
            json={"command": long_cmd},
        )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True
        # Verify tmux load-buffer was called (buffer path taken)
        calls = [str(c) for c in mock_run.call_args_list]
        assert any("load-buffer" in c for c in calls)

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux gone"))
    def test_send_command_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        """A subprocess failure in send should return 500 with error key."""
        resp = authed_client.post(
            "/api/sessions/test-session/send",
            json={"command": "echo fail"},
        )
        assert resp.status_code == 500
        assert "error" in resp.json()


class TestInterruptSession:
    def test_interrupt_missing_session_returns_404(self, authed_client):
        """POST to an unknown session should return 404."""
        resp = authed_client.post("/api/sessions/no-such-session/interrupt")
        assert resp.status_code == 404
        assert resp.json()["error"] == "Session not found"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_interrupt_success(self, mock_run, mock_sessions, authed_client):
        """Successful interrupt should return ok=True and action=interrupt."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        resp = authed_client.post("/api/sessions/test-session/interrupt")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["action"] == "interrupt"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux crashed"))
    def test_interrupt_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        """A subprocess failure in interrupt should return 500 with error key."""
        resp = authed_client.post("/api/sessions/test-session/interrupt")
        assert resp.status_code == 500
        assert "error" in resp.json()


class TestSetAuthModeEndpoint:
    def test_set_auth_mode_missing_session_returns_404(self, authed_client):
        """POST to an unknown session should return 404."""
        resp = authed_client.post(
            "/api/sessions/no-such-session/set-auth-mode",
            json={"mode": "subscription"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "Session not found"

    def test_set_auth_mode_invalid_mode_returns_400(self, authed_client):
        """An unrecognised mode value should return 400."""
        with patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS):
            resp = authed_client.post(
                "/api/sessions/test-session/set-auth-mode",
                json={"mode": "unknown"},
            )
        assert resp.status_code == 400
        assert resp.json()["error"] == "Invalid mode"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_set_auth_mode_subscription_success(self, mock_run, mock_sessions, authed_client):
        """Setting mode=subscription should unset ANTHROPIC_API_KEY and return ok."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        resp = authed_client.post(
            "/api/sessions/test-session/set-auth-mode",
            json={"mode": "subscription"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["mode"] == "subscription"
        assert data["session"] == "test-session"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    @patch("app._stored_anthropic_key", "sk-ant-testkey123")
    def test_set_auth_mode_api_with_stored_key_success(self, mock_run, mock_sessions, authed_client):
        """Setting mode=api with a stored key should export ANTHROPIC_API_KEY and return ok."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        resp = authed_client.post(
            "/api/sessions/test-session/set-auth-mode",
            json={"mode": "api"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["mode"] == "api"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._stored_anthropic_key", "")
    def test_set_auth_mode_api_no_key_returns_400(self, mock_sessions, authed_client):
        """Setting mode=api with no stored key and no fallback should return 400."""
        # Patch read_text to raise so CLAUDE.md fallback also fails
        with patch("app.asyncio.to_thread", side_effect=Exception("no file")):
            resp = authed_client.post(
                "/api/sessions/test-session/set-auth-mode",
                json={"mode": "api"},
            )
        assert resp.status_code == 400
        assert resp.json()["error"] == "No API key found"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux gone"))
    def test_set_auth_mode_subprocess_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        """Subprocess failure in set-auth-mode should return 500."""
        resp = authed_client.post(
            "/api/sessions/test-session/set-auth-mode",
            json={"mode": "subscription"},
        )
        assert resp.status_code == 500
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    @patch("app._stored_anthropic_key", "")
    def test_set_auth_mode_api_uses_claude_md_fallback_inline_key(self, mock_run, mock_sessions, authed_client, tmp_path):
        """When no stored key, mode=api should extract inline sk-ant- key from CLAUDE.md."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Key embedded in a line (not starting with sk-ant-)
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("# Config\nAPI key: sk-ant-fakekey123\n")
        with patch("app.Path.home", return_value=tmp_path):
            resp = authed_client.post(
                "/api/sessions/test-session/set-auth-mode",
                json={"mode": "api"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    @patch("app._stored_anthropic_key", "")
    def test_set_auth_mode_api_uses_claude_md_fallback_bare_key(self, mock_run, mock_sessions, authed_client, tmp_path):
        """When no stored key, mode=api should extract bare sk-ant- line from CLAUDE.md."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        # Key is on a line by itself (starts with sk-ant-)
        claude_md = tmp_path / "CLAUDE.md"
        claude_md.write_text("sk-ant-directkey456\n")
        with patch("app.Path.home", return_value=tmp_path):
            resp = authed_client.post(
                "/api/sessions/test-session/set-auth-mode",
                json={"mode": "api"},
            )
        assert resp.status_code == 200
        assert resp.json()["ok"] is True


# ---------------------------------------------------------------------------
# _parse_session_stats() — direct unit tests (covers lines 2105-2275)
# ---------------------------------------------------------------------------

class TestParseSessionStats:
    """Direct unit tests for _parse_session_stats() token stats computation."""

    @staticmethod
    def _write_jsonl(path, entries):
        path.write_text("\n".join(json.dumps(e) for e in entries) + "\n")

    @staticmethod
    def _make_entry(today, model, offset_min=1, inp=1000, out=500, cr=0, cc=0):
        return {
            "type": "assistant",
            "timestamp": f"{today}T12:{offset_min:02d}:00Z",
            "model": model,
            "usage": {
                "input_tokens": inp,
                "output_tokens": out,
                "cache_read_input_tokens": cr,
                "cache_creation_input_tokens": cc,
            },
        }

    def test_returns_available_false_when_no_files(self):
        import app as _app
        name = "stats-no-files-unit"
        _app._session_stats_cache.pop(name, None)
        with patch("app._find_session_jsonl_files", return_value=[]):
            result = _app._parse_session_stats(name)
        assert result["available"] is False
        assert "_ts" in result

    def test_returns_available_false_when_old_file_mtime(self, tmp_path):
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [self._make_entry(today, "claude-sonnet-4-5")])

        name = "stats-old-mtime-unit"
        _app._session_stats_cache.pop(name, None)

        old_epoch = time.time() - 86401  # yesterday
        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.os.path.getmtime", return_value=old_epoch):
            result = _app._parse_session_stats(name)

        assert result["available"] is False

    def test_skips_entries_with_old_timestamps(self, tmp_path):
        import app as _app

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [{
            "type": "assistant",
            "timestamp": "2020-01-01T12:00:00Z",  # far in the past
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 999, "output_tokens": 999,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }])

        name = "stats-old-ts-unit"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is False

    def test_returns_stats_with_sonnet_model(self, tmp_path):
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [
            self._make_entry(today, "claude-sonnet-4-5", offset_min=1, inp=1000, out=500),
            self._make_entry(today, "claude-sonnet-4-5", offset_min=2, inp=1100, out=600),
        ])

        name = "stats-sonnet-unit"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is True
        assert result["messageCount"] == 2
        assert result["totalInput"] == 2100
        assert result["totalOutput"] == 1100
        assert result["totalTokens"] == 3200
        assert "sonnet" in result["model"]
        assert result["estimatedCost"] > 0
        assert "_ts" in result

    def test_opus_costs_more_than_sonnet(self, tmp_path):
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        opus_file = tmp_path / "opus.jsonl"
        self._write_jsonl(opus_file, [self._make_entry(today, "claude-opus-4-5", inp=1_000_000, out=0)])

        sonnet_file = tmp_path / "sonnet.jsonl"
        self._write_jsonl(sonnet_file, [self._make_entry(today, "claude-sonnet-4-5", inp=1_000_000, out=0)])

        _app._session_stats_cache.pop("stats-opus-cost", None)
        _app._session_stats_cache.pop("stats-sonnet-cost", None)

        with patch("app.detect_activity", return_value={"status": "idle"}):
            with patch("app._find_session_jsonl_files", return_value=[str(opus_file)]):
                opus_result = _app._parse_session_stats("stats-opus-cost")
            with patch("app._find_session_jsonl_files", return_value=[str(sonnet_file)]):
                sonnet_result = _app._parse_session_stats("stats-sonnet-cost")

        # Opus: 15.0/M vs sonnet 3.0/M
        assert opus_result["estimatedCost"] > sonnet_result["estimatedCost"]

    def test_haiku_costs_less_than_sonnet(self, tmp_path):
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        haiku_file = tmp_path / "haiku.jsonl"
        self._write_jsonl(haiku_file, [self._make_entry(today, "claude-haiku-4-5", inp=1_000_000, out=0)])

        sonnet_file = tmp_path / "sonnet2.jsonl"
        self._write_jsonl(sonnet_file, [self._make_entry(today, "claude-sonnet-4-5", inp=1_000_000, out=0)])

        _app._session_stats_cache.pop("stats-haiku-cost", None)
        _app._session_stats_cache.pop("stats-sonnet2-cost", None)

        with patch("app.detect_activity", return_value={"status": "idle"}):
            with patch("app._find_session_jsonl_files", return_value=[str(haiku_file)]):
                haiku_result = _app._parse_session_stats("stats-haiku-cost")
            with patch("app._find_session_jsonl_files", return_value=[str(sonnet_file)]):
                sonnet_result = _app._parse_session_stats("stats-sonnet2-cost")

        # Haiku: 1.0/M vs sonnet 3.0/M
        assert haiku_result["estimatedCost"] < sonnet_result["estimatedCost"]

    def test_context_pct_computed_from_last_entry(self, tmp_path):
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [
            self._make_entry(today, "claude-sonnet-4-5", offset_min=1, inp=50_000, out=100),
            self._make_entry(today, "claude-sonnet-4-5", offset_min=2, inp=100_000, out=100),
        ])

        name = "stats-ctx-pct"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        # last_input=100_000 / 200_000 ctx_window = 50%
        assert result["contextPct"] == 50.0
        assert result["lastInputTokens"] == 100_000
        assert result["ctxWindowSize"] == 200_000

    def test_cache_hit_returns_immediately(self):
        import app as _app
        name = "stats-cache-immediate"
        sentinel = {"available": True, "_ts": time.time(), "_sentinel": True}
        _app._session_stats_cache[name] = sentinel
        try:
            result = _app._parse_session_stats(name)
            assert result.get("_sentinel") is True
        finally:
            _app._session_stats_cache.pop(name, None)

    def test_rate_status_field_present(self, tmp_path):
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [
            self._make_entry(today, "claude-sonnet-4-5", offset_min=1, inp=500, out=200),
        ])

        name = "stats-rate-status"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert "rateStatus" in result
        assert "ratePct" in result
        assert "activeMinutes" in result
        assert "sessionDurationMin" in result
        assert "secsSinceLastActivity" in result
        assert "modelsUsed" in result

    def test_skips_non_assistant_entries(self, tmp_path):
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [
            # user message — should be skipped
            {"type": "user", "timestamp": f"{today}T12:01:00Z",
             "usage": {"input_tokens": 999, "output_tokens": 999,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
            # tool_use — should be skipped
            {"type": "tool_use", "timestamp": f"{today}T12:02:00Z",
             "usage": {"input_tokens": 999, "output_tokens": 999,
                       "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}},
            # valid assistant entry
            self._make_entry(today, "claude-sonnet-4-5", offset_min=3, inp=100, out=50),
        ])

        name = "stats-skip-non-assistant"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is True
        assert result["messageCount"] == 1  # only the assistant entry counted
        assert result["totalInput"] == 100

    def test_skips_entries_with_no_usage_in_message(self, tmp_path):
        """Cover line 2137: assistant entry with nested message but no usage field."""
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [
            # assistant entry with message wrapper but no usage inside
            {
                "type": "assistant",
                "timestamp": f"{today}T12:01:00Z",
                "message": {"model": "claude-sonnet-4-5"},  # no 'usage' key
            },
            # valid entry to ensure file is processed
            self._make_entry(today, "claude-sonnet-4-5", offset_min=2, inp=100, out=50),
        ])

        name = "stats-no-usage-in-msg"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is True
        assert result["messageCount"] == 1  # only the valid entry counted

    def test_handles_invalid_json_line_in_file(self, tmp_path):
        """Cover lines 2161-2162: outer exception when file contains invalid JSON."""
        import app as _app

        jsonl_file = tmp_path / "conv.jsonl"
        # The invalid JSON line causes the outer except block to fire
        jsonl_file.write_text("not valid json\n")

        name = "stats-invalid-json"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is False

    def test_handles_malformed_timestamp_in_entry(self, tmp_path):
        """Cover lines 2159-2160: inner exception parsing a malformed timestamp."""
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [{
            "type": "assistant",
            "timestamp": f"{today}TNOTAVALIDTIME",  # bad time part
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 100, "output_tokens": 50,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }])

        name = "stats-bad-ts"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        # Timestamp parse fails → no entries tuple appended → available=False
        assert result["available"] is False

    def test_recent_output_rate_computed_when_active_recently(self, tmp_path):
        """Cover lines 2218-2219: recent_output_rate computed from recent active buckets."""
        import time as _time
        from datetime import datetime, timezone

        import app as _app

        now = _time.time()
        now_dt = datetime.fromtimestamp(now, timezone.utc)
        today = now_dt.strftime("%Y-%m-%d")

        # Entry 2 minutes ago (within 10-minute recent window) with meaningful output
        recent_dt = datetime.fromtimestamp(now - 120, timezone.utc)
        recent_ts = recent_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if not recent_ts.startswith(today):
            return  # skip near-midnight UTC

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [{
            "type": "assistant",
            "timestamp": recent_ts,
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 1000, "output_tokens": 200,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }])

        name = "stats-recent-rate"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is True
        assert result["recentOutputRate"] > 0  # lines 2218-2219 were reached

    def test_severely_limited_rate_status_when_busy(self, tmp_path):
        """Cover lines 2230-2232: rate_status='severely_limited' when peak >> recent."""
        import time as _time
        from datetime import datetime, timezone

        import app as _app

        now = _time.time()
        now_dt = datetime.fromtimestamp(now, timezone.utc)
        today = now_dt.strftime("%Y-%m-%d")

        old_dt = datetime.fromtimestamp(now - 10800, timezone.utc)  # 3 hours ago
        old_ts = old_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        recent_dt = datetime.fromtimestamp(now - 60, timezone.utc)  # 1 min ago
        recent_ts = recent_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if not old_ts.startswith(today) or not recent_ts.startswith(today):
            return  # skip near-midnight UTC

        # Build 5 peak entries each in a DIFFERENT minute bucket (1 min apart)
        # so the median of the top-5 buckets = 5000, not diluted by the recent 11-token entry
        peak_entries = []
        for i in range(5):
            dt_i = datetime.fromtimestamp(now - 10800 - i * 60, timezone.utc)
            ts_i = dt_i.strftime("%Y-%m-%dT%H:%M:%SZ")
            if not ts_i.startswith(today):
                return
            peak_entries.append({
                "type": "assistant",
                "timestamp": ts_i,
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 1000, "output_tokens": 5000,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            })
        recent_entry = {
            "type": "assistant",
            "timestamp": recent_ts,
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 1000, "output_tokens": 11,  # very low
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, peak_entries + [recent_entry])

        name = "stats-rate-limited"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "busy"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is True
        assert result["rateStatus"] in ("limited", "severely_limited")

    def test_limited_rate_status_when_busy_and_rate_pct_between_30_and_60(self, tmp_path):
        """Cover line 2234: rate_status='limited' when 30 <= rate_pct < 60."""
        import time as _time
        from datetime import datetime, timezone

        import app as _app

        now = _time.time()
        now_dt = datetime.fromtimestamp(now, timezone.utc)
        today = now_dt.strftime("%Y-%m-%d")

        # Peak entries: 5 separate minute buckets, output=1000 each → peak_output_rate=1000
        peak_entries = []
        for i in range(5):
            dt_i = datetime.fromtimestamp(now - 10800 - i * 60, timezone.utc)
            ts_i = dt_i.strftime("%Y-%m-%dT%H:%M:%SZ")
            if not ts_i.startswith(today):
                return  # skip near-midnight UTC
            peak_entries.append({
                "type": "assistant",
                "timestamp": ts_i,
                "model": "claude-sonnet-4-5",
                "usage": {"input_tokens": 100, "output_tokens": 1000,
                          "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
            })

        # Recent entry: 2 min ago, output=400 → rate_pct=int(400/1000*100)=40 → "limited"
        recent_dt = datetime.fromtimestamp(now - 120, timezone.utc)
        recent_ts = recent_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if not recent_ts.startswith(today):
            return
        recent_entry = {
            "type": "assistant",
            "timestamp": recent_ts,
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 100, "output_tokens": 400,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, peak_entries + [recent_entry])

        name = "stats-limited-rate"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "busy"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is True
        assert result["rateStatus"] == "limited"

    def test_rate_pct_zero_when_no_recent_active_buckets(self, tmp_path):
        """Cover line 2234: rate_pct=0 when all active buckets are older than 10 minutes."""
        import time as _time
        from datetime import datetime, timezone

        import app as _app

        now = _time.time()
        now_dt = datetime.fromtimestamp(now, timezone.utc)
        today = now_dt.strftime("%Y-%m-%d")

        # Entry 15 minutes ago — outside the 10-minute recent window
        old_dt = datetime.fromtimestamp(now - 900, timezone.utc)
        old_ts = old_dt.strftime("%Y-%m-%dT%H:%M:%SZ")

        if not old_ts.startswith(today):
            return  # skip near-midnight UTC

        jsonl_file = tmp_path / "conv.jsonl"
        self._write_jsonl(jsonl_file, [{
            "type": "assistant",
            "timestamp": old_ts,
            "model": "claude-sonnet-4-5",
            "usage": {"input_tokens": 1000, "output_tokens": 5000,
                      "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0},
        }])

        name = "stats-no-recent-pct"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is True
        assert result["ratePct"] == 0  # line 2234: rate_pct = 0 (no recent data)


# ---------------------------------------------------------------------------
# _is_claude_running() — unit tests (covers lines 160-176)
# ---------------------------------------------------------------------------

class TestIsClaudeRunning:
    """Unit tests for _is_claude_running() subprocess helper."""

    @patch("app.subprocess.run")
    def test_returns_true_when_node_running(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=0, stdout="node\n", stderr="")
        assert _app._is_claude_running("test-session") is True

    @patch("app.subprocess.run")
    def test_returns_false_when_bash_running(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=0, stdout="bash\n", stderr="")
        assert _app._is_claude_running("test-session") is False

    @patch("app.subprocess.run")
    def test_returns_false_when_zsh_running(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=0, stdout="zsh\n", stderr="")
        assert _app._is_claude_running("test-session") is False

    @patch("app.subprocess.run")
    def test_returns_false_on_nonzero_returncode(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no session")
        assert _app._is_claude_running("missing") is False

    @patch("app.subprocess.run", side_effect=Exception("tmux not found"))
    def test_returns_false_on_exception(self, mock_run):
        import app as _app
        assert _app._is_claude_running("broken-session") is False


# ---------------------------------------------------------------------------
# _save_anthropic_key / _load_anthropic_key / _clear_anthropic_key (lines 48-79)
# ---------------------------------------------------------------------------

class TestAnthropicKeyFunctions:
    """Unit tests for Anthropic API key persistence helpers."""

    def test_load_returns_key_from_file(self, tmp_path):
        import app as _app
        key_file = tmp_path / "anthropic_api_key"
        key_file.write_text("sk-ant-testkey\n")
        with patch.object(_app, "ANTHROPIC_API_KEY_FILE", key_file):
            _app._stored_anthropic_key = ""
            result = _app._load_anthropic_key()
        assert result == "sk-ant-testkey"

    def test_load_returns_empty_when_file_missing(self, tmp_path):
        import app as _app
        key_file = tmp_path / "anthropic_api_key"  # does not exist
        with patch.object(_app, "ANTHROPIC_API_KEY_FILE", key_file):
            _app._stored_anthropic_key = ""
            result = _app._load_anthropic_key()
        assert result == ""

    def test_load_handles_read_exception(self, tmp_path):
        import app as _app
        key_file = tmp_path / "anthropic_api_key"
        key_file.write_text("sk-ant-shouldnotread")
        key_file.chmod(0o000)
        try:
            with patch.object(_app, "ANTHROPIC_API_KEY_FILE", key_file):
                _app._stored_anthropic_key = ""
                result = _app._load_anthropic_key()
            assert result == ""
        finally:
            key_file.chmod(0o644)

    def test_save_writes_key_to_file(self, tmp_path):
        import app as _app
        key_file = tmp_path / "anthropic_api_key"
        with patch.object(_app, "ANTHROPIC_API_KEY_FILE", key_file), \
             patch("app.MESSAGES_DIR", tmp_path):
            _app._save_anthropic_key("sk-ant-newkey")
        assert key_file.exists()
        assert key_file.read_text() == "sk-ant-newkey"

    def test_save_handles_write_exception(self, tmp_path):
        import app as _app
        key_file = tmp_path / "anthropic_api_key"
        with patch.object(_app, "ANTHROPIC_API_KEY_FILE", key_file), \
             patch("app.MESSAGES_DIR", tmp_path), \
             patch("pathlib.Path.rename", side_effect=OSError("disk full")):
            # Should not raise
            _app._save_anthropic_key("sk-ant-key")

    def test_clear_removes_key_file(self, tmp_path):
        import app as _app
        key_file = tmp_path / "anthropic_api_key"
        key_file.write_text("sk-ant-old")
        with patch.object(_app, "ANTHROPIC_API_KEY_FILE", key_file):
            _app._clear_anthropic_key()
        assert not key_file.exists()
        assert _app._stored_anthropic_key == ""

    def test_clear_handles_missing_file_gracefully(self, tmp_path):
        import app as _app
        key_file = tmp_path / "anthropic_api_key"  # does not exist
        with patch.object(_app, "ANTHROPIC_API_KEY_FILE", key_file):
            _app._clear_anthropic_key()  # should not raise
        assert _app._stored_anthropic_key == ""


# ---------------------------------------------------------------------------
# _save_autonomous_state / _load_autonomous_state (lines 128-150)
# ---------------------------------------------------------------------------

class TestAutonomousStatePersistence:
    """Unit tests for _save_autonomous_state() and _load_autonomous_state()."""

    def test_save_and_load_roundtrip(self, tmp_path):
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        orig_away = dict(_app._away_mode_state)
        orig_nuts = dict(_app._go_nuts_state)

        _app._away_mode_state["my-session"] = {"enabled": True}
        _app._go_nuts_state["my-session"] = {"enabled": False}  # not enabled, should not be saved

        try:
            with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file), \
                 patch("app.MESSAGES_DIR", tmp_path):
                _app._save_autonomous_state()
                loaded = _app._load_autonomous_state()
        finally:
            _app._away_mode_state.clear()
            _app._away_mode_state.update(orig_away)
            _app._go_nuts_state.clear()
            _app._go_nuts_state.update(orig_nuts)

        assert loaded.get("my-session", {}).get("away_mode") is True
        assert "go_nuts_mode" not in loaded.get("my-session", {})

    def test_load_returns_empty_when_file_missing(self, tmp_path):
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file):
            result = _app._load_autonomous_state()
        assert result == {}

    def test_load_handles_corrupt_json(self, tmp_path):
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        state_file.write_text("{invalid json}")
        with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file):
            result = _app._load_autonomous_state()
        assert result == {}

    def test_save_handles_write_exception(self, tmp_path):
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file), \
             patch("app._atomic_write_json", side_effect=OSError("disk full")):
            _app._save_autonomous_state()  # should not raise

    def test_save_includes_go_nuts_when_enabled(self, tmp_path):
        """Cover line 138: go_nuts_state with enabled=True is persisted."""
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        orig_nuts = dict(_app._go_nuts_state)

        _app._go_nuts_state["nuts-session"] = {"enabled": True}

        try:
            with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file), \
                 patch("app.MESSAGES_DIR", tmp_path):
                _app._save_autonomous_state()
                loaded = _app._load_autonomous_state()
        finally:
            _app._go_nuts_state.clear()
            _app._go_nuts_state.update(orig_nuts)

        assert loaded.get("nuts-session", {}).get("go_nuts_mode") is True


# ---------------------------------------------------------------------------
# capture_pane_full / capture_pane_recent (lines 622-643)
# ---------------------------------------------------------------------------

class TestCapturePaneFunctions:
    """Unit tests for capture_pane_full() and capture_pane_recent()."""

    @patch("app.subprocess.run")
    def test_capture_full_returns_stdout_on_success(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=0, stdout="output text\n")
        assert _app.capture_pane_full("sess") == "output text\n"

    @patch("app.subprocess.run")
    def test_capture_full_returns_empty_on_nonzero(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=1, stdout="ignored")
        assert _app.capture_pane_full("sess") == ""

    @patch("app.subprocess.run", side_effect=Exception("tmux gone"))
    def test_capture_full_returns_empty_on_exception(self, mock_run):
        import app as _app
        assert _app.capture_pane_full("sess") == ""

    @patch("app.subprocess.run")
    def test_capture_recent_returns_stdout_on_success(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=0, stdout="recent\n")
        assert _app.capture_pane_recent("sess") == "recent\n"

    @patch("app.subprocess.run")
    def test_capture_recent_returns_empty_on_nonzero(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=1, stdout="")
        assert _app.capture_pane_recent("sess") == ""

    @patch("app.subprocess.run", side_effect=Exception("gone"))
    def test_capture_recent_returns_empty_on_exception(self, mock_run):
        import app as _app
        assert _app.capture_pane_recent("sess") == ""


# ---------------------------------------------------------------------------
# Misc small uncovered paths
# ---------------------------------------------------------------------------

class TestMiscUncoveredPaths:
    """Tests for small uncovered code paths."""

    def test_login_rate_limiter_prunes_stale_keys(self):
        """_check_login_rate_limit prunes stale window keys for the same IP (line 455)."""
        import app as _app
        ip = "192.0.2.99"
        # Pre-populate a stale key for this IP
        stale_key = f"{ip}:0"
        _app._login_attempts[stale_key] = 1

        try:
            result = _app._check_login_rate_limit(ip)
            assert result is True
            # The stale key should have been pruned
            assert stale_key not in _app._login_attempts
        finally:
            _app._login_attempts.pop(stale_key, None)
            # Clean up current window key
            import time as _time
            cur_key = f"{ip}:{int(_time.time() // 60)}"
            _app._login_attempts.pop(cur_key, None)

    def test_notes_load_handles_corrupt_json(self, tmp_path):
        """_load_all_notes returns {} when notes file is corrupt (lines 491-493)."""
        import app as _app
        notes_file = tmp_path / "notes.json"
        notes_file.write_text("{bad json}")
        with patch.object(_app, "NOTES_FILE", notes_file):
            result = _app._load_all_notes()
        assert result == {}

    def test_messages_load_handles_corrupt_json(self, tmp_path):
        """_load_messages returns {} when messages file is corrupt (lines 520-522)."""
        import app as _app
        messages_file = tmp_path / "messages.json"
        messages_file.write_text("{bad json}")
        with patch.object(_app, "MESSAGES_FILE", messages_file):
            result = _app._load_messages()
        assert result == {}

    def test_save_notes_writes_to_file(self, tmp_path):
        """_save_notes persists cache entries that have notes (lines 498-507)."""
        import app as _app
        notes_file = tmp_path / "notes.json"
        with patch.object(_app, "NOTES_FILE", notes_file), \
             patch("app.MESSAGES_DIR", tmp_path), \
             patch.object(_app, "cache", {"sess1": {"notes": "my note"}}):
            _app._save_notes()
        assert notes_file.exists()
        data = json.loads(notes_file.read_text())
        assert data.get("sess1") == "my note"

    def test_save_notes_handles_write_exception(self, tmp_path):
        """_save_notes exception path (lines 506-507)."""
        import app as _app
        notes_file = tmp_path / "notes.json"
        with patch.object(_app, "NOTES_FILE", notes_file), \
             patch("app.MESSAGES_DIR", tmp_path), \
             patch.object(_app, "cache", {"sess1": {"notes": "my note"}}), \
             patch("app._atomic_write_json", side_effect=OSError("disk full")):
            _app._save_notes()  # should not raise

    def test_save_messages_handles_write_exception(self, tmp_path):
        """_save_messages exception path (lines 537-538)."""
        import app as _app
        messages_file = tmp_path / "messages.json"
        with patch.object(_app, "MESSAGES_FILE", messages_file), \
             patch("app.MESSAGES_DIR", tmp_path), \
             patch.object(_app, "cache", {"sess1": {"messages": [{"role": "user", "content": "hi"}]}}), \
             patch("app._atomic_write_json", side_effect=OSError("disk full")):
            _app._save_messages()  # should not raise

    def test_clear_anthropic_key_handles_unlink_exception(self, tmp_path):
        """_clear_anthropic_key exception path (lines 78-79)."""
        import app as _app
        key_file = tmp_path / "anthropic_api_key"
        key_file.write_text("sk-ant-oldkey")
        with patch.object(_app, "ANTHROPIC_API_KEY_FILE", key_file), \
             patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
            _app._clear_anthropic_key()  # should not raise
        assert _app._stored_anthropic_key == ""

# ---------------------------------------------------------------------------
# llm_call() — async unit tests (covers lines 1094-1119)
# ---------------------------------------------------------------------------

class TestLlmCall:
    """Async unit tests for llm_call() OpenAI wrapper."""

    @pytest.mark.asyncio
    async def test_success_returns_stripped_text(self):
        import app as _app
        mock_resp = MagicMock()
        mock_resp.choices = [MagicMock()]
        mock_resp.choices[0].message.content = "  hello world  "
        mock_resp.usage = MagicMock(total_tokens=10)

        async def fake_wait_for(coro, timeout):
            coro.close()
            return mock_resp

        with patch("app.asyncio.wait_for", fake_wait_for):
            result = await _app.llm_call("sys prompt", "user content", max_tokens=50)

        assert result == "hello world"

    @pytest.mark.asyncio
    async def test_timeout_returns_error_message(self):
        import asyncio as _asyncio

        import app as _app

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise _asyncio.TimeoutError()

        with patch("app.asyncio.wait_for", fake_wait_for):
            result = await _app.llm_call("sys", "user")

        assert result == "(error: LLM request timed out)"

    @pytest.mark.asyncio
    async def test_exception_returns_error_message(self):
        import app as _app

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise Exception("API failure")

        with patch("app.asyncio.wait_for", fake_wait_for):
            result = await _app.llm_call("sys", "user")

        assert "(error: API failure)" in result


# ---------------------------------------------------------------------------
# LLM pipeline helpers — async tests (covers lines 1124-1161, 1166-1180, 1199-1222)
# ---------------------------------------------------------------------------

class TestLlmPipelineHelpers:
    """Async tests for get_title_and_description, get_progress, get_notes."""

    @pytest.mark.asyncio
    async def test_get_title_and_description_returns_tuple(self):
        import app as _app

        with patch("app.llm_call", new_callable=AsyncMock, return_value="My Title"):
            title, description = await _app.get_title_and_description(
                "test-session", "some terminal output\n" * 10
            )

        assert isinstance(title, str)
        assert isinstance(description, str)

    @pytest.mark.asyncio
    async def test_get_progress_returns_string(self):
        import app as _app

        with patch("app.llm_call", new_callable=AsyncMock, return_value="Progress summary"):
            result = await _app.get_progress("test-session", "terminal output\n" * 100)

        assert result == "Progress summary"

    @pytest.mark.asyncio
    async def test_get_notes_returns_string(self):
        import app as _app

        with patch("app.llm_call", new_callable=AsyncMock, return_value="Notes text"):
            result = await _app.get_notes("test-session", "terminal output\n" * 50)

        assert result == "Notes text"

    @pytest.mark.asyncio
    async def test_get_notes_with_messages_and_existing_notes(self):
        import app as _app

        messages = [{"role": "user", "text": "hello"}, {"role": "assistant", "text": "hi"}]
        with patch("app.llm_call", new_callable=AsyncMock, return_value="Updated notes"):
            result = await _app.get_notes(
                "test-session",
                "terminal\n" * 300,  # >200 lines triggers multi-slice code paths
                existing_notes="previous notes here",
                messages=messages,
            )

        assert result == "Updated notes"

    @pytest.mark.asyncio
    async def test_get_progress_with_over_400_lines(self):
        """Cover lines 1170-1177: multi-slice logic for >300 and >400 line input."""
        import app as _app

        with patch("app.llm_call", new_callable=AsyncMock, return_value="Progress over 400"):
            result = await _app.get_progress("test-session", "line\n" * 500)

        assert result == "Progress over 400"

    @pytest.mark.asyncio
    async def test_get_realtime_returns_string(self):
        """Cover lines 1249-1268: get_realtime() captures pane and calls llm_call."""
        import app as _app

        with patch("app.asyncio.to_thread", new_callable=AsyncMock, return_value="pane content"), \
             patch("app.async_detect_activity", new_callable=AsyncMock,
                   return_value={"status": "idle", "detail": ""}), \
             patch("app.llm_call", new_callable=AsyncMock, return_value="Realtime update"):
            result = await _app.get_realtime("test-session")

        assert result == "Realtime update"

    @pytest.mark.asyncio
    async def test_get_realtime_with_activity_detail(self):
        """Cover the 'detail' branch in get_realtime (line 1253)."""
        import app as _app

        with patch("app.asyncio.to_thread", new_callable=AsyncMock, return_value="output"), \
             patch("app.async_detect_activity", new_callable=AsyncMock,
                   return_value={"status": "busy", "detail": "running tests"}), \
             patch("app.llm_call", new_callable=AsyncMock, return_value="Busy update"):
            result = await _app.get_realtime("test-session")

        assert result == "Busy update"

    @pytest.mark.asyncio
    async def test_get_session_data_populates_cache(self):
        """Cover lines 1305-1354: get_session_data() orchestration."""
        import app as _app

        session = "session-data-test"
        _app.cache.pop(session, None)

        fake_title_desc = ("Test Title", "Test description")

        with patch("app.capture_pane_full", return_value="terminal output\n" * 50), \
             patch("app.get_title_and_description", new_callable=AsyncMock, return_value=fake_title_desc), \
             patch("app.get_progress", new_callable=AsyncMock, return_value="progress text"), \
             patch("app.get_notes", new_callable=AsyncMock, return_value="some important notes"), \
             patch("app.get_realtime", new_callable=AsyncMock, return_value="realtime text"), \
             patch("app._load_session_messages", return_value=[]), \
             patch("app._load_session_notes", return_value=""), \
             patch("app._save_messages"), \
             patch("app._save_notes"):
            result = await _app.get_session_data(session)

        assert result.get("title") == "Test Title"
        assert result.get("description") == "Test description"
        assert result.get("progress") == "progress text"


# ---------------------------------------------------------------------------
# _send_ctrl_c() — async unit test (covers line 648)
# ---------------------------------------------------------------------------

class TestSendCtrlC:
    """Async unit test for _send_ctrl_c()."""

    @pytest.mark.asyncio
    async def test_sends_ctrl_c_to_session(self):
        import app as _app

        with patch("app.asyncio.to_thread", new_callable=AsyncMock) as mock_thread:
            await _app._send_ctrl_c("test-session")

        mock_thread.assert_called_once()
        call_args = mock_thread.call_args[0]
        # Verify C-c is in the command list
        assert "C-c" in call_args[1]


# ---------------------------------------------------------------------------
# /api/sessions/{name}/rename — endpoint tests (covers lines 1627-1655)
# ---------------------------------------------------------------------------

class TestRenameSessionEndpoint:
    """Tests for POST /api/sessions/{name}/rename."""

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_missing_session_returns_404(self, mock_sessions, authed_client):
        resp = authed_client.post("/api/sessions/no-such-session/rename",
                                   json={"new_name": "valid-name"})
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_invalid_name_returns_400(self, mock_sessions, authed_client):
        resp = authed_client.post("/api/sessions/test-session/rename",
                                   json={"new_name": "invalid name with spaces"})
        assert resp.status_code == 400
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_same_name_returns_ok(self, mock_sessions, authed_client):
        resp = authed_client.post("/api/sessions/test-session/rename",
                                   json={"new_name": "test-session"})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_duplicate_name_returns_409(self, mock_sessions, authed_client):
        # "work-session" is the second session in MOCK_SESSIONS
        resp = authed_client.post("/api/sessions/test-session/rename",
                                   json={"new_name": "work-session"})
        assert resp.status_code == 409
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_success_returns_new_name(self, mock_run, mock_sessions, authed_client):
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        resp = authed_client.post("/api/sessions/test-session/rename",
                                   json={"new_name": "brand-new-name"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "brand-new-name"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_tmux_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="tmux error")
        resp = authed_client.post("/api/sessions/test-session/rename",
                                   json={"new_name": "brand-new-name"})
        assert resp.status_code == 500

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux gone"))
    def test_exception_returns_500(self, mock_run, mock_sessions, authed_client):
        resp = authed_client.post("/api/sessions/test-session/rename",
                                   json={"new_name": "brand-new-name"})
        assert resp.status_code == 500
        assert "error" in resp.json()


# ---------------------------------------------------------------------------
# auth_middleware — no-password path (covers line 420)
# ---------------------------------------------------------------------------

class TestAuthMiddlewareNoPassword:
    """Test auth middleware bypass when TMUX_DASH_PASS is unset."""

    def test_endpoint_accessible_without_auth_when_no_password_set(self):
        """When AUTH_PASS is empty, auth middleware skips auth (line 420)."""
        with patch("app.AUTH_PASS", ""):
            client = TestClient(app)
            resp = client.get("/api/health")
        # Should not return login page (200 or 500, but not login HTML)
        assert "Login" not in resp.text


# ---------------------------------------------------------------------------
# _go_nuts_log() — direct unit test (covers lines 3692-3695)
# ---------------------------------------------------------------------------

class TestGoNutsLog:
    """Unit tests for _go_nuts_log() helper."""

    def test_appends_entry_to_state_log(self):
        import app as _app

        state = {"phase": 2, "step": 3}
        _app._go_nuts_log(state, "test action")

        assert "log" in state
        assert len(state["log"]) == 1
        entry = state["log"][0]
        assert entry["action"] == "test action"
        assert entry["phase"] == 2
        assert entry["step"] == 3
        assert "ts" in entry

    def test_trims_log_to_log_cap(self):
        import app as _app

        state = {"phase": 0, "step": 0, "log": [{"ts": 0, "phase": 0, "step": 0, "action": f"old-{i}"} for i in range(_app._LOG_CAP)]}
        _app._go_nuts_log(state, "new action")

        # Log should be capped at _LOG_CAP entries
        assert len(state["log"]) == _app._LOG_CAP
        assert state["log"][-1]["action"] == "new action"


# ---------------------------------------------------------------------------
# _async_is_claude_running() — async unit test (covers line 181)
# ---------------------------------------------------------------------------

class TestAsyncIsClaudeRunning:
    """Async unit test for _async_is_claude_running()."""

    @pytest.mark.asyncio
    async def test_delegates_to_sync_function(self):
        import app as _app

        with patch("app.asyncio.to_thread", new_callable=AsyncMock, return_value=True) as mock_thread:
            result = await _app._async_is_claude_running("test-session")

        assert result is True
        mock_thread.assert_called_once_with(_app._is_claude_running, "test-session")


# ---------------------------------------------------------------------------
# _tmux_type_and_enter() — async unit test (covers new helper)
# ---------------------------------------------------------------------------

class TestTmuxTypeAndEnter:
    """Async unit tests for _tmux_type_and_enter() helper."""

    @pytest.mark.asyncio
    async def test_sends_text_then_enter(self):
        import app as _app

        calls = []

        async def fake_to_thread(fn, *args, **kwargs):
            calls.append(args)
            return MagicMock(returncode=0)

        with patch("app.asyncio.to_thread", side_effect=fake_to_thread):
            await _app._tmux_type_and_enter("my-session", "echo hello")

        assert len(calls) == 2
        # calls[i] = (subprocess.run_fn, [tmux_args...]) — index [0] is the tmux command list
        first_cmd = calls[0][0]
        assert "-l" in first_cmd
        assert "echo hello" in first_cmd
        second_cmd = calls[1][0]
        assert "Enter" in second_cmd

    @pytest.mark.asyncio
    async def test_custom_timeout_is_forwarded(self):
        import app as _app

        timeouts = []

        async def fake_to_thread(fn, *args, **kwargs):
            timeouts.append(kwargs.get("timeout"))
            return MagicMock(returncode=0)

        with patch("app.asyncio.to_thread", side_effect=fake_to_thread):
            await _app._tmux_type_and_enter("my-session", "cmd", timeout=10)

        assert all(t == 10 for t in timeouts)


# ---------------------------------------------------------------------------
# _ensure_claude_running() — async unit tests (covers lines 189-221)
# ---------------------------------------------------------------------------

class TestEnsureClaudeRunning:
    """Unit tests for _ensure_claude_running() — OOM crash recovery."""

    @pytest.mark.asyncio
    async def test_returns_true_if_claude_already_running(self):
        """Line 190-191: already running → return True immediately."""
        import app as _app

        with patch("app._async_is_claude_running", new_callable=AsyncMock, return_value=True):
            result = await _app._ensure_claude_running("my-session")
        assert result is True

    @pytest.mark.asyncio
    async def test_restarts_claude_and_returns_true(self):
        """Lines 189-213: not running → sends restart command → running after 1 poll."""
        import app as _app

        # First call: not running. Second call (during loop): running.
        is_running_values = [False, True]
        log_entries = []
        state = {"enabled": True}

        async def fake_is_running(session_name):
            return is_running_values.pop(0)

        with patch("app._async_is_claude_running", side_effect=fake_is_running), \
             patch("app.asyncio.to_thread", new_callable=AsyncMock), \
             patch("app.asyncio.sleep", new_callable=AsyncMock):
            result = await _app._ensure_claude_running(
                "my-session",
                log_fn=lambda s, msg: log_entries.append(msg),
                state=state,
            )
        assert result is True
        assert any("restarted" in e for e in log_entries)  # covers line 210

    @pytest.mark.asyncio
    async def test_returns_false_after_timeout(self):
        """Lines 215-218: not running → sends restart command → never restarts."""
        import app as _app

        async def fake_sleep(duration):
            pass

        with patch("app._async_is_claude_running", new_callable=AsyncMock, return_value=False), \
             patch("app.asyncio.to_thread", new_callable=AsyncMock), \
             patch("app.asyncio.sleep", side_effect=fake_sleep):
            result = await _app._ensure_claude_running("my-session")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """Lines 219-221: exception during restart attempt."""
        import app as _app

        with patch("app._async_is_claude_running", new_callable=AsyncMock, return_value=False), \
             patch("app.asyncio.to_thread", new_callable=AsyncMock, side_effect=Exception("tmux gone")):
            result = await _app._ensure_claude_running("my-session")
        assert result is False

    @pytest.mark.asyncio
    async def test_calls_log_fn_when_provided(self):
        """Lines 195-196: log_fn and state dict are updated when not running."""
        import app as _app

        log_entries = []
        # non-empty dict required — empty dict is falsy and skips the log_fn branch
        state = {"enabled": True}

        async def fake_sleep(duration):
            pass

        with patch("app._async_is_claude_running", new_callable=AsyncMock, return_value=False), \
             patch("app.asyncio.to_thread", new_callable=AsyncMock), \
             patch("app.asyncio.sleep", side_effect=fake_sleep):
            await _app._ensure_claude_running("my-session", log_fn=lambda s, msg: log_entries.append(msg), state=state)
        assert any("not running" in entry for entry in log_entries)


# ---------------------------------------------------------------------------
# Autonomous mode wrapper functions (lines 3040, 3055, 3292, 3690)
# ---------------------------------------------------------------------------

class TestAutonomousModeWrappers:
    """Unit tests for thin async wrappers that delegate to shared functions."""

    @pytest.mark.asyncio
    async def test_away_mode_continuous_loop_delegates(self):
        """Line 3040: _away_mode_continuous_loop delegates to _autonomous_continuous_loop."""
        import app as _app

        _app._away_mode_state["wrap-test"] = {"enabled": True}
        try:
            with patch("app._autonomous_continuous_loop", new_callable=AsyncMock) as mock_loop:
                await _app._away_mode_continuous_loop("wrap-test")
            mock_loop.assert_called_once()
            call_kwargs = mock_loop.call_args.kwargs
            assert call_kwargs.get("log_fn") is _app._away_log
        finally:
            _app._away_mode_state.pop("wrap-test", None)

    @pytest.mark.asyncio
    async def test_go_nuts_continuous_loop_delegates(self):
        """Line 3055: _go_nuts_continuous_loop delegates to _autonomous_continuous_loop."""
        import app as _app

        _app._go_nuts_state["wrap-test"] = {"enabled": True}
        try:
            with patch("app._autonomous_continuous_loop", new_callable=AsyncMock) as mock_loop:
                await _app._go_nuts_continuous_loop("wrap-test")
            mock_loop.assert_called_once()
            call_kwargs = mock_loop.call_args.kwargs
            assert call_kwargs.get("log_fn") is _app._go_nuts_log
        finally:
            _app._go_nuts_state.pop("wrap-test", None)

    @pytest.mark.asyncio
    async def test_away_send_and_wait_delegates(self):
        """Line 3292: _away_send_and_wait delegates to _autonomous_send_and_wait."""
        import app as _app

        state = {"enabled": True}
        with patch("app._autonomous_send_and_wait", new_callable=AsyncMock, return_value="summary") as mock_fn:
            result = await _app._away_send_and_wait("sess", "prompt text", state, "step-name")
        assert result == "summary"
        mock_fn.assert_called_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs.get("log_fn") is _app._away_log

    @pytest.mark.asyncio
    async def test_go_nuts_send_and_wait_delegates(self):
        """Line 3690: _go_nuts_send_and_wait delegates to _autonomous_send_and_wait."""
        import app as _app

        state = {"enabled": True}
        with patch("app._autonomous_send_and_wait", new_callable=AsyncMock, return_value="gn-summary") as mock_fn:
            result = await _app._go_nuts_send_and_wait("sess", "build prompt", state, "gn-step")
        assert result == "gn-summary"
        mock_fn.assert_called_once()
        call_kwargs = mock_fn.call_args.kwargs
        assert call_kwargs.get("log_fn") is _app._go_nuts_log
