"""Focused tests for cross-worker leases and systemd resource wrappers."""

import threading

import runtime_control
from runtime_control import (
    BrowserLeaseStore,
    LockedJsonStore,
    SessionLifecycleStore,
    browser_start_argv,
    browser_unit_name,
    scoped_codex_command,
)


def test_locked_store_keeps_parallel_updates(tmp_path):
    store = LockedJsonStore(tmp_path / "state.json", lambda: {"count": 0})

    def increment():
        for _ in range(30):
            store.update(lambda value: value.update(count=value.get("count", 0) + 1))

    threads = [threading.Thread(target=increment) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert store.read()["count"] == 120


def test_browser_leases_expire_and_capacity_counts_active_only(tmp_path, monkeypatch):
    clock = [1_000.0]
    monkeypatch.setattr(runtime_control.time, "time", lambda: clock[0])
    leases = BrowserLeaseStore(tmp_path / "leases.json")
    first = leases.acquire("browser-a", kind="agent", owner="one", ttl=60)
    second = leases.acquire("browser-a", kind="viewer", owner="two", ttl=120)
    leases.acquire("browser-b", kind="agent", ttl=60)
    assert leases.snapshot()["by_browser"] == {"browser-a": 2, "browser-b": 1}

    clock[0] += 61
    assert leases.renew(second["token"], 60)
    snapshot = leases.snapshot()
    assert snapshot["active"] == 1
    assert snapshot["by_browser"] == {"browser-a": 1}
    assert leases.renew(first["token"], 60) is None


def test_browser_release_all_is_scoped_to_one_profile(tmp_path):
    leases = BrowserLeaseStore(tmp_path / "leases.json")
    leases.acquire("a", kind="agent")
    leases.acquire("a", kind="viewer")
    leases.acquire("b", kind="agent")
    assert leases.release_browser("a") == 2
    assert leases.snapshot()["by_browser"] == {"b": 1}


def test_session_lifecycle_parks_and_resumes_without_deleting_state(tmp_path):
    lifecycle = SessionLifecycleStore(tmp_path / "sessions.json")
    lifecycle.touch("work", source="viewer")
    parked = lifecycle.mark_parked(
        "work",
        reason="inactive",
        last_activity=123.0,
        cwd="/srv/work",
        virtual=True,
        scrollback_file="/state/work.log",
    )
    assert parked["parked"] is True
    assert parked["cwd"] == "/srv/work"
    assert parked["virtual"] is True
    assert parked["scrollback_file"] == "/state/work.log"
    resumed = lifecycle.mark_resumed("work", source="send")
    assert resumed["parked"] is False
    assert resumed["virtual"] is False
    assert resumed["park_reason"] == "inactive"
    assert lifecycle.snapshot()["sessions"]["work"]["cwd"] == "/srv/work"
    assert lifecycle.remove("work") is True
    assert lifecycle.get("work") == {}


def test_codex_scope_has_pressure_task_and_cpu_controls():
    command = scoped_codex_command("project one", "codex resume --last")
    assert "systemd-run --user --scope" in command
    assert "XDG_RUNTIME_DIR=/run/user/" in command
    assert "DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/" in command
    assert "MemoryHigh=" in command
    assert "MemoryMax=" in command
    assert "TasksMax=" in command
    assert "CPUWeight=" in command
    assert "ManagedOOMMemoryPressure=kill" in command
    assert "ManagedOOMSwap=kill" in command
    assert "codex resume --last" in command
    assert "else exec codex resume --last" in command


def test_browser_service_runs_headless_until_viewer_requests_headed():
    browser = {
        "id": "s 1",
        "display": 100,
        "rfb_port": 5901,
        "vnc_port": 6081,
        "cdp_port": 9223,
    }
    headless = browser_start_argv("/opt/browser-session.sh", browser, mode="headless")
    headed = browser_start_argv("/opt/browser-session.sh", browser, mode="headed")
    assert headless[-1] == "headless"
    assert headed[-1] == "headed"
    assert headless[0] == "env"
    assert any(part.startswith("XDG_RUNTIME_DIR=/run/user/") for part in headless)
    assert any(part.startswith("DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/") for part in headless)
    assert browser_unit_name("s 1").startswith("browser-s-1-")
    assert browser_unit_name("s 1").endswith(".scope")
    assert "--scope" in headless
    assert any(part.startswith("--property=MemoryHigh=") for part in headless)
    assert any(part.startswith("--property=TasksMax=") for part in headless)
