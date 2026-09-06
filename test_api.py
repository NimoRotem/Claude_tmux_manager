"""Integration tests for tmux Dashboard API endpoints using FastAPI TestClient."""
import asyncio
import json
import os
import re
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Patch environment before importing app
os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

from fastapi.testclient import TestClient

import app as app_module
from app import AUTH_COOKIE, AUTH_PASS, AUTH_USER, BRAND_NAME, _make_token, app

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
def isolated_session_tab_labels(tmp_path, monkeypatch):
    """Never let frontend/API tests mutate the live dashboard's label store."""
    store = app_module.LockedJsonStore(
        tmp_path / "session-tab-labels.json",
        lambda: {"version": 1, "sessions": {}},
    )
    monkeypatch.setattr(app_module, "_session_tab_labels", store)


@pytest.fixture(autouse=True)
def isolated_session_tab_order(tmp_path, monkeypatch):
    """Never let API tests mutate any live account's saved tab ordering."""
    store = app_module.LockedJsonStore(
        tmp_path / "session-tab-order.json",
        lambda: {"version": 1, "owners": {}},
    )
    monkeypatch.setattr(app_module, "_session_tab_order_store", store)


@pytest.fixture(autouse=True)
def isolated_session_lifecycle_and_owners(tmp_path, monkeypatch):
    """API tests must never mutate live owner or reboot-recovery state."""
    lifecycle = app_module.SessionLifecycleStore(tmp_path / "session-lifecycle.json")
    monkeypatch.setattr(app_module, "_session_lifecycle", lifecycle)
    monkeypatch.setattr(
        app_module, "SESSION_OWNERS_FILE", tmp_path / "session_owners.json"
    )
    for session in MOCK_SESSIONS:
        app_module._set_session_owner(session["name"], "admin")


@pytest.fixture(autouse=True)
def stable_test_terminal_identity(monkeypatch):
    """Generic endpoint tests use one immutable, owner-bound tmux incarnation."""
    def binding(name, owner_id):
        if not owner_id:
            return None
        return {
            "name": name,
            "owner_id": owner_id,
            "generation": "",
            "session_id": "$99",
            "session_created": "1700000000",
            "managed": False,
            "key": (name, owner_id, "", "$99@1700000000"),
        }

    monkeypatch.setattr(app_module, "_terminal_binding", binding)
    monkeypatch.setattr(app_module, "_terminal_binding_state", lambda _binding: "current")


@pytest.fixture(autouse=True)
def isolated_session_close_runtime():
    app_module._session_close_jobs.clear()
    app_module._session_close_tasks.clear()
    app_module._session_close_barriers.clear()
    yield
    for task in app_module._session_close_tasks.values():
        if not task.done():
            task.cancel()
    app_module._session_close_jobs.clear()
    app_module._session_close_tasks.clear()
    app_module._session_close_barriers.clear()


# ─── Auth & Middleware Tests ───


class TestAuthMiddleware:
    def test_unauthenticated_returns_login_page(self, client):
        resp = client.get("/", follow_redirects=False)
        assert resp.status_code == 200
        assert f"{BRAND_NAME} Dashboard" in resp.text
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


class TestAdminSessionRecovery:
    def test_admin_starts_bodyless_controller_recovery(self, authed_client):
        controller = AsyncMock(return_value={
            "ok": True,
            "accepted": True,
            "job": {"id": "job_abcdefghijklmnop", "status": "queued"},
            "_status": 202,
        })
        with patch("app._controller_call", controller):
            resp = authed_client.post(
                "/api/admin/sessions/recover",
                headers={"X-Tmux-Recovery": "1"},
            )

        assert resp.status_code == 202
        assert resp.json()["job"]["status"] == "queued"
        controller.assert_awaited_once_with(
            "durable_reconcile_start", owner_id="admin"
        )

    def test_recovery_requires_confirmation_header(self, authed_client):
        controller = AsyncMock()
        with patch("app._controller_call", controller):
            resp = authed_client.post("/api/admin/sessions/recover")

        assert resp.status_code == 400
        controller.assert_not_awaited()

    def test_non_admin_cannot_start_recovery(self, authed_client):
        controller = AsyncMock()
        member = {"id": "u_member", "username": "member", "role": "user"}
        with (
            patch("app._current_user", return_value=member),
            patch("app._controller_call", controller),
        ):
            resp = authed_client.post(
                "/api/admin/sessions/recover",
                headers={"X-Tmux-Recovery": "1"},
            )

        assert resp.status_code == 403
        controller.assert_not_awaited()

    def test_admin_polls_only_own_recovery_job(self, authed_client):
        controller = AsyncMock(return_value={
            "ok": True,
            "job": {
                "id": "job_abcdefghijklmnop",
                "status": "completed",
                "ready": ["debug"],
                "pending": [],
            },
            "_status": 200,
        })
        with patch("app._controller_call", controller):
            resp = authed_client.get(
                "/api/admin/sessions/recover/job_abcdefghijklmnop"
            )

        assert resp.status_code == 200
        assert resp.json()["job"]["ready"] == ["debug"]
        controller.assert_awaited_once_with(
            "durable_reconcile_status",
            owner_id="admin",
            job_id="job_abcdefghijklmnop",
        )


class TestDashboardFrontendRegressions:
    def test_admin_tools_include_async_tab_recovery(self, authed_client):
        html = authed_client.get("/").text

        assert "> Recover tabs</button>" in html
        assert "async function recoverTabs()" in html
        assert "'/api/admin/sessions/recover'" in html
        assert "'X-Tmux-Recovery':'1'" in html
        assert "function _recoveryModalOwns(run)" in html
        assert "transientFailures>5" in html
        assert "recovery will continue" in html

    def test_delete_ui_saves_knowledge_before_close(self, authed_client):
        html = authed_client.get("/").text

        assert "Summarize and close ${esc(name)}?" in html
        assert "Summarize &amp; Close" in html
        assert "Saving session knowledge…" in html
        assert "/close/'+encodeURIComponent(job.id)" in html
        assert "The tab remains open" in html
        assert "transientFailures>5" in html

    def test_session_tab_bar_stays_pinned_while_page_scrolls(self, authed_client):
        html = authed_client.get("/").text
        nav_rule = re.search(r"\.nav-wrapper\{([^}]*)\}", html)

        assert nav_rule
        declarations = nav_rule.group(1)
        assert "position:sticky" in declarations
        assert "top:0" in declarations
        assert "z-index:1000" in declarations

    def test_toolbar_keeps_only_compact_stats_and_settings_visible(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        nav_start = html.index('<div class="nav-right">')
        status_menu_start = html.index(
            '<div class="nav-status-menu"',
            nav_start,
        )
        tools_start = html.index('<div class="nav-tools-wrap">', status_menu_start)
        nav_before_menu = html[nav_start:status_menu_start]

        assert 'id="nav-cpu-summary"' in nav_before_menu
        assert 'id="nav-usage-cap-summary"' in nav_before_menu
        assert '>Status <span class="nav-status-chevron">' in nav_before_menu
        assert 'title="Settings"' in html[tools_start:]

        for hidden_id in (
            "nav-server-stats",
            "nav-usage",
            "nav-codex-alert",
            "nav-browser-badge",
            "codex-auth",
            "nav-status-whoami",
        ):
            assert f'id="{hidden_id}"' not in nav_before_menu

    def test_status_dropdown_owns_details_account_and_actions(self, authed_client):
        html = authed_client.get("/").text
        status_start = html.index('<div class="nav-status-menu"')
        status_end = html.index('<div class="nav-tools-wrap">', status_start)
        status = html[status_start:status_end]
        tools_end = html.index(
            "<!-- Member-only nav controls",
            status_end,
        )
        tools = html[status_end:tools_end]

        for moved_id in (
            "nav-server-stats",
            "nav-usage",
            "nav-codex-alert",
            "nav-browser-badge",
            "codex-auth",
            "nav-status-whoami",
        ):
            assert f'id="{moved_id}"' in status

        assert "Full system stats" in status
        assert "Log out" in status
        assert "System Stats" not in tools
        assert 'nav-tools-mobile" type="button" role="menuitem" onclick="doLogout()' in tools
        assert "nav-tools-whoami" not in html
        assert "mobile-bottom-bar" not in html
        assert "syncMobileBottomBar" not in html

    def test_mobile_toolbar_reserves_header_for_tabs_and_gear(self, authed_client):
        html = authed_client.get("/").text
        mobile_start = html.index("@media(max-width:768px){", html.index("/* Mobile */"))
        mobile_end = html.index("</style>", mobile_start)
        mobile = html[mobile_start:mobile_end]

        assert ".top-nav{padding:0 0 0 8px}" in mobile
        assert ".nav-new-btn,.nav-compact-status,.nav-status-toggle,.nav-status-text," in mobile
        assert ".nav-right>.member-only{display:none!important}" in mobile
        assert "body.member-simple .nav-tools-wrap{display:block}" in mobile
        assert "body.member-simple .nav-status-wrap{display:block" not in mobile
        assert ".nav-tools-mobile{display:flex}" in mobile
        assert ".nav-tools-menu{position:fixed" in mobile

        nav_start = html.index('<div class="nav-wrapper">')
        nav_end = html.index('<div class="auth-dropdown"', nav_start)
        nav = html[nav_start:nav_end]
        assert 'id="top-nav"' in nav
        assert 'id="nav-tools-toggle"' in nav
        assert 'aria-label="Settings and tools"' in nav

    def test_mobile_gear_preserves_displaced_toolbar_actions(self, authed_client):
        html = authed_client.get("/").text
        menu_start = html.index('<div class="nav-tools-menu" id="nav-tools-menu"')
        menu_end = html.index("</div>\n  </div>", menu_start)
        menu = html[menu_start:menu_end]

        for label in (
            "New session",
            "Status &amp; usage",
            "Settings",
            "My private browser",
            "Connections",
            "Log out",
        ):
            assert label in menu
        assert "createSessionAuto();closeToolsMenu()" in menu
        assert "toggleStatusMenu(event)" in menu
        assert "openSettings('browser');closeToolsMenu()" in menu
        assert "openConnections();closeToolsMenu()" in menu
        assert "doLogout();closeToolsMenu()" in menu
        assert 'nav-tools-mobile nav-tools-admin" type="button" role="menuitem"' in menu
        assert menu.count('role="menuitem"') == 9

    def test_new_session_controls_auto_create_without_asking_for_a_name(
        self, authed_client
    ):
        html = authed_client.get("/").text
        nav_start = html.index('<div class="nav-wrapper">')
        nav_end = html.index('<div class="auth-dropdown"', nav_start)
        nav = html[nav_start:nav_end]
        create_start = html.index("async function createSessionAuto()")
        create_end = html.index("// ── Connections", create_start)
        create = html[create_start:create_end]

        assert 'class="nav-new-btn" onclick="createSessionAuto()"' in nav
        assert "createSessionAuto();closeToolsMenu()" in nav
        assert "new-session-name" not in html
        assert "showCreateModal" not in html
        assert "JSON.stringify({name:_autoSessionName()})" in create
        assert "crypto.getRandomValues" in html
        assert "for(let attempt=0;attempt<5;attempt++)" in create
        assert "if(resp.status===409&&data.code==='name_conflict')continue" in create
        assert "if(_sessionCreatePending)return" in create
        assert "_sessionCreatePending=false" in create
        assert "_sessionCreateModalOwns(run)" in create
        assert "data.ok!==true||typeof data.name!=='string'||!data.name" in create
        assert "await _waitForCreatedSession(created.name)" in create
        assert "for(let attempt=0;attempt<30;attempt++)" in html
        assert "encodeURIComponent(name)+'/uploads'" in html
        assert "Session is still starting" in create
        assert 'onclick="closeModal();loadAll()">Refresh tabs</button>' in create
        assert "Creating session…" in create
        assert "Couldn't create session" in create
        assert 'onclick="createSessionAuto()">Try again</button>' in create

    def test_tools_menu_tracks_accessible_expanded_state(self, authed_client):
        html = authed_client.get("/").text
        toggle_start = html.index("function toggleToolsMenu(e)")
        toggle_end = html.index("// Close dropdowns on outside click", toggle_start)
        controls = html[toggle_start:toggle_end]

        assert "button.setAttribute('aria-expanded',open?'true':'false')" in controls
        assert "button.setAttribute('aria-expanded','false')" in controls
        assert "closeStatusMenu()" in controls

    def test_key_bar_has_plan_mode_shortcut(self, authed_client):
        html = authed_client.get("/").text

        assert "sendSlashCommand('${name}','/plan')" in html
        assert 'title="Switch to Plan mode">/plan</button>' in html
        assert "sendSlashCommand('${name}','/work')" not in html

    def test_voice_button_names_recording_and_transcribing_states(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        recording_start = html.index("async function toggleRecording(key)")
        recording_end = html.index("async function sendChat(name)", recording_start)

        assert (
            "btn.setAttribute('aria-label','Stop recording and transcribe')"
            in html[recording_start:recording_end],
            "btn.setAttribute('aria-label','Transcribing voice message')"
            in html[recording_start:recording_end],
        ) == (True, True)

    def test_voice_recording_has_visible_recording_and_transcribing_feedback(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        recording_rule = re.search(
            r"\.composer-action\.is-recording\{([^}]*)\}",
            html,
        )
        spinner_rule = re.search(r"\.composer-spin\{([^}]*)\}", html)

        assert (
            bool(recording_rule and "background:#da3633" in recording_rule.group(1)),
            bool(spinner_rule and "animation:" in spinner_rule.group(1)),
        ) == (True, True)

    def test_composer_button_accessible_name_tracks_its_current_action(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        update_start = html.index("function updateComposerBtn(key)")
        update_end = html.index("function composerAction(key)", update_start)

        assert "btn.setAttribute('aria-label',label)" in html[update_start:update_end]

    def test_successful_sends_return_both_buttons_to_microphone_state(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        chat_start = html.index("async function sendChat(name)")
        chat_end = html.index("function setOptimisticBusy", chat_start)
        raw_start = html.index("async function sendCmd(name,source)")
        raw_end = html.index("function startRawPolling", raw_start)

        assert (
            "updateComposerBtn('chat-'+name)" in html[chat_start:chat_end],
            "updateComposerBtn(source+'-'+name)" in html[raw_start:raw_end],
        ) == (True, True)

    def test_restored_drafts_restore_the_send_button_state(self, authed_client):
        html = authed_client.get("/").text
        restore_start = html.index("function restoreDrafts()")
        restore_end = html.index("function updateFavicon", restore_start)

        assert "updateComposerBtn(key)" in html[restore_start:restore_end]

    def test_user_request_completion_plays_a_glass_chime(self, authed_client):
        html = authed_client.get("/").text
        chime_start = html.index("// ── Completion chime")
        chime_end = html.index("// Local chat messages mirror", chime_start)
        chime = html[chime_start:chime_end]
        send_start = html.index("function setOptimisticBusy(name)")
        send_end = html.index("function scheduleBusyVerification", send_start)

        assert "window.AudioContext||window.webkitAudioContext" in chime
        assert "master.gain.setValueAtTime(0.84,now)" in chime
        assert "prev==='busy'&&status==='idle'&&_completionWatch[name]" in chime
        assert "playCompletionChime()" in chime
        assert "armCompletionChime(name)" in html[send_start:send_end]

    def test_hidden_dashboard_keeps_lightweight_completion_polling(self, authed_client):
        import shutil
        import subprocess

        html = authed_client.get("/").text
        poll_start = html.index("async function pollStatus()")
        poll_end = html.index("// --- Inline server stats", poll_start)
        poll = html[poll_start:poll_end]

        assert "Object.values(lastStatus).some(status=>status==='busy')" in poll
        assert (
            "const needsCacheStatus=sessions.some(s=>"
            "['warm','warning'].includes(_cacheTelemetryState(s).phase))"
        ) in poll
        assert (
            "if(document.hidden&&!Object.keys(_completionWatch).length"
            "&&!hasBusySession&&!needsIdleNudgeStatus&&!needsCacheStatus)return"
        ) in poll
        assert "trackSessionStatus(st.name,st.activity_status)" in poll
        assert "fetch(BASE+'/api/status')" in poll

        node = shutil.which("node")
        if node is None:
            return
        # Execute the actual early-return guard: hidden warm/warning sessions
        # must refresh their status so work started elsewhere cancels an alert.
        guard = poll[poll.index("const hasBusySession="):poll.index("  try{")]
        driver = r"""
const vm=require('vm');
const cases=[
  {phase:'warm',expected:true},
  {phase:'warning',expected:true},
  {phase:'cold',expected:false},
  {phase:'unknown',expected:false},
  {phase:'cold',busy:true,expected:true},
  {phase:'cold',completion:true,expected:true},
  {phase:'cold',nudge:true,expected:true},
  {phase:'cold',hidden:false,expected:true},
];
for(const row of cases){
  const context=vm.createContext({
    document:{hidden:row.hidden!==false},
    sessions:[{phase:row.phase}],
    lastStatus:row.busy?{alpha:'busy'}:{},
    _completionWatch:row.completion?{alpha:true}:{},
    getIdleNudgeMode:()=>row.nudge?'adhd':'off',
    _idleNudgeNames:()=>row.nudge?['alpha']:[],
    _cacheTelemetryState:session=>session,
  });
  const actual=vm.runInContext('(function(){'+process.argv[1]+';return true})()',context);
  if(Boolean(actual)!==row.expected)throw new Error(JSON.stringify(row));
}
"""
        result = subprocess.run(
            [node, "-e", driver, guard], capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr

    def test_idle_nudge_has_off_light_and_adhd_modes(self, authed_client):
        html = authed_client.get("/").text

        assert '>Idle nudge</div>' in html
        assert 'aria-label="Idle nudge mode"' in html
        assert "b('off','Off')+b('light','Light')+b('adhd','ADHD')" in html
        assert "localStorage.getItem('idleNudgeMode')" in html
        assert "localStorage.setItem('idleNudgeMode',mode)" in html
        assert ".idle-nudge-seg button.in-light.active{background:#1f6feb}" in html
        assert ".idle-nudge-seg button.in-adhd.active{background:#da3633}" in html

    def test_idle_nudge_repeats_every_twenty_seconds_until_mode_condition(self, authed_client):
        html = authed_client.get("/").text
        nudge_start = html.index("const IDLE_NUDGE_INTERVAL_MS=20000")
        nudge_end = html.index("function _navDotClass", nudge_start)
        nudge = html[nudge_start:nudge_end]
        tracker_start = html.index("function trackSessionStatus(name,status,interrupted)")
        tracker_end = html.index("['pointerdown'", tracker_start)
        tracker = html[tracker_start:tracker_end]
        acknowledge_start = html.index("function _acknowledgeCompletion(name)")
        acknowledge_end = html.index("// ── Completion chime", acknowledge_start)
        acknowledge = html[acknowledge_start:acknowledge_end]

        assert "},IDLE_NUDGE_INTERVAL_MS)" in nudge
        assert "playCompletionChime()" in nudge
        assert "const pending=mode==='adhd'?_idleNudgeAdhdPending:_completedUnread" in nudge
        assert "_syncIdleNudgeTimer()" in acknowledge
        assert "if(status==='busy'&&_idleNudgeAdhdPending[name])" in tracker
        assert "delete _idleNudgeAdhdPending[name]" in tracker
        assert "if(getIdleNudgeMode()==='adhd')" in tracker
        assert "_idleNudgeAdhdPending[name]=true" in tracker

    def test_successful_new_message_stops_only_that_tabs_idle_nudge(self, authed_client):
        html = authed_client.get("/").text
        clear_start = html.index("function _clearIdleNudgeForNewWork(name)")
        clear_end = html.index("// ── Completion chime", clear_start)
        clear = html[clear_start:clear_end]
        busy_start = html.index("function setOptimisticBusy(name)")
        busy_end = html.index("function scheduleBusyVerification", busy_start)
        busy = html[busy_start:busy_end]

        assert "delete _completedUnread[name]" in clear
        assert "delete _idleNudgeAdhdPending[name]" in clear
        assert "if(wasPending)_syncIdleNudgeTimer()" in clear
        assert "_clearIdleNudgeForNewWork(name)" in busy

    def test_completed_session_dot_pulses_green_until_the_session_is_viewed(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        pulse_rule = re.search(
            r"\.nav-dot\.idle\.completed-unread\{([^}]*)\}",
            html,
        )
        tracker_start = html.index("function trackSessionStatus(name,status,interrupted)")
        tracker_end = html.index("['pointerdown'", tracker_start)
        tracker = html[tracker_start:tracker_end]
        select_start = html.index("function selectSession(name)")
        select_end = html.index("function switchTab", select_start)
        visibility_start = html.index("document.addEventListener('visibilitychange'")
        visibility_end = html.index("function _ensureRawScrollTracking", visibility_start)
        interrupt_start = html.index("async function interruptSession(name,source)")
        interrupt_end = html.index("function toggleInterruptButtons", interrupt_start)

        assert pulse_rule
        assert "background:#3fb950" in html
        assert "animation:pulse-glow 1.5s ease-in-out infinite" in pulse_rule.group(1)
        assert "_markCompletionUnread(name)" in tracker
        assert "if(completed&&!interrupted){" in tracker
        assert "_navDotClass(s.name,lastStatus[s.name]||s.activity_status)" in html
        assert "_acknowledgeCompletion(name)" in html[select_start:select_end]
        assert "_acknowledgeCompletion(selectedSession)" in html[
            visibility_start:visibility_end
        ]
        assert "_paintNavDot(name,status)" in html
        assert "trackSessionStatus(name,'idle',true)" in html[
            interrupt_start:interrupt_end
        ]

    def test_composer_action_is_a_round_record_button(self, authed_client):
        html = authed_client.get("/").text
        action_rule = re.search(r"\.btn\.composer-action\{([^}]*)\}", html)

        assert (
            action_rule
            and "width:48px" in action_rule.group(1)
            and "height:48px" in action_rule.group(1)
            and "border-radius:50%" in action_rule.group(1)
        )


class TestDynamicSessionTabLabels:
    def test_tab_uses_dynamic_label_but_keeps_stable_session_routing(self, authed_client):
        html = authed_client.get("/").text

        assert "function sessionTabLabel(session)" in html
        assert "${esc(sessionTabLabel(s))}" in html
        assert (
            "item.onclick=()=>{if(!_tabOrderSuppressClick)selectSession(s.name)}"
            in html
        )
        assert "const nextTabLabel=st.tab_label||''" in html
        assert "_paintSessionTabLabel(st.name)" in html
        assert "max-width:148px" in html
        assert "text-overflow:ellipsis" in html

    def test_tab_labels_have_a_fast_in_place_poll(self, authed_client):
        html = authed_client.get("/").text
        poll_start = html.index("async function pollTabLabels()")
        poll_end = html.index("async function pollStatus()", poll_start)
        poll = html[poll_start:poll_end]

        assert "setInterval(pollTabLabels,2000)" in html
        assert "fetch(BASE+'/api/tab-labels',{cache:'no-store'})" in poll
        assert "session.tab_label=next" in poll
        assert "_paintSessionTabLabel(row.name)" in poll
        assert "renderNav()" not in poll
        visibility = html[
            html.index("document.addEventListener('visibilitychange'"):
            html.index("function _ensureRawScrollTracking")
        ]
        assert "pollTabLabels();" in visibility

    def test_tab_label_poll_returns_current_account_sessions_only(
        self,
        authed_client,
    ):
        request_id = app_module._queue_session_tab_label(
            "test-session", "admin", "Fix OAuth login", started_at=1_000
        )
        app_module._finish_session_tab_label(
            "test-session", request_id, "OAuth Login", finished_at=1_120
        )
        with patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS):
            resp = authed_client.get("/api/tab-labels")

        assert resp.status_code == 200
        assert resp.json() == [
            {"name": "test-session", "tab_label": "OAuth Login"},
            {"name": "work-session", "tab_label": ""},
        ]

    def test_local_summary_is_always_two_words(self):
        prompts = {
            'add a toggle called "idle nudge" with three modes': "Idle Nudge",
            "Fix the OAuth login timeout bug": "OAuth Login",
            "count to 60": "Count 60",
            (
                "each tab has a fixed name; if work takes more than 2 minutes, "
                "rename that tab by summarizing my ask in 2 words"
            ): "Rename Tab",
        }

        for prompt, expected in prompts.items():
            label = app_module._fallback_two_word_tab_label(prompt)
            assert label == expected
            assert len(label.split()) == 2

    @pytest.mark.asyncio
    async def test_busy_request_renames_at_two_minutes(self, monkeypatch):
        monkeypatch.setattr(app_module, "TAB_RENAME_AFTER_SECONDS", 120)
        monkeypatch.setattr(app_module, "client", None)
        monkeypatch.setattr(
            app_module,
            "_detect_activity_raw",
            lambda _name: {"status": "busy", "detail": "Working"},
        )
        monkeypatch.setattr(
            app_module,
            "_latest_tab_label_prompt",
            lambda _name, _owner, _source_ts: "Fix the OAuth login timeout bug",
        )
        request_id = app_module._queue_session_tab_label(
            "test-session",
            "admin",
            "Fix the OAuth login timeout bug",
            started_at=1_000,
        )

        assert await app_module._session_tab_label_pass(
            now=1_119,
            live_session_names={"test-session"},
        ) == 0
        assert app_module._session_tab_label_rows()["test-session"]["pending"]["id"] == request_id

        assert await app_module._session_tab_label_pass(
            now=1_120,
            live_session_names={"test-session"},
        ) == 1
        row = app_module._session_tab_label_rows()["test-session"]
        assert row["label"] == "OAuth Login"
        assert "pending" not in row

    @pytest.mark.asyncio
    async def test_short_request_keeps_previous_label(self, monkeypatch):
        monkeypatch.setattr(app_module, "TAB_RENAME_AFTER_SECONDS", 120)
        monkeypatch.setattr(app_module, "client", None)
        monkeypatch.setattr(
            app_module,
            "_detect_activity_raw",
            lambda _name: {"status": "idle", "detail": ""},
        )
        first = app_module._queue_session_tab_label(
            "test-session", "admin", "Existing tab label", started_at=500
        )
        app_module._finish_session_tab_label(
            "test-session", first, "Existing Label", finished_at=620
        )
        app_module._queue_session_tab_label(
            "test-session", "admin", "Say hello", started_at=1_000
        )

        assert await app_module._session_tab_label_pass(
            now=1_120,
            live_session_names={"test-session"},
        ) == 0
        row = app_module._session_tab_label_rows()["test-session"]
        assert row["label"] == "Existing Label"
        assert "pending" not in row

    def test_new_requests_supersede_old_timers_and_duplicate_labels_are_distinct(self):
        old_id = app_module._queue_session_tab_label(
            "test-session", "admin", "Fix OAuth login", started_at=1_000
        )
        current_id = app_module._queue_session_tab_label(
            "test-session", "admin", "Add idle nudge", started_at=1_010
        )
        assert not app_module._finish_session_tab_label(
            "test-session", old_id, "OAuth Login"
        )
        assert (
            app_module._session_tab_label_rows()["test-session"]["pending"]["id"]
            == current_id
        )

        app_module._finish_session_tab_label(
            "test-session", current_id, "OAuth Login"
        )
        other_id = app_module._queue_session_tab_label(
            "work-session", "admin", "Fix OAuth login", started_at=1_020
        )
        other = app_module._finish_session_tab_label(
            "work-session", other_id, "OAuth Login"
        )
        assert other["label"] == "OAuth Login-2"
        assert app_module._session_tab_label("work-session") == "OAuth Login-2"
        assert len(other["label"].split()) == 2

    def test_short_approval_uses_the_previous_substantive_ask(self, monkeypatch):
        substantive = (
            "each tab has a fixed name; if work takes more than 2 minutes, "
            "rename that tab by summarizing my ask in 2 words"
        )
        monkeypatch.setattr(
            app_module,
            "_load_session_messages",
            lambda _name: [
                {"role": "user", "text": substantive, "ts": 900.0},
                {"role": "assistant", "text": "Here is the plan", "ts": 950.0},
                {"role": "user", "text": "ok", "ts": 1_000.0},
            ],
        )

        app_module._queue_session_tab_label(
            "test-session", "admin", "ok", started_at=1_000
        )
        pending = app_module._session_tab_label_rows()["test-session"]["pending"]
        assert pending["candidate"] == "Rename Tab"
        assert pending["source_ts"] == 900.0


class TestDashboardFrontendRegressionsContinued:
    def test_composer_action_stays_green_when_hovered(self, authed_client):
        html = authed_client.get("/").text
        hover_rule = re.search(r"\.btn\.composer-action:hover\{([^}]*)\}", html)

        assert hover_rule and "background:#2ea043" in hover_rule.group(1)

    def test_typed_message_state_styles_the_action_button_green(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        send_rule = re.search(r"\.composer-action\.is-send\{([^}]*)\}", html)

        assert send_rule and "background:#238636" in send_rule.group(1)

    def test_typing_updates_both_composer_buttons(self, authed_client):
        html = authed_client.get("/").text

        assert html.count('oninput="autoGrow(this);updateComposerBtn(\'') == 2

    def test_clipboard_images_become_sendable_composer_attachments(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        paste_start = html.index("function handleComposerPaste(event,name,tab)")
        paste_end = html.index("function handleDrop(event,name,tab)", paste_start)
        paste = html[paste_start:paste_end]

        assert html.count('onpaste="handleComposerPaste(event,') == 2
        assert html.count("or paste an image...") == 2
        assert "item.kind==='file'" in html
        assert ".startsWith('image/')" in html
        assert "new File([blob],filename" in html
        assert "await _uploadOneFile(name,tab,file)" in paste
        assert "previewUrl:await _clipboardImagePreview(file)" in paste
        assert "reader.readAsDataURL(file)" in html
        assert paste.index("if(!blobs.length)return") < paste.index(
            "event.preventDefault()"
        ), "ordinary text paste must retain the browser's default behavior"

        assert html.count(
            "_commandWithComposerAttachments(typed,attachments)"
        ) == 2
        assert "_clearComposerAttachments(name,'chat')" in html
        assert "_clearComposerAttachments(name,source)" in html

    def test_stop_restores_the_last_submitted_draft_for_editing(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text
        restore_start = html.index("function _restoreSubmittedDraft(name,source)")
        restore_end = html.index("function captureComposerFocus()", restore_start)
        restore = html[restore_start:restore_end]
        interrupt_start = html.index("async function interruptSession(name,source)")
        interrupt_end = html.index("function toggleInterruptButtons", interrupt_start)
        interrupt = html[interrupt_start:interrupt_end]

        assert "interruptSession('${s.name}','chat')" in html
        assert "interruptSession('${s.name}','raw')" in html
        assert html.count("_rememberSubmittedDraft(name,typed,attachments)") == 2
        assert "if(input.value.length||(_composerAttachments[key]||[]).length)" in restore
        assert "input.value=submitted.text" in restore
        assert "_composerAttachments[key]=attachments" in restore
        assert "input.setSelectionRange(input.value.length,input.value.length)" in restore
        assert "if(!resp.ok)" in interrupt
        assert "_restoreSubmittedDraft(name,target)" in interrupt

    def test_empty_chat_and_terminal_composers_render_microphone_buttons(
        self,
        authed_client,
    ):
        html = authed_client.get("/").text

        assert html.count('class="btn cmd-send composer-action is-mic"') == 2

    def test_message_composer_is_tall_enough_for_multiple_lines(self, authed_client):
        html = authed_client.get("/").text
        composer_rule = re.search(r"\.cmd-input\{[^}]*min-height:(\d+)px", html)

        assert composer_rule and int(composer_rule.group(1)) >= 80

    def test_terminal_reload_is_in_the_session_header(self, authed_client):
        html = authed_client.get("/").text
        header_start = html.index('<div class="detail-badges">')
        header_end = html.index(
            '\n      </div>\n    </div>\n\n    <div class="tab-content',
            header_start,
        )

        assert "onclick=\"loadRaw('${s.name}')\"" in html[header_start:header_end]

    def test_terminal_renderer_defines_update_filter_before_use(self, authed_client):
        html = authed_client.get("/").text
        # The renderer calls the noise filter while streaming lines, so the
        # function has to be defined above the loop or the terminal throws on
        # the first frame. The call site moved out of the old
        # `hideBash && _isUpdateNoise(line)` form; guard the real one.
        definition = html.index("function _isNoise(line){")
        usage = html.index("if(_isNoise(line)){mode='noise';continue;}")
        assert definition < usage

    def test_context_files_live_only_inside_settings(self, authed_client):
        html = authed_client.get("/").text
        assert 'id="settings-overlay"' in html
        assert "function openSettings(tab)" in html
        assert "{id:'context', label:'Context Files'}" in html
        assert 'id="claudemd-overlay"' not in html
        assert 'id="config-overlay"' not in html
        assert "getElementById('config-content')" not in html
        assert "Usage resets on a 5-hour rolling window" not in html


# ─── Session List API Tests ───


class TestTerminalRosterUiContracts:
    def test_short_lived_websocket_open_does_not_reset_backoff(self, authed_client):
        html = authed_client.get("/").text
        start = html.index("function startRawPolling(name)")
        end = html.index("function stopRawPolling(name)", start)
        block = html[start:end]
        assert "socket.onopen" not in block
        assert "['full','delta','ping'].includes(data.mode)" in block
        assert "st.backoff=500" in block

    def test_nonretryable_terminal_error_stops_reconnects(self, authed_client):
        html = authed_client.get("/").text
        start = html.index("function applyRawPayload(name,data)")
        end = html.index("async function loadRaw(name)", start)
        block = html[start:end]
        assert "data.retryable===false" in block
        assert "st.polling=false" in block
        assert "reconcileSessionRoster()" in block

    def test_authoritative_roster_adds_removes_and_checks_incarnation(self, authed_client):
        html = authed_client.get("/").text
        start = html.index("function _applySessionRoster(envelope,force,autopushVersions)")
        end = html.index("async function loadAll(", start)
        block = html[start:end]
        assert "envelope.authoritative!==true" in block
        assert "nextByName.get(oldRow.name)" in block
        assert "nextRow.incarnation" in block
        assert "nextRow.logical_incarnation" in block
        assert "const sameLogical=oldRow&&String(row.logical_incarnation" in block
        assert "if(sameLogical&&(_autopushPending.has(row.name)" in block
        assert "_discardSessionClientState(oldRow.name)" in block
        assert "_resetSessionRuntimeState(oldRow.name)" in block
        assert "if(discarded)mainEl.replaceChildren()" in block
        assert "request!==_sessionRosterRequest" in block

    def test_autopush_response_is_scoped_to_session_client_epoch(self, authed_client):
        html = authed_client.get("/").text
        start = html.index("async function setAutopush(name,mode)")
        end = html.index("function renderWatchdogLog", start)
        block = html[start:end]
        assert "const clientEpoch=_sessionClientEpoch[name]||0" in block
        assert "if((_sessionClientEpoch[name]||0)!==clientEpoch)return" in block
        assert "if((_sessionClientEpoch[name]||0)===clientEpoch)" in block

        status_start = html.index("async function loadWatchdogStatus(name)")
        status_end = html.index("// ── Key Bar", status_start)
        status_block = html[status_start:status_end]
        assert "const clientEpoch=_sessionClientEpoch[name]||0" in status_block
        assert "clientEpoch!==(_sessionClientEpoch[name]||0)" in status_block

    def test_tab_strip_has_persistent_mouse_and_touch_drag_contract(self, authed_client):
        html = authed_client.get("/").text
        assert "item.draggable=true" in html
        assert "_installTabTouchDrag(item,s.name)" in html
        assert "X-Tmux-Tab-Order':'1'" in html
        assert "&&_tabOrderDragging)return false" in html
        assert "The tab order could not be saved" in html
        render = html[html.index("function renderNav()") : html.index("/* ── Chat bubbles")]
        assert "navEl.insertBefore(item,anchor||null)" in render
        assert "items.reverse()" not in render


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

    @patch("app.async_detect_activity", new_callable=AsyncMock)
    @patch("app._tmux_inventory_snapshot")
    def test_session_roster_is_authoritative_and_owner_scoped(
        self, mock_inventory, mock_activity, authed_client
    ):
        mock_activity.return_value = {"status": "idle", "command": "", "detail": ""}
        mock_inventory.return_value = {
            "authoritative": True,
            "state": "ok",
            "observed_at": 123,
            "owners": {"test-session": "admin"},
            "sessions": [{**MOCK_SESSIONS[0], "runtime_state": "running", "incarnation": "inc-a"}],
            "expected_rows": {},
            "missing_expected": [],
        }
        resp = authed_client.get("/api/session-roster")
        assert resp.status_code == 200
        data = resp.json()
        assert data["authoritative"] is True
        assert [(row["name"], row["incarnation"]) for row in data["sessions"]] == [
            ("test-session", "inc-a")
        ]

    def test_tab_order_is_owner_scoped_and_appends_unknown_tabs(self):
        app_module._save_session_tab_order("admin", ["work-session"])
        app_module._save_session_tab_order("u_other", ["test-session"])
        inventory = {
            "owners": {"test-session": "admin", "work-session": "admin"},
            "sessions": MOCK_SESSIONS,
            "expected_rows": {},
            "missing_expected": [],
        }
        rows = app_module._roster_sessions_for_user(inventory, {"id": "admin"})
        assert [row["name"] for row in rows] == ["work-session", "test-session"]

    def test_unsaved_order_preserves_the_legacy_visual_default(self):
        inventory = {
            "owners": {"test-session": "admin", "work-session": "admin"},
            "sessions": MOCK_SESSIONS,
            "expected_rows": {},
            "missing_expected": [],
        }
        rows = app_module._roster_sessions_for_user(inventory, {"id": "admin"})
        assert [row["name"] for row in rows] == ["work-session", "test-session"]

    def test_save_tab_order_requires_confirmation_and_exact_owner(self, authed_client):
        body = {"sessions": ["work-session", "test-session"]}
        assert authed_client.post("/api/session-tab-order", json=body).status_code == 400
        response = authed_client.post(
            "/api/session-tab-order",
            json=body,
            headers={"X-Tmux-Tab-Order": "1"},
        )
        assert response.status_code == 200
        assert response.json() == {"ok": True, "sessions": body["sessions"]}
        assert app_module._session_tab_order("admin") == body["sessions"]

        duplicate = authed_client.post(
            "/api/session-tab-order",
            json={"sessions": ["test-session", "test-session"]},
            headers={"X-Tmux-Tab-Order": "1"},
        )
        assert duplicate.status_code == 422
        app_module._set_session_owner("other-session", "u_other")
        other = authed_client.post(
            "/api/session-tab-order",
            json={"sessions": ["other-session"]},
            headers={"X-Tmux-Tab-Order": "1"},
        )
        assert other.status_code == 404
        assert "other-session" not in other.text

    def test_deleted_tab_is_removed_only_from_its_owners_order(self):
        app_module._save_session_tab_order("admin", ["test-session", "work-session"])
        app_module._save_session_tab_order("u_other", ["test-session", "elsewhere"])
        app_module._remove_session_from_tab_order("test-session", "admin")
        assert app_module._session_tab_order("admin") == ["work-session"]
        assert app_module._session_tab_order("u_other") == ["test-session", "elsewhere"]

    @patch("app._tmux_inventory_snapshot")
    def test_session_roster_never_turns_inventory_failure_into_empty(self, mock_inventory, authed_client):
        mock_inventory.return_value = {
            "authoritative": False,
            "state": "error",
            "observed_at": 123,
            "sessions": [],
            "expected_rows": {},
            "missing_expected": [],
        }
        resp = authed_client.get("/api/session-roster")
        assert resp.status_code == 503
        assert resp.json() == {
            "authoritative": False,
            "state": "error",
            "observed_at": 123,
            "sessions": [],
        }

    @patch("app._load_session_notes", return_value="new notes")
    @patch("app._load_session_messages", return_value=[{"role": "user", "text": "new owner", "ts": 2}])
    @patch("app.async_detect_activity", new_callable=AsyncMock)
    @patch("app._tmux_inventory_snapshot")
    def test_roster_discards_stale_other_owner_process_cache(
        self,
        mock_inventory,
        mock_activity,
        mock_messages,
        mock_notes,
        authed_client,
    ):
        mock_activity.return_value = {"status": "idle", "command": "", "detail": ""}
        mock_inventory.return_value = {
            "authoritative": True,
            "state": "ok",
            "observed_at": 123,
            "owners": {"test-session": "admin"},
            "sessions": [{
                **MOCK_SESSIONS[0],
                "runtime_state": "running",
                "incarnation": "runtime-new",
                "logical_incarnation": "logical-new",
            }],
            "expected_rows": {},
            "missing_expected": [],
        }
        app_module.cache["test-session"] = {
            "_owner_id": "u_old",
            "_logical_incarnation": "logical-old",
            "messages": [{"role": "user", "text": "old owner secret", "ts": 1}],
            "notes": "old owner notes",
        }
        try:
            resp = authed_client.get("/api/session-roster")
            assert resp.status_code == 200
            row = resp.json()["sessions"][0]
            assert [message["text"] for message in row["messages"]] == ["new owner"]
            assert row["notes"] == "new notes"
            assert "old owner" not in json.dumps(row)
        finally:
            app_module.cache.pop("test-session", None)

    @patch("app.detect_activity", return_value={"status": "idle", "command": "", "detail": ""})
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_status_returns_activity(self, mock_sessions, mock_activity, authed_client):
        resp = authed_client.get("/api/status")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data) == 2
        assert all(s["activity_status"] == "idle" for s in data)

    def test_status_discards_metadata_if_owner_changes_during_build(
        self, authed_client
    ):
        async def swap_owner(_name):
            app_module._set_session_owner("test-session", "u_replacement")
            return {
                "status": "busy",
                "command": "replacement-secret-command",
                "detail": "replacement-secret-detail",
            }

        def binding_state(binding):
            owner = app_module._load_session_owners().get(binding["name"], "")
            return "current" if owner == binding["owner_id"] else "replaced"

        with (
            patch("app.get_tmux_sessions", return_value=[MOCK_SESSIONS[0]]),
            patch("app.async_detect_activity", side_effect=swap_owner),
            patch("app._terminal_binding_state", side_effect=binding_state),
        ):
            resp = authed_client.get("/api/status")

        assert resp.status_code == 503
        assert "replacement-secret" not in resp.text

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
        with patch("app._exact_tmux_session_id", return_value="$1"):
            resp = authed_client.post(
                "/api/sessions/create",
                json={"name": "test-session"},

            )
        assert resp.status_code == 409
        assert "already exists" in resp.json()["error"]

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
        expected_keys = {
            "cpu_load", "cpu_percent", "cpu_iowait_percent", "cpu_measurement",
            "memory", "disk", "tmux_sessions", "codex_processes",
        }
        assert expected_keys <= data.keys(), f"Missing keys: {expected_keys - data.keys()}"

    @patch("app._sample_cpu_utilization", return_value=(37.5, 8.0))
    def test_stats_uses_proc_delta_not_load_as_utilization(self, sample, authed_client):
        resp = authed_client.get("/api/stats")
        assert resp.status_code == 200
        data = resp.json()
        assert data["cpu_percent"] == 37.5
        assert data["cpu_iowait_percent"] == 8.0
        assert data["cpu_measurement"] == "proc_stat_delta"
        assert data["cpu_load"]  # load is preserved as its own metric
        sample.assert_called_once()

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

        with patch("app._session_config_base", return_value=tmp_path / ".codex"):
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
        with patch("app._session_config_base", return_value=tmp_path / ".codex"):
            result = app._find_session_jsonl_files("has-rollout-session")
        assert result == [str(rollout)]

    def test_root_thread_validator_rejects_subagent_in_same_owner_home(
        self, tmp_path
    ):
        import app

        root_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
        child_id = "01a03616-3191-7b51-a79f-af48d2475db8"
        sessions = tmp_path / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        root = sessions / f"rollout-root-{root_id}.jsonl"
        child = sessions / f"rollout-child-{child_id}.jsonl"
        root.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "id": root_id,
                "session_id": root_id,
                "thread_source": "user",
            },
        }) + "\n")
        child.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "id": child_id,
                "session_id": root_id,
                "thread_source": "subagent",
            },
        }) + "\n")

        with patch("app._session_config_base", return_value=tmp_path / ".codex"):
            assert app._validated_session_root_thread_id("debug", root_id) == root_id
            assert app._validated_session_root_thread_id("debug", child_id) is None

    def test_recorded_but_invalid_root_fails_closed(self):
        import app

        with (
            patch.object(
                app._session_lifecycle,
                "get",
                return_value={
                    "resume_uuid": "01a035f8-3188-7c21-8cca-582b01ad3002"
                },
            ),
            patch("app._validated_session_root_thread_id", return_value=None),
            patch(
                "app._strict_session_owner",
                return_value=("u_michiel", {"id": "u_michiel"}),
            ),
        ):
            with pytest.raises(ValueError, match="missing or invalid"):
                app._find_session_transcript_uuid("debug")

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
        with patch("app._session_config_base", return_value=tmp_path / ".codex"):
            result = app._find_session_jsonl_files("other-rollout-session")
        assert result == []

    @patch("app.get_session_cwd", return_value="/home/user/new-project")
    def test_recorded_resume_uuid_finds_rollout_after_project_move(
        self, mock_cwd, tmp_path
    ):
        import app

        thread_id = "01a020d4-d4e0-75a3-b832-b830e6f4fd87"
        sessions = tmp_path / ".codex" / "sessions"
        sessions.mkdir(parents=True)
        rollout = sessions / f"rollout-old-{thread_id}.jsonl"
        rollout.write_text(
            json.dumps({
                "type": "session_meta",
                "payload": {"id": thread_id, "cwd": "/home/user/old-project"},
            }) + "\n"
        )
        with (
            patch("app._session_config_base", return_value=tmp_path / ".codex"),
            patch("app._find_session_transcript_uuid", return_value=thread_id),
        ):
            result = app._find_session_jsonl_files("moved-session")

        assert result == [str(rollout)]


def test_usage_aliases_migrated_root_without_relabeling_unrelated_rollout(
    tmp_path,
):
    import app

    mapped_id = "01a020d4-d4e0-75a3-b832-b830e6f4fd87"
    unrelated_id = "01a09999-1111-7222-8333-444455556666"
    timestamp = datetime.now(timezone.utc).isoformat()

    def write_rollout(path, thread_id, input_tokens):
        path.write_text("\n".join([
            json.dumps({
                "type": "session_meta",
                "payload": {
                    "id": thread_id,
                    "session_id": thread_id,
                    "thread_source": "user",
                    "cwd": "/home/nimrod_rotem/lisa-my",
                },
            }),
            json.dumps({
                "type": "event_msg",
                "timestamp": timestamp,
                "payload": {
                    "type": "token_count",
                    "info": {
                        "last_token_usage": {
                            "input_tokens": input_tokens,
                            "output_tokens": 2,
                            "cached_input_tokens": 0,
                            "reasoning_output_tokens": 0,
                        },
                    },
                },
            }),
        ]) + "\n")

    mapped = tmp_path / f"rollout-mapped-{mapped_id}.jsonl"
    unrelated = tmp_path / f"rollout-unrelated-{unrelated_id}.jsonl"
    write_rollout(mapped, mapped_id, 10)
    write_rollout(unrelated, unrelated_id, 20)
    old_cache = dict(app._stats_usage_cache)
    app._stats_usage_cache.update({"ts": 0, "data": {}})
    try:
        with (
            patch("app._all_codex_rollouts", return_value=[mapped, unrelated]),
            patch.object(
                app._session_lifecycle,
                "snapshot",
                return_value={
                    "sessions": {
                        "logoutflow": {
                            "resume_uuid": mapped_id,
                            "cwd": "/home/nimrod_rotem/web-projects/Michiel/logoutflow",
                        },
                    },
                },
            ),
            patch(
                "app._validated_session_root_thread_id",
                return_value=mapped_id,
            ),
            patch(
                "app.get_tmux_sessions",
                return_value=[{"name": "logoutflow"}],
            ),
            patch(
                "app.get_session_cwd",
                return_value="/home/nimrod_rotem/web-projects/Michiel/logoutflow",
            ),
        ):
            response = asyncio.run(app.api_stats_usage())
    finally:
        app._stats_usage_cache.clear()
        app._stats_usage_cache.update(old_cache)

    payload = json.loads(response.body)
    rows = {row["name"]: row for row in payload["sessions"]}
    assert rows["logoutflow"]["thisWeek"]["totalTokens"] == 12
    assert rows["lisa-my"]["thisWeek"]["totalTokens"] == 22
    assert len(payload["sessions"]) == 2


def test_session_model_falls_back_to_config_when_rollout_tail_has_no_context(
    tmp_path,
):
    import app

    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text(json.dumps({
        "type": "session_meta",
        "payload": {"id": "01a035f8-3188-7c21-8cca-582b01ad3002"},
    }) + "\n")
    codex_home = tmp_path / ".codex"
    codex_home.mkdir()
    (codex_home / "config.toml").write_text('model = "gpt-5.6-sol"\n')
    app._session_model_cache.pop("restored", None)
    try:
        with (
            patch("app._find_session_jsonl_files", return_value=[str(rollout)]),
            patch("app._session_config_base", return_value=codex_home),
        ):
            assert app._get_session_model("restored") == "gpt-5.6-sol"
    finally:
        app._session_model_cache.pop("restored", None)


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
        cached_result = {
            "available": False,
            "_ts": time.time(),
            "_from_cache": True,
            "_identity": app._session_derived_cache_identity(unique_session),
        }
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
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout=(
                "session1:1:1700000000:0:::::$1\n"
                "session2:1:1700000001:0:::::$2\n"
            ),
            stderr="",
        )
        resp = authed_client.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tmux"]["query_ok"] is True
        assert data["tmux"]["server_running"] is True

    @patch("app.subprocess.run")
    def test_health_degraded_when_tmux_fails(self, mock_run, authed_client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
        resp = authed_client.get("/api/health")
        assert resp.status_code == 503
        data = resp.json()
        assert data["status"] == "degraded"
        assert data["tmux"]["query_ok"] is False

    @patch("app.subprocess.run")
    def test_health_ok_when_no_tmux_server(self, mock_run, authed_client):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no server running")
        resp = authed_client.get("/api/health")
        data = resp.json()
        assert data["status"] == "ok"
        assert data["tmux"]["query_ok"] is True
        assert data["tmux"]["server_running"] is False

    @patch("app._tmux_inventory_snapshot")
    def test_health_degraded_when_durable_session_is_missing(self, mock_inventory, authed_client):
        mock_inventory.return_value = {
            "authoritative": True,
            "state": "no_server",
            "expected": 1,
            "ready_expected": 0,
            "missing_expected": ["expected-tab"],
        }
        resp = authed_client.get("/api/health")
        assert resp.status_code == 503
        assert resp.json()["tmux"]["missing_durable"] == 1

    @patch("app.subprocess.run")
    def test_health_degraded_on_exception(self, mock_run, authed_client):
        mock_run.side_effect = Exception("timeout")
        resp = authed_client.get("/api/health")
        data = resp.json()
        assert data["status"] == "degraded"
        assert resp.status_code == 503
        assert data["tmux"]["query_ok"] is False

    @patch("app._health_report", new_callable=AsyncMock)
    def test_public_health_routes_return_json_without_login_html(
        self, mock_report, client
    ):
        mock_report.return_value = ({"status": "degraded"}, 503)
        live = client.get("/health/live")
        ready = client.get("/health/ready")
        detailed = client.get("/api/health")
        assert live.status_code == 200 and live.json() == {"status": "ok"}
        assert ready.status_code == 503 and ready.json() == {"status": "degraded"}
        assert detailed.status_code == 401
        assert detailed.json() == {"error": "Authentication required"}

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
             patch("app.get_session_cwd", return_value="/tmp"), \
             patch("app._current_user", return_value={"id": "admin", "role": "admin"}):
            resp = await _app.api_upload_file(
                MagicMock(), "test-session", FakeLargeFile()
            )

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
        app._set_session_owner(fresh_name, "admin")
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
        mock_load.assert_called_once_with(fresh_name, "admin")

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

    def test_permissions_policy_allows_same_origin_microphone(self, authed_client):
        resp = authed_client.get("/")

        assert "microphone=(self)" in resp.headers.get("Permissions-Policy", "")

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


# ─── Away Mode Status Endpoint ───


class TestAwayModeStatus:
    @patch("app.get_tmux_sessions", return_value=[])
    def test_rejects_unknown_session(self, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/no-such-session/away-mode")
        assert resp.status_code == 404
        assert resp.json() == {"error": "Session not found"}

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_returns_disabled_for_known_session_not_running(
        self, mock_sessions, authed_client, monkeypatch
    ):
        monkeypatch.setattr(app_module, "_away_mode_state", {})
        resp = authed_client.get("/api/sessions/test-session/away-mode")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        assert "phase" in resp.json()
        assert "log" in resp.json()


# ─── Go Nuts Mode Status Endpoint ───


class TestGoNutsModeStatus:
    @patch("app.get_tmux_sessions", return_value=[])
    def test_rejects_unknown_session(self, mock_sessions, authed_client):
        resp = authed_client.get("/api/sessions/no-such-session/go-nuts-mode")
        assert resp.status_code == 404
        assert resp.json() == {"error": "Session not found"}

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_status_schema_has_required_fields(self, mock_sessions, authed_client, monkeypatch):
        monkeypatch.setattr(app_module, "_go_nuts_state", {})
        resp = authed_client.get("/api/sessions/test-session/go-nuts-mode")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        for field in ("enabled", "phase", "log"):
            assert field in data, f"Missing field: {field}"


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


# ─── Away Mode Toggle (404 path) ───


class TestAwayModeToggle:
    def test_toggle_missing_session_returns_404(self, authed_client):
        """POST away-mode toggle with unknown session must return 404."""
        resp = authed_client.post(
            "/api/sessions/does-not-exist/away-mode",
            json={"enabled": True},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_disable_missing_session_returns_404(self, authed_client):
        """Disabling away-mode on unknown session must return 404."""
        resp = authed_client.post(
            "/api/sessions/does-not-exist/away-mode",
            json={"enabled": False},
        )
        assert resp.status_code == 404


# ─── Go Nuts Mode Toggle (404 path) ───


class TestGoNutsModeToggle:
    def test_toggle_missing_session_returns_404(self, authed_client):
        """POST go-nuts-mode toggle with unknown session must return 404."""
        resp = authed_client.post(
            "/api/sessions/does-not-exist/go-nuts-mode",
            json={"enabled": True},
        )
        assert resp.status_code == 404
        assert "error" in resp.json()

    def test_disable_missing_session_returns_404(self, authed_client):
        """Disabling go-nuts-mode on unknown session must return 404."""
        resp = authed_client.post(
            "/api/sessions/does-not-exist/go-nuts-mode",
            json={"enabled": False},
        )
        assert resp.status_code == 404


# ─── Create Session Tests ───


class TestCreateSession:
    @pytest.fixture(autouse=True)
    def _verified_model_catalog(self, tmp_path, monkeypatch):
        """Model settings tests must not depend on the host's saved catalog."""
        catalog = tmp_path / "verified-models.json"
        catalog.write_text(json.dumps({
            "models": app_module._SEED_MODEL_CATALOG,
            "model_efforts": app_module._SEED_MODEL_EFFORTS,
            "last_check": 1.0,
        }))
        monkeypatch.setattr(app_module, "MODELS_FILE", catalog)

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
            patch("app._set_session_owner"),
            patch("app._clear_session_owner"),
            patch("app._mark_tmux_session_managed", return_value=True),
            patch("app._publish_tmux_session", return_value=True),
            patch(
                "app._session_launch_command",
                side_effect=lambda *_args, **_kwargs: app_module.NEW_SESSION_CMD or "codex",
            ),
            patch.object(
                app_module._session_lifecycle,
                "register_active",
                return_value={
                    "desired_state": "running",
                    "generation": "a" * 32,
                    "owner_id": "admin",
                },
            ),
            patch.object(app_module._session_lifecycle, "matches", return_value=True),
            patch("app._checkpoint_active_session", return_value={}),
            patch(
                "app._controller_call",
                new=AsyncMock(return_value={"ok": True, "mode": "basic"}),
            ),
        ):
            yield

    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.subprocess.run")
    def test_create_session_with_valid_name(self, mock_run, mock_sessions, authed_client):
        """POST /api/sessions/create with a valid name should return ok=True."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="$1\tmy-session\n", stderr=""
        )
        with (
            patch("app._exact_tmux_session_id", side_effect=["", "$1", "$1"]),
            patch("app._send_session_owner_environment", return_value=True),
        ):
            resp = authed_client.post("/api/sessions/create", json={"name": "my-session"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["name"] == "my-session"
        create_call = next(
            call.args[0]
            for call in mock_run.call_args_list
            if call.args and call.args[0][0:2] == ["tmux", "new-session"]
        )
        assert [";", "set-option", app_module._TMUX_QUARANTINED_OPTION, "1"] == (
            create_call[create_call.index(";"):create_call.index(";") + 4]
        )
        token_index = create_call.index(app_module._TMUX_CREATE_TOKEN_OPTION)
        assert re.fullmatch(r"[0-9a-f]{32}", create_call[token_index + 1])

    @patch("app.get_tmux_sessions", return_value=[])
    @patch("app.subprocess.run")
    def test_create_session_auto_name(self, mock_run, mock_sessions, authed_client):
        """POST /api/sessions/create with empty name should auto-name the session."""
        mock_run.return_value = MagicMock(
            returncode=0, stdout="$2\tauto-1\n", stderr=""
        )
        with (
            patch("app._exact_tmux_session_id", return_value="$2"),
            patch("app._send_session_owner_environment", return_value=True),
        ):
            resp = authed_client.post("/api/sessions/create", json={"name": ""})
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["name"] == "auto-1"
        create_call = next(
            call.args[0]
            for call in mock_run.call_args_list
            if call.args and call.args[0][0:2] == ["tmux", "new-session"]
        )
        assert "-s" not in create_call

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
        with patch("app._exact_tmux_session_id", return_value="$3"):
            resp = authed_client.post("/api/sessions/create", json={"name": "existing"})
        assert resp.status_code == 409
        assert "error" in resp.json()
        assert resp.json()["code"] == "name_conflict"

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
        mock_run.return_value = MagicMock(
            returncode=0, stdout="$4\tkeyed-session\n", stderr=""
        )
        with (
            patch("app.get_tmux_sessions", side_effect=[[], sessions_before]),
            patch("app._exact_tmux_session_id", side_effect=["", "$4", "$4"]),
            patch("app._send_session_owner_environment", return_value=True),
        ):
            resp = authed_client.post("/api/sessions/create", json={"name": "keyed-session"})
        assert resp.status_code == 200
        calls_str = [str(c) for c in mock_run.call_args_list]
        assert not any("sk-test-not-real" in c for c in calls_str)

    @patch("app.subprocess.run")
    @patch("app.NEW_SESSION_CMD", "codex --dangerously-bypass-approvals-and-sandbox")
    def test_create_session_sends_new_session_cmd(self, mock_run, authed_client):
        """When NEW_SESSION_CMD is set, session creation should send it to the new pane."""
        sessions_before = [{"name": "cmd-session", "windows": "1", "created": "0", "attached": False}]
        mock_run.return_value = MagicMock(
            returncode=0, stdout="$5\tcmd-session\n", stderr=""
        )
        with (
            patch("app.get_tmux_sessions", side_effect=[[], sessions_before]),
            patch("app._exact_tmux_session_id", side_effect=["", "$5", "$5"]),
            patch("app._send_session_owner_environment", return_value=True),
        ):
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
    def test_model_catalog_reports_model_specific_efforts(
        self, mock_sessions, authed_client, tmp_path
    ):
        with (
            patch("app.MODELS_FILE", tmp_path / "models.json"),
            patch("app._refresh_model_catalog", new=AsyncMock(return_value=False)),
        ):
            resp = authed_client.get("/api/models")

        assert resp.status_code == 200
        data = resp.json()
        assert data["model_efforts"]["gpt-6-astra"] == [
            "low", "medium", "high", "xhigh", "max", "ultra",
        ]
        assert "none" not in data["model_efforts"]["gpt-6-astra"]

        html = authed_client.get("/").text
        assert "function catalogModelId(model)" in html
        assert "MODEL_EFFORTS[effortModel]" in html
        assert "sessions[si].effort=st.effort" in html
        assert "Model saved, but restart failed" in html
        assert "Effort saved, but restart failed" in html

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_model_change_rejects_unavailable_model(
        self, mock_sessions, authed_client
    ):
        resp = authed_client.post(
            "/api/sessions/test-session/model",
            json={"model": "gpt-99-fake", "restart": False},
        )

        assert resp.status_code == 400
        assert resp.json()["error"] == "Model is not available for this account."

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_model_change_rejects_unverified_seed_fallback(
        self, mock_sessions, authed_client, tmp_path
    ):
        with patch("app.MODELS_FILE", tmp_path / "missing-models.json"):
            resp = authed_client.post(
                "/api/sessions/test-session/model",
                json={"model": "gpt-6-astra", "restart": False},
            )

        assert resp.status_code == 503
        assert "catalog is unavailable" in resp.json()["error"]

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_astra_rejects_unsupported_none_effort(
        self, mock_sessions, authed_client, tmp_path
    ):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text(
            'model = "gpt-6-astra"\nmodel_reasoning_effort = "max"\n'
        )

        with patch(
            "app._verified_session_codex_config_path", return_value=config
        ):
            resp = authed_client.post(
                "/api/sessions/test-session/effort",
                json={"effort": "none", "restart": False},
            )

        assert resp.status_code == 400
        assert "gpt-6-astra does not support none" in resp.json()["error"]
        assert app_module.tomllib.loads(config.read_text())["model_reasoning_effort"] == "max"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_model_change_atomically_downgrades_unsupported_effort(
        self, mock_sessions, authed_client, tmp_path
    ):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text(
            'model = "gpt-6-astra"\nmodel_reasoning_effort = "max"\n'
        )

        with (
            patch("app._verified_session_codex_config_path", return_value=config),
            patch("app._async_is_codex_running", new=AsyncMock(return_value=False)),
            patch(
                "app._send_session_owner_environment_serialized",
                new=AsyncMock(return_value=True),
            ),
        ):
            resp = authed_client.post(
                "/api/sessions/test-session/model",
                json={"model": "gpt-5.5", "restart": False},
            )

        assert resp.status_code == 200
        assert resp.json()["effort"] == "xhigh"
        parsed = app_module.tomllib.loads(config.read_text())
        assert parsed["model"] == "gpt-5.5"
        assert parsed["model_reasoning_effort"] == "xhigh"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_model_change_preserves_the_accounts_agents_md(
        self, mock_sessions, authed_client, tmp_path
    ):
        """Changing the model must never disturb the account's AGENTS.md.

        The profile picker this once covered is gone — a session's Codex home now
        comes from `_session_config_base` — but the guarantee still matters: a
        model write merges into config.toml and must leave the account's
        instructions byte-identical.
        """
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        agents_path = codex_home / "AGENTS.md"
        original = b"# Global Codex instructions\n\nKeep this context byte-identical.\n"
        agents_path.write_bytes(original)

        with (
            patch("app._session_config_base", return_value=codex_home),
            patch(
                "app._verified_session_codex_config_path",
                return_value=codex_home / "config.toml",
            ),
            patch("app._async_is_codex_running", new=AsyncMock(return_value=False)),
            patch("app._send_session_owner_environment", return_value=True),
        ):
            resp = authed_client.post(
                "/api/sessions/test-session/model",
                json={"model": "gpt-5.6-sol", "restart": False},
            )

        assert resp.status_code == 200
        assert agents_path.read_bytes() == original
        # and the model really did land in that home's config
        assert 'model = "gpt-5.6-sol"' in (codex_home / "config.toml").read_text()

    def test_model_and_effort_changes_reject_a_cross_owner_session(self, authed_client):
        with (
            patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS),
            patch("app._session_owner_id", return_value="another-user"),
        ):
            model_resp = authed_client.post(
                "/api/sessions/test-session/model",
                json={"model": "gpt-5.6-sol", "restart": False},
            )
            effort_resp = authed_client.post(
                "/api/sessions/test-session/effort",
                json={"effort": "high", "restart": False},
            )

        assert model_resp.status_code == 404
        assert effort_resp.status_code == 404

    def test_model_change_rejects_symlinked_config_without_touching_target(
        self, authed_client, tmp_path
    ):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        target = tmp_path / "outside.toml"
        target.write_text('model = "do-not-change"\n')
        (codex_home / "config.toml").symlink_to(target)

        with (
            patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS),
            patch("app.CODEX_HOME", codex_home),
        ):
            resp = authed_client.post(
                "/api/sessions/test-session/model",
                json={"model": "gpt-5.6-sol", "restart": False},
            )

        assert resp.status_code == 409
        assert target.read_text() == 'model = "do-not-change"\n'
        assert (codex_home / "config.toml").is_symlink()

    def test_model_and_effort_writes_are_serialized_without_losing_fields(
        self, tmp_path
    ):
        import threading
        from concurrent.futures import ThreadPoolExecutor

        import app

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text('approval_policy = "never"\n')
        original_merge = app._merge_top_level_toml_keys
        active = 0
        max_active = 0
        counter_lock = threading.Lock()

        def slow_merge(existing, managed):
            nonlocal active, max_active
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
            try:
                time.sleep(0.05)
                return original_merge(existing, managed)
            finally:
                with counter_lock:
                    active -= 1

        with (
            patch("app._session_config_base", return_value=codex_home),
            patch("app._merge_top_level_toml_keys", side_effect=slow_merge),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = (
                pool.submit(
                    app._write_session_codex_settings,
                    "test-session",
                    {"model": "gpt-5.6-sol"},
                ),
                pool.submit(
                    app._write_session_codex_settings,
                    "test-session",
                    {"model_reasoning_effort": "high"},
                ),
            )
            for future in futures:
                future.result(timeout=2)

        parsed = app.tomllib.loads(config.read_text())
        assert parsed["model"] == "gpt-5.6-sol"
        assert parsed["model_reasoning_effort"] == "high"
        assert max_active == 1
        assert not list(codex_home.glob("*.lock"))

    def test_concurrent_model_and_effort_changes_leave_a_supported_pair(
        self, tmp_path
    ):
        from concurrent.futures import ThreadPoolExecutor

        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text(
            'model = "gpt-6-astra"\nmodel_reasoning_effort = "max"\n'
        )
        efforts = {
            "gpt-6-astra": ["low", "medium", "high", "xhigh", "max", "ultra"],
            "gpt-5.5": ["low", "medium", "high", "xhigh"],
        }

        with (
            patch("app._verified_session_codex_config_path", return_value=config),
            ThreadPoolExecutor(max_workers=2) as pool,
        ):
            futures = [
                pool.submit(
                    app_module._write_validated_session_codex_settings,
                    "test-session",
                    model_efforts=efforts,
                    requested_model="gpt-5.5",
                ),
                pool.submit(
                    app_module._write_validated_session_codex_settings,
                    "test-session",
                    model_efforts=efforts,
                    requested_effort="ultra",
                ),
            ]
            for future in futures:
                try:
                    future.result(timeout=2)
                except app_module._UnsupportedModelEffortError:
                    pass

        parsed = app_module.tomllib.loads(config.read_text())
        assert parsed["model"] == "gpt-5.5"
        assert parsed["model_reasoning_effort"] == "xhigh"

    def test_model_change_fails_closed_without_reasoning_metadata(self, tmp_path):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text(
            'model = "gpt-6-astra"\nmodel_reasoning_effort = "max"\n'
        )

        with patch("app._verified_session_codex_config_path", return_value=config):
            with pytest.raises(app_module._UnsupportedModelEffortError):
                app_module._write_validated_session_codex_settings(
                    "test-session",
                    model_efforts={},
                    requested_model="gpt-5.5",
                )

        assert app_module.tomllib.loads(config.read_text())["model"] == "gpt-6-astra"

    def test_pending_model_is_derived_from_config_without_rollout_context(
        self, tmp_path
    ):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text(
            'model = "gpt-6-astra"\nmodel_reasoning_effort = "ultra"\n'
        )

        with (
            patch("app._session_config_base", return_value=codex_home),
            patch("app._get_session_model", return_value=""),
        ):
            fields = app_module._session_model_fields("test-session")

        assert fields == {
            "model": "gpt-6-astra",
            "model_pending": "gpt-6-astra",
            "effort": "ultra",
        }

    def test_config_replace_fails_closed_across_filesystems(self, tmp_path):
        import errno

        config = tmp_path / "config.toml"
        config.write_text('model = "gpt-5.6-luna"\n')
        staging = tmp_path / "protected-staging"
        staging.mkdir(mode=0o700)

        with (
            patch("app._protected_codex_config_io_dir", return_value=staging),
            patch("app.os.replace", side_effect=OSError(errno.EXDEV, "cross-device")),
            pytest.raises(app_module._UnsafeCodexConfigError, match="different filesystem"),
        ):
            app_module._atomic_write_codex_config(
                config, 'model = "gpt-5.6-sol"\n', 0o600
            )

        assert config.read_text() == 'model = "gpt-5.6-luna"\n'
        assert list(staging.iterdir()) == []

    def test_atomic_config_replace_inherits_destination_parent_group(self, tmp_path):
        import stat

        alternate_groups = [gid for gid in os.getgroups() if gid != os.getegid()]
        if not alternate_groups:
            pytest.skip("requires a supplementary group distinct from the effective gid")

        codex_home = tmp_path / "member-codex-home"
        codex_home.mkdir()
        try:
            os.chown(codex_home, -1, alternate_groups[0])
        except PermissionError:
            pytest.skip("cannot assign a supplementary group to the test directory")
        config = codex_home / "config.toml"
        staging = tmp_path / "protected-staging"
        staging.mkdir(mode=0o700)
        assert staging.stat().st_gid != codex_home.stat().st_gid

        with patch("app._protected_codex_config_io_dir", return_value=staging):
            app_module._atomic_write_codex_config(
                config, 'model = "gpt-5.6-sol"\n', 0o660
            )

        assert config.stat().st_gid == codex_home.stat().st_gid
        assert stat.S_IMODE(config.stat().st_mode) == 0o660

    def test_symlink_swap_after_a_write_cannot_modify_its_target(
        self, authed_client, tmp_path
    ):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        config.write_text('model = "gpt-5.6-luna"\n')
        target = tmp_path / "outside.toml"
        target.write_text('model_reasoning_effort = "do-not-change"\n')

        with patch("app._session_config_base", return_value=codex_home):
            app_module._write_session_codex_settings(
                "test-session", {"model": "gpt-5.6-sol"}
            )
            config.unlink()
            config.symlink_to(target)
            with patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS), \
                 patch("app.CODEX_HOME", codex_home):
                resp = authed_client.post(
                    "/api/sessions/test-session/effort",
                    json={"effort": "high", "restart": False},
                )

        assert resp.status_code == 409
        assert target.read_text() == 'model_reasoning_effort = "do-not-change"\n'
        assert config.is_symlink()

    def test_member_config_rebuild_preserves_selected_model_and_effort(
        self, tmp_path
    ):
        member = {
            "id": "u_member",
            "username": "member",
            "role": "user",
            "group": "",
        }
        config = tmp_path / "config.toml"
        config.write_text(
            'model = "gpt-5.6-luna"\n'
            'model_reasoning_effort = "high"\n'
            'approval_policy = "on-request"\n'
        )

        with (
            patch("app.PROJECTS_ROOT", tmp_path / "projects"),
            patch("app._load_session_owners", return_value={}),
            patch("app._account_advisor_token_path", return_value=tmp_path / "missing"),
        ):
            assert app_module._configure_member_codex_isolation(tmp_path, member)

        parsed = app_module.tomllib.loads(config.read_text())
        assert parsed["model"] == "gpt-5.6-luna"
        assert parsed["model_reasoning_effort"] == "high"

    def test_owner_environment_aborts_on_symlinked_member_config(
        self, tmp_path
    ):
        member = {
            "id": "u_member",
            "username": "member",
            "role": "user",
            "group": "",
        }
        codex_home = tmp_path / ".codex-user-u_member"
        codex_home.mkdir()
        target = tmp_path / "outside.toml"
        target.write_text('model = "do-not-change"\n')
        (codex_home / "config.toml").symlink_to(target)
        run = MagicMock()

        with (
            patch("app._user_for_session", return_value=member),
            patch("app._user_codex_config_dir", return_value=codex_home),
            patch("app.subprocess.run", run),
        ):
            assert app_module._send_session_owner_environment("test-session") is False

        assert target.read_text() == 'model = "do-not-change"\n'
        run.assert_not_called()

    @pytest.mark.parametrize("unsafe_kind", ["symlink", "oversized"])
    def test_saved_model_effort_rejects_unsafe_config(
        self, tmp_path, unsafe_kind
    ):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        if unsafe_kind == "symlink":
            target = tmp_path / "outside.toml"
            target.write_text('model = "gpt-5.6-luna"\n')
            config.symlink_to(target)
        else:
            config.write_bytes(b"#" * (app_module._CODEX_CONFIG_MAX_BYTES + 1))

        with (
            patch("app._session_config_base", return_value=codex_home),
            pytest.raises(app_module._UnsafeCodexConfigError),
        ):
            app_module._saved_session_model_effort("test-session")

    def test_model_change_aborts_if_owner_changes_after_authorization(
        self, authed_client, tmp_path
    ):
        codex_home = tmp_path / ".codex"
        codex_home.mkdir()
        config = codex_home / "config.toml"
        original = 'model = "gpt-5.6-luna"\n'
        config.write_text(original)

        with (
            patch("app._find_session_for_user", return_value=(MOCK_SESSIONS, MOCK_SESSIONS[0])),
            patch("app._session_config_base", return_value=codex_home),
            patch("app._session_owner_id", side_effect=["admin", "another-user"]),
            patch("app._async_is_codex_running", new=AsyncMock()) as running,
        ):
            resp = authed_client.post(
                "/api/sessions/test-session/model",
                json={"model": "gpt-5.6-sol", "restart": True},
            )

        assert resp.status_code == 409
        assert config.read_text() == original
        running.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_restart_refuses_to_mutate_session_after_owner_changes(self):
        import app

        run = MagicMock()
        with (
            patch("app._strict_session_owner", return_value=None),
            patch("app.subprocess.run", run),
        ):
            result = await app._restart_codex_for_session(
                "test-session", expected_owner_id="admin"
            )

        assert result == (False, False)
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_pins_saved_model_and_effort_over_resumed_thread(
        self, tmp_path
    ):
        import app

        events = []

        def record_run(args, **_kwargs):
            events.append(("run", tuple(args)))
            return MagicMock(returncode=0, stdout="", stderr="")

        async def record_sleep(duration):
            events.append(("sleep", duration))

        run = MagicMock(side_effect=record_run)
        thread_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
        generation = "a" * 32
        row = {
            "managed": True,
            "generation": generation,
            "owner_id": "admin",
            "desired_state": "running",
            "restore_on_startup": True,
            "resume_uuid": thread_id,
            "cwd": str(tmp_path),
        }
        ensure = AsyncMock(return_value=True)

        with (
            patch(
                "app._strict_session_owner",
                return_value=("admin", {"id": "admin", "username": "admin"}),
            ),
            patch("app._checkpoint_active_session", return_value=row),
            patch.object(app._session_lifecycle, "get", return_value=row),
            patch.object(app._session_lifecycle, "matches", return_value=True),
            patch("app._active_session_root_thread_id", return_value=thread_id),
            patch("app._validated_session_root_thread_id", return_value=thread_id),
            patch("app._durable_session_cwd", return_value=str(tmp_path)),
            patch("app.get_session_cwd", return_value=str(tmp_path)),
            patch("app._exact_tmux_session_id", return_value="$1"),
            patch("app._tmux_session_matches_owner", return_value=True),
            patch("app._async_is_codex_running", new=AsyncMock(return_value=False)),
            patch("app._ensure_codex_running", ensure),
            patch("app.subprocess.run", run),
            patch("app.asyncio.sleep", side_effect=record_sleep),
        ):
            exported, restarted = await app._restart_codex_for_session(
                "test-session", expected_owner_id="admin"
            )

        assert (exported, restarted) == (True, True)
        ensure.assert_awaited_once_with(
            "test-session",
            resume_uuid=thread_id,
            resume_cwd=str(tmp_path),
            expected_owner_id="admin",
            expected_generation=generation,
            expected_desired_states={"running"},
            allow_fresh=False,
            operation_locked=True,
            tmux_locked=True,
            autopush_guard=False,
            expected_binding=None,
        )
        quit_index = next(
            index
            for index, event in enumerate(events)
            if event[0] == "run" and event[1][-2:] == ("-l", "/quit")
        )
        enter_index = next(
            index
            for index, event in enumerate(events[quit_index + 1 :], quit_index + 1)
            if event[0] == "run" and event[1][-1] == "Enter"
        )
        assert ("sleep", 1.0) in events[quit_index + 1 : enter_index]

    @pytest.mark.asyncio
    async def test_restart_relaunches_provably_unused_session_fresh(self, tmp_path):
        import app

        run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        generation = "a" * 32
        row = {
            "managed": True,
            "generation": generation,
            "owner_id": "admin",
            "desired_state": "running",
            "restore_on_startup": True,
            "cwd": str(tmp_path),
            "had_conversation_input": False,
        }
        ensure = AsyncMock(return_value=True)

        with (
            patch(
                "app._strict_session_owner",
                return_value=("admin", {"id": "admin", "username": "admin"}),
            ),
            patch("app._checkpoint_active_session", return_value=row),
            patch.object(app._session_lifecycle, "get", return_value=row),
            patch.object(app._session_lifecycle, "matches", return_value=True),
            patch("app._active_session_root_thread_id", return_value=None),
            patch("app._session_close_can_skip_archive", return_value=True),
            patch("app._durable_session_cwd", return_value=str(tmp_path)),
            patch("app.get_session_cwd", return_value=str(tmp_path)),
            patch("app._exact_tmux_session_id", return_value="$1"),
            patch("app._tmux_session_matches_owner", return_value=True),
            patch("app._async_is_codex_running", new=AsyncMock(return_value=False)),
            patch("app._ensure_codex_running", ensure),
            patch("app.subprocess.run", run),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
        ):
            exported, restarted = await app._restart_codex_for_session(
                "test-session", expected_owner_id="admin"
            )

        assert (exported, restarted) == (True, True)
        ensure.assert_awaited_once_with(
            "test-session",
            resume_uuid="",
            resume_cwd=str(tmp_path),
            expected_owner_id="admin",
            expected_generation=generation,
            expected_desired_states={"running"},
            allow_fresh=True,
            operation_locked=True,
            tmux_locked=True,
            autopush_guard=False,
            expected_binding=None,
        )

    @pytest.mark.asyncio
    async def test_restart_refuses_fresh_launch_when_input_may_exist(self, tmp_path):
        import app

        generation = "a" * 32
        row = {
            "managed": True,
            "generation": generation,
            "owner_id": "admin",
            "desired_state": "running",
            "restore_on_startup": True,
            "cwd": str(tmp_path),
            "had_conversation_input": True,
        }
        ensure = AsyncMock()
        run = MagicMock()

        with (
            patch(
                "app._strict_session_owner",
                return_value=("admin", {"id": "admin", "username": "admin"}),
            ),
            patch("app._checkpoint_active_session", return_value=row),
            patch.object(app._session_lifecycle, "get", return_value=row),
            patch("app._active_session_root_thread_id", return_value=None),
            patch("app._session_close_can_skip_archive", return_value=False),
            patch("app._durable_session_cwd", return_value=str(tmp_path)),
            patch("app.get_session_cwd", return_value=str(tmp_path)),
            patch("app._ensure_codex_running", ensure),
            patch("app.subprocess.run", run),
        ):
            result = await app._restart_codex_for_session(
                "test-session", expected_owner_id="admin"
            )

        assert result == (False, False)
        ensure.assert_not_awaited()
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_restart_relaunches_unused_session_when_bootstrap_root_appears(
        self, tmp_path
    ):
        import app

        active_root = "01a020d4-d4e0-75a3-b832-b830e6f4fd87"
        generation = "a" * 32
        row = {
            "managed": True,
            "generation": generation,
            "owner_id": "admin",
            "desired_state": "running",
            "restore_on_startup": True,
            "cwd": str(tmp_path),
            "had_conversation_input": False,
        }
        ensure = AsyncMock(return_value=True)
        run = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )

        with (
            patch(
                "app._strict_session_owner",
                return_value=("admin", {"id": "admin", "username": "admin"}),
            ),
            patch("app._checkpoint_active_session", return_value=row),
            patch.object(app._session_lifecycle, "get", return_value=row),
            patch.object(app._session_lifecycle, "matches", return_value=True),
            patch("app._active_session_root_thread_id", return_value=active_root),
            patch("app._session_generation_is_unused", return_value=True),
            patch("app._durable_session_cwd", return_value=str(tmp_path)),
            patch("app.get_session_cwd", return_value=str(tmp_path)),
            patch("app._exact_tmux_session_id", return_value="$1"),
            patch("app._tmux_session_matches_owner", return_value=True),
            patch("app._async_is_codex_running", new=AsyncMock(return_value=False)),
            patch("app._ensure_codex_running", ensure),
            patch("app.subprocess.run", run),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await app._restart_codex_for_session(
                "test-session", expected_owner_id="admin"
            )

        assert result == (True, True)
        ensure.assert_awaited_once_with(
            "test-session",
            resume_uuid="",
            resume_cwd=str(tmp_path),
            expected_owner_id="admin",
            expected_generation=generation,
            expected_desired_states={"running"},
            allow_fresh=True,
            operation_locked=True,
            tmux_locked=True,
            autopush_guard=False,
            expected_binding=None,
        )

    @pytest.mark.asyncio
    async def test_restart_clears_unused_recorded_bootstrap_root(self, tmp_path):
        import app

        bootstrap_root = "01a020d4-d4e0-75a3-b832-b830e6f4fd87"
        generation = "a" * 32
        row = {
            "managed": True,
            "generation": generation,
            "owner_id": "admin",
            "desired_state": "running",
            "restore_on_startup": True,
            "resume_uuid": bootstrap_root,
            "cwd": str(tmp_path),
            "had_conversation_input": False,
        }
        cleared_row = {key: value for key, value in row.items() if key != "resume_uuid"}
        ensure = AsyncMock(return_value=True)
        clear_root = MagicMock(return_value=cleared_row)

        with (
            patch(
                "app._strict_session_owner",
                return_value=("admin", {"id": "admin", "username": "admin"}),
            ),
            patch("app._checkpoint_active_session", return_value=row),
            patch.object(app._session_lifecycle, "get", return_value=row),
            patch.object(app._session_lifecycle, "matches", return_value=True),
            patch.object(app._session_lifecycle, "clear_resume_uuid", clear_root),
            patch("app._active_session_root_thread_id", return_value=None),
            patch(
                "app._validated_session_root_thread_id",
                return_value=bootstrap_root,
            ),
            patch("app._session_generation_is_unused", return_value=True),
            patch("app._durable_session_cwd", return_value=str(tmp_path)),
            patch("app.get_session_cwd", return_value=str(tmp_path)),
            patch("app._exact_tmux_session_id", return_value="$1"),
            patch("app._tmux_session_matches_owner", return_value=True),
            patch("app._async_is_codex_running", new=AsyncMock(return_value=False)),
            patch("app._ensure_codex_running", ensure),
            patch(
                "app.subprocess.run",
                return_value=MagicMock(returncode=0, stdout="", stderr=""),
            ),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
        ):
            result = await app._restart_codex_for_session(
                "test-session", expected_owner_id="admin"
            )

        assert result == (True, True)
        clear_root.assert_called_once_with(
            "test-session",
            owner_id="admin",
            expected_generation=generation,
        )
        ensure.assert_awaited_once_with(
            "test-session",
            resume_uuid="",
            resume_cwd=str(tmp_path),
            expected_owner_id="admin",
            expected_generation=generation,
            expected_desired_states={"running"},
            allow_fresh=True,
            operation_locked=True,
            tmux_locked=True,
            autopush_guard=False,
            expected_binding=None,
        )

    @pytest.mark.asyncio
    async def test_restart_refuses_stale_recorded_root(self, tmp_path):
        import app

        recorded_root = "01a035f8-3188-7c21-8cca-582b01ad3002"
        active_root = "01a020d4-d4e0-75a3-b832-b830e6f4fd87"
        generation = "a" * 32
        row = {
            "managed": True,
            "generation": generation,
            "owner_id": "admin",
            "desired_state": "running",
            "restore_on_startup": True,
            "resume_uuid": recorded_root,
            "cwd": str(tmp_path),
            "had_conversation_input": True,
        }
        ensure = AsyncMock()
        run = MagicMock()

        with (
            patch(
                "app._strict_session_owner",
                return_value=("admin", {"id": "admin", "username": "admin"}),
            ),
            patch("app._checkpoint_active_session", return_value=row),
            patch.object(app._session_lifecycle, "get", return_value=row),
            patch("app._active_session_root_thread_id", return_value=active_root),
            patch(
                "app._validated_session_root_thread_id",
                return_value=recorded_root,
            ),
            patch("app._durable_session_cwd", return_value=str(tmp_path)),
            patch("app.get_session_cwd", return_value=str(tmp_path)),
            patch("app._ensure_codex_running", ensure),
            patch("app.subprocess.run", run),
        ):
            result = await app._restart_codex_for_session(
                "test-session", expected_owner_id="admin"
            )

        assert result == (False, False)
        ensure.assert_not_awaited()
        run.assert_not_called()


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
        """Interactive deletion starts a non-blocking knowledge-preserving job."""
        with patch(
            "app._controller_call",
            new=AsyncMock(return_value={
                "ok": True,
                "accepted": True,
                "job": {"id": "close_abcdefghijklmnop", "status": "queued"},
                "_status": 202,
            }),
        ):
            resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 202
        data = resp.json()
        assert data["ok"] is True
        assert data["job"]["status"] == "queued"

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_delete_session_kill_failure_returns_500(self, mock_run, mock_sessions, authed_client):
        """If tmux kill-session fails, return 500."""
        with patch(
            "app._controller_call",
            new=AsyncMock(return_value={"error": "can't kill session", "_status": 500}),
        ):
            resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 500
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_delete_session_with_pane_pids(self, mock_run, mock_sessions, authed_client):
        """The HTTP worker delegates process cleanup to the controller."""
        call = AsyncMock(return_value={
            "ok": True,
            "job": {"id": "close_abcdefghijklmnop", "status": "queued"},
            "_status": 202,
        })
        with patch("app._controller_call", new=call):
            resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 202
        assert resp.json()["ok"] is True
        call.assert_awaited_once_with(
            "session_close_start", session="test-session", owner_id="admin"
        )
        mock_run.assert_not_called()

    @pytest.mark.asyncio
    async def test_close_pauses_active_go_nuts_task(self, monkeypatch):
        """Accepted close stops background prompts before transcript capture."""
        gate = asyncio.Event()

        async def worker():
            await gate.wait()

        task = asyncio.create_task(worker())
        app_module._go_nuts_state["test-session"] = {
            "enabled": True,
            "task": task,
        }
        save = MagicMock()
        monkeypatch.setattr(app_module, "_save_autonomous_state", save)
        try:
            await app_module._pause_autonomous_work_for_close("test-session")
            assert task.cancelled()
            assert app_module._go_nuts_state["test-session"]["enabled"] is False
            save.assert_called_once()
        finally:
            app_module._go_nuts_state.pop("test-session", None)

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run", side_effect=Exception("tmux daemon gone"))
    def test_delete_session_outer_exception_returns_500(self, mock_run, mock_sessions, authed_client):
        """An unexpected outer exception should return 500."""
        with patch(
            "app._controller_call",
            new=AsyncMock(return_value={"error": "tmux daemon gone", "_status": 500}),
        ):
            resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 500
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_delete_session_pkill_exception_still_succeeds(self, mock_run, mock_sessions, authed_client):
        """Controller-reported successful cleanup is returned unchanged."""
        with patch(
            "app._controller_call",
            new=AsyncMock(return_value={
                "ok": True,
                "job": {"id": "close_abcdefghijklmnop", "status": "queued"},
                "_status": 202,
            }),
        ):
            resp = authed_client.delete("/api/sessions/test-session")
        assert resp.status_code == 202
        assert resp.json()["ok"] is True

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_owner_polls_close_job(self, mock_sessions, authed_client):
        call = AsyncMock(return_value={
            "ok": True,
            "job": {
                "id": "close_abcdefghijklmnop",
                "session": "test-session",
                "status": "completed",
                "phase": "complete",
                "spec_file": "TECHNICAL_SPEC.md",
            },
            "_status": 200,
        })
        with patch("app._controller_call", new=call):
            resp = authed_client.get(
                "/api/sessions/test-session/close/close_abcdefghijklmnop"
            )
        assert resp.status_code == 200
        assert resp.json()["job"]["spec_file"] == "TECHNICAL_SPEC.md"
        call.assert_awaited_once_with(
            "session_close_status",
            session="test-session",
            owner_id="admin",
            job_id="close_abcdefghijklmnop",
        )

    def test_member_can_poll_completed_close_after_owner_cleanup(self, authed_client):
        member = {"id": "u_member", "username": "member", "role": "user"}
        call = AsyncMock(return_value={
            "ok": True,
            "job": {
                "id": "close_abcdefghijklmnop",
                "session": "gone-session",
                "status": "completed",
                "tab_state": "closed",
            },
            "_status": 200,
        })
        with (
            patch("app._current_user", return_value=member),
            patch("app._user_can_access_session", return_value=False),
            patch("app._controller_call", new=call),
        ):
            resp = authed_client.get(
                "/api/sessions/gone-session/close/close_abcdefghijklmnop"
            )
        assert resp.status_code == 200
        call.assert_awaited_once_with(
            "session_close_status",
            session="gone-session",
            owner_id="u_member",
            job_id="close_abcdefghijklmnop",
        )


class TestKnowledgePreservingClose:
    def _binding(self, project_root: Path):
        return {
            "owner": {"id": "admin", "username": "admin", "role": "admin"},
            "owner_id": "admin",
            "session_name": "test-session",
            "generation": "a" * 32,
            "resume_uuid": "01a035f8-3188-7c21-8cca-582b01ad3002",
            "project_root": project_root,
            "tmux_state": "present",
            "session_id": "$1",
            "tab_label": "Close Knowledge",
        }

    def _patch_close_binding_state(
        self,
        monkeypatch,
        project_root: Path,
        *,
        recorded_root: str = "",
        validated_recorded_root: str = "",
        active_root: str | None = None,
        last_source: str = "",
        had_conversation_input: bool | None = None,
    ):
        owner = self._binding(project_root)["owner"]
        lifecycle = {
            "managed": True,
            "owner_id": "admin",
            "desired_state": "running",
            "generation": "a" * 32,
            "resume_uuid": recorded_root,
            "last_source": last_source,
        }
        if had_conversation_input is not None:
            lifecycle["had_conversation_input"] = had_conversation_input
        monkeypatch.setattr(
            app_module,
            "_strict_session_owner",
            lambda *_args: ("admin", owner),
        )
        monkeypatch.setattr(
            app_module._session_lifecycle,
            "get",
            lambda _name: lifecycle,
        )
        monkeypatch.setattr(
            app_module,
            "_durable_session_cwd",
            lambda *_args: str(project_root),
        )
        monkeypatch.setattr(
            app_module,
            "_validated_session_root_thread_id",
            lambda _name, value, _owner="": (
                validated_recorded_root
                if value == validated_recorded_root and validated_recorded_root
                else None
            ),
        )
        active = MagicMock(return_value=active_root)
        monkeypatch.setattr(app_module, "_active_session_root_thread_id", active)
        monkeypatch.setattr(
            app_module,
            "_exact_tmux_session_state",
            lambda _name: ("present", "$1"),
        )
        monkeypatch.setattr(
            app_module,
            "_tmux_session_matches_owner",
            lambda *_args: True,
        )
        monkeypatch.setattr(
            app_module,
            "get_session_cwd",
            lambda _name: str(project_root),
        )
        return active

    def test_close_archive_precedes_idempotent_spec_write(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        project = tmp_path / "project"
        project.mkdir()
        (project / "TECHNICAL_SPEC.md").write_text(
            "# Existing spec\n\nHuman-maintained architecture.\n"
        )
        monkeypatch.setattr(app_module, "MESSAGES_DIR", state_root)
        binding = self._binding(project)
        context = {
            "messages": [{"role": "assistant", "text": "Implemented close."}],
            "source_hash": "b" * 64,
            "source_bytes": 123,
            "root_thread_id": binding["resume_uuid"],
        }
        original_write = app_module._atomic_write_spec_at
        observed = []

        def verify_archive_first(parent_fd, name, content, mode=0o644):
            archive = state_root / "session-closures" / "test-session" / (
                "a" * 32 + "-" + "b" * 16 + ".json"
            )
            assert archive.is_file()
            observed.append(name)
            return original_write(parent_fd, name, content, mode)

        monkeypatch.setattr(app_module, "_atomic_write_spec_at", verify_archive_first)
        app_module._persist_session_close_knowledge(
            binding, context, "**Outcome**\n\nImplemented safe close."
        )
        app_module._persist_session_close_knowledge(
            binding, context, "**Outcome**\n\nImplemented safe close."
        )

        spec = (project / "TECHNICAL_SPEC.md").read_text()
        assert "Human-maintained architecture." in spec
        assert spec.count("tmux-dashboard-session-close:") == 2
        assert len(observed) == 2

    def test_lisa_workspace_writes_canonical_repo_spec(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        project = tmp_path / "project"
        canonical = project / "repo" / "lisa-app" / "docs" / "TECHNICAL_SPEC.md"
        canonical.parent.mkdir(parents=True)
        canonical.write_text("# Canonical Lisa spec\n")
        (project / "TECHNICAL_SPEC.md").write_text("# Workspace pointer\n")
        monkeypatch.setattr(app_module, "MESSAGES_DIR", state_root)
        binding = self._binding(project)

        result = app_module._persist_session_close_knowledge(
            binding,
            {
                "messages": [{"role": "assistant", "text": "Implemented safe close."}],
                "source_hash": "c" * 64,
                "source_bytes": 100,
                "root_thread_id": binding["resume_uuid"],
            },
            "**Outcome**\n\nImplemented safe close.",
        )

        assert Path(result["spec_path"]) == canonical
        assert "Implemented safe close." in canonical.read_text()
        assert (project / "TECHNICAL_SPEC.md").read_text() == "# Workspace pointer\n"

    def test_canonical_lisa_spec_symlink_parent_fails_closed(self, tmp_path):
        project = tmp_path / "project"
        victim = tmp_path / "victim"
        (project / "repo" / "lisa-app").mkdir(parents=True)
        victim.mkdir()
        (project / "repo" / "lisa-app" / "docs").symlink_to(
            victim, target_is_directory=True
        )
        with pytest.raises(app_module._SessionCloseError):
            app_module._technical_spec_path(project)
        assert not list(victim.iterdir())

    def test_existing_archive_tracks_canonical_spec_migration(self, tmp_path, monkeypatch):
        state_root = tmp_path / "state"
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setattr(app_module, "MESSAGES_DIR", state_root)
        binding = self._binding(project)
        context = {
            "messages": [{"role": "assistant", "text": "Implemented safe close."}],
            "source_hash": "d" * 64,
            "source_bytes": 100,
            "root_thread_id": binding["resume_uuid"],
        }
        first = app_module._persist_session_close_knowledge(
            binding, context, "**Outcome**\n\nOld handoff."
        )
        canonical = project / "repo" / "lisa-app" / "docs" / "TECHNICAL_SPEC.md"
        canonical.parent.mkdir(parents=True)
        second = app_module._persist_session_close_knowledge(
            binding, context, "**Outcome**\n\nImproved technical handoff."
        )

        archived = json.loads(Path(second["archive_path"]).read_text())
        assert Path(second["spec_path"]) == canonical
        assert archived["technical_spec"] == str(canonical)
        assert archived["summary"] == "**Outcome**\n\nImproved technical handoff."
        assert archived["summary_history"][-1]["technical_spec"] == first["spec_path"]
        assert "Improved technical handoff." in canonical.read_text()

    def test_close_summary_redacts_secrets_and_private_paths(self, tmp_path):
        text = (
            "API_KEY=sk-abcdefghijklmnopqrstuvwxyz\n"
            "password: swordfish\n"
            "Authorization: Bearer abcdefghijklmnopqrstuvwxyz\n"
            "github_pat_abcdefghijklmnopqrstuvwxyz123456\n"
            "AKIAABCDEFGHIJKLMNOP\n"
            "postgres://alice:hunter2@db.example/app\n"
            "DATABASE_PASSWORD=swordfish2\n"
            "TWILIO_AUTH_TOKEN=0123456789abcdef0123456789abcdef\n"
            "Contact monica@example.com or +1 415-555-1212\n"
            "/root/.ssh/id_ed25519\n"
            f"Changed {tmp_path}/private/module.py"
        )
        clean = app_module._sanitize_session_spec_text(text, tmp_path)
        assert "sk-abcdefghijklmnopqrstuvwxyz" not in clean
        assert "swordfish" not in clean
        assert "github_pat_" not in clean
        assert "AKIAABCDEFGHIJKLMNOP" not in clean
        assert "hunter2" not in clean
        assert "swordfish2" not in clean
        assert "0123456789abcdef0123456789abcdef" not in clean
        assert "monica@example.com" not in clean
        assert "415-555-1212" not in clean
        assert "/root/" not in clean
        assert "private/module.py" in clean
        assert "[REDACTED]" in clean

    def test_close_summary_removes_owner_name_and_optional_surname(self, tmp_path):
        clean = app_module._sanitize_session_spec_text(
            (
                "Michiel Rauws requested a change. Michiel's account was checked. "
                "Branch `Michiel/task-hold-ui` was validated."
            ),
            tmp_path,
            private_terms=["Michiel"],
        )
        assert "Michiel" not in clean
        assert "Rauws" not in clean
        assert clean.count("the user") == 2
        assert "`task-hold-ui`" in clean

    def test_close_summary_drops_residual_case_narrative(self):
        cleaned = app_module._remove_session_case_narrative(
            "\n".join((
                "## Outcome",
                "- User account recovery was successfully completed.",
                "- Task #691 was confirmed delivered.",
                "- The maintenance request was paused.",
                "- The booking authorization gate requires verified approval.",
                "## Open Work",
                "- Implement this in a dashboard-authorized session.",
            ))
        )
        assert "account recovery" not in cleaned
        assert "Task #691" not in cleaned
        assert "maintenance request" not in cleaned
        assert "dashboard-authorized" not in cleaned
        assert "booking authorization gate" in cleaned

    @pytest.mark.asyncio
    async def test_final_handoff_edit_excludes_personal_case_narrative(
        self, tmp_path, monkeypatch
    ):
        calls = []

        async def llm(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                return (
                    "## Outcome\nMichiel Rauws cancelled an appointment with a vendor. "
                    "The authorization gate was implemented."
                )
            return "## Outcome\nThe authorization gate was implemented and validated."

        monkeypatch.setattr(app_module, "client", object())
        monkeypatch.setattr(app_module, "llm_call", llm)
        result = await app_module._summarize_session_close(
            {
                "messages": [{
                    "role": "assistant",
                    "text": "Michiel Rauws cancelled an appointment after implementing the gate.",
                }],
                "private_terms": ["Michiel"],
            },
            tmp_path,
        )

        assert len(calls) == 3
        assert "personal names" in calls[-2]["system_prompt"]
        assert "appointments" in calls[-2]["system_prompt"]
        assert "final privacy" in calls[-1]["system_prompt"]
        assert "Michiel" not in result
        assert "appointment" not in result
        assert "authorization gate" in result

    @pytest.mark.asyncio
    async def test_no_llm_fails_closed_without_deleting_knowledge(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module, "client", None)
        llm = AsyncMock()
        monkeypatch.setattr(app_module, "llm_call", llm)
        context = {
            "messages": [
                {"role": "user", "text": "Build safe close"},
                {"role": "assistant", "text": "Implemented the archive and spec update."},
            ]
        }
        with pytest.raises(app_module._SessionCloseError):
            await app_module._summarize_session_close(context, tmp_path)
        llm.assert_not_awaited()

    def test_spec_symlink_is_rejected(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        victim = tmp_path / "victim.md"
        victim.write_text("do not touch")
        (project / "TECHNICAL_SPEC.md").symlink_to(victim)
        with pytest.raises(app_module._SessionCloseError):
            app_module._technical_spec_path(project)
        assert victim.read_text() == "do not touch"

    def test_spec_hardlink_is_rejected(self, tmp_path):
        project = tmp_path / "project"
        project.mkdir()
        victim = tmp_path / "victim.md"
        victim.write_text("do not disclose")
        os.link(victim, project / "TECHNICAL_SPEC.md")
        with pytest.raises(app_module._SessionCloseError):
            app_module._technical_spec_path(project)
        assert victim.read_text() == "do not disclose"

    def test_archive_generation_path_traversal_is_rejected(self, tmp_path):
        binding = {**self._binding(tmp_path), "generation": "../../escape"}
        with pytest.raises(app_module._SessionCloseError):
            app_module._persist_session_close_knowledge(
                binding,
                {"source_hash": "a" * 64, "messages": []},
                "**Outcome**\n\nSafe summary",
            )
        assert not (tmp_path.parent / "escape.json").exists()

    @pytest.mark.asyncio
    async def test_summary_includes_early_messages_not_only_tail(self, tmp_path, monkeypatch):
        calls = []

        async def llm(**kwargs):
            calls.append(kwargs["user_content"])
            return "**Outcome**\n\nComplete handoff from all supplied evidence."

        monkeypatch.setattr(app_module, "client", object())
        monkeypatch.setattr(app_module, "llm_call", llm)
        context = {
            "messages": [
                {
                    "role": "assistant",
                    "text": (
                        "message-0 DATABASE_PASSWORD=never-send-this"
                        if index == 0 else f"message-{index}"
                    ),
                }
                for index in range(30)
            ]
        }
        await app_module._summarize_session_close(context, tmp_path)
        assert any("message-0" in call for call in calls)
        assert any("message-29" in call for call in calls)
        assert all("never-send-this" not in call for call in calls)

    @pytest.mark.asyncio
    async def test_llm_input_sanitizing_does_not_truncate_chunk(self, tmp_path, monkeypatch):
        calls = []

        async def llm(**kwargs):
            calls.append(kwargs["user_content"])
            return "**Outcome**\n\nComplete handoff from every supplied conversation part."

        monkeypatch.setattr(app_module, "client", object())
        monkeypatch.setattr(app_module, "llm_call", llm)
        text = (
            "START-KEEP "
            + "a" * 9_000
            + " MIDDLE-KEEP "
            + "b" * 11_000
            + " END-KEEP"
        )
        await app_module._summarize_session_close(
            {"messages": [{"role": "assistant", "text": text}]},
            tmp_path,
        )
        combined = "\n".join(calls)
        assert "START-KEEP" in combined
        assert "MIDDLE-KEEP" in combined
        assert "END-KEEP" in combined

    @pytest.mark.asyncio
    async def test_empty_final_reduce_fails_closed(self, tmp_path, monkeypatch):
        calls = 0

        async def llm(**_kwargs):
            nonlocal calls
            calls += 1
            return "**Outcome**\n\nPartial handoff." if calls <= 2 else ""

        monkeypatch.setattr(app_module, "client", object())
        monkeypatch.setattr(app_module, "llm_call", llm)
        context = {
            "messages": [{"role": "assistant", "text": "x" * 50_000}]
        }
        with pytest.raises(app_module._SessionCloseError):
            await app_module._summarize_session_close(context, tmp_path)

    @pytest.mark.asyncio
    async def test_oversized_summary_fails_closed_before_llm(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app_module, "client", object())
        llm = AsyncMock()
        monkeypatch.setattr(app_module, "llm_call", llm)
        context = {
            "messages": [{
                "role": "assistant",
                "text": "x" * (app_module._SESSION_CLOSE_MAX_SUMMARY_CHARS + 1),
            }]
        }
        with pytest.raises(app_module._SessionCloseError):
            await app_module._summarize_session_close(context, tmp_path)
        llm.assert_not_awaited()

    def test_archive_symlink_parent_is_rejected(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        victim = tmp_path / "victim"
        state.mkdir()
        victim.mkdir()
        (state / "session-closures").symlink_to(victim, target_is_directory=True)
        monkeypatch.setattr(app_module, "MESSAGES_DIR", state)
        with pytest.raises(app_module._SessionCloseError):
            app_module._persist_session_close_knowledge(
                self._binding(tmp_path),
                {"source_hash": "a" * 64, "messages": []},
                "**Outcome**\n\nSafe summary",
            )
        assert not list(victim.iterdir())

    def test_oversized_existing_spec_is_rejected_before_read(self, tmp_path, monkeypatch):
        state = tmp_path / "state"
        project = tmp_path / "project"
        state.mkdir()
        project.mkdir()
        spec = project / "TECHNICAL_SPEC.md"
        with spec.open("wb") as stream:
            stream.seek(app_module._SESSION_CLOSE_MAX_SPEC_BYTES)
            stream.write(b"x")
        monkeypatch.setattr(app_module, "MESSAGES_DIR", state)
        with pytest.raises(app_module._SessionCloseError):
            app_module._persist_session_close_knowledge(
                self._binding(project),
                {"source_hash": "a" * 64, "messages": []},
                "**Outcome**\n\nSafe summary",
            )

    def test_held_rollout_fd_must_identify_exact_root(self, tmp_path, monkeypatch):
        binding = self._binding(tmp_path)
        sessions = tmp_path / "codex" / "sessions" / "2026" / "08" / "27"
        sessions.mkdir(parents=True)
        rollout = sessions / f"rollout-test-{binding['resume_uuid']}.jsonl"
        rollout.write_text(json.dumps({
            "type": "session_meta",
            "payload": {
                "session_id": "wrong-root",
                "id": "wrong-root",
                "thread_source": "user",
                "cwd": str(tmp_path),
            },
        }) + "\n")
        monkeypatch.setattr(
            app_module,
            "_user_codex_config_dir",
            lambda _owner: tmp_path / "codex",
        )
        monkeypatch.setattr(
            app_module,
            "_session_close_rollout_path",
            lambda *_args: rollout,
        )
        with pytest.raises(app_module._SessionCloseError):
            app_module._capture_session_close_context(binding)

    def test_capture_reads_current_response_item_message_format(self, tmp_path, monkeypatch):
        binding = self._binding(tmp_path)
        original_cwd = tmp_path / "original-cwd"
        original_cwd.mkdir()
        sessions = tmp_path / "codex" / "sessions" / "2026" / "08" / "27"
        sessions.mkdir(parents=True)
        rollout = sessions / f"rollout-test-{binding['resume_uuid']}.jsonl"
        events = [
            {
                "type": "session_meta",
                "payload": {
                    "session_id": binding["resume_uuid"],
                    "id": binding["resume_uuid"],
                    "thread_source": "user",
                    # Codex keeps this immutable origin when an exact root is
                    # later resumed with `-C` into the tab's current project.
                    "cwd": str(original_cwd),
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": (
                            "# AGENTS.md instructions for /project\n\n<INSTRUCTIONS>\n"
                            "managed context\n</INSTRUCTIONS>\n<environment_context>"
                            "managed environment\n</environment_context>"
                        ),
                    }],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Build the close flow."}],
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "Implemented and tested it."}],
                },
            },
        ]
        rollout.write_text("".join(json.dumps(event) + "\n" for event in events))
        monkeypatch.setattr(
            app_module,
            "_user_codex_config_dir",
            lambda _owner: tmp_path / "codex",
        )
        monkeypatch.setattr(
            app_module,
            "_session_close_rollout_path",
            lambda *_args: rollout,
        )

        context = app_module._capture_session_close_context(binding)

        assert context["messages"] == [
            {"role": "user", "text": "Build the close flow."},
            {"role": "assistant", "text": "Implemented and tested it."},
        ]

    def test_capture_verified_bootstrap_only_rollout_returns_empty_messages(
        self, tmp_path, monkeypatch
    ):
        binding = self._binding(tmp_path)
        sessions = tmp_path / "codex" / "sessions" / "2026" / "08" / "29"
        sessions.mkdir(parents=True)
        rollout = sessions / f"rollout-test-{binding['resume_uuid']}.jsonl"
        events = [
            {
                "type": "session_meta",
                "payload": {
                    "session_id": binding["resume_uuid"],
                    "id": binding["resume_uuid"],
                    "thread_source": "user",
                    "cwd": str(tmp_path),
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "task_started", "turn_id": "bootstrap-turn"},
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "message",
                    "role": "user",
                    "content": [{
                        "type": "input_text",
                        "text": (
                            "# AGENTS.md instructions for /project\n\n<INSTRUCTIONS>\n"
                            "managed context\n</INSTRUCTIONS>\n<environment_context>"
                            "managed environment\n</environment_context>"
                        ),
                    }],
                },
            },
        ]
        rollout.write_text("".join(json.dumps(event) + "\n" for event in events))
        monkeypatch.setattr(
            app_module,
            "_user_codex_config_dir",
            lambda _owner: tmp_path / "codex",
        )
        monkeypatch.setattr(
            app_module,
            "_session_close_rollout_path",
            lambda *_args: rollout,
        )

        context = app_module._capture_session_close_context(binding)

        assert context["messages"] == []
        assert context["parse_errors"] == 0
        assert context["source_bytes"] == rollout.stat().st_size
        assert context["root_thread_id"] == binding["resume_uuid"]

    def test_missing_lifecycle_root_adopts_validated_live_root(
        self, tmp_path, monkeypatch
    ):
        live_root = "01a00000-0000-7000-8000-000000000001"
        active = self._patch_close_binding_state(
            monkeypatch,
            tmp_path,
            recorded_root="",
            active_root=live_root,
        )

        binding = app_module._session_close_binding("test-session", "admin")

        assert binding["resume_uuid"] == live_root
        assert active.call_count == 2

    def test_nonempty_unverifiable_recorded_root_fails_closed(
        self, tmp_path, monkeypatch
    ):
        self._patch_close_binding_state(
            monkeypatch,
            tmp_path,
            recorded_root="01a00000-0000-7000-8000-000000000002",
            validated_recorded_root="",
            active_root=None,
        )

        with pytest.raises(
            app_module._SessionCloseError,
            match="exact Codex conversation could not be verified",
        ):
            app_module._session_close_binding("test-session", "admin")

    def test_unverifiable_recorded_root_adopts_exact_live_root(
        self, tmp_path, monkeypatch
    ):
        live_root = "01a00000-0000-7000-8000-000000000003"
        self._patch_close_binding_state(
            monkeypatch,
            tmp_path,
            recorded_root="01a00000-0000-7000-8000-000000000002",
            validated_recorded_root="",
            active_root=live_root,
        )

        binding = app_module._session_close_binding("test-session", "admin")

        assert binding["resume_uuid"] == live_root

    def test_missing_root_with_recorded_user_history_fails_closed(
        self, tmp_path, monkeypatch
    ):
        self._patch_close_binding_state(
            monkeypatch,
            tmp_path,
            recorded_root="",
            active_root=None,
            last_source="terminal-stream",
        )
        monkeypatch.setattr(
            app_module,
            "_load_session_messages",
            lambda *_args: [{"role": "user", "text": "Please keep this work."}],
        )

        with pytest.raises(
            app_module._SessionCloseError,
            match="exact Codex conversation could not be verified",
        ):
            app_module._session_close_binding("test-session", "admin")

    def test_positively_empty_passive_tab_can_bind_without_root(
        self, tmp_path, monkeypatch
    ):
        active = self._patch_close_binding_state(
            monkeypatch,
            tmp_path,
            recorded_root="",
            active_root=None,
            last_source="terminal-stream",
            had_conversation_input=False,
        )
        monkeypatch.setattr(app_module, "_load_session_messages", lambda *_args: [])
        monkeypatch.setattr(app_module, "_iter_prompt_audit_reverse", lambda: [])
        monkeypatch.setattr(app_module, "_session_tab_label_rows", lambda: {})
        monkeypatch.setattr(app_module, "_away_mode_state", {})
        monkeypatch.setattr(app_module, "_go_nuts_state", {})

        binding = app_module._session_close_binding("test-session", "admin")

        assert binding["resume_uuid"] == ""
        assert active.call_count == 2

    def test_new_empty_generation_ignores_stale_name_scoped_input_evidence(
        self, tmp_path, monkeypatch
    ):
        active = self._patch_close_binding_state(
            monkeypatch,
            tmp_path,
            recorded_root="",
            active_root=None,
            last_source="send-command",
            had_conversation_input=False,
        )
        old_messages = MagicMock(
            return_value=[{"role": "user", "text": "Old generation work"}]
        )
        old_audit = MagicMock(return_value=[{
            "session_name": "test-session",
            "user_id": "admin",
            "prompt": "Old generation prompt",
        }])
        monkeypatch.setattr(app_module, "_load_session_messages", old_messages)
        monkeypatch.setattr(app_module, "_iter_prompt_audit_reverse", old_audit)
        monkeypatch.setattr(
            app_module,
            "_session_tab_label_rows",
            lambda: {
                "test-session": {
                    "owner_id": "admin",
                    "generation": "b" * 32,
                    "pending": False,
                    "label": "Old Work",
                }
            },
        )
        monkeypatch.setattr(app_module, "_away_mode_state", {})
        monkeypatch.setattr(app_module, "_go_nuts_state", {})

        binding = app_module._session_close_binding("test-session", "admin")

        assert binding["resume_uuid"] == ""
        assert active.call_count == 2
        old_messages.assert_not_called()
        old_audit.assert_not_called()

    def test_generation_with_conversation_input_cannot_skip_archive(
        self, tmp_path, monkeypatch
    ):
        self._patch_close_binding_state(
            monkeypatch,
            tmp_path,
            recorded_root="",
            active_root=None,
            had_conversation_input=True,
        )

        with pytest.raises(
            app_module._SessionCloseError,
            match="exact Codex conversation could not be verified",
        ):
            app_module._session_close_binding("test-session", "admin")

    def test_completed_label_for_current_generation_prevents_rootless_close(
        self, tmp_path, monkeypatch
    ):
        self._patch_close_binding_state(
            monkeypatch,
            tmp_path,
            recorded_root="",
            active_root=None,
            had_conversation_input=False,
        )
        monkeypatch.setattr(
            app_module,
            "_session_tab_label_rows",
            lambda: {
                "test-session": {
                    "owner_id": "admin",
                    "generation": "a" * 32,
                    "pending": False,
                    "label": "Current Work",
                }
            },
        )
        monkeypatch.setattr(app_module, "_away_mode_state", {})
        monkeypatch.setattr(app_module, "_go_nuts_state", {})

        with pytest.raises(
            app_module._SessionCloseError,
            match="exact Codex conversation could not be verified",
        ):
            app_module._session_close_binding("test-session", "admin")

    def test_stale_checkpoint_root_cannot_be_closed(self, tmp_path, monkeypatch):
        owner = self._binding(tmp_path)["owner"]
        old_root = self._binding(tmp_path)["resume_uuid"]
        monkeypatch.setattr(
            app_module,
            "_strict_session_owner",
            lambda *_args: ("admin", owner),
        )
        monkeypatch.setattr(
            app_module._session_lifecycle,
            "get",
            lambda _name: {
                "managed": True,
                "owner_id": "admin",
                "desired_state": "running",
                "generation": "a" * 32,
                "resume_uuid": old_root,
            },
        )
        monkeypatch.setattr(app_module, "_durable_session_cwd", lambda *_args: str(tmp_path))
        monkeypatch.setattr(app_module, "_validated_session_root_thread_id", lambda *_args: old_root)
        monkeypatch.setattr(app_module, "_exact_tmux_session_state", lambda _name: ("present", "$1"))
        monkeypatch.setattr(app_module, "_tmux_session_matches_owner", lambda *_args: True)
        monkeypatch.setattr(
            app_module,
            "_active_session_root_thread_id",
            lambda *_args: "01a00000-0000-7000-8000-000000000001",
        )
        monkeypatch.setattr(app_module, "get_session_cwd", lambda _name: str(tmp_path))
        with pytest.raises(app_module._SessionCloseError):
            app_module._session_close_binding("test-session", "admin")

    @pytest.mark.asyncio
    async def test_simple_watchdog_records_input_before_sending_tmux_keys(
        self, monkeypatch, owned_autopush_runtime
    ):
        events = []

        class OperationFence:
            def __enter__(self):
                events.append(("lock", "enter"))

            def __exit__(self, *_args):
                events.append(("lock", "exit"))

        def touch(*args, **kwargs):
            events.append(("touch", args, kwargs))
            return {"had_conversation_input": True}

        def send_keys(command, **_kwargs):
            events.append(("tmux", command))
            return MagicMock(returncode=0)

        monkeypatch.setattr(
            app_module,
            "_session_operation_lock",
            lambda _name: OperationFence(),
        )
        monkeypatch.setattr(app_module._session_lifecycle, "touch", touch)
        monkeypatch.setattr(app_module.subprocess, "run", send_keys)
        monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())

        sent = await app_module._simple_watchdog_send_text(
            "test-session", "  keep   going  "
        )

        assert sent is True
        assert events[0] == ("lock", "enter")
        assert events[1] == (
            "touch",
            ("test-session",),
            {"source": "autopilot-watchdog", "records_input": True},
        )
        assert events[2][0] == "tmux"
        assert events[2][1] == [
            "tmux", "send-keys", "-t", "test-session", "-l", "keep going",
        ]
        assert events[3][0] == "tmux"
        assert events[3][1] == [
            "tmux", "send-keys", "-t", "test-session", "Enter",
        ]
        assert events[4] == ("lock", "exit")

    @pytest.mark.parametrize("replacement", ["owner", "generation"])
    def test_close_barrier_install_rejects_replacement_under_operation_lock(
        self, replacement, monkeypatch
    ):
        expected_generation = "a" * 32
        state = {"owner_id": "admin", "generation": expected_generation}
        owner = {"id": "admin", "username": "admin", "role": "admin"}

        class ReplacingOperationFence:
            def __enter__(self):
                if replacement == "owner":
                    state["owner_id"] = "u_replacement"
                else:
                    state["generation"] = "b" * 32

            def __exit__(self, *_args):
                return None

        monkeypatch.setattr(
            app_module,
            "_session_operation_lock",
            lambda _name: ReplacingOperationFence(),
        )
        monkeypatch.setattr(
            app_module,
            "_strict_session_owner",
            lambda _name, expected_owner="": (
                ("admin", owner)
                if state["owner_id"] == "admin"
                and (not expected_owner or expected_owner == "admin")
                else None
            ),
        )
        monkeypatch.setattr(
            app_module._session_lifecycle,
            "matches",
            lambda _name, *, generation, owner_id, desired_states: (
                state["generation"] == generation
                and state["owner_id"] == owner_id
                and desired_states == {"running"}
            ),
        )
        exact_tmux = MagicMock(return_value=("present", "$1"))
        monkeypatch.setattr(app_module, "_exact_tmux_session_state", exact_tmux)

        with pytest.raises(
            app_module._SessionCloseError,
            match=(
                "Session ownership changed; retry close"
                if replacement == "owner"
                else "Session generation changed; retry close"
            ),
        ):
            app_module._install_session_close_barrier(
                "test-session",
                "close_candidate_job",
                "admin",
                expected_generation,
            )

        assert "test-session" not in app_module._session_close_barriers
        exact_tmux.assert_not_called()

    @pytest.mark.asyncio
    async def test_concurrent_close_start_cannot_overwrite_existing_barrier(
        self, monkeypatch
    ):
        existing_job = "close_existing_job"
        app_module._session_close_barriers["test-session"] = existing_job
        owner = {"id": "admin", "username": "admin", "role": "admin"}
        lifecycle = {
            "managed": True,
            "owner_id": "admin",
            "desired_state": "running",
            "generation": "a" * 32,
        }
        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(app_module, "_find_user_by_id", lambda _owner: owner)
        monkeypatch.setattr(
            app_module,
            "_strict_session_owner",
            lambda *_args: ("admin", owner),
        )
        monkeypatch.setattr(
            app_module._session_lifecycle,
            "get",
            lambda _name: lifecycle,
        )

        result = await app_module._start_session_close("admin", "test-session")

        assert result["ok"] is False
        assert result["_status"] == 409
        assert app_module._session_close_barriers["test-session"] == existing_job
        assert ("admin", "test-session") not in app_module._session_close_jobs
        assert ("admin", "test-session") not in app_module._session_close_tasks

    @pytest.mark.asyncio
    async def test_close_job_finally_preserves_replacement_barrier(self, monkeypatch):
        original_job = "close_original_job"
        replacement_job = "close_replacement_job"
        app_module._session_close_barriers["test-session"] = original_job

        async def replace_barrier(_session_name):
            app_module._session_close_barriers["test-session"] = replacement_job

        monkeypatch.setattr(
            app_module,
            "_pause_autonomous_work_for_close",
            replace_barrier,
        )
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={}),
        )
        owner = {"id": "admin", "username": "admin", "role": "admin"}
        job = {"id": original_job, "session": "test-session"}

        await app_module._run_session_close_job(owner, job)

        assert job["status"] == "failed"
        assert app_module._session_close_barriers["test-session"] == replacement_job

    @pytest.mark.asyncio
    async def test_close_barrier_rejects_new_session_resume(self, monkeypatch):
        resume = AsyncMock()
        monkeypatch.setattr(app_module, "_resume_parked_session", resume)
        app_module._session_close_barriers["test-session"] = "job-id"
        try:
            result = await app_module._controller_dispatch({
                "op": "session_resume",
                "session": "test-session",
                "source": "send-command",
            })
        finally:
            app_module._session_close_barriers.pop("test-session", None)
        assert result["_status"] == 409
        resume.assert_not_awaited()

    def test_inflight_send_cannot_cross_exclusive_close_fence(
        self, authed_client, monkeypatch
    ):
        monkeypatch.setattr(app_module, "get_tmux_sessions", lambda: MOCK_SESSIONS)
        monkeypatch.setattr(
            app_module,
            "_controller_call",
            AsyncMock(return_value={"ok": True}),
        )
        monkeypatch.setattr(
            app_module,
            "_wait_for_codex_input_ready",
            AsyncMock(return_value=True),
        )
        send = MagicMock()
        monkeypatch.setattr(app_module.subprocess, "run", send)
        with app_module._session_operation_lock("test-session"):
            response = authed_client.post(
                "/api/sessions/test-session/send",
                json={"command": "must not cross close"},
            )
        assert response.status_code == 409
        send.assert_not_called()

    def test_send_obeys_authoritative_input_check_under_operation_fence(
        self, authed_client, monkeypatch
    ):
        lock_state = {"held": False}
        operations = []

        class TrackedOperationFence:
            def __enter__(self):
                assert lock_state["held"] is False
                lock_state["held"] = True

            def __exit__(self, *_args):
                lock_state["held"] = False

        async def controller_call(operation, **kwargs):
            operations.append((operation, kwargs))
            if operation == "session_resume":
                assert lock_state["held"] is False
                return {"ok": True}
            assert operation == "session_input_check"
            assert lock_state["held"] is True
            return {
                "ok": False,
                "error": "Session is being summarized and closed",
                "_status": 409,
            }

        send = MagicMock()
        terminal_binding = MagicMock()
        monkeypatch.setattr(app_module, "get_tmux_sessions", lambda: MOCK_SESSIONS)
        monkeypatch.setattr(app_module, "_controller_call", controller_call)
        monkeypatch.setattr(
            app_module,
            "_wait_for_codex_input_ready",
            AsyncMock(return_value=True),
        )
        monkeypatch.setattr(
            app_module,
            "_session_operation_lock",
            lambda _name: TrackedOperationFence(),
        )
        monkeypatch.setattr(app_module, "_terminal_binding", terminal_binding)
        monkeypatch.setattr(app_module.subprocess, "run", send)

        response = authed_client.post(
            "/api/sessions/test-session/send",
            json={"command": "must not cross authoritative close fence"},
        )

        assert response.status_code == 409
        assert response.json()["error"] == "Session is being summarized and closed"
        assert [operation for operation, _kwargs in operations] == [
            "session_resume",
            "session_input_check",
        ]
        assert operations[1][1]["records_input"] is True
        assert operations[1][1]["source"] == "send-command"
        assert lock_state["held"] is False
        terminal_binding.assert_not_called()
        send.assert_not_called()

    @pytest.mark.asyncio
    async def test_spec_failure_leaves_tab_open(self, tmp_path, monkeypatch):
        binding = self._binding(tmp_path)
        context = {
            "messages": [{"role": "assistant", "text": "Work result"}],
            "source_hash": "c" * 64,
            "source_bytes": 10,
            "root_thread_id": binding["resume_uuid"],
        }
        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={"managed": True}),
        )
        monkeypatch.setattr(app_module, "_checkpoint_active_session", MagicMock(return_value={}))
        monkeypatch.setattr(app_module, "_session_close_binding", lambda *_args: binding)
        monkeypatch.setattr(app_module, "_capture_session_close_context", lambda _binding: context)
        monkeypatch.setattr(app_module, "_session_close_source_unchanged", lambda *_args: True)
        monkeypatch.setattr(app_module, "async_detect_activity", AsyncMock(return_value={"status": "idle"}))
        monkeypatch.setattr(app_module, "_summarize_session_close", AsyncMock(return_value="summary"))
        monkeypatch.setattr(
            app_module,
            "_persist_session_close_knowledge",
            MagicMock(side_effect=OSError("disk full")),
        )
        delete = AsyncMock()
        monkeypatch.setattr(app_module, "_api_delete_session_unlocked", delete)
        job = {"session": "test-session"}

        await app_module._run_session_close_job(binding["owner"], job)

        assert job["status"] == "failed"
        assert job["tab_state"] == "open"
        delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rootless_close_publishes_unarchived_and_skips_archive_pipeline(
        self, tmp_path, monkeypatch
    ):
        binding = {**self._binding(tmp_path), "resume_uuid": ""}
        pause = AsyncMock()
        capture = MagicMock()
        summarize = AsyncMock()
        persist = MagicMock()
        source_unchanged = MagicMock()
        delete = AsyncMock(
            return_value=app_module.JSONResponse(
                {"ok": True, "killed": "test-session"}
            )
        )

        @asynccontextmanager
        async def tmux_lock():
            yield

        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(app_module, "_pause_autonomous_work_for_close", pause)
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={"managed": True}),
        )
        checkpoint = MagicMock(return_value={"managed": True})
        monkeypatch.setattr(app_module, "_checkpoint_active_session", checkpoint)
        close_binding = MagicMock(return_value=binding)
        monkeypatch.setattr(app_module, "_session_close_binding", close_binding)
        monkeypatch.setattr(app_module, "async_detect_activity", AsyncMock(
            return_value={"status": "idle"}
        ))
        monkeypatch.setattr(app_module, "_capture_session_close_context", capture)
        monkeypatch.setattr(app_module, "_summarize_session_close", summarize)
        monkeypatch.setattr(app_module, "_persist_session_close_knowledge", persist)
        monkeypatch.setattr(
            app_module,
            "_session_close_source_unchanged",
            source_unchanged,
        )
        monkeypatch.setattr(app_module, "_api_delete_session_unlocked", delete)
        monkeypatch.setattr(app_module, "_async_tmux_server_mutation_lock", tmux_lock)
        job = {"session": "test-session"}

        await app_module._run_session_close_job(binding["owner"], job)

        public_job = app_module._public_session_close_job(job)
        assert job["status"] == "completed"
        assert job["tab_state"] == "closed"
        assert public_job["archived"] is False
        assert public_job["spec_file"] is None
        pause.assert_awaited_once_with("test-session")
        capture.assert_not_called()
        summarize.assert_not_awaited()
        persist.assert_not_called()
        source_unchanged.assert_not_called()
        checkpoint.assert_called_once_with(
            "test-session",
            source="knowledge-close-final",
            expected_owner_id="admin",
        )
        assert close_binding.call_count == 2
        delete.assert_awaited_once_with(
            None,
            "test-session",
            expected_user=binding["owner"],
        )

    @pytest.mark.asyncio
    async def test_verified_bootstrap_only_close_is_unarchived_and_deletes(
        self, tmp_path, monkeypatch
    ):
        binding = {
            **self._binding(tmp_path),
            "conversation_input_known": True,
            "had_conversation_input": False,
        }
        context = {
            "messages": [],
            "parse_errors": 0,
            "source_hash": "e" * 64,
            "source_bytes": 123,
            "source_relative": (
                f"2026/08/29/rollout-test-{binding['resume_uuid']}.jsonl"
            ),
            "source_fingerprint": (1, 2, 123, 4),
            "root_thread_id": binding["resume_uuid"],
        }
        pause = AsyncMock()
        capture = MagicMock(return_value=context)
        summarize = AsyncMock()
        persist = MagicMock()
        source_unchanged = MagicMock(return_value=True)
        delete = AsyncMock(
            return_value=app_module.JSONResponse(
                {"ok": True, "killed": "test-session"}
            )
        )

        @asynccontextmanager
        async def tmux_lock():
            yield

        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(app_module, "_pause_autonomous_work_for_close", pause)
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={"managed": True}),
        )
        checkpoint = MagicMock(return_value={"managed": True})
        monkeypatch.setattr(app_module, "_checkpoint_active_session", checkpoint)
        close_binding = MagicMock(return_value=binding)
        monkeypatch.setattr(app_module, "_session_close_binding", close_binding)
        monkeypatch.setattr(
            app_module,
            "async_detect_activity",
            AsyncMock(return_value={"status": "idle"}),
        )
        monkeypatch.setattr(app_module, "_capture_session_close_context", capture)
        monkeypatch.setattr(app_module, "_summarize_session_close", summarize)
        monkeypatch.setattr(app_module, "_persist_session_close_knowledge", persist)
        monkeypatch.setattr(
            app_module,
            "_session_close_source_unchanged",
            source_unchanged,
        )
        monkeypatch.setattr(app_module, "_api_delete_session_unlocked", delete)
        monkeypatch.setattr(app_module, "_async_tmux_server_mutation_lock", tmux_lock)
        job = {"session": "test-session"}

        await app_module._run_session_close_job(binding["owner"], job)

        public_job = app_module._public_session_close_job(job)
        assert job["status"] == "completed"
        assert job["tab_state"] == "closed"
        assert public_job["archived"] is False
        assert public_job["spec_file"] is None
        capture.assert_called_once_with(binding)
        summarize.assert_not_awaited()
        persist.assert_not_called()
        source_unchanged.assert_called_once_with(binding, context)
        checkpoint.assert_called_once_with(
            "test-session",
            source="knowledge-close-final",
            expected_owner_id="admin",
        )
        assert close_binding.call_count == 2
        delete.assert_awaited_once_with(
            None,
            "test-session",
            expected_user=binding["owner"],
        )

    @pytest.mark.asyncio
    async def test_changed_bootstrap_only_source_fails_before_delete(
        self, tmp_path, monkeypatch
    ):
        binding = {
            **self._binding(tmp_path),
            "conversation_input_known": True,
            "had_conversation_input": False,
        }
        context = {
            "messages": [],
            "parse_errors": 0,
            "source_hash": "f" * 64,
            "source_bytes": 123,
            "source_relative": (
                f"2026/08/29/rollout-test-{binding['resume_uuid']}.jsonl"
            ),
            "source_fingerprint": (1, 2, 123, 4),
            "root_thread_id": binding["resume_uuid"],
        }
        capture = MagicMock(return_value=context)
        summarize = AsyncMock()
        persist = MagicMock()
        source_unchanged = MagicMock(return_value=False)
        delete = AsyncMock()

        @asynccontextmanager
        async def tmux_lock():
            yield

        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(
            app_module,
            "_pause_autonomous_work_for_close",
            AsyncMock(),
        )
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={"managed": True}),
        )
        checkpoint = MagicMock(return_value={"managed": True})
        monkeypatch.setattr(app_module, "_checkpoint_active_session", checkpoint)
        close_binding = MagicMock(return_value=binding)
        monkeypatch.setattr(app_module, "_session_close_binding", close_binding)
        monkeypatch.setattr(
            app_module,
            "async_detect_activity",
            AsyncMock(return_value={"status": "idle"}),
        )
        monkeypatch.setattr(app_module, "_capture_session_close_context", capture)
        monkeypatch.setattr(app_module, "_summarize_session_close", summarize)
        monkeypatch.setattr(app_module, "_persist_session_close_knowledge", persist)
        monkeypatch.setattr(
            app_module,
            "_session_close_source_unchanged",
            source_unchanged,
        )
        monkeypatch.setattr(app_module, "_api_delete_session_unlocked", delete)
        monkeypatch.setattr(app_module, "_async_tmux_server_mutation_lock", tmux_lock)
        job = {"session": "test-session"}

        await app_module._run_session_close_job(binding["owner"], job)

        assert job["status"] == "failed"
        assert job["tab_state"] == "unknown"
        assert job["archived"] is False
        assert job["error"] == (
            "New session activity arrived before close; retry to include it"
        )
        capture.assert_called_once_with(binding)
        summarize.assert_not_awaited()
        persist.assert_not_called()
        source_unchanged.assert_called_once_with(binding, context)
        checkpoint.assert_called_once_with(
            "test-session",
            source="knowledge-close-final",
            expected_owner_id="admin",
        )
        assert close_binding.call_count == 2
        delete.assert_not_awaited()

    @pytest.mark.parametrize(
        ("conversation_input_known", "had_conversation_input", "parse_errors"),
        [
            (False, False, 0),
            (True, True, 0),
            (True, False, 1),
            (True, False, None),
        ],
        ids=(
            "unknown-input-marker",
            "known-user-input",
            "parse-error",
            "missing-parse-errors",
        ),
    )
    @pytest.mark.asyncio
    async def test_empty_rootful_context_fails_closed_without_positive_proof(
        self,
        conversation_input_known,
        had_conversation_input,
        parse_errors,
        tmp_path,
        monkeypatch,
    ):
        binding = {
            **self._binding(tmp_path),
            "conversation_input_known": conversation_input_known,
            "had_conversation_input": had_conversation_input,
        }
        context = {
            "messages": [],
            "source_hash": "1" * 64,
            "source_bytes": 123,
            "root_thread_id": binding["resume_uuid"],
        }
        if parse_errors is not None:
            context["parse_errors"] = parse_errors
        capture = MagicMock(return_value=context)
        summarize = AsyncMock()
        persist = MagicMock()
        source_unchanged = MagicMock()
        delete = AsyncMock()
        checkpoint = MagicMock()
        close_binding = MagicMock(return_value=binding)

        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(
            app_module,
            "_pause_autonomous_work_for_close",
            AsyncMock(),
        )
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={"managed": True}),
        )
        monkeypatch.setattr(app_module, "_checkpoint_active_session", checkpoint)
        monkeypatch.setattr(app_module, "_session_close_binding", close_binding)
        monkeypatch.setattr(
            app_module,
            "async_detect_activity",
            AsyncMock(return_value={"status": "idle"}),
        )
        monkeypatch.setattr(app_module, "_capture_session_close_context", capture)
        monkeypatch.setattr(app_module, "_summarize_session_close", summarize)
        monkeypatch.setattr(app_module, "_persist_session_close_knowledge", persist)
        monkeypatch.setattr(
            app_module,
            "_session_close_source_unchanged",
            source_unchanged,
        )
        monkeypatch.setattr(app_module, "_api_delete_session_unlocked", delete)
        job = {"session": "test-session"}

        await app_module._run_session_close_job(binding["owner"], job)

        assert job["status"] == "failed"
        assert job["tab_state"] == "open"
        assert job["error"] == (
            "No conversation could be recovered from the exact Codex transcript; "
            "the tab was not closed"
        )
        capture.assert_called_once_with(binding)
        summarize.assert_not_awaited()
        persist.assert_not_called()
        source_unchanged.assert_not_called()
        checkpoint.assert_not_called()
        close_binding.assert_called_once_with("test-session", "admin")
        delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_spec_is_persisted_before_tmux_delete(self, tmp_path, monkeypatch):
        binding = self._binding(tmp_path)
        context = {
            "messages": [{"role": "assistant", "text": "Work result"}],
            "source_hash": "d" * 64,
            "source_bytes": 10,
            "root_thread_id": binding["resume_uuid"],
        }
        state = {"persisted": False}
        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={"managed": True}),
        )
        monkeypatch.setattr(app_module, "_checkpoint_active_session", MagicMock(return_value={}))
        monkeypatch.setattr(app_module, "_session_close_binding", lambda *_args: binding)
        monkeypatch.setattr(app_module, "_capture_session_close_context", lambda _binding: context)
        monkeypatch.setattr(app_module, "_session_close_source_unchanged", lambda *_args: True)
        monkeypatch.setattr(app_module, "async_detect_activity", AsyncMock(return_value={"status": "idle"}))
        monkeypatch.setattr(app_module, "_summarize_session_close", AsyncMock(return_value="summary"))

        def persist(*_args):
            state["persisted"] = True
            return {"spec_path": str(tmp_path / "TECHNICAL_SPEC.md")}

        async def delete(*_args, **_kwargs):
            assert state["persisted"] is True
            return app_module.JSONResponse({"ok": True, "killed": "test-session"})

        @asynccontextmanager
        async def tmux_lock():
            yield

        monkeypatch.setattr(app_module, "_persist_session_close_knowledge", persist)
        monkeypatch.setattr(app_module, "_api_delete_session_unlocked", delete)
        monkeypatch.setattr(app_module, "_async_tmux_server_mutation_lock", tmux_lock)
        job = {"session": "test-session"}

        await app_module._run_session_close_job(binding["owner"], job)

        assert job["status"] == "completed"
        assert job["spec_file"] == "TECHNICAL_SPEC.md"

    @pytest.mark.asyncio
    async def test_generation_change_aborts_before_spec_or_delete(self, tmp_path, monkeypatch):
        original = self._binding(tmp_path)
        replaced = {**original, "generation": "e" * 32, "session_id": "$2"}
        bindings = iter([original, replaced])
        context = {
            "messages": [{"role": "assistant", "text": "Work result"}],
            "source_hash": "f" * 64,
            "source_bytes": 10,
            "root_thread_id": original["resume_uuid"],
        }
        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={"managed": True}),
        )
        monkeypatch.setattr(app_module, "_checkpoint_active_session", MagicMock(return_value={}))
        monkeypatch.setattr(app_module, "_session_close_binding", lambda *_args: next(bindings))
        monkeypatch.setattr(app_module, "_capture_session_close_context", lambda _binding: context)
        monkeypatch.setattr(app_module, "async_detect_activity", AsyncMock(return_value={"status": "idle"}))
        monkeypatch.setattr(app_module, "_summarize_session_close", AsyncMock(return_value="summary"))
        persist = MagicMock()
        delete = AsyncMock()
        monkeypatch.setattr(app_module, "_persist_session_close_knowledge", persist)
        monkeypatch.setattr(app_module, "_api_delete_session_unlocked", delete)
        job = {"session": "test-session"}

        await app_module._run_session_close_job(original["owner"], job)

        assert job["status"] == "failed"
        persist.assert_not_called()
        delete.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_partial_delete_is_uncertain_not_retryable_open(self, tmp_path, monkeypatch):
        binding = self._binding(tmp_path)
        context = {
            "messages": [{"role": "assistant", "text": "Work result"}],
            "source_hash": "9" * 64,
            "source_bytes": 10,
            "root_thread_id": binding["resume_uuid"],
        }
        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(
            app_module,
            "_checkpoint_active_session_serialized",
            AsyncMock(return_value={"managed": True}),
        )
        monkeypatch.setattr(app_module, "_checkpoint_active_session", MagicMock(return_value={}))
        monkeypatch.setattr(app_module, "_session_close_binding", lambda *_args: binding)
        monkeypatch.setattr(app_module, "_capture_session_close_context", lambda _binding: context)
        monkeypatch.setattr(app_module, "_session_close_source_unchanged", lambda *_args: True)
        monkeypatch.setattr(app_module, "async_detect_activity", AsyncMock(return_value={"status": "idle"}))
        monkeypatch.setattr(app_module, "_summarize_session_close", AsyncMock(return_value="summary"))
        monkeypatch.setattr(
            app_module,
            "_persist_session_close_knowledge",
            lambda *_args: {"spec_path": str(tmp_path / "TECHNICAL_SPEC.md")},
        )
        monkeypatch.setattr(
            app_module,
            "_api_delete_session_unlocked",
            AsyncMock(return_value=app_module.JSONResponse({"error": "kill failed"}, status_code=500)),
        )
        monkeypatch.setattr(app_module, "_exact_tmux_session_state", lambda _name: ("present", "$1"))
        monkeypatch.setattr(
            app_module._session_lifecycle,
            "get",
            lambda _name: {"desired_state": "deleting"},
        )

        job = {"session": "test-session"}
        await app_module._run_session_close_job(binding["owner"], job)

        assert job["status"] == "failed"
        assert job["tab_state"] == "unknown"

    @pytest.mark.asyncio
    async def test_duplicate_close_start_reuses_running_job(self, monkeypatch):
        gate = asyncio.Event()
        owner = {"id": "admin", "username": "admin", "role": "admin"}
        generation = "a" * 32
        monkeypatch.setattr(app_module, "_shutting_down", False)
        monkeypatch.setattr(app_module, "_find_user_by_id", lambda _owner: owner)
        monkeypatch.setattr(app_module, "_strict_session_owner", lambda *_args: ("admin", owner))
        monkeypatch.setattr(
            app_module._session_lifecycle,
            "get",
            lambda _name: {
                "managed": True,
                "owner_id": "admin",
                "desired_state": "running",
                "generation": generation,
            },
        )
        monkeypatch.setattr(
            app_module._session_lifecycle,
            "matches",
            lambda _name, **_kwargs: True,
        )
        monkeypatch.setattr(
            app_module,
            "_exact_tmux_session_state",
            lambda _name: ("present", "$1"),
        )
        monkeypatch.setattr(
            app_module,
            "_tmux_session_matches_owner",
            lambda *_args: True,
        )

        async def worker(_owner, job):
            job["status"] = "running"
            await gate.wait()
            job["status"] = "completed"

        monkeypatch.setattr(app_module, "_run_session_close_job", worker)
        app_module._session_close_jobs.clear()
        app_module._session_close_tasks.clear()
        first = await app_module._start_session_close("admin", "test-session")
        second = await app_module._start_session_close("admin", "test-session")
        assert first["accepted"] is True
        assert second["accepted"] is False
        assert second["job"]["id"] == first["job"]["id"]
        gate.set()
        await app_module._session_close_tasks[("admin", "test-session")]
        app_module._session_close_barriers.pop("test-session", None)


# ─── Send Command Success Path Tests ───


class TestSendCommandEndpoint:
    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_send_short_command_success(self, mock_run, mock_sessions, authed_client):
        """Short commands (<=200 chars) should be sent via send-keys and return ok=True."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        with (
            patch("app.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("app._live_tmux_session_names", return_value={"test-session"}),
            patch("app._checkpoint_active_session", return_value={}),
        ):
            resp = authed_client.post(
                "/api/sessions/test-session/send",
                json={"command": "echo hello"},
            )
        assert resp.status_code == 200
        data = resp.json()
        assert data["ok"] is True
        assert data["sent"] == "echo hello"
        mock_sleep.assert_any_await(0.25)
        # The send path scrolls the pane back into view first, and submits with
        # C-m rather than the "Enter" key name; both reach tmux identically.
        # The final -8 is the post-submit pane check that confirms the prompt
        # left the composer instead of becoming stranded there.
        assert [call.args[0][-1] for call in mock_run.call_args_list] == ["-40", "echo hello", "C-m", "-8"]

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app.subprocess.run")
    def test_send_slash_command_stays_literal_when_account_instructions_are_stale(
        self, mock_run, mock_sessions, authed_client, tmp_path
    ):
        """A stale member thread must not turn a native Codex command into prose."""
        import app

        codex_home = tmp_path / ".codex-user-u_member"
        codex_home.mkdir()
        (codex_home / "AGENTS.md").write_text("Updated member instructions.\n")
        member = {"id": "u_member", "username": "member", "role": "user"}
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        with (
            patch("app._user_for_session", return_value=member),
            patch("app._user_codex_config_dir", return_value=codex_home),
            patch("app._session_codex_process_id", return_value=321),
            patch("app._process_environment", return_value={}),
            patch("app._session_account_instruction_marker", return_value=("", "")),
            patch("app._ensure_codex_submitted", new=AsyncMock(return_value="submitted")),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
        ):
            resp = authed_client.post(
                "/api/sessions/test-session/send",
                json={"command": "/model gpt-5.6-sol"},
            )
            # The refresh stays pending and still wraps the next ordinary prompt.
            wrapped, marker = app._account_instruction_refresh_for_prompt(
                "test-session", "continue the task"
            )

        assert resp.status_code == 200
        literal_inputs = [
            call.args[0][-1]
            for call in mock_run.call_args_list
            if call.args and call.args[0][:4] == ["tmux", "send-keys", "-t", "$99"]
            and "-l" in call.args[0]
        ]
        assert literal_inputs == ["/model gpt-5.6-sol"]
        assert all("[Read/apply " not in value for value in literal_inputs)
        assert wrapped.endswith("ORIGINAL:\ncontinue the task")
        assert marker is not None

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
    def test_valid_mode_is_managed_per_account_without_tmux_secret(self, mock_run, mock_sessions, authed_client):
        """The retired pane toggle cannot place credentials in terminal history."""
        resp = authed_client.post(
            "/api/sessions/test-session/set-auth-mode",
            json={"mode": "api"},
        )
        assert resp.status_code == 409
        assert "per account" in resp.json()["error"].lower()
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
        sentinel = {
            "available": True,
            "_ts": time.time(),
            "_sentinel": True,
            "_identity": _app._session_derived_cache_identity(name),
        }
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
    """A pane counts as active only when one of its descendants is Codex.

    Descendants come from a single cached ``ps -eo pid=,ppid=,comm=`` snapshot,
    not the per-process ``pgrep -P`` walk these tests were first written for, so
    the second mocked call has to look like ps output and the cache has to be
    cleared between cases.
    """

    @pytest.fixture(autouse=True)
    def _clear_process_tree_cache(self):
        import app as _app
        _app._PROCESS_TREE_CACHE = None
        yield
        _app._PROCESS_TREE_CACHE = None

    @patch("app.subprocess.run")
    def test_returns_true_for_codex_descendant(self, mock_run):
        import app as _app

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="100\n", stderr=""),          # pane_pid
            MagicMock(returncode=0, stdout="100 1 bash\n101 100 codex\n",
                      stderr=""),                                        # ps snapshot
        ]
        assert _app._is_codex_running("test-session") is True

    @patch("app.subprocess.run")
    def test_returns_false_for_unrelated_node_descendant(self, mock_run):
        import app as _app

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="100\n", stderr=""),
            MagicMock(returncode=0, stdout="100 1 bash\n101 100 node\n", stderr=""),
        ]
        assert _app._is_codex_running("test-session") is False

    @patch("app.subprocess.run")
    def test_returns_false_for_codex_outside_the_pane(self, mock_run):
        """A Codex running under a *different* pane must not count."""
        import app as _app

        mock_run.side_effect = [
            MagicMock(returncode=0, stdout="100\n", stderr=""),
            MagicMock(returncode=0, stdout="100 1 bash\n999 2 codex\n", stderr=""),
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

class TestAutonomousStatePersistence:
    """Unit tests for _save_autonomous_state() and _load_autonomous_state()."""

    def test_save_and_load_roundtrip(self, tmp_path):
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        orig_away = dict(_app._away_mode_state)
        orig_nuts = dict(_app._go_nuts_state)

        _app._away_mode_state["my-session"] = {"enabled": True}
        _app._go_nuts_state["my-session"] = {"enabled": False}  # not enabled, should not be saved

        try:
            with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file), \
                 patch.object(
                     _app,
                     "_autonomous_state_store",
                     _app.LockedJsonStore(state_file, dict),
                 ), \
                 patch("app.MESSAGES_DIR", tmp_path):
                _app._save_autonomous_state()
                loaded = _app._load_autonomous_state()
        finally:
            _app._away_mode_state.clear()
            _app._away_mode_state.update(orig_away)
            _app._go_nuts_state.clear()
            _app._go_nuts_state.update(orig_nuts)

        assert loaded.get("my-session", {}).get("away_mode") is True
        assert "go_nuts_mode" not in loaded.get("my-session", {})

    def test_load_returns_empty_when_file_missing(self, tmp_path):
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file), \
             patch.object(
                 _app,
                 "_autonomous_state_store",
                 _app.LockedJsonStore(state_file, dict),
             ):
            result = _app._load_autonomous_state()
        assert result == {}

    def test_load_handles_corrupt_json(self, tmp_path):
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        state_file.write_text("{invalid json}")
        with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file), \
             patch.object(
                 _app,
                 "_autonomous_state_store",
                 _app.LockedJsonStore(state_file, dict),
             ):
            result = _app._load_autonomous_state()
        assert result == {}

    def test_save_handles_write_exception(self, tmp_path):
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file), \
             patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            _app._save_autonomous_state()  # should not raise

    def test_save_includes_go_nuts_when_enabled(self, tmp_path):
        """Cover line 138: go_nuts_state with enabled=True is persisted."""
        import app as _app
        state_file = tmp_path / "autonomous-modes.json"
        orig_nuts = dict(_app._go_nuts_state)

        _app._go_nuts_state["nuts-session"] = {"enabled": True}

        try:
            with patch.object(_app, "AUTONOMOUS_STATE_FILE", state_file), \
                 patch.object(
                     _app,
                     "_autonomous_state_store",
                     _app.LockedJsonStore(state_file, dict),
                 ), \
                 patch("app.MESSAGES_DIR", tmp_path):
                _app._save_autonomous_state()
                loaded = _app._load_autonomous_state()
        finally:
            _app._go_nuts_state.clear()
            _app._go_nuts_state.update(orig_nuts)

        assert loaded.get("nuts-session", {}).get("go_nuts_mode") is True


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
             patch.object(_app, "cache", {"sess1": {
                 "notes": "my note",
                 "_owner_id": "admin",
                 "_logical_incarnation": "legacy-test",
             }}):
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

        mock_client = MagicMock()
        mock_client.chat.completions.create = AsyncMock(return_value=mock_resp)
        with patch.object(_app, "client", mock_client), \
             patch("app.asyncio.wait_for", fake_wait_for):
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
# _go_nuts_log() — direct unit test (covers lines 3692-3695)
# ---------------------------------------------------------------------------

class TestGoNutsLog:
    """Unit tests for _go_nuts_log() helper."""

    def test_appends_entry_to_state_log(self):
        import app as _app

        state = {"phase": 2, "step": 3}
        _app._go_nuts_log(state, "test action")

        assert "log" in state
        assert len(state["log"]) == 1
        entry = state["log"][0]
        assert entry["action"] == "test action"
        assert entry["phase"] == 2
        assert entry["step"] == 3
        assert "ts" in entry

    def test_trims_log_to_log_cap(self):
        import app as _app

        state = {"phase": 0, "step": 0, "log": [{"ts": 0, "phase": 0, "step": 0, "action": f"old-{i}"} for i in range(_app._GO_NUTS_LOG_CAP)]}
        _app._go_nuts_log(state, "new action")

        assert len(state["log"]) == _app._GO_NUTS_LOG_CAP
        assert state["log"][-1]["action"] == "new action"


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
    async def test_owner_environment_failure_aborts_before_relaunch(self):
        import app as _app

        async def run_inline(function, *args, **kwargs):
            return function(*args, **kwargs)

        with (
            patch(
                "app._async_is_codex_running",
                new_callable=AsyncMock,
                return_value=False,
            ),
            patch("app._send_session_owner_environment", return_value=False),
            patch("app.asyncio.to_thread", side_effect=run_inline),
            patch("app._session_launch_command") as launch,
        ):
            result = await _app._ensure_codex_running("my-session")

        assert result is False
        launch.assert_not_called()

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
             patch("app._send_session_owner_environment", return_value=True), \
             patch("app._exact_tmux_session_id", return_value="$1"), \
             patch("app.subprocess.run", return_value=MagicMock(returncode=0)), \
             patch("app._find_session_transcript_uuid", return_value=None), \
             patch("app.asyncio.sleep", new_callable=AsyncMock), \
             patch.object(_app._session_lifecycle, "get", return_value={}):
            result = await _app._ensure_codex_running(
                "my-session",
                log_fn=lambda s, msg: log_entries.append(msg),
                state=state,
            )
        assert result is True
        assert any("restarted" in e for e in log_entries)  # covers line 210

    @pytest.mark.asyncio
    async def test_recovery_relaunch_pins_saved_model_and_effort(self):
        """Crash recovery must not let stale rollout settings win on resume."""
        import app as _app

        is_running_values = [False, True]

        async def fake_is_running(session_name):
            return is_running_values.pop(0)

        with (
            patch("app._async_is_codex_running", side_effect=fake_is_running),
            patch("app._send_session_owner_environment", return_value=True),
            patch("app._exact_tmux_session_id", return_value="$1"),
            patch("app.subprocess.run", return_value=MagicMock(returncode=0)),
            patch("app._find_session_transcript_uuid", return_value=None),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
            patch.object(_app._session_lifecycle, "get", return_value={}),
            patch("app._session_launch_base", return_value="codex --yolo"),
            patch(
                "app._saved_session_model_effort",
                return_value=("gpt-5.6-sol", "high"),
            ),
            patch("app._session_launch_command", return_value="pinned-launch") as launch,
        ):
            result = await _app._ensure_codex_running("my-session")

        assert result is True
        launch.assert_called_once_with(
            "my-session",
            "codex --yolo",
            expected_owner_id=None,
            pin_model=False,
            resume=True,
            model="gpt-5.6-sol",
            effort="high",
        )

    @pytest.mark.asyncio
    async def test_explicit_fresh_restart_launches_without_resume(self, tmp_path):
        """A confirmed settings restart may relaunch a verified unused tab."""
        import app as _app

        generation = "a" * 32
        row = {
            "managed": True,
            "generation": generation,
            "owner_id": "admin",
            "desired_state": "running",
            "restore_on_startup": True,
            "cwd": str(tmp_path),
            "had_conversation_input": False,
        }
        is_running_values = [False, True]

        async def fake_is_running(_session_name):
            return is_running_values.pop(0)

        with (
            patch("app._async_is_codex_running", side_effect=fake_is_running),
            patch(
                "app._strict_session_owner",
                return_value=("admin", {"id": "admin", "username": "admin"}),
            ),
            patch.object(_app._session_lifecycle, "get", return_value=row),
            patch.object(_app._session_lifecycle, "matches", return_value=True),
            patch("app._durable_session_cwd", return_value=str(tmp_path)),
            patch("app._tmux_session_matches_owner", return_value=True),
            patch("app._send_session_owner_environment", return_value=True),
            patch("app._ensure_codex_auth_with_fallback"),
            patch("app._uses_private_account_runtime", return_value=False),
            patch(
                "app._find_session_transcript_uuid",
                return_value="01a035f8-3188-7c21-8cca-582b01ad3002",
            ) as find_transcript,
            patch("app._session_launch_base", return_value="codex --yolo"),
            patch(
                "app._saved_session_model_effort",
                return_value=("gpt-6-astra", "xhigh"),
            ),
            patch("app._exact_tmux_session_id", return_value="$1"),
            patch("app.subprocess.run", return_value=MagicMock(returncode=0)),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
            patch("app._session_launch_command", return_value="fresh-launch") as launch,
        ):
            result = await _app._ensure_codex_running(
                "my-session",
                expected_owner_id="admin",
                expected_generation=generation,
                expected_desired_states={"running"},
                allow_fresh=True,
            )

        assert result is True
        find_transcript.assert_not_called()
        launch.assert_called_once_with(
            "my-session",
            "codex --yolo",
            expected_owner_id="admin",
            pin_model=False,
            resume=False,
            model="gpt-6-astra",
            effort="xhigh",
        )

    @pytest.mark.asyncio
    async def test_recovery_forwards_recorded_thread_id_and_cwd(self):
        """Production recovery must use the root recorded for the dashboard tab."""
        import app as _app

        thread_id = "01a020d4-d4e0-75a3-b832-b830e6f4fd87"
        is_running_values = [False, True]

        async def fake_is_running(session_name):
            return is_running_values.pop(0)

        with (
            patch("app._async_is_codex_running", side_effect=fake_is_running),
            patch("app._send_session_owner_environment", return_value=True),
            patch("app._exact_tmux_session_id", return_value="$1"),
            patch("app.subprocess.run", return_value=MagicMock(returncode=0)),
            patch("app.asyncio.sleep", new_callable=AsyncMock),
            patch("app._session_launch_base", return_value="codex --yolo"),
            patch("app._find_session_transcript_uuid", return_value=thread_id),
            patch(
                "app._saved_session_model_effort",
                return_value=("gpt-5.6-sol", "high"),
            ),
            patch("app.get_session_cwd", return_value="/workspace/recovered"),
            patch("app._session_launch_command", return_value="pinned-launch") as launch,
        ):
            result = await _app._ensure_codex_running("my-session")

        assert result is True
        launch.assert_called_once_with(
            "my-session",
            "codex --yolo",
            expected_owner_id=None,
            pin_model=False,
            resume=True,
            model="gpt-5.6-sol",
            effort="high",
            resume_uuid=thread_id,
            resume_cwd="/workspace/recovered",
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


def test_restore_virtual_session_uses_exact_tmux_targets(tmp_path):
    """A missing `debug` session must not accidentally target `debugtmux`."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        result = MagicMock()
        result.returncode = 1 if args[1] == "has-session" else 0
        result.stderr = ""
        if args[1] == "new-session":
            result.stdout = "$9\tdebug\n"
        elif args[1] == "display-message":
            result.stdout = "$9\tdebug\n"
        else:
            result.stdout = ""
        return result

    with (
        patch(
            "app._user_for_session",
            return_value={"id": "u_michiel", "username": "Michiel"},
        ),
        patch("app._uses_private_account_runtime", return_value=False),
        patch("app.subprocess.run", side_effect=fake_run),
    ):
        restored = app_module._restore_parked_tmux_shell(
            "debug", {"cwd": str(tmp_path), "virtual": True}
        )

    assert restored is True
    assert ["tmux", "has-session", "-t", "=debug"] in calls
    new_session = next(call for call in calls if call[1] == "new-session")
    assert [";", "set-option", app_module._TMUX_QUARANTINED_OPTION, "1"] == (
        new_session[new_session.index(";"):new_session.index(";") + 4]
    )
    assert any(
        call[:5] == ["tmux", "set-option", "-pt", "$9:", "remain-on-exit"]
        for call in calls
    )
    assert all(
        call[3] == "$9:"
        for call in calls
        if len(call) > 3 and call[0:2] == ["tmux", "send-keys"]
    )


def test_restore_mapped_session_rejects_missing_cwd_before_tmux(tmp_path):
    run = MagicMock()
    with (
        patch(
            "app._user_for_session",
            return_value={"id": "u_michiel", "username": "Michiel"},
        ),
        patch("app.subprocess.run", run),
    ):
        restored = app_module._restore_parked_tmux_shell(
            "debug",
            {
                "cwd": str(tmp_path / "missing"),
                "virtual": True,
                "resume_uuid": "01a035f8-3188-7c21-8cca-582b01ad3002",
            },
        )

    assert restored is False
    run.assert_not_called()


@pytest.mark.asyncio
async def test_park_session_uses_exact_pane_targets(tmp_path):
    calls = []
    events = []
    lifecycle = {
        "managed": True,
        "generation": "a" * 32,
        "owner_id": "u_michiel",
        "desired_state": "running",
        "resume_uuid": "01a035f8-3188-7c21-8cca-582b01ad3002",
    }

    def record_run(args, **kwargs):
        calls.append(args)
        events.append(("run", tuple(args)))
        return MagicMock(returncode=0, stdout="", stderr="")

    async def record_sleep(duration):
        events.append(("sleep", duration))

    with (
        patch("app.async_detect_activity", new=AsyncMock(return_value={"status": "idle"})),
        patch("app._session_has_autonomous_work", return_value=False),
        patch(
            "app._strict_session_owner",
            return_value=("u_michiel", {"id": "u_michiel"}),
        ),
        patch("app._archive_tmux_scrollback", return_value=""),
        patch("app.get_session_cwd", return_value=str(tmp_path)),
        patch("app._checkpoint_active_session", return_value=lifecycle),
        patch(
            "app._validated_session_root_thread_id",
            return_value=lifecycle["resume_uuid"],
        ),
        patch(
            "app._active_session_root_thread_id",
            return_value=lifecycle["resume_uuid"],
        ),
        patch("app._durable_session_cwd", return_value=str(tmp_path.resolve())),
        patch("app._exact_tmux_session_id", return_value="$7"),
        patch("app._tmux_session_matches_owner", return_value=True),
        patch(
            "app._async_is_codex_running",
            new=AsyncMock(side_effect=[True, False, False, False]),
        ),
        patch("app.subprocess.run", side_effect=record_run),
        patch("app.asyncio.sleep", side_effect=record_sleep),
        patch.object(
            app_module._session_lifecycle,
            "get",
            return_value=lifecycle,
        ),
        patch.object(
            app_module._session_lifecycle,
            "mark_parked",
            return_value={"parked": True},
        ),
        patch.object(
            app_module._session_lifecycle,
            "begin_transition",
            return_value={"desired_state": "parking"},
        ),
    ):
        result = await app_module._park_session_local("debug", 123.0)

    assert result["ok"] is True
    targeted = [
        call for call in calls
        if call[:2] == ["tmux", "send-keys"]
        or call[:2] == ["tmux", "set-option"]
    ]
    assert targeted
    assert all(call[3] == "$7:" for call in targeted)
    quit_index = next(
        index
        for index, event in enumerate(events)
        if event[0] == "run" and event[1][-2:] == ("-l", "/quit")
    )
    enter_index = next(
        index
        for index, event in enumerate(events[quit_index + 1 :], quit_index + 1)
        if event[0] == "run" and event[1][-1] == "Enter"
    )
    assert ("sleep", 1.0) in events[quit_index + 1 : enter_index]


# ─── Away Mode Toggle (enable/disable paths) ───


@pytest.fixture
def owned_autopush_runtime(tmp_path, monkeypatch):
    """Autonomous behavior tests start with valid, isolated mode and owner state."""
    path = tmp_path / "autopush-mode.json"
    store = app_module.LockedJsonStore(path, lambda: {})
    store.update(lambda data: data.clear())
    monkeypatch.setattr(app_module, "AUTOPUSH_MODE_FILE", path)
    monkeypatch.setattr(app_module, "_autopush_mode_store", store)
    monkeypatch.setattr(app_module, "_autopush_mode", {})
    monkeypatch.setattr(app_module, "_autopush_mode_state_valid", True)
    monkeypatch.setattr(app_module, "_autopush_mode_file_stamp", app_module._autopush_file_stamp())
    monkeypatch.setattr(app_module, "_autopush_action_locks", {})
    monkeypatch.setattr(app_module, "_autonomous_toggle_revision", {})
    monkeypatch.setattr(app_module, "_away_mode_state", {})
    monkeypatch.setattr(app_module, "_go_nuts_state", {})

    def register(name, state=None, mode="away"):
        app_module._set_session_owner(name, "admin")
        if state is not None:
            state.update(owner_id="admin", generation="")
            registry = app_module._away_mode_state if mode == "away" else app_module._go_nuts_state
            registry[name] = state
        return app_module._terminal_binding(name, "admin")

    async def terminal_binding(name, expected_owner_id=""):
        owner_id = app_module._session_owner_id(name)
        if not owner_id or (expected_owner_id and expected_owner_id != owner_id):
            return None
        return app_module._terminal_binding(name, owner_id)

    monkeypatch.setattr(app_module, "_autopush_terminal_binding", terminal_binding)
    return register


@pytest.mark.usefixtures("owned_autopush_runtime")
class TestAwayModeToggleEnable:
    """Cover lines 3617-3649: api_away_mode_toggle enable/disable with valid session."""

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._save_autonomous_state")
    def test_enable_fresh_creates_task_and_returns_summary(
        self, mock_save, mock_sessions, authed_client
    ):
        """Lines 3622-3639: enabling away mode on a session initialises state and creates task."""
        import app as _app

        _app._away_mode_state.pop("test-session", None)

        async def instant_worker(session_name):
            pass  # Completes immediately, no leaked coroutine

        try:
            with patch("app._away_mode_worker", instant_worker), \
                 patch("app._resume_parked_session", new=AsyncMock(return_value={"ok": True})):
                resp = authed_client.post(
                    "/api/sessions/test-session/away-mode", json={"enabled": True}
                )
        finally:
            _app._away_mode_state.pop("test-session", None)
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        mock_save.assert_called()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._save_autonomous_state")
    def test_enable_when_already_running_returns_current_state(
        self, mock_save, mock_sessions, authed_client
    ):
        """Line 3619-3620: if away mode is already enabled, returns existing state immediately."""
        import app as _app

        _app._away_mode_state["test-session"] = {
            "enabled": True,
            "phase": 2,
            "phase_name": "Running",
            "step": 3,
            "step_name": "work",
            "started_at": 0.0,
            "log": [],
            "report": "",
            "task": None,
        }
        try:
            resp = authed_client.post(
                "/api/sessions/test-session/away-mode", json={"enabled": True}
            )
        finally:
            _app._away_mode_state.pop("test-session", None)
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True
        # _save_autonomous_state should NOT be called (early return before it)
        mock_save.assert_not_called()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._save_autonomous_state")
    def test_disable_cancels_running_task(
        self, mock_save, mock_sessions, authed_client
    ):
        """Lines 3640-3649: disable cancels the active task and returns disabled state."""
        import app as _app

        mock_task = MagicMock()
        mock_task.done.return_value = False
        _app._away_mode_state["test-session"] = {
            "enabled": True,
            "phase": 1,
            "phase_name": "Running",
            "step": 1,
            "step_name": "",
            "started_at": 0.0,
            "log": [],
            "report": "",
            "task": mock_task,
        }
        try:
            resp = authed_client.post(
                "/api/sessions/test-session/away-mode", json={"enabled": False}
            )
        finally:
            _app._away_mode_state.pop("test-session", None)
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        mock_task.cancel.assert_called_once()
        mock_save.assert_called()


# ─── Go Nuts Mode Toggle (enable/disable paths) ───


@pytest.mark.usefixtures("owned_autopush_runtime")
class TestGoNutsModeToggleEnable:
    """Cover lines 3966-3999: api_go_nuts_mode_toggle enable/disable with valid session."""

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._save_autonomous_state")
    def test_enable_fresh_creates_task(
        self, mock_save, mock_sessions, authed_client
    ):
        """Lines 3974-3990: enabling go-nuts mode on a clean session creates task."""
        import app as _app

        _app._away_mode_state.pop("test-session", None)
        _app._go_nuts_state.pop("test-session", None)

        async def instant_worker(session_name):
            pass  # Completes immediately, no leaked coroutine

        try:
            with patch("app._go_nuts_mode_worker", instant_worker), \
                 patch("app._resume_parked_session", new=AsyncMock(return_value={"ok": True})):
                resp = authed_client.post(
                    "/api/sessions/test-session/go-nuts-mode", json={"enabled": True}
                )
        finally:
            _app._go_nuts_state.pop("test-session", None)
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is True

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    def test_enable_conflicts_with_away_mode_returns_409(
        self, mock_sessions, authed_client
    ):
        """Line 3968-3969: enabling go-nuts when away mode is active returns 409."""
        import app as _app

        _app._away_mode_state["test-session"] = {
            "enabled": True, "phase": 1, "phase_name": "Running",
            "step": 1, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        try:
            resp = authed_client.post(
                "/api/sessions/test-session/go-nuts-mode", json={"enabled": True}
            )
        finally:
            _app._away_mode_state.pop("test-session", None)
        assert resp.status_code == 409
        assert "error" in resp.json()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._save_autonomous_state")
    def test_enable_when_already_running_returns_current_state(
        self, mock_save, mock_sessions, authed_client
    ):
        """Line 3971-3972: if go-nuts already enabled, returns existing state."""
        import app as _app

        _app._go_nuts_state["test-session"] = {
            "enabled": True, "phase": 3, "phase_name": "Building",
            "step": 5, "step_name": "features", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        try:
            resp = authed_client.post(
                "/api/sessions/test-session/go-nuts-mode", json={"enabled": True}
            )
        finally:
            _app._go_nuts_state.pop("test-session", None)
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True
        mock_save.assert_not_called()

    @patch("app.get_tmux_sessions", return_value=MOCK_SESSIONS)
    @patch("app._save_autonomous_state")
    def test_disable_cancels_running_task(
        self, mock_save, mock_sessions, authed_client
    ):
        """Lines 3991-3999: disable cancels the task and returns disabled state."""
        import app as _app

        mock_task = MagicMock()
        mock_task.done.return_value = False
        _app._go_nuts_state["test-session"] = {
            "enabled": True, "phase": 2, "phase_name": "Running",
            "step": 1, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": mock_task,
        }
        try:
            resp = authed_client.post(
                "/api/sessions/test-session/go-nuts-mode", json={"enabled": False}
            )
        finally:
            _app._go_nuts_state.pop("test-session", None)
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False
        mock_task.cancel.assert_called_once()


# ─── Away Phase Functions ───


class TestAwayPhaseFunctions:
    """Cover lines 3407-3443: _away_phase_study, _away_phase_select, _away_phase_execute."""

    @pytest.mark.asyncio
    async def test_phase_study_sets_state_and_sends(self):
        """Lines 3407-3412: _away_phase_study sets phase/step and calls _away_send_and_wait."""
        import app as _app

        calls = []

        async def capture(*args, **kwargs):
            calls.append(args)

        state = {"enabled": True, "phase": 0, "step": 0, "log": []}
        with patch("app._away_send_and_wait", capture):
            await _app._away_phase_study("my-session", state)
        assert state["phase"] == 1
        assert state["step"] == 1
        assert len(calls) == 1
        assert calls[0][0] == "my-session"

    @pytest.mark.asyncio
    async def test_phase_select_sets_state_and_sends(self):
        """Lines 3417-3422: _away_phase_select sets phase/step and calls _away_send_and_wait."""
        import app as _app

        calls = []

        async def capture(*args, **kwargs):
            calls.append(args)

        state = {"enabled": True, "phase": 1, "step": 0, "log": []}
        with patch("app._away_send_and_wait", capture):
            await _app._away_phase_select("my-session", state)
        assert state["phase"] == 2
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_phase_execute_iterates_rounds(self):
        """Lines 3427-3443: _away_phase_execute runs 3 rounds and sleeps between them."""
        import app as _app

        calls = []

        async def capture(*args, **kwargs):
            calls.append(args)

        async def noop_sleep(_secs):
            pass

        state = {"enabled": True, "phase": 2, "step": 0, "log": []}
        with patch("app._away_send_and_wait", capture), \
             patch("app.asyncio.sleep", noop_sleep):
            await _app._away_phase_execute("my-session", state)
        assert state["phase"] == 3
        assert len(calls) == 3  # One call per round

    @pytest.mark.asyncio
    async def test_phase_execute_stops_when_disabled(self):
        """Line 3438-3439: _away_phase_execute exits early if state disabled mid-loop."""
        import app as _app

        call_count = 0

        # Use a plain async def (not a Mock) to avoid AsyncMock internal coroutine leaks
        async def fake_send(session_name, prompt, state, step_name, timeout=900):
            nonlocal call_count
            call_count += 1
            state["enabled"] = False  # Disable after first round

        async def noop_sleep(_secs):
            pass

        state = {"enabled": True, "phase": 2, "step": 0, "log": []}
        with patch("app._away_send_and_wait", fake_send), \
             patch("app.asyncio.sleep", noop_sleep):
            await _app._away_phase_execute("my-session", state)
        assert call_count == 1  # Stopped after first round


# ─── Go Nuts Phase Functions ───


class TestGoNutsPhaseFunctions:
    """Cover lines 3817-3841: _go_nuts_phase_discover, _go_nuts_phase_backlog, _go_nuts_phase_build."""

    @pytest.mark.asyncio
    async def test_phase_discover_sets_state_and_sends(self):
        """Lines 3817-3822: _go_nuts_phase_discover sets phase/step and calls send."""
        import app as _app

        calls = []

        async def capture(*args, **kwargs):
            calls.append(args)

        state = {"enabled": True, "phase": 0, "step": 0, "log": []}
        with patch("app._go_nuts_send_and_wait", capture):
            await _app._go_nuts_phase_discover("gn-session", state)
        assert state["phase"] == 1
        assert state["step"] == 1
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_phase_backlog_sets_phase_2(self):
        """Lines 3827-3832: _go_nuts_phase_backlog sets phase 2."""
        import app as _app

        calls = []

        async def capture(*args, **kwargs):
            calls.append(args)

        state = {"enabled": True, "phase": 1, "step": 0, "log": []}
        with patch("app._go_nuts_send_and_wait", capture):
            await _app._go_nuts_phase_backlog("gn-session", state)
        assert state["phase"] == 2
        assert len(calls) == 1

    @pytest.mark.asyncio
    async def test_phase_build_sets_phase_3(self):
        """Lines 3837-3842: _go_nuts_phase_build sets phase 3."""
        import app as _app

        calls = []

        async def capture(*args, **kwargs):
            calls.append(args)

        state = {"enabled": True, "phase": 2, "step": 0, "log": []}
        with patch("app._go_nuts_send_and_wait", capture):
            await _app._go_nuts_phase_build("gn-session", state)
        assert state["phase"] == 3
        assert len(calls) == 1


# ─── _away_wait_for_idle ───


class TestAwayWaitForIdle:
    """Cover lines 3206-3234: _away_wait_for_idle."""

    def _make_time_mock(self, values, default=None):
        """Return a callable that yields from values then repeats the last value."""
        import itertools
        if default is None:
            default = values[-1]
        seq = iter(itertools.chain(values, itertools.repeat(default)))
        return lambda: next(seq)

    @pytest.mark.asyncio
    async def test_becomes_busy_then_idle_returns_true(self):
        """Normal path: session becomes busy then returns to idle."""
        import app as _app

        # time.time() calls: start=0, phA-cond1=0 (enter), phB-cond1=1, phB-cond2=2
        activity_vals = [
            {"status": "busy"},   # Phase A: session is busy → break
            {"status": "idle"},   # Phase B: idle count 1
            {"status": "idle"},   # Phase B: idle count 2 → return True
        ]
        act_idx = [0]

        async def mock_activity(session_name):
            v = activity_vals[act_idx[0]]
            act_idx[0] += 1
            return v

        async def noop_sleep(_secs):
            pass

        with patch("app.time.time", side_effect=self._make_time_mock([0, 0, 1, 2])), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity):
            result = await _app._away_wait_for_idle("sess")
        assert result is True

    @pytest.mark.asyncio
    async def test_never_becomes_busy_but_idles_returns_true(self):
        """Session never becomes busy (Phase A times out), but Phase B detects idle."""
        import app as _app

        # time: start=0, phA-cond=0 (enter), phA-cond=31 (exit), phB-cond=32 (enter), phB-cond=33 (enter)
        activity_vals = [
            {"status": "idle"},  # Phase A: never busy
            {"status": "idle"},  # Phase B: idle count 1
            {"status": "idle"},  # Phase B: idle count 2 → return True
        ]
        act_idx = [0]

        async def mock_activity(session_name):
            v = activity_vals[act_idx[0]]
            act_idx[0] += 1
            return v

        async def noop_sleep(_secs):
            pass

        with patch("app.time.time", side_effect=self._make_time_mock([0, 0, 31, 32, 33])), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity):
            result = await _app._away_wait_for_idle("sess")
        assert result is True

    @pytest.mark.asyncio
    async def test_phase_b_resets_idle_count_and_times_out(self):
        """Phase B resets idle_count when session is non-idle and eventually returns False."""
        import app as _app

        # time: start=0, phA-cond1=0, phA-cond2=31, phB×4 within timeout, then exceed timeout
        activity_vals = [
            {"status": "idle"},   # Phase A: not busy
            {"status": "idle"},   # Phase B: idle count 1
            {"status": "busy"},   # Phase B: reset → idle_count=0
            {"status": "idle"},   # Phase B: idle count 1 again
            # loop exits via timeout after this
        ]
        act_idx = [0]

        async def mock_activity(session_name):
            v = activity_vals[act_idx[0]]
            act_idx[0] += 1
            return v

        async def noop_sleep(_secs):
            pass

        # Enough time values: start, phA×2, phB×4, then high value to exit phB
        with patch("app.time.time", side_effect=self._make_time_mock(
                [0, 0, 31, 32, 33, 34, 35, 950], default=950)), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity):
            result = await _app._away_wait_for_idle("sess", timeout=900)
        assert result is False  # Timed out before reaching idle_count=2


# ─── _restore_autonomous_mode ───


class TestRestoreAutonomousMode:
    """Cover lines 2664-2723: _restore_autonomous_mode."""

    @pytest.fixture(autouse=True)
    def owned_restore_session(self, owned_autopush_runtime):
        owned_autopush_runtime("my-sess")

    @pytest.mark.asyncio
    async def test_restore_away_mode_success(self, owned_autopush_runtime):
        """Normal restore: session exists, idle, sends prompt, enters continuous loop."""
        import app as _app

        loop_called = []

        async def noop_sleep(_secs):
            pass

        async def mock_activity(session_name):
            return {"status": "idle"}

        async def mock_ensure_codex(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_owner_id"] == "admin"
            return True

        async def mock_send_prompt(session_name, prompt, *, expected_binding=None):
            assert expected_binding["owner_id"] == "admin"

        async def mock_continuous_loop(session_name):
            loop_called.append(session_name)

        state = {"enabled": True, "phase": 4, "step": 0, "log": []}
        owned_autopush_runtime("my-sess", state)
        with patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app._ensure_codex_running", mock_ensure_codex), \
             patch("app._away_send_prompt", mock_send_prompt), \
             patch("app._away_mode_continuous_loop", mock_continuous_loop):
            await _app._restore_autonomous_mode("my-sess", state, "away")
        assert loop_called == ["my-sess"]
        assert state["task"] is None  # Cleared in finally

    @pytest.mark.asyncio
    async def test_restore_disabled_before_send_stops_early(self):
        """If state disabled during restore sleep, exits without sending prompt."""
        import app as _app

        loop_called = []

        async def disabling_sleep(_secs):
            state["enabled"] = False  # Disable during the initial 15s sleep

        async def mock_activity(session_name):
            return {"status": "idle"}

        async def mock_continuous_loop(session_name):
            loop_called.append(session_name)

        state = {"enabled": True, "phase": 0, "step": 0, "log": []}
        with patch("app.asyncio.sleep", disabling_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app._away_mode_continuous_loop", mock_continuous_loop):
            await _app._restore_autonomous_mode("my-sess", state, "away")
        assert loop_called == []  # Should not have entered loop

    @pytest.mark.asyncio
    async def test_restore_session_not_found_stops(self):
        """If async_detect_activity raises (session gone), restore stops."""
        import app as _app

        async def noop_sleep(_secs):
            pass

        async def failing_activity(session_name):
            raise RuntimeError("session not found")

        state = {"enabled": True, "phase": 0, "step": 0, "log": []}
        with patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", failing_activity), \
             patch("app._save_autonomous_state"):
            await _app._restore_autonomous_mode("my-sess", state, "away")
        assert state["enabled"] is False

    @pytest.mark.asyncio
    async def test_restore_go_nuts_mode_uses_gonuts_loop(self, owned_autopush_runtime):
        """Restore for go-nuts mode enters _go_nuts_continuous_loop."""
        import app as _app

        loop_called = []

        async def noop_sleep(_secs):
            pass

        async def mock_activity(session_name):
            return {"status": "idle"}

        async def mock_ensure_codex(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_owner_id"] == "admin"
            return True

        async def mock_send_prompt(session_name, prompt, *, expected_binding=None):
            assert expected_binding["owner_id"] == "admin"

        async def mock_gonuts_loop(session_name):
            loop_called.append(("gonuts", session_name))

        state = {"enabled": True, "phase": 2, "step": 0, "log": []}
        owned_autopush_runtime("my-sess", state, mode="gonuts")
        with patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app._ensure_codex_running", mock_ensure_codex), \
             patch("app._away_send_prompt", mock_send_prompt), \
             patch("app._go_nuts_continuous_loop", mock_gonuts_loop):
            await _app._restore_autonomous_mode("my-sess", state, "gonuts")
        assert loop_called == [("gonuts", "my-sess")]


# ─── _watchdog_loop (no-active-sessions path) ───


class TestWatchdogLoop:
    """Cover lines 2728-2770: _watchdog_loop no-active-sessions and zombie detection."""

    @pytest.mark.asyncio
    async def test_no_active_sessions_clears_snapshots(self):
        """Lines 2742-2745: when no active sessions, snapshots are cleared then loop runs once."""
        import asyncio as _asyncio

        import app as _app

        sleep_count = 0

        async def counting_sleep(secs):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise _asyncio.CancelledError()

        _app._watchdog_snapshots["stale"] = {"content_hash": "abc", "first_seen": 0}

        try:
            with patch("app.asyncio.sleep", counting_sleep):
                try:
                    await _app._watchdog_loop()
                except _asyncio.CancelledError:
                    pass
        finally:
            _app._watchdog_snapshots.pop("stale", None)

        # Snapshots cleared when no active sessions
        assert "stale" not in _app._watchdog_snapshots

    @pytest.mark.asyncio
    async def test_zombie_away_mode_detected_and_restarted(self):
        """Lines 2756-2759: zombie away mode (enabled but task done) triggers restart.

        Zombie detection only runs when active_sessions is non-empty (the watchdog
        'continue' skips it when there are zero live sessions), so we need both a
        live session and a zombie session.
        """
        import asyncio as _asyncio

        import app as _app

        sleep_count = 0
        restart_calls = []

        async def counting_sleep(secs):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise _asyncio.CancelledError()

        async def mock_restart(name, state, mode, wlog):
            restart_calls.append((name, mode))

        async def mock_check_session(session_name, state, mode, wlog):
            pass  # No-op: don't try to capture terminal content

        live_task = MagicMock()
        live_task.done.return_value = False  # Active session
        dead_task = MagicMock()
        dead_task.done.return_value = True   # Zombie session (task finished)

        live_state = {"enabled": True, "phase": 1, "step": 0, "log": [], "task": live_task}
        zombie_state = {"enabled": True, "phase": 1, "step": 0, "log": [], "task": dead_task}

        _app._away_mode_state["live-sess"] = live_state
        _app._away_mode_state["zombie-sess"] = zombie_state
        try:
            with patch("app.asyncio.sleep", counting_sleep), \
                 patch("app._watchdog_restart_mode", mock_restart), \
                 patch("app._watchdog_check_session", mock_check_session):
                try:
                    await _app._watchdog_loop()
                except _asyncio.CancelledError:
                    pass
        finally:
            _app._away_mode_state.pop("live-sess", None)
            _app._away_mode_state.pop("zombie-sess", None)

        assert any(name == "zombie-sess" and mode == "away"
                   for name, mode in restart_calls)


# ─── _away_send_prompt ───


class TestAwaySendPrompt:
    """Cover lines 3107-3201: _away_send_prompt."""

    @pytest.fixture(autouse=True)
    def owned_prompt_session(self, owned_autopush_runtime):
        owned_autopush_runtime("test-sess")

    @pytest.mark.asyncio
    async def test_busy_detected_returns_early(self):
        """Lines 3160-3162: if session is busy after paste, returns immediately."""
        import app as _app

        async def mock_to_thread(fn, *args, **kwargs):
            if fn is _app._terminal_binding_state:
                return fn(*args, **kwargs)
            if fn is _app.capture_pane_recent:
                return "snapshot"
            return MagicMock(returncode=0, stdout="", stderr="")

        async def mock_activity(session_name):
            return {"status": "busy"}

        async def noop_sleep(_secs):
            pass

        with patch("app.asyncio.to_thread", mock_to_thread), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app.tempfile.mkstemp", return_value=(0, "/tmp/test-prompt.md")), \
             patch("app.os.close"), \
             patch("app.os.unlink"):
            await _app._away_send_prompt("test-sess", "hello prompt")
        # If we reach here without exception, the function returned successfully

    @pytest.mark.asyncio
    async def test_terminal_changed_detected(self):
        """Lines 3165-3168: if pre/post snapshot differs, returns without retry."""
        import app as _app

        snapshot_count = [0]

        async def mock_to_thread(fn, *args, **kwargs):
            if fn is _app._terminal_binding_state:
                return fn(*args, **kwargs)
            if fn is _app.capture_pane_recent:
                snapshot_count[0] += 1
                # pre-snapshot returns "before", post-snapshot returns "after"
                return "before" if snapshot_count[0] == 1 else "after"
            return MagicMock(returncode=0, stdout="", stderr="")

        async def mock_activity(session_name):
            return {"status": "idle"}  # Not busy — will check snapshot diff

        async def noop_sleep(_secs):
            pass

        with patch("app.asyncio.to_thread", mock_to_thread), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app.tempfile.mkstemp", return_value=(0, "/tmp/test-prompt.md")), \
             patch("app.os.close"), \
             patch("app.os.unlink"):
            await _app._away_send_prompt("test-sess", "hello prompt")
        # Terminal content changed → function returned early (line 3168)

    @pytest.mark.asyncio
    async def test_all_retries_fail_logs_and_returns(self):
        """Lines 3170-3175: all 3 Enter retries fail (same content), function returns."""
        import app as _app

        async def mock_to_thread(fn, *args, **kwargs):
            if fn is _app._terminal_binding_state:
                return fn(*args, **kwargs)
            if fn is _app.capture_pane_recent:
                return "same content"  # Never changes
            return MagicMock(returncode=0, stdout="", stderr="")

        async def mock_activity(session_name):
            return {"status": "idle"}  # Always idle

        async def noop_sleep(_secs):
            pass

        with patch("app.asyncio.to_thread", mock_to_thread), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app.tempfile.mkstemp", return_value=(0, "/tmp/test-prompt.md")), \
             patch("app.os.close"), \
             patch("app.os.unlink"):
            await _app._away_send_prompt("test-sess", "hello " * 200)
        # All retries failed, "Pasted text" not detected, function returns normally

    @pytest.mark.asyncio
    async def test_stuck_paste_preview_cleared(self):
        """Lines 3179-3191: 'Pasted text' in recent output triggers Escape + resend."""
        import app as _app

        call_count = [0]
        tmux_commands = []

        async def mock_to_thread(fn, *args, **kwargs):
            if fn is _app._terminal_binding_state:
                return fn(*args, **kwargs)
            if fn is _app.capture_pane_recent:
                call_count[0] += 1
                # Pre-snapshot (call 1) and post-snapshot (calls 2,3,4) and recent check (call 5)
                if call_count[0] == 5:
                    return "Pasted text +10 lines in buffer"  # Stuck paste!
                return "same"
            if fn is _app.subprocess.run and args:
                tmux_commands.append(args[0])
            return MagicMock(returncode=0, stdout="", stderr="")

        async def mock_activity(session_name):
            return {"status": "idle"}

        async def noop_sleep(_secs):
            pass

        with patch("app.asyncio.to_thread", mock_to_thread), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app.tempfile.mkstemp", return_value=(0, "/tmp/test-prompt.md")), \
             patch("app.os.close"), \
             patch("app.os.unlink"):
            await _app._away_send_prompt("test-sess", "hello prompt")
        assert any("C-c" in command for command in tmux_commands)

    @pytest.mark.asyncio
    async def test_activity_exception_treated_as_unknown(self):
        """Lines 3157-3158: when async_detect_activity raises, activity = unknown."""
        import app as _app

        async def mock_to_thread(fn, *args, **kwargs):
            if fn is _app._terminal_binding_state:
                return fn(*args, **kwargs)
            if fn is _app.capture_pane_recent:
                return "snapshot"
            return MagicMock(returncode=0, stdout="", stderr="")

        async def failing_activity(session_name):
            raise RuntimeError("tmux connection lost")

        async def noop_sleep(_secs):
            pass

        # If unknown status → not busy → snapshot check → same → retry loop × 3
        # Function should return without error despite activity failure
        with patch("app.asyncio.to_thread", mock_to_thread), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", failing_activity), \
             patch("app.tempfile.mkstemp", return_value=(0, "/tmp/test-prompt.md")), \
             patch("app.os.close"), \
             patch("app.os.unlink"):
            await _app._away_send_prompt("test-sess", "hello prompt")
        # Function completes normally (3 retries attempted, no crash)

    @pytest.mark.asyncio
    async def test_exception_during_paste_logged(self):
        """Lines 3193-3200: exception during send is caught and logged, finally cleans up."""
        import app as _app

        async def failing_to_thread(fn, *args, **kwargs):
            raise OSError("tmux not found")

        async def noop_sleep(_secs):
            pass

        with patch("app.asyncio.to_thread", failing_to_thread), \
             patch("app.asyncio.sleep", noop_sleep), \
             patch("app.tempfile.mkstemp", return_value=(0, "/tmp/test-prompt.md")), \
             patch("app.os.close"), \
             patch("app.os.unlink"):
            # Should not raise — exception caught internally
            await _app._away_send_prompt("test-sess", "hello prompt")


# ─── _restore_autonomous_mode additional paths ───


class TestRestoreAutonomousModeExtra:
    """Cover lines 2686-2698, 2713-2720: busy-wait path and claude-dead path in restore."""

    @pytest.fixture(autouse=True)
    def owned_restore_sessions(self, owned_autopush_runtime):
        owned_autopush_runtime("busy-sess")
        owned_autopush_runtime("dead-sess")

    @pytest.mark.asyncio
    async def test_restore_waits_when_busy(self, owned_autopush_runtime):
        """Lines 2686-2687: if session is busy on restore, wait for idle before sending."""
        import app as _app

        wait_called = []

        async def noop_sleep(_secs):
            pass

        async def mock_activity(session_name):
            return {"status": "busy"}  # Session is busy when restoring

        async def mock_wait_idle(session_name, timeout=900):
            wait_called.append(session_name)

        async def mock_ensure_claude(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_owner_id"] == "admin"
            return True

        async def mock_send_prompt(session_name, prompt, *, expected_binding=None):
            assert expected_binding["owner_id"] == "admin"

        async def mock_loop(session_name):
            pass

        state = {"enabled": True, "phase": 4, "step": 0, "log": []}
        owned_autopush_runtime("busy-sess", state)
        with patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app._away_wait_for_idle", mock_wait_idle), \
             patch("app._ensure_codex_running", mock_ensure_claude), \
             patch("app._away_send_prompt", mock_send_prompt), \
             patch("app._away_mode_continuous_loop", mock_loop):
            await _app._restore_autonomous_mode("busy-sess", state, "away")
        assert wait_called == ["busy-sess"]  # _away_wait_for_idle was called

    @pytest.mark.asyncio
    async def test_restore_stops_when_claude_dead(self):
        """Lines 2694-2698: if Codex can't be restarted, restore stops."""
        import app as _app

        loop_called = []

        async def noop_sleep(_secs):
            pass

        async def mock_activity(session_name):
            return {"status": "idle"}

        async def mock_ensure_dead(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_owner_id"] == "admin"
            return False  # Codex couldn't restart

        async def mock_loop(session_name):
            loop_called.append(session_name)

        state = {"enabled": True, "phase": 4, "step": 0, "log": []}
        with patch("app.asyncio.sleep", noop_sleep), \
             patch("app.async_detect_activity", mock_activity), \
             patch("app._ensure_codex_running", mock_ensure_dead), \
             patch("app._away_mode_continuous_loop", mock_loop), \
             patch("app._save_autonomous_state"):
            await _app._restore_autonomous_mode("dead-sess", state, "away")
        assert loop_called == []  # Loop NOT entered
        assert state["enabled"] is False  # Disabled


# ─── Watchdog go-nuts zombie detection ───


class TestWatchdogGoNutsZombie:
    """Cover lines 2760-2762: zombie go-nuts mode detection in _watchdog_loop."""

    @pytest.mark.asyncio
    async def test_zombie_go_nuts_detected_and_restarted(self):
        """Lines 2760-2762: zombie go-nuts (enabled but task done) triggers restart."""
        import asyncio as _asyncio

        import app as _app

        sleep_count = 0
        restart_calls = []

        async def counting_sleep(secs):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count >= 2:
                raise _asyncio.CancelledError()

        async def mock_restart(name, state, mode, wlog):
            restart_calls.append((name, mode))

        async def mock_check_session(session_name, state, mode, wlog):
            pass

        live_task = MagicMock()
        live_task.done.return_value = False
        dead_task = MagicMock()
        dead_task.done.return_value = True

        live_state = {"enabled": True, "phase": 1, "step": 0, "log": [], "task": live_task}
        zombie_gn_state = {"enabled": True, "phase": 2, "step": 0, "log": [], "task": dead_task}

        _app._away_mode_state["live-sess-gn"] = live_state
        _app._go_nuts_state["zombie-gn"] = zombie_gn_state
        try:
            with patch("app.asyncio.sleep", counting_sleep), \
                 patch("app._watchdog_restart_mode", mock_restart), \
                 patch("app._watchdog_check_session", mock_check_session):
                try:
                    await _app._watchdog_loop()
                except _asyncio.CancelledError:
                    pass
        finally:
            _app._away_mode_state.pop("live-sess-gn", None)
            _app._go_nuts_state.pop("zombie-gn", None)

        assert any(name == "zombie-gn" and mode == "gonuts"
                   for name, mode in restart_calls)


# ─── _away_mode_worker paths ───


class TestAwayModeWorkerPaths:
    """Cover lines 3499-3610: _away_mode_worker exception, disabled, and cancel paths."""

    @pytest.mark.asyncio
    async def test_phase1_exception_is_skipped(self):
        """Lines 3505-3507: exception in phase 1 is caught and logged, phase 2 continues."""
        import app as _app

        phase2_called = []

        async def failing_phase1(session_name, state):
            raise RuntimeError("Phase 1 broke!")

        async def success_phase2(session_name, state):
            phase2_called.append(True)

        async def instant_execute(session_name, state):
            pass  # Phase 3 does nothing

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        _app._away_mode_state["worker-test"] = state
        try:
            with patch("app._away_phase_study", failing_phase1), \
                 patch("app._away_phase_select", success_phase2), \
                 patch("app._away_phase_execute", instant_execute), \
                 patch("app._away_mode_continuous_loop", None):
                # Disable after phase 2 so worker exits before continuous loop
                async def execute_and_disable(session_name, state):
                    state["enabled"] = False

                with patch("app._away_phase_execute", execute_and_disable):
                    await _app._away_mode_worker("worker-test")
        finally:
            _app._away_mode_state.pop("worker-test", None)

        assert phase2_called  # Phase 2 ran despite phase 1 error
        assert state["task"] is None  # Cleaned up in finally

    @pytest.mark.asyncio
    async def test_disabled_after_phase1_exits_early(self):
        """Lines 3509-3510: if disabled after phase 1, worker exits before phase 2."""
        import app as _app

        phase2_called = []

        async def phase1_that_disables(session_name, state):
            state["enabled"] = False  # Simulate user disabling during phase 1

        async def phase2(session_name, state):
            phase2_called.append(True)

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        _app._away_mode_state["worker-test2"] = state
        try:
            with patch("app._away_phase_study", phase1_that_disables), \
                 patch("app._away_phase_select", phase2):
                await _app._away_mode_worker("worker-test2")
        finally:
            _app._away_mode_state.pop("worker-test2", None)

        assert not phase2_called  # Phase 2 was NOT reached

    @pytest.mark.asyncio
    async def test_cancelled_error_sets_disabled_and_reraises(self):
        """Lines 3594-3598: CancelledError from phase sets enabled=False and re-raises."""
        import asyncio as _asyncio

        import app as _app

        async def cancelling_phase1(session_name, state):
            raise _asyncio.CancelledError()

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        _app._away_mode_state["worker-cancel"] = state
        try:
            with patch("app._away_phase_study", cancelling_phase1), \
                 patch("app._save_autonomous_state"):
                with pytest.raises(_asyncio.CancelledError):
                    await _app._away_mode_worker("worker-cancel")
        finally:
            _app._away_mode_state.pop("worker-cancel", None)

        assert state["enabled"] is False  # Set to False on cancel
        assert state["task"] is None      # Cleared in finally

# ---------------------------------------------------------------------------
# Phase 23 — High-coverage tests for remaining uncovered async paths
# ---------------------------------------------------------------------------


class TestCodexSubmitVerification:
    @pytest.mark.asyncio
    async def test_slash_command_never_retries_enter_into_its_picker(self, monkeypatch):
        run = MagicMock()
        capture = MagicMock(
            return_value=(
                "› /model\n\nSelect model and reasoning effort\n"
                "❯ 1. gpt-5.6-sol xhigh\n  2. gpt-6-astra xhigh\n"
            )
        )
        monkeypatch.setattr(app_module, "capture_pane_recent", capture)
        monkeypatch.setattr(app_module.subprocess, "run", run)
        monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())

        result = await app_module._ensure_codex_submitted(
            "off-session", "/model", "$99"
        )

        assert result == "submitted"
        capture.assert_not_called()
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_echoed_prompt_above_picker_does_not_retry_enter(self, monkeypatch):
        tail = (
            "› deploy the change\n\n"
            "Would you like to proceed?\n"
            "❯ 1. Yes\n  2. No\n"
        )
        run = MagicMock()
        monkeypatch.setattr(app_module, "capture_pane_recent", MagicMock(return_value=tail))
        monkeypatch.setattr(app_module.subprocess, "run", run)
        monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())

        result = await app_module._ensure_codex_submitted(
            "off-session", "deploy the change", "$99"
        )

        assert result == "submitted"
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_historical_picker_does_not_hide_a_pending_composer(self, monkeypatch):
        tail = (
            "Would you like to proceed?\n"
            "❯ 1. Yes\n  2. No\n\n"
            "› deploy the change\n\n"
            "  gpt-5.6-sol xhigh · ~/project · Main [default]\n"
        )
        run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr(app_module, "capture_pane_recent", MagicMock(return_value=tail))
        monkeypatch.setattr(app_module.subprocess, "run", run)
        monkeypatch.setattr(app_module.asyncio, "sleep", AsyncMock())

        result = await app_module._ensure_codex_submitted(
            "off-session", "deploy the change", "$99"
        )

        assert result == "stranded"
        assert [call.args[0][-1] for call in run.call_args_list] == ["C-m"] * 3

    def test_only_footer_adjacent_prompt_is_the_active_composer(self):
        pane = (
            "› old echoed message\n\n• Done.\n\n"
            "› new pending message\n\n  gpt-5.6-sol xhigh · ~/project · Main [default]\n"
        )

        assert app_module._active_codex_composer_text(pane) == "new pending message"

    def test_wrapped_live_composer_is_joined(self):
        pane = (
            "• Earlier response.\n\n"
            "› deploy the dashboard change and verify it\n"
            "  against the live service\n\n"
            "  gpt-5.6-sol xhigh · ~/lisa-my · Main [default]\n"
        )

        assert app_module._active_codex_composer_text(pane) == (
            "deploy the dashboard change and verify it against the live service"
        )

    def test_live_cursor_variants_are_safe(self):
        menu = "Select one\n› 2. Continue\n  3. Stop\n"
        assert app_module._detect_interactive_prompt(menu) == "selection_prompt"
        assert app_module._parse_menu_options(menu) == (
            [(2, "Continue"), (3, "Stop")], 0
        )
        draft = "› do not submit this\n\n  gpt-5.6-sol xhigh · ~/project · Main [default]\n"
        assert app_module._has_pending_user_input(draft) is True

        picker_after_old_footer = (
            "› Ask Codex to do anything\n"
            "  gpt-5.6-sol xhigh · ~/project · Main [default]\n\n"
            "Select model\n› 1. gpt-5.6-sol\n  2. gpt-6-astra\n"
        )
        assert app_module._detect_interactive_prompt(
            picker_after_old_footer
        ) == "selection_prompt"

    def test_historical_menu_cannot_submit_a_live_draft(self):
        pane = (
            "Select one\n› 2. Continue\n  3. Stop\n\n"
            "› do not submit this\n\n"
            "  gpt-5.6-sol xhigh · ~/project · Main [default]\n"
        )

        assert app_module._detect_interactive_prompt(pane) is None

    def test_menu_parser_ignores_historical_numbered_lists(self):
        pane = (
            "Earlier choices:\n  1. old one\n  2. old two\n  3. old three\n\n"
            "Choose how to proceed:\n❯ 1. Continue\n  2. Stop\n"
        )

        assert app_module._parse_menu_options(pane) == (
            [(1, "Continue"), (2, "Stop")], 0
        )
        assert app_module._detect_interactive_prompt(pane) == "selection_prompt"

    def test_numbered_live_draft_is_not_a_menu(self):
        pane = (
            "Select one\n› 2. Continue\n  3. Stop\n\n"
            "› 1. preserve this draft\n\n"
            "  gpt-5.6-sol xhigh · ~/project · Main [default]\n"
        )

        assert app_module._has_pending_user_input(pane) is True
        assert app_module._detect_interactive_prompt(pane) is None

        multiline = (
            "› 1. keep this draft\n"
            "  2. do not auto-submit it\n\n"
            "  gpt-5.6-sol xhigh · ~/project · Main [default]\n"
        )
        assert app_module._has_pending_user_input(multiline) is True
        assert app_module._detect_interactive_prompt(multiline) is None

        cwd_with_spaces = (
            "› 1. keep this draft\n"
            "  2. do not auto-submit it\n\n"
            "  gpt-5.6-sol xhigh · ~/My Project · Main [default]\n"
        )
        assert app_module._has_pending_user_input(cwd_with_spaces) is True
        assert app_module._detect_interactive_prompt(cwd_with_spaces) is None

    def test_live_placeholder_is_an_empty_composer(self):
        pane = (
            "› Ask Codex to do anything\n\n"
            "  gpt-5.6-sol xhigh · ~/project · Main [default]\n"
        )

        assert app_module._active_codex_composer_text(pane) == ""
        assert app_module._has_pending_user_input(pane) is False


class TestAutopushModeState:
    def test_missing_mode_file_fails_closed_to_off(self, tmp_path, monkeypatch):
        path = tmp_path / "missing-autopush-mode.json"
        monkeypatch.setattr(app_module, "PROCESS_ROLE", "api")
        monkeypatch.setattr(app_module, "AUTOPUSH_MODE_FILE", path)
        monkeypatch.setattr(
            app_module, "_autopush_mode_store",
            app_module.LockedJsonStore(path, lambda: {}),
        )
        monkeypatch.setattr(
            app_module, "_autopush_mode_file_stamp",
            app_module._AUTOPUSH_UNCHECKED_STAMP,
        )
        monkeypatch.setattr(app_module, "_autopush_mode_state_valid", True)
        monkeypatch.setattr(app_module, "_autopush_mode", {"session": "full"})

        assert app_module._get_autopush_mode("session") == "off"
        assert app_module._autopush_mode_state_valid is False

    def test_api_worker_reads_controller_persisted_off_mode(self, tmp_path, monkeypatch):
        path = tmp_path / "autopush-mode.json"
        store = app_module.LockedJsonStore(path, lambda: {})
        store.update(lambda data: data.update({"off-session": "off"}))
        monkeypatch.setattr(app_module, "PROCESS_ROLE", "api")
        monkeypatch.setattr(app_module, "AUTOPUSH_MODE_FILE", path)
        monkeypatch.setattr(app_module, "_autopush_mode_store", store)
        monkeypatch.setattr(app_module, "_autopush_mode_file_stamp", None)
        monkeypatch.setattr(app_module, "_autopush_mode_state_valid", True)
        monkeypatch.setattr(app_module, "_autopush_mode", {})

        assert app_module._get_autopush_mode("off-session") == "off"
        store.update(lambda data: data.update({"off-session": "basic"}))
        assert app_module._get_autopush_mode("off-session") == "basic"

    def test_corrupt_mode_file_fails_closed_to_off(self, tmp_path, monkeypatch):
        path = tmp_path / "autopush-mode.json"
        path.write_text("{not valid json")
        monkeypatch.setattr(app_module, "PROCESS_ROLE", "api")
        monkeypatch.setattr(app_module, "AUTOPUSH_MODE_FILE", path)
        monkeypatch.setattr(
            app_module, "_autopush_mode_store",
            app_module.LockedJsonStore(path, lambda: {}),
        )
        monkeypatch.setattr(app_module, "_autopush_mode_file_stamp", None)
        monkeypatch.setattr(app_module, "_autopush_mode_state_valid", True)
        monkeypatch.setattr(app_module, "_autopush_mode", {"session": "full"})

        assert app_module._get_autopush_mode("session") == "off"
        assert app_module._autopush_mode_state_valid is False

    def test_corrupt_recovery_publishes_fail_closed_snapshot_in_memory(
        self, tmp_path, monkeypatch
    ):
        path = tmp_path / "autopush-mode.json"
        path.write_text("{broken")
        monkeypatch.setattr(app_module, "PROCESS_ROLE", "controller")
        monkeypatch.setattr(app_module, "AUTOPUSH_MODE_FILE", path)
        monkeypatch.setattr(
            app_module, "_autopush_mode_store",
            app_module.LockedJsonStore(path, lambda: {}),
        )
        monkeypatch.setattr(app_module, "_autopush_mode_state_valid", False)
        monkeypatch.setattr(app_module, "_autopush_mode", {"chosen": "full"})
        monkeypatch.setattr(
            app_module, "get_tmux_sessions",
            lambda: [{"name": "chosen"}, {"name": "unrelated"}],
        )

        app_module._save_autopush_mode()

        expected = {"chosen": "full", "unrelated": "off"}
        assert app_module._autopush_mode == expected
        assert json.loads(path.read_text()) == expected
        assert app_module._get_autopush_mode("unrelated") == "off"

    @pytest.mark.asyncio
    async def test_queued_set_rejects_replaced_generation(self, monkeypatch):
        name = "generation-race-session"
        owner_id = "owner-1"
        old_generation = "a" * 32
        lifecycle = {
            "owner_id": owner_id,
            "generation": old_generation,
            "desired_state": "running",
        }
        app_module._autopush_mode[name] = "basic"
        app_module._autopush_action_locks.pop(name, None)
        lock = app_module._autopush_action_lock(name)
        await lock.acquire()
        monkeypatch.setattr(
            app_module, "_strict_session_owner",
            lambda session, expected="": (owner_id, {})
            if session == name and expected == owner_id else None,
        )
        monkeypatch.setattr(
            app_module._session_lifecycle, "get", lambda session: dict(lifecycle)
        )
        monkeypatch.setattr(app_module, "_save_autopush_mode", MagicMock())

        setting = asyncio.create_task(app_module._controller_dispatch({
            "op": "autopush_set",
            "session": name,
            "mode": "off",
            "owner_id": owner_id,
            "generation": old_generation,
        }))
        await asyncio.sleep(0)
        lifecycle["generation"] = "b" * 32
        lock.release()
        result = await setting

        try:
            assert result["_status"] == 404
            assert app_module._autopush_mode[name] == "basic"
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)

    @pytest.mark.asyncio
    async def test_off_response_waits_for_inflight_auto_action(self, monkeypatch):
        name = "off-barrier-session"
        entered = asyncio.Event()
        release = asyncio.Event()
        app_module._autopush_mode[name] = "basic"
        app_module._autopush_action_locks.pop(name, None)
        monkeypatch.setattr(app_module, "_save_autopush_mode", MagicMock())

        async def inflight_action():
            async with app_module._autopush_action_lock(name):
                entered.set()
                await release.wait()

        action = asyncio.create_task(inflight_action())
        await entered.wait()
        setting = asyncio.create_task(app_module._controller_dispatch({
            "op": "autopush_set", "session": name, "mode": "off",
        }))
        await asyncio.sleep(0)
        assert not setting.done()

        release.set()
        await action
        result = await setting

        try:
            assert result["ok"] is True
            assert result["mode"] == "off"
            assert app_module._get_autopush_mode(name) == "off"
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)

    @pytest.mark.asyncio
    async def test_off_cancels_autonomous_modes_and_blocks_their_sender(self, monkeypatch):
        name = "off-autonomous-session"
        away_task = asyncio.create_task(asyncio.sleep(3600))
        nuts_task = asyncio.create_task(asyncio.sleep(3600))
        await asyncio.sleep(0)
        app_module._autopush_mode[name] = "basic"
        app_module._away_mode_state[name] = {"enabled": True, "task": away_task}
        app_module._go_nuts_state[name] = {"enabled": True, "task": nuts_task}
        app_module._autopush_action_locks.pop(name, None)
        monkeypatch.setattr(app_module, "_save_autopush_mode", MagicMock())
        monkeypatch.setattr(app_module, "_save_autonomous_state", MagicMock())

        try:
            result = await app_module._controller_dispatch({
                "op": "autopush_set", "session": name, "mode": "off",
            })
            await asyncio.gather(away_task, nuts_task, return_exceptions=True)
            sender = AsyncMock()
            monkeypatch.setattr(app_module, "_away_send_prompt_unlocked", sender)
            await app_module._away_send_prompt(name, "do not send")

            assert result["ok"] is True
            assert app_module._away_mode_state[name]["enabled"] is False
            assert app_module._go_nuts_state[name]["enabled"] is False
            assert away_task.cancelled()
            assert nuts_task.cancelled()
            sender.assert_not_awaited()
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._away_mode_state.pop(name, None)
            app_module._go_nuts_state.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)


    @pytest.mark.asyncio
    async def test_off_is_not_acknowledged_without_durable_autonomous_stop(
        self, monkeypatch
    ):
        name = "autonomous-save-failure"
        app_module._autopush_mode[name] = "basic"
        app_module._away_mode_state[name] = {"enabled": True, "task": None}
        app_module._autopush_action_locks.pop(name, None)
        monkeypatch.setattr(app_module, "_autopush_mode_state_valid", True)
        mode_save = MagicMock()
        monkeypatch.setattr(app_module, "_save_autopush_mode", mode_save)
        monkeypatch.setattr(app_module, "_save_autonomous_state", lambda *_args: False)

        try:
            result = await app_module._controller_dispatch({
                "op": "autopush_set", "session": name, "mode": "off",
            })

            assert result["_status"] == 503
            assert app_module._autopush_mode[name] == "basic"
            assert app_module._away_mode_state[name]["enabled"] is False
            mode_save.assert_not_called()
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._away_mode_state.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)


class TestAutopushLoginBarrier:
    @pytest.mark.asyncio
    async def test_off_during_auth_validation_blocks_login_writes(self, monkeypatch):
        name = "login-off-race"
        validation_started = asyncio.Event()
        release_validation = asyncio.Event()
        sends = []

        async def fake_to_thread(fn, *args, **kwargs):
            if fn is app_module._ensure_codex_auth_with_fallback:
                validation_started.set()
                await release_validation.wait()
                return {
                    "loggedIn": True, "fallbackActive": False,
                    "activeMode": "chatgpt",
                }
            if fn is app_module.subprocess.run:
                sends.append(args[0])
                return MagicMock(returncode=0)
            return fn(*args, **kwargs)

        app_module._autopush_mode[name] = "basic"
        app_module._autopush_action_locks.pop(name, None)
        monkeypatch.setattr(app_module.asyncio, "to_thread", fake_to_thread)
        monkeypatch.setattr(app_module, "_save_autopush_mode", MagicMock())
        monkeypatch.setattr(app_module, "_save_autonomous_state", MagicMock())
        restart = AsyncMock(return_value=(True, True))
        monkeypatch.setattr(app_module, "_restart_codex_for_session", restart)

        recovery = asyncio.create_task(app_module._auto_fix_login(
            name, respect_autopush=True
        ))
        await validation_started.wait()
        setting = await app_module._controller_dispatch({
            "op": "autopush_set", "session": name, "mode": "off",
        })
        release_validation.set()
        result = await recovery

        try:
            assert setting["ok"] is True
            assert result["error"] == "auto-push is Off"
            assert sends == []
            restart.assert_not_awaited()
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)


class TestAutopushSessionBinding:
    @pytest.mark.asyncio
    async def test_full_writer_rejects_same_name_replacement(self, monkeypatch):
        name = "full-replaced"
        binding = {"session_id": "$old"}
        run = MagicMock(return_value=MagicMock(returncode=0))
        app_module._autopush_mode[name] = "full"
        app_module._autopush_action_locks.pop(name, None)
        monkeypatch.setattr(app_module, "_autopush_mode_state_valid", True)
        monkeypatch.setattr(
            app_module, "_terminal_binding_state", lambda _binding: "replaced"
        )
        monkeypatch.setattr(app_module.subprocess, "run", run)

        try:
            sent = await app_module._simple_watchdog_send_text(
                name, "keep going", required_modes={"full"},
                expected_binding=binding,
            )
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)

        assert sent is False
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_autonomous_batch_rejects_same_name_replacement(
        self, monkeypatch
    ):
        name = "autonomous-replaced"
        binding = {"session_id": "$old"}
        run = MagicMock(return_value=MagicMock(returncode=0))
        app_module._autopush_mode[name] = "basic"
        app_module._autopush_action_locks.pop(name, None)
        monkeypatch.setattr(app_module, "_autopush_mode_state_valid", True)
        monkeypatch.setattr(
            app_module, "_terminal_binding_state", lambda _binding: "replaced"
        )
        monkeypatch.setattr(app_module.subprocess, "run", run)

        try:
            sent = await app_module._autopush_tmux_batch(
                name,
                [(["tmux", "send-keys", "-t", name, "Enter"], 3)],
                expected_binding=binding,
            )
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)

        assert sent is False
        run.assert_not_called()

    @pytest.mark.asyncio
    async def test_autonomous_enable_superseded_by_disable(self, monkeypatch):
        name = "away-enable-race"
        resume_started = asyncio.Event()
        release_resume = asyncio.Event()

        async def delayed_resume(*_args, **_kwargs):
            resume_started.set()
            await release_resume.wait()
            return {"ok": True}

        monkeypatch.setattr(
            app_module, "_find_session", lambda _name: (0, {"name": name})
        )
        monkeypatch.setattr(
            app_module, "_autopush_identity_matches", lambda *_args: True
        )
        monkeypatch.setattr(app_module, "_get_autopush_mode", lambda _name: "basic")
        monkeypatch.setattr(app_module, "_resume_parked_session", delayed_resume)
        monkeypatch.setattr(app_module, "_save_autonomous_state", lambda *_args: True)
        app_module._away_mode_state.pop(name, None)
        app_module._go_nuts_state.pop(name, None)
        app_module._autopush_action_locks.pop(name, None)
        app_module._autonomous_toggle_revision.pop(name, None)

        enabling = asyncio.create_task(
            app_module._away_toggle_local(name, True)
        )
        await resume_started.wait()
        disabled = await app_module._away_toggle_local(name, False)
        release_resume.set()
        enabled = await enabling

        try:
            assert disabled["ok"] is True
            assert enabled["_status"] == 409
            assert not app_module._away_mode_state.get(name, {}).get("enabled")
        finally:
            app_module._away_mode_state.pop(name, None)
            app_module._go_nuts_state.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)
            app_module._autonomous_toggle_revision.pop(name, None)


class TestCacheKeepaliveSafety:
    @pytest.mark.asyncio
    async def test_expired_cache_never_sends_a_keepalive(self, monkeypatch):
        import asyncio as _asyncio

        sleeps = 0

        async def one_iteration(_seconds):
            nonlocal sleeps
            sleeps += 1
            if sleeps <= 2:
                return
            raise _asyncio.CancelledError()

        monkeypatch.setattr(app_module.asyncio, "sleep", one_iteration)
        monkeypatch.setattr(
            app_module, "get_tmux_sessions", lambda: [{"name": "cache-session"}]
        )
        monkeypatch.setattr(
            app_module, "_get_autopush_mode", lambda _name: "basicplus"
        )
        monkeypatch.setattr(
            app_module, "_codex_session_facts",
            lambda _name: {
                "cache_ttl": 1800, "last_turn_end": 100,
                "detect_sure": True,
            },
        )
        monkeypatch.setattr(app_module.time, "time", lambda: 2000)
        sender = AsyncMock(return_value=True)
        monkeypatch.setattr(app_module, "_simple_watchdog_send_text", sender)

        with pytest.raises(_asyncio.CancelledError):
            await app_module._cache_keepalive_loop()

        sender.assert_not_awaited()


@pytest.mark.usefixtures("owned_autopush_runtime")
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
            # Exit after the initial delay + one complete polling iteration.
            if sleep_count[0] >= 3:
                raise _asyncio.CancelledError()

        mock_capture = MagicMock(returncode=0, stdout="❯ 1. Yes\n2. No\nWould you like to proceed")
        mock_enter = MagicMock(returncode=0, stdout="")
        sent = []

        async def mock_to_thread(fn, *args, **kwargs):
            if fn is _app._terminal_binding:
                return fn(*args, **kwargs)
            if fn is _app._terminal_binding_state:
                return "current"
            if fn is _app.subprocess.run and args[0][1] == "capture-pane":
                return mock_capture
            if fn is _app.subprocess.run and args[0][1] == "send-keys":
                sent.append(args[0])
            return mock_enter

        _app._auto_respond_cooldown.clear()
        _app._auto_respond_log.clear()
        try:
            with patch("app.get_tmux_sessions", return_value=[{
                     "name": "ar-sess", "owner_id": "admin",
                 }]), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.asyncio.to_thread", mock_to_thread), \
                 patch("app._detect_interactive_prompt", return_value="plan_approval"), \
                 patch("app._llm_pick_menu_option", new=AsyncMock(return_value=None)), \
                 patch("app.time.time", return_value=1000.0):
                with pytest.raises(_asyncio.CancelledError):
                    await _app._auto_responder_loop()
        finally:
            _app._auto_respond_cooldown.clear()
            _app._auto_respond_log.clear()

        assert sent and sent[-1][-1] == "Enter"

    @pytest.mark.asyncio
    async def test_switching_off_while_picker_awaits_aborts_selection(
        self, monkeypatch, owned_autopush_runtime
    ):
        import asyncio as _asyncio

        name = "off-race-session"
        owned_autopush_runtime(name)
        sleep_count = 0
        picker_calls = []

        async def one_iteration_sleep(_secs):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count <= 2:
                return
            raise _asyncio.CancelledError()

        async def switch_off_during_pick(*_args, **_kwargs):
            picker_calls.append(name)
            app_module._autopush_mode[name] = "off"
            return 1

        def tmux(command, **_kwargs):
            if command[1] == "capture-pane":
                return MagicMock(
                    returncode=0,
                    stdout="Would you like to proceed?\n❯ 1. Yes\n  2. No\n",
                )
            return MagicMock(returncode=0, stdout="")

        app_module._autopush_mode[name] = "basic"
        app_module._auto_respond_cooldown.pop(name, None)
        app_module._auto_respond_log.clear()
        app_module._autopush_action_locks.pop(name, None)
        run = MagicMock(side_effect=tmux)
        monkeypatch.setattr(app_module, "get_tmux_sessions", lambda: [{"name": name}])
        monkeypatch.setattr(
            app_module, "_terminal_binding",
            lambda *_args: {"session_id": "$42"},
        )
        monkeypatch.setattr(
            app_module, "_terminal_binding_state", lambda _binding: "current"
        )
        monkeypatch.setattr(app_module.asyncio, "sleep", one_iteration_sleep)
        monkeypatch.setattr(app_module, "_llm_pick_menu_option", switch_off_during_pick)
        monkeypatch.setattr(app_module.subprocess, "run", run)
        monkeypatch.setattr(app_module._session_lifecycle, "touch", MagicMock())

        try:
            with pytest.raises(_asyncio.CancelledError):
                await app_module._auto_responder_loop()
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._auto_respond_cooldown.pop(name, None)
            app_module._auto_respond_log.clear()
            app_module._autopush_action_locks.pop(name, None)

        assert not [
            call for call in run.call_args_list
            if call.args and call.args[0][1:3] == ["send-keys", "-t"]
        ]
        assert picker_calls == [name]

    @pytest.mark.asyncio
    async def test_replaced_same_name_session_aborts_selection(self, monkeypatch):
        import asyncio as _asyncio

        name = "replaced-session"
        sleep_count = 0

        async def one_iteration_sleep(_secs):
            nonlocal sleep_count
            sleep_count += 1
            if sleep_count <= 2:
                return
            raise _asyncio.CancelledError()

        def tmux(command, **_kwargs):
            if command[1] == "capture-pane":
                return MagicMock(
                    returncode=0,
                    stdout="Would you like to proceed?\n❯ 1. Yes\n  2. No\n",
                )
            return MagicMock(returncode=0, stdout="")

        app_module._autopush_mode[name] = "basic"
        app_module._auto_respond_cooldown.pop(name, None)
        app_module._auto_respond_log.clear()
        app_module._autopush_action_locks.pop(name, None)
        run = MagicMock(side_effect=tmux)
        monkeypatch.setattr(
            app_module, "get_tmux_sessions",
            lambda: [{"name": name, "owner_id": "admin"}],
        )
        monkeypatch.setattr(
            app_module, "_terminal_binding",
            lambda *_args: {"session_id": "$old"},
        )
        monkeypatch.setattr(
            app_module, "_terminal_binding_state", lambda _binding: "replaced"
        )
        monkeypatch.setattr(app_module.asyncio, "sleep", one_iteration_sleep)
        monkeypatch.setattr(
            app_module, "_llm_pick_menu_option", AsyncMock(return_value=None)
        )
        monkeypatch.setattr(app_module.subprocess, "run", run)
        monkeypatch.setattr(app_module._session_lifecycle, "touch", MagicMock())

        try:
            with pytest.raises(_asyncio.CancelledError):
                await app_module._auto_responder_loop()
        finally:
            app_module._autopush_mode.pop(name, None)
            app_module._auto_respond_cooldown.pop(name, None)
            app_module._auto_respond_log.clear()
            app_module._autopush_action_locks.pop(name, None)

        assert not [
            call for call in run.call_args_list
            if call.args and call.args[0][1:3] == ["send-keys", "-t"]
        ]

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


class TestGoNutsModeWorkerPaths:
    """Tests for _go_nuts_mode_worker top-level paths (lines 3849-3870)."""

    @pytest.mark.asyncio
    async def test_phase1_exception_skipped_phase2_runs(self):
        """Lines 3857-3862: phase 1 exception is caught and skipped."""
        import app as _app

        async def failing_phase1(session_name, state):
            raise RuntimeError("Phase 1 boom")

        phase2_called = [False]

        async def noop_phase2(session_name, state):
            phase2_called[0] = True

        async def noop_phase(session_name, state):
            pass

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        _app._go_nuts_state["gn-test1"] = state

        try:
            with patch("app._go_nuts_phase_discover", failing_phase1), \
                 patch("app._go_nuts_phase_backlog", noop_phase2), \
                 patch("app._go_nuts_phase_build", noop_phase), \
                 patch("app._save_autonomous_state"), \
                 patch("app.asyncio.sleep", side_effect=RuntimeError("break")), \
                 patch("app.async_detect_activity", return_value={"status": "idle"}):
                try:
                    await _app._go_nuts_mode_worker("gn-test1")
                except RuntimeError:
                    pass
        finally:
            _app._go_nuts_state.pop("gn-test1", None)

        assert phase2_called[0]

    @pytest.mark.asyncio
    async def test_disabled_after_phase1_exits_early(self):
        """Lines 3863-3864: worker exits early if disabled after phase 1."""
        import app as _app

        phase2_called = [False]

        async def disabling_phase1(session_name, state):
            state["enabled"] = False

        async def noop_phase2(session_name, state):
            phase2_called[0] = True

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        _app._go_nuts_state["gn-test2"] = state

        try:
            with patch("app._go_nuts_phase_discover", disabling_phase1), \
                 patch("app._go_nuts_phase_backlog", noop_phase2), \
                 patch("app._save_autonomous_state"):
                await _app._go_nuts_mode_worker("gn-test2")
        finally:
            _app._go_nuts_state.pop("gn-test2", None)

        assert not phase2_called[0]

    @pytest.mark.asyncio
    async def test_cancelled_error_sets_disabled_and_reraises(self):
        """Lines 3945-3950: CancelledError propagates with enabled=False."""
        import asyncio as _asyncio

        import app as _app

        async def cancelling_phase1(session_name, state):
            raise _asyncio.CancelledError()

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        _app._go_nuts_state["gn-cancel"] = state

        try:
            with patch("app._go_nuts_phase_discover", cancelling_phase1), \
                 patch("app._save_autonomous_state"):
                with pytest.raises(_asyncio.CancelledError):
                    await _app._go_nuts_mode_worker("gn-cancel")
        finally:
            _app._go_nuts_state.pop("gn-cancel", None)

        assert state["enabled"] is False
        assert state["task"] is None


class TestAwayModeWorkerContinuousLoop:
    """Tests for the phase 4+ continuous loop in _away_mode_worker (lines 3538-3594)."""

    @pytest.mark.asyncio
    async def test_one_cycle_then_disabled(self, owned_autopush_runtime):
        """Lines 3538-3594: runs one full ping cycle then exits when disabled."""
        import app as _app

        sleep_count = [0]
        time_values = [0.0, 91.0, 91.0]
        time_idx = [0]

        async def counting_sleep(secs):
            sleep_count[0] += 1
            if sleep_count[0] > 20:
                pytest.fail("Away worker exceeded one mocked cycle")
            if secs == 5:  # sleep after successful cycle → disable
                state["enabled"] = False

        def mock_time():
            val = time_values[min(time_idx[0], len(time_values) - 1)]
            time_idx[0] += 1
            return val

        send_calls = [0]

        async def mock_send_wait(session_name, prompt, state, step_name, timeout=900):
            send_calls[0] += 1

        async def noop_ensure(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_owner_id"] == "admin"
            assert kwargs["autopush_guard"] is True
            return True

        async def mock_activity(session_name):
            return {"status": "idle"}

        async def noop_phase(session_name, state):
            pass

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        owned_autopush_runtime("cont-away", state)

        try:
            with patch("app._away_phase_study", noop_phase), \
                 patch("app._away_phase_select", noop_phase), \
                 patch("app._away_phase_execute", noop_phase), \
                 patch("app._away_log"), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.async_detect_activity", mock_activity), \
                 patch("app.time.time", mock_time), \
                 patch("app._ensure_codex_running", noop_ensure), \
                 patch("app._away_send_and_wait", mock_send_wait), \
                 patch("app._save_autonomous_state"):
                await asyncio.wait_for(_app._away_mode_worker("cont-away"), timeout=2)
        finally:
            _app._away_mode_state.pop("cont-away", None)

        assert send_calls[0] == 1
        assert state["step"] == 1
        assert state["phase"] == 4
        assert state["task"] is None

    @pytest.mark.asyncio
    async def test_claude_dead_stops_loop(self, owned_autopush_runtime):
        """Lines 3554-3559: Codex dead and can't restart → stop."""
        import app as _app

        sleep_count = [0]

        async def counting_sleep(secs):
            sleep_count[0] += 1
            if sleep_count[0] > 20:
                pytest.fail("Away worker did not stop after failed Codex recovery")

        async def mock_activity(session_name):
            return {"status": "idle"}

        time_idx = [0]
        time_vals = [0.0, 91.0]

        def mock_time():
            val = time_vals[min(time_idx[0], len(time_vals) - 1)]
            time_idx[0] += 1
            return val

        async def dead_ensure(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_owner_id"] == "admin"
            return False  # Codex dead, can't restart

        async def noop_phase(session_name, state):
            pass

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        owned_autopush_runtime("dead-away", state)

        try:
            with patch("app._away_phase_study", noop_phase), \
                 patch("app._away_phase_select", noop_phase), \
                 patch("app._away_phase_execute", noop_phase), \
                 patch("app._away_log"), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.async_detect_activity", mock_activity), \
                 patch("app.time.time", mock_time), \
                 patch("app._ensure_codex_running", dead_ensure), \
                 patch("app._save_autonomous_state"):
                await asyncio.wait_for(_app._away_mode_worker("dead-away"), timeout=2)
        finally:
            _app._away_mode_state.pop("dead-away", None)

        assert state["enabled"] is False
        assert state["task"] is None


class TestGoNutsModeWorkerContinuousLoop:
    """Tests for the phase 4+ continuous loop in _go_nuts_mode_worker (lines 3883-3958)."""

    @pytest.mark.asyncio
    async def test_one_cycle_then_disabled(self, owned_autopush_runtime):
        """Lines 3883-3958: go-nuts runs one full build cycle then exits."""
        import app as _app

        time_values = [0.0, 91.0, 91.0]
        time_idx = [0]
        sleep_count = [0]

        async def counting_sleep(secs):
            sleep_count[0] += 1
            if sleep_count[0] > 20:
                pytest.fail("Go Nuts worker exceeded one mocked cycle")
            if secs == 5:
                state["enabled"] = False

        def mock_time():
            val = time_values[min(time_idx[0], len(time_values) - 1)]
            time_idx[0] += 1
            return val

        send_calls = [0]

        async def mock_send_wait(session_name, prompt, state, step_name, timeout=900):
            send_calls[0] += 1

        async def noop_ensure(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_owner_id"] == "admin"
            assert kwargs["autopush_guard"] is True
            return True

        async def mock_activity(session_name):
            return {"status": "idle"}

        async def noop_phase(session_name, state):
            pass

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        owned_autopush_runtime("cont-gn", state, mode="gonuts")

        try:
            with patch("app._go_nuts_phase_discover", noop_phase), \
                 patch("app._go_nuts_phase_backlog", noop_phase), \
                 patch("app._go_nuts_phase_build", noop_phase), \
                 patch("app._go_nuts_log"), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.async_detect_activity", mock_activity), \
                 patch("app.time.time", mock_time), \
                 patch("app._ensure_codex_running", noop_ensure), \
                 patch("app._go_nuts_send_and_wait", mock_send_wait), \
                 patch("app._save_autonomous_state"):
                await asyncio.wait_for(_app._go_nuts_mode_worker("cont-gn"), timeout=2)
        finally:
            _app._go_nuts_state.pop("cont-gn", None)

        assert send_calls[0] == 1
        assert state["step"] == 1
        assert state["phase"] == 4
        assert state["task"] is None

    @pytest.mark.asyncio
    async def test_claude_dead_stops_loop(self, owned_autopush_runtime):
        """Go-nuts: Codex dead → stops loop."""
        import app as _app

        sleep_count = [0]

        async def counting_sleep(secs):
            sleep_count[0] += 1
            if sleep_count[0] > 20:
                pytest.fail("Go Nuts worker did not stop after failed Codex recovery")

        time_idx = [0]
        time_vals = [0.0, 91.0]

        def mock_time():
            val = time_vals[min(time_idx[0], len(time_vals) - 1)]
            time_idx[0] += 1
            return val

        async def dead_ensure(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_owner_id"] == "admin"
            return False

        async def mock_activity(session_name):
            return {"status": "idle"}

        async def noop_phase(session_name, state):
            pass

        state = {
            "enabled": True, "phase": 0, "phase_name": "Init",
            "step": 0, "step_name": "", "started_at": 0.0,
            "log": [], "report": "", "task": None,
        }
        owned_autopush_runtime("dead-gn", state, mode="gonuts")

        try:
            with patch("app._go_nuts_phase_discover", noop_phase), \
                 patch("app._go_nuts_phase_backlog", noop_phase), \
                 patch("app._go_nuts_phase_build", noop_phase), \
                 patch("app._go_nuts_log"), \
                 patch("app.asyncio.sleep", counting_sleep), \
                 patch("app.async_detect_activity", mock_activity), \
                 patch("app.time.time", mock_time), \
                 patch("app._ensure_codex_running", dead_ensure), \
                 patch("app._save_autonomous_state"):
                await asyncio.wait_for(_app._go_nuts_mode_worker("dead-gn"), timeout=2)
        finally:
            _app._go_nuts_state.pop("dead-gn", None)

        assert state["enabled"] is False


class TestWatchdogCheckSession:
    """Tests for _watchdog_check_session (lines 2774-2882)."""

    @pytest.mark.asyncio
    async def test_empty_pane_returns_early(self, owned_autopush_runtime):
        """Lines 2775-2779: empty pane → return immediately."""
        import logging

        import app as _app

        wlog = logging.getLogger("watchdog-test")
        state = {"enabled": True, "log": []}
        owned_autopush_runtime("empty-sess", state)

        with patch("app.asyncio.to_thread", return_value="   "):
            await _app._watchdog_check_session("empty-sess", state, "away", wlog)
        # No assertion needed — function returned without error

    @pytest.mark.asyncio
    async def test_new_content_resets_snapshot(self, owned_autopush_runtime):
        """Lines 2781-2790: new content hash → reset snapshot, return."""
        import logging

        import app as _app

        wlog = logging.getLogger("watchdog-test")
        state = {"enabled": True, "log": []}
        owned_autopush_runtime("snap-sess", state)

        _app._watchdog_snapshots.pop("snap-sess", None)
        try:
            with patch("app.asyncio.to_thread", return_value="new terminal content here"), \
                 patch("app.time.time", return_value=1000.0):
                await _app._watchdog_check_session("snap-sess", state, "away", wlog)
            snap = _app._watchdog_snapshots.get("snap-sess")
            assert snap is not None
            assert snap["nudge_count"] == 0
        finally:
            _app._watchdog_snapshots.pop("snap-sess", None)

    @pytest.mark.asyncio
    async def test_stall_below_threshold_returns_early(self, owned_autopush_runtime):
        """Lines 2793-2795: stall detected but < _STALL_THRESHOLD → return."""
        import logging

        import app as _app

        wlog = logging.getLogger("watchdog-test")
        state = {"enabled": True, "log": []}
        owned_autopush_runtime("thresh-sess", state)

        # Pre-seed a snapshot with same content hash
        import hashlib

        content = "unchanged terminal output"
        content_hash = hashlib.md5(content.encode()).hexdigest()
        _app._watchdog_snapshots["thresh-sess"] = {
            "content_hash": content_hash,
            "first_seen": 900.0,  # 100s ago at now=1000
            "nudge_count": 0,
            "last_nudge": 0,
        }
        try:
            with patch("app.asyncio.to_thread", return_value=content), \
                 patch("app.time.time", return_value=1000.0), \
                 patch("app._STALL_THRESHOLD", 600):  # threshold = 600s, stall = 100s < 600
                await _app._watchdog_check_session("thresh-sess", state, "away", wlog)
        finally:
            _app._watchdog_snapshots.pop("thresh-sess", None)

    @pytest.mark.asyncio
    async def test_stall_llm_says_stuck_triggers_nudge(self, owned_autopush_runtime):
        """Lines 2801-2879: LLM says STUCK → nudge sent."""
        import hashlib
        import logging

        import app as _app

        wlog = logging.getLogger("watchdog-test")
        state = {"enabled": True, "log": []}
        owned_autopush_runtime("nudge-sess", state)

        content = "stuck terminal output"
        content_hash = hashlib.md5(content.encode()).hexdigest()
        _app._watchdog_snapshots["nudge-sess"] = {
            "content_hash": content_hash,
            "first_seen": 0.0,  # 1000s ago
            "nudge_count": 0,
            "last_nudge": 0.0,
        }

        nudge_called = [False]

        async def mock_send_prompt(session_name, prompt, *, expected_binding=None):
            assert expected_binding["owner_id"] == "admin"
            assert expected_binding["session_id"] == "$99"
            nudge_called[0] = True

        try:
            with patch("app.asyncio.to_thread", return_value=content), \
                 patch("app.time.time", return_value=1000.0), \
                 patch("app._STALL_THRESHOLD", 10), \
                 patch("app._async_is_codex_running", return_value=True), \
                 patch("app.llm_call", return_value="STUCK"), \
                 patch("app.async_detect_activity",
                       return_value={"status": "idle"}), \
                 patch("app._away_send_prompt", mock_send_prompt), \
                 patch("app._NUDGE_COOLDOWN", 0), \
                 patch("app._MAX_NUDGES_BEFORE_RESTART", 3):
                await _app._watchdog_check_session("nudge-sess", state, "away", wlog)
        finally:
            _app._watchdog_snapshots.pop("nudge-sess", None)

        assert nudge_called[0]


class TestWatchdogRestartMode:
    """Tests for _watchdog_restart_mode (lines 2887-2950)."""

    @pytest.mark.asyncio
    async def test_away_mode_restart_launches_continuous_loop(self, owned_autopush_runtime):
        """Lines 2887-2950: restart cancels old task, sends unstick, creates new task."""
        import asyncio as _asyncio
        import logging

        import app as _app

        wlog = logging.getLogger("watchdog-test")

        old_task = MagicMock()
        old_task.done.return_value = False
        old_task.cancel = MagicMock()

        state = {
            "enabled": True, "log": ["existing log"],
            "started_at": 100.0, "step": 3,
            "phase": 2, "phase_name": "Phase 2",
            "task": old_task,
        }
        binding = owned_autopush_runtime("restart-test", state)

        send_calls = [0]

        async def mock_send_prompt(session_name, prompt, **kwargs):
            assert kwargs["expected_binding"] == binding
            send_calls[0] += 1

        async def mock_ensure(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_binding"] == binding
            assert kwargs["expected_owner_id"] == "admin"
            assert kwargs["autopush_guard"] is True
            return True

        async def instant_continuous(session_name):
            pass

        async def mock_to_thread(fn, *args, **kwargs):
            if fn is _app._terminal_binding_state:
                return fn(*args, **kwargs)
            return MagicMock()

        try:
            with patch("app.asyncio.to_thread", mock_to_thread), \
                 patch("app.asyncio.sleep", return_value=None), \
                 patch("app._get_autopush_mode", return_value="basic"), \
                 patch("app._autopush_tmux_batch", new=AsyncMock(return_value=True)), \
                 patch("app._ensure_codex_running", mock_ensure), \
                 patch("app._away_send_prompt", mock_send_prompt), \
                 patch("app._away_mode_continuous_loop", instant_continuous), \
                 patch("app._save_autonomous_state"), \
                 patch("app.asyncio.wait_for", side_effect=_asyncio.TimeoutError()):
                await _app._watchdog_restart_mode("restart-test", state, "away", wlog)
            if state.get("task"):
                await asyncio.wait_for(state["task"], timeout=2)
        finally:
            _app._away_mode_state.pop("restart-test", None)

        assert send_calls[0] == 1
        assert state["phase"] == 4
        assert state["enabled"] is True

    @pytest.mark.asyncio
    async def test_restart_codex_dead_sets_disabled(self, owned_autopush_runtime):
        """If Codex cannot be restarted, the autonomous mode is disabled."""
        import logging

        import app as _app

        wlog = logging.getLogger("watchdog-test")

        state = {
            "enabled": True, "log": [],
            "started_at": 0.0, "step": 0,
            "task": None,
        }
        binding = owned_autopush_runtime("restart-dead", state)

        async def dead_ensure(session_name, log_fn=None, state=None, **kwargs):
            assert kwargs["expected_binding"] == binding
            return False

        try:
            with patch("app.asyncio.to_thread", new_callable=AsyncMock), \
                 patch("app.asyncio.sleep", return_value=None), \
                 patch("app._get_autopush_mode", return_value="basic"), \
                 patch("app._autopush_tmux_batch", new=AsyncMock(return_value=True)), \
                 patch("app._ensure_codex_running", dead_ensure), \
                 patch("app._save_autonomous_state"):
                await _app._watchdog_restart_mode("restart-dead", state, "away", wlog)
        finally:
            _app._away_mode_state.pop("restart-dead", None)

        assert state["enabled"] is False

    @pytest.mark.asyncio
    async def test_off_during_restart_blocks_later_keys_and_resurrection(
        self, monkeypatch, owned_autopush_runtime
    ):
        import logging

        name = "restart-off-race"
        state = {
            "enabled": True, "log": [], "started_at": 0.0,
            "step": 0, "task": None,
        }
        binding = owned_autopush_runtime(name, state)
        first_key_sent = asyncio.Event()
        release_restart = asyncio.Event()
        key_calls = []

        async def fenced_batch(session_name, commands, *, expected_binding=None):
            assert expected_binding == binding
            async with app_module._autopush_action_lock(session_name):
                if app_module._get_autopush_mode(session_name) == "off":
                    return False
                key_calls.extend(commands)
                first_key_sent.set()
                return True

        async def controlled_sleep(seconds):
            if seconds == 3:
                await asyncio.wait_for(release_restart.wait(), timeout=2)

        app_module._away_mode_state[name] = state
        app_module._autopush_mode[name] = "basic"
        app_module._autopush_action_locks.pop(name, None)
        monkeypatch.setattr(app_module, "_autopush_tmux_batch", fenced_batch)
        monkeypatch.setattr(app_module.asyncio, "sleep", controlled_sleep)
        monkeypatch.setattr(app_module, "_save_autopush_mode", MagicMock())
        monkeypatch.setattr(app_module, "_save_autonomous_state", MagicMock())
        ensure = AsyncMock(return_value=True)
        monkeypatch.setattr(app_module, "_ensure_codex_running", ensure)

        restarting = asyncio.create_task(app_module._watchdog_restart_mode(
            name, state, "away", logging.getLogger("watchdog-test")
        ))
        try:
            await asyncio.wait_for(first_key_sent.wait(), timeout=2)
            setting = await app_module._controller_dispatch({
                "op": "autopush_set", "session": name, "mode": "off",
            })
            release_restart.set()
            await asyncio.wait_for(restarting, timeout=2)
            assert setting["ok"] is True
            assert len(key_calls) == 1
            assert state["enabled"] is False
            ensure.assert_not_awaited()
        finally:
            release_restart.set()
            if not restarting.done():
                restarting.cancel()
            await asyncio.gather(restarting, return_exceptions=True)
            app_module._away_mode_state.pop(name, None)
            app_module._autopush_mode.pop(name, None)
            app_module._autopush_action_locks.pop(name, None)
