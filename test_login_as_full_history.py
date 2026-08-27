"""Regressions for admin impersonation and durable terminal history."""

import json
import os
import time
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import parse_qs, urlparse

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import app as dashboard

ADMIN = {"id": "admin", "username": "admin", "role": "admin"}
MEMBER = {"id": "u_member", "username": "member@example.com", "role": "user"}
OWNERS = {"member-work": "u_member"}


def _admin_client() -> TestClient:
    client = TestClient(dashboard.app)
    client.cookies.set(dashboard.AUTH_COOKIE, dashboard._make_token("admin"))
    return client


@pytest.fixture
def isolated_impersonation_store(monkeypatch, tmp_path):
    """Point impersonation state at a private file and preserve legacy globals."""
    old_tickets = dict(getattr(dashboard, "_impersonation_tickets", {}))
    old_sessions = dict(getattr(dashboard, "_impersonation_sessions", {}))
    old_loaded = getattr(dashboard, "_impersonation_sessions_loaded", False)
    monkeypatch.setattr(
        dashboard,
        "IMPERSONATION_SESSIONS_FILE",
        tmp_path / "impersonation.json",
    )
    if hasattr(dashboard, "_impersonation_tickets"):
        dashboard._impersonation_tickets.clear()
    if hasattr(dashboard, "_impersonation_sessions"):
        dashboard._impersonation_sessions.clear()
    if hasattr(dashboard, "_impersonation_sessions_loaded"):
        dashboard._impersonation_sessions_loaded = False
    yield tmp_path / "impersonation.json"
    if hasattr(dashboard, "_impersonation_tickets"):
        dashboard._impersonation_tickets.clear()
        dashboard._impersonation_tickets.update(old_tickets)
    if hasattr(dashboard, "_impersonation_sessions"):
        dashboard._impersonation_sessions.clear()
        dashboard._impersonation_sessions.update(old_sessions)
    if hasattr(dashboard, "_impersonation_sessions_loaded"):
        dashboard._impersonation_sessions_loaded = old_loaded


def _issue_ticket(client: TestClient) -> str:
    response = client.post("/api/admin/users/u_member/impersonate")
    assert response.status_code == 200
    return parse_qs(urlparse(response.json()["url"]).query)["impersonate_ticket"][0]


def _exchange_ticket(client: TestClient, ticket: str) -> str:
    response = client.post(
        "/api/admin/impersonation/exchange",
        json={"ticket": ticket},
    )
    assert response.status_code == 200
    return response.json()["token"]


def test_impersonation_ticket_survives_api_worker_boundary(
    monkeypatch,
    isolated_impersonation_store,
):
    monkeypatch.setattr(dashboard, "_load_users", lambda: [ADMIN, MEMBER])
    client = _admin_client()
    ticket = _issue_ticket(client)

    # A different API worker has no access to the issuing worker's memory.
    if hasattr(dashboard, "_impersonation_tickets"):
        dashboard._impersonation_tickets.clear()

    token = _exchange_ticket(client, ticket)
    assert len(token) >= 40


def test_impersonation_session_is_visible_to_every_api_worker(
    monkeypatch,
    isolated_impersonation_store,
):
    monkeypatch.setattr(dashboard, "_load_users", lambda: [ADMIN, MEMBER])
    client = _admin_client()
    token = _exchange_ticket(client, _issue_ticket(client))

    # Simulate a worker that loaded an empty cache before the token was issued.
    if hasattr(dashboard, "_impersonation_sessions"):
        dashboard._impersonation_sessions.clear()
    if hasattr(dashboard, "_impersonation_sessions_loaded"):
        dashboard._impersonation_sessions_loaded = True

    response = client.get(
        "/api/me",
        headers={"X-Tmux-Impersonate": token},
    )
    assert response.status_code == 200
    assert response.json()["id"] == MEMBER["id"]
    assert response.json()["impersonating"] is True


def test_malformed_old_impersonation_record_is_pruned(
    monkeypatch,
    isolated_impersonation_store,
):
    isolated_impersonation_store.write_text(json.dumps({
        "sessions": {
            "broken": {
                "admin_id": "admin",
                "target_id": "u_member",
                "expires_at": "not-a-timestamp",
            }
        }
    }))
    monkeypatch.setattr(dashboard, "_load_users", lambda: [ADMIN, MEMBER])

    response = _admin_client().post("/api/admin/users/u_member/impersonate")

    assert response.status_code == 200
    stored = json.loads(isolated_impersonation_store.read_text())
    assert "broken" not in stored["sessions"]


def test_impersonated_admin_can_open_member_terminal_websocket(
    monkeypatch,
    isolated_impersonation_store,
):
    token = "websocket-token-with-enough-entropy-123456789"
    isolated_impersonation_store.write_text(json.dumps({
        "version": 1,
        "tickets": {},
        "sessions": {
            token: {
                "admin_id": ADMIN["id"],
                "target_id": MEMBER["id"],
                "expires_at": time.time() + 300,
            }
        },
    }))
    if hasattr(dashboard, "_impersonation_sessions"):
        dashboard._impersonation_sessions.clear()
        dashboard._impersonation_sessions[token] = {
            "admin_id": ADMIN["id"],
            "target_id": MEMBER["id"],
            "expires_at": time.time() + 300,
        }
    if hasattr(dashboard, "_impersonation_sessions_loaded"):
        dashboard._impersonation_sessions_loaded = True

    monkeypatch.setattr(dashboard, "_load_users", lambda: [ADMIN, MEMBER])
    monkeypatch.setattr(dashboard, "_load_session_owners", lambda: OWNERS)
    monkeypatch.setattr(dashboard, "PROCESS_ROLE", "combined")

    async def send_one_payload(_session_name, writer):
        writer.write(b'{"mode":"ping","pane_total":1}\n')
        writer.queue.put_nowait(None)
        return {}

    client = _admin_client()
    with (
        patch.object(
            dashboard,
            "_resume_parked_session",
            AsyncMock(return_value={"ok": True}),
        ),
        patch.object(dashboard, "_terminal_subscribe", send_one_payload),
        client.websocket_connect(
            "/ws/sessions/member-work/raw",
            subprotocols=["tmux-impersonate", token],
        ) as socket,
    ):
        assert socket.accepted_subprotocol == "tmux-impersonate"
        assert socket.receive_json()["mode"] == "ping"
        with pytest.raises(WebSocketDisconnect):
            socket.receive_json()


def test_browser_sends_tab_impersonation_token_to_terminal_websocket(monkeypatch):
    monkeypatch.setattr(dashboard, "_load_users", lambda: [ADMIN, MEMBER])
    html = _admin_client().get("/").text
    start = html.index("function startRawPolling(name)")
    end = html.index("function stopRawPolling(name)", start)
    websocket_code = html[start:end]

    assert "_storedImpersonationToken()" in websocket_code
    assert "tmux-impersonate" in websocket_code
    assert "new WebSocket" in websocket_code


def test_browser_keeps_every_server_supplied_terminal_line(monkeypatch):
    monkeypatch.setattr(dashboard, "_load_users", lambda: [ADMIN, MEMBER])
    html = _admin_client().get("/").text

    assert "RAW_MAX_LINES" not in html
    assert "rows.slice(rows.length-" not in html


def _event(payload_type: str, message: str | None = None) -> str:
    payload = {"type": payload_type}
    if message is not None:
        payload["message"] = message
    return json.dumps({"type": "event_msg", "payload": payload})


def test_visible_transcript_excludes_hidden_roles_and_tool_details(tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join([
        _event("user_message", "First request"),
        json.dumps({
            "type": "response_item",
            "payload": {"type": "message", "role": "developer", "content": "HIDDEN POLICY"},
        }),
        _event("agent_message", "Early response"),
        json.dumps({
            "type": "response_item",
            "payload": {"type": "custom_tool_call_output", "output": "HIDDEN TOOL"},
        }),
        _event("context_compacted"),
    ]) + "\n")

    visible = dashboard._read_visible_codex_transcript(rollout)

    assert "User:\nFirst request" in visible
    assert "Codex:\nEarly response" in visible
    assert "[Context compacted]" in visible
    assert "HIDDEN POLICY" not in visible
    assert "HIDDEN TOOL" not in visible


def test_full_terminal_prepends_only_missing_visible_conversation(monkeypatch, tmp_path):
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("\n".join([
        _event("user_message", "First request"),
        _event("agent_message", "Early response"),
        _event("context_compacted"),
        _event("agent_message", "Later response still missing"),
        _event("context_compacted"),
        _event("agent_message", "Visible response from Codex"),
    ]) + "\n")
    pane = "* [Context compacted]\n* Visible response from Codex\nLive shell line\n"
    monkeypatch.setattr(
        dashboard,
        "_find_session_rollout_path",
        lambda _session_name: rollout,
    )
    monkeypatch.setattr(dashboard, "capture_pane_full", lambda _session_name: pane)

    restored = dashboard._capture_session_full_output("work")

    assert "Earlier Codex conversation restored from the session log" in restored
    assert "First request" in restored
    assert "Early response" in restored
    assert "Later response still missing" in restored
    assert "[Context compacted]" in restored
    assert restored.count("Visible response from Codex") == 1
    assert restored.endswith(pane)


def test_full_terminal_falls_back_when_owner_transcript_is_unreadable(monkeypatch):
    pane = "Live pane remains available\n"
    monkeypatch.setattr(dashboard, "capture_pane_full", lambda _name: pane)
    monkeypatch.setattr(
        dashboard,
        "_find_session_rollout_path",
        lambda _name: (_ for _ in ()).throw(PermissionError("owner only")),
    )

    assert dashboard._capture_session_full_output("member-work") == pane


def test_session_thread_is_resolved_from_matching_prompt_timestamp(monkeypatch, tmp_path):
    expected = "019ffc1c-879f-7033-a2dd-3b20c94a49d3"
    unrelated = "01a02867-a877-7f52-a8e6-1835c630c7a6"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    audit = tmp_path / "prompt-history.jsonl"
    audit.write_text("\n".join([
        json.dumps({"ts": 100.25, "session_name": "work", "prompt": "first"}),
        json.dumps({"ts": 200.75, "session_name": "other", "prompt": "other"}),
    ]) + "\n")
    (codex_home / "history.jsonl").write_text("\n".join([
        json.dumps({"session_id": expected, "ts": 100, "text": "first"}),
        json.dumps({"session_id": unrelated, "ts": 200, "text": "other"}),
    ]) + "\n")
    rollout = codex_home / "sessions" / "2026" / "08" / "22"
    rollout.mkdir(parents=True)
    (rollout / f"rollout-2026-08-22T00-00-00-{expected}.jsonl").write_text("\n")
    monkeypatch.setattr(dashboard, "PROMPT_AUDIT_FILE", audit)
    monkeypatch.setattr(dashboard, "_session_config_base", lambda _name: codex_home)

    assert dashboard._find_session_transcript_uuid("work") == expected
    assert dashboard._find_session_rollout_path("work").name.endswith(f"-{expected}.jsonl")


def test_session_thread_uses_prompt_content_when_timestamps_collide(monkeypatch, tmp_path):
    expected = "019ffc1c-879f-7033-a2dd-3b20c94a49d3"
    unrelated = "01a02867-a877-7f52-a8e6-1835c630c7a6"
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    audit = tmp_path / "prompt-history.jsonl"
    audit.write_text(json.dumps({
        "ts": 100.25,
        "session_name": "work",
        "prompt": "the correct dashboard prompt",
    }) + "\n")
    (codex_home / "history.jsonl").write_text("\n".join([
        json.dumps({
            "session_id": expected,
            "ts": 100,
            "text": "the correct dashboard prompt",
        }),
        json.dumps({
            "session_id": unrelated,
            "ts": 100,
            "text": "a different session prompt",
        }),
    ]) + "\n")
    monkeypatch.setattr(dashboard, "PROMPT_AUDIT_FILE", audit)
    monkeypatch.setattr(dashboard, "_session_config_base", lambda _name: codex_home)

    assert dashboard._find_session_transcript_uuid("work") == expected


def test_exact_resume_never_uses_global_last_thread(tmp_path):
    thread_id = "019ffc1c-879f-7033-a2dd-3b20c94a49d3"
    command = dashboard._launch_codex_cmd(
        "codex --yolo",
        pin_model=False,
        resume=True,
        resume_uuid=thread_id,
        resume_cwd=str(tmp_path),
        codex_home=tmp_path,
    )

    assert "codex resume -C" in command
    assert thread_id in command
    assert "resume --last" not in command


@pytest.mark.asyncio
async def test_crash_recovery_passes_the_session_bound_thread_to_launcher():
    thread_id = "019ffc1c-879f-7033-a2dd-3b20c94a49d3"
    running = AsyncMock(side_effect=[False, True])
    with (
        patch.object(dashboard, "_async_is_codex_running", running),
        patch.object(dashboard, "_send_session_owner_environment", return_value=True),
        patch.object(dashboard, "_multi_tenant_enabled", return_value=False),
        patch.object(dashboard, "_ensure_codex_auth_with_fallback", return_value={}),
        patch.object(dashboard, "_find_session_transcript_uuid", return_value=thread_id),
        patch.object(dashboard, "_exact_tmux_session_id", return_value="$1"),
        patch.object(dashboard, "get_session_cwd", return_value="/workspace/work"),
        patch.object(
            dashboard, "_saved_session_model_effort", return_value=(None, None)
        ),
        patch.object(dashboard, "_session_launch_command", return_value="launch") as launcher,
        patch.object(
            dashboard.subprocess, "run", return_value=MagicMock(returncode=0)
        ),
        patch.object(dashboard.asyncio, "sleep", AsyncMock()),
    ):
        assert await dashboard._ensure_codex_running("work") is True

    launcher.assert_called_once_with(
        "work",
        dashboard._session_launch_base("work"),
        expected_owner_id=None,
        pin_model=False,
        resume=True,
        model=None,
        effort=None,
        resume_uuid=thread_id,
        resume_cwd="/workspace/work",
    )
