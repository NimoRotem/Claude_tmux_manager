"""Voice security contracts with synthetic owners, temp state and no provider I/O."""

import asyncio
import contextlib
import json
import logging
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

import voice_mode as voice


GENERATION = "1" * 32


def binding(name="alpha", owner="one", generation=GENERATION, root="root"):
    return {"session_name": name, "owner_id": owner, "generation": generation,
            "resume_uuid": root}


@pytest.fixture(autouse=True)
def forbid_real_provider_io(monkeypatch):
    """A missing test mock must fail rather than contact a paid voice provider."""
    monkeypatch.setattr(voice.httpx, "AsyncClient", Mock(side_effect=AssertionError("Unexpected provider HTTP")))
    monkeypatch.setattr(voice.websockets, "connect", Mock(side_effect=AssertionError("Unexpected provider WebSocket")))


@pytest.fixture
def host(tmp_path):
    identities = {"alpha": binding(), "beta": binding("beta", root="beta-root")}

    def user(request):
        if request.headers.get("x-invalid-impersonation"):
            request.state._invalid_impersonation = True
        return {"id": "one"} if request.cookies.get("test") == "one" else None

    return SimpleNamespace(
        MESSAGES_DIR=tmp_path,
        identities=identities,
        logger=logging.getLogger("voice-test"),
        _current_user=user,
        _user_can_access_session=lambda account, name: bool(account and name in identities),
        _terminal_history_binding=lambda name, owner: identities.get(name),
        _terminal_binding=lambda name, owner: {"generation": identities[name]["generation"], "owner_id": owner},
        _session_lifecycle={name: {"desired_state": "running", "owner_id": "one", "resume_uuid": ""}
                            for name in identities},
        _session_operation_lock=lambda name: contextlib.nullcontext(),
        _managed_openai_key=lambda: "synthetic-provider-key-not-for-output",
        _read_terminal_history=Mock(return_value={"entries": [], "cursor": "", "has_more": False}),
        _session_tab_label_rows=lambda: {},
        _session_tab_label=lambda name, labels: {"alpha": "Attached project", "beta": "Other project"}[name],
        api_send_command=AsyncMock(return_value=JSONResponse({"ok": True})),
        api_interrupt_session=AsyncMock(return_value=JSONResponse({"ok": True})),
        async_detect_activity=AsyncMock(return_value={"status": "idle"}),
        _detect_activity_raw=lambda name: {"status": "idle"},
        _restart_codex_for_session=AsyncMock(side_effect=AssertionError("Voice must not restart coding workers")),
        SendCommand=lambda **values: SimpleNamespace(**values),
    )


@pytest.fixture
def connection(host):
    socket = SimpleNamespace(scope={"headers": [(b"cookie", b"test=one")], "state": {}},
                             send_json=AsyncMock())
    conn = voice.Connection(host, socket, {"id": "one"}, "alpha", dict(host.identities["alpha"]))
    voice.store(host.MESSAGES_DIR).update(lambda rows: rows.update(alpha={
        "active": True, "connected": False, "owner_id": "one", "generation": GENERATION,
        "root": "root", "nonce": conn.nonce, "revision": 1, "paused": False,
    }))
    return conn


@pytest.fixture
def client(host):
    application = FastAPI()
    voice.install(application, host)
    return TestClient(application)


@pytest.mark.parametrize("method,path", [
    ("get", "/api/sessions/alpha/voice/status"),
    ("post", "/api/sessions/alpha/voice/stop"),
])
def test_voice_http_requires_authenticated_owner(client, host, method, path):
    assert getattr(client, method)(path).status_code in (401, 403, 404)
    host.api_send_command.assert_not_awaited()
    host.api_interrupt_session.assert_not_awaited()


@pytest.mark.parametrize("method", ["get", "post"])
def test_voice_routes_reject_binding_from_another_owner(client, host, method):
    client.cookies.set("test", "one")
    host.identities["alpha"] = binding(owner="other")
    suffix = "status" if method == "get" else "stop"
    response = getattr(client, method)("/api/sessions/alpha/voice/" + suffix,
                                       headers={"origin": "http://testserver"})
    assert response.status_code in (401, 403, 404)
    host.api_send_command.assert_not_awaited()


def test_invalid_impersonation_fails_even_if_effective_user_is_returned(client):
    client.cookies.set("test", "one")
    response = client.get("/api/sessions/alpha/voice/status", headers={"x-invalid-impersonation": "1"})
    assert response.status_code in (401, 403, 404)


def test_missing_history_can_use_owner_bound_starting_terminal_without_prompting(client, host):
    client.cookies.set("test", "one")
    host._terminal_history_binding = lambda *args: None
    response = client.get("/api/sessions/alpha/voice/status")
    assert response.status_code == 200
    host.api_send_command.assert_not_awaited()
    host._restart_codex_for_session.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("resume_uuid", "existing-missing-root"),
                                         ("desired_state", "stopped"), ("owner_id", "other")])
def test_missing_existing_history_is_not_reclassified_as_new_session(client, host, field, value):
    client.cookies.set("test", "one")
    host._terminal_history_binding = lambda *args: None
    host._session_lifecycle["alpha"][field] = value
    assert client.get("/api/sessions/alpha/voice/status").status_code == 404
    host.api_send_command.assert_not_awaited()


def test_voice_script_route_serves_the_actual_frontend_asset(client):
    response = client.get("/voice-mode.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
    assert "RTCPeerConnection" in response.text


def test_status_does_not_expose_old_generation_or_private_guard_fields(client, connection):
    client.cookies.set("test", "one")
    voice.store(connection.host.MESSAGES_DIR).update(lambda rows: rows["alpha"].update(
        active=True, connected=True, generation="old", nonce="private-nonce", root="private-root"))
    response = client.get("/api/sessions/alpha/voice/status")
    assert response.status_code == 200
    assert not response.json()["active"] and not response.json()["connected"]
    assert "no-store" in response.headers["cache-control"]
    assert all(value not in response.text for value in ("private-nonce", "private-root", "synthetic-provider-key"))


@pytest.mark.parametrize("origin", ["https://attacker.example", "null", "", "ftp://testserver"])
def test_websocket_rejects_cross_origin_before_startup(client, monkeypatch, origin):
    client.cookies.set("test", "one")
    run = AsyncMock()
    monkeypatch.setattr(voice.Connection, "run", run)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/sessions/alpha/voice", headers={"origin": origin}):
            pass
    run.assert_not_awaited()


def test_websocket_requires_authentication_even_with_same_origin(client, monkeypatch):
    run = AsyncMock()
    monkeypatch.setattr(voice.Connection, "run", run)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect("/api/sessions/alpha/voice", headers={"origin": "http://testserver"}):
            pass
    run.assert_not_awaited()


@pytest.mark.parametrize("message", [
    {"type": "start", "sdp": "invalid"}, {"type": "start", "sdp": "v=0" + "x" * 64001},
    {"type": "audio", "sdp": "v=0"}, {"type": "start", "sdp": {}}, [],
])
def test_invalid_start_never_contacts_provider(client, host, monkeypatch, message):
    client.cookies.set("test", "one")
    run = AsyncMock()
    monkeypatch.setattr(voice.Connection, "run", run)
    with client.websocket_connect("/api/sessions/alpha/voice", headers={"origin": "http://testserver"}) as socket:
        socket.send_json(message)
        assert socket.receive_json()["type"] == "error"
    run.assert_not_awaited()
    host.api_send_command.assert_not_awaited()


@pytest.mark.parametrize("field,value", [("owner_id", "other"), ("generation", "replacement"),
                                         ("resume_uuid", "new-root")])
@pytest.mark.asyncio
async def test_changed_identity_blocks_action_before_delivery(connection, field, value):
    connection.host.identities["alpha"] = {**connection.binding, field: value}
    with pytest.raises(voice.VoiceError):
        await connection.execute({"name": "send_instructions", "arguments": '{"summary":"Change only the label","interrupt":false}'})
    connection.host.api_send_command.assert_not_awaited()
    connection.host.api_interrupt_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_context_is_discarded_if_identity_changes_during_read(connection):
    def changed(*args):
        connection.host.identities["alpha"] = binding(root="new-root")
        return {"entries": [{"kind": "assistant", "text": "Must not escape"}], "cursor": "", "has_more": False}
    connection.host._read_terminal_history.side_effect = changed
    with pytest.raises(voice.VoiceError):
        await connection.context()


@pytest.mark.asyncio
async def test_duplicate_completed_function_calls_deliver_once(connection):
    connection.execute = AsyncMock(return_value={"ok": True})
    connection.live = AsyncMock()
    item = {"type": "function_call", "call_id": "call-one", "name": "send_instructions", "arguments": "{}"}
    done = {"event": {"type": "response.output_item.done", "response_id": "response-one", "item": item}}
    finish = {"event": {"type": "response.completed", "response": {"id": "response-one", "output": []}}}
    for event in [done, done, finish, done, finish]:
        await connection.handle_response(event)
    connection.execute.assert_awaited_once()


@pytest.mark.asyncio
async def test_failed_delivery_is_not_reported_as_success(connection):
    connection.host.api_send_command.return_value = JSONResponse({"error": "Delivery unavailable"}, status_code=503)
    with pytest.raises(voice.VoiceError, match="Delivery unavailable"):
        await connection.send("Change only the label")
    assert not any(call.args[0].get("type") == "action" for call in connection.ws.send_json.await_args_list)


@pytest.mark.parametrize("provider_failure", ["rejected", "network", "malformed"])
@pytest.mark.asyncio
async def test_failed_startup_does_not_mutate_coding_session_or_persist_speech(connection, monkeypatch, provider_failure):
    conn = connection
    private_speech = "PRIVATE_SYNTHETIC_SPOKEN_DRAFT_987"
    conn.transcripts = [{"role": "user", "text": private_speech, "time": time.time()}]
    conn.idle_offer = {"summary": private_speech}
    requests = []

    class Provider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            if provider_failure == "network":
                raise voice.httpx.ConnectError("Synthetic connection failure")
            if provider_failure == "malformed":
                return SimpleNamespace(status_code=201, json=lambda: {})
            return SimpleNamespace(status_code=503)

    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: Provider())
    with pytest.raises(Exception):
        await conn.run("v=0")
    assert requests, "Startup must reach signaling without a coding restart or setup prompt"
    conn.host._restart_codex_for_session.assert_not_awaited()
    conn.host.api_send_command.assert_not_awaited()
    conn.host.api_interrupt_session.assert_not_awaited()
    assert not conn.state().get("connected") and not conn.state().get("active")
    assert not conn.transcripts and conn.idle_offer is None
    for path in conn.host.MESSAGES_DIR.rglob("*"):
        if path.is_file():
            saved = path.read_text(errors="replace")
            assert private_speech not in saved
            assert "synthetic-provider-key-not-for-output" not in saved
    assert requests[0][0] == "https://api.openai.com/v1/live/sessions"
    assert requests[0][1]["json"]["session"]["store"] is False


@pytest.mark.asyncio
async def test_missing_provider_key_never_touches_coding_runtime(connection):
    connection.host._managed_openai_key = lambda: ""
    with pytest.raises(voice.VoiceError):
        await connection.run("v=0")
    connection.host.api_send_command.assert_not_awaited()
    connection.host._restart_codex_for_session.assert_not_awaited()


@pytest.mark.asyncio
async def test_provider_transcripts_stay_volatile_and_bounded(connection):
    marker = "SYNTHETIC_PRIVATE_VOICE_FRAGMENT"

    class EventStream:
        async def __aiter__(self):
            for index in range(1510):
                yield json.dumps({"type": "session.input_transcript.delta", "delta": marker + str(index)})
            yield json.dumps({"type": "session.closed"})

    connection.upstream = EventStream()
    await connection.provider_events()
    assert 0 < len(connection.transcripts) <= 1500
    for path in connection.host.MESSAGES_DIR.rglob("*"):
        if path.is_file():
            assert marker not in path.read_text(errors="replace")


def test_confirmation_needs_exact_recent_question_and_unqualified_user_reply(connection):
    now = time.time()
    offer = {"created": now - 1, "expires": now + 60,
             "confirmation": "Can I send these instructions to Other project: fix the label?"}
    connection.transcripts = [
        {"role": "assistant", "text": offer["confirmation"], "time": now},
        {"role": "user", "text": "yes, but do something else", "time": now + .1},
    ]
    with pytest.raises(voice.VoiceError):
        connection.confirmed_offer(offer, "yes")
    connection.transcripts[-1]["text"] = "yes"
    assert connection.confirmed_offer(offer, "yes") is offer
    connection.transcripts[0]["text"] = "Can I deploy the entire project?"
    with pytest.raises(voice.VoiceError):
        connection.confirmed_offer(offer, "yes")


@pytest.mark.asyncio
async def test_other_session_action_confirmation_is_frozen_scoped_and_one_use(connection):
    conn = connection
    target = dict(conn.host.identities["beta"])
    conn.other_targets["opaque-target"] = {"binding": target, "expires": time.time() + 60}
    request_text = "Please fix the label in Other project"
    conn.transcripts = [{"role": "user", "text": request_text, "time": time.time()}]
    prepared = await conn.session_control.prepare({"target_id": "opaque-target", "action": "instruct",
                                                   "summary": "Fix only the label", "request_quote": request_text})
    conn.host.api_send_command.assert_not_awaited()
    now = time.time()
    conn.transcripts += [{"role": "assistant", "text": prepared["confirmation"], "time": now},
                         {"role": "user", "text": "yes", "time": now + .1}]
    args = {"confirmation_id": prepared["confirmation_id"], "approval_quote": "yes"}
    result = await conn.session_control.confirm(args)
    assert result["ok"] and result["session"] == "beta"
    request, name, command = conn.host.api_send_command.await_args.args
    assert name == "beta" and "Fix only the label" in command.command
    assert request.state.voice_target_binding == target
    assert not request.state.voice_approval
    with pytest.raises(voice.VoiceError):
        await conn.session_control.confirm(args)
    assert conn.host.api_send_command.await_count == 1


@pytest.mark.asyncio
async def test_changed_target_history_invalidates_scoped_confirmation(connection):
    conn = connection
    conn.other_targets["opaque-target"] = {"binding": dict(conn.host.identities["beta"]),
                                            "expires": time.time() + 60}
    request_text = "Please fix the label in Other project"
    conn.transcripts = [{"role": "user", "text": request_text, "time": time.time()}]
    prepared = await conn.session_control.prepare({"target_id": "opaque-target", "action": "instruct",
                                                   "summary": "Fix only the label", "request_quote": request_text})
    now = time.time()
    conn.transcripts += [{"role": "assistant", "text": prepared["confirmation"], "time": now},
                         {"role": "user", "text": "yes", "time": now + .1}]
    conn.host._read_terminal_history.return_value = {
        "entries": [{"kind": "assistant", "text": "A different task is now running"}],
        "cursor": "", "has_more": False,
    }
    with pytest.raises(voice.VoiceError, match="changed"):
        await conn.session_control.confirm({"confirmation_id": prepared["confirmation_id"], "approval_quote": "yes"})
    conn.host.api_send_command.assert_not_awaited()


def test_request_target_binding_cannot_move_to_another_owner_or_generation(host):
    request = Request({"type": "http", "headers": [(b"cookie", b"test=one")],
                       "state": {"voice_target_binding": dict(host.identities["alpha"])}})
    assert voice.request_target_matches(host, request, "alpha")
    host.identities["alpha"] = binding(generation="replacement")
    assert not voice.request_target_matches(host, request, "alpha")
    assert not voice.request_target_matches(host, request, "beta")


def test_guard_store_is_private_and_contains_no_voice_transcript(connection):
    path = connection.host.MESSAGES_DIR / "voice-guards.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert not any(word in path.read_text() for word in ("transcripts", "audio", "approval_quote"))


def test_old_connection_cannot_finish_new_connection_state(connection):
    voice.store(connection.host.MESSAGES_DIR).update(lambda rows: rows["alpha"].update(
        nonce="new-connection", active=True, connected=True))
    connection.finish_connection_state()
    assert connection.state()["active"] and connection.state()["connected"]


def test_reconnect_between_cleanup_read_and_lock_still_clears_local_buffers(connection, monkeypatch):
    saved_store = voice.store(connection.host.MESSAGES_DIR)
    connection.transcripts = [{"role": "user", "text": "Volatile draft", "time": time.time()}]
    connection.session_control.offer = {"summary": "Volatile confirmation"}

    class ConcurrentStore:
        def read(self):
            return saved_store.read()

        def update(self, change):
            saved_store.update(lambda rows: rows["alpha"].update(
                nonce="replacement-connection", active=True, connected=True))
            return saved_store.update(change)

    monkeypatch.setattr(voice, "store", lambda directory: ConcurrentStore())
    connection.finish_connection_state()
    assert not connection.transcripts and connection.session_control.offer is None
    replacement = saved_store.read()["alpha"]
    assert replacement["nonce"] == "replacement-connection" and replacement["active"] and replacement["connected"]


@pytest.mark.parametrize("body", [{}, [], {"nonce": "stale", "generation": GENERATION},
                                   {"nonce": "stale", "generation": "old"}])
def test_stop_requires_exact_nonce_and_generation(client, connection, body):
    client.cookies.set("test", "one")
    response = client.post("/api/sessions/alpha/voice/stop", json=body,
                           headers={"origin": "http://testserver"})
    assert response.status_code == 409
    assert connection.state()["active"]
    connection.host.api_interrupt_session.assert_not_awaited()


def test_stop_only_disconnects_bound_voice_not_coding_work(client, connection):
    client.cookies.set("test", "one")
    response = client.post("/api/sessions/alpha/voice/stop", json={
        "nonce": connection.nonce, "generation": GENERATION, "root": "root"}, headers={"origin": "http://testserver"})
    assert response.status_code == 200 and response.json()["ok"]
    assert not connection.state()["active"] and not connection.state()["connected"]
    connection.host.api_interrupt_session.assert_not_awaited()
    connection.host.api_send_command.assert_not_awaited()


def test_stop_cross_origin_cannot_change_active_connection(client, connection):
    client.cookies.set("test", "one")
    response = client.post("/api/sessions/alpha/voice/stop", json={
        "nonce": connection.nonce, "generation": GENERATION}, headers={"origin": "https://attacker.example"})
    assert response.status_code == 403
    assert connection.state()["active"]


@pytest.mark.asyncio
async def test_revoked_authentication_blocks_existing_socket_actions(connection):
    connection.host._current_user = lambda request: None
    with pytest.raises(voice.VoiceError, match="authentication"):
        await connection.send("Change the label")
    connection.host.api_send_command.assert_not_awaited()


@pytest.mark.parametrize("update", [{"active": False}, {"nonce": "replacement-connection"}])
@pytest.mark.asyncio
async def test_end_or_reconnect_invalidates_attached_request_inside_delivery_lock(connection, update):
    await connection.validate()
    assert voice.request_target_matches(connection.host, connection.request, "alpha")
    voice.store(connection.host.MESSAGES_DIR).update(lambda rows: rows["alpha"].update(update))
    assert not voice.request_target_matches(connection.host, connection.request, "alpha")


@pytest.mark.asyncio
async def test_request_acknowledgment_does_not_invalidate_explicit_user_direction(connection):
    now = time.time()
    quote = "Please change the button label to Start"
    connection.transcripts = [{"role": "user", "text": quote, "time": now},
                              {"role": "assistant", "text": "I will send that direction.", "time": now + .1}]
    result = await connection.execute({"name": "send_instructions", "arguments": json.dumps({
        "request_quote": quote, "summary": "Change only the button label to Start", "interrupt": False})})
    assert result["ok"]
    connection.host.api_send_command.assert_awaited_once()
    request = connection.host.api_send_command.await_args.args[0]
    assert request.state.voice_target_binding == connection.binding
    connection.host.api_interrupt_session.assert_not_awaited()


@pytest.mark.parametrize("speech,quote,interrupt", [
    ("Do not pause the task", "pause", True),
    ("What did you change?", "you change", False),
    ("Do not pause the task", "Do not pause the task", True),
    ("What did you change?", "What did you change?", False),
])
def test_attached_action_cannot_extract_permission_from_negation_or_question(connection, speech, quote, interrupt):
    connection.transcripts = [{"role": "user", "text": speech, "time": time.time()}]
    with pytest.raises(voice.VoiceError):
        connection.require_user_request({"request_quote": quote}, interrupt=interrupt)


@pytest.mark.asyncio
async def test_attached_request_is_consumed_on_uncertain_delivery(connection):
    quote = "Please change the button label to Start"
    connection.transcripts = [{"role": "user", "text": quote, "time": time.time()}]
    connection.host.api_send_command.return_value = JSONResponse({"error": "Uncertain delivery"}, status_code=503)
    item = {"name": "send_instructions", "arguments": json.dumps({"request_quote": quote,
        "summary": "Change the button label to Start", "interrupt": False})}
    with pytest.raises(voice.VoiceError):
        await connection.execute(item)
    with pytest.raises(voice.VoiceError):
        await connection.execute(item)
    connection.host.api_send_command.assert_awaited_once()


@pytest.mark.parametrize("status", ["unknown", "stalled", "busy"])
@pytest.mark.asyncio
async def test_interrupt_only_claims_stopped_for_positive_idle_evidence(connection, monkeypatch, status):
    connection.host.async_detect_activity.return_value = {"status": status}
    monkeypatch.setattr(voice.asyncio, "sleep", AsyncMock())
    result = await connection.pause()
    assert not result["paused"] and not result["durable_hold"]
    connection.host.api_interrupt_session.assert_awaited_once()
    connection.host.api_send_command.assert_not_awaited()
    assert not connection.state()["paused"]


@pytest.mark.asyncio
async def test_explicit_interrupt_uses_existing_route_without_installing_supervision(connection):
    connection.host.async_detect_activity.return_value = {"status": "idle"}
    result = await connection.pause()
    assert result == {"paused": True, "durable_hold": False}
    request, name = connection.host.api_interrupt_session.await_args.args
    assert name == "alpha" and request.state.voice_target_binding == connection.binding
    connection.host._restart_codex_for_session.assert_not_awaited()
    connection.host.api_send_command.assert_not_awaited()
    assert not connection.state()["paused"]


@pytest.mark.asyncio
async def test_parallel_voice_start_rejected_before_provider_or_coding_io(connection):
    voice.store(connection.host.MESSAGES_DIR).update(lambda rows: rows.update(beta={
        "owner_id": "one", "active": True, "connected": True, "heartbeat": time.time()}))
    with pytest.raises(voice.VoiceError, match="other voice"):
        await connection.run("v=0")
    connection.host.api_send_command.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_sideband_attach_hangs_up_created_provider_session_and_clears_state(connection, monkeypatch):
    requests = []
    provider_id = "synthetic/session?private"

    class Provider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return SimpleNamespace(status_code=201, json=lambda: {
                "session": {"id": provider_id}, "transport": {"sdp": "v=0 synthetic"}})

    class FailedAttach:
        async def __aenter__(self):
            raise OSError("Synthetic attach failure")

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: Provider())
    monkeypatch.setattr(voice.websockets, "connect", lambda *args, **kwargs: FailedAttach())
    connection.transcripts = [{"role": "user", "text": "Private unfinished speech", "time": time.time()}]
    connection.session_control.offer = {"summary": "Private pending action"}
    with pytest.raises(OSError, match="Synthetic attach failure"):
        await connection.run("v=0")
    assert requests[-1][0] == "https://api.openai.com/v1/live/sessions/synthetic%2Fsession%3Fprivate/hangup"
    assert not connection.state()["active"] and not connection.state()["connected"]
    assert not connection.transcripts and connection.session_control.offer is None
    assert not any(call.args[0].get("type") == "answer" for call in connection.ws.send_json.await_args_list)
    connection.host.api_send_command.assert_not_awaited()
    connection.host.api_interrupt_session.assert_not_awaited()


def test_malformed_browser_json_has_sanitized_error_and_no_coding_side_effect(client, host):
    client.cookies.set("test", "one")
    with client.websocket_connect("/api/sessions/alpha/voice", headers={"origin": "http://testserver"}) as socket:
        socket.send_text("not-json-private-speech-123")
        error = socket.receive_json()
        assert error["type"] == "error" and "private-speech" not in error["message"]
    host.api_send_command.assert_not_awaited()
    assert not voice.active(host.MESSAGES_DIR, "alpha")


@pytest.mark.parametrize("rootless", [False, True])
@pytest.mark.asyncio
async def test_successful_voice_session_is_passive_and_closes_transient_resources(connection, monkeypatch, rootless):
    if rootless:
        connection.binding["resume_uuid"] = ""
        connection.host._terminal_history_binding = lambda *args: None
    requests, provider_sent = [], []

    class Provider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, url, **kwargs):
            requests.append((url, kwargs))
            return SimpleNamespace(status_code=201, json=lambda: {
                "session": {"id": "synthetic-live"}, "transport": {"sdp": "v=0 synthetic answer"}})

    class Sideband:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def send(self, payload):
            provider_sent.append(json.loads(payload))

    async def provider_events():
        connection.transcripts.append({"role": "user", "text": "Volatile voice only", "time": time.time()})
        connection.ended = True

    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: Provider())
    monkeypatch.setattr(voice.websockets, "connect", lambda *args, **kwargs: Sideband())
    connection.provider_events = provider_events
    for method in ("observe", "browser_events", "heartbeat", "response_worker"):
        monkeypatch.setattr(connection, method, AsyncMock())
    await connection.run("v=0 synthetic offer")
    assert requests[0][1]["json"]["session"]["store"] is False
    replies = [call.args[0] for call in connection.ws.send_json.await_args_list]
    answer = next(reply for reply in replies if reply["type"] == "answer")
    assert answer["sdp"] == "v=0 synthetic answer"
    assert answer["nonce"] == connection.nonce and answer["generation"] == GENERATION
    assert {"type": "session.close"} in provider_sent
    assert not connection.state()["active"] and not connection.transcripts
    connection.host._restart_codex_for_session.assert_not_awaited()
    connection.host.api_send_command.assert_not_awaited()
    connection.host.api_interrupt_session.assert_not_awaited()
    if rootless:
        connection.host._read_terminal_history.assert_not_called()


@pytest.mark.asyncio
async def test_cancelled_pending_start_clears_own_guard_and_transient_speech(connection, monkeypatch):
    class Provider:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def post(self, *args, **kwargs):
            raise asyncio.CancelledError()

    monkeypatch.setattr(voice.httpx, "AsyncClient", lambda **kwargs: Provider())
    connection.transcripts = [{"role": "user", "text": "Unsaved draft", "time": time.time()}]
    with pytest.raises(asyncio.CancelledError):
        await connection.run("v=0")
    assert not connection.state()["active"] and not connection.state()["connected"]
    assert not connection.transcripts
    connection.host.api_send_command.assert_not_awaited()
