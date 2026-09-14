"""Direct-first proxy controls with private synthetic state and no network I/O."""

import copy
import json
from unittest.mock import AsyncMock

import pytest
from fastapi import Request
from pydantic import ValidationError

import app


@pytest.fixture
def proxy(tmp_path, monkeypatch):
    root = tmp_path / "browser"
    root.mkdir()
    (root / "state").mkdir()
    path = root / "proxy.json"
    config = {
        "enabled": True,
        "provider": "synthetic",
        "password": "synthetic-private-password",
        "username": "synthetic-account",
        "sessions": {
            "default": {"enabled": True, "local_port": 3128, "session_id": "default-sticky"},
            "other": {"enabled": True, "local_port": 3129, "session_id": "other-sticky"},
        },
    }
    path.write_text(json.dumps(config))
    monkeypatch.setattr(app, "CB_ROOT", root)
    monkeypatch.setattr(app, "BROWSER_PROXY_CONF", path)
    monkeypatch.setattr(app, "_auth_admin_ok", lambda request: True)
    monkeypatch.setattr(app, "_proxy_presets", lambda: {})
    monkeypatch.setattr(app, "_proxy_usage", lambda: {})
    monkeypatch.setattr(app, "_load_browser_sessions", lambda: [
        {"id": "default", "name": "Default browser"},
        {"id": "other", "name": "Other browser"},
    ])
    monkeypatch.setattr(app, "_proxy_exit_info", AsyncMock(side_effect=AssertionError("Unexpected network check")))
    return root, path, config


def request():
    return Request({"type": "http", "headers": [], "method": "POST", "path": "/api/browser/proxy"})


def test_new_browser_defaults_direct_without_changing_existing_accounts(proxy):
    _root, path, before = proxy
    app._claim_browser_proxy_session({"id": "new", "display": 101})
    saved = json.loads(path.read_text())
    assert saved["sessions"]["new"]["enabled"] is False
    assert saved["sessions"]["new"]["local_port"] == 3130
    assert saved["sessions"]["new"]["session_id"]
    for sid in ("default", "other"):
        assert saved["sessions"][sid] == before["sessions"][sid]
    assert saved["password"] == before["password"]


@pytest.mark.parametrize("enabled", [False, True])
def test_existing_browser_keeps_its_explicit_route(proxy, enabled):
    _root, path, config = proxy
    config["sessions"]["other"]["enabled"] = enabled
    path.write_text(json.dumps(config))
    app._claim_browser_proxy_session({"id": "other", "display": 100})
    assert json.loads(path.read_text()) == config


@pytest.mark.asyncio
async def test_enabling_proxy_requires_explicit_direct_failure(proxy):
    _root, path, _config = proxy
    before = path.read_bytes()
    response = await app.api_browser_proxy_set(app.BrowserProxyBody(enabled=True), request())
    assert response.status_code == 400
    assert "normal IP first" in json.loads(response.body)["error"]
    assert path.read_bytes() == before


@pytest.mark.parametrize("acknowledgment", ["true", "false", 1, 0, None, []])
def test_direct_failure_acknowledgment_is_a_strict_boolean(acknowledgment):
    with pytest.raises(ValidationError):
        app.BrowserProxyBody(enabled=True, direct_failed=acknowledgment)


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_toggle_changes_only_default_session_and_its_timezone(proxy, enabled):
    root, path, config = proxy
    before_other = copy.deepcopy(config["sessions"]["other"])
    default_cache = root / "state" / "default.geo.json"
    other_cache = root / "state" / "other.geo.json"
    default_cache.write_text("default cached timezone")
    other_cache.write_text("other cached timezone")
    response = await app.api_browser_proxy_set(
        app.BrowserProxyBody(enabled=enabled, direct_failed=enabled), request()
    )
    assert response.status_code == 200
    saved = json.loads(path.read_text())
    assert saved["enabled"] is True
    assert saved["sessions"]["default"]["enabled"] is enabled
    assert saved["sessions"]["default"]["session_id"] == "default-sticky"
    assert saved["sessions"]["other"] == before_other
    assert not default_cache.exists()
    assert other_cache.read_text() == "other cached timezone"
    assert saved["password"] == config["password"]
    assert path.stat().st_mode & 0o777 == 0o600


@pytest.mark.asyncio
async def test_enabling_default_can_start_disabled_relay_without_rewriting_other_rows(proxy):
    _root, path, config = proxy
    config["enabled"] = False
    config["sessions"]["other"]["enabled"] = False
    path.write_text(json.dumps(config))
    response = await app.api_browser_proxy_set(
        app.BrowserProxyBody(enabled=True, direct_failed=True), request()
    )
    saved = json.loads(path.read_text())
    assert response.status_code == 200 and saved["enabled"] is True
    assert saved["sessions"]["other"] == config["sessions"]["other"]


@pytest.mark.asyncio
@pytest.mark.parametrize("explicit_flag", [True, False])
async def test_enabling_default_cannot_reactivate_globally_paused_other_routes(proxy, explicit_flag):
    root, path, config = proxy
    config["enabled"] = False
    if not explicit_flag:
        # Older relay rows treated an absent flag as enabled.
        del config["sessions"]["other"]["enabled"]
    path.write_text(json.dumps(config))
    before = path.read_bytes()
    cache = root / "state" / "other.geo.json"
    cache.write_text("other cached timezone")
    response = await app.api_browser_proxy_set(
        app.BrowserProxyBody(enabled=True, direct_failed=True), request()
    )
    assert response.status_code == 409
    assert "globally paused" in json.loads(response.body)["error"]
    assert path.read_bytes() == before
    assert cache.read_text() == "other cached timezone"


@pytest.mark.asyncio
async def test_editing_provider_fields_does_not_enable_routes_or_clear_caches(proxy):
    root, path, config = proxy
    cache = root / "state" / "default.geo.json"
    cache.write_text("unchanged")
    response = await app.api_browser_proxy_set(
        app.BrowserProxyBody(username=" renamed-account ", password="", country=" US "), request()
    )
    saved = json.loads(path.read_text())
    assert response.status_code == 200
    assert saved["username"] == "renamed-account" and saved["country"] == "us"
    assert saved["password"] == config["password"]
    assert saved["sessions"] == config["sessions"]
    assert cache.read_text() == "unchanged"


@pytest.mark.asyncio
@pytest.mark.parametrize("master,default", [(False, True), (True, False), (True, True)])
async def test_status_reports_effective_routes_without_exposing_password(proxy, master, default):
    _root, path, config = proxy
    config["enabled"] = master
    config["sessions"]["default"]["enabled"] = default
    path.write_text(json.dumps(config))
    response = await app.api_browser_proxy_get(request())
    data = json.loads(response.body)
    assert data["enabled"] is (master and default)
    rows = {row["id"]: row for row in data["browsers"]}
    assert rows["default"]["enabled"] is (master and default)
    assert rows["other"]["enabled"] is master
    assert data["password_set"] is True
    assert config["password"] not in response.body.decode()


@pytest.mark.asyncio
async def test_status_missing_opt_in_flag_is_direct(proxy):
    _root, path, config = proxy
    del config["sessions"]["default"]["enabled"]
    path.write_text(json.dumps(config))
    response = await app.api_browser_proxy_get(request())
    data = json.loads(response.body)
    assert data["enabled"] is False
    assert data["browsers"][0]["enabled"] is False


@pytest.mark.asyncio
async def test_proxy_read_and_write_remain_admin_only(proxy, monkeypatch):
    _root, path, _config = proxy
    before = path.read_bytes()
    monkeypatch.setattr(app, "_auth_admin_ok", lambda request: False)
    read = await app.api_browser_proxy_get(request(), check=1)
    write = await app.api_browser_proxy_set(
        app.BrowserProxyBody(enabled=True, direct_failed=True), request()
    )
    assert read.status_code == write.status_code == 403
    assert path.read_bytes() == before
    app._proxy_exit_info.assert_not_awaited()
