"""Focused coverage for dashboard startup, IPC, CPU, and workload controls."""

from __future__ import annotations

import asyncio
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

import app
import runtime_control


def test_session_lifecycle_persists_only_valid_resume_uuid(tmp_path):
    store = runtime_control.SessionLifecycleStore(tmp_path / "lifecycle.json")
    thread_id = "01a035f8-3188-7c21-8cca-582b01ad3002"

    row = store.set_resume_uuid("debug", thread_id.upper())

    assert row["resume_uuid"] == thread_id
    assert store.get("debug")["resume_uuid"] == thread_id
    with pytest.raises(ValueError, match="invalid Codex resume UUID"):
        store.set_resume_uuid("debug", "--last")


def test_active_checkpoint_preserves_intent_and_exact_root(tmp_path):
    store = runtime_control.SessionLifecycleStore(tmp_path / "lifecycle.json")
    thread_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
    registered = store.register_active(
        "debug", cwd="/workspace/debug", owner_id="u_michiel"
    )
    touched = store.touch("debug", source="browser")
    checkpointed = store.checkpoint_active(
        "debug",
        cwd="/workspace/debug",
        owner_id="u_michiel",
        resume_uuid=thread_id.upper(),
    )

    assert registered["generation"]
    assert checkpointed["generation"] == registered["generation"]
    assert checkpointed["resume_uuid"] == thread_id
    assert checkpointed["owner_id"] == "u_michiel"
    assert checkpointed["desired_state"] == "running"
    assert checkpointed["restore_on_startup"] is True
    assert checkpointed["last_interaction"] == touched["last_interaction"]

    parked = store.mark_parked(
        "debug", reason="idle", last_activity=1, cwd="/workspace/debug"
    )
    assert parked["desired_state"] == "parked"
    assert parked["restore_on_startup"] is False
    unchanged = store.checkpoint_active(
        "debug", cwd="/other", owner_id="u_michiel", resume_uuid=thread_id
    )
    assert unchanged["desired_state"] == "parked"
    assert unchanged["cwd"] == "/workspace/debug"
    resumed = store.mark_resumed("debug")
    assert resumed["desired_state"] == "running"
    assert resumed["restore_on_startup"] is True


def test_active_checkpoint_rejects_bad_uuid_and_owner_atomically(tmp_path):
    store = runtime_control.SessionLifecycleStore(tmp_path / "lifecycle.json")
    store.register_active("debug", cwd="/workspace/debug", owner_id="u_michiel")
    before = store.snapshot()

    with pytest.raises(ValueError, match="invalid Codex resume UUID"):
        store.checkpoint_active(
            "debug", cwd="/workspace/debug", owner_id="u_michiel", resume_uuid="--last"
        )
    with pytest.raises(ValueError, match="session owner is required"):
        store.checkpoint_active("debug", cwd="/workspace/debug", owner_id="")
    with pytest.raises(ValueError, match="session owner changed"):
        store.checkpoint_active(
            "debug", cwd="/workspace/debug", owner_id="u_monica"
        )
    assert store.snapshot() == before

    store.set_resume_uuid("debug", "01a035f8-3188-7c21-8cca-582b01ad3002")
    old_generation = store.get("debug")["generation"]
    reused = store.register_active(
        "debug", cwd="/workspace/new-debug", owner_id="u_michiel"
    )
    assert reused["generation"] != old_generation
    assert "resume_uuid" not in reused


def _durable_candidate(name: str, owner_id: str = "u_michiel") -> dict:
    thread_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
    generation = "a" * 32
    return {
        "name": name,
        "owner_id": owner_id,
        "owner": {"id": owner_id, "username": "Michiel"},
        "cwd": f"/workspace/{name}",
        "resume_uuid": thread_id,
        "generation": generation,
        "lifecycle": {
            "managed": True,
            "generation": generation,
            "owner_id": owner_id,
            "desired_state": "running",
            "parked": False,
            "cwd": f"/workspace/{name}",
            "resume_uuid": thread_id,
            "restore_on_startup": True,
        },
    }


def test_cold_start_recreates_missing_tmux_without_prefix_collision(monkeypatch):
    candidates = [_durable_candidate("debug"), _durable_candidate("debugtmux")]
    restore = MagicMock(return_value=True)
    checkpoint = MagicMock(return_value={"restore_on_startup": True})
    monkeypatch.setattr(app, "_checkpoint_live_sessions", MagicMock(return_value=0))
    monkeypatch.setattr(app, "_reconcile_provisional_tmux_sessions", lambda: 0)
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: {"debugtmux"})
    monkeypatch.setattr(app, "_durable_session_candidates", lambda: candidates)
    monkeypatch.setattr(app, "_durable_candidate_still_current", lambda _candidate: True)
    monkeypatch.setattr(
        app,
        "_strict_session_owner",
        lambda _name, owner_id="": (
            owner_id or "u_michiel",
            {"id": owner_id or "u_michiel", "username": "Michiel"},
        ),
    )
    monkeypatch.setattr(app, "_tmux_session_matches_owner", lambda *_args: True)
    monkeypatch.setattr(app, "_restore_parked_tmux_shell", restore)
    monkeypatch.setattr(app._session_lifecycle, "checkpoint_active", checkpoint)

    prepared = app._prepare_durable_sessions_on_startup()

    assert prepared == ["debug", "debugtmux"]
    restore.assert_called_once_with(
        "debug",
        candidates[0]["lifecycle"],
        expected_owner_id="u_michiel",
        expected_generation="a" * 32,
        expected_desired_states={"running"},
    )
    checkpoint.assert_called_once()


def test_raw_tmux_inventory_treats_missing_server_as_empty(monkeypatch):
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(
            returncode=1, stdout="", stderr="no server running"
        ),
    )
    assert app._live_tmux_session_names() == set()


def test_real_tmux_cold_restore_uses_isolated_exact_session(monkeypatch, tmp_path):
    if not shutil.which("tmux"):
        pytest.skip("tmux is required by the dashboard runtime")
    socket_root = tmp_path / "tmux-sockets"
    socket_root.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(socket_root))
    monkeypatch.delenv("TMUX", raising=False)
    owner = {"id": "u_michiel", "username": "Michiel"}
    monkeypatch.setattr(
        app, "_strict_session_owner", lambda *_args: ("u_michiel", owner)
    )
    monkeypatch.setattr(app, "_user_for_session", lambda _name: owner)
    monkeypatch.setattr(app, "_uses_private_account_runtime", lambda _owner: False)

    subprocess.run(
        ["tmux", "new-session", "-d", "-s", "debugtmux", "-c", str(tmp_path)],
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        assert app._restore_parked_tmux_shell(
            "debug",
            {
                "cwd": str(tmp_path),
                "resume_uuid": "01a035f8-3188-7c21-8cca-582b01ad3002",
            },
            expected_owner_id="u_michiel",
        )
        sessions = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.splitlines()
        assert sorted(sessions) == ["debug", "debugtmux"]
        marker = subprocess.run(
            [
                "tmux", "show-options", "-t", app._exact_tmux_session_id("debug"),
                "-v", "@dashboard_owner_id",
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        assert marker == "u_michiel"
    finally:
        subprocess.run(["tmux", "kill-server"], capture_output=True, text=True)


def test_cold_start_leaves_intentionally_parked_mapping_parked(monkeypatch):
    thread_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
    monkeypatch.setattr(
        app._session_lifecycle,
        "snapshot",
        lambda: {
            "sessions": {
                "debug": {
                    "parked": True,
                    "desired_state": "parked",
                    "resume_uuid": thread_id,
                    "cwd": "/workspace/debug",
                }
            }
        },
    )
    strict_owner = MagicMock()
    monkeypatch.setattr(app, "_strict_session_owner", strict_owner)

    assert app._durable_session_candidates() == []
    strict_owner.assert_not_called()


def test_cold_start_rejects_invalid_cwd_and_unknown_owner(monkeypatch):
    thread_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
    rows = {
        "badcwd": {"resume_uuid": thread_id, "cwd": "relative/path"},
        "unknown": {"resume_uuid": thread_id, "cwd": "/workspace/unknown"},
    }
    monkeypatch.setattr(
        app._session_lifecycle, "snapshot", lambda: {"sessions": rows}
    )

    def strict_owner(name, _expected=""):
        if name == "unknown":
            return None
        return "u_michiel", {"id": "u_michiel", "username": "Michiel"}

    monkeypatch.setattr(app, "_strict_session_owner", strict_owner)
    monkeypatch.setattr(
        app,
        "_validated_session_root_thread_id",
        lambda _name, value, _owner="": value,
    )

    assert app._durable_session_candidates() == []


def test_durable_member_cwd_is_confined_to_its_session_project(monkeypatch, tmp_path):
    owner = {"id": "u_michiel", "username": "Michiel"}
    project = tmp_path / "projects" / "Michiel" / "debug"
    project.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.setattr(app, "PROJECTS_ROOT", tmp_path / "projects")
    monkeypatch.setattr(app, "_uses_private_account_runtime", lambda _owner: True)

    assert app._durable_session_cwd("debug", {"cwd": str(project)}, owner) == str(
        project.resolve()
    )
    assert app._durable_session_cwd("debug", {"cwd": str(outside)}, owner) == ""
    assert app._durable_session_cwd("debug", {}, owner) == str(project.resolve())

    project.rmdir()
    project.symlink_to(outside, target_is_directory=True)
    assert app._durable_session_cwd("debug", {}, owner) == ""


def test_live_root_discovery_ignores_open_subagent_rollouts(monkeypatch, tmp_path):
    root_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
    child_id = "01a035f8-3188-7c21-8cca-582b01ad3003"
    home = tmp_path / "codex-home"
    sessions = home / "sessions" / "2026" / "08" / "25"
    root = sessions / f"rollout-root-{root_id}.jsonl"
    child = sessions / f"rollout-child-{child_id}.jsonl"
    owner = {"id": "u_michiel", "username": "Michiel"}
    monkeypatch.setattr(app, "_session_codex_process_id", lambda _name: 123)
    monkeypatch.setattr(
        app,
        "_process_environment",
        lambda _pid: {"DASH_SESSION": "debug", "CODEX_HOME": str(home)},
    )
    monkeypatch.setattr(
        app, "_strict_session_owner", lambda *_args: ("u_michiel", owner)
    )
    monkeypatch.setattr(app, "_user_codex_config_dir", lambda _owner: home)
    monkeypatch.setattr(app, "_uses_private_account_runtime", lambda _owner: True)
    monkeypatch.setattr(app, "_process_open_file_paths", lambda _pid: [root, child])
    monkeypatch.setattr(
        app,
        "_validated_session_root_thread_id",
        lambda _name, value, _owner="": value if value == root_id else None,
    )

    assert app._active_session_root_thread_id("debug", "u_michiel") == root_id


def test_durable_resume_rechecks_owner_and_is_idempotent(monkeypatch):
    candidate = _durable_candidate("debug")
    binding = ("u_michiel", {"id": "u_michiel", "username": "Michiel"})
    ensure = AsyncMock(return_value=True)
    checkpoint = MagicMock(return_value={"restore_on_startup": True})
    monkeypatch.setattr(app, "_strict_session_owner", lambda *_args: binding)
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: {"debug"})
    monkeypatch.setattr(app, "_tmux_session_matches_owner", lambda *_args: True)
    monkeypatch.setattr(app, "_async_is_codex_running", AsyncMock(return_value=True))
    monkeypatch.setattr(app, "_ensure_codex_running", ensure)
    monkeypatch.setattr(app, "_checkpoint_active_session", checkpoint)
    monkeypatch.setattr(app, "_durable_candidate_still_current", lambda _candidate: True)

    result = asyncio.run(
        app._resume_one_durable_session(candidate, asyncio.Semaphore(1))
    )

    assert result is True
    ensure.assert_not_awaited()
    checkpoint.assert_called_once()

    ensure.reset_mock()
    monkeypatch.setattr(
        app,
        "_durable_candidate_still_current",
        MagicMock(side_effect=[True, False]),
    )
    monkeypatch.setattr(app, "_async_is_codex_running", AsyncMock(return_value=False))
    checkpoint.reset_mock()
    result = asyncio.run(
        app._resume_one_durable_session(candidate, asyncio.Semaphore(1))
    )
    assert result is False
    ensure.assert_not_awaited()
    checkpoint.assert_not_called()


def test_durable_resume_isolates_per_session_failures(monkeypatch):
    candidates = [
        _durable_candidate("one"),
        _durable_candidate("two"),
        _durable_candidate("three"),
    ]
    monkeypatch.setattr(app, "_durable_session_candidates", lambda: candidates)
    resume = AsyncMock(side_effect=[True, False, RuntimeError("broken")])
    monkeypatch.setattr(app, "_resume_one_durable_session", resume)

    assert asyncio.run(app._resume_durable_sessions_once()) == (1, 2)


@pytest.mark.asyncio
async def test_manual_durable_reconcile_reports_healthy_tabs_idempotently(monkeypatch):
    candidates = [_durable_candidate("one"), _durable_candidate("two")]
    resume = AsyncMock(side_effect=[True, True, True, True])
    lifecycle = {
        candidate["name"]: candidate["lifecycle"] for candidate in candidates
    }
    monkeypatch.setattr(app, "_checkpoint_live_sessions", MagicMock(return_value=2))
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: {"one", "two"})
    monkeypatch.setattr(app, "_durable_session_candidates", lambda: candidates)
    monkeypatch.setattr(app, "_async_is_codex_running", AsyncMock(return_value=True))
    monkeypatch.setattr(app, "_resume_one_durable_session", resume)
    monkeypatch.setattr(
        app._session_lifecycle, "snapshot", lambda: {"sessions": lifecycle}
    )
    monkeypatch.setattr(app, "_activate_pending_autonomous_modes", MagicMock())

    first = await app._run_durable_session_reconciliation(
        source="test", owner_id="u_michiel"
    )
    second = await app._run_durable_session_reconciliation(
        source="test", owner_id="u_michiel"
    )

    for report in (first, second):
        assert report["ok"] is True
        assert report["ready"] == ["one", "two"]
        assert report["already_ready"] == ["one", "two"]
        assert report["restored"] == []
        assert report["restarted"] == []
        assert report["pending"] == []
    assert resume.await_count == 4


def test_checkpoint_live_sessions_is_scoped_to_requested_owner(monkeypatch):
    checkpoint = MagicMock(return_value={"restore_on_startup": True})
    monkeypatch.setattr(
        app, "_load_session_owners",
        lambda: {"mine": "u_michiel", "other": "u_other"},
    )
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: {"mine", "other"})
    monkeypatch.setattr(app, "_checkpoint_active_session", checkpoint)

    count = app._checkpoint_live_sessions(
        source="admin-manual-recovery", owner_id="u_michiel"
    )

    assert count == 1
    checkpoint.assert_called_once_with(
        "mine",
        source="admin-manual-recovery",
        expected_owner_id="u_michiel",
    )


def test_checkpoint_live_sessions_rechecks_owner_inside_session_fence(monkeypatch):
    owner_reads = iter([
        {"mine": "u_michiel"},
        {"mine": "u_other"},
    ])
    checkpoint = MagicMock(return_value={"restore_on_startup": True})
    monkeypatch.setattr(app, "_load_session_owners", lambda: next(owner_reads))
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: {"mine"})
    monkeypatch.setattr(app, "_checkpoint_active_session", checkpoint)

    assert app._checkpoint_live_sessions(owner_id="u_michiel") == 0
    checkpoint.assert_not_called()


def test_pending_autonomous_activation_is_scoped_to_requested_owner(monkeypatch):
    monkeypatch.setattr(
        app, "_pending_autonomous_state",
        {"mine": {"away_mode": True}, "other": {"away_mode": True}},
    )
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: {"mine", "other"})
    monkeypatch.setattr(
        app, "_load_session_owners",
        lambda: {"mine": "u_michiel", "other": "u_other"},
    )
    monkeypatch.setattr(app, "_durable_running_intent_exists", lambda _name: True)
    start = MagicMock(return_value=True)
    monkeypatch.setattr(app, "_start_restored_autonomous_mode", start)
    monkeypatch.setattr(app, "_save_autonomous_state", MagicMock())

    assert app._activate_pending_autonomous_modes(owner_id="u_michiel") == 1
    start.assert_called_once_with("mine", {"away_mode": True})


def test_pending_autonomous_activation_rechecks_owner_inside_fence(monkeypatch):
    monkeypatch.setattr(
        app, "_pending_autonomous_state", {"mine": {"away_mode": True}}
    )
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: {"mine"})
    monkeypatch.setattr(app, "_durable_running_intent_exists", lambda _name: True)
    monkeypatch.setattr(app, "_load_session_owners", lambda: {"mine": "u_other"})
    start = MagicMock(return_value=True)
    monkeypatch.setattr(app, "_start_restored_autonomous_mode", start)

    assert app._activate_pending_autonomous_modes(owner_id="u_michiel") == 0
    start.assert_not_called()


@pytest.mark.asyncio
async def test_manual_reconcile_does_not_report_another_owners_new_tab(monkeypatch):
    candidate = _durable_candidate("mine")
    live = iter([set(), {"mine", "other"}])
    checkpoint = MagicMock(return_value=1)
    activate = MagicMock()
    monkeypatch.setattr(app, "_checkpoint_live_sessions", checkpoint)
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: next(live))
    monkeypatch.setattr(app, "_durable_session_candidates", lambda: [candidate])
    monkeypatch.setattr(app, "_async_is_codex_running", AsyncMock(return_value=False))
    monkeypatch.setattr(app, "_resume_one_durable_session", AsyncMock(return_value=True))
    monkeypatch.setattr(
        app._session_lifecycle,
        "snapshot",
        lambda: {"sessions": {"mine": candidate["lifecycle"]}},
    )
    monkeypatch.setattr(app, "_activate_pending_autonomous_modes", activate)

    report = await app._run_durable_session_reconciliation(
        source="test", owner_id="u_michiel"
    )

    assert report["shells_recreated"] == ["mine"]
    checkpoint.assert_called_once_with(source="test", owner_id="u_michiel")
    activate.assert_called_once_with(owner_id="u_michiel")


@pytest.mark.asyncio
async def test_manual_durable_reconcile_start_reuses_running_job(monkeypatch):
    gate = asyncio.Event()
    monkeypatch.setattr(app, "_shutting_down", False)

    async def reconcile(**_kwargs):
        await gate.wait()
        return {
            "ok": True,
            "checkpointed": 1,
            "eligible": ["one"],
            "shells_recreated": [],
            "already_ready": ["one"],
            "restored": [],
            "restarted": [],
            "ready": ["one"],
            "pending": [],
        }

    monkeypatch.setattr(app, "_run_durable_session_reconciliation", reconcile)
    app._manual_durable_reconcile_jobs.clear()
    app._manual_durable_reconcile_tasks.clear()

    first = await app._start_manual_durable_reconcile("u_michiel")
    second = await app._start_manual_durable_reconcile("u_michiel")

    assert first["accepted"] is True
    assert second["accepted"] is False
    assert second["job"]["id"] == first["job"]["id"]
    gate.set()
    await app._manual_durable_reconcile_tasks["u_michiel"]
    status = app._manual_durable_reconcile_status(
        "u_michiel", first["job"]["id"]
    )
    assert status["job"]["status"] == "completed"
    assert status["job"]["ready"] == ["one"]


@pytest.mark.asyncio
async def test_durable_resume_stops_before_next_session_during_shutdown(monkeypatch):
    monkeypatch.setattr(app, "_shutting_down", True)
    owner = MagicMock()
    monkeypatch.setattr(app, "_strict_session_owner", owner)

    result = await app._resume_one_durable_session(
        _durable_candidate("queued"),
        asyncio.Semaphore(1),
        stop_on_shutdown=True,
    )

    assert result is False
    owner.assert_not_called()


def test_parked_session_lost_with_tmux_server_resumes_on_demand(monkeypatch):
    thread_id = "01a035f8-3188-7c21-8cca-582b01ad3002"
    row = {
        "parked": True,
        "virtual": False,
        "managed": True,
        "generation": "a" * 32,
        "owner_id": "u_michiel",
        "desired_state": "parked",
        "cwd": "/workspace/debug",
        "resume_uuid": thread_id,
    }
    owner = ("u_michiel", {"id": "u_michiel", "username": "Michiel"})
    touch = MagicMock(return_value=row)
    restore = MagicMock(return_value=True)
    ensure = AsyncMock(return_value=True)
    resumed = MagicMock(return_value={**row, "parked": False})
    monkeypatch.setattr(app._session_lifecycle, "touch", touch)
    monkeypatch.setattr(app._session_lifecycle, "mark_resumed", resumed)
    monkeypatch.setattr(app, "_strict_session_owner", lambda *_args: owner)
    monkeypatch.setattr(
        app, "_durable_session_cwd", lambda _name, lifecycle, _owner: lifecycle["cwd"]
    )
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: set())
    monkeypatch.setattr(app, "_restore_parked_tmux_shell", restore)
    monkeypatch.setattr(app, "_find_session", lambda _name: ([], {"name": "debug"}))
    monkeypatch.setattr(app, "_ensure_codex_running", ensure)

    result = asyncio.run(app._resume_parked_session("debug"))

    assert result["ok"] is True
    restore.assert_called_once_with(
        "debug", row, "u_michiel", "a" * 32, {"parked", "parking"}
    )
    ensure.assert_awaited_once_with(
        "debug",
        resume_uuid=thread_id,
        resume_cwd="/workspace/debug",
        expected_owner_id="u_michiel",
        expected_generation="a" * 32,
        expected_desired_states={"parked", "parking"},
        operation_locked=True,
        tmux_locked=True,
    )


def test_cpu_sampler_reports_execution_separately_from_iowait():
    counters = [(1_000, 600, 100), (1_100, 620, 110)]
    with patch.object(app, "_read_proc_cpu_times", side_effect=counters), patch.object(
        app.time, "sleep"
    ) as sleep:
        busy, iowait = app._sample_cpu_utilization(0.2)
    assert busy == 70.0
    assert iowait == 10.0
    sleep.assert_called_once_with(0.2)


def test_loopback_bind_host_rejects_public_interfaces():
    assert app._loopback_bind_host("127.0.0.2") == "127.0.0.2"
    assert app._loopback_bind_host("::1") == "::1"
    assert app._loopback_bind_host("0.0.0.0") == "127.0.0.1"
    assert app._loopback_bind_host("203.0.113.8") == "127.0.0.1"


def test_api_server_uses_configured_loopback_and_worker_count(monkeypatch):
    run = MagicMock()
    monkeypatch.setattr(app.uvicorn, "run", run)
    monkeypatch.setattr(app, "API_HOST", "127.0.0.2")
    app._run_api_server(3)
    assert run.call_args.kwargs["host"] == "127.0.0.2"
    assert run.call_args.kwargs["workers"] == 3


def test_api_role_lifespan_does_not_run_controller_mutations(monkeypatch):
    monkeypatch.setattr(app, "PROCESS_ROLE", "api")
    mutations = [
        "_load_simple_watchdog_disabled",
        "_load_autopush_mode",
        "_restore_default_model_setting",
        "_setup_shared_git_config",
        "_start_controller_socket",
    ]
    calls = {name: MagicMock() for name in mutations}
    for name, function in calls.items():
        monkeypatch.setattr(app, name, function)

    async def exercise():
        async with app.lifespan(app.app):
            pass

    asyncio.run(exercise())
    for function in calls.values():
        function.assert_not_called()


def test_durable_startup_opens_controller_before_slow_codex_resume(monkeypatch):
    events = []

    def sessions():
        events.append("inventory")
        return [{"name": "debug"}]

    async def start_socket():
        events.append("controller-ready")

    async def reconcile(**kwargs):
        events.append("reconcile")
        assert kwargs == {"source": "controller-startup"}
        return {"prepared": ["debug"], "ready": ["debug"], "pending": []}

    monkeypatch.setattr(app, "get_tmux_sessions", sessions)
    monkeypatch.setattr(app, "_start_controller_socket", start_socket)
    monkeypatch.setattr(app, "_run_durable_session_reconciliation", reconcile)

    result = asyncio.run(app._initialize_durable_session_runtime())

    assert result == ([{"name": "debug"}], ["debug"], 1, 0)
    assert events == [
        "controller-ready",
        "reconcile",
        "inventory",
    ]


def test_combined_runner_spawns_trusted_controller_and_stops_it(monkeypatch):
    class FakeController:
        def __init__(self):
            self.terminated = False

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return None

        def terminate(self):
            self.terminated = True

        def kill(self):
            raise AssertionError("graceful controller stop should succeed")

    controller = FakeController()
    popen = MagicMock(return_value=controller)
    run_api = MagicMock()
    thread = MagicMock()
    monkeypatch.setenv("TMUX_DASH_WEB_WORKERS", "4")
    monkeypatch.delenv("TMUX_DASH_TRUSTED_MAIN_PID", raising=False)
    monkeypatch.delenv("TMUX_DASH_TRUSTED_MAIN_START", raising=False)
    monkeypatch.setattr(app.subprocess, "Popen", popen)
    monkeypatch.setattr(app, "_run_api_server", run_api)
    monkeypatch.setattr(app, "_wait_for_controller_ready", MagicMock())
    monkeypatch.setattr(app, "_proc_parent_and_start", lambda _pid: (1, "98765"))
    monkeypatch.setattr(app.threading, "Thread", MagicMock(return_value=thread))

    app._run_combined_server()

    controller_env = popen.call_args.kwargs["env"]
    assert controller_env["TMUX_DASH_PROCESS_ROLE"] == "controller"
    assert controller_env["TMUX_DASH_TRUSTED_MAIN_PID"] == str(os.getpid())
    assert controller_env["TMUX_DASH_TRUSTED_MAIN_START"] == "98765"
    assert popen.call_args.kwargs["start_new_session"] is True
    run_api.assert_called_once_with(4)
    assert controller.terminated is True


def test_controller_parent_watch_stops_orphan_immediately(monkeypatch):
    monkeypatch.setattr(app, "_trusted_main_alive", lambda: False)

    async def exercise():
        stop = asyncio.Event()
        await app._watch_trusted_main(stop)
        return stop.is_set()

    assert asyncio.run(exercise()) is True


def test_codex_scope_has_aggregate_quota_slice_and_low_priority():
    command = runtime_control.scoped_codex_command(
        "demo", "codex --yolo", aggregate_cpu_quota_percent=150
    )
    assert "--slice=codexworkload.slice" in command
    assert "--nice=10" in command
    assert "CPUQuota=150%" in command
    assert "CPUWeight=25" in command
    assert "IOWeight=25" in command
    assert "MemoryHigh=75%" in command
    assert "MemoryMax=90%" in command
    assert "TasksMax=4096" in command
    assert command.count("set-property") == 2
    assert "else exec nice -n 10 codex --yolo; fi" in command


def test_dynamic_aggregate_quota_scales_for_two_and_four_vcpus():
    assert runtime_control.default_codex_aggregate_cpu_quota_percent(1) == 100
    assert runtime_control.default_codex_aggregate_cpu_quota_percent(2) == 150
    assert runtime_control.default_codex_aggregate_cpu_quota_percent(4) == 300
    assert runtime_control.default_codex_aggregate_cpu_quota_percent(16) == 300


def test_pytest_launch_env_explicitly_loads_serialization_plugin(monkeypatch):
    monkeypatch.setenv(
        "TMUX_DASH_PYTEST_LOCK_PATH", "/missing/mandatory/pytest-heavy.lock"
    )
    monkeypatch.setenv("PYTHONPATH", "/existing path:/more")
    monkeypatch.setenv("PYTEST_PLUGINS", "existing_plugin")
    prefix = runtime_control.build_pytest_serialization_env_prefix(
        "/opt/tmux-dashboard hooks", account="account-1"
    )
    assert "TMUX_DASH_PYTEST_SERIAL_LOCK=" in prefix
    assert "TMUX_DASH_PYTEST_GATE_REQUIRED=1" in prefix
    assert "TMUX_DASH_PYTEST_ACCOUNT=account-1" in prefix
    assert "tmux_dashboard_pytest_gate,existing_plugin" in prefix
    # Complete assignments are shell quoted, so whitespace cannot split env's
    # arguments and turn part of PYTHONPATH into an accidental command.
    result = subprocess.run(
        ["bash", "-c", prefix + " python3 -c 'import os; print(os.environ[\"PYTHONPATH\"])'"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "/opt/tmux-dashboard hooks:/existing path:/more"


def _write_proc_stat(root: Path, pid: int, parent: int, started: str) -> None:
    process_dir = root / str(pid)
    process_dir.mkdir()
    fields = ["S", str(parent), *(["0"] * 17), started]
    (process_dir / "stat").write_text(f"{pid} (worker with spaces) " + " ".join(fields))


def test_process_ancestry_uses_trusted_main_start_identity(tmp_path):
    _write_proc_stat(tmp_path, 300, 200, "3000")
    _write_proc_stat(tmp_path, 200, 100, "2000")
    _write_proc_stat(tmp_path, 100, 1, "1000")
    assert app._process_descends_from(300, 100, "1000", tmp_path)
    assert not app._process_descends_from(300, 100, "reused", tmp_path)
    assert not app._process_descends_from(300, 99, "1000", tmp_path)


def test_controller_peer_allows_api_descendant_and_rejects_tmux_branch(monkeypatch):
    writer = MagicMock()
    monkeypatch.setattr(app, "TRUSTED_MAIN_PID", 100)
    monkeypatch.setattr(app, "TRUSTED_MAIN_START", "1000")
    monkeypatch.setattr(
        app, "_controller_peer_credentials", lambda _writer: (300, os.geteuid(), 1)
    )
    monkeypatch.setattr(app, "_process_descends_from", lambda *args: True)
    assert app._controller_peer_authorized(writer)
    monkeypatch.setattr(app, "_process_descends_from", lambda *args: False)
    assert not app._controller_peer_authorized(writer)


def _ipc_reader(message):
    reader = MagicMock()
    reader.readline = AsyncMock(return_value=(message + "\n").encode())
    return reader


def _ipc_writer():
    writer = MagicMock()
    writer.drain = AsyncMock()
    writer.wait_closed = AsyncMock()
    return writer


def test_untrusted_controller_peer_cannot_dispatch_privileged_operation(monkeypatch):
    reader = _ipc_reader('{"op":"session_resume","session":"work"}')
    writer = _ipc_writer()
    dispatch = AsyncMock()
    monkeypatch.setattr(app, "_controller_peer_authorized", lambda _writer: False)
    monkeypatch.setattr(
        app, "_controller_peer_credentials", lambda _writer: (4321, os.geteuid(), 1)
    )
    monkeypatch.setattr(app, "_controller_dispatch", dispatch)

    asyncio.run(app._controller_client(reader, writer))

    reader.readline.assert_awaited_once()
    dispatch.assert_not_called()
    writer.write.assert_called_once_with(b'{"ok":false,"error":"forbidden"}\n')


@pytest.mark.parametrize("op", ["browser_renew", "browser_release"])
def test_untrusted_controller_peer_can_dispatch_only_its_bound_browser_lease(
    op, monkeypatch
):
    message = '{"op":"' + op + '","token":"opaque"}'
    reader = _ipc_reader(message)
    writer = _ipc_writer()
    dispatch = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(app, "_controller_peer_authorized", lambda _writer: False)
    binding = {
        "browser_id": "account-browser",
        "owner_prefix": "playwright-mcp:4321:",
        "lease_owner": "playwright-mcp:4321:9001:principal-a",
    }
    monkeypatch.setattr(app, "_browser_ipc_peer_binding", lambda _writer: binding)
    monkeypatch.setattr(
        app,
        "_browser_lease_for_token",
        lambda _token: {
            "token": "opaque",
            "browser_id": "account-browser",
            "kind": "agent",
            "owner": binding["lease_owner"],
        },
    )
    monkeypatch.setattr(app, "_controller_dispatch", dispatch)

    asyncio.run(app._controller_client(reader, writer))

    dispatch.assert_awaited_once()
    assert dispatch.await_args.args[0]["op"].startswith("browser_")
    assert b'"ok":true' in writer.write.call_args.args[0]


def test_untrusted_browser_acquire_uses_server_binding_and_canonical_owner(monkeypatch):
    message = {
        "op": "browser_acquire",
        "browser_id": "account-browser",
        "kind": "agent",
        "mode": "headless",
        "owner": "playwright-mcp:4321:browser_click",
    }
    writer = _ipc_writer()
    binding = {
        "browser_id": "account-browser",
        "owner_prefix": "playwright-mcp:4321:",
        "lease_owner": "playwright-mcp:4321:9001:principal-a",
    }
    monkeypatch.setattr(app, "_browser_ipc_peer_binding", lambda _writer: binding)
    assert app._untrusted_browser_ipc_allowed(writer, message)
    assert message["owner"] == binding["lease_owner"]
    assert message["ttl"] == app.BROWSER_LEASE_TTL

    for field, bad_value in (
        ("browser_id", "other-account"),
        ("kind", "dashboard"),
        ("mode", "headed"),
        ("owner", "playwright-mcp:9999:browser_click"),
    ):
        changed = {
            "op": "browser_acquire",
            "browser_id": "account-browser",
            "kind": "agent",
            "mode": "headless",
            "owner": "playwright-mcp:4321:browser_click",
        }
        changed[field] = bad_value
        assert not app._untrusted_browser_ipc_allowed(writer, changed)


@pytest.mark.parametrize("op", ["browser_renew", "browser_release"])
def test_browser_token_rejects_cross_session_browser_peer_and_pid_reuse(
    op, monkeypatch
):
    writer = _ipc_writer()
    message = {"op": op, "token": "opaque", "ttl": 3600}
    binding = {
        "browser_id": "browser-a",
        "owner_prefix": "playwright-mcp:4321:",
        "lease_owner": "playwright-mcp:4321:9001:session-a",
    }
    lease = {
        "token": "opaque",
        "browser_id": "browser-a",
        "kind": "agent",
        "owner": binding["lease_owner"],
    }
    monkeypatch.setattr(app, "_browser_ipc_peer_binding", lambda _writer: binding)
    monkeypatch.setattr(app, "_browser_lease_for_token", lambda _token: lease)
    assert app._untrusted_browser_ipc_allowed(writer, message)
    if op == "browser_renew":
        assert message["ttl"] == app.BROWSER_LEASE_TTL

    for changed_binding in (
        {**binding, "browser_id": "browser-b"},
        {**binding, "lease_owner": "playwright-mcp:9876:9001:session-b"},
        {**binding, "lease_owner": "playwright-mcp:4321:9002:session-a"},
    ):
        monkeypatch.setattr(
            app, "_browser_ipc_peer_binding", lambda _writer, row=changed_binding: row
        )
        assert not app._untrusted_browser_ipc_allowed(
            writer, {"op": op, "token": "opaque"}
        )

    monkeypatch.setattr(app, "_browser_lease_for_token", lambda _token: {})
    assert not app._untrusted_browser_ipc_allowed(
        writer, {"op": op, "token": "not-a-live-token"}
    )


def test_peer_pid_resolves_to_exactly_one_tmux_session(monkeypatch):
    result = SimpleNamespace(
        returncode=0,
        stdout="session-a\t100\nsession-b\t200\nmalformed\n",
    )
    monkeypatch.setattr(app.subprocess, "run", lambda *args, **kwargs: result)
    monkeypatch.setattr(
        app, "_process_descends_from", lambda peer, pane: peer == 4321 and pane == 100
    )
    assert app._peer_tmux_session(4321) == "session-a"

    monkeypatch.setattr(app, "_process_descends_from", lambda *_args: True)
    assert app._peer_tmux_session(4321) == "", "ambiguous ancestry must fail closed"


def test_browser_peer_binding_requires_explicit_owner_and_mapped_uid(monkeypatch):
    writer = _ipc_writer()
    member = {"id": "u_member", "username": "member", "role": "user"}
    browser_id = app._tenant_browser_id(member)
    monkeypatch.setattr(
        app, "_controller_peer_credentials", lambda _writer: (4321, 2222, 1)
    )
    monkeypatch.setattr(app, "_peer_tmux_session", lambda _pid: "member-session")
    monkeypatch.setattr(
        app, "_load_session_owners", lambda: {"member-session": "u_member"}
    )
    monkeypatch.setattr(
        app, "_find_user_by_id", lambda user_id: member if user_id == "u_member" else None
    )
    monkeypatch.setattr(app, "_mapped_unix_account_name", lambda _user: "gx-member")
    monkeypatch.setattr(
        app.pwd, "getpwnam", lambda _name: SimpleNamespace(pw_uid=2222)
    )
    monkeypatch.setattr(
        app,
        "_browser_session_by_id",
        lambda sid: {
            "id": sid,
            "owner_id": "u_member",
            "account_browser": True,
        }
        if sid == browser_id
        else {},
    )
    monkeypatch.setattr(app, "_proc_parent_and_start", lambda _pid: (100, "9001"))

    binding = app._browser_ipc_peer_binding(writer)
    assert binding["session"] == "member-session"
    assert binding["browser_id"] == browser_id
    assert binding["peer_start"] == "9001"

    monkeypatch.setattr(app, "_load_session_owners", lambda: {})
    assert app._browser_ipc_peer_binding(writer) is None

    monkeypatch.setattr(
        app, "_load_session_owners", lambda: {"member-session": "u_member"}
    )
    monkeypatch.setattr(
        app, "_controller_peer_credentials", lambda _writer: (4321, 3333, 1)
    )
    assert app._browser_ipc_peer_binding(writer) is None


def test_browser_peer_binding_shared_runtime_requires_dashboard_uid(monkeypatch):
    writer = _ipc_writer()
    admin = {"id": "admin", "username": "admin", "role": "admin"}
    peer = {"pid": 4321, "uid": os.geteuid()}
    monkeypatch.setattr(
        app,
        "_controller_peer_credentials",
        lambda _writer: (peer["pid"], peer["uid"], os.getegid()),
    )
    monkeypatch.setattr(app, "_peer_tmux_session", lambda _pid: "admin-session")
    monkeypatch.setattr(
        app, "_load_session_owners", lambda: {"admin-session": "admin"}
    )
    monkeypatch.setattr(
        app, "_find_user_by_id", lambda user_id: admin if user_id == "admin" else None
    )
    monkeypatch.setattr(app, "_mapped_unix_account_name", lambda _user: "")
    monkeypatch.setattr(
        app,
        "_browser_session_by_id",
        lambda sid: {
            "id": sid,
            "owner_id": "admin",
            "account_browser": True,
        }
        if sid == "default"
        else {},
    )
    monkeypatch.setattr(app, "_proc_parent_and_start", lambda _pid: (100, "9001"))

    assert app._browser_ipc_peer_binding(writer)["browser_id"] == "default"
    peer["uid"] = os.geteuid() + 1
    assert app._browser_ipc_peer_binding(writer) is None


def test_controller_socket_keeps_private_parent_and_socket_modes(tmp_path, monkeypatch):
    socket_path = tmp_path / "state" / "controller.sock"
    monkeypatch.setattr(app, "CONTROLLER_SOCKET", socket_path)
    monkeypatch.setattr(app, "CONTROLLER_LEADER_LOCK", tmp_path / "controller.lock")
    monkeypatch.setattr(app.grp, "getgrnam", MagicMock(side_effect=KeyError("gxauth")))
    app._controller_server = None
    app._controller_leader_fd = None

    async def exercise():
        await app._start_controller_socket()
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o600
        await app._stop_controller_socket()

    asyncio.run(exercise())
    assert not socket_path.exists()


def test_controller_socket_uses_gxauth_group_modes_when_member(tmp_path, monkeypatch):
    socket_path = tmp_path / "state" / "controller.sock"
    monkeypatch.setattr(app, "CONTROLLER_SOCKET", socket_path)
    monkeypatch.setattr(app, "CONTROLLER_LEADER_LOCK", tmp_path / "controller.lock")
    monkeypatch.setattr(
        app.grp, "getgrnam", lambda _name: SimpleNamespace(gr_gid=os.getegid())
    )
    app._controller_server = None
    app._controller_leader_fd = None

    async def exercise():
        await app._start_controller_socket()
        assert stat.S_IMODE(socket_path.parent.stat().st_mode) == 0o710
        assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660
        assert socket_path.parent.stat().st_gid == os.getegid()
        assert socket_path.stat().st_gid == os.getegid()
        await app._stop_controller_socket()

    asyncio.run(exercise())


def test_tmux_mutation_fence_blocks_a_second_process_path(tmp_path, monkeypatch):
    import threading

    monkeypatch.setattr(app, "MESSAGES_DIR", tmp_path)
    observed = []
    with app._tmux_server_mutation_lock():
        # Use an explicit helper so the exception is captured in the worker.
        def contend():
            try:
                fd = app._acquire_tmux_mutation_fd(0.1)
            except app._TmuxMutationBusy:
                observed.append("busy")
            else:
                app._release_tmux_mutation_fd(fd)
                observed.append("acquired")

        thread = threading.Thread(target=contend)
        thread.start()
        thread.join(timeout=2)
    assert observed == ["busy"]


def test_create_cleanup_rejects_a_recycled_tmux_id_token(monkeypatch):
    calls = []

    monkeypatch.setattr(
        app, "_exact_tmux_session_state", lambda _name: ("present", "$1")
    )

    def fake_run(args, **_kwargs):
        calls.append(args)
        return MagicMock(returncode=0, stdout="different-token\n", stderr="")

    monkeypatch.setattr(app.subprocess, "run", fake_run)

    assert app._kill_exact_created_tmux("debug", "$1", "expected-token") is False
    assert calls == [
        [
            "tmux", "show-options", "-t", "$1",
            "-v", app._TMUX_CREATE_TOKEN_OPTION,
        ]
    ]


def test_startup_retires_an_unowned_provisional_session(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "MESSAGES_DIR", tmp_path)
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda *_args, **_kwargs: MagicMock(
            returncode=0,
            stdout="$3\torphan\t1\t" + "b" * 32 + "\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(app._session_lifecycle, "get", lambda _name: {})
    monkeypatch.setattr(app, "_load_session_owners", lambda: {})
    retire = MagicMock(return_value=True)
    monkeypatch.setattr(app, "_kill_exact_created_tmux", retire)

    assert app._reconcile_provisional_tmux_sessions() == 1
    retire.assert_called_once_with("orphan", "$3", "b" * 32)


def test_startup_adopts_a_launched_provisional_session(tmp_path, monkeypatch):
    generation = "a" * 32
    token = "b" * 32
    root = "01a035f8-3188-7c21-8cca-582b01ad3002"
    row = {
        "managed": True,
        "generation": generation,
        "owner_id": "u_michiel",
        "desired_state": "running",
    }
    monkeypatch.setattr(app, "MESSAGES_DIR", tmp_path)
    monkeypatch.setattr(
        app.subprocess,
        "run",
        lambda *_args, **_kwargs: MagicMock(
            returncode=0,
            stdout=f"$4\tlaunched\t1\t{token}\n",
            stderr="",
        ),
    )
    monkeypatch.setattr(app._session_lifecycle, "get", lambda _name: row)
    monkeypatch.setattr(app, "_load_session_owners", lambda: {"launched": "u_michiel"})
    monkeypatch.setattr(app, "_tmux_session_matches_owner", lambda *_args: True)
    monkeypatch.setattr(app, "_active_session_root_thread_id", lambda *_args: root)
    checkpoint = MagicMock(return_value={**row, "resume_uuid": root})
    publish = MagicMock(return_value=True)
    retire = MagicMock(return_value=True)
    monkeypatch.setattr(app, "_checkpoint_active_session", checkpoint)
    monkeypatch.setattr(app, "_publish_tmux_session", publish)
    monkeypatch.setattr(app, "_kill_exact_created_tmux", retire)

    assert app._reconcile_provisional_tmux_sessions() == 1
    publish.assert_called_once_with(
        "launched", "u_michiel", generation, token
    )
    retire.assert_not_called()


def test_cold_start_recreates_a_real_isolated_tmux_shell(tmp_path, monkeypatch):
    tmux_tmp = tmp_path / "tmux"
    tmux_tmp.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    monkeypatch.setenv("TMUX_TMPDIR", str(tmux_tmp))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.setattr(app, "MESSAGES_DIR", state_dir)
    candidate = _durable_candidate("cold-auto-restore")
    candidate["cwd"] = str(tmp_path)
    candidate["lifecycle"]["cwd"] = str(tmp_path)
    lifecycle = MagicMock()
    lifecycle.matches.return_value = True
    lifecycle.checkpoint_active.return_value = candidate["lifecycle"]
    monkeypatch.setattr(app, "_session_lifecycle", lifecycle)
    monkeypatch.setattr(app, "_checkpoint_live_sessions", lambda **_kwargs: 0)
    monkeypatch.setattr(app, "_reconcile_provisional_tmux_sessions", lambda: 0)
    monkeypatch.setattr(app, "_durable_session_candidates", lambda: [candidate])
    monkeypatch.setattr(app, "_durable_candidate_still_current", lambda _candidate: True)
    monkeypatch.setattr(
        app,
        "_strict_session_owner",
        lambda _name, _owner_id="": (
            "u_michiel",
            {"id": "u_michiel", "username": "Michiel"},
        ),
    )
    monkeypatch.setattr(app, "_send_session_owner_environment", lambda *_args: True)

    try:
        assert app._prepare_durable_sessions_on_startup() == ["cold-auto-restore"]
        listed = subprocess.run(
            [
                "tmux", "list-sessions", "-F",
                "#{session_name}\t#{@dashboard_owner_id}\t"
                "#{@dashboard_generation}\t#{@dashboard_quarantined}",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        assert listed.stdout.strip() == (
            "cold-auto-restore\tu_michiel\t"
            + candidate["generation"]
            + "\t0"
        )
    finally:
        subprocess.run(
            ["tmux", "kill-server"], capture_output=True, text=True, timeout=5
        )
