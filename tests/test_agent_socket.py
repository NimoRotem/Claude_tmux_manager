"""agent_socket: private-socket sessions, the idle reaper, and ending through the dashboard.

Every server here runs `sleep`, never an agent, on a socket named for this test run; conftest's
pytest_sessionfinish kills any that survive a failure.
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import agent_socket as a
from conftest import RUN_SOCKET_PREFIX


@pytest.fixture
def sock(request):
    name = RUN_SOCKET_PREFIX + request.node.name[:20]
    yield name
    a.tmux(name, "kill-server")
    try:
        os.unlink("/tmp/tmux-%d/%s" % (os.getuid(), name))
    except OSError:
        pass


def test_list_and_idle_come_from_window_activity(sock):
    assert a.new_session("lisa-fix-1", "/tmp", socket=sock, command="sleep 300")
    rows = a.list_sessions(sock)
    assert [r["name"] for r in rows] == ["lisa-fix-1"]
    assert abs(rows[0]["activity"] - time.time()) < 30


def test_reap_ends_every_stale_session_and_then_the_server(sock):
    for n in ("lisa-fix-1", "lisa-fix-2", "other-1"):
        assert a.new_session(n, "/tmp", socket=sock, command="sleep 300")
    # nothing is idle yet
    assert a.reap("lisa-fix-", 3600, sockets=(sock,), log=lambda m: None) == []
    done = a.reap("lisa-fix-", 3600, sockets=(sock,), log=lambda m: None,
                  now=time.time() + 7200)
    assert sorted(n for _, n, _, _ in done) == ["lisa-fix-1", "lisa-fix-2"]
    assert [r["name"] for r in a.list_sessions(sock)] == ["other-1"]
    a.end_session("other-1", socket=sock, log=lambda m: None)
    assert a.tmux(sock, "list-sessions").returncode != 0        # server is gone


def test_reap_keeps_named_and_attached_sessions(sock, monkeypatch):
    for n in ("cronfix-a", "cronfix-b"):
        a.new_session(n, "/tmp", socket=sock, command="sleep 300")
    done = a.reap("cronfix-", 60, sockets=(sock,), keep={"cronfix-a"}, log=lambda m: None,
                  now=time.time() + 7200)
    assert [n for _, n, _, _ in done] == ["cronfix-b"]


def test_dry_run_ends_nothing(sock):
    a.new_session("lisa-fix-9", "/tmp", socket=sock, command="sleep 300")
    done = a.reap("lisa-fix-", 60, sockets=(sock,), dry=True, log=lambda m: None,
                  now=time.time() + 7200)
    assert done and done[0][3] == "dry-run"
    assert a.has_session("lisa-fix-9", sock)


def test_a_registered_default_socket_session_goes_through_the_dashboard(monkeypatch):
    calls = []
    monkeypatch.setattr(a, "dashboard_registered", lambda n: True)
    monkeypatch.setattr(a, "dashboard_delete", lambda n, log=print: calls.append(n) or True)
    monkeypatch.setattr(a, "tmux", lambda *x, **k: pytest.fail("must not raw-kill"))
    assert a.end_session("lisa-fix-x", socket="default", log=lambda m: None) == "dashboard"
    assert calls == ["lisa-fix-x"]


def test_a_failed_dashboard_delete_falls_back_to_tmux(monkeypatch):
    killed = []

    class R:
        returncode = 0
        stdout = ""
    monkeypatch.setattr(a, "dashboard_registered", lambda n: True)
    monkeypatch.setattr(a, "dashboard_delete", lambda n, log=print: False)
    monkeypatch.setattr(a, "tmux", lambda s, *args, **k: killed.append((s, args)) or R())
    assert a.end_session("lisa-fix-x", socket="default", log=lambda m: None) == "tmux"
    assert killed == [("default", ("kill-session", "-t", "=lisa-fix-x"))]


def test_supervisor_environment_line_is_parsed(tmp_path, monkeypatch):
    conf = tmp_path / "d.conf"
    conf.write_text('[program:x]\nenvironment=HOME="/h",TMUX_DASH_USER="Nimo",'
                    'TMUX_DASH_PASS="p,a=ss",OTHER=bare\n')
    monkeypatch.setattr(a, "SUPERVISOR_CONF", str(conf))
    env = a._supervisor_env()
    assert env["TMUX_DASH_USER"] == "Nimo" and env["TMUX_DASH_PASS"] == "p,a=ss"
    assert env["OTHER"] == "bare"


def test_credentials_prefer_the_running_dashboard(monkeypatch):
    monkeypatch.setattr(a, "_running_dashboard_env",
                        lambda: {"TMUX_DASH_USER": "Nimo", "TMUX_DASH_PASS": "live"})
    monkeypatch.setenv("TMUX_DASH_PASS", "stale-inherited")
    assert a._dash_credentials() == ("Nimo", "live")


def test_watch_ends_an_idle_session_and_its_server(sock):
    assert a.new_session("prb-x", "/tmp", socket=sock, command="sleep 300")
    t0 = time.time()
    how = a.watch("prb-x", socket=sock, idle_sec=3600, poll=0, log=lambda m: None,
                  now=lambda: t0 + 7200, sleep=lambda s: None)
    assert how == "tmux"
    assert a.list_sessions(sock) == []
    assert a.tmux(sock, "list-sessions").returncode != 0      # server gone with it


def test_watch_honours_max_age_and_waits_while_fresh(sock):
    assert a.new_session("netagent-y", "/tmp", socket=sock, command="sleep 300")
    ticks = []

    def fake_sleep(_):
        ticks.append(1)
    t0 = time.time()
    clock = iter([t0, t0 + 3000 + 60])      # first pass fresh, second pass past max age
    how = a.watch("netagent-y", socket=sock, idle_sec=3600, max_age_sec=3000, poll=0,
                  log=lambda m: None, now=lambda: next(clock), sleep=fake_sleep)
    assert how == "tmux" and ticks == [1]


def test_watch_returns_when_someone_else_ended_it(sock):
    assert a.new_session("prb-z", "/tmp", socket=sock, command="sleep 300")
    a.tmux(sock, "kill-session", "-t", "=prb-z")
    assert a.watch("prb-z", socket=sock, poll=0, log=lambda m: None) == "gone"


def test_spawn_watcher_runs_detached_and_ends_the_session(sock, tmp_path):
    assert a.new_session("prb-w", "/tmp", socket=sock, command="sleep 300")
    logf = tmp_path / "w.log"
    pid = a.spawn_watcher("prb-w", socket=sock, idle_sec=1, poll=1, logfile=str(logf))
    deadline = time.time() + 20
    while time.time() < deadline and a.has_session("prb-w", sock):
        time.sleep(0.5)
    assert not a.has_session("prb-w", sock)
    assert "watcher ended 'prb-w'" in logf.read_text()
