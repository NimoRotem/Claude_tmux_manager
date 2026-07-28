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

from app import AUTH_COOKIE, AUTH_PASS, AUTH_USER, _make_token, app

# Auth cookies carry the stable user id, not the configurable display/login name.
AUTH_TOKEN = _make_token("admin")
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


@pytest.fixture(autouse=True)
def isolated_prompt_audit(tmp_path, monkeypatch):
    """Tests must never mutate live prompt or impersonation state."""
    import app as app_module

    monkeypatch.setattr(
        app_module,
        "PROMPT_AUDIT_FILE",
        tmp_path / "prompt-history.jsonl",
    )
    monkeypatch.setattr(
        app_module,
        "PROMPT_AUDIT_BACKFILL_MARKER",
        tmp_path / "prompt-history-backfill-v1.json",
    )
    app_module._prompt_audit_summary_cache.update({
        "signature": None,
        "data": {},
    })
    monkeypatch.setattr(
        app_module,
        "IMPERSONATION_SESSIONS_FILE",
        tmp_path / "impersonation-sessions.json",
    )
    monkeypatch.setattr(app_module, "_impersonation_tickets", {})
    monkeypatch.setattr(app_module, "_impersonation_sessions", {})
    monkeypatch.setattr(app_module, "_impersonation_sessions_loaded", False)
    monkeypatch.setattr(
        app_module,
        "BROWSER_SESSIONS_FILE",
        tmp_path / "browser-sessions.json",
    )
    browser_root = tmp_path / "browsers"
    monkeypatch.setattr(app_module, "CB_ROOT", browser_root)
    monkeypatch.setattr(
        app_module,
        "BROWSER_PROXY_CONF",
        browser_root / "proxy.json",
    )
    monkeypatch.setattr(
        app_module,
        "BROWSER_PROXY_USAGE",
        browser_root / "state" / "proxy-usage.json",
    )
    monkeypatch.setattr(
        app_module,
        "BROWSER_FINGERPRINT_TOOL",
        browser_root / "bin" / "fingerprint-audit.py",
    )
    monkeypatch.setattr(
        app_module,
        "BROWSER_LAUNCHER",
        str(browser_root / "bin" / "browser-session.sh"),
    )
    monkeypatch.setattr(app_module, "_browser_starting", {})


# ─── Auth & Middleware Tests ───


class TestAuthMiddleware:
    def test_unauthenticated_returns_login_page(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert "Dashboard" in resp.text
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
        assert AUTH_COOKIE in resp.cookies

    def test_login_failure_redirects_with_error(self, client):
        resp = client.post(
            "/login",
            data={"username": "wrong", "password": "wrong"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "err=1" in resp.headers.get("location", "")


class TestMemberAuthAndContextIsolation:
    @staticmethod
    def _member(password: str = "member-pass") -> dict:
        import app as app_module

        salt = "member-test-salt"
        return {
            "id": "u_member",
            "username": "member@example.com",
            "password_hash": app_module._hash_password(password, salt),
            "password_salt": salt,
            "role": "user",
            "created_at": 1,
            "last_login": 0,
        }

    def test_member_launch_uses_yolo_when_team_mode_is_disabled(self):
        import app as app_module

        member = self._member()
        with (
            patch.object(app_module, "TEAM_MODE", False),
            patch.object(
                app_module,
                "NEW_SESSION_CMD",
                "codex --dangerously-bypass-approvals-and-sandbox",
            ),
        ):
            command = app_module._session_launch_base(user=member)

        assert command == "codex --yolo"

    def test_member_resume_preserves_yolo(self):
        import app as app_module

        member = self._member()
        launch = app_module._launch_codex_cmd(
            app_module._session_launch_base(user=member),
            pin_model=False,
            resume=True,
        )

        assert launch == "codex resume --last --yolo"

    def test_member_codex_home_uses_the_complete_yolo_config(self, tmp_path):
        import tomllib

        import app as app_module

        member = self._member()
        config = tmp_path / "config.toml"
        config.write_text(
            'model = "gpt-test"\n'
            'sandbox_mode = "danger-full-access"\n'
            '\n[projects."/work"]\n'
            'trust_level = "trusted"\n'
            "\n[mcp_servers.admin-browser]\n"
            'command = "node"\n'
            'args = ["browser.js", "--cdp", "http://127.0.0.1:9222"]\n'
        )
        browser = {
            "id": "member-browser",
            "owner_id": member["id"],
            "cdp_port": 9223,
        }
        projects_root = tmp_path / "projects"

        with (
            patch.object(app_module, "PROJECTS_ROOT", projects_root),
            patch.object(
                app_module,
                "_load_session_owners",
                return_value={"member-work": member["id"]},
            ),
            patch.object(app_module, "_google_mcp_command", return_value=""),
        ):
            app_module._configure_member_codex_isolation(
                tmp_path,
                member,
                browser,
            )

        parsed = tomllib.loads(config.read_text())
        member_project = str(
            projects_root / member["username"] / "member-work"
        )
        assert (
            parsed["model"],
            parsed["model_reasoning_effort"],
            parsed["approval_policy"],
            parsed["default_permissions"],
            "sandbox_mode" in parsed,
            "permissions" in parsed,
            parsed["features"]["apps"],
            parsed["features"]["remote_plugin"],
            parsed["notice"]["hide_full_access_warning"],
            parsed["tui"]["model_availability_nux"]["gpt-5.6-sol"],
            parsed["apps"]["_default"]["enabled"],
            "mcp_servers" in parsed,
            set(parsed["projects"]),
            parsed["projects"][member_project]["trust_level"],
        ) == (
            "gpt-5.6-sol",
            "max",
            "never",
            ":danger-full-access",
            False,
            False,
            False,
            False,
            True,
            3,
            False,
            False,
            {member_project},
            "trusted",
        )

    def test_member_relaunch_preserves_its_codex_home(self, tmp_path):
        import shlex

        import app as app_module

        member = self._member()
        projects_root = tmp_path / "projects"
        with (
            patch.object(app_module, "_user_for_session", return_value=member),
            patch.object(app_module, "_user_codex_config_dir", return_value=tmp_path),
            patch.object(app_module, "PROJECTS_ROOT", projects_root),
            patch.object(app_module, "_ensure_user_codex_config_dir"),
            patch.object(app_module, "_ensure_user_browser_session"),
            patch.object(app_module.subprocess, "run") as run,
        ):
            exported = app_module._send_profile_export(
                "member-session",
                app_module.DEFAULT_PROFILE_ID,
            )

        sent = run.call_args_list[0].args[0]
        project_dir = projects_root / member["username"] / "member-session"
        assert (
            exported,
            sent[:5],
            sent[5],
            project_dir.is_dir(),
        ) == (
            True,
            ["tmux", "send-keys", "-t", "member-session", "-l"],
            (
                f"export CODEX_HOME={shlex.quote(str(tmp_path))}; "
                f"cd -- {shlex.quote(str(project_dir))}"
            ),
            True,
        )

    def test_member_config_editor_restores_the_complete_yolo_config(
        self,
        tmp_path,
    ):
        import tomllib

        import app as app_module

        member = self._member()
        browser = {
            "id": "member-browser",
            "owner_id": member["id"],
            "cdp_port": 9223,
        }
        submitted = (
            'model = "gpt-test"\n'
            'sandbox_mode = "danger-full-access"\n'
            '\n[features]\n'
            'apps = true\n'
            '\n[mcp_servers.admin-browser]\n'
            'command = "node"\n'
            'args = ["--cdp", "http://127.0.0.1:9222"]\n'
        )
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(
                app_module,
                "_user_codex_config_dir",
                return_value=tmp_path,
            ),
            patch.object(
                app_module,
                "_ensure_user_codex_config_dir",
            ),
            patch.object(
                app_module,
                "_ensure_user_browser_session",
                return_value=browser,
            ),
            patch.object(
                app_module,
                "PROJECTS_ROOT",
                tmp_path / "projects",
            ),
            patch.object(app_module, "_google_mcp_command", return_value=""),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.post(
                "/api/my/context/config.toml",
                json={"content": submitted},
            )

        parsed = tomllib.loads((tmp_path / "config.toml").read_text())
        assert (
            response.status_code,
            parsed["model"],
            parsed["model_reasoning_effort"],
            parsed["approval_policy"],
            "sandbox_mode" in parsed,
            "permissions" in parsed,
            parsed["features"]["apps"],
            parsed["features"]["remote_plugin"],
            parsed["default_permissions"],
            parsed["notice"]["hide_full_access_warning"],
            "mcp_servers" in parsed,
        ) == (
            200,
            "gpt-5.6-sol",
            "max",
            "never",
            False,
            False,
            False,
            False,
            ":danger-full-access",
            True,
            False,
        )

    def test_member_cannot_exit_codex_into_the_shared_host_shell(self):
        import app as app_module

        member = self._member()
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(
                app_module,
                "_session_owner_id",
                return_value=member["id"],
            ),
            patch.object(
                app_module,
                "_find_session",
                return_value=(0, {"name": "member-session"}),
            ),
            patch.object(
                app_module,
                "_is_codex_running",
                return_value=True,
            ),
            patch.object(app_module.subprocess, "run") as run,
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.post(
                "/api/sessions/member-session/send-keys",
                json={"keys": ["C-c"]},
            )

        assert (response.status_code, run.call_count) == (403, 0)

    def test_member_command_is_rejected_when_codex_is_not_running(self):
        import app as app_module

        member = self._member()
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(
                app_module,
                "_session_owner_id",
                return_value=member["id"],
            ),
            patch.object(
                app_module,
                "_find_session",
                return_value=(0, {"name": "member-session"}),
            ),
            patch.object(
                app_module,
                "_is_codex_running",
                return_value=False,
            ),
            patch.object(app_module.subprocess, "run") as run,
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.post(
                "/api/sessions/member-session/send",
                json={"command": "cat ~/.codex/auth.json"},
            )

        assert (response.status_code, run.call_count) == (409, 0)

    def test_member_prompt_cannot_embed_terminal_control_bytes(self):
        import app as app_module

        member = self._member()
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(
                app_module,
                "_session_owner_id",
                return_value=member["id"],
            ),
            patch.object(
                app_module,
                "_find_session",
                return_value=(0, {"name": "member-session"}),
            ),
            patch.object(
                app_module,
                "_is_codex_running",
                return_value=True,
            ),
            patch.object(app_module.subprocess, "run") as run,
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.post(
                "/api/sessions/member-session/send",
                json={"command": "\u0003"},
            )

        assert (response.status_code, run.call_count) == (403, 0)

    def test_member_file_links_are_confined_to_its_project_root(
        self,
        tmp_path,
    ):
        import app as app_module

        member = self._member()
        projects_root = tmp_path / "projects"
        own_file = (
            projects_root
            / member["username"]
            / "session"
            / "result.txt"
        )
        own_file.parent.mkdir(parents=True)
        own_file.write_text("member result")
        admin_file = tmp_path / "admin-browser-profile.txt"
        admin_file.write_text("admin secret")
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "PROJECTS_ROOT", projects_root),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            own_response = member_client.get(
                "/file",
                params={"path": str(own_file)},
            )
            admin_response = member_client.get(
                "/file",
                params={"path": str(admin_file)},
            )

        assert (
            own_response.status_code,
            own_response.text,
            admin_response.status_code,
            "admin secret" in admin_response.text,
        ) == (200, "member result", 403, False)

    def test_member_project_proxy_cannot_target_admin_cdp(
        self,
        tmp_path,
    ):
        from fastapi.responses import HTMLResponse

        import app as app_module

        member = self._member()
        projects_root = tmp_path / "projects"
        project = projects_root / member["username"] / "member-app"
        project.mkdir(parents=True)
        (project / ".serve.json").write_text('{"port": 9222}')
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "PROJECTS_ROOT", projects_root),
            patch.object(
                app_module,
                "_proxy_to_port",
                new_callable=AsyncMock,
                return_value=HTMLResponse("proxied"),
            ) as proxy,
        ):
            response = TestClient(app).get(
                f"/{member['username']}/member-app/json/version",
            )

        assert (response.status_code, proxy.await_count) == (403, 0)

    def test_member_project_proxy_cannot_target_unowned_local_service(
        self,
        tmp_path,
    ):
        from fastapi.responses import HTMLResponse

        import app as app_module

        member = self._member()
        projects_root = tmp_path / "projects"
        project = projects_root / member["username"] / "member-app"
        project.mkdir(parents=True)
        (project / ".serve.json").write_text('{"port": 45678}')
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "PROJECTS_ROOT", projects_root),
            patch.object(
                app_module,
                "_proxy_to_port",
                new_callable=AsyncMock,
                return_value=HTMLResponse("proxied"),
            ) as proxy,
        ):
            response = TestClient(app).get(
                f"/{member['username']}/member-app/",
            )

        assert (response.status_code, proxy.await_count) == (403, 0)

    def test_member_project_proxy_allows_its_session_listener(
        self,
        tmp_path,
    ):
        from fastapi.responses import HTMLResponse

        import app as app_module

        member = self._member()
        projects_root = tmp_path / "projects"
        project = projects_root / member["username"] / "member-app"
        project.mkdir(parents=True)
        (project / ".serve.json").write_text('{"port": 45678}')
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "PROJECTS_ROOT", projects_root),
            patch.object(
                app_module,
                "_member_project_port_allowed",
                return_value=True,
            ) as allowed,
            patch.object(
                app_module,
                "_proxy_to_port",
                new_callable=AsyncMock,
                return_value=HTMLResponse("proxied"),
            ) as proxy,
        ):
            response = TestClient(app).get(
                f"/{member['username']}/member-app/",
            )

        assert (
            response.status_code,
            response.text,
            allowed.call_args.args,
            proxy.await_count,
        ) == (200, "proxied", (member, "member-app", 45678), 1)

    def test_member_project_listener_must_be_in_its_tmux_process_tree(self):
        import subprocess

        import app as app_module

        member = self._member()

        def process_result(command, **_kwargs):
            if command[0] == "tmux":
                return subprocess.CompletedProcess(command, 0, "100\n", "")
            if command[0] == "ps":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    "100 1\n101 100\n102 101\n999 1\n",
                    "",
                )
            if command[0] == "ss":
                return subprocess.CompletedProcess(
                    command,
                    0,
                    'LISTEN users:(("python",pid=102,fd=3))\n',
                    "",
                )
            raise AssertionError(command)

        with (
            patch.object(
                app_module,
                "_protected_local_service_ports",
                return_value=set(),
            ),
            patch.object(
                app_module,
                "_session_owner_id",
                return_value=member["id"],
            ),
            patch.object(
                app_module.subprocess,
                "run",
                side_effect=process_result,
            ),
        ):
            allowed = app_module._member_project_port_allowed(
                member,
                "member-app",
                45678,
            )

        assert allowed is True

    def test_member_project_directory_symlink_cannot_escape_user_root(
        self,
        tmp_path,
    ):
        import app as app_module

        member = self._member()
        projects_root = tmp_path / "projects"
        member_root = projects_root / member["username"]
        member_root.mkdir(parents=True)
        admin_dir = tmp_path / "admin-private"
        admin_dir.mkdir()
        (admin_dir / "index.html").write_text("admin browser secret")
        (member_root / "member-app").symlink_to(
            admin_dir,
            target_is_directory=True,
        )
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "PROJECTS_ROOT", projects_root),
        ):
            response = TestClient(app).get(
                f"/{member['username']}/member-app/",
            )

        assert (
            response.status_code,
            "admin browser secret" in response.text,
        ) == (403, False)

    def test_member_project_index_symlink_cannot_escape_project(
        self,
        tmp_path,
    ):
        import app as app_module

        member = self._member()
        projects_root = tmp_path / "projects"
        project = projects_root / member["username"] / "member-app"
        project.mkdir(parents=True)
        admin_file = tmp_path / "admin-private.html"
        admin_file.write_text("admin browser secret")
        (project / "index.html").symlink_to(admin_file)
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "PROJECTS_ROOT", projects_root),
        ):
            response = TestClient(app).get(
                f"/{member['username']}/member-app/missing",
            )

        assert (
            response.status_code,
            "admin browser secret" in response.text,
        ) == (403, False)

    def test_non_admin_password_login_uses_configured_cookie_name(self):
        import app as app_module

        member = self._member()
        with (
            patch.object(app_module, "AUTH_COOKIE", "custom_tmux_auth"),
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_save_users"),
        ):
            member_client = TestClient(app)
            login = member_client.post(
                "/login",
                data={"username": member["username"], "password": "member-pass"},
                follow_redirects=False,
            )
            me = member_client.get("/api/me")

        assert (
            login.status_code,
            "custom_tmux_auth" in login.cookies,
            me.headers.get("content-type", "").startswith("application/json"),
        ) == (303, True, True)

    def test_non_admin_cannot_read_global_context_registry(self):
        import app as app_module

        member = self._member()
        with patch.object(app_module, "_load_users", return_value=[member]):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/api/context-files")

        assert response.status_code == 403

    def test_non_admin_cannot_write_global_context_registry(self, tmp_path):
        import app as app_module

        member = self._member()
        registry_file = tmp_path / "global.md"
        registry_file.write_text("admin-only")
        registry = [{
            "id": "global",
            "path": registry_file,
            "load": "auto",
            "note": "Global",
        }]
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_context_file_entries", return_value=registry),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.post(
                "/api/context-files",
                json={"name": "global", "content": "member overwrite"},
            )

        assert (response.status_code, registry_file.read_text()) == (403, "admin-only")

    def test_member_context_hides_managed_global_prompt(self, tmp_path):
        import app as app_module

        member = self._member()
        agents = tmp_path / "AGENTS.md"
        agents.write_text(
            app_module._GLOBAL_CTX_BEGIN
            + "\nadmin-only global prompt\n"
            + app_module._GLOBAL_CTX_END
            + "\n\n# Member context\n"
        )
        (tmp_path / "MEMORY.md").write_text("# Memory\n")
        (tmp_path / "config.toml").write_text('model = "gpt-test"\n')
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_user_codex_config_dir", return_value=tmp_path),
            patch.object(app_module, "_ensure_user_codex_config_dir"),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/api/my/context")

        agents_response = next(
            item["content"] for item in response.json()["files"]
            if item["name"] == "AGENTS.md"
        )
        assert agents_response == "# Member context\n"

    def test_member_can_see_all_context_files_except_global_context(self, tmp_path):
        import app as app_module

        member = self._member()
        (tmp_path / "skills" / "research").mkdir(parents=True)
        (tmp_path / "skills" / "research" / "SKILL.md").write_text("# Research\n")
        (tmp_path / "AGENTS.md").write_text(
            app_module._GLOBAL_CTX_BEGIN
            + "\nadmin-only global prompt\n"
            + app_module._GLOBAL_CTX_END
            + "\n\n# Member context\n"
        )
        (tmp_path / "MEMORY.md").write_text("# Memory\n")
        (tmp_path / "config.toml").write_text('model = "gpt-5"\n')
        (tmp_path / ".mcp.json").write_text("{}\n")
        (tmp_path / "auth.json").write_text('{"secret":"hidden"}\n')

        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_user_codex_config_dir", return_value=tmp_path),
            patch.object(app_module, "_ensure_user_codex_config_dir"),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/api/my/context")

        files = {item["name"]: item for item in response.json()["files"]}
        assert set(files) == {
            "AGENTS.md",
            "MEMORY.md",
            "config.toml",
            ".mcp.json",
            "skills/research/SKILL.md",
        }
        assert "admin-only global prompt" not in files["AGENTS.md"]["content"]
        assert files["skills/research/SKILL.md"]["editable"] is False

    def test_member_context_save_preserves_hidden_global_prompt(self, tmp_path):
        import app as app_module

        member = self._member()
        real_block = (
            app_module._GLOBAL_CTX_BEGIN
            + "\nadmin-only global prompt\n"
            + app_module._GLOBAL_CTX_END
        )
        agents = tmp_path / "AGENTS.md"
        agents.write_text(real_block + "\n\n# Old member context\n")
        submitted = (
            app_module._GLOBAL_CTX_BEGIN
            + "\nmember-supplied fake global prompt\n"
            + app_module._GLOBAL_CTX_END
            + "\n\n# New member context\n"
        )
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_user_codex_config_dir", return_value=tmp_path),
            patch.object(app_module, "_ensure_user_codex_config_dir"),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.post(
                "/api/my/context/AGENTS.md",
                json={"content": submitted},
            )

        assert (response.status_code, agents.read_text()) == (
            200,
            real_block + "\n\n# New member context\n",
        )

    def test_member_browser_status_is_safe_and_reports_working(self):
        import app as app_module

        member = self._member()
        browser = {
            "id": "user-member",
            "owner_id": member["id"],
            "account_browser": True,
            "name": "Member browser",
            "display": 100,
            "rfb_port": 5901,
            "vnc_port": 6081,
            "cdp_port": 9223,
            "notes": "admin-only notes",
            "managed": True,
        }
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_load_browser_sessions", return_value=[browser]),
            patch.object(app_module, "_browser_port_alive", return_value=True),
            patch.object(
                app_module,
                "_browser_signin_state",
                return_value={"signed_in": True, "email": "private@example.com"},
            ),
            patch.object(app_module, "_display_idle_ms", return_value=100),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/api/my/browser")

        assert response.status_code == 200
        data = response.json()
        assert data["state"] == "working"
        assert (data["connected"], data["working"], data["needs_sign_in"]) == (
            True,
            True,
            False,
        )
        assert data["session"] == {
            "id": "user-member",
            "name": "Member browser",
            "running": True,
            "viewer_url": app_module._browser_viewer_url(browser),
        }
        serialized = json.dumps(data)
        for secret_field in (
            "private@example.com",
            "admin-only notes",
            "rfb_port",
            "vnc_port",
            "cdp_port",
            '"display"',
        ):
            assert secret_field not in serialized

    def test_member_browser_status_requires_a_signed_in_browser(self):
        import app as app_module

        member = self._member()
        browser = {
            "id": "user-member",
            "owner_id": member["id"],
            "account_browser": True,
            "name": "Member browser",
            "vnc_port": 6081,
        }
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_load_browser_sessions", return_value=[browser]),
            patch.object(app_module, "_browser_port_alive", return_value=True),
            patch.object(
                app_module,
                "_browser_signin_state",
                return_value={"signed_in": False, "email": ""},
            ),
            patch.object(app_module, "_display_idle_ms", return_value=-1),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/api/my/browser")

        assert response.status_code == 200
        assert response.json() | {} == {
            "session": {
                "id": "user-member",
                "name": "Member browser",
                "running": True,
                "viewer_url": app_module._browser_viewer_url(browser),
            },
            "connected": False,
            "signed_in": False,
            "working": False,
            "needs_sign_in": True,
            "state": "disconnected",
        }

    def test_each_account_receives_its_owned_browser_session(self):
        import app as app_module

        admin = {
            "id": "admin",
            "username": "admin",
            "role": "admin",
        }
        member = self._member()
        sessions = [
            {
                "id": "default",
                "owner_id": "admin",
                "name": "Admin browser",
                "display": 99,
                "vnc_port": 6080,
            },
            {
                "id": "user-member",
                "owner_id": member["id"],
                "account_browser": True,
                "name": "Member browser",
                "display": 100,
                "vnc_port": 6081,
            },
        ]
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(app_module, "_load_browser_sessions", return_value=sessions),
            patch.object(app_module, "_browser_port_alive", return_value=True),
            patch.object(
                app_module,
                "_browser_signin_state",
                return_value={"signed_in": True, "email": "private@example.com"},
            ),
            patch.object(app_module, "_display_idle_ms", return_value=60_000),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token(admin["id"]))
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            admin_response = admin_client.get("/api/my/browser")
            member_response = member_client.get("/api/my/browser")

        assert (
            admin_response.json()["session"]["id"],
            member_response.json()["session"]["id"],
        ) == ("default", "user-member")

    def test_member_browser_is_provisioned_when_the_account_has_none(
        self,
        tmp_path,
    ):
        import app as app_module

        member = self._member()
        sessions_file = tmp_path / "browser-sessions.json"
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "BROWSER_SESSIONS_FILE", sessions_file),
            patch.object(app_module, "CB_ROOT", tmp_path / "browsers"),
            patch.object(app_module, "_ensure_browser_launcher"),
            patch.object(app_module.subprocess, "Popen") as spawn,
            patch.object(app_module, "_browser_port_alive", return_value=False),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/api/my/browser")

        saved = json.loads(sessions_file.read_text())["sessions"]
        owned = next(
            session for session in saved
            if session.get("owner_id") == member["id"]
        )
        assert (
            response.status_code,
            response.json()["session"]["id"],
            owned["managed"],
            spawn.call_count,
        ) == (200, owned["id"], True, 1)

    def test_member_cannot_open_an_undesignated_browser_session(self):
        import app as app_module

        member = self._member()
        sessions = [
            {
                "id": "default",
                "owner_id": "admin",
                "name": "Admin browser",
                "vnc_port": 6080,
            },
            {
                "id": "user-member",
                "owner_id": member["id"],
                "account_browser": True,
                "name": "Member browser",
                "vnc_port": 6081,
            },
        ]
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_load_browser_sessions", return_value=sessions),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/browser/default/vnc.html")

        assert response.status_code == 404

    def test_admin_cannot_open_a_member_owned_browser_session(self):
        import app as app_module

        member = self._member()
        admin = {"id": "admin", "username": "admin", "role": "admin"}
        sessions = [
            dict(app_module._DEFAULT_BROWSER_SESSION),
            {
                "id": "user-member",
                "owner_id": member["id"],
                "account_browser": True,
                "name": "Member browser",
                "vnc_port": 6081,
            },
        ]
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(app_module, "_load_browser_sessions", return_value=sessions),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token(admin["id"]))
            response = admin_client.get("/browser/user-member/vnc.html")

        assert response.status_code == 404

    def test_admin_browser_catalog_omits_member_owned_browsers(self):
        import app as app_module

        member = self._member()
        admin = {"id": "admin", "username": "admin", "role": "admin"}
        sessions = [
            dict(app_module._DEFAULT_BROWSER_SESSION),
            {
                "id": "user-member",
                "owner_id": member["id"],
                "account_browser": True,
                "name": "Member browser",
                "vnc_port": 6081,
            },
        ]
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(app_module, "_load_browser_sessions", return_value=sessions),
            patch.object(app_module, "_browser_port_alive", return_value=True),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token(admin["id"]))
            response = admin_client.get("/api/browser/sessions")

        assert [row["id"] for row in response.json()["sessions"]] == ["default"]

    def test_admin_cannot_drive_member_browser_fingerprint(self):
        import app as app_module

        member = self._member()
        admin = {"id": "admin", "username": "admin", "role": "admin"}
        member_browser = {
            "id": "user-member",
            "owner_id": member["id"],
            "cdp_port": 9223,
        }
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(
                app_module,
                "_load_browser_sessions",
                return_value=[dict(app_module._DEFAULT_BROWSER_SESSION), member_browser],
            ),
            patch.object(app_module.subprocess, "run") as run,
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token(admin["id"]))
            response = admin_client.get("/api/browser/fingerprint/user-member")

        assert (response.status_code, run.call_count) == (404, 0)

    def test_admin_cannot_update_or_delete_member_browser(self):
        import app as app_module

        member = self._member()
        admin = {"id": "admin", "username": "admin", "role": "admin"}
        member_browser = {
            "id": "user-member",
            "owner_id": member["id"],
            "account_browser": True,
            "managed": True,
            "name": "Member browser",
            "vnc_port": 6081,
        }
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(
                app_module,
                "_load_browser_sessions",
                return_value=[
                    dict(app_module._DEFAULT_BROWSER_SESSION),
                    member_browser,
                ],
            ),
            patch.object(app_module, "_save_browser_sessions") as save,
            patch.object(app_module.subprocess, "run") as run,
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token(admin["id"]))
            update = admin_client.patch(
                "/api/browser/sessions/user-member",
                json={"name": "Taken over"},
            )
            delete = admin_client.delete(
                "/api/browser/sessions/user-member",
            )

        assert (
            update.status_code,
            delete.status_code,
            save.call_count,
            run.call_count,
            member_browser["name"],
        ) == (404, 404, 0, 0, "Member browser")

    def test_admin_browser_metadata_update_does_not_modify_member_browser(self):
        import app as app_module

        member = self._member()
        admin = {"id": "admin", "username": "admin", "role": "admin"}
        admin_browser = {
            **dict(app_module._DEFAULT_BROWSER_SESSION),
            "use_for_login": False,
        }
        member_browser = {
            "id": "user-member",
            "owner_id": member["id"],
            "account_browser": True,
            "name": "Member browser",
            "use_for_login": True,
            "vnc_port": 6081,
        }
        sessions = [admin_browser, member_browser]
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(
                app_module,
                "_load_browser_sessions",
                return_value=sessions,
            ),
            patch.object(app_module, "_save_browser_sessions"),
            patch.object(app_module, "_browser_port_alive", return_value=True),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token(admin["id"]))
            response = admin_client.patch(
                "/api/browser/sessions/default",
                json={"use_for_login": True},
            )

        assert (
            response.status_code,
            admin_browser["use_for_login"],
            member_browser["use_for_login"],
        ) == (200, True, True)

    def test_admin_proxy_endpoints_hide_member_browser(self):
        import app as app_module

        member = self._member()
        admin = {"id": "admin", "username": "admin", "role": "admin"}
        sessions = [
            dict(app_module._DEFAULT_BROWSER_SESSION),
            {
                "id": "user-member",
                "owner_id": member["id"],
                "name": "Member browser",
            },
        ]
        proxy_conf = {
            "sessions": {
                "default": {"local_port": 18880, "session_id": "admin"},
                "user-member": {
                    "local_port": 18881,
                    "session_id": "member",
                },
            },
        }
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(
                app_module,
                "_load_browser_sessions",
                return_value=sessions,
            ),
            patch.object(app_module, "_proxy_conf", return_value=proxy_conf),
            patch.object(app_module, "_proxy_usage", return_value={}),
            patch.object(app_module, "_proxy_presets", return_value={}),
            patch.object(app_module, "_proxy_save") as save,
            patch.object(app_module.asyncio, "sleep"),
            patch.object(app_module, "_proxy_exit_info") as exit_info,
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token(admin["id"]))
            catalog = admin_client.get("/api/browser/proxy")
            rotate = admin_client.post(
                "/api/browser/proxy/user-member/rotate",
            )

        assert (
            [row["id"] for row in catalog.json()["browsers"]],
            rotate.status_code,
            save.call_count,
            exit_info.call_count,
        ) == (["default"], 404, 0, 0)

    def test_admin_browser_auth_status_hides_member_browser(self):
        import app as app_module

        member = self._member()
        admin = {"id": "admin", "username": "admin", "role": "admin"}
        sessions = [
            dict(app_module._DEFAULT_BROWSER_SESSION),
            {
                "id": "user-member",
                "owner_id": member["id"],
                "name": "Member browser",
                "vnc_port": 6081,
            },
        ]
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(
                app_module,
                "_load_browser_sessions",
                return_value=sessions,
            ),
            patch.object(app_module, "_browser_port_alive", return_value=True),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token(admin["id"]))
            response = admin_client.get("/api/browser/auth-status")

        assert [row["id"] for row in response.json()["sessions"]] == ["default"]

    def test_member_api_catalog_omits_keys_and_admin_metadata(self):
        import app as app_module

        member = self._member()
        registry = [{
            "id": "api_search",
            "name": "Search API",
            "provider": "search",
            "category": "search",
            "key": "sk-do-not-return-this",
            "env_var": "SEARCH_API_KEY",
            "key_label": "private-account",
            "plan": "Team",
            "limits": "100/day",
            "costs": "$99",
            "notes": "private admin note",
            "docs_url": "https://docs.example.com",
            "dashboard_url": "https://admin.example.com",
            "status": "active",
        }]
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_load_api_registry", return_value=registry),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/api/my/apis")

        assert response.status_code == 200
        entry = response.json()["apis"][0]
        assert entry == {
            "id": "api_search",
            "name": "Search API",
            "provider": "search",
            "category": "search",
            "available": True,
            "plan": "Team",
            "limits": "100/day",
            "docs_url": "https://docs.example.com",
            "status": "active",
        }
        assert "sk-do-not-return-this" not in response.text
        assert "private admin note" not in response.text

    def test_member_api_catalog_rejects_non_http_documentation_links(self):
        import app as app_module

        entry = app_module._member_api_entry({
            "id": "api_bad_link",
            "name": "Bad link",
            "key": "configured",
            "docs_url": 'javascript:alert("xss")',
        })

        assert entry["docs_url"] == ""

    def test_member_cannot_mutate_shared_openai_key(self):
        import app as app_module

        member = self._member()
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_save_openai_key") as save_key,
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.post(
                "/api/auth/api-key",
                json={"apiKey": "sk-member-must-not-set-global-key"},
            )

        assert response.status_code == 403
        save_key.assert_not_called()

    def test_member_cannot_use_full_browser_management_api(self):
        import app as app_module

        member = self._member()
        with patch.object(app_module, "_load_users", return_value=[member]):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token(member["id"]))
            response = member_client.get("/api/browser/sessions")

        assert response.status_code == 403

    def test_fresh_login_opens_browser_reminder_only_when_sign_in_is_missing(self):
        import app as app_module

        member = self._member()
        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_save_users"),
            patch.object(app_module, "_check_login_rate_limit", return_value=True),
            patch.object(app_module, "_browser_login_ready", return_value=False),
        ):
            member_client = TestClient(app)
            missing = member_client.post(
                "/login",
                data={"username": member["username"], "password": "member-pass"},
                follow_redirects=False,
            )

        with (
            patch.object(app_module, "_load_users", return_value=[member]),
            patch.object(app_module, "_save_users"),
            patch.object(app_module, "_check_login_rate_limit", return_value=True),
            patch.object(app_module, "_browser_login_ready", return_value=True),
        ):
            member_client = TestClient(app)
            ready = member_client.post(
                "/login",
                data={"username": member["username"], "password": "member-pass"},
                follow_redirects=False,
            )

        assert "browser_login=1" in missing.headers["location"]
        assert "browser_login=1" not in ready.headers["location"]


class TestAdminUserManagement:
    @staticmethod
    def _admin() -> dict:
        return {
            "id": "admin",
            "username": "admin",
            "role": "admin",
            "created_at": 1,
            "last_login": 2,
        }

    @staticmethod
    def _member() -> dict:
        return {
            "id": "u_member",
            "username": "member@example.com",
            "role": "user",
            "created_at": 1,
            "last_login": 2,
        }

    def test_new_account_gets_a_dedicated_browser_allocation(self):
        import app as app_module

        users = [self._admin()]
        with (
            patch.object(app_module, "_load_users", return_value=users),
            patch.object(app_module, "_save_users"),
            patch.object(app_module, "_new_user_id", return_value="u_new"),
            patch.object(app_module, "_user_data_dir"),
            patch.object(app_module, "_ensure_user_codex_config_dir"),
            patch.object(
                app_module,
                "_ensure_user_browser_session",
            ) as provision_browser,
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            response = admin_client.post(
                "/api/admin/users",
                json={
                    "username": "new@example.com",
                    "password": "member-pass",
                    "role": "user",
                },
            )

        assert (
            response.status_code,
            provision_browser.call_args.args[0]["id"],
            provision_browser.call_args.kwargs,
        ) == (200, "u_new", {"start": False})

    def test_deleted_account_browser_is_stopped_and_removed(self):
        import app as app_module

        users = [self._admin(), self._member()]
        with (
            patch.object(app_module, "_load_users", return_value=users),
            patch.object(app_module, "_save_users"),
            patch.object(app_module, "_load_session_owners", return_value={}),
            patch.object(app_module.shutil, "rmtree"),
            patch.object(
                app_module,
                "_delete_user_browser_session",
                create=True,
            ) as delete_browser,
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            response = admin_client.delete("/api/admin/users/u_member")

        assert (
            response.status_code,
            delete_browser.call_args.args,
        ) == (200, ("u_member",))

    def test_startup_ensures_one_browser_for_every_account(self):
        import app as app_module

        users = [self._admin(), self._member()]
        with (
            patch.object(app_module, "_load_users", return_value=users),
            patch.object(app_module, "_ensure_user_codex_config_dir"),
            patch.object(
                app_module,
                "_ensure_user_browser_session",
            ) as ensure_browser,
        ):
            app_module._ensure_all_user_browser_sessions()

        assert [
            call.args[0]["id"] for call in ensure_browser.call_args_list
        ] == ["admin", "u_member"]

    def test_admin_can_filter_append_only_prompt_audit_by_user(self, tmp_path):
        import app as app_module

        audit_file = tmp_path / "prompt-history.jsonl"
        audit_file.write_text(
            "\n".join([
                json.dumps({
                    "id": "p-admin",
                    "ts": 10,
                    "user_id": "admin",
                    "username": "admin",
                    "session_name": "admin-work",
                    "prompt": "admin prompt",
                }),
                json.dumps({
                    "id": "p-member",
                    "ts": 20,
                    "user_id": "u_member",
                    "username": "member@example.com",
                    "session_name": "member-work",
                    "prompt": "member prompt",
                }),
            ]) + "\n"
        )
        users = [self._admin(), self._member()]
        with (
            patch.object(app_module, "PROMPT_AUDIT_FILE", audit_file),
            patch.object(app_module, "_load_users", return_value=users),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            response = admin_client.get(
                "/api/admin/prompts",
                params={"user_id": "u_member", "limit": 10},
            )

        assert response.status_code == 200
        assert [item["id"] for item in response.json()["prompts"]] == ["p-member"]

    def test_prompt_audit_cursor_does_not_skip_equal_timestamps(self, tmp_path):
        import app as app_module

        audit_file = tmp_path / "prompt-history.jsonl"
        audit_file.write_text("".join(
            json.dumps({
                "id": f"p-{index}",
                "ts": 10,
                "user_id": "admin",
                "username": "admin",
                "session_name": "work",
                "prompt": f"prompt {index}",
            }) + "\n"
            for index in range(4)
        ))
        with (
            patch.object(app_module, "PROMPT_AUDIT_FILE", audit_file),
            patch.object(app_module, "_load_users", return_value=[self._admin()]),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            first = admin_client.get("/api/admin/prompts", params={"limit": 2})
            second = admin_client.get(
                "/api/admin/prompts",
                params={"limit": 2, "cursor": first.json()["next_cursor"]},
            )

        ids = [
            item["id"]
            for page in (first.json(), second.json())
            for item in page["prompts"]
        ]
        assert ids == ["p-3", "p-2", "p-1", "p-0"]

    def test_member_cannot_read_the_global_prompt_audit(self):
        import app as app_module

        users = [self._admin(), self._member()]
        with patch.object(app_module, "_load_users", return_value=users):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token("u_member"))
            response = member_client.get("/api/admin/prompts")

        assert response.status_code == 403

    def test_user_usage_summary_totals_all_rollout_tokens(self, tmp_path):
        import app as app_module

        rollout_dir = tmp_path / "sessions" / "2026" / "07" / "27"
        rollout_dir.mkdir(parents=True)
        rollout = rollout_dir / "rollout-test.jsonl"
        rollout.write_text(
            "\n".join([
                json.dumps({
                    "timestamp": "2026-07-27T10:00:00Z",
                    "type": "session_meta",
                    "payload": {"cwd": "/work/member"},
                }),
                json.dumps({
                    "timestamp": "2026-07-27T10:00:01Z",
                    "type": "turn_context",
                    "payload": {"model": "gpt-5.4"},
                }),
                json.dumps({
                    "timestamp": "2026-07-27T10:01:00Z",
                    "type": "event_msg",
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "last_token_usage": {
                                "input_tokens": 10,
                                "output_tokens": 20,
                                "cached_input_tokens": 30,
                                "reasoning_output_tokens": 40,
                            },
                        },
                    },
                }),
            ]) + "\n"
        )
        member = self._member()
        with patch.object(app_module, "_user_codex_config_dir", return_value=tmp_path):
            usage = app_module._user_usage_summary(member, force=True)

        assert (
            usage["total_tokens"],
            usage["input_tokens"],
            usage["output_tokens"],
            usage["turns"],
            usage["last_active"],
        ) == (100, 10, 20, 1, "2026-07-27T10:01:00Z")

    def test_prompt_audit_backfills_existing_admin_and_member_history_once(self, tmp_path):
        import app as app_module

        users = [self._admin(), self._member()]
        histories = {}
        for user, timestamp in zip(users, (10, 20)):
            path = tmp_path / f"{user['id']}-messages.json"
            path.write_text(json.dumps({
                f"{user['id']}-session": [
                    {"role": "user", "text": f"{user['id']} old prompt", "ts": timestamp},
                    {"role": "assistant", "text": "not a human prompt", "ts": timestamp + 1},
                ],
            }))
            histories[user["id"]] = path

        audit_file = tmp_path / "prompt-history.jsonl"
        marker_file = tmp_path / "prompt-history-backfill-v1.json"
        with (
            patch.object(app_module, "PROMPT_AUDIT_FILE", audit_file),
            patch.object(app_module, "PROMPT_AUDIT_BACKFILL_MARKER", marker_file, create=True),
            patch.object(
                app_module,
                "_user_messages_file",
                side_effect=lambda user: histories[user["id"]],
            ),
        ):
            first = app_module._backfill_prompt_audit(users)
            second = app_module._backfill_prompt_audit(users)

        records = [json.loads(line) for line in audit_file.read_text().splitlines()]
        assert (first, second, len(records)) == (2, 0, 2)
        assert {(row["user_id"], row["ts"], row["source"]) for row in records} == {
            ("admin", 10, "legacy_messages_backfill"),
            ("u_member", 20, "legacy_messages_backfill"),
        }
        assert marker_file.stat().st_mode & 0o777 == 0o600

    def test_admin_user_list_includes_presence_work_tokens_and_prompts(self):
        import app as app_module

        now = time.time()
        users = [self._admin(), self._member()]
        sessions = [
            {"name": "admin-work", "windows": "1", "created": "1", "attached": False},
            {"name": "member-work", "windows": "1", "created": "1", "attached": False},
        ]
        owners = {"admin-work": "admin", "member-work": "u_member"}

        async def activity(session_name):
            return {
                "status": "busy" if session_name == "member-work" else "idle",
                "command": "",
                "detail": "",
            }

        def usage(user):
            total = 500 if user["id"] == "u_member" else 100
            return {
                "total_tokens": total,
                "tokens_7d": total,
                "estimated_cost": total / 1000,
                "estimated_cost_7d": total / 1000,
                "turns": 2,
                "last_active": "2026-07-27T10:00:00Z",
            }

        with (
            patch.object(app_module, "_load_users", return_value=users),
            patch.object(app_module, "get_tmux_sessions", return_value=sessions),
            patch.object(app_module, "_load_session_owners", return_value=owners),
            patch.object(app_module, "async_detect_activity", side_effect=activity),
            patch.object(app_module, "_user_usage_summary", side_effect=usage),
            patch.object(
                app_module,
                "_prompt_audit_summary",
                return_value={
                    "admin": {"count": 1, "last_ts": now - 20},
                    "u_member": {"count": 3, "last_ts": now - 10},
                },
                create=True,
            ),
            patch.object(
                app_module,
                "_user_presence",
                {"admin": now - 5, "u_member": now - 5},
                create=True,
            ),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            response = admin_client.get("/api/admin/users")

        member = next(user for user in response.json()["users"] if user["id"] == "u_member")
        assert (
            member["online"],
            member["working"],
            member["busy_session_count"],
            member["usage"]["total_tokens"],
            member["prompt_count"],
            response.json()["summary"]["working_count"],
        ) == (True, True, 1, 500, 3, 1)

    def test_impersonation_returns_new_window_ticket_without_swapping_cookie(self):
        import app as app_module

        users = [self._admin(), self._member()]
        with patch.object(app_module, "_load_users", return_value=users):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            response = admin_client.post(
                "/api/admin/users/u_member/impersonate",
            )

        assert response.status_code == 200
        assert "impersonate_ticket=" in response.json()["url"]
        assert AUTH_COOKIE not in response.cookies
        assert "tmux_imp_orig" not in response.cookies

    def test_impersonation_ticket_is_one_time_and_header_is_tab_scoped(self):
        import urllib.parse

        import app as app_module

        users = [self._admin(), self._member()]
        with (
            patch.object(app_module, "_load_users", return_value=users),
            patch.object(app_module, "_impersonation_tickets", {}),
            patch.object(app_module, "_impersonation_sessions", {}),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            issued = admin_client.post("/api/admin/users/u_member/impersonate")
            ticket = urllib.parse.parse_qs(
                urllib.parse.urlsplit(issued.json()["url"]).query
            )["impersonate_ticket"][0]

            exchanged = admin_client.post(
                "/api/admin/impersonation/exchange",
                json={"ticket": ticket},
            )
            replay = admin_client.post(
                "/api/admin/impersonation/exchange",
                json={"ticket": ticket},
            )
            original_me = admin_client.get("/api/me")
            member_me = admin_client.get(
                "/api/me",
                headers={"X-Tmux-Impersonate": exchanged.json()["token"]},
            )

        assert exchanged.status_code == 200
        assert replay.status_code in (400, 410)
        assert original_me.json()["id"] == "admin"
        assert (
            member_me.json()["id"],
            member_me.json()["impersonating"],
            member_me.json()["impersonator"],
        ) == ("u_member", True, "admin")

    def test_invalid_impersonation_header_cannot_fall_back_to_admin(self):
        users = [self._admin(), self._member()]
        with patch("app._load_users", return_value=users):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            response = admin_client.get(
                "/api/me",
                headers={"X-Tmux-Impersonate": "expired-or-forged-token"},
            )

        assert response.status_code == 401

    def test_return_to_admin_revokes_tab_impersonation_token(self):
        import urllib.parse

        import app as app_module

        users = [self._admin(), self._member()]
        with (
            patch.object(app_module, "_load_users", return_value=users),
            patch.object(app_module, "_impersonation_tickets", {}),
            patch.object(app_module, "_impersonation_sessions", {}),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            issued = admin_client.post("/api/admin/users/u_member/impersonate")
            ticket = urllib.parse.parse_qs(
                urllib.parse.urlsplit(issued.json()["url"]).query
            )["impersonate_ticket"][0]
            token = admin_client.post(
                "/api/admin/impersonation/exchange",
                json={"ticket": ticket},
            ).json()["token"]
            ended = admin_client.post(
                "/api/impersonation/end",
                headers={"X-Tmux-Impersonate": token},
            )
            replay = admin_client.get(
                "/api/me",
                headers={"X-Tmux-Impersonate": token},
            )

        assert ended.status_code == 200
        assert replay.status_code == 401

    def test_impersonation_session_survives_dashboard_restart(self, tmp_path):
        import urllib.parse

        import app as app_module

        users = [self._admin(), self._member()]
        session_file = tmp_path / "impersonation-sessions.json"
        with (
            patch.object(app_module, "_load_users", return_value=users),
            patch.object(app_module, "IMPERSONATION_SESSIONS_FILE", session_file, create=True),
            patch.object(app_module, "_impersonation_tickets", {}),
            patch.object(app_module, "_impersonation_sessions", {}),
            patch.object(app_module, "_impersonation_sessions_loaded", False, create=True),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            issued = admin_client.post("/api/admin/users/u_member/impersonate")
            ticket = urllib.parse.parse_qs(
                urllib.parse.urlsplit(issued.json()["url"]).query
            )["impersonate_ticket"][0]
            exchanged = admin_client.post(
                "/api/admin/impersonation/exchange",
                json={"ticket": ticket},
            )
            token = exchanged.json()["token"]

            app_module._impersonation_sessions.clear()
            app_module._impersonation_sessions_loaded = False
            after_restart = admin_client.get(
                "/api/me",
                headers={"X-Tmux-Impersonate": token},
            )

        assert session_file.stat().st_mode & 0o777 == 0o600
        assert (after_restart.status_code, after_restart.json()["id"]) == (
            200,
            "u_member",
        )


class TestSecurityHeaders:
    def test_security_headers_present(self, authed_client):
        resp = authed_client.get("/")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"
        assert resp.headers.get("Referrer-Policy") == "strict-origin-when-cross-origin"
        assert resp.headers.get("X-XSS-Protection") == "1; mode=block"

    def test_security_headers_on_api(self, authed_client):
        with patch("app.get_tmux_sessions", return_value=[]):
            resp = authed_client.get("/api/status")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"


class TestDashboardFrontendRegressions:
    def test_terminal_renderer_defines_update_filter_before_use(self, authed_client):
        html = authed_client.get("/").text
        definition = html.index("function _isNoise(line)")
        usage = html.index("if(_isNoise(line))")
        assert definition < usage

    def test_context_files_live_only_inside_settings(self, authed_client):
        html = authed_client.get("/").text
        assert 'id="settings-overlay"' in html
        assert "function openSettings(tab)" in html
        assert "{id:'mycontext', label:'Context Files'}" in html
        assert "{id:'context', label:'System Context'}" in html
        assert 'id="claudemd-overlay"' not in html
        assert 'id="config-overlay"' not in html
        assert "getElementById('config-content')" not in html
        assert "Usage resets on a 5-hour rolling window" not in html

    def test_frontend_never_requests_role_profiles(self, authed_client):
        html = authed_client.get("/").text

        assert "fetch(BASE+'/api/profiles')" not in html

    def test_dashboard_has_no_role_profile_ui(self, authed_client):
        html = authed_client.get("/").text
        forbidden = (
            'class="profile-select"',
            'id="profiles-overlay"',
            "function renderProfileDropdown",
            "function openProfiles",
        )

        assert [token for token in forbidden if token in html] == []

    def test_skills_ui_is_global_not_profile_scoped(self, authed_client):
        html = authed_client.get("/").text
        profile_scoped_copy = (
            "_profileForSession",
            "loaded by Codex in this profile",
            "toggle on/off for this profile",
            "No skills installed for this profile",
        )

        assert [text for text in profile_scoped_copy if text in html] == []

    def test_admin_users_screen_has_live_table_routing_and_tab_impersonation(self, authed_client):
        html = authed_client.get("/").text

        required = (
            "openSettings('users')",
            "#/settings/users",
            "window.addEventListener('hashchange',handleAppRoute)",
            "sessionStorage.getItem('tmuxImpersonationToken')",
            "'X-Tmux-Impersonate'",
            "window.open('about:blank'",
            'id="users-summary"',
            'id="users-status-chips"',
            'id="users-top-scroll"',
            'id="users-columns-menu"',
            "user-col-resizer",
            "Total tokens",
            "Prompts",
        )

        assert [token for token in required if token not in html] == []
        assert "window.location.href = BASE + '/';  // reload as the impersonated user" not in html

    def test_context_screen_lists_all_member_files_and_explains_hidden_global_context(self, authed_client):
        html = authed_client.get("/").text

        assert 'id="my-context-file-list"' in html
        assert "Admin-managed global context is applied to every session but hidden here" in html
        assert "global_context_hidden" in html
        assert "read-only" in html

    def test_member_shell_exposes_settings_browser_apis_and_status_reminder(self, authed_client):
        html = authed_client.get("/").text

        required = (
            'id="nav-settings-btn"',
            "{id:'apis',      label:'APIs'}",
            "{id:'browser',   label:'Browser'}",
            "loadApisMember()",
            "fetch(BASE+'/api/my/apis')",
            "fetch(BASE+'/api/my/browser')",
            "if(!categories.includes(category)) categories.push(category);",
            "consumeBrowserLoginReminder()",
            "browser_login",
            ".nav-browser-badge.working .nbb-dot",
            "Browser connected and working",
        )
        assert [token for token in required if token not in html] == []
        assert "if(isAdmin) startBrowserAuthPolling();" not in html
        assert "el.style.display='none'; return;" not in html

    def test_admin_browser_cards_identify_account_ownership(self, authed_client):
        html = authed_client.get("/").text

        assert (
            "s.owner_username||s.owner_id",
            "s.managed&&!s.account_browser",
            "Each account browser has its own persistent Chrome profile",
        ) == tuple(
            token
            for token in (
                "s.owner_username||s.owner_id",
                "s.managed&&!s.account_browser",
                "Each account browser has its own persistent Chrome profile",
            )
            if token in html
        )

    def test_fresh_account_browser_view_reloads_until_novnc_is_ready(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text

        assert (
            "scheduleMemberBrowserRefresh(data)",
            "clearMemberBrowserRefresh()",
            "_memberBrowserRefreshTimer=setTimeout",
        ) == tuple(
            token
            for token in (
                "scheduleMemberBrowserRefresh(data)",
                "clearMemberBrowserRefresh()",
                "_memberBrowserRefreshTimer=setTimeout",
            )
            if token in html
        )


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

    def test_session_response_reads_real_apikey_auth_mode(self, tmp_path):
        import app as app_module

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(json.dumps({
            "auth_mode": "apikey",
            "OPENAI_API_KEY": "sk-test-not-real",
        }))
        session = {
            "name": "test-session",
            "windows": "1",
            "attached": False,
        }
        activity = {"status": "idle", "command": "", "detail": ""}
        with patch("app._session_config_base", return_value=codex_home):
            result = app_module.build_session_response(session, {}, activity)
        assert result["auth_mode"] == "api"


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
        assert resp.status_code == 200
        assert resp.json()["path"].endswith("/uploads/test-session/test.txt")

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_claude_md_missing_session(self, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/nonexistent/codex-md")
        assert resp.status_code == 404

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.get_session_cwd", return_value="/tmp/test-cwd")
    def test_get_claude_md_success_returns_files_list(self, mock_cwd, mock_sessions, authed_client):
        """GET success: returns files list with cwd field (files may or may not exist)."""
        resp = authed_client.get("/api/sessions/test-session/codex-md")
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
        """When CWD AGENTS.md exists but cannot be read, content should be empty string."""
        md_path = tmp_path / "AGENTS.md"
        md_path.write_text("secret content")
        md_path.chmod(0o000)
        try:
            with patch("app.get_session_cwd", return_value=str(tmp_path)):
                resp = authed_client.get("/api/sessions/test-session/codex-md")
            assert resp.status_code == 200
            data = resp.json()
            project_file = next(f for f in data["files"] if f["label"] == "Project")
            assert project_file["exists"] is True
            assert project_file["content"] == ""
        finally:
            md_path.chmod(0o644)

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_get_claude_md_handles_unreadable_home_file(self, mock_sessions, authed_client, tmp_path):
        """When home AGENTS.md exists but cannot be read, content should be empty string."""
        md_path = tmp_path / "AGENTS.md"
        md_path.write_text("home content")
        md_path.chmod(0o000)
        try:
            with patch("app.get_session_cwd", return_value=""), \
                 patch("app.Path.home", return_value=tmp_path):
                resp = authed_client.get("/api/sessions/test-session/codex-md")
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
    def test_create_session_contract_has_no_profile_selector(self):
        import app as app_module

        assert set(app_module.CreateSession.model_fields) == {"name"}

    def test_legacy_profile_mapping_does_not_change_session_codex_home(self):
        import app as app_module

        legacy_roles = {
            "profiles": [
                {"id": "default"},
                {"id": "ui-expert"},
            ],
            "session_profiles": {"test-session": "ui-expert"},
        }
        with (
            patch.object(app_module, "_load_roles", return_value=legacy_roles),
            patch.object(app_module, "_user_for_session", return_value=None),
        ):
            codex_home = app_module._session_config_base("test-session")

        assert codex_home == app_module.CODEX_HOME

    def test_legacy_profile_mapping_is_reported_as_default(self):
        import app as app_module

        legacy_roles = {
            "profiles": [{"id": "default"}, {"id": "ui-expert"}],
            "session_profiles": {"test-session": "ui-expert"},
        }
        with patch.object(app_module, "_load_roles", return_value=legacy_roles):
            profile_id = app_module._get_session_profile_id("test-session")

        assert profile_id == app_module.DEFAULT_PROFILE_ID

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_session_profile_switching_is_retired(self, mock_sessions, authed_client):
        response = authed_client.post(
            "/api/sessions/test-session/profile",
            json={"profile_id": "ui-expert", "restart": False},
        )

        assert response.status_code == 410

    def test_role_profiles_can_no_longer_be_created(self, authed_client):
        import app as app_module

        roles = {
            "profiles": [app_module._default_profile_record()],
            "session_profiles": {},
        }
        with (
            patch.object(app_module, "_load_roles", return_value=roles),
            patch.object(app_module, "_save_roles"),
            patch.object(app_module, "_materialize_profile"),
        ):
            response = authed_client.post(
                "/api/profiles",
                json={"name": "Retired role profile"},
            )

        assert response.status_code == 410

    def test_legacy_role_profiles_are_hidden_from_api(self, authed_client):
        import app as app_module

        legacy_roles = {
            "profiles": [
                app_module._default_profile_record(),
                {"id": "ui-expert", "name": "UI Expert"},
            ],
            "session_profiles": {"test-session": "ui-expert"},
        }
        with patch.object(app_module, "_load_roles", return_value=legacy_roles):
            response = authed_client.get("/api/profiles")

        assert [profile["id"] for profile in response.json()["profiles"]] == ["default"]

    def test_legacy_custom_profile_routes_are_retired(self, authed_client, tmp_path):
        import app as app_module

        legacy_roles = {
            "profiles": [
                app_module._default_profile_record(),
                {"id": "ui-expert", "name": "UI Expert"},
            ],
            "session_profiles": {"test-session": "ui-expert"},
        }
        requests = (
            ("GET", "/api/profiles/ui-expert", {}),
            ("PUT", "/api/profiles/ui-expert", {"json": {}}),
            ("GET", "/api/profiles/ui-expert/skills", {}),
            ("GET", "/api/profiles/ui-expert/skills/library", {}),
            ("POST", "/api/profiles/ui-expert/skills/library/example", {}),
            ("DELETE", "/api/profiles/ui-expert/skills/library/example", {}),
            ("POST", "/api/profiles/ui-expert/skills/example/promote", {}),
            (
                "POST",
                "/api/profiles/ui-expert/skills",
                {"json": {"name": "legacy.md", "content": "unused"}},
            ),
            ("DELETE", "/api/profiles/ui-expert/skills/legacy.md", {}),
            ("GET", "/api/profiles/ui-expert/files", {}),
            (
                "PUT",
                "/api/profiles/ui-expert/file",
                {"json": {"path": "agents/test.md", "content": "unused"}},
            ),
            (
                "GET",
                "/api/profiles/ui-expert/file",
                {"params": {"path": "agents/test.md"}},
            ),
            (
                "DELETE",
                "/api/profiles/ui-expert/file",
                {"params": {"path": "agents/test.md"}},
            ),
            ("GET", "/api/profiles/ui-expert/credentials", {}),
            ("DELETE", "/api/profiles/ui-expert/credentials", {}),
            ("DELETE", "/api/profiles/ui-expert", {}),
        )
        with (
            patch.object(app_module, "_load_roles", return_value=legacy_roles),
            patch.object(app_module, "_save_roles"),
            patch.object(app_module, "_materialize_profile"),
            patch.object(
                app_module,
                "_profile_dir",
                side_effect=lambda profile_id: tmp_path / f".codex-{profile_id}",
            ),
        ):
            responses = [
                authed_client.request(method, path, **kwargs)
                for method, path, kwargs in requests
            ]

        assert [response.status_code for response in responses] == [410] * len(requests)

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

    @patch("app._ensure_codex_auth_with_fallback")
    @patch("app._codex_cli_readiness", return_value=(True, "ready", {}))
    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.subprocess.run")
    def test_create_session_explains_hidden_tmux_name_collision(
        self, mock_run, mock_sessions, mock_readiness, mock_auth, authed_client
    ):
        def run_tmux(args, **kwargs):
            if args[:2] == ["tmux", "list-sessions"]:
                return MagicMock(returncode=0, stdout="hidden-test\n", stderr="")
            return MagicMock(returncode=1, stdout="", stderr="duplicate session: hidden-test")

        mock_run.side_effect = run_tmux
        resp = authed_client.post(
            "/api/sessions/create",
            json={"name": "hidden-test"},
        )

        assert (resp.status_code, resp.json()["error"]) == (
            409,
            "A tmux session named 'hidden-test' already exists but is hidden from this Codex dashboard.",
        )

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
        expected_keys = {"cpu_load", "memory", "disk", "tmux_sessions", "codex_processes"}
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
        assert "codex_processes" in data

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


# ─── Codex Auth Endpoints ───


class TestCodexAuthEndpoints:
    @pytest.fixture(autouse=True)
    def _isolated_codex_auth(self, tmp_path):
        """Credential endpoint tests must never touch the developer's real home."""
        import app as app_module

        codex_home = tmp_path / ".codex"
        key_file = tmp_path / "state" / "openai_api_key"
        previous_cache = dict(app_module._codex_auth_cache)
        previous_fallback = dict(app_module._codex_auth_fallback_state)
        with (
            patch.object(app_module, "CODEX_HOME", codex_home),
            patch.object(app_module, "MESSAGES_DIR", tmp_path / "state"),
            patch.object(app_module, "OPENAI_KEY_FILE", key_file),
            patch.object(app_module, "_stored_openai_key", ""),
        ):
            app_module._codex_auth_cache.update({"ts": 0, "data": {}})
            app_module._codex_auth_fallback_state.update({
                "path": "", "reason": "", "ts": 0.0,
            })
            yield
        app_module._codex_auth_cache.clear()
        app_module._codex_auth_cache.update(previous_cache)
        app_module._codex_auth_fallback_state.clear()
        app_module._codex_auth_fallback_state.update(previous_fallback)

    def test_codex_status(self, authed_client):
        resp = authed_client.get("/api/auth/codex-status")
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

    @patch("app._save_openai_key")
    def test_set_key_valid(self, mock_save, authed_client):
        resp = authed_client.post(
            "/api/auth/api-key",
            json={"apiKey": "sk-test-key-12345"},

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

    def test_codex_status_has_stable_schema(self, authed_client):
        resp = authed_client.get("/api/auth/codex-status")
        assert resp.status_code == 200
        data = resp.json()
        assert "hasApiKey" in data
        assert "authMode" in data
        assert "activeMode" in data
        assert "loggedIn" in data

    def test_missing_chatgpt_tokens_activate_stored_api_fallback(self, tmp_path):
        import app as app_module

        codex_home = tmp_path / "fallback-home"
        codex_home.mkdir()
        auth_path = codex_home / "auth.json"
        auth_path.write_text(json.dumps({"auth_mode": "chatgpt", "tokens": {}}))
        with patch("app._active_openai_key", return_value="sk-fallback-not-real"):
            state = app_module._ensure_codex_auth_with_fallback(codex_home, True)

        assert state["activeMode"] == "apikey"
        assert state["fallbackActive"] is True
        assert "missing" in state["fallbackReason"].lower()
        assert json.loads(auth_path.read_text()) == {
            "auth_mode": "apikey",
            "OPENAI_API_KEY": "sk-fallback-not-real",
        }

    def test_status_payload_reports_active_api_fallback(self):
        import app as app_module

        app_module.CODEX_HOME.mkdir(parents=True)
        (app_module.CODEX_HOME / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {},
        }))
        with patch("app._active_openai_key", return_value="sk-fallback-not-real"):
            status = app_module._codex_auth_display()
        assert status["authMode"] == "apikey"
        assert status["activeMode"] == "apikey"
        assert status["fallbackActive"] is True
        assert status["loggedIn"] is True

    def test_revoked_chatgpt_token_uses_stored_api_fallback(self, tmp_path):
        import app as app_module

        codex_home = tmp_path / "revoked-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "expired-access",
                "refresh_token": "revoked-refresh",
                "id_token": "not-a-jwt",
            },
        }))
        with (
            patch("app._active_openai_key", return_value="sk-fallback-not-real"),
            patch(
                "app._codex_app_server_account_read",
                return_value={"ok": False, "error": "refresh rejected"},
            ),
        ):
            state = app_module._ensure_codex_auth_with_fallback(codex_home, True)

        assert state["activeMode"] == "apikey"
        assert state["fallbackActive"] is True
        assert "revoked" in state["fallbackReason"].lower()

    def test_valid_chatgpt_refresh_remains_active(self, tmp_path):
        import app as app_module

        codex_home = tmp_path / "valid-home"
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(json.dumps({
            "auth_mode": "chatgpt",
            "tokens": {
                "access_token": "valid-access",
                "refresh_token": "valid-refresh",
                "id_token": "not-a-jwt",
            },
        }))
        with (
            patch("app._active_openai_key", return_value="sk-fallback-not-real"),
            patch(
                "app._codex_app_server_account_read",
                return_value={
                    "ok": True,
                    "account": {
                        "type": "chatgpt",
                        "email": "person@example.com",
                        "planType": "pro",
                    },
                },
            ),
            patch("app._write_codex_api_auth") as write_fallback,
        ):
            state = app_module._ensure_codex_auth_with_fallback(codex_home, True)

        assert state["activeMode"] == "chatgpt"
        assert state["fallbackActive"] is False
        write_fallback.assert_not_called()

    @patch("app._start_codex_chatgpt_login")
    def test_chatgpt_device_login_endpoint_surfaces_url_and_code(
        self, mock_start, authed_client
    ):
        mock_start.return_value = {
            "status": "pending",
            "verificationUrl": "https://auth.openai.com/codex/device",
            "userCode": "ABCD-1234",
            "loginId": "login-1",
            "expiresAt": 123456,
            "error": "",
        }
        resp = authed_client.post("/api/auth/chatgpt/start")
        assert resp.status_code == 200
        assert resp.json()["verificationUrl"] == "https://auth.openai.com/codex/device"
        assert resp.json()["userCode"] == "ABCD-1234"


# ─── AGENTS.md Path Traversal Protection ───


class TestClaudeMdSaveEndpoint:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_rejects_non_claude_md_path(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/test-session/codex-md",
            json={"path": "/etc/passwd", "content": "pwned"},

        )
        assert resp.status_code == 400

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_rejects_path_outside_home(self, mock_sessions, authed_client):
        resp = authed_client.post(
            "/api/sessions/test-session/codex-md",
            json={"path": "/etc/AGENTS.md", "content": "pwned"},

        )
        assert resp.status_code == 403

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_rejects_traversal_attack(self, mock_sessions, authed_client):
        from pathlib import Path
        evil_path = str(Path.home() / ".." / "etc" / "AGENTS.md")
        resp = authed_client.post(
            "/api/sessions/test-session/codex-md",
            json={"path": evil_path, "content": "pwned"},

        )
        assert resp.status_code == 403

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_save_claude_md_missing_session_returns_404(self, mock_sessions, authed_client):
        """POST to a non-existent session should return 404."""
        resp = authed_client.post(
            "/api/sessions/no-such-session/codex-md",
            json={"path": "/home/user/AGENTS.md", "content": "test"},
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "Session not found"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_save_claude_md_success(self, mock_sessions, authed_client, tmp_path):
        """POST with a valid in-home AGENTS.md path should write the file and return ok."""
        from pathlib import Path as RealPath
        target = str(tmp_path / "AGENTS.md")
        with patch("app.Path.home", return_value=tmp_path):
            resp = authed_client.post(
                "/api/sessions/test-session/codex-md",
                json={"path": target, "content": "# hello"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert (tmp_path / "AGENTS.md").read_text() == "# hello"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_save_claude_md_non_slash_name_returns_400(self, mock_sessions, authed_client, tmp_path):
        """Path that passes endswith('AGENTS.md') but not endswith('/AGENTS.md') should return 400."""
        # e.g. /home/user/sub/prefixAGENTS.md ends with "AGENTS.md" but not "/AGENTS.md"
        target = str(tmp_path / "prefixAGENTS.md")
        with patch("app.Path.home", return_value=tmp_path):
            resp = authed_client.post(
                "/api/sessions/test-session/codex-md",
                json={"path": target, "content": "bad"},
            )
        assert resp.status_code == 400
        assert "Invalid path" in resp.json()["error"]

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.os.makedirs", side_effect=OSError("disk full"))
    def test_save_claude_md_write_failure_returns_500(self, mock_write, mock_sessions, authed_client, tmp_path):
        """A write failure during AGENTS.md save should return 500."""
        target = str(tmp_path / "AGENTS.md")
        with patch("app.Path.home", return_value=tmp_path):
            resp = authed_client.post(
                "/api/sessions/test-session/codex-md",
                json={"path": target, "content": "oops"},
            )
        assert resp.status_code == 500
        assert "error" in resp.json()


class TestContextFileRegistry:
    def test_candidate_registry_prefers_existing_codex_then_claude(self, tmp_path):
        import app as app_module

        preferred = tmp_path / "CODEX_GITHUB_RULES.md"
        fallback = tmp_path / "CLAUDE_GITHUB_RULES.md"
        fallback.write_text("legacy rules")
        configured = [{
            "id": "github-rules",
            "paths": [preferred, fallback],
            "load": "ondemand",
            "note": "Git rules",
        }]
        with (
            patch.object(app_module, "_CONTEXT_FILES", configured),
            patch.object(app_module, "_INFRA_DETAIL_DIRS", [tmp_path / "no-infra"]),
        ):
            entries = app_module._context_file_entries()
            assert entries[0]["path"] == fallback

            preferred.write_text("codex rules")
            entries = app_module._context_file_entries()
            assert entries[0]["path"] == preferred

    def test_registry_contains_secret_full_context(self):
        import app as app_module

        entry = next(e for e in app_module._CONTEXT_FILES if e["id"] == "full-context")
        assert entry["load"] == "ondemand"
        assert entry["secret"] is True
        assert entry["paths"][-1].name == "CLAUDE_FULL_CONTEXT.md"


class TestBrowserExternalUrl:
    def test_allocated_accounts_have_distinct_displays_ports_and_profiles(
        self,
        tmp_path,
    ):
        import app as app_module

        with (
            patch.object(
                app_module,
                "BROWSER_SESSIONS_FILE",
                tmp_path / "browser-sessions.json",
            ),
            patch.object(app_module, "CB_ROOT", tmp_path / "browsers"),
        ):
            first = app_module._ensure_user_browser_session(
                {"id": "u_first", "username": "first"},
                start=False,
            )
            second = app_module._ensure_user_browser_session(
                {"id": "u_second", "username": "second"},
                start=False,
            )
            first_profile = app_module._browser_profile_dir(first)
            second_profile = app_module._browser_profile_dir(second)

        assert (
            first["id"] != second["id"],
            first["display"] != second["display"],
            first["vnc_port"] != second["vnc_port"],
            first["cdp_port"] != second["cdp_port"],
            first_profile != second_profile,
        ) == (True, True, True, True, True)

    def test_account_browser_reclaims_its_proxy_port_from_a_stale_user(
        self,
        tmp_path,
    ):
        import app as app_module

        proxy_conf = {
            "sessions": {
                "acct-deleted-user": {
                    "local_port": 3129,
                    "session_id": "old-sticky",
                    "enabled": True,
                },
                "unrelated": {
                    "local_port": 3199,
                    "session_id": "keep-me",
                    "enabled": True,
                },
            },
        }
        with (
            patch.object(
                app_module,
                "BROWSER_SESSIONS_FILE",
                tmp_path / "browser-sessions.json",
            ),
            patch.object(app_module, "CB_ROOT", tmp_path / "browsers"),
            patch.object(
                app_module,
                "BROWSER_PROXY_CONF",
                tmp_path / "proxy.json",
            ),
            patch.object(app_module, "_proxy_conf", return_value=proxy_conf),
            patch.object(app_module, "_proxy_save") as save_proxy,
            patch.object(app_module, "_browser_port_alive", return_value=True),
        ):
            session = app_module._ensure_user_browser_session(
                {"id": "u_replacement", "username": "replacement"},
            )

        saved_sessions = save_proxy.call_args.args[0]["sessions"]
        assert (
            saved_sessions.get(session["id"], {}).get("local_port"),
            "acct-deleted-user" in saved_sessions,
            saved_sessions.get("unrelated", {}).get("session_id"),
        ) == (3129, False, "keep-me")

    def test_deleting_account_browser_releases_its_proxy_identity(
        self,
        tmp_path,
    ):
        import app as app_module

        account_browser = {
            "id": "acct-member",
            "owner_id": "u_member",
            "account_browser": True,
            "managed": True,
        }
        proxy_conf = {
            "sessions": {
                "acct-member": {
                    "local_port": 3129,
                    "session_id": "member-sticky",
                },
                "unrelated": {
                    "local_port": 3130,
                    "session_id": "keep-me",
                },
            },
        }
        with (
            patch.object(
                app_module,
                "_load_browser_sessions",
                return_value=[
                    dict(app_module._DEFAULT_BROWSER_SESSION),
                    account_browser,
                ],
            ),
            patch.object(app_module, "_save_browser_sessions"),
            patch.object(app_module, "CB_ROOT", tmp_path / "browsers"),
            patch.object(app_module.subprocess, "run"),
            patch.object(app_module, "_proxy_conf", return_value=proxy_conf),
            patch.object(app_module, "_proxy_save") as save_proxy,
        ):
            app_module._delete_user_browser_session("u_member")

        saved_sessions = save_proxy.call_args.args[0]["sessions"]
        assert (
            "acct-member" in saved_sessions,
            saved_sessions.get("unrelated", {}).get("session_id"),
        ) == (False, "keep-me")

    def test_account_browsers_do_not_consume_the_admin_extra_browser_cap(self):
        import app as app_module

        sessions = [dict(app_module._DEFAULT_BROWSER_SESSION)]
        sessions.extend(
            {
                "id": f"acct-{index}",
                "owner_id": f"u_{index}",
                "account_browser": True,
                "managed": True,
                "slot": index,
                "display": 99 + index,
                "rfb_port": 5900 + index,
                "vnc_port": 6080 + index,
                "cdp_port": 9222 + index,
            }
            for index in range(1, app_module.BROWSER_MAX_EXTRA + 1)
        )
        with (
            patch.object(app_module, "_load_browser_sessions", return_value=sessions),
            patch.object(app_module, "_save_browser_sessions"),
            patch.object(app_module, "_ensure_browser_launcher"),
            patch.object(app_module.subprocess, "Popen"),
            patch.object(app_module, "_browser_port_alive", return_value=True),
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            response = admin_client.post(
                "/api/browser/sessions",
                json={"name": "Admin extra"},
            )

        assert response.status_code == 200

    def test_other_accounts_browser_is_hidden_from_admin_delete(self):
        import app as app_module

        account_browser = {
            "id": "acct-member",
            "owner_id": "u_member",
            "account_browser": True,
            "managed": True,
        }
        with (
            patch.object(
                app_module,
                "_load_browser_sessions",
                return_value=[
                    dict(app_module._DEFAULT_BROWSER_SESSION),
                    account_browser,
                ],
            ),
            patch.object(app_module, "_save_browser_sessions") as save_sessions,
            patch.object(app_module.subprocess, "run") as stop_browser,
        ):
            admin_client = TestClient(app)
            admin_client.cookies.set(AUTH_COOKIE, _make_token("admin"))
            response = admin_client.delete(
                "/api/browser/sessions/acct-member",
            )

        assert (
            response.status_code,
            save_sessions.call_count,
            stop_browser.call_count,
        ) == (404, 0, 0)

    def test_direct_url_uses_this_dashboard_host_and_root_path(self):
        import app as app_module

        session = {"id": "default"}
        with (
            patch.object(app_module, "PUBLIC_BASE_URL", "https://grabo.tech/"),
            patch.object(app_module, "ROOT_PATH", "/codex"),
        ):
            url = app_module._browser_external_url(session)
        assert url.startswith("https://grabo.tech/codex/browser/default/vnc.html?")
        assert "path=codex/browser/default/websockify" in url
        assert "rotem.ai" not in url

    def test_direct_url_is_omitted_without_public_base(self):
        import app as app_module

        with patch.object(app_module, "PUBLIC_BASE_URL", ""):
            row = app_module._browser_response_row({"id": "default"})
        assert "external_url" not in row

    def test_browser_profile_signin_reads_chrome_account_without_tokens(self, tmp_path):
        import app as app_module

        profile = tmp_path / "profile"
        chrome_profile = profile / "Profile 7"
        chrome_profile.mkdir(parents=True)
        (profile / "Local State").write_text(json.dumps({
            "profile": {
                "last_used": "Profile 7",
                "info_cache": {
                    "Profile 7": {
                        "user_name": "person@example.com",
                        "gaia_id": "gaia-123",
                    },
                },
            },
        }))
        (chrome_profile / "Preferences").write_text(json.dumps({
            "account_info": [{
                "email": "person@example.com",
                "gaia": "gaia-123",
                "refresh_token": "must-never-be-returned",
            }],
        }))

        with patch.object(app_module, "CB_ROOT", tmp_path):
            state = app_module._browser_signin_state({"id": "default"})

        assert state == {"signed_in": True, "email": "person@example.com"}


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
    def test_returns_empty_when_sessions_dir_not_found(self, mock_cwd, tmp_path):
        """Codex has no matching rollouts when CODEX_HOME/sessions is absent."""
        import app

        with patch("app._profile_dir", return_value=tmp_path / ".codex"):
            result = app._find_session_jsonl_files("no-dir-session")
        assert result == []

    @patch("app.get_session_cwd", return_value="/home/user/myproject")
    def test_returns_rollout_when_session_meta_cwd_matches(self, mock_cwd, tmp_path):
        """Codex rollout discovery uses the cwd recorded in session_meta."""
        import app

        sessions = tmp_path / ".codex" / "sessions" / "2026" / "07" / "26"
        sessions.mkdir(parents=True)
        rollout = sessions / "rollout-test.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"cwd": "/home/user/myproject/"},
            }) + "\n"
        )
        with patch("app._profile_dir", return_value=tmp_path / ".codex"):
            result = app._find_session_jsonl_files("has-rollout-session")
        assert result == [str(rollout)]

    @patch("app.get_session_cwd", return_value="/home/user/myproject")
    def test_ignores_rollout_for_different_cwd(self, mock_cwd, tmp_path):
        """A rollout from another workspace must not leak into session stats."""
        import app

        sessions = tmp_path / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        rollout = sessions / "rollout-other.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"cwd": "/home/user/other-project"},
            }) + "\n"
        )
        with patch("app._profile_dir", return_value=tmp_path / ".codex"):
            result = app._find_session_jsonl_files("other-rollout-session")
        assert result == []


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
    @pytest.fixture(autouse=True)
    def _codex_cli_is_ready(self):
        """Keep health tests focused on the individual dependency under test."""
        with patch(
            "app._codex_cli_readiness",
            return_value=(True, "ready", {"version": "0.145.0"}),
        ):
            yield

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
        """The upload handler rejects an oversized body after reading it."""
        import app as _app

        class FakeLargeFile:
            filename = "big.bin"

            async def read(self):
                return b"x" * (51 * 1024 * 1024)

        with patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS), \
             patch("app.get_session_cwd", return_value="/tmp"):
            resp = await _app.api_upload_file("test-session", FakeLargeFile())

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
    def test_upload_uses_session_storage_when_tmux_cwd_unavailable(self, mock_run, mock_sessions, authed_client):
        """Uploads remain available even if tmux cannot report a workspace cwd."""
        from io import BytesIO
        resp = authed_client.post(
            "/api/sessions/test-session/upload",
            files={"file": ("test.txt", BytesIO(b"data"), "text/plain")},
        )
        assert resp.status_code == 200
        assert resp.json()["path"].endswith("/uploads/test-session/test.txt")

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
    @patch("app.open", side_effect=OSError("disk full"), create=True)
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

    @patch("app.subprocess.run", side_effect=FileNotFoundError("codex not found"))
    def test_logout_subprocess_error_generic(self, mock_run, authed_client):
        resp = authed_client.post("/api/auth/logout")
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        # Error message should be generic, not expose exception detail
        assert "warnings" in data
        assert "codex not found" not in str(data["warnings"])


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

    def test_hsts_and_security_headers_on_unauthenticated_login(self, client):
        """The auth middleware's direct login response must keep HTTPS headers."""
        resp = client.get("/", headers={"x-forwarded-proto": "https"})
        assert "max-age=" in resp.headers.get("Strict-Transport-Security", "")
        assert resp.headers.get("X-Content-Type-Options") == "nosniff"
        assert resp.headers.get("X-Frame-Options") == "SAMEORIGIN"


# ─── Codex Usage Endpoint ───


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


class TestCodexRateLimitsEndpoint:
    @pytest.fixture(autouse=True)
    def _reset_limits_cache(self):
        import app as app_module

        previous = dict(app_module._openai_limits_cache)
        app_module._openai_limits_cache.update({"ts": 0, "data": None})
        yield
        app_module._openai_limits_cache.clear()
        app_module._openai_limits_cache.update(previous)

    @patch("app._ensure_codex_auth_with_fallback")
    def test_api_key_mode_has_no_fake_plan_windows(self, mock_auth, authed_client):
        mock_auth.return_value = {
            "activeMode": "apikey",
            "account": {},
        }
        resp = authed_client.get("/api/usage/limits")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_mode"] == "apikey"
        assert data["billing_mode"] == "pay_as_you_go"
        assert data["windows"] == []
        assert "five_hour" not in data
        assert "seven_day" not in data
        assert "soft_limit" not in json.dumps(data)

    @patch("app._codex_app_server_rate_limits")
    @patch("app._ensure_codex_auth_with_fallback")
    def test_chatgpt_mode_uses_codex_reported_window_durations(
        self, mock_auth, mock_limits, authed_client
    ):
        mock_auth.return_value = {
            "activeMode": "chatgpt",
            "account": {"type": "chatgpt", "planType": "pro"},
        }
        mock_limits.return_value = {
            "rateLimits": {
                "planType": "pro",
                "limitId": "codex",
                "primary": {
                    "usedPercent": 12,
                    "windowDurationMins": 180,
                    "resetsAt": 1_800_000_000,
                },
                "secondary": {
                    "usedPercent": 34,
                    "windowDurationMins": 14_400,
                    "resetsAt": 1_800_086_400,
                },
            }
        }
        resp = authed_client.get("/api/usage/limits")
        assert resp.status_code == 200
        data = resp.json()
        assert data["auth_mode"] == "chatgpt"
        assert data["billing_mode"] == "plan"
        assert data["plan_type"] == "pro"
        assert [(window["label"], window["utilization"]) for window in data["windows"]] == [
            ("3h", 12),
            ("10d", 34),
        ]
        assert all(window["resets_at"].endswith("Z") for window in data["windows"])


class TestParseUsageFile:
    def test_parses_codex_token_deltas_and_agent_messages(self, tmp_path):
        """Only Codex token_count deltas and agent messages are counted."""
        import json
        from datetime import datetime, timezone

        import app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        jsonl_file = tmp_path / "test.jsonl"
        jsonl_file.write_text(
            json.dumps({
                "type": "event_msg",
                "timestamp": f"{today}T12:00:00Z",
                "payload": {"type": "agent_message", "message": "Done"},
            }) + "\n" +
            json.dumps({
                "type": "event_msg",
                "timestamp": f"{today}T12:01:00Z",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": {
                        "input_tokens": 100,
                        "output_tokens": 50,
                        "cached_input_tokens": 20,
                        "reasoning_output_tokens": 10,
                    }},
                },
            }) + "\n" +
            # A cumulative snapshot without a last-turn delta must be ignored.
            json.dumps({
                "type": "event_msg",
                "timestamp": f"{today}T12:02:00Z",
                "payload": {"type": "token_count", "info": {"total_token_usage": {
                    "input_tokens": 9999,
                }}},
            }) + "\n"
        )
        result = app._parse_usage_file(str(jsonl_file), today)
        assert result == (100, 50, 20, 10, 1)


# ─── Create Session Tests ───


class TestCreateSession:
    @pytest.fixture(autouse=True)
    def _codex_cli_is_ready(self):
        with (
            patch(
                "app._codex_cli_readiness",
                return_value=(True, "ready", {"version": "0.145.0"}),
            ),
            patch(
                "app._ensure_codex_auth_with_fallback",
                return_value={"activeMode": "apikey", "loggedIn": True},
            ),
        ):
            yield

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
    @patch("app._stored_openai_key", "sk-test-not-real")
    def test_create_session_does_not_put_api_key_in_tmux_command(self, mock_run, authed_client):
        """Codex reads auth.json or the service env; tmux commands never expose the key."""
        sessions_before = [{"name": "keyed-session", "windows": "1", "created": "0", "attached": False}]
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.get_tmux_sessions", side_effect=[[], sessions_before]):
            resp = authed_client.post("/api/sessions/create", json={"name": "keyed-session"})
        assert resp.status_code == 200
        calls_str = [str(c) for c in mock_run.call_args_list]
        assert not any("sk-test-not-real" in c for c in calls_str)

    @patch("app.subprocess.run")
    @patch("app.NEW_SESSION_CMD", "codex --dangerously-bypass-approvals-and-sandbox")
    def test_create_session_sends_new_session_cmd(self, mock_run, authed_client):
        """When NEW_SESSION_CMD is set, session creation should send it to the new pane."""
        sessions_before = [{"name": "cmd-session", "windows": "1", "created": "0", "attached": False}]
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with patch("app.get_tmux_sessions", side_effect=[[], sessions_before]):
            resp = authed_client.post("/api/sessions/create", json={"name": "cmd-session"})
        assert resp.status_code == 200
        calls_str = [str(c) for c in mock_run.call_args_list]
        assert any("codex --dangerously-bypass-approvals-and-sandbox" in c for c in calls_str)

    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.subprocess.run", side_effect=Exception("tmux daemon crashed"))
    def test_create_session_exception_returns_500(self, mock_run, mock_sessions, authed_client):
        """An unexpected exception in create should return 500."""
        resp = authed_client.post("/api/sessions/create", json={"name": "crash-session"})
        assert resp.status_code == 500
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_default_profile_model_change_preserves_global_agents(
        self, mock_sessions, authed_client, tmp_path
    ):
        """An empty built-in profile field must never erase ~/.codex/AGENTS.md."""
        import app as app_module

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        agents_path = codex_home / "AGENTS.md"
        original = b"# Global Codex instructions\n\nKeep this context byte-identical.\n"
        agents_path.write_bytes(original)
        profile = {
            "id": app_module.DEFAULT_PROFILE_ID,
            "name": "Default",
            "model": app_module.DEFAULT_MODEL,
            "effort": "max",
            "codex_md": "",
            "memory_md": "",
            "env": {},
        }
        roles = {"profiles": [profile], "session_profiles": {}}

        with (
            patch("app._load_roles", return_value=roles),
            patch("app._save_roles"),
            patch("app._profile_dir", return_value=codex_home),
            patch(
                "app._get_session_profile_id",
                return_value=app_module.DEFAULT_PROFILE_ID,
            ),
            patch("app._async_is_codex_running", new=AsyncMock(return_value=False)),
            patch("app._send_profile_export", return_value=True),
        ):
            resp = authed_client.post(
                "/api/sessions/test-session/model",
                json={"model": "gpt-5.6-sol", "restart": False},
            )

        assert resp.status_code == 200
        assert agents_path.read_bytes() == original


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
        with patch("app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            resp = authed_client.post(
                "/api/sessions/test-session/send",
                json={"command": "echo hello"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["sent"] == "echo hello"
        mock_sleep.assert_awaited_once_with(0.25)
        assert [call.args[0][-1] for call in mock_run.call_args_list] == ["echo hello", "Enter"]

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_send_saves_admin_prompt_to_append_only_audit(
        self,
        mock_run,
        mock_sessions,
        authed_client,
        tmp_path,
    ):
        import app as app_module

        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        audit_file = tmp_path / "prompt-history.jsonl"
        with (
            patch.object(
                app_module,
                "PROMPT_AUDIT_FILE",
                audit_file,
                create=True,
            ),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
        ):
            response = authed_client.post(
                "/api/sessions/test-session/send",
                json={"command": "audit this admin prompt"},
            )

        records = [json.loads(line) for line in audit_file.read_text().splitlines()]
        assert (
            response.status_code,
            records[0]["user_id"],
            records[0]["session_name"],
            records[0]["prompt"],
        ) == (
            200,
            "admin",
            "test-session",
            "audit this admin prompt",
        )

    @patch("app.subprocess.run")
    def test_send_saves_member_prompt_under_the_member_account(
        self,
        mock_run,
        tmp_path,
    ):
        import app as app_module

        admin = {"id": "admin", "username": "admin", "role": "admin"}
        member = {"id": "u_member", "username": "member@example.com", "role": "user"}
        sessions = [{"name": "member-work", "windows": "1", "created": "1", "attached": False}]
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        audit_file = tmp_path / "prompt-history.jsonl"
        with (
            patch.object(app_module, "_load_users", return_value=[admin, member]),
            patch.object(app_module, "PROMPT_AUDIT_FILE", audit_file),
            patch.object(app_module, "get_tmux_sessions", return_value=sessions),
            patch.object(app_module, "_load_session_owners", return_value={"member-work": "u_member"}),
            patch.object(app_module, "_save_messages"),
            patch.object(app_module, "_is_codex_running", return_value=True),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
        ):
            member_client = TestClient(app)
            member_client.cookies.set(AUTH_COOKIE, _make_token("u_member"))
            response = member_client.post(
                "/api/sessions/member-work/send",
                json={"command": "member-owned prompt"},
            )

        record = json.loads(audit_file.read_text().splitlines()[0])
        assert (response.status_code, record["user_id"], record["prompt"]) == (
            200,
            "u_member",
            "member-owned prompt",
        )

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
    def test_valid_mode_is_managed_globally_without_tmux_secret(self, mock_run, mock_sessions, authed_client):
        """The retired pane toggle cannot place credentials in terminal history."""
        resp = authed_client.post(
            "/api/sessions/test-session/set-auth-mode",
            json={"mode": "api"},
        )
        assert resp.status_code == 409
        assert "configured globally" in resp.json()["error"].lower()
        mock_run.assert_not_called()


# ---------------------------------------------------------------------------
# _parse_session_stats() — direct unit tests (covers lines 2105-2275)
# ---------------------------------------------------------------------------

class TestParseSessionStats:
    """Direct unit tests for _parse_session_stats() token stats computation."""

    @staticmethod
    def _write_jsonl(path, entries):
        flattened = []
        for entry in entries:
            flattened.extend(entry if isinstance(entry, list) else [entry])
        path.write_text("\n".join(json.dumps(e) for e in flattened) + "\n")

    @staticmethod
    def _make_entry(today, model, offset_min=1, inp=1000, out=500, cr=0, cc=0):
        timestamp = f"{today}T12:{offset_min:02d}:00Z"
        return TestParseSessionStats._make_entry_at(
            timestamp, model, inp=inp, out=out, cr=cr, cc=cc
        )

    @staticmethod
    def _make_entry_at(timestamp, model, inp=1000, out=500, cr=0, cc=0):
        usage = {
            "input_tokens": inp,
            "output_tokens": out,
            "cached_input_tokens": cr,
            "reasoning_output_tokens": cc,
            "total_tokens": inp + out + cr + cc,
        }
        return [
            {
                "type": "turn_context",
                "timestamp": timestamp,
                "payload": {"model": model},
            },
            {
                "type": "event_msg",
                "timestamp": timestamp,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": usage,
                        "total_token_usage": usage,
                        "model_context_window": 200_000,
                    },
                },
            },
        ]

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
        self._write_jsonl(jsonl_file, [self._make_entry(today, "gpt-5.6")])

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
            "model": "gpt-5.6",
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
            self._make_entry(today, "gpt-5.6", offset_min=1, inp=1000, out=500),
            self._make_entry(today, "gpt-5.6", offset_min=2, inp=1100, out=600),
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
        assert result["model"] == "gpt-5.6"
        assert result["estimatedCost"] > 0
        assert "_ts" in result

    def test_opus_costs_more_than_sonnet(self, tmp_path):
        from datetime import datetime, timezone

        import app as _app
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        opus_file = tmp_path / "opus.jsonl"
        self._write_jsonl(opus_file, [self._make_entry(today, "gpt-4o", inp=1_000_000, out=0)])

        sonnet_file = tmp_path / "sonnet.jsonl"
        self._write_jsonl(sonnet_file, [self._make_entry(today, "gpt-5.6", inp=1_000_000, out=0)])

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
        self._write_jsonl(haiku_file, [self._make_entry(today, "gpt-5-mini", inp=1_000_000, out=0)])

        sonnet_file = tmp_path / "sonnet2.jsonl"
        self._write_jsonl(sonnet_file, [self._make_entry(today, "gpt-5.6", inp=1_000_000, out=0)])

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
            self._make_entry(today, "gpt-5.6", offset_min=1, inp=50_000, out=100),
            self._make_entry(today, "gpt-5.6", offset_min=2, inp=100_000, out=100),
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
            self._make_entry(today, "gpt-5.6", offset_min=1, inp=500, out=200),
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
            self._make_entry(today, "gpt-5.6", offset_min=3, inp=100, out=50),
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
                "message": {"model": "gpt-5.6"},  # no 'usage' key
            },
            # valid entry to ensure file is processed
            self._make_entry(today, "gpt-5.6", offset_min=2, inp=100, out=50),
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
            "model": "gpt-5.6",
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
        self._write_jsonl(jsonl_file, [
            self._make_entry_at(recent_ts, "gpt-5.6", inp=1000, out=200)
        ])

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
            peak_entries.append(
                self._make_entry_at(ts_i, "gpt-5.6", inp=1000, out=5000)
            )
        recent_entry = self._make_entry_at(
            recent_ts, "gpt-5.6", inp=1000, out=11
        )

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
            peak_entries.append(
                self._make_entry_at(ts_i, "gpt-5.6", inp=100, out=1000)
            )

        # Recent entry: 2 min ago, output=400 → rate_pct=int(400/1000*100)=40 → "limited"
        recent_dt = datetime.fromtimestamp(now - 120, timezone.utc)
        recent_ts = recent_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
        if not recent_ts.startswith(today):
            return
        recent_entry = self._make_entry_at(
            recent_ts, "gpt-5.6", inp=100, out=400
        )

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
        self._write_jsonl(jsonl_file, [
            self._make_entry_at(old_ts, "gpt-5.6", inp=1000, out=5000)
        ])

        name = "stats-no-recent-pct"
        _app._session_stats_cache.pop(name, None)

        with patch("app._find_session_jsonl_files", return_value=[str(jsonl_file)]), \
             patch("app.detect_activity", return_value={"status": "idle"}):
            result = _app._parse_session_stats(name)

        assert result["available"] is True
        assert result["ratePct"] == 0  # line 2234: rate_pct = 0 (no recent data)


# ---------------------------------------------------------------------------
# _is_codex_running() — unit tests
# ---------------------------------------------------------------------------

class TestIsCodexRunning:
    """A pane counts as active only when one of its descendants is Codex."""

    @patch("app.subprocess.run")
    @patch("pathlib.Path.read_bytes", return_value=b"/usr/bin/codex\0")
    @patch("pathlib.Path.read_text", return_value="codex\n")
    def test_returns_true_for_codex_descendant(self, mock_text, mock_bytes, mock_run):
        import app as _app

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="100\n", stderr=""),
            MagicMock(returncode=0, stdout="101\n", stderr=""),
        ]
        assert _app._is_codex_running("test-session") is True

    @patch("app.subprocess.run")
    @patch("pathlib.Path.read_bytes", return_value=b"/usr/bin/node\0server.js\0")
    @patch("pathlib.Path.read_text", return_value="node\n")
    def test_returns_false_for_unrelated_node_descendant(self, mock_text, mock_bytes, mock_run):
        import app as _app

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="100\n", stderr=""),
            MagicMock(returncode=0, stdout="101\n", stderr=""),
            MagicMock(returncode=1, stdout="", stderr=""),
        ]
        assert _app._is_codex_running("test-session") is False

    @patch("app.subprocess.run")
    def test_returns_false_on_nonzero_returncode(self, mock_run):
        import app as _app
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no session")
        assert _app._is_codex_running("missing") is False

    @patch("app.subprocess.run", side_effect=Exception("tmux not found"))
    def test_returns_false_on_exception(self, mock_run):
        import app as _app
        assert _app._is_codex_running("broken-session") is False


# ---------------------------------------------------------------------------
# OpenAI key persistence
# ---------------------------------------------------------------------------

class TestOpenAIKeyFunctions:
    """OpenAI API keys are stored privately and mirrored into Codex auth.json."""

    def test_load_returns_key_from_file(self, tmp_path):
        import app as _app
        key_file = tmp_path / "openai_api_key"
        key_file.write_text("sk-test-not-real\n")
        with patch.object(_app, "OPENAI_KEY_FILE", key_file):
            _app._stored_openai_key = ""
            result = _app._load_openai_key()
        assert result == "sk-test-not-real"

    def test_load_returns_empty_when_file_missing(self, tmp_path):
        import app as _app
        key_file = tmp_path / "openai_api_key"  # does not exist
        with patch.object(_app, "OPENAI_KEY_FILE", key_file):
            _app._stored_openai_key = ""
            result = _app._load_openai_key()
        assert result == ""

    def test_load_handles_read_exception(self, tmp_path):
        import app as _app
        key_file = tmp_path / "openai_api_key"
        with patch.object(_app, "OPENAI_KEY_FILE", key_file), \
             patch.object(key_file.__class__, "exists", return_value=True), \
             patch.object(key_file.__class__, "read_text", side_effect=OSError("denied")):
            _app._stored_openai_key = ""
            result = _app._load_openai_key()
        assert result == ""

    def test_save_writes_key_to_file(self, tmp_path):
        import app as _app
        key_file = tmp_path / "openai_api_key"
        codex_home = tmp_path / ".codex"
        with patch.object(_app, "OPENAI_KEY_FILE", key_file), \
             patch.object(_app, "CODEX_HOME", codex_home), \
             patch("app.MESSAGES_DIR", tmp_path):
            _app._save_openai_key("sk-new-not-real")
        assert key_file.exists()
        assert key_file.read_text() == "sk-new-not-real"
        auth = json.loads((codex_home / "auth.json").read_text())
        assert auth == {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-new-not-real"}
        assert (key_file.stat().st_mode & 0o777) == 0o600
        assert ((codex_home / "auth.json").stat().st_mode & 0o777) == 0o600

    def test_save_handles_write_exception(self, tmp_path):
        import app as _app
        key_file = tmp_path / "openai_api_key"
        with patch.object(_app, "OPENAI_KEY_FILE", key_file), \
             patch("app.MESSAGES_DIR", tmp_path), \
             patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            # Should not raise
            _app._save_openai_key("sk-test-not-real")

    def test_clear_removes_key_file(self, tmp_path):
        import app as _app
        key_file = tmp_path / "openai_api_key"
        key_file.write_text("sk-old-not-real")
        with patch.object(_app, "OPENAI_KEY_FILE", key_file):
            _app._clear_openai_key()
        assert not key_file.exists()
        assert _app._stored_openai_key == ""

    def test_clear_handles_missing_file_gracefully(self, tmp_path):
        import app as _app
        key_file = tmp_path / "openai_api_key"  # does not exist
        with patch.object(_app, "OPENAI_KEY_FILE", key_file):
            _app._clear_openai_key()  # should not raise
        assert _app._stored_openai_key == ""


# ---------------------------------------------------------------------------
# _save_autonomous_state / _load_autonomous_state (lines 128-150)
# ---------------------------------------------------------------------------



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
        with patch.object(_app, "MESSAGES_DIR", tmp_path):
            result = _app._load_all_notes()
        assert result == {}

    def test_messages_load_handles_corrupt_json(self, tmp_path):
        """_load_messages returns {} when messages file is corrupt (lines 520-522)."""
        import app as _app
        messages_file = tmp_path / "messages.json"
        messages_file.write_text("{bad json}")
        with patch.object(_app, "MESSAGES_DIR", tmp_path):
            result = _app._load_messages()
        assert result == {}

    def test_save_notes_writes_to_file(self, tmp_path):
        """_save_notes persists cache entries that have notes (lines 498-507)."""
        import app as _app
        notes_file = tmp_path / "notes.json"
        with patch("app.MESSAGES_DIR", tmp_path), \
             patch.object(_app, "cache", {"sess1": {"notes": "my note"}}):
            _app._save_notes()
        assert notes_file.exists()
        data = json.loads(notes_file.read_text())
        assert data.get("sess1") == "my note"

    def test_save_notes_handles_write_exception(self, tmp_path):
        """_save_notes exception path (lines 506-507)."""
        import app as _app
        with patch("app.MESSAGES_DIR", tmp_path), \
             patch.object(_app, "cache", {"sess1": {"notes": "my note"}}), \
             patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            _app._save_notes()  # should not raise

    def test_save_messages_handles_write_exception(self, tmp_path):
        """_save_messages exception path (lines 537-538)."""
        import app as _app
        with patch("app.MESSAGES_DIR", tmp_path), \
             patch.object(_app, "cache", {"sess1": {"messages": [{"role": "user", "content": "hi"}]}}), \
             patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            _app._save_messages()  # should not raise

    def test_clear_openai_key_handles_unlink_exception(self, tmp_path):
        """Key clearing suppresses filesystem failures but clears memory."""
        import app as _app
        key_file = tmp_path / "openai_api_key"
        key_file.write_text("sk-old-not-real")
        with patch.object(_app, "OPENAI_KEY_FILE", key_file), \
             patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
            _app._clear_openai_key()  # should not raise
        assert _app._stored_openai_key == ""

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
    async def test_timeout_returns_empty_fail_safe(self):
        import asyncio as _asyncio

        import app as _app

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise _asyncio.TimeoutError()

        with patch("app.asyncio.wait_for", fake_wait_for):
            result = await _app.llm_call("sys", "user")

        assert result == ""

    @pytest.mark.asyncio
    async def test_exception_returns_empty_fail_safe(self):
        import app as _app

        async def fake_wait_for(coro, timeout):
            coro.close()
            raise Exception("API failure")

        with patch("app.asyncio.wait_for", fake_wait_for):
            result = await _app.llm_call("sys", "user")

        assert result == ""


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
        """Realtime text is extracted directly from the Codex terminal."""
        import app as _app

        with patch(
            "app.asyncio.to_thread",
            new_callable=AsyncMock,
            return_value="❯ user prompt\n● Realtime update",
        ):
            result = await _app.get_realtime("test-session")

        assert result == "Realtime update"

    @pytest.mark.asyncio
    async def test_get_realtime_summarizes_very_long_codex_text(self):
        import app as _app

        long_output = "❯ prompt\n● " + "word " * 501
        with patch("app.asyncio.to_thread", new_callable=AsyncMock, return_value=long_output), \
             patch("app.AUTO_SUMMARIZER_ENABLED", True), \
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

        with patch("app.AUTO_SUMMARIZER_ENABLED", True), \
             patch("app.capture_pane_full", return_value="terminal output\n" * 50), \
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
# _async_is_codex_running() — async unit test
# ---------------------------------------------------------------------------

class TestAsyncIsCodexRunning:
    """Async unit test for _async_is_codex_running()."""

    @pytest.mark.asyncio
    async def test_delegates_to_sync_function(self):
        import app as _app

        with patch("app.asyncio.to_thread", new_callable=AsyncMock, return_value=True) as mock_thread:
            result = await _app._async_is_codex_running("test-session")

        assert result is True
        mock_thread.assert_called_once_with(_app._is_codex_running, "test-session")


# ---------------------------------------------------------------------------
# _ensure_codex_running() — async unit tests
# ---------------------------------------------------------------------------

class TestEnsureCodexRunning:
    """Unit tests for _ensure_codex_running() — OOM crash recovery."""

    @pytest.mark.asyncio
    async def test_returns_true_if_codex_already_running(self):
        """Line 190-191: already running → return True immediately."""
        import app as _app

        with patch("app._async_is_codex_running", new_callable=AsyncMock, return_value=True):
            result = await _app._ensure_codex_running("my-session")
        assert result is True

    @pytest.mark.asyncio
    async def test_restarts_codex_and_returns_true(self):
        """Lines 189-213: not running → sends restart command → running after 1 poll."""
        import app as _app

        # First call: not running. Second call (during loop): running.
        is_running_values = [False, True]
        log_entries = []
        state = {"enabled": True}

        async def fake_is_running(session_name):
            return is_running_values.pop(0)

        with patch("app._async_is_codex_running", side_effect=fake_is_running), \
             patch("app.asyncio.to_thread", new_callable=AsyncMock), \
             patch("app.asyncio.sleep", new_callable=AsyncMock):
            result = await _app._ensure_codex_running(
                "my-session",
                log_fn=lambda s, msg: log_entries.append(msg),
                state=state,
            )
        assert result is True
        assert any("restarted" in e for e in log_entries)  # covers line 210

    @pytest.mark.asyncio
    async def test_recovery_reapplies_the_session_codex_home_before_launch(self):
        """Crash recovery must not inherit an admin CODEX_HOME in member panes."""
        import app as _app

        is_running_values = [False, True]

        async def fake_is_running(session_name):
            return is_running_values.pop(0)

        with (
            patch(
                "app._async_is_codex_running",
                side_effect=fake_is_running,
            ),
            patch(
                "app.asyncio.to_thread",
                new_callable=AsyncMock,
            ) as to_thread,
            patch("app.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await _app._ensure_codex_running("member-session")

        export_calls = [
            call
            for call in to_thread.await_args_list
            if call.args and call.args[0] is _app._send_profile_export
        ]
        assert result is True
        assert export_calls[0].args[1:] == (
            "member-session",
            _app.DEFAULT_PROFILE_ID,
        )

    @pytest.mark.asyncio
    async def test_returns_false_after_timeout(self):
        """Lines 215-218: not running → sends restart command → never restarts."""
        import app as _app

        async def fake_sleep(duration):
            pass

        with patch("app._async_is_codex_running", new_callable=AsyncMock, return_value=False), \
             patch("app.asyncio.to_thread", new_callable=AsyncMock), \
             patch("app.asyncio.sleep", side_effect=fake_sleep):
            result = await _app._ensure_codex_running("my-session")
        assert result is False

    @pytest.mark.asyncio
    async def test_returns_false_on_exception(self):
        """Lines 219-221: exception during restart attempt."""
        import app as _app

        with patch("app._async_is_codex_running", new_callable=AsyncMock, return_value=False), \
             patch("app.asyncio.to_thread", new_callable=AsyncMock, side_effect=Exception("tmux gone")):
            result = await _app._ensure_codex_running("my-session")
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

        with patch("app._async_is_codex_running", new_callable=AsyncMock, return_value=False), \
             patch("app.asyncio.to_thread", new_callable=AsyncMock), \
             patch("app.asyncio.sleep", side_effect=fake_sleep):
            await _app._ensure_codex_running("my-session", log_fn=lambda s, msg: log_entries.append(msg), state=state)
        assert any("not running" in entry for entry in log_entries)


class TestHostBrowserIsolation:
    def test_proxy_relay_classifies_local_addresses_as_blocked(self):
        import importlib.util
        from pathlib import Path

        relay_path = (
            Path.home() / ".claude-browser" / "bin" / "proxy_relay.py"
        )
        if not relay_path.exists():
            pytest.skip("host browser relay is not installed")
        spec = importlib.util.spec_from_file_location(
            "host_proxy_relay",
            relay_path,
        )
        relay = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(relay)

        assert (
            relay._address_is_public("127.0.0.1"),
            relay._address_is_public("169.254.169.254"),
            relay._address_is_public("10.0.0.1"),
            relay._address_is_public("::1"),
            relay._address_is_public("8.8.8.8"),
        ) == (False, False, False, False, True)
        auth_failure = relay._upstream_failure_response(
            b"HTTP/1.1 407 Proxy Authentication Required"
        )
        assert auth_failure.startswith(b"HTTP/1.1 502 ")
        assert b"407" not in auth_failure

    def test_member_chrome_routes_loopback_through_the_guarded_relay(
        self,
        tmp_path,
    ):
        import subprocess
        from pathlib import Path

        common = (
            Path.home() / ".claude-browser" / "bin" / "chrome-common.sh"
        )
        if not common.exists():
            pytest.skip("host browser launcher is not installed")
        root = tmp_path / ".claude-browser"
        root.mkdir()
        (root / "proxy.json").write_text(
            json.dumps({
                "sessions": {
                    "acct-test": {
                        "local_port": 3129,
                        "enabled": True,
                    },
                },
            })
        )
        command = (
            f"source {common}; "
            "cb_chrome_flags /tmp/member-profile 9223 acct-test"
        )
        result = subprocess.run(
            ["bash", "-c", command],
            env={
                "PATH": os.environ["PATH"],
                "CB_HOME": str(tmp_path),
            },
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )

        assert "--proxy-bypass-list=<-loopback>" in result.stdout.splitlines()


# ─── _watchdog_loop (no-active-sessions path) ───

# ---------------------------------------------------------------------------
# Phase 23 — High-coverage tests for remaining uncovered async paths
# ---------------------------------------------------------------------------


class TestAutoResponderLoop:
    """Tests for _auto_responder_loop (lines 2571-2612)."""

    @pytest.mark.asyncio
    async def test_detects_prompt_and_responds_with_enter(self):
        """Full path: session found, not in cooldown, prompt detected, Enter sent."""
        import asyncio as _asyncio

        import app as _app

        sleep_count = [0]

        async def counting_sleep(secs):
            sleep_count[0] += 1
            # Exit after the initial delay + one interval sleep
            if sleep_count[0] >= 2:
                raise _asyncio.CancelledError()

        mock_capture = MagicMock(returncode=0, stdout="❯ 1. Yes\n2. No\nWould you like to proceed")
        mock_enter = MagicMock(returncode=0, stdout="")

        call_seq = [0]

        async def mock_to_thread(fn, *args, **kwargs):
            call_seq[0] += 1
            # First call = capture-pane, second = send-keys Enter
            if call_seq[0] == 1:
                return mock_capture
            return mock_enter

        _app._auto_respond_cooldown.clear()
        _app._auto_respond_log.clear()
        try:
            with patch("app.get_tmux_sessions", return_value=[{"name": "ar-sess"}]), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.asyncio.to_thread", mock_to_thread), \
                 patch("app._detect_interactive_prompt", return_value="plan_approval"), \
                 patch("app.time.time", return_value=1000.0):
                with pytest.raises(_asyncio.CancelledError):
                    await _app._auto_responder_loop()
        finally:
            _app._auto_respond_cooldown.clear()
            _app._auto_respond_log.clear()

    @pytest.mark.asyncio
    async def test_cooldown_skips_session(self):
        """Lines 2582-2583: session within cooldown window is skipped."""
        import asyncio as _asyncio

        import app as _app

        sleep_count = [0]

        async def counting_sleep(secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise _asyncio.CancelledError()

        to_thread_called = [False]

        async def mock_to_thread(fn, *args, **kwargs):
            to_thread_called[0] = True
            return MagicMock(returncode=0, stdout="")

        _app._auto_respond_cooldown["cd-sess"] = 995.0  # recent → within 10s cooldown
        try:
            with patch("app.get_tmux_sessions", return_value=[{"name": "cd-sess"}]), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.asyncio.to_thread", mock_to_thread), \
                 patch("app.time.time", return_value=1000.0):
                with pytest.raises(_asyncio.CancelledError):
                    await _app._auto_responder_loop()
        finally:
            _app._auto_respond_cooldown.pop("cd-sess", None)

        assert not to_thread_called[0]

    @pytest.mark.asyncio
    async def test_tmux_capture_exception_is_silently_skipped(self):
        """Lines 2590-2591: exception from to_thread capture continues."""
        import asyncio as _asyncio

        import app as _app

        sleep_count = [0]

        async def counting_sleep(secs):
            sleep_count[0] += 1
            if sleep_count[0] >= 2:
                raise _asyncio.CancelledError()

        async def failing_to_thread(fn, *args, **kwargs):
            raise OSError("tmux not available")

        _app._auto_respond_cooldown.clear()
        try:
            with patch("app.get_tmux_sessions", return_value=[{"name": "ex-sess"}]), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.asyncio.to_thread", failing_to_thread), \
                 patch("app.time.time", return_value=1000.0):
                with pytest.raises(_asyncio.CancelledError):
                    await _app._auto_responder_loop()
        finally:
            _app._auto_respond_cooldown.pop("ex-sess", None)

    @pytest.mark.asyncio
    async def test_outer_exception_continues_loop(self):
        """Lines 2608-2609: outer except Exception keeps the loop running."""
        import asyncio as _asyncio

        import app as _app

        sleep_count = [0]

        async def counting_sleep(secs):
            sleep_count[0] += 1
            if sleep_count[0] == 1:
                return  # initial delay passes
            if sleep_count[0] == 2:
                return  # first interval passes (exception will be raised after)
            raise _asyncio.CancelledError()

        call_count = [0]

        async def mock_to_thread(fn, *args, **kwargs):
            call_count[0] += 1
            raise RuntimeError("unexpected failure")

        _app._auto_respond_cooldown.clear()
        try:
            with patch("app.get_tmux_sessions", side_effect=[RuntimeError("boom"), []]), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.asyncio.to_thread", mock_to_thread), \
                 patch("app.time.time", return_value=1000.0):
                with pytest.raises(_asyncio.CancelledError):
                    await _app._auto_responder_loop()
        finally:
            _app._auto_respond_cooldown.clear()
