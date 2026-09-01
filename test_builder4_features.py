from pathlib import Path
from types import SimpleNamespace

import pytest


def test_session_lifecycle_registration_is_owner_and_generation_bound(tmp_path: Path):
    from runtime_control import SessionLifecycleStore

    lifecycle = SessionLifecycleStore(tmp_path / "session-lifecycle.json")
    row = lifecycle.register_active(
        "drafting",
        cwd="/srv/drafting",
        owner_id="admin",
        resume_uuid="12345678-1234-1234-1234-123456789abc",
    )

    assert row["owner_id"] == "admin"
    assert row["generation"]
    assert row["resume_uuid"] == "12345678-1234-1234-1234-123456789abc"
    with pytest.raises(ValueError, match="owner changed"):
        lifecycle.checkpoint_active(
            "drafting",
            cwd="/srv/drafting",
            owner_id="someone-else",
            expected_generation=row["generation"],
        )


def test_session_lifecycle_delete_intent_cannot_remove_a_new_generation(tmp_path: Path):
    from runtime_control import SessionLifecycleStore

    lifecycle = SessionLifecycleStore(tmp_path / "session-lifecycle.json")
    first = lifecycle.register_active(
        "drafting",
        cwd="/srv/first",
        owner_id="admin",
    )
    deleting = lifecycle.begin_transition(
        "drafting",
        owner_id="admin",
        desired_state="deleting",
        expected_generation=first["generation"],
        expected_desired_states={"running"},
    )
    second = lifecycle.register_active(
        "drafting",
        cwd="/srv/second",
        owner_id="admin",
    )

    assert deleting["desired_state"] == "deleting"
    assert second["generation"] != first["generation"]
    assert not lifecycle.remove(
        "drafting",
        expected_generation=first["generation"],
        owner_id="admin",
    )
    assert lifecycle.matches(
        "drafting",
        generation=second["generation"],
        owner_id="admin",
        desired_states={"running"},
        restore_on_startup=True,
    )


def test_agent_scope_limits_only_builder4_workloads():
    from runtime_control import scoped_agent_command

    command = scoped_agent_command(
        "drafting",
        "claude --dangerously-skip-permissions --resume 1234",
        slice_name="builder4-agents.slice",
        aggregate_cpu_quota_percent=400,
    )

    assert "systemd-run" in command
    assert "builder4-agents.slice" in command
    assert "CPUQuota=400%" in command
    assert "MemoryHigh=6144M" in command
    assert "MemoryMax=8192M" in command
    assert "exec nice -n 5 claude" in command


def test_managed_agent_injects_the_host_pytest_gate(monkeypatch, tmp_path: Path):
    from runtime_control import build_pytest_gate_env_prefix

    monkeypatch.setenv("PYTEST_PLUGINS", "existing_plugin")
    prefix = build_pytest_gate_env_prefix(tmp_path, account="builder4")

    assert "TMUX_DASH_PYTEST_GATE_REQUIRED=1" in prefix
    assert "TMUX_DASH_PYTEST_ACCOUNT=builder4" in prefix
    assert "tmux_dashboard_pytest_gate,existing_plugin" in prefix
    assert str(tmp_path.resolve()) in prefix


def test_pytest_gate_rejects_a_user_mutable_host_lock(tmp_path: Path):
    from runtime_hooks.tmux_dashboard_pytest_gate import _open_trusted_host_lock

    lock = tmp_path / "pytest-heavy.lock"
    lock.write_text("")

    with pytest.raises(PermissionError, match="root-owned"):
        _open_trusted_host_lock(lock)


def test_tmpfiles_config_recreates_the_trusted_pytest_lock_after_reboot():
    config = Path(__file__).parent / "runtime_hooks" / "builder4-pytest.conf"
    lines = {
        line.strip()
        for line in config.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert "d /run/lock/builder4 0750 root nimrod_rotem -" in lines
    assert "f /run/lock/builder4/pytest-heavy.lock 0440 root nimrod_rotem -" in lines
    assert "z /run/lock/builder4/pytest-heavy.lock 0440 root nimrod_rotem -" in lines


def test_claude_launch_is_scoped_and_inherits_the_pytest_gate(monkeypatch):
    import app

    monkeypatch.setattr(app, "PYTEST_PLUGIN_DIR", Path("/opt/builder4-hooks"))
    command = app._managed_agent_command(
        "drafting",
        "claude --dangerously-skip-permissions",
    )

    assert "builder4-agents.slice" in command
    assert "PYTEST_PLUGINS=tmux_dashboard_pytest_gate" in command
    assert "/opt/builder4-hooks" in command
    assert "claude --dangerously-skip-permissions" in command


def test_claude_relaunch_uses_the_same_managed_scope(monkeypatch):
    import app

    captured: dict[str, list[str]] = {}
    monkeypatch.setattr(
        app,
        "_managed_agent_command",
        lambda name, command: f"SCOPED[{name}] {command}",
    )
    monkeypatch.setattr(app, "_session_owner_name", lambda _name: "Nimo")
    monkeypatch.setattr(app, "_launch_banner", lambda *_args: "banner")
    monkeypatch.setattr(
        app,
        "_launch_script_line",
        lambda name, lines, banner: captured.update({"lines": list(lines)}) or "source launch.sh",
    )

    result = app._relaunch_line(
        "drafting",
        "claude --dangerously-skip-permissions --resume 1234",
    )

    assert result == "source launch.sh"
    assert captured["lines"][-1].startswith("SCOPED[drafting] claude")


def test_exact_tmux_create_uses_the_id_printed_by_tmux(monkeypatch):
    import app

    calls: list[list[str]] = []

    def fake_run(argv, **_kwargs):
        calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="$17\tdrafting\n", stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    created_id, created_name = app._create_exact_tmux_session(
        "drafting",
        "/srv/drafting",
    )

    assert (created_id, created_name) == ("$17", "drafting")
    assert calls == [[
        "tmux", "new-session", "-d", "-P", "-F",
        "#{session_id}\t#{session_name}", "-s", "drafting", "-c", "/srv/drafting",
    ]]


def test_exact_tmux_lookup_rejects_a_recycled_name(monkeypatch):
    import app

    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=0,
            stdout="$22\tnot-drafting\n",
            stderr="",
        ),
    )

    with pytest.raises(RuntimeError, match="identity changed"):
        app._exact_tmux_session_id("drafting")


def test_tmux_mutation_fence_blocks_a_second_operation(monkeypatch, tmp_path: Path):
    import app

    monkeypatch.setattr(app, "TMUX_MUTATION_LOCK", tmp_path / "tmux-mutation.lock")
    first = app._acquire_tmux_mutation_fd(timeout=0.1)
    try:
        with pytest.raises(TimeoutError, match="tmux mutation lock"):
            app._acquire_tmux_mutation_fd(timeout=0.02)
    finally:
        app._release_tmux_mutation_fd(first)


def test_recovery_command_resumes_the_exact_claude_conversation(monkeypatch):
    import app

    monkeypatch.setattr(app, "NEW_SESSION_CMD", "claude --dangerously-skip-permissions --continue")
    monkeypatch.setattr(
        app,
        "_claude_cmd_with_flags",
        lambda command, pin_model=True: (command + " --model opus --effort high", "opus", "high"),
    )
    monkeypatch.setattr(
        app,
        "_managed_agent_command",
        lambda name, command: f"SCOPED[{name}] {command}",
    )

    command = app._claude_recovery_command(
        "drafting",
        "12345678-1234-1234-1234-123456789abc",
    )

    assert command.startswith("SCOPED[drafting] claude")
    assert "--resume 12345678-1234-1234-1234-123456789abc" in command
    assert "--continue" not in command
    assert "--session-id" not in command


@pytest.mark.asyncio
async def test_new_session_launches_claude_inside_the_managed_scope(monkeypatch):
    import app

    captured: dict[str, list[str]] = {}
    lifecycle_calls: list[tuple[str, str, dict]] = []

    class FakeLifecycle:
        def register_active(self, name, **kwargs):
            lifecycle_calls.append(("register", name, kwargs))
            return {"generation": "generation-1", **kwargs}

        def checkpoint_active(self, name, **kwargs):
            lifecycle_calls.append(("checkpoint", name, kwargs))
            return {"generation": "generation-1", **kwargs}

        def remove(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(app, "_current_user", lambda _request: {"id": "admin", "username": "Nimo", "role": "admin"})
    monkeypatch.setattr(app, "get_tmux_sessions", lambda: [])
    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())
    monkeypatch.setattr(app, "_acquire_tmux_mutation_fd", lambda _timeout: 99)
    monkeypatch.setattr(app, "_release_tmux_mutation_fd", lambda _fd: None)
    monkeypatch.setattr(app, "_create_exact_tmux_session", lambda name, cwd: ("$17", name))
    monkeypatch.setattr(app, "_session_cwd", lambda _target: "/srv/drafting")
    monkeypatch.setattr(app, "_set_session_owner", lambda *_args: None)
    monkeypatch.setattr(app, "_set_session_convo", lambda *_args: None)
    monkeypatch.setattr(app, "_session_convo", lambda *_args: "12345678-1234-1234-1234-123456789abc")
    monkeypatch.setattr(app, "_git_identity_for", lambda *_args: ("Nimo", "Nimo@grabo.tech"))
    monkeypatch.setattr(app, "NEW_SESSION_CMD", "claude --dangerously-skip-permissions")
    monkeypatch.setattr(
        app,
        "_managed_agent_command",
        lambda name, command: f"SCOPED[{name}] {command}",
    )
    monkeypatch.setattr(
        app,
        "_launch_script_line",
        lambda name, boot, banner: captured.update({"boot": list(boot)}) or "",
    )
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    response = await app.api_create_session(object(), app.CreateSession(name="drafting"))

    assert response.status_code == 200
    assert captured["boot"][-1].startswith("SCOPED[drafting] claude")
    assert lifecycle_calls[0] == (
        "register",
        "drafting",
        {
            "cwd": "/srv/drafting",
            "owner_id": "admin",
            "resume_uuid": "",
        },
    )
    assert lifecycle_calls[-1][0:2] == ("checkpoint", "drafting")
    assert lifecycle_calls[-1][2]["expected_generation"] == "generation-1"


@pytest.mark.asyncio
async def test_delete_marks_intent_and_kills_only_the_exact_tmux_id(monkeypatch):
    import app

    lifecycle_calls: list[tuple[str, dict]] = []
    tmux_calls: list[list[str]] = []

    class FakeLifecycle:
        def get(self, name):
            assert name == "drafting"
            return {
                "generation": "generation-1",
                "owner_id": "admin",
                "desired_state": "running",
            }

        def begin_transition(self, name, **kwargs):
            lifecycle_calls.append(("begin", kwargs))
            return {"generation": "generation-1", **kwargs}

        def matches(self, *_args, **_kwargs):
            return True

        def remove(self, name, **kwargs):
            lifecycle_calls.append(("remove", kwargs))
            return True

    def fake_run(argv, **_kwargs):
        tmux_calls.append(list(argv))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(app, "_current_user", lambda _request: {"id": "admin", "role": "admin"})
    monkeypatch.setattr(
        app,
        "_find_session_for_user",
        lambda *_args: ([], {"name": "drafting", "cwd": "/srv/drafting"}),
    )
    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())
    monkeypatch.setattr(app, "_acquire_tmux_mutation_fd", lambda _timeout: 99)
    monkeypatch.setattr(app, "_release_tmux_mutation_fd", lambda _fd: None)
    monkeypatch.setattr(app, "_exact_tmux_session_id", lambda _name: "$17")
    monkeypatch.setattr(app, "_clear_session_owner", lambda *_args: None)
    monkeypatch.setattr(app, "_clear_session_convo", lambda *_args: None)
    monkeypatch.setattr(app, "_load_roles", lambda: {"session_profiles": {}})
    monkeypatch.setattr(app.subprocess, "run", fake_run)

    response = await app.api_delete_session(object(), "drafting")

    assert response.status_code == 200
    assert ["tmux", "kill-session", "-t", "$17"] in tmux_calls
    assert lifecycle_calls[0] == (
        "begin",
        {
            "owner_id": "admin",
            "desired_state": "deleting",
            "expected_generation": "generation-1",
            "expected_desired_states": {"running"},
        },
    )
    assert lifecycle_calls[-1] == (
        "remove",
        {"expected_generation": "generation-1", "owner_id": "admin"},
    )


def test_durable_restore_recreates_the_exact_claude_conversation(monkeypatch, tmp_path: Path):
    import app

    sent: list[list[str]] = []
    checkpoints: list[dict] = []
    resume_uuid = "12345678-1234-1234-1234-123456789abc"

    class FakeLifecycle:
        def matches(self, *_args, **_kwargs):
            return True

        def checkpoint_active(self, _name, **kwargs):
            checkpoints.append(kwargs)
            return kwargs

    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())
    monkeypatch.setattr(
        app,
        "_find_user_by_id",
        lambda owner: {"id": owner, "username": "Nimo", "role": "admin"},
    )
    monkeypatch.setattr(app, "_create_exact_tmux_session", lambda name, cwd: ("$23", name))
    monkeypatch.setattr(app, "_set_session_owner", lambda *_args: None)
    monkeypatch.setattr(app, "_set_session_convo", lambda *_args: None)
    monkeypatch.setattr(app, "_git_identity_for", lambda *_args: ("Nimo", "Nimo@grabo.tech"))
    monkeypatch.setattr(app, "_claude_recovery_command", lambda name, uuid: f"SCOPED[{name}] --resume {uuid}")
    monkeypatch.setattr(app, "_claude_launch_env_prefix", lambda: "unset ANTHROPIC_API_KEY; ")
    monkeypatch.setattr(app, "_launch_banner", lambda *_args: "restored")
    monkeypatch.setattr(app, "_launch_script_line", lambda *_args: "source launch.sh")
    monkeypatch.setattr(app, "_session_cwd", lambda _target: str(tmp_path))
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda argv, **_kwargs: sent.append(list(argv)) or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = app._restore_durable_session(
        {
            "name": "drafting",
            "generation": "generation-1",
            "owner_id": "admin",
            "cwd": str(tmp_path),
            "resume_uuid": resume_uuid,
            "desired_state": "running",
            "restore_on_startup": True,
        }
    )

    assert result == {"name": "drafting", "status": "restored", "tmux_id": "$23"}
    assert ["tmux", "send-keys", "-t", "$23", "-l", "source launch.sh"] in sent
    assert ["tmux", "send-keys", "-t", "$23", "Enter"] in sent
    assert checkpoints[-1]["resume_uuid"] == resume_uuid
    assert checkpoints[-1]["expected_generation"] == "generation-1"


def test_durable_restore_rejects_an_owner_registry_mismatch(monkeypatch, tmp_path: Path):
    import app

    resume_uuid = "12345678-1234-1234-1234-123456789abc"

    class FakeLifecycle:
        def matches(self, *_args, **_kwargs):
            return True

    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())
    monkeypatch.setattr(
        app,
        "_find_user_by_id",
        lambda owner: {"id": owner, "username": "Nimo", "role": "admin"},
    )
    monkeypatch.setattr(app, "_session_owner_id", lambda _name: "someone-else")
    monkeypatch.setattr(
        app,
        "_create_exact_tmux_session",
        lambda *_args: (_ for _ in ()).throw(AssertionError("tmux must not be mutated")),
    )

    with pytest.raises(ValueError, match="owner binding"):
        app._restore_durable_session(
            {
                "name": "drafting",
                "generation": "generation-1",
                "owner_id": "admin",
                "cwd": str(tmp_path),
                "resume_uuid": resume_uuid,
                "desired_state": "running",
                "restore_on_startup": True,
            }
        )


def test_live_sessions_are_checkpointed_with_owner_and_conversation(monkeypatch):
    import app

    registrations: list[tuple[str, dict]] = []

    class FakeLifecycle:
        def get(self, _name):
            return {}

        def register_active(self, name, **kwargs):
            registrations.append((name, kwargs))
            return {"generation": "generation-1", **kwargs}

    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())
    monkeypatch.setattr(
        app,
        "get_tmux_sessions",
        lambda: [{"name": "drafting", "cwd": "/srv/drafting"}],
    )
    monkeypatch.setattr(app, "_session_owner_id", lambda _name: "admin")
    monkeypatch.setattr(
        app,
        "_session_convo",
        lambda _name: "12345678-1234-1234-1234-123456789abc",
    )

    assert app._checkpoint_live_sessions() == 1
    assert registrations == [
        (
            "drafting",
            {
                "cwd": "/srv/drafting",
                "owner_id": "admin",
                "resume_uuid": "12345678-1234-1234-1234-123456789abc",
                "source": "live-checkpoint",
            },
        )
    ]


def test_durable_candidates_exclude_live_and_intentionally_deleted_sessions(monkeypatch):
    import app

    class FakeLifecycle:
        def snapshot(self):
            return {
                "sessions": {
                    "live": {
                        "managed": True,
                        "generation": "g-live",
                        "owner_id": "admin",
                        "desired_state": "running",
                        "restore_on_startup": True,
                    },
                    "missing": {
                        "managed": True,
                        "generation": "g-missing",
                        "owner_id": "admin",
                        "desired_state": "running",
                        "restore_on_startup": True,
                    },
                    "deleted": {
                        "managed": True,
                        "generation": "g-deleted",
                        "owner_id": "admin",
                        "desired_state": "deleting",
                        "restore_on_startup": False,
                    },
                }
            }

    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())

    candidates = app._durable_session_candidates({"live"})

    assert [row["name"] for row in candidates] == ["missing"]


@pytest.mark.asyncio
async def test_admin_can_trigger_durable_recovery(monkeypatch):
    import app

    monkeypatch.setattr(app, "_current_user", lambda _request: {"id": "admin", "role": "admin"})

    async def fake_reconcile():
        return {
            "checkpointed": 2,
            "healthy": 3,
            "restored": ["drafting"],
            "failed": [],
        }

    monkeypatch.setattr(app, "_reconcile_durable_sessions", fake_reconcile)

    response = await app.api_admin_recover_sessions(object())

    assert response.status_code == 200
    assert b'"restored":["drafting"]' in response.body


def test_a_spaced_session_name_is_shown_but_never_given_to_tmux(monkeypatch, tmp_path: Path):
    import app
    from runtime_control import LockedJsonStore

    store = LockedJsonStore(
        tmp_path / "session-display-names.json",
        lambda: {"version": 1, "sessions": {}},
    )
    monkeypatch.setattr(app, "SESSION_DISPLAY_NAMES", store)

    assert app._split_session_name("  word1   word2 ") == ("word1word2", "word1 word2")
    assert app._split_session_name("plain") == ("plain", "plain")
    assert app._split_session_name("bad;rm -rf /") == ("", "")

    app._set_session_display_name("word1word2", "word1 word2")
    assert app._session_display_name("word1word2") == "word1 word2"
    # A name with no spaces needs no row, and an unknown session is its own name.
    app._set_session_display_name("plain", "plain")
    assert store.read()["sessions"] == {
        "word1word2": store.read()["sessions"]["word1word2"]
    }
    assert app._session_display_name("plain") == "plain"

    # A recycled tmux name must not inherit a stale display name.
    app._set_session_display_name("word1word2", "other name")
    assert app._session_display_name("word1word2") == "word1word2"

    app._remove_session_display_name("word1word2")
    assert store.read()["sessions"] == {}


def test_clipboard_images_become_sendable_composer_attachments():
    import app

    html = app.HTML_PAGE
    paste_start = html.index("function handleComposerPaste(event,name,tab)")
    paste_end = html.index("function handleDrop(event,name,tab)", paste_start)
    paste = html[paste_start:paste_end]

    assert html.count('onpaste="handleComposerPaste(event,') == 2
    assert html.count('placeholder="Send a message or paste an image..."') == 1
    assert html.count('placeholder="Type a command or paste an image..."') == 1
    assert "item.kind==='file'" in html
    assert ".startsWith('image/')" in html
    assert "new File([blob],filename" in html
    assert "await _uploadOneFile(name,tab,file)" in paste
    assert "reader.readAsDataURL(file)" in html
    assert paste.index("if(!blobs.length)return") < paste.index("event.preventDefault()")


def test_session_and_view_are_deep_linked_through_the_hash():
    import app

    html = app.HTML_PAGE

    assert "function _sessionRouteHash(name,tab)" in html
    assert "function applySessionRoute()" in html
    assert "window.addEventListener('hashchange',applySessionRoute)" in html
    assert "history.pushState(null,'',_sessionRouteHash(name,tab))" in html


def test_a_session_tab_shows_the_typed_name_and_no_two_word_label():
    import app

    html = app.HTML_PAGE

    assert "function sessionLabel(sessionOrName)" in html
    assert '<span class="nav-session-id">${esc(sessionLabel(s))}</span>' in html
    # The two-word label that used to sit beside the name is gone for good.
    assert "tab_label" not in html
    assert "nav-title" not in html


def test_nav_status_includes_memory_and_admin_recovery_control():
    import app

    html = app.HTML_PAGE

    assert "RAM <span class=\"stat-val '+memClass+'\">'+memPct+'%</span>" in html
    assert "onclick=\"recoverSessions();closeToolsMenu()\"" in html
    assert "async function recoverSessions()" in html
    assert "BASE+'/api/admin/sessions/recover'" in html


def _assistant(effort, model, ts, ctx=(10, 20, 30), ttl_1h=0, ttl_5m=0, sidechain=False):
    import json

    return json.dumps({
        "type": "assistant",
        "effort": effort,
        "isSidechain": sidechain,
        "timestamp": ts,
        "message": {
            "model": model,
            "usage": {
                "input_tokens": ctx[0],
                "cache_read_input_tokens": ctx[1],
                "cache_creation_input_tokens": ctx[2],
                "cache_creation": {
                    "ephemeral_1h_input_tokens": ttl_1h,
                    "ephemeral_5m_input_tokens": ttl_5m,
                },
            },
        },
    })


def test_a_subagents_effort_is_never_read_as_the_sessions_own():
    import app

    lines = [
        _assistant("high", "claude-opus-5", "2026-09-01T00:00:00.000Z", ttl_1h=99),
        # A sub-agent turn lands in the SAME transcript and runs with its own
        # model, effort and context. Reading "the last assistant entry" made it
        # the session's.
        _assistant("low", "claude-haiku-4-5", "2026-09-01T00:01:00.000Z",
                   ctx=(1, 2, 3), sidechain=True),
    ]
    facts = app._scan_assistant_tail(lines)

    assert facts["effort"] == "high"
    assert facts["model"] == "claude-opus-5"
    assert facts["ctx"] == 60
    assert facts["ttl"] == 3600


def test_context_and_end_time_come_off_the_newest_reply():
    import app

    lines = [
        _assistant("high", "claude-opus-5", "2026-09-01T00:00:00.000Z", ctx=(1, 1, 1)),
        _assistant("max", "claude-opus-5", "2026-09-01T00:05:00.000Z",
                   ctx=(100, 200, 300), ttl_5m=7),
    ]
    facts = app._scan_assistant_tail(lines)

    assert facts["effort"] == "max"
    assert facts["ctx"] == 600
    assert facts["ttl"] == 300           # a 5m cache, read rather than assumed
    assert facts["ts"] == app._iso_epoch("2026-09-01T00:05:00.000Z")


def test_a_transcript_tail_grows_until_it_holds_a_reply(tmp_path):
    import app

    # One enormous tool result between the newest reply and the end of the file:
    # the old fixed 32KB window held none of the reply, so the caller fell back
    # to the configured default and mislabelled the session.
    p = tmp_path / "t.jsonl"
    filler = '{"type":"user","content":"' + ("x" * 200_000) + '"}'
    p.write_text("\n".join([
        _assistant("high", "claude-opus-5", "2026-09-01T00:00:00.000Z", ttl_1h=5),
        filler,
    ]))

    facts = app._last_assistant_facts(str(p))

    assert facts["effort"] == "high"
    assert facts["model"] == "claude-opus-5"


def test_a_printed_effort_line_is_not_a_switch(monkeypatch):
    import app

    # An agent that merely PRINTS the phrase (grepping app.py does) must not be
    # read as having run /effort. Only the CLI's own result elbow counts.
    monkeypatch.setattr(app, "capture_pane_recent",
                        lambda name, lines=80: 'grep -n "set effort level to low" app.py\n')
    assert app._pane_model_effort("s") == {}

    monkeypatch.setattr(app, "capture_pane_recent", lambda name, lines=80: (
        "  ⎿  Set effort level to high (this session only)\n"
        "  ⎿  Set effort level to max (this session only): Maximum capability\n"))
    # The LAST confirmation wins: it is the one that is still in force.
    assert app._pane_model_effort("s")["effort"] == "max"


def test_the_live_bar_carries_silence_and_context():
    import app

    html = app.HTML_PAGE

    assert 'class="tl-since" id="tl-since-${s.name}"' in html
    assert 'class="tl-ctx" id="tl-ctx-${s.name}"' in html
    assert "function _paintIdleSince(name)" in html
    assert "function _paintContext(name)" in html
    # The silence counter has to keep ticking while the session is idle.
    assert "_paintIdleSince(selectedSession);" in html
    # And the bar shows on server-measured facts alone, for a session that was
    # already idle before the page loaded and has no spinner row left.
    assert "!!(sess.context_tokens||sess.last_turn_end)" in html


def test_your_own_words_are_marked_and_jumpable():
    import app

    html = app.HTML_PAGE

    assert "function _markUserRows(rows)" in html
    assert "function jumpToLastUserMessage(name)" in html
    assert "function jumpToLive(name)" in html
    assert '<span class="tl-you">' in html
    assert ".raw-output .tl-you{" in html
    assert 'onclick="jumpToLastUserMessage(' in html


def test_settled_scrollback_is_archived_without_duplicating(monkeypatch, tmp_path):
    import app

    # A pane whose settled region grows, exactly as tmux reports it.
    pane = {"lines": [f"line {i}" for i in range(120)]}
    monkeypatch.setattr(app, "SCROLLBACK_DIR", tmp_path)
    monkeypatch.setattr(app, "_settled_pane_lines",
                        lambda name, count: pane["lines"][-count:])
    app._scrollback_state.clear()

    assert app.archive_scrollback("s") == 120
    # Nothing new: a second pass must add nothing, or the log doubles every poll.
    assert app.archive_scrollback("s") == 0

    pane["lines"] += [f"line {i}" for i in range(120, 150)]
    assert app.archive_scrollback("s") == 30

    stored = (tmp_path / "s.log").read_text().splitlines()
    assert stored == [f"line {i}" for i in range(150)]


def test_the_archive_keeps_what_the_tmux_ring_has_already_dropped(monkeypatch, tmp_path):
    import app

    # tmux keeps a fixed ring: here the last 100 lines. The archive has to hold
    # everything the ring has already thrown away, which is the whole point of
    # it. It can, as long as it is read again before its anchor scrolls out.
    all_lines = [f"line {i}" for i in range(300)]
    ring, seen = 100, {"upto": 120}
    monkeypatch.setattr(app, "SCROLLBACK_DIR", tmp_path)
    monkeypatch.setattr(
        app, "_settled_pane_lines",
        lambda name, count: all_lines[max(0, seen["upto"] - ring):seen["upto"]][-count:])
    app._scrollback_state.clear()

    app.archive_scrollback("s")
    for upto in (180, 240, 300):     # the ring drops 60 lines between passes
        seen["upto"] = upto
        app.archive_scrollback("s")

    stored = (tmp_path / "s.log").read_text().splitlines()
    assert stored == all_lines[20:]          # 0..19 predate the first read
    assert app.read_scrollback("s", 0, 5)["lines"] == all_lines[20:25]
    assert app.read_scrollback("s", 0, 5)["total"] == 280


def test_a_pane_that_moved_past_the_anchor_resyncs_instead_of_duplicating(monkeypatch, tmp_path):
    import app

    # Poll too slowly and the ring drops the anchor. Nothing can bring those
    # lines back, but the archive must not silently glue unrelated stretches
    # together either: it marks the break and carries on.
    monkeypatch.setattr(app, "SCROLLBACK_DIR", tmp_path)
    pane = {"lines": [f"old {i}" for i in range(60)]}
    monkeypatch.setattr(app, "_settled_pane_lines",
                        lambda name, count: pane["lines"][-count:])
    app._scrollback_state.clear()
    app.archive_scrollback("s")

    pane["lines"] = [f"new {i}" for i in range(60)]      # nothing in common
    added = app.archive_scrollback("s")

    stored = (tmp_path / "s.log").read_text().splitlines()
    assert added == 60
    assert stored[:60] == [f"old {i}" for i in range(60)]
    assert stored[60] == ""                              # the break marker
    assert stored[61:] == [f"new {i}" for i in range(60)]


def test_terminal_can_load_history_from_before_the_ring(monkeypatch):
    import app

    html = app.HTML_PAGE

    assert "async function loadEarlierHistory(name)" in html
    assert "function _trimOverlap(older,current)" in html
    assert "function updateOlderBar(name)" in html
    assert "/history?before=" in html
    assert 'id="raw-older-${s.name}"' in html
    # The head trim must make room for history that was deliberately loaded.
    assert "const cap=RAW_MAX_LINES+olderRows;" in html
