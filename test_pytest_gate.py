"""Focused and subprocess coverage for the dashboard's host pytest gate."""

from __future__ import annotations

import fcntl
import grp
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import provision_accounts

HOOK_DIR = Path(__file__).resolve().parent / "runtime_hooks"
sys.path.insert(0, str(HOOK_DIR))
import tmux_dashboard_pytest_gate as gate  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_gate(monkeypatch):
    gate._release(record_result=False)
    for name in (
        gate.LOCK_ENV,
        gate.REQUIRED_ENV,
        gate.HELD_ENV,
        gate.ACCOUNT_ENV,
        gate.STATE_DIR_ENV,
        gate.FORCE_HEAVY_ENV,
        "PYTEST_XDIST_WORKER",
    ):
        monkeypatch.delenv(name, raising=False)
    yield
    gate._release(record_result=False)


def _config(tmp_path: Path, targets, invocation=()):
    manager = MagicMock()
    manager.get_plugin.return_value = None
    return SimpleNamespace(
        args=list(targets),
        rootpath=tmp_path,
        invocation_params=SimpleNamespace(args=tuple(invocation), dir=tmp_path),
        pluginmanager=manager,
    )


def test_classifier_covers_directories_flags_and_compact_xdist(tmp_path):
    test_file = tmp_path / "test_one.py"
    test_file.write_text("def test_one(): pass\n")
    assert gate._run_is_heavy(_config(tmp_path, [str(tmp_path)]))
    assert gate._run_is_heavy(
        _config(tmp_path, [str(test_file)], ("--cov=app", str(test_file)))
    )
    assert gate._run_is_heavy(
        _config(tmp_path, [str(test_file)], ("-n4", str(test_file)))
    )
    assert not gate._run_is_heavy(
        _config(tmp_path, [str(test_file) + "::test_one"], (str(test_file),))
    )
    assert not gate._run_is_heavy(
        _config(tmp_path, [str(tmp_path)], ("--collect-only", str(tmp_path)))
    )


def test_xdist_worker_never_takes_the_controller_lock(tmp_path, monkeypatch):
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw0")
    assert not gate._run_is_heavy(_config(tmp_path, [str(tmp_path)]))


def test_managed_heavy_run_fails_closed_without_a_safe_lock(tmp_path, monkeypatch):
    config = _config(tmp_path, [str(tmp_path)])
    monkeypatch.setenv(gate.REQUIRED_ENV, "1")
    with pytest.raises(pytest.UsageError, match="host lock path is missing"):
        gate._acquire(config)

    monkeypatch.setenv(gate.LOCK_ENV, str(tmp_path / "untrusted.lock"))
    with pytest.raises(pytest.UsageError, match="unavailable or unsafe"):
        gate._acquire(config)


def test_focused_run_does_not_require_the_host_lock(tmp_path, monkeypatch):
    test_file = tmp_path / "test_one.py"
    test_file.write_text("def test_one(): pass\n")
    monkeypatch.setenv(gate.REQUIRED_ENV, "1")
    gate._acquire(_config(tmp_path, [str(test_file)]))
    assert gate._global_lock is None


def test_lock_is_retained_through_sessionfinish_and_released_at_unconfigure(
    tmp_path, monkeypatch
):
    lock_path = tmp_path / "lock"
    lock_path.write_text("")
    held = lock_path.open("r")
    fcntl.flock(held, fcntl.LOCK_EX)
    gate._global_lock = held
    monkeypatch.setenv(gate.HELD_ENV, "parent")

    gate.pytest_sessionfinish(None, 0)
    competitor = lock_path.open("r")
    try:
        with pytest.raises(BlockingIOError):
            fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        gate.pytest_unconfigure(None)
        fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        fcntl.flock(competitor, fcntl.LOCK_UN)
        competitor.close()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="requires fork")
def test_atfork_child_closes_inherited_lock_without_releasing_parent(tmp_path):
    lock_path = tmp_path / "lock"
    lock_path.write_text("")
    held = lock_path.open("r")
    fcntl.flock(held, fcntl.LOCK_EX)
    gate._global_lock = held
    read_fd, write_fd = os.pipe()
    pid = os.fork()
    if pid == 0:
        os.close(read_fd)
        try:
            try:
                os.fstat(held.fileno())
                result = b"open"
            except (OSError, ValueError):
                result = b"closed"
            os.write(write_fd, result)
        finally:
            os._exit(0)
    os.close(write_fd)
    try:
        assert os.read(read_fd, 32) == b"closed"
        os.waitpid(pid, 0)
        competitor = lock_path.open("r")
        try:
            with pytest.raises(BlockingIOError):
                fcntl.flock(competitor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        finally:
            competitor.close()
    finally:
        os.close(read_fd)
        gate._release(record_result=False)


def test_unique_plugin_loads_explicitly_even_with_autoload_disabled(tmp_path):
    (tmp_path / "runtime_control.py").write_text(
        "raise AssertionError('generic project module must not be imported')\n"
    )
    test_file = tmp_path / "test_loaded.py"
    test_file.write_text(
        "def test_loaded(pytestconfig):\n"
        "    assert pytestconfig.pluginmanager.hasplugin('tmux_dashboard_pytest_gate')\n"
    )
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=str(HOOK_DIR),
        PYTEST_PLUGINS="tmux_dashboard_pytest_gate",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONDONTWRITEBYTECODE="1",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(test_file), "-q", "-p", "no:cacheprovider"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=30,
        env=environment,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _coalescing_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    repo = tmp_path / "repo"
    other = tmp_path / "other"
    marker = tmp_path / "executions.log"
    repo.mkdir()
    _git("init", "-q", cwd=repo)
    _git("config", "user.name", "pytest gate", cwd=repo)
    _git("config", "user.email", "pytest-gate@example.invalid", cwd=repo)
    (repo / "test_slow.py").write_text(
        "import os, time\n"
        "def test_slow():\n"
        "    with open(os.environ['GATE_EXECUTION_LOG'], 'a') as handle:\n"
        "        handle.write('executed\\n'); handle.flush()\n"
        "    time.sleep(1.5)\n"
    )
    _git("add", "test_slow.py", cwd=repo)
    _git("commit", "-qm", "test", cwd=repo)
    _git("worktree", "add", "-q", str(other), "HEAD", cwd=repo)
    return repo, other, marker


def _gated_pytest_command() -> list[str]:
    command = [
        sys.executable,
        "-m",
        "pytest",
        "test_slow.py",
        "-q",
        "-p",
        "no:cacheprovider",
    ]
    # The current long-lived shell may predate pytestgate group membership.
    # `sg` starts the probe with the account's current supplementary groups.
    return ["sg", "pytestgate", "-c", shlex.join(command)]


def test_simultaneous_clean_worktrees_coalesce_only_the_inflight_pass(tmp_path):
    try:
        grp.getgrnam("pytestgate")
    except KeyError:
        pytest.skip("requires a provisioned pytestgate host group")
    if not Path("/run/lock/tmux-dashboard/pytest-heavy.lock").is_file():
        pytest.skip("requires the provisioned immutable pytest host lock")

    repo, other, marker = _coalescing_repo(tmp_path)
    state = tmp_path / "state"
    environment = os.environ.copy()
    environment.update(
        PYTHONPATH=str(HOOK_DIR),
        PYTEST_PLUGINS="tmux_dashboard_pytest_gate",
        PYTHONDONTWRITEBYTECODE="1",
        TMUX_DASH_PYTEST_SERIAL_LOCK="/run/lock/tmux-dashboard/pytest-heavy.lock",
        TMUX_DASH_PYTEST_GATE_REQUIRED="1",
        TMUX_DASH_PYTEST_ACCOUNT="same-account",
        TMUX_DASH_PYTEST_STATE_DIR=str(state),
        TMUX_DASH_PYTEST_FORCE_HEAVY="1",
        GATE_EXECUTION_LOG=str(marker),
    )
    first_env = environment | {"DASH_SESSION": "one", "TMUX_PANE": "%101"}
    second_env = environment | {"DASH_SESSION": "two", "TMUX_PANE": "%202"}
    command = _gated_pytest_command()

    first = subprocess.Popen(
        command, cwd=repo, env=first_env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    second = subprocess.Popen(
        command, cwd=other, env=second_env, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    first_output, _ = first.communicate(timeout=40)
    second_output, _ = second.communicate(timeout=40)
    assert first.returncode == second.returncode == 0, first_output + second_output
    assert marker.read_text().splitlines() == ["executed"]
    assert "coalesced without rerunning" in (first_output + second_output)

    # Reuse is deliberately simultaneous-only. A later invocation executes.
    later = subprocess.run(
        command, cwd=repo, env=first_env, capture_output=True, text=True, timeout=40
    )
    assert later.returncode == 0, later.stdout + later.stderr
    assert marker.read_text().splitlines() == ["executed", "executed"]


def test_account_provisioning_persists_group_locks_and_stale_session_acl(
    tmp_path, monkeypatch
):
    lock_dir = tmp_path / "locks"
    lock_file = lock_dir / "pytest-heavy.lock"
    daily_lock = lock_dir / "dashboard-daily.lock"
    tmpfiles = tmp_path / "tmux-dashboard.conf"
    calls = []

    monkeypatch.setattr(provision_accounts, "PYTEST_LOCK_DIR", lock_dir)
    monkeypatch.setattr(provision_accounts, "PYTEST_LOCK", lock_file)
    monkeypatch.setattr(provision_accounts, "DAILY_TEST_LOCK", daily_lock)
    monkeypatch.setattr(provision_accounts, "PYTEST_TMPFILES", tmpfiles)
    monkeypatch.setattr(
        provision_accounts.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_name=name),
    )
    monkeypatch.setattr(
        provision_accounts,
        "sh",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )

    result = provision_accounts.configure_pytest_gate(["gx-member"])

    rendered = tmpfiles.read_text()
    assert f"d {lock_dir} 0750 root pytestgate -" in rendered
    assert f"f {lock_file} 0440 root pytestgate -" in rendered
    assert f"f {daily_lock} 0440 root pytestgate -" in rendered
    assert f"a+ {lock_dir} - - - - u:nimrod_rotem:r-x" in rendered
    assert f"a+ {lock_file} - - - - u:nimrod_rotem:r--" in rendered
    assert ("usermod", "-aG", "pytestgate", "gx-member") in calls
    assert any(call[:2] == ("systemd-tmpfiles", "--create") for call in calls)
    assert any("root-owned locks" in line for line in result)
