"""Regression coverage for private handoffs, user totals, and key-bar controls."""

import json
import os
from pathlib import Path

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

from fastapi.testclient import TestClient

import app as dashboard


SOURCE = Path(dashboard.__file__).read_text()


def _section(start: str, end: str) -> str:
    return SOURCE.split(start, 1)[1].split(end, 1)[0]


def _user(user_id: str, username: str, role: str = "user") -> dict:
    return {"id": user_id, "username": username, "role": role}


def _private_project(tmp_path: Path, username: str, marker: str) -> None:
    project = tmp_path / username / "demo"
    project.mkdir(parents=True)
    (project / "index.html").write_text(marker)


def test_project_work_product_requires_dashboard_sign_in(tmp_path, monkeypatch):
    marker = "PRIVATE-WORK-PRODUCT"
    users = [_user("admin", "admin", "admin")]
    _private_project(tmp_path, "admin", marker)
    monkeypatch.setattr(dashboard, "AUTH_PASS", "testpass")
    monkeypatch.setattr(dashboard, "PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "_load_users", lambda: users)

    response = TestClient(dashboard.app).get("/admin/demo/")

    assert "Log in to continue." in response.text and marker not in response.text


def test_project_work_product_opens_for_its_signed_in_owner(tmp_path, monkeypatch):
    marker = "PRIVATE-WORK-PRODUCT"
    users = [_user("u_owner", "owner")]
    _private_project(tmp_path, "owner", marker)
    monkeypatch.setattr(dashboard, "AUTH_PASS", "testpass")
    monkeypatch.setattr(dashboard, "PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "_load_users", lambda: users)
    client = TestClient(dashboard.app)
    client.cookies.set(dashboard.AUTH_COOKIE, dashboard._make_token("u_owner"))

    response = client.get("/owner/demo/")

    assert response.status_code == 200 and marker in response.text


def test_legacy_project_namespace_uses_its_session_owner(tmp_path, monkeypatch):
    marker = "PRIVATE-LEGACY-WORK-PRODUCT"
    users = [_user("admin", "admin", "admin"), _user("u_other", "other")]
    _private_project(tmp_path, "Nimo", marker)
    monkeypatch.setattr(dashboard, "AUTH_PASS", "testpass")
    monkeypatch.setattr(dashboard, "PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "_load_users", lambda: users)
    monkeypatch.setattr(dashboard, "_load_session_owners", lambda: {"demo": "admin"})
    client = TestClient(dashboard.app)
    client.cookies.set(dashboard.AUTH_COOKIE, dashboard._make_token("admin"))

    response = client.get("/Nimo/demo/")
    other_client = TestClient(dashboard.app)
    other_client.cookies.set(dashboard.AUTH_COOKIE, dashboard._make_token("u_other"))
    blocked = other_client.get("/Nimo/demo/")

    assert response.status_code == 200 and marker in response.text
    assert blocked.status_code == 403 and marker not in blocked.text


def test_admin_can_open_unregistered_legacy_project_namespace(tmp_path, monkeypatch):
    marker = "PRIVATE-UNREGISTERED-LEGACY-WORK-PRODUCT"
    users = [_user("admin", "admin", "admin"), _user("u_other", "other")]
    _private_project(tmp_path, "Nimo", marker)
    monkeypatch.setattr(dashboard, "AUTH_PASS", "testpass")
    monkeypatch.setattr(dashboard, "PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "_load_users", lambda: users)
    monkeypatch.setattr(dashboard, "_load_session_owners", lambda: {})
    admin_client = TestClient(dashboard.app)
    admin_client.cookies.set(dashboard.AUTH_COOKIE, dashboard._make_token("admin"))
    other_client = TestClient(dashboard.app)
    other_client.cookies.set(dashboard.AUTH_COOKIE, dashboard._make_token("u_other"))

    opened = admin_client.get("/Nimo/demo/")
    blocked = other_client.get("/Nimo/demo/")

    assert opened.status_code == 200 and marker in opened.text
    assert blocked.status_code == 404 and marker not in blocked.text


def test_project_work_product_is_private_between_member_accounts(tmp_path, monkeypatch):
    marker = "PRIVATE-WORK-PRODUCT"
    users = [_user("u_owner", "owner"), _user("u_other", "other")]
    _private_project(tmp_path, "owner", marker)
    monkeypatch.setattr(dashboard, "AUTH_PASS", "testpass")
    monkeypatch.setattr(dashboard, "PROJECTS_ROOT", tmp_path)
    monkeypatch.setattr(dashboard, "_load_users", lambda: users)
    client = TestClient(dashboard.app)
    client.cookies.set(dashboard.AUTH_COOKIE, dashboard._make_token("u_other"))

    response = client.get("/owner/demo/")

    assert response.status_code == 403 and marker not in response.text


def test_email_username_is_valid_for_an_authenticated_project_url():
    assert dashboard._safe_seg("member@example.com")


def test_agent_context_forbids_local_only_work_product_handoffs():
    rule = "Never return a local filesystem path as the only link to a work product."

    assert SOURCE.count(rule) == 2


def test_existing_member_context_receives_the_work_product_handoff_rule(monkeypatch):
    monkeypatch.setattr(dashboard, "_read_global_context", lambda: "Existing admin policy")

    instructions = dashboard._member_developer_instructions(_user("u_owner", "owner"))

    assert "Never return a local filesystem path as the only link to a work product." in instructions


def test_chat_and_terminal_paths_use_the_authenticated_file_viewer():
    linkifier = _section("//  Absolute paths like", "/* ── Chat bubbles")
    chat = _section("/* ── Chat bubbles", "function _chatLinksHtml")

    assert all(
        snippet in linkifier + chat
        for snippet in (
            "BASE+'/file?path='+encodeURIComponent",
            "function _chatHref(target,sessionName)",
            "'&session='+encodeURIComponent(sessionName)",
        )
    )


def test_requested_key_and_command_buttons_are_absent():
    key_bar = _section("function buildKeyBar(name,tab){", "// ── Saved project keys")
    removed = (
        "['q']", "['y']", "['n']", "['Tab']", "'/approvals'", "'/diff'",
        "sendSlashCommand('${name}','/model gpt-5.4')", "Paste Mode: ON",
        'title="Upload file"',
    )

    assert not any(snippet in key_bar for snippet in removed)


def test_clickable_upload_drop_areas_remain_in_both_key_bars():
    key_bar = _section("function buildKeyBar(name,tab){", "// ── Saved project keys")

    assert 'class="upload-drop"' in key_bar and 'class="drop-zone"' in key_bar


def test_rollout_lifetime_usage_uses_the_final_cumulative_total(tmp_path):
    rollout = tmp_path / "rollout-test.jsonl"
    events = [
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 8, "output_tokens": 2},
                    "total_token_usage": {"input_tokens": 8, "output_tokens": 2, "total_tokens": 10},
                },
            },
        },
        {
            "type": "event_msg",
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {"input_tokens": 12, "output_tokens": 3},
                    "total_token_usage": {"input_tokens": 20, "output_tokens": 5, "total_tokens": 25},
                },
            },
        },
        {"type": "response_item", "payload": {"text": "x" * 70_000}},
    ]
    rollout.write_text("\n".join(json.dumps(event) for event in events) + "\n")

    usage = dashboard._rollout_lifetime_usage(rollout)

    assert usage["totalTokens"] == 25


def test_usage_by_account_includes_all_time_prompt_and_token_totals(monkeypatch, tmp_path):
    user = _user("u_owner", "owner")
    monkeypatch.setattr(dashboard, "_load_users", lambda: [user])
    monkeypatch.setattr(dashboard, "_user_codex_config_dir", lambda _user: tmp_path)
    monkeypatch.setattr(
        dashboard,
        "_prompt_counts_by_user",
        lambda _cutoffs: {"u_owner": {"today": 2, "week": 5}},
    )
    monkeypatch.setattr(
        dashboard,
        "_prompt_audit_summary",
        lambda: {"u_owner": {"count": 11}},
    )

    def token_usage(_home, cutoffs):
        values = {"today": 100, "week": 500, "all": 900}
        return {
            key: {
                "inputTokens": values[key], "outputTokens": 0,
                "cacheReadTokens": 0, "reasoningTokens": 0,
                "totalTokens": values[key], "turns": 0, "estimatedCost": 0.0,
            }
            for key in cutoffs
        }

    monkeypatch.setattr(dashboard, "_token_usage_for_home", token_usage)

    data = dashboard._usage_by_account()

    assert (data["users"][0]["promptsTotal"], data["users"][0]["all"]["totalTokens"], data["totals"]["tokensTotal"]) == (11, 900, 900)


def test_admin_user_stats_include_lifetime_prompts_and_tokens(monkeypatch, tmp_path):
    user = _user("u_owner", "owner")
    monkeypatch.setattr(dashboard, "_prompt_audit_summary", lambda: {"u_owner": {"count": 11}})
    monkeypatch.setattr(dashboard, "_user_codex_config_dir", lambda _user: tmp_path)
    monkeypatch.setattr(
        dashboard,
        "_token_usage_for_home",
        lambda _home, _cutoffs: {"all": {"totalTokens": 900}},
    )

    stats = dashboard._user_lifetime_stats([user])

    assert stats["u_owner"] == {"total_prompts": 11, "total_tokens": 900}


def test_users_table_exposes_lifetime_usage_columns():
    users_ui = _section("function renderUsersAdmin(){", "function initUsersTableUX(){")

    assert all(label in users_ui for label in ("Total prompts", "Total tokens", "u.total_prompts", "u.total_tokens"))


def test_usage_by_user_table_renders_total_columns():
    stats_ui = _section("// Per-user usage:", "document.getElementById('stats-content').innerHTML=html;")

    assert all(label in stats_ui for label in ("Total prompts", "Total tokens", "u.promptsTotal", "u.all.totalTokens", "n>=1e9"))
