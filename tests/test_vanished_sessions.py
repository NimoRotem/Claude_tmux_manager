"""A session ended outside the dashboard stays ended; a reboot still brings tabs back.

THE DEFECT THIS CATCHES: durable recovery restored ANY registered session that was missing,
including one somebody had just ended with `tmux kill-session`. The lisa-autofix reaper did exactly
that every hour on instance-3, and the dashboard recreated each session inside 20 seconds with a
fresh Claude, so 13 finished fix sessions (about 1.7 GB) lived for a month. No test here starts a
real agent: the restore launcher is stubbed.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

import pytest

import app
from runtime_control import SessionLifecycleStore

SERVER = "4242:1790000000"


@pytest.fixture
def store(tmp_path, monkeypatch):
    s = SessionLifecycleStore(tmp_path / "lifecycle.json")
    monkeypatch.setattr(app, "SESSION_LIFECYCLE", s)
    return s


@pytest.fixture
def world(monkeypatch):
    """A fake tmux: which server is up, which sessions exist, and a stub restore."""
    w = {"server": SERVER, "names": {"alive"}, "visible": ["alive"], "restored": []}
    monkeypatch.setattr(app, "_tmux_server_state",
                        lambda: (w["server"], set(w["names"]) if w["server"] else None))
    monkeypatch.setattr(app, "_checkpoint_live_sessions", lambda server_id="": 0)
    monkeypatch.setattr(app, "get_tmux_sessions",
                        lambda: [{"name": n} for n in w["visible"]])

    def fake_restore(candidate):
        w["restored"].append(candidate["name"])
        return {"name": candidate["name"], "status": "restored"}

    monkeypatch.setattr(app, "_restore_durable_session", fake_restore)
    monkeypatch.setattr(app, "_forget_vanished_session", lambda name: None)
    return w


def _row(store, name, server_id=""):
    return store.register_active(name, cwd="/tmp", owner_id="admin", server_id=server_id)


def test_a_session_killed_under_a_live_server_is_not_restored(store, world):
    _row(store, "lisa-fix-202609230400", SERVER)
    report = app._reconcile_durable_sessions_locked()
    assert world["restored"] == []
    assert report["vanished"] == ["lisa-fix-202609230400"]
    row = store.get("lisa-fix-202609230400")
    assert row["desired_state"] == "deleted" and row["restore_on_startup"] is False
    # and it stays down on the next pass, and after a dashboard restart (same code path)
    app._reconcile_durable_sessions_locked()
    assert world["restored"] == []


def test_a_reboot_still_restores(store, world):
    _row(store, "mytab", "999:1700000000")          # checkpointed on a server that is gone
    app._reconcile_durable_sessions_locked()
    assert world["restored"] == ["mytab"]
    assert store.get("mytab")["desired_state"] == "running"


def test_no_server_at_all_still_restores(store, world):
    _row(store, "mytab", SERVER)
    world["server"] = ""                            # the tmux server itself died
    app._reconcile_durable_sessions_locked()
    assert world["restored"] == ["mytab"]


def test_a_row_from_before_the_change_is_restored_once_as_before(store, world):
    _row(store, "legacy")                           # no server on record
    app._reconcile_durable_sessions_locked()
    assert world["restored"] == ["legacy"]


def test_a_live_session_hidden_from_the_claude_list_is_left_alone(store, world):
    _row(store, "hidden", SERVER)
    world["names"].add("hidden")                    # on the server, filtered out of the list
    report = app._reconcile_durable_sessions_locked()
    assert world["restored"] == [] and report["vanished"] == []
    assert store.get("hidden")["desired_state"] == "running"


def test_a_new_session_reusing_a_dead_name_is_registered_fresh(store):
    first = _row(store, "main", SERVER)
    assert store.mark_vanished("main", expected_generation=first["generation"], server_id=SERVER)
    again = store.checkpoint_active("main", cwd="/tmp", owner_id="someone-else",
                                    expected_generation=first["generation"], server_id=SERVER)
    assert again["desired_state"] == "running" and again["restore_on_startup"] is True
    assert again["generation"] != first["generation"]
    assert "vanished_at" not in again


def test_mark_vanished_refuses_a_row_seen_on_another_server(store):
    row = _row(store, "mytab", "1:1")
    assert not store.mark_vanished("mytab", expected_generation=row["generation"], server_id=SERVER)
    assert store.get("mytab")["desired_state"] == "running"


def test_old_tombstones_are_pruned(store, monkeypatch):
    a = _row(store, "old", SERVER)
    store.mark_vanished("old", expected_generation=a["generation"], server_id=SERVER)
    monkeypatch.setattr(SessionLifecycleStore, "TOMBSTONE_TTL_SEC", -1)
    b = _row(store, "new", SERVER)
    store.mark_vanished("new", expected_generation=b["generation"], server_id=SERVER)
    assert store.get("old") == {}


def test_server_state_reads_the_raw_list(monkeypatch):
    class R:
        returncode = 0
        stdout = "4242 1790000000\talpha\n4242 1790000000\tbeta\n"
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R())
    assert app._tmux_server_state() == (SERVER, {"alpha", "beta"})


def test_watched_socket_agents_are_not_counted_as_orphans():
    assert app.agent_socket.SOCKET in app._watched_sockets()
    assert "default" not in app._watched_sockets()
