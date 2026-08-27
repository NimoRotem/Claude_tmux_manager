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
    monkeypatch.setattr(app, "_convo_has_transcript", lambda *_args: True)

    command = app._claude_recovery_command(
        "drafting",
        "12345678-1234-1234-1234-123456789abc",
    )

    assert command.startswith("SCOPED[drafting] claude")
    assert "--resume 12345678-1234-1234-1234-123456789abc" in command
    assert "--continue" not in command
    assert "--session-id" not in command


def test_recovery_command_creates_a_conversation_that_was_never_written(monkeypatch):
    """`--resume` on a conversation with no transcript aborts Claude and leaves
    the pane at a shell, which reads as a logout. Same id, creating flag."""
    import app

    monkeypatch.setattr(app, "NEW_SESSION_CMD", "claude --dangerously-skip-permissions")
    monkeypatch.setattr(
        app,
        "_claude_cmd_with_flags",
        lambda command, pin_model=True: (command, "opus", "high"),
    )
    monkeypatch.setattr(
        app,
        "_managed_agent_command",
        lambda name, command: f"SCOPED[{name}] {command}",
    )
    monkeypatch.setattr(app, "_convo_has_transcript", lambda *_args: False)

    command = app._claude_recovery_command(
        "drafting",
        "12345678-1234-1234-1234-123456789abc",
    )

    assert "--session-id 12345678-1234-1234-1234-123456789abc" in command
    assert "--resume" not in command


def test_convo_has_transcript_reads_the_sessions_own_config_dir(monkeypatch, tmp_path: Path):
    import app

    convo = "12345678-1234-1234-1234-123456789abc"
    proj = tmp_path / "projects" / "-srv-drafting"
    proj.mkdir(parents=True)
    monkeypatch.setattr(app, "_session_config_base", lambda _name: tmp_path)

    assert not app._convo_has_transcript("drafting", convo)
    (proj / (convo + ".jsonl")).write_text("{}\n")
    assert app._convo_has_transcript("drafting", convo)
    assert not app._convo_has_transcript("drafting", "not-a-uuid")


def test_resume_flag_never_resumes_a_conversation_with_no_transcript(monkeypatch):
    import app

    convo = "12345678-1234-1234-1234-123456789abc"
    monkeypatch.setattr(app, "_session_convo", lambda _name: convo)
    monkeypatch.setattr(
        app,
        "_find_session_transcript_uuid",
        lambda *_args: (_ for _ in ()).throw(AssertionError("must not guess")),
    )

    monkeypatch.setattr(app, "_convo_has_transcript", lambda *_args: True)
    assert app._resume_flag("drafting") == "--resume " + convo

    monkeypatch.setattr(app, "_convo_has_transcript", lambda *_args: False)
    assert app._resume_flag("drafting") == "--session-id " + convo


def test_a_launch_that_aborts_on_its_conversation_counts_as_a_crash():
    import app

    assert app._looks_like_crash(
        "No conversation found with session ID: 1bd36661-ef36-4fd5-8223-4a0c5776634f"
    )
    assert app._looks_like_crash(
        "Session ID 1bd36661-ef36-4fd5-8223-4a0c5776634f is already in use"
    )
    assert not app._looks_like_crash("nimrod_rotem@lisa-claude:~/lisa-my$ ")


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


def test_durable_restore_mints_a_conversation_when_the_row_has_none(monkeypatch, tmp_path: Path):
    """A row whose tab died before Claude wrote a conversation used to fail the
    reconcile loop every 20s forever and never come back."""
    import app

    checkpoints: list[dict] = []
    launched: list[str] = []
    matched: list[dict] = []

    class FakeLifecycle:
        def matches(self, _name, **kwargs):
            matched.append(kwargs)
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
    monkeypatch.setattr(app, "_session_owner_id", lambda _name: "admin")
    monkeypatch.setattr(app, "_create_exact_tmux_session", lambda name, cwd: ("$24", name))
    monkeypatch.setattr(app, "_set_session_owner", lambda *_args: None)
    monkeypatch.setattr(app, "_set_session_convo", lambda *_args: None)
    monkeypatch.setattr(app, "_git_identity_for", lambda *_args: ("Nimo", "nimo@lisa.my"))
    monkeypatch.setattr(
        app,
        "_claude_recovery_command",
        lambda name, convo: launched.append(convo) or f"SCOPED[{name}] --session-id {convo}",
    )
    monkeypatch.setattr(app, "_claude_launch_env_prefix", lambda: "unset ANTHROPIC_API_KEY; ")
    monkeypatch.setattr(app, "_launch_banner", lambda *_args: "restored")
    monkeypatch.setattr(app, "_launch_script_line", lambda *_args: "source launch.sh")
    monkeypatch.setattr(app, "_session_cwd", lambda _target: str(tmp_path))
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda argv, **_kwargs: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )

    result = app._restore_durable_session(
        {
            "name": "authsetup",
            "generation": "generation-1",
            "owner_id": "admin",
            "cwd": str(tmp_path),
            "resume_uuid": "",
            "desired_state": "running",
            "restore_on_startup": True,
        }
    )

    assert result["status"] == "restored"
    # The binding check still asks about the row as stored, not the minted id.
    assert all(row["resume_uuid"] == "" for row in matched)
    minted = launched[0]
    assert app._UUID_RE.fullmatch(minted)
    assert checkpoints[-1]["resume_uuid"] == minted


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
    monkeypatch.setattr(app, "_convo_has_transcript", lambda *_args: True)

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


def test_checkpoint_corrects_a_recorded_conversation_that_was_never_written(monkeypatch):
    """A recorded id with no transcript points at nothing: the pane must be in
    some other conversation, so checkpointing it would strand the next restore."""
    import app

    registrations: list[tuple[str, dict]] = []
    stale = "12345678-1234-1234-1234-123456789abc"
    live = "abcdef12-1234-1234-1234-123456789abc"

    class FakeLifecycle:
        def get(self, _name):
            return {}

        def register_active(self, name, **kwargs):
            registrations.append((name, kwargs))
            return {"generation": "generation-1", **kwargs}

    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())
    monkeypatch.setattr(app, "get_tmux_sessions", lambda: [{"name": "drafting", "cwd": "/srv/drafting"}])
    monkeypatch.setattr(app, "_session_owner_id", lambda _name: "admin")
    monkeypatch.setattr(app, "_session_convo", lambda _name: stale)
    monkeypatch.setattr(app, "_convo_has_transcript", lambda *_args: False)
    monkeypatch.setattr(app, "_clear_session_convo", lambda _name: None)
    monkeypatch.setattr(app, "_set_session_convo", lambda *_args: None)
    monkeypatch.setattr(app, "_learn_session_convo", lambda _name: live)

    assert app._checkpoint_live_sessions() == 1
    assert registrations[-1][1]["resume_uuid"] == live


def test_checkpoint_keeps_a_stale_id_when_the_pane_offers_nothing_better(monkeypatch):
    import app

    registrations: list[tuple[str, dict]] = []
    restored: list[tuple] = []
    stale = "12345678-1234-1234-1234-123456789abc"

    class FakeLifecycle:
        def get(self, _name):
            return {}

        def register_active(self, name, **kwargs):
            registrations.append((name, kwargs))
            return {"generation": "generation-1", **kwargs}

    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())
    monkeypatch.setattr(app, "get_tmux_sessions", lambda: [{"name": "drafting", "cwd": "/srv/drafting"}])
    monkeypatch.setattr(app, "_session_owner_id", lambda _name: "admin")
    monkeypatch.setattr(app, "_session_convo", lambda _name: stale)
    monkeypatch.setattr(app, "_convo_has_transcript", lambda *_args: False)
    monkeypatch.setattr(app, "_clear_session_convo", lambda _name: None)
    monkeypatch.setattr(app, "_set_session_convo", lambda *args: restored.append(args))
    monkeypatch.setattr(app, "_learn_session_convo", lambda _name: "")

    assert app._checkpoint_live_sessions() == 1
    assert registrations[-1][1]["resume_uuid"] == stale
    assert restored == [("drafting", stale)]


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


def test_tab_labels_are_two_words_persistent_and_ignore_continue(monkeypatch, tmp_path: Path):
    import app
    from runtime_control import LockedJsonStore

    store = LockedJsonStore(
        tmp_path / "session-tab-labels.json",
        lambda: {"version": 1, "sessions": {}},
    )
    monkeypatch.setattr(app, "SESSION_TAB_LABELS", store)

    first = app._set_session_tab_label(
        "drafting",
        "admin",
        "Please fix durable session recovery on builder4",
    )
    continued = app._set_session_tab_label("drafting", "admin", "continue")

    assert first == "Durable Session"
    assert continued == first
    assert app._session_tab_label("drafting") == "Durable Session"


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


def test_nav_status_includes_memory_and_admin_recovery_control():
    import app

    html = app.HTML_PAGE

    assert "RAM <span class=\"stat-val '+memClass+'\">'+memPct+'%</span>" in html
    assert "onclick=\"recoverSessions();closeToolsMenu()\"" in html
    assert "async function recoverSessions()" in html
    assert "BASE+'/api/admin/sessions/recover'" in html


def test_every_admin_commits_under_their_own_name(monkeypatch):
    """Three admins share one OS user and one git config. Without per-account
    identity, git log files all their work under the box owner."""
    import app

    monkeypatch.setattr(app, "TEAM_MODE", False)
    monkeypatch.setattr(app, "GIT_OWNER_NAME", "Nimo")
    monkeypatch.setattr(app, "GIT_OWNER_EMAIL", "nimo@lisa.my")
    monkeypatch.setattr(app, "GIT_EMAIL_DOMAIN", "lisa.my")

    owner = {"id": "admin", "username": "Nimo", "role": "admin",
             "google_email": "nimrod.rotem@gmail.com"}
    michiel = {"id": "u_61b301c4a914b160", "username": "Michiel", "role": "admin",
               "google_email": "michielrauws@gmail.com"}
    no_google = {"id": "u_deadbeef", "username": "Sam", "role": "admin"}

    assert app._git_identity_for(owner, "Nimo") == ("Nimo", "nimo@lisa.my")
    assert app._git_identity_for(michiel, "Nimo") == ("Michiel", "michielrauws@gmail.com")
    assert app._git_identity_for(no_google, "Nimo") == ("Sam", "Sam@lisa.my")
    # No account at all is still the box's own identity, not a synthesised one.
    assert app._git_identity_for(None, "Nimo") == ("Nimo", "nimo@lisa.my")


def test_browser_launcher_bootstrap_writes_the_file_it_sources(tmp_path, monkeypatch):
    """browser-session.sh sources chrome-common.sh. Writing one without the
    other is how every browser on a rebuilt box died on `CB_ROOT: unbound
    variable` while browser_sessions.json still listed one per account."""
    import app

    launcher = tmp_path / "bin" / "browser-session.sh"
    common = tmp_path / "bin" / "chrome-common.sh"
    monkeypatch.setattr(app, "BROWSER_LAUNCHER", str(launcher))
    monkeypatch.setattr(app, "CHROME_COMMON", str(common))

    app._ensure_browser_launcher()

    assert launcher.exists() and common.exists()
    assert "chrome-common.sh" in launcher.read_text()
    for fn in ("cb_chrome_env", "cb_chrome_flags", "CB_ROOT=", "CB_SCREEN_W="):
        assert fn in common.read_text()
    # Rewritten when it drifts, not just when it is missing.
    common.write_text("stale\n")
    app._ensure_browser_launcher()
    assert "cb_chrome_flags" in common.read_text()


def test_chrome_flags_skip_a_proxy_that_is_not_listening():
    """Pointing Chrome at a dead proxy port fails every page load in that
    browser, so the flag is conditional on the port answering."""
    import app

    script = app._CHROME_COMMON_SCRIPT

    assert "cb_port_open" in script
    assert 'if [ -n "$port" ] && cb_port_open "$port"; then' in script
    assert "--user-data-dir=$profile" in script
    assert "--remote-debugging-port=$cdp" in script


def test_offline_menu_pick_rescues_the_bypass_permissions_prompt():
    """Claude's bypass-permissions warning highlights "No, exit". With no LLM to
    ask, pressing Enter kills the session that was just launched."""
    import app

    options, selected = app._parse_menu_options(
        "  WARNING: Claude Code running in Bypass Permissions mode\n"
        "  ❯ 1. No, exit\n"
        "    2. Yes, I accept\n"
        "  Enter to confirm · Esc to cancel\n"
    )

    assert options == [(1, "No, exit"), (2, "Yes, I accept")]
    assert selected == 0
    assert app._pick_menu_option_offline(options, selected) == 2


def test_offline_menu_pick_stays_out_of_every_other_menu():
    """It only overrides Enter when the default stops the agent and exactly one
    other option clearly proceeds. Guessing at anything else is worse than Enter."""
    import app

    # Default already proceeds: leave it alone.
    assert app._pick_menu_option_offline([(1, "Yes, proceed"), (2, "No, exit")], 0) is None
    # Two ways to proceed: ambiguous, that is the LLM's job.
    assert app._pick_menu_option_offline(
        [(1, "No, keep planning"), (2, "Yes, run it"), (3, "Accept edits")], 0) is None
    # Nothing proceeds.
    assert app._pick_menu_option_offline([(1, "No, exit"), (2, "Cancel")], 0) is None
    # Not a menu.
    assert app._pick_menu_option_offline([(1, "No, exit")], 0) is None
    assert app._pick_menu_option_offline([], 0) is None


def test_scaffold_sessions_are_never_adopted_by_durable_recovery(monkeypatch):
    """The dashboard's own throwaway sessions (_authsetup, prime_<pid>) must not
    be checkpointed or restored: recovery recreates whatever it has a row for,
    so an adopted scaffold session comes back every 20s after it is killed."""
    import app

    registered: list[str] = []

    class FakeLifecycle:
        def get(self, _name):
            return {}

        def register_active(self, name, **kwargs):
            registered.append(name)
            return {"generation": "g", **kwargs}

        def snapshot(self):
            return {"sessions": {
                "prime_4242": {"managed": True, "generation": "g1", "owner_id": "admin",
                               "desired_state": "running", "restore_on_startup": True},
                "_authsetup": {"managed": True, "generation": "g2", "owner_id": "admin",
                               "desired_state": "running", "restore_on_startup": True},
                "drafting": {"managed": True, "generation": "g3", "owner_id": "admin",
                             "desired_state": "running", "restore_on_startup": True},
            }}

    monkeypatch.setattr(app, "SESSION_LIFECYCLE", FakeLifecycle())
    monkeypatch.setattr(app, "get_tmux_sessions", lambda: [
        {"name": "drafting", "cwd": "/srv/drafting"},
        {"name": "prime_4242", "cwd": "/home/x"},
        {"name": "_authsetup", "cwd": "/home/x"},
    ])
    monkeypatch.setattr(app, "_session_owner_id", lambda _name: "admin")
    monkeypatch.setattr(app, "_session_convo", lambda _name: "12345678-1234-1234-1234-123456789abc")
    monkeypatch.setattr(app, "_convo_has_transcript", lambda *_args: True)

    assert app._checkpoint_live_sessions() == 1
    assert registered == ["drafting"]
    assert [row["name"] for row in app._durable_session_candidates(set())] == ["drafting"]
    assert app._is_ephemeral_session("prime_4242") and app._is_ephemeral_session("_authsetup")
    assert not app._is_ephemeral_session("authsetup")
