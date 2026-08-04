"""Focused regression tests for account-owned dashboard sessions."""

import asyncio
import os
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

from fastapi.testclient import TestClient

import app as app_module

ADMIN = {"id": "admin", "username": "admin", "role": "admin"}
MEMBER = {"id": "u_member", "username": "member@example.com", "role": "user"}
SESSIONS = [
    {"name": "admin-work", "windows": "1", "created": "1", "attached": False},
    {"name": "member-work", "windows": "1", "created": "2", "attached": False},
]
OWNERS = {"admin-work": "admin", "member-work": "u_member"}


def _admin_client() -> TestClient:
    client = TestClient(app_module.app)
    client.cookies.set(app_module.AUTH_COOKIE, app_module._make_token("admin"))
    return client


def test_admin_cannot_access_member_session_without_impersonation():
    with patch.object(app_module, "_load_session_owners", return_value=OWNERS):
        allowed = app_module._user_can_access_session(ADMIN, "member-work")

    assert allowed is False


def test_all_scope_cannot_add_member_sessions_to_admin_workspace():
    activity = {"status": "idle", "command": "", "detail": ""}
    with (
        patch.object(app_module, "_load_users", return_value=[ADMIN, MEMBER]),
        patch.object(app_module, "_load_session_owners", return_value=OWNERS),
        patch.object(app_module, "get_tmux_sessions", return_value=SESSIONS),
        patch.object(
            app_module,
            "async_detect_activity",
            AsyncMock(return_value=activity),
        ),
    ):
        response = _admin_client().get("/api/status?scope=all")

    assert [row["name"] for row in response.json()] == ["admin-work"]


def test_admin_history_cannot_open_member_session_without_impersonation():
    with (
        patch.object(app_module, "_load_users", return_value=[ADMIN, MEMBER]),
        patch.object(app_module, "_load_session_owners", return_value=OWNERS),
    ):
        response = _admin_client().get("/api/history/member-work")

    assert response.status_code == 404


def test_dashboard_has_no_all_users_session_switch():
    with patch.object(app_module, "_load_users", return_value=[ADMIN, MEMBER]):
        response = _admin_client().get("/")

    assert "nav-session-scope" not in response.text


def test_impersonated_member_can_access_own_session():
    with patch.object(app_module, "_load_session_owners", return_value=OWNERS):
        allowed = app_module._user_can_access_session(MEMBER, "member-work")

    assert allowed is True


def test_owner_restart_never_injects_shell_commands_into_a_running_codex():
    calls = []

    def record_run(args, **kwargs):
        calls.append(args)
        return Mock(returncode=0, stdout="", stderr="")

    with (
        patch.object(app_module.subprocess, "run", side_effect=record_run),
        patch.object(
            app_module,
            "_async_is_codex_running",
            AsyncMock(return_value=True),
        ),
        patch.object(app_module.asyncio, "sleep", AsyncMock()),
        patch.object(app_module, "_send_session_owner_environment") as export_owner,
    ):
        result = asyncio.run(app_module._restart_codex_for_session("member-work"))

    literal_quits = [
        args for args in calls
        if args[:5] == ["tmux", "send-keys", "-t", "member-work", "-l"]
        and args[-1] == "/quit"
    ]
    assert literal_quits
    assert result == (False, False)
    export_owner.assert_not_called()
    assert not any("resume --last" in " ".join(args) for args in calls)


def test_scoped_member_launch_rebinds_identity_after_login_shell_startup(tmp_path):
    codex_home = tmp_path / ".codex-user-u_member"
    codex_home.mkdir()
    token_path = codex_home / "advisor-token"
    token_path.write_text("do-not-embed-this-value")

    with (
        patch.object(app_module, "_user_for_session", return_value=MEMBER),
        patch.object(app_module, "_user_codex_config_dir", return_value=codex_home),
    ):
        launch = app_module._session_launch_command(
            "member-work",
            "codex --yolo",
            pin_model=False,
        )

    assert f"CODEX_HOME={codex_home}" in launch
    assert str(token_path) in launch
    assert "do-not-embed-this-value" not in launch
