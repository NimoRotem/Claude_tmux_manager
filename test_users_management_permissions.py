"""Focused regression coverage for the dedicated Users workspace and fixed groups."""

import os
from unittest.mock import MagicMock, patch

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

from fastapi.testclient import TestClient

import app as app_module


def _admin_client() -> TestClient:
    client = TestClient(app_module.app)
    client.cookies.set(
        app_module.AUTH_COOKIE,
        app_module._make_token("admin"),
    )
    return client


def test_fixed_permission_groups_match_the_requested_catalog():
    groups = app_module.PERMISSION_GROUPS

    assert list(groups) == [
        "managers",
        "engineers",
        "accounting-cn",
        "accounting-all",
        "dev",
        "limited-dev",
    ]
    assert [groups[group_id]["name"] for group_id in groups] == [
        "Managers",
        "Engineers",
        "Accounting-CN",
        "Accounting-all",
        "Dev",
        "Limited-Dev",
    ]
    policies = {key: value["instructions"].lower() for key, value in groups.items()}
    assert "modify permissions" in policies["managers"]
    # Personal identity documents and the Ramp card estate are not company
    # data: a manager who needs a figure asks for that figure.
    assert "ramp" in groups["managers"]["advisor_cannot_see"]
    assert "identity" not in groups["managers"]["advisor_can_see"].split(",")
    # The advisor matches the bare token "identity" for get_identity;
    # "identity_docs" reads like it should work and does not, which is how a
    # manager kept pulling passports while ramp in the same list was refused.
    for group_id, group in groups.items():
        assert "identity" in group["advisor_cannot_see"].split(","), group_id
    assert "sales" in policies["engineers"] and "salaries" in policies["engineers"]
    assert "mainland china" in policies["accounting-cn"]
    assert "all companies" in policies["accounting-all"]
    assert "sensitive bank" in policies["dev"] and "make transactions" in policies["dev"]
    assert "junior developer" in policies["limited-dev"]
    assert "senior developer" in policies["limited-dev"]


def test_invalid_group_is_rejected_before_user_update(monkeypatch):
    admin = {"id": "admin", "username": "admin", "role": "admin"}
    member = {"id": "u_member", "username": "member", "role": "user", "group": "dev"}
    monkeypatch.setattr(app_module, "_load_users", lambda: [admin, member])
    monkeypatch.setattr(app_module, "_save_users", lambda users: None)

    response = _admin_client().patch(
        "/api/admin/users/u_member",
        json={"group": "made-up-group"},
    )

    assert response.status_code == 400
    assert "permission group" in response.json()["error"].lower()


def test_last_activity_comes_from_human_prompts_not_agent_output(monkeypatch):
    member = {"id": "u_member", "username": "member", "role": "user"}
    monkeypatch.setattr(app_module, "_prompt_audit_summary", lambda: {})
    monkeypatch.setattr(
        app_module,
        "_load_messages",
        lambda user=None: {
            "one": [
                {"role": "user", "text": "first", "ts": 100},
                {"role": "assistant", "text": "newer agent output", "ts": 900},
            ],
            "two": [{"role": "user", "text": "latest human input", "ts": 500}],
        },
    )

    assert app_module._last_human_activity(member) == 500


def test_last_activity_excludes_prompts_sent_during_admin_impersonation(monkeypatch):
    member = {"id": "u_member", "username": "member", "role": "user"}
    monkeypatch.setattr(
        app_module,
        "_prompt_audit_summary",
        lambda: {
            member["id"]: {
                "count": 3,
                "last_ts": 900,
                "last_direct_ts": 500,
            }
        },
    )

    assert app_module._last_human_activity(member) == 500


def test_last_activity_accepts_pre_impersonation_audit_summaries(monkeypatch):
    member = {"id": "u_member", "username": "member", "role": "user"}
    monkeypatch.setattr(
        app_module,
        "_prompt_audit_summary",
        lambda: {member["id"]: {"count": 2, "last_ts": 700}},
    )

    assert app_module._last_human_activity(member) == 700


def test_admin_users_api_exposes_human_activity_instead_of_last_login(monkeypatch):
    admin = {
        "id": "admin",
        "username": "admin",
        "role": "admin",
        "last_login": 999,
    }
    monkeypatch.setattr(app_module, "_load_users", lambda: [admin])
    monkeypatch.setattr(app_module, "_load_session_owners", lambda: {})
    monkeypatch.setattr(app_module, "_last_human_activity", lambda user: 123)

    response = _admin_client().get("/api/admin/users")
    payload = response.json()["users"][0]

    assert response.status_code == 200
    assert payload["last_activity"] == 123
    assert "last_login" not in payload
    assert "last_login_ip" not in payload


def test_user_overview_combines_prompts_history_memories_and_context(monkeypatch, tmp_path):
    admin = {"id": "admin", "username": "admin", "role": "admin"}
    member = {"id": "u_member", "username": "member", "role": "user", "group": "dev"}
    codex_home = tmp_path / ".codex-user-u_member"
    (codex_home / "memories").mkdir(parents=True)
    (codex_home / "memories" / "MEMORY.md").write_text("native memory")
    (codex_home / "AGENTS.md").write_text("member instructions")
    monkeypatch.setattr(app_module, "_load_users", lambda: [admin, member])
    monkeypatch.setattr(app_module, "_load_session_owners", lambda: {})
    monkeypatch.setattr(app_module, "_user_codex_config_dir", lambda user: codex_home)
    monkeypatch.setattr(
        app_module,
        "_load_messages",
        lambda user=None: {"work": [{"role": "user", "text": "my prompt", "ts": 42}]},
    )
    monkeypatch.setattr(app_module, "_load_all_notes", lambda user=None: {"work": "saved note"})
    monkeypatch.setattr(app_module, "get_tmux_sessions", lambda: [])

    response = _admin_client().get("/api/admin/users/u_member/overview")
    payload = response.json()

    assert response.status_code == 200
    assert payload["user"]["id"] == "u_member"
    assert payload["prompts"][0]["prompt"] == "my prompt"
    assert payload["history"][0]["session_name"] == "work"
    assert any(item["path"] == "memories/MEMORY.md" for item in payload["memories"])
    assert any(item["path"] == "AGENTS.md" for item in payload["context"])


def test_admin_can_open_inventory_files_but_not_private_credentials(monkeypatch, tmp_path):
    admin = {"id": "admin", "username": "admin", "role": "admin"}
    member = {"id": "u_member", "username": "member", "role": "user"}
    codex_home = tmp_path / ".codex-user-u_member"
    codex_home.mkdir()
    (codex_home / "AGENTS.md").write_text("full account instructions")
    (codex_home / "auth.json").write_text("secret credential")
    monkeypatch.setattr(app_module, "_load_users", lambda: [admin, member])
    monkeypatch.setattr(app_module, "_user_codex_config_dir", lambda user: codex_home)

    allowed = _admin_client().get(
        "/api/admin/users/u_member/data-file",
        params={"kind": "context", "path": "AGENTS.md"},
    )
    denied = _admin_client().get(
        "/api/admin/users/u_member/data-file",
        params={"kind": "context", "path": "auth.json"},
    )

    assert allowed.status_code == 200
    assert allowed.json()["content"] == "full account instructions"
    assert denied.status_code == 404


def test_profiles_are_absent_from_routes_session_contract_and_html():
    paths = {route.path for route in app_module.app.routes}
    html = app_module.HTML_PAGE

    assert not any(path == "/api/profiles" or path.startswith("/api/profiles/") for path in paths)
    assert not any(path.endswith("/profile") for path in paths)
    assert "profile_id" not in app_module.CreateSession.model_fields
    assert 'id="profiles-overlay"' not in html
    assert "function openProfiles" not in html
    assert "> Profiles<" not in html
    assert "UI Expert" not in html
    assert "UX Expert" not in html
    assert "QA Agent" not in html


def test_users_is_a_large_route_driven_workspace_outside_settings():
    html = app_module.HTML_PAGE

    required = (
        'id="users-overlay"',
        'id="users-content"',
        'onclick="openUsers();closeToolsMenu()"',
        "const USERS_ROUTE='#/users'",
        "window.addEventListener('hashchange',handleAppRoute)",
        "function openUserDetail(userId)",
        "function backToUsers()",
        "history.pushState(null,'','#/users/",
        'class="workspace-panel users-panel"',
        'id="users-top-scroll"',
        "user-col-resizer",
    )
    assert [token for token in required if token not in html] == []
    assert "openSettings('users')" not in html
    assert "{id:'users', label:'Users (Admin only)'}" not in html
    assert ".workspace-panel{width:min(1440px,calc(100vw - 24px));" in html


def test_owner_environment_uses_private_codex_home_and_advisor_token(monkeypatch, tmp_path):
    member = {"id": "u_member", "username": "member", "role": "user"}
    codex_home = tmp_path / ".codex-user-u_member"
    codex_home.mkdir()
    (codex_home / "advisor-token").write_text("never-print-this-token")
    calls = []
    monkeypatch.setattr(app_module, "_user_for_session", lambda name: member)
    monkeypatch.setattr(app_module, "_user_codex_config_dir", lambda user: codex_home)
    monkeypatch.setattr(app_module, "_ensure_user_codex_config_dir", lambda user: None)
    monkeypatch.setattr(app_module, "_member_session_project_dir", lambda user, name: tmp_path / "project")
    monkeypatch.setattr(app_module, "_exact_tmux_session_id", lambda _name: "$1")
    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda args, **kwargs: calls.append(args) or MagicMock(returncode=0),
    )

    assert app_module._send_session_owner_environment("work") is True
    command = calls[0][-1]
    assert f"export CODEX_HOME={codex_home}" in command
    assert str(codex_home / "advisor-token") in command
    assert "never-print-this-token" not in command


def test_owner_environment_reports_tmux_send_failure(monkeypatch, tmp_path):
    member = {"id": "u_member", "username": "member", "role": "user"}
    codex_home = tmp_path / ".codex-user-u_member"
    codex_home.mkdir()
    (codex_home / "advisor-token").write_text("private-token")
    monkeypatch.setattr(app_module, "_user_for_session", lambda name: member)
    monkeypatch.setattr(app_module, "_user_codex_config_dir", lambda user: codex_home)
    monkeypatch.setattr(app_module, "_ensure_user_codex_config_dir", lambda user: None)
    monkeypatch.setattr(app_module, "_member_session_project_dir", lambda user, name: tmp_path / "project")
    monkeypatch.setattr(
        app_module.subprocess,
        "run",
        lambda *args, **kwargs: MagicMock(returncode=1),
    )

    assert app_module._send_session_owner_environment("work") is False


def test_advisor_sync_applies_the_fixed_group_and_its_scopes(monkeypatch, tmp_path):
    member = {
        "id": "u_member",
        "username": "member",
        "role": "user",
        "group": "accounting-cn",
    }
    codex_home = tmp_path / ".codex-user-u_member"
    codex_home.mkdir()
    (codex_home / "advisor-token").write_text("private-token")
    calls = []
    monkeypatch.setattr(app_module, "_user_codex_config_dir", lambda user: codex_home)
    monkeypatch.setattr(
        app_module,
        "_advisor_request",
        lambda method, path, payload=None: calls.append((method, path, payload)) or {"ok": True},
    )

    assert app_module._sync_advisor_user(member, provision=False) is True
    _, path, payload = calls[-1]
    assert path == "/api/clients"
    assert payload["group_name"] == "accounting-cn"
    assert payload["scopes"] == "memories.write,payments"
