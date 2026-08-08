"""Regression coverage for dashboard login and logout."""

import hashlib
import os
from urllib.parse import parse_qs, quote, urlparse

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")

from fastapi.testclient import TestClient

import app as app_module


def test_google_enabled_login_page_keeps_the_password_alternative(monkeypatch):
    monkeypatch.setattr(app_module, "_google_login_enabled", lambda: True)

    page = app_module._login_page()

    assert (
        'id="gbtn"' in page,
        'name="username"' in page,
        'name="password"' in page,
        "or use username and password" in page,
    ) == (True, True, True, True)


def test_password_fallback_remains_available_without_google(monkeypatch):
    monkeypatch.setattr(app_module, "_google_login_enabled", lambda: False)

    page = app_module._login_page()

    assert (
        'id="gbtn"' in page,
        'name="username"' in page,
        'name="password"' in page,
    ) == (False, True, True)


def test_google_start_binds_signed_state_to_requesting_browser(monkeypatch):
    monkeypatch.setattr(app_module, "_google_client", lambda: ("client-id", "client-secret"))
    client = TestClient(app_module.app, base_url="https://testserver")

    response = client.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(response.headers["location"]).query)["state"][0]
    cookie_header = response.headers["set-cookie"].lower()

    assert (
        response.status_code,
        client.cookies.get(app_module.GOOGLE_LOGIN_STATE_COOKIE),
        "httponly" in cookie_header,
        "secure" in cookie_header,
        "samesite=lax" in cookie_header,
    ) == (307, hashlib.sha256(state.encode()).hexdigest(), True, True, True)


def test_google_callback_rejects_state_from_another_browser(monkeypatch):
    monkeypatch.setattr(app_module, "_google_client", lambda: ("client-id", "client-secret"))
    monkeypatch.setattr(
        app_module.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("token exchange must not run for an unbound state")
        ),
    )
    initiating_browser = TestClient(app_module.app, base_url="https://testserver")
    state_response = initiating_browser.get("/auth/google/start", follow_redirects=False)
    state = parse_qs(urlparse(state_response.headers["location"]).query)["state"][0]
    other_browser = TestClient(app_module.app, base_url="https://testserver")

    response = other_browser.get(
        "/auth/google/callback?code=fake&state=" + quote(state),
        follow_redirects=False,
    )

    assert (response.status_code, response.headers["location"]) == (303, "/?gerr=state")


def test_visible_codex_sign_out_control_logs_out_of_the_dashboard():
    html = app_module.HTML_PAGE

    assert (
        'onclick="doLogout()">Sign out</button>' in html,
        'onclick="codexLogout()">Sign out of Codex</button>' in html,
    ) == (True, False)


def test_dashboard_logout_removes_the_session_cookie():
    client = TestClient(app_module.app)
    client.cookies.set(
        app_module.AUTH_COOKIE,
        "stale-session",
        domain="testserver.local",
        path="/",
    )

    response = client.post("/logout")

    assert (
        response.status_code,
        client.cookies.get(app_module.AUTH_COOKIE),
    ) == (200, None)
