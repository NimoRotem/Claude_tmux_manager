"""Focused Lisa admin bug-report handoff tests."""
import os
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlsplit

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

from fastapi.testclient import TestClient

import app as dashboard


TICKET = "t" * 43
PAYLOAD = {"report_id": "a" * 24, "prompt": "Trusted server-built report"}


def _pending(location):
    return parse_qs(urlsplit(location).query)["pending"][0]


def setup_function():
    dashboard._pending_bug_handoffs.clear()


def test_public_ticket_is_redeemed_then_survives_dashboard_login():
    client = TestClient(dashboard.app)
    with patch.object(dashboard, "_redeem_lisa_bug_ticket", new=AsyncMock(return_value=PAYLOAD)) as redeem:
        first = client.get("/bug-handoff", params={"ticket": TICKET}, follow_redirects=False)
    assert first.status_code == 303
    pending = _pending(first.headers["location"])
    redeem.assert_awaited_once_with(TICKET)

    with patch.object(dashboard, "_current_user", return_value={"id": "u_thief"}):
        stolen = TestClient(dashboard.app).get("/bug-handoff", params={"pending": pending})
    assert stolen.status_code == 404
    assert "different browser" in stolen.text

    login = client.get("/bug-handoff", params={"pending": pending})
    assert login.status_code == 200
    assert "Log in" in login.text
    assert "location.pathname+location.search" in login.text

    created = dashboard.JSONResponse({"ok": True, "name": "lisa-bug-login"})
    sent = dashboard.JSONResponse({"ok": True})
    with patch.object(dashboard, "_current_user", return_value={"id": "u_test"}), \
            patch.object(dashboard, "api_create_session", new=AsyncMock(return_value=created)), \
            patch.object(dashboard, "api_send_command", new=AsyncMock(return_value=sent)):
        resumed = client.get("/bug-handoff", params={"pending": pending}, follow_redirects=False)
    assert resumed.status_code == 303
    assert resumed.headers["location"] == "/?bug_session=lisa-bug-login"


def test_handoff_is_idempotent_and_never_resends_on_refresh():
    created = dashboard.JSONResponse({"ok": True, "name": "lisa-bug-once"})
    sent = dashboard.JSONResponse({"ok": True})
    with patch.object(dashboard, "AUTH_PASS", ""), \
            patch.object(dashboard, "_redeem_lisa_bug_ticket", new=AsyncMock(return_value=PAYLOAD)), \
            patch.object(dashboard, "api_create_session", new=AsyncMock(return_value=created)) as create, \
            patch.object(dashboard, "api_send_command", new=AsyncMock(return_value=sent)) as send:
        client = TestClient(dashboard.app)
        start = client.get("/bug-handoff", params={"ticket": TICKET}, follow_redirects=False)
        pending = _pending(start.headers["location"])
        one = client.get("/bug-handoff", params={"pending": pending}, follow_redirects=False)
        two = client.get("/bug-handoff", params={"pending": pending}, follow_redirects=False)
    assert one.headers["location"] == two.headers["location"] == "/?bug_session=lisa-bug-once"
    assert create.await_count == 1
    assert send.await_count == 1
    assert send.await_args.args[2].command == PAYLOAD["prompt"]


def test_two_pending_reports_share_the_same_browser_binding():
    second_ticket = "u" * 43
    second_payload = {"report_id": "b" * 24, "prompt": "Second trusted report"}
    client = TestClient(dashboard.app)
    redeem = AsyncMock(side_effect=[PAYLOAD, second_payload])
    with patch.object(dashboard, "_redeem_lisa_bug_ticket", new=redeem):
        first = client.get("/bug-handoff", params={"ticket": TICKET}, follow_redirects=False)
        second = client.get("/bug-handoff", params={"ticket": second_ticket}, follow_redirects=False)
    first_pending = _pending(first.headers["location"])
    second_pending = _pending(second.headers["location"])
    with patch.object(dashboard, "_current_user", return_value={"id": "u_test"}), \
            patch.object(dashboard, "api_create_session", new=AsyncMock(
                side_effect=[dashboard.JSONResponse({"ok": True, "name": "lisa-bug-first"}),
                             dashboard.JSONResponse({"ok": True, "name": "lisa-bug-second"})])), \
            patch.object(dashboard, "api_send_command", new=AsyncMock(
                return_value=dashboard.JSONResponse({"ok": True}))):
        first_result = client.get("/bug-handoff", params={"pending": first_pending}, follow_redirects=False)
        second_result = client.get("/bug-handoff", params={"pending": second_pending}, follow_redirects=False)
    assert first_result.status_code == second_result.status_code == 303
    assert first_result.headers["location"] == "/?bug_session=lisa-bug-first"
    assert second_result.headers["location"] == "/?bug_session=lisa-bug-second"


def test_send_failure_is_uncertain_and_does_not_invite_blind_resend():
    created = dashboard.JSONResponse({"ok": True, "name": "lisa-bug-uncertain"})
    failed = dashboard.JSONResponse({"error": "unknown"}, status_code=500)
    with patch.object(dashboard, "AUTH_PASS", ""), \
            patch.object(dashboard, "_redeem_lisa_bug_ticket", new=AsyncMock(return_value=PAYLOAD)), \
            patch.object(dashboard, "api_create_session", new=AsyncMock(return_value=created)), \
            patch.object(dashboard, "api_send_command", new=AsyncMock(return_value=failed)) as send:
        client = TestClient(dashboard.app)
        start = client.get("/bug-handoff", params={"ticket": TICKET}, follow_redirects=False)
        pending = _pending(start.headers["location"])
        first = client.get("/bug-handoff", params={"pending": pending}, follow_redirects=False)
        second = client.get("/bug-handoff", params={"pending": pending}, follow_redirects=False)
    assert first.headers["location"] == second.headers["location"]
    assert "bug_send=uncertain" in first.headers["location"]
    assert send.await_count == 1
    assert "Inspect this session before retrying" in dashboard.HTML_PAGE


def test_rejects_invalid_ticket_and_raw_prompt_post():
    client = TestClient(dashboard.app)
    invalid = client.get("/bug-handoff", params={"ticket": "short"})
    raw = client.post("/bug-handoff", data={"report": "arbitrary prompt"})
    assert invalid.status_code == 410
    assert raw.status_code == 405


def test_dashboard_selects_and_scrubs_the_handoff_session():
    assert "async function loadAll(preferredSession)" in dashboard.HTML_PAGE
    assert "url.searchParams.get('bug_session')" in dashboard.HTML_PAGE
    assert "history.replaceState" in dashboard.HTML_PAGE
    assert "await loadAll(bugSession)" in dashboard.HTML_PAGE
