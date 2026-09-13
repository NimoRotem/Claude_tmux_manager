"""Focused regression tests for account-owned dashboard sessions."""

import asyncio
import hashlib
import os
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

from fastapi.testclient import TestClient

import app as app_module

ADMIN = {"id": "admin", "username": "admin", "role": "admin"}
SECONDARY_ADMIN = {
    "id": "u_admin",
    "username": "second-admin@example.com",
    "role": "admin",
}
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

    def binding(name, owner_id):
        return {
            "name": name,
            "owner_id": owner_id,
            "generation": "",
            "session_id": "$1",
            "session_created": "1",
            "managed": False,
            "key": (name, owner_id, "", "$1@1"),
        }

    with (
        patch.object(app_module, "_load_users", return_value=[ADMIN, MEMBER]),
        patch.object(app_module, "_load_session_owners", return_value=OWNERS),
        patch.object(app_module, "get_tmux_sessions", return_value=SESSIONS),
        patch.object(app_module, "_terminal_binding", side_effect=binding),
        patch.object(
            app_module, "_terminal_binding_state", return_value="current"
        ),
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


def test_owner_restart_never_injects_shell_commands_into_a_running_codex(tmp_path):
    calls = []
    thread_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
    generation = "a" * 32
    row = {
        "managed": True,
        "generation": generation,
        "owner_id": MEMBER["id"],
        "desired_state": "running",
        "restore_on_startup": True,
        "resume_uuid": thread_id,
        "cwd": str(tmp_path),
    }

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
        patch.object(
            app_module,
            "_strict_session_owner",
            return_value=(MEMBER["id"], MEMBER),
        ),
        patch.object(app_module, "_checkpoint_active_session", return_value=row),
            patch.object(app_module._session_lifecycle, "get", return_value=row),
            patch.object(app_module._session_lifecycle, "matches", return_value=True),
            patch.object(
                app_module, "_active_session_root_thread_id", return_value=thread_id
            ),
            patch.object(
                app_module, "_validated_session_root_thread_id", return_value=thread_id
            ),
            patch.object(app_module, "_durable_session_cwd", return_value=str(tmp_path)),
            patch.object(app_module, "get_session_cwd", return_value=str(tmp_path)),
            patch.object(app_module, "_exact_tmux_session_id", return_value="$1"),
        patch.object(app_module, "_tmux_session_matches_owner", return_value=True),
    ):
        result = asyncio.run(
            app_module._restart_codex_for_session(
                "member-work", expected_owner_id=MEMBER["id"]
            )
        )

    literal_quits = [
        args for args in calls
        if args[:5] == ["tmux", "send-keys", "-t", "$1:", "-l"]
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
    assert "TMUX_DASH_ACCOUNT_INSTRUCTIONS_SHA=" in launch
    assert str(token_path) in launch
    assert "do-not-embed-this-value" not in launch


def test_secondary_admin_launch_keeps_its_private_codex_identity(tmp_path):
    codex_home = tmp_path / ".codex-user-u_admin"
    codex_home.mkdir()
    (codex_home / "AGENTS.md").write_text("Private administrator instructions.\n")
    token_path = tmp_path / "shared-admin-advisor-token"
    token_path.write_text("do-not-embed-this-value")

    with (
        patch.object(app_module, "_user_for_session", return_value=SECONDARY_ADMIN),
        patch.object(app_module, "_user_codex_config_dir", return_value=codex_home),
        patch.object(app_module, "_account_advisor_token_path", return_value=token_path),
    ):
        launch = app_module._session_launch_command(
            "secondary-admin-work",
            "codex --yolo",
            pin_model=False,
        )

    assert f"CODEX_HOME={codex_home}" in launch
    assert "TMUX_DASH_ACCOUNT_INSTRUCTIONS_SHA=" in launch
    assert str(token_path) in launch
    assert "do-not-embed-this-value" not in launch


def test_stale_open_member_thread_receives_updated_account_instructions_once(tmp_path):
    codex_home = tmp_path / ".codex-user-u_member"
    codex_home.mkdir()
    agents_path = codex_home / "AGENTS.md"
    agents_path.write_text(
        "If the message is only `eli`, rewrite the previous answer in at most 75 words.\n"
    )
    digest = hashlib.sha256(agents_path.read_bytes()).hexdigest()

    with (
        patch.object(app_module, "_user_for_session", return_value=SECONDARY_ADMIN),
        patch.object(app_module, "_user_codex_config_dir", return_value=codex_home),
        patch.object(app_module, "_session_codex_process_id", return_value=321),
        patch.object(app_module, "_process_environment", return_value={}),
        patch.object(
            app_module,
            "_session_account_instruction_marker",
            return_value=("", ""),
        ),
    ):
        wrapped, marker = app_module._account_instruction_refresh_for_prompt(
            "member-work", "eli"
        )

    assert str(agents_path) in wrapped
    assert wrapped.endswith("ORIGINAL:\neli")
    assert "For exact-message rules, use only ORIGINAL" in wrapped
    assert len(wrapped) <= 200
    assert marker == (321, digest)

    with (
        patch.object(app_module, "_user_for_session", return_value=SECONDARY_ADMIN),
        patch.object(app_module, "_user_codex_config_dir", return_value=codex_home),
        patch.object(app_module, "_session_codex_process_id", return_value=321),
        patch.object(app_module, "_process_environment", return_value={}),
        patch.object(
            app_module,
            "_session_account_instruction_marker",
            return_value=("321", digest),
        ),
    ):
        unchanged, marker = app_module._account_instruction_refresh_for_prompt(
            "member-work", "eli"
        )

    assert unchanged == "eli"
    assert marker is None


# ─── Roster: an unowned tmux session is nobody's tab ───

ROSTER_ROWS = [
    {
        "name": "admin-work",
        "windows": "1",
        "created": "1",
        "attached": False,
        "managed": False,
        "owner_id": "admin",
    },
    {
        "name": "member-work",
        "windows": "1",
        "created": "2",
        "attached": False,
        "managed": False,
        "owner_id": "u_member",
    },
    {
        # What the inventory says about a session nobody claimed: its identity
        # fallback names the admin. That must not decide who sees the tab.
        "name": "started-from-a-shell",
        "windows": "1",
        "created": "3",
        "attached": False,
        "managed": False,
        "owner_id": "admin",
    },
]

ROSTER_INVENTORY = {
    "state": "ok",
    "authoritative": True,
    "owners": OWNERS,
    "sessions": ROSTER_ROWS,
    "expected_rows": {},
    "missing_expected": [],
}


def _roster_names(user):
    with patch.object(app_module, "_session_tab_order", return_value=[]):
        return [
            row["name"]
            for row in app_module._roster_sessions_for_user(ROSTER_INVENTORY, user)
        ]


def test_roster_never_hands_an_unowned_session_to_the_admin():
    """The tab strip is built from the roster, not from _filter_sessions_for_user.

    Fixing only the workspace list left this path defaulting an unowned session
    to the admin, so shell sessions belonging to other accounts kept appearing
    in their tab strip.
    """
    assert _roster_names(ADMIN) == ["admin-work"]
    assert _roster_names(SECONDARY_ADMIN) == []
    assert _roster_names(MEMBER) == ["member-work"]


def test_an_unowned_session_cannot_make_the_whole_roster_unavailable():
    """An unowned row can never be bound to a terminal.

    While one was being selected into the admin's roster, _bind_session_response_rows
    failed for the whole request and /api/session-roster answered 503 for every
    tab that account had, not just the unclaimed one.
    """
    for row in app_module._roster_sessions_for_user(ROSTER_INVENTORY, ADMIN):
        assert OWNERS.get(row["name"]) == "admin"


def test_a_managed_row_still_needs_both_its_owner_records_to_agree():
    forged = dict(ROSTER_ROWS[1], managed=True, owner_id="admin")
    inventory = dict(ROSTER_INVENTORY, sessions=[forged])
    with patch.object(app_module, "_session_tab_order", return_value=[]):
        assert app_module._roster_sessions_for_user(inventory, ADMIN) == []


# ─── Recovery: only promise a tab that can actually come back ───

RESUME_UUID = "0199f0a1-2b3c-4d5e-8f60-112233445566"
RECOVERABLE_ROW = {
    "managed": True,
    "owner_id": "admin",
    "desired_state": "running",
    "restore_on_startup": True,
    "generation": "a" * 32,
    "resume_uuid": RESUME_UUID,
}
GHOST_ROW = {key: value for key, value in RECOVERABLE_ROW.items() if key != "resume_uuid"}


def test_a_tab_with_no_resume_thread_can_never_be_recovered():
    assert app_module._session_row_is_recoverable(RECOVERABLE_ROW) is True
    assert app_module._session_row_is_recoverable(GHOST_ROW) is False
    assert app_module._session_row_is_recoverable(dict(RECOVERABLE_ROW, generation="")) is False
    assert app_module._session_row_is_recoverable({}) is False


def _inventory_with_rows(rows):
    listing = Mock(returncode=0, stdout="", stderr="")
    with (
        patch.object(app_module._session_lifecycle, "snapshot", return_value={"sessions": rows}),
        patch.object(app_module, "_load_session_owners", return_value={"ghost": "admin", "live": "admin"}),
        patch.object(app_module.subprocess, "run", return_value=listing),
    ):
        return app_module._tmux_inventory_snapshot()


def test_a_tab_that_cannot_be_restored_is_not_reported_as_recovering():
    """It used to render "Restoring X…" forever with nothing able to finish it.

    Recovery runs once, at controller startup, and drops any row without a
    resume thread. Expecting such a row also held /health/ready at degraded for
    as long as it existed.
    """
    inventory = _inventory_with_rows({"ghost": GHOST_ROW})
    assert inventory["missing_expected"] == []
    assert inventory["expected_rows"] == {}


def test_a_tab_that_can_be_restored_is_still_reported_as_recovering():
    inventory = _inventory_with_rows({"live": RECOVERABLE_ROW})
    assert inventory["missing_expected"] == ["live"]


def test_startup_retires_a_tab_whose_session_is_gone_and_cannot_return():
    rows = {"ghost": dict(GHOST_ROW), "live": dict(RECOVERABLE_ROW)}
    removed: list[str] = []

    def remove(name, *, expected_generation="", owner_id=""):
        removed.append(name)
        return True

    with (
        patch.object(app_module._session_lifecycle, "snapshot", return_value={"sessions": rows}),
        patch.object(app_module._session_lifecycle, "remove", side_effect=remove),
        patch.object(app_module, "_live_tmux_session_names", return_value=set()),
    ):
        retired = app_module._retire_unrecoverable_session_rows(set())

    assert retired == ["ghost"]
    assert removed == ["ghost"]


def test_startup_keeps_a_tab_whose_tmux_session_is_still_alive():
    rows = {"ghost": dict(GHOST_ROW)}
    with (
        patch.object(app_module._session_lifecycle, "snapshot", return_value={"sessions": rows}),
        patch.object(app_module._session_lifecycle, "remove", side_effect=AssertionError("must not remove")),
        patch.object(app_module, "_live_tmux_session_names", return_value={"ghost"}),
    ):
        assert app_module._retire_unrecoverable_session_rows({"ghost"}) == []


# ─── Closing a session saves nothing ───


def test_closing_a_session_has_no_summarize_or_archive_step():
    """Context is kept in the advisor and reaches a session over MCP.

    The dashboard used to summarize the transcript with an LLM and write it into
    a TECHNICAL_SPEC.md on close. Nothing in the close path may do that again.
    """
    for name in (
        "_start_session_close",
        "_session_close_status",
        "_run_session_close_job",
        "_summarize_session_close",
        "_persist_session_close_knowledge",
        "_technical_spec_path",
        "_install_session_close_barrier",
        "_session_close_barriers",
    ):
        assert not hasattr(app_module, name), f"{name} is still defined"


def test_no_route_or_controller_op_can_start_a_close_job():
    paths = {getattr(route, "path", "") for route in app_module.app.routes}
    assert "/api/sessions/{session_name}/close/{job_id}" not in paths
    source = (Path(app_module.__file__).with_name("app.py")).read_text()
    assert "session_close_start" not in source
