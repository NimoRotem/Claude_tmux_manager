import os
import types
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
    monkeypatch.setattr(app, "_tmux_server_state", lambda: ("7:100", set()))

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
            "server_id": "7:100",
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
                "server_id": "",
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

    # CPU and RAM are two stacked rows, laid out like the 5h/7d usage pair they
    # sit next to: a labelled track plus the percentage, not a run of prose.
    assert "row('CPU',cpuPct,cpuClass," in html
    assert "row('RAM',memPct,memClass,memTxt)" in html
    assert '<span class="nav-stat-bar"><span class="nav-stat-fill ' in html
    assert ".nav-server-stats{color:#8b949e;white-space:nowrap;display:flex;flex-direction:column" in html
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


def _stub_proc(monkeypatch, cmdline="", pid=0, fds=()):
    """Pretend a session's `claude` runs with this argv and these open files."""
    import app

    monkeypatch.setattr(app, "_session_claude_proc",
                        lambda name: {"pid": pid, "cmdline": cmdline, "env": {}})
    if pid:
        monkeypatch.setattr(app.os, "listdir",
                            lambda p: [str(i) for i in range(len(fds))]
                            if p == f"/proc/{pid}/fd" else os.listdir(p))
        monkeypatch.setattr(app.os, "readlink",
                            lambda p: fds[int(os.path.basename(p))]
                            if p.startswith(f"/proc/{pid}/fd/") else os.readlink(p))


def test_the_session_id_flag_names_the_transcript(monkeypatch):
    import app

    _stub_proc(monkeypatch, cmdline=(
        "claude --dangerously-skip-permissions --model claude-opus-5[1m] "
        "--effort xhigh --session-id 20d33bb7-bb5b-4770-9b67-e2b83b16a64c"))

    assert app._session_transcript_uuid("s") == "20d33bb7-bb5b-4770-9b67-e2b83b16a64c"


def test_the_open_scratch_dir_names_the_transcript(monkeypatch):
    import app

    # A session launched WITHOUT --session-id still holds its per-session scratch
    # directory open, and that path carries the same uuid.
    _stub_proc(monkeypatch, cmdline="claude --dangerously-skip-permissions", pid=4242,
               fds=("/dev/pts/9", "socket:[123]",
                    "/tmp/claude-1000/-home-nimo-proj/aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee/tasks"))

    assert app._session_transcript_uuid("s") == "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def test_the_named_transcript_wins_over_the_newest_one(tmp_path, monkeypatch):
    import app

    # A dozen sessions share one project directory. "Newest by mtime" hands each
    # of them a sibling's file, which is how a three-minute-old session reported
    # an 800k-token prompt as its own.
    mine = tmp_path / "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee.jsonl"
    theirs = tmp_path / "ffffffff-1111-2222-3333-444444444444.jsonl"
    mine.write_text("{}\n")
    theirs.write_text("{}\n")
    os.utime(mine, (1, 1))                       # mine is the OLDER file
    _stub_proc(monkeypatch, cmdline=(
        "claude --session-id aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"))
    app._session_transcript_cache.pop("s", None)

    picked = app._pick_session_transcript_file("s", [str(mine), str(theirs)])

    assert picked == str(mine)
    assert app._session_transcript_cache["s"]["confident"] is True


def test_an_unproven_transcript_reports_no_context(monkeypatch, tmp_path):
    import app

    # Model and effort survive an unproven read because argv carries this
    # session's own launch flags. The prompt size, the cache TTL and the time of
    # the last reply exist nowhere but the transcript, so an unproven read of
    # them is a neighbour's figure wearing this session's name.
    a = tmp_path / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    b = tmp_path / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl"
    for p in (a, b):
        p.write_text(_assistant("high", "claude-opus-5",
                                "2026-09-01T00:00:00.000Z", ctx=(1, 800_000, 1)) + "\n")
    monkeypatch.setattr(app, "_find_session_jsonl_files", lambda n: [str(a), str(b)])
    monkeypatch.setattr(app, "_pane_match_fragments", lambda n: [])
    monkeypatch.setattr(app, "_pane_model_effort", lambda n: {})
    _stub_proc(monkeypatch, cmdline="claude --effort high --model claude-opus-5")
    app._session_model_cache.pop("s", None)
    app._session_transcript_cache.pop("s", None)

    out = app._detect_session_model_effort("s")

    assert out["sure"] is False
    assert out["effort"] == "high"           # from argv, which is this session's
    assert out["context_tokens"] == 0        # never a neighbour's number
    assert out["cache_ttl"] == 0
    assert out["last_turn_end"] == 0


def _tool_use(name, **inp):
    import json

    return json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "name": name, "input": inp}]}})


def _said(text):
    import json

    return json.dumps({"type": "assistant",
                       "message": {"content": [{"type": "text", "text": text}]}})


def test_saved_reference_is_read_off_the_transcript(tmp_path):
    import app

    p = tmp_path / "t.jsonl"
    p.write_text("\n".join([
        _tool_use("Write", file_path="/home/n/proj/report.md", content="x"),
        _tool_use("Edit", file_path="/home/n/.tmux-dashboard/uploads/s-1/DRAFT.md"),
        _tool_use("Write", file_path="/home/n/.claude/settings.json"),
        _tool_use("Bash", command="curl -s https://demo.rotem.ai/panel/ | head"),
        _said("Live at https://demo.rotem.ai/panel/ with login user: nimo\n"
              "Password: hunter2-correct-horse\n"
              "max_tokens: 1024\n"
              "See http://www.w3.org/2000/svg for the namespace."),
    ]) + "\n")

    got = app._saved_scan_transcript(str(p))

    assert "/home/n/proj/report.md" in got["files"]
    # The session's OWN output directory sits under a dot-dir; the rule that
    # keeps ~/.claude out was throwing away every deliverable with it.
    assert "/home/n/.tmux-dashboard/uploads/s-1/DRAFT.md" in got["files"]
    assert "/home/n/.claude/settings.json" not in got["files"]
    assert got["urls"] == ["https://demo.rotem.ai/panel/"]      # deduped, no w3.org
    pairs = {(c["label"].lower(), c["value"]) for c in got["creds"]}
    assert ("login user", "nimo") in pairs
    assert ("password", "hunter2-correct-horse") in pairs
    # A token COUNT is not a login. Matching `max_tokens` turned every usage line
    # in an LLM session into a fake credential.
    assert not [c for c in got["creds"] if "token" in c["label"].lower()]


def test_saved_reference_scan_resumes_where_it_stopped(tmp_path):
    import app

    p = tmp_path / "t.jsonl"
    p.write_text(_tool_use("Write", file_path="/home/n/one.md") + "\n")
    first = app._saved_scan_transcript(str(p))
    assert first["files"] == ["/home/n/one.md"]

    with open(p, "a") as fh:
        fh.write(_tool_use("Write", file_path="/home/n/two.md") + "\n")
    second = app._saved_scan_transcript(str(p))

    # The appended record is picked up WITHOUT re-reading the earlier bytes, and
    # the earlier finding is still there.
    assert set(second["files"]) == {"/home/n/one.md", "/home/n/two.md"}
    assert app._saved_scan_state[str(p)]["offset"] == p.stat().st_size


def test_saved_reference_is_empty_when_the_transcript_is_unproven(monkeypatch, tmp_path):
    import app

    a = tmp_path / "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa.jsonl"
    b = tmp_path / "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb.jsonl"
    for p in (a, b):
        p.write_text(_tool_use("Write", file_path="/home/n/theirs.md") + "\n")
    monkeypatch.setattr(app, "_find_session_jsonl_files", lambda n: [str(a), str(b)])
    monkeypatch.setattr(app, "_pane_match_fragments", lambda n: [])
    _stub_proc(monkeypatch, cmdline="claude")
    app._session_transcript_cache.pop("s", None)

    got = app._session_saved_reference("s")

    assert got == {"urls": [], "creds": [], "files": [], "identified": False}


def test_the_saved_drawer_reads_the_server_not_a_dead_summariser():
    import app

    html = app.HTML_PAGE

    assert "async function refreshSavedKeys(name,force)" in html
    assert "'/api/sessions/'+encodeURIComponent(name)+'/saved'" in html
    # The old empty state told the reader to press "Full" on the Info tab, which
    # has not extracted anything since the auto-summariser was switched off.
    assert 'Press "Full" on the Info tab' not in html


def test_a_new_session_is_the_one_you_land_on():
    import app

    html = app.HTML_PAGE

    # loadAll() re-reads the URL hash, so setting `selectedSession` alone was
    # undone the moment it ran: the new session was created and you stayed on
    # the old one. The route has to move.
    assert "function _openNewSession(name)" in html
    assert "location.hash=_sessionRouteHash(name,tab);" in html
    assert "_openNewSession(data.name);" in html
    assert "selectedSession=data.name;" not in html


def test_the_key_bar_drops_the_retired_controls():
    import app

    html = app.HTML_PAGE

    assert "sendSlashCommand('${name}','/plan')" not in html
    assert ">Clear Input</button>" not in html
    # The ones that earn their place are still there.
    assert "sendSlashCommand('${name}','/context')" in html
    assert "sendSlashCommand('${name}','/usage')" in html


def test_the_account_caps_are_shown_once(): 
    import app

    html = app.HTML_PAGE

    # The header already carries 5h and 7d. A second copy under every terminal
    # meant the same pair twice on one screen, which is a duplicate rather than
    # a reading.
    assert "tl-cap" not in html
    assert "paintSessionUsageCaps" not in html
    assert 'id="nav-usage-5h-fill"' in html
    assert 'id="nav-usage-7d-fill"' in html
    # The percentage beside each track is the reading, so the track itself is
    # short: it only has to say roughly where in the range the number sits.
    assert ".nav-usage-bar{position:relative;width:22px" in html
    assert ".nav-stat-bar{position:relative;width:22px" in html


def test_claude_processes_are_attributed_to_their_session(monkeypatch):
    import app

    # pid 100 tmux server on the dashboard's own socket; 200 its pane shell;
    # 300 the agent. pid 400 is an agent under a tmux server on another socket,
    # which is exactly the shape of an abandoned test fixture.
    table = {
        100: {"ppid": 1, "rss_mb": 3, "age_s": 900, "argv": "tmux new-session -d -s work"},
        200: {"ppid": 100, "rss_mb": 4, "age_s": 900, "argv": "-bash"},
        300: {"ppid": 200, "rss_mb": 500, "age_s": 900,
              "argv": "claude --dangerously-skip-permissions"},
        400: {"ppid": 500, "rss_mb": 220, "age_s": 160000,
              "argv": "claude --dangerously-skip-permissions"},
        500: {"ppid": 1, "rss_mb": 3, "age_s": 160000,
              "argv": "tmux -L pffive new-session -d -s de-43"},
    }
    monkeypatch.setattr(app, "_read_proc_table", lambda: table)
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: SimpleNamespace(
        stdout="200 work\n" if "-L" not in a[0] else "", returncode=0))
    app._CLAUDE_INVENTORY_CACHE.update({"ts": 0.0, "data": None})

    inv = app._claude_process_inventory()

    assert inv["total"] == 2
    assert inv["attached"] == 1
    assert inv["orphaned"] == 1
    # The number that answers "should these still be running".
    assert inv["orphan_rss_mb"] == 220
    by_pid = {p["pid"]: p for p in inv["processes"]}
    assert by_pid[300]["session"] == "work" and by_pid[300]["attached"] is True
    assert by_pid[400]["socket"] == "pffive" and by_pid[400]["attached"] is False


def test_the_stats_panel_names_the_owner_of_every_agent():
    import app

    html = app.HTML_PAGE

    assert "const inv=s.claude_inventory||null;" in html
    assert "Claude Processes ('+inv.total+' running)</div>" in html
    assert "Not attached to any session here" in html


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


def test_a_borrowed_transcript_never_outranks_the_launch_flags(tmp_path, monkeypatch):
    import app

    # A brand-new session has no transcript of its own yet, so the newest file in
    # a shared project dir belongs to some OTHER session. Its level and model
    # must not be shown as this session's: argv still says what it launched on.
    other = tmp_path / "other.jsonl"
    other.write_text(_assistant("high", "claude-fable-5-1", "2026-09-01T00:00:00.000Z"))
    files = [str(other), str(tmp_path / "another.jsonl")]
    monkeypatch.setattr(app, "_find_session_jsonl_files", lambda name: files)
    monkeypatch.setattr(app, "_pick_session_transcript_file", lambda name, fs: str(other))
    monkeypatch.setattr(app, "_session_transcript_cache", {})
    monkeypatch.setattr(app, "_session_model_cache", {})
    monkeypatch.setattr(app, "_session_claude_proc", lambda name: {
        "cmdline": "claude --dangerously-skip-permissions --model claude-opus-5[1m] --effort xhigh"})
    monkeypatch.setattr(app, "capture_pane_recent", lambda name, lines=80: "")

    out = app._detect_session_model_effort("fresh")

    assert out["effort"] == "xhigh"
    assert out["effort_source"] == "launch"
    assert out["model"] == "claude-opus-5[1m]"


# ── Renaming a session, and the tmux name that must not move ─────────────────

def test_a_renamed_session_shows_the_new_name():
    import app

    rows = {"report": {"display": "Q3 revenue report", "created": "1788800000"}}

    assert app._session_display_name("report", rows, "1788800000") == "Q3 revenue report"


def test_a_recycled_tmux_name_does_not_inherit_the_old_label(tmp_path):
    """Kill `report` and make a new `report`: the label belonged to the session
    that is gone, and wearing it would be worse than showing the plain name. The
    tmux creation stamp is what tells the two apart."""
    import app

    rows = {"report": {"display": "Q3 revenue report", "created": "1788800000"}}

    assert app._session_display_name("report", rows, "1788899999") == "report"


def test_a_label_written_before_the_stamp_existed_still_works():
    """Rows predating the stamp were only ever allowed to re-space the tmux
    name, and that rule still governs them, so nobody's existing name changes."""
    import app

    old = {"twoword": {"display": "two word"}}

    assert app._session_display_name("twoword", old, "1788800000") == "two word"
    # ...and the guard those rows relied on is still enforced for them.
    wrong = {"twoword": {"display": "something else"}}
    assert app._session_display_name("twoword", wrong, "1788800000") == "twoword"


def test_the_rename_endpoint_is_wired_and_scoped():
    import app

    routes = {getattr(r, "path", "") : getattr(r, "methods", set()) for r in app.app.routes}
    assert "PATCH" in routes.get("/api/sessions/{session_name}/display-name", set())
    src = app.HTML_PAGE
    # First click on another tab still SELECTS it; only the tab you are in renames.
    assert "if(s.name!==selectedSession)return;" in src
    assert "function startSessionRename(name,labelEl)" in src
    assert "'/display-name'" in src or "/display-name'" in src
    # A repaint mid-edit would take the input away under the cursor.
    assert "if(_renamingSession)return;" in src


def test_the_project_picker_hides_directories_with_nothing_in_them():
    import app

    src = app.HTML_PAGE

    # A directory earns its place by having a session, a project-scope file, or
    # being the current selection. Claude Code records every directory it has
    # ever run in, so without this the picker fills with throwaway run dirs.
    assert "live.has(p.path) || (p.present||0) > 0" in src
    assert "p.path===CURRENT_PROJECT || PROJ_ALIAS[p.path]" in src
    # Never hide everything, and always say what was hidden.
    assert "return kept.length ? kept : all;" in src
    assert "function toggleAllProjects(ev)" in src
    assert "with nothing in them" in src
