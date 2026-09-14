"""Host-wide pytest resource gate for dashboard-managed Codex sessions.

The dashboard injects this module through ``PYTEST_PLUGINS``.  Heavy runs take
one immutable host lock, while identical clean-tree runs that overlap in time
may share a successful result within the same dashboard account.  Failed,
interrupted, dirty, or already-finished runs are never reused.
"""

from __future__ import annotations

import atexit
import fcntl
import hashlib
import importlib.metadata
import json
import os
import secrets
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest

LOCK_ENV = "TMUX_DASH_PYTEST_SERIAL_LOCK"
REQUIRED_ENV = "TMUX_DASH_PYTEST_GATE_REQUIRED"
HELD_ENV = "TMUX_DASH_PYTEST_LOCK_HELD"
ACCOUNT_ENV = "TMUX_DASH_PYTEST_ACCOUNT"
STATE_DIR_ENV = "TMUX_DASH_PYTEST_STATE_DIR"
FORCE_HEAVY_ENV = "TMUX_DASH_PYTEST_FORCE_HEAVY"

_global_lock = None
_fingerprint_lock = None
_fingerprint = ""
_result_path: Path | None = None
_run_id = ""
_exit_status: int | None = None
_coalesced = False

_VOLATILE_ENV = {
    "_",
    "COLUMNS",
    "DASH_PROJECT_DIR",
    "DASH_PROJECT_URL",
    "DASH_SESSION",
    "LINES",
    "OLDPWD",
    "PWD",
    "PYTEST_CURRENT_TEST",
    "SHLVL",
    "TERM",
    "TMUX",
    "TMUX_PANE",
    "WINDOWID",
    HELD_ENV,
    LOCK_ENV,
    REQUIRED_ENV,
    STATE_DIR_ENV,
}


def _truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _say(config, message: str) -> None:
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(message)
    else:
        sys.stderr.write(message + "\n")
        sys.stderr.flush()


def _is_xdist_worker(config) -> bool:
    return bool(os.environ.get("PYTEST_XDIST_WORKER") or hasattr(config, "workerinput"))


def _run_is_heavy(config) -> bool:
    """Conservatively gate suite-like runs, but leave focused checks responsive."""
    if _is_xdist_worker(config):
        return False
    if _truthy(FORCE_HEAVY_ENV):
        return True
    invocation = tuple(str(arg) for arg in config.invocation_params.args)
    if any(arg in {"--collect-only", "--co", "--help", "-h"} for arg in invocation):
        return False
    heavy_flags = ("--cov", "--numprocesses", "--dist")
    if any(
        (arg == "-n" or (arg.startswith("-n") and not arg.startswith("--")))
        or any(arg == flag or arg.startswith(flag + "=") for flag in heavy_flags)
        for arg in invocation
    ):
        return True
    targets = [str(arg) for arg in getattr(config, "args", ())]
    if not targets:
        return True
    paths = [Path(target.split("::", 1)[0]) for target in targets]
    unique = {str(path) for path in paths}
    if len(unique) > 1:
        return True
    for path in paths:
        if path.is_dir() or path.suffix != ".py":
            return True
        try:
            if path.stat().st_size >= 128 * 1024:
                return True
        except OSError:
            pass
    return False


def _open_trusted_host_lock(path: str | Path) -> int:
    lock_path = Path(path)
    if not lock_path.is_absolute():
        raise PermissionError("host lock path is not absolute")
    parent = lock_path.parent.lstat()
    if (
        not stat.S_ISDIR(parent.st_mode)
        or lock_path.parent.is_symlink()
        or parent.st_uid != 0
        or parent.st_mode & 0o022
        or parent.st_mode & 0o007
    ):
        raise PermissionError("host lock parent is not private and root-owned")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != 0
        or info.st_mode & 0o222
        or info.st_mode & 0o007
    ):
        os.close(fd)
        raise PermissionError("host lock is not private, immutable, and root-owned")
    return fd


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        timeout=8,
    )
    if result.returncode != 0:
        raise OSError((result.stderr or result.stdout or "git failed").strip())
    return result.stdout.strip()


def _normalise(value: str, repo_root: Path) -> str:
    root = str(repo_root)
    return value.replace(root, "<repo>") if root else value


def _clean_run_fingerprint(config) -> str:
    """Return an opaque exact-run key, or empty when result reuse is unsafe."""
    try:
        root_hint = Path(getattr(config, "rootpath", Path.cwd()))
        repo_root = Path(_git(root_hint, "rev-parse", "--show-toplevel")).resolve()
        if _git(repo_root, "status", "--porcelain=v1", "--untracked-files=all"):
            return ""
        head = _git(repo_root, "rev-parse", "HEAD")
        try:
            remote = _git(repo_root, "config", "--get", "remote.origin.url")
        except OSError:
            common = _git(repo_root, "rev-parse", "--git-common-dir")
            common_path = Path(common)
            if not common_path.is_absolute():
                common_path = repo_root / common_path
            remote = str(common_path.resolve())

        invocation = [
            _normalise(str(arg), repo_root) for arg in config.invocation_params.args
        ]
        environment = {
            key: _normalise(value, repo_root)
            for key, value in os.environ.items()
            if key not in _VOLATILE_ENV
        }
        distributions = sorted(
            (
                str(dist.metadata.get("Name") or "").lower(),
                str(dist.version or ""),
            )
            for dist in importlib.metadata.distributions()
        )
        payload = {
            "account": os.environ.get(ACCOUNT_ENV) or os.environ.get("DASH_USER") or "",
            "uid": os.geteuid(),
            "repository": remote,
            "head": head,
            "invocation": invocation,
            "python": _normalise(sys.executable, repo_root),
            "python_version": sys.version,
            "pytest": pytest.__version__,
            "distributions": distributions,
            "environment": environment,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()
    except (OSError, ValueError, subprocess.SubprocessError):
        return ""


def _validate_private_dir(path: Path) -> None:
    info = path.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or path.is_symlink()
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        raise PermissionError(f"unsafe pytest gate state directory: {path}")


def _state_dir() -> Path:
    configured = os.environ.get(STATE_DIR_ENV, "").strip()
    base = Path(configured) if configured else Path.home() / ".cache" / "tmux-dashboard-pytest"
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(base, 0o700)
    _validate_private_dir(base)
    account = os.environ.get(ACCOUNT_ENV) or os.environ.get("DASH_USER") or "default"
    account_key = hashlib.sha256(
        f"{os.geteuid()}:{account}".encode("utf-8", "replace")
    ).hexdigest()[:24]
    target = base / account_key
    target.mkdir(mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    _validate_private_dir(target)
    return target


def _open_private_lock(path: Path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != os.geteuid()
        or info.st_mode & 0o077
    ):
        os.close(fd)
        raise PermissionError("unsafe pytest fingerprint lock")
    return os.fdopen(fd, "a+")


def _read_result(path: Path) -> dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return {}
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return {}
        with os.fdopen(fd, "r") as handle:
            fd = -1
            value = json.load(handle)
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError, TypeError):
        return {}
    finally:
        if fd >= 0:
            os.close(fd)


def _write_result(path: Path, value: dict[str, Any]) -> None:
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            fd = -1
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _release(*, record_result: bool) -> None:
    global _global_lock, _fingerprint_lock, _fingerprint, _result_path, _run_id
    global _exit_status, _coalesced
    if record_result and _fingerprint_lock is not None and _result_path and _run_id:
        try:
            _write_result(
                _result_path,
                {
                    "fingerprint": _fingerprint,
                    "run_id": _run_id,
                    "status": "passed" if _exit_status == 0 else "failed",
                    "exit_status": _exit_status,
                    "finished_at": time.time(),
                },
            )
        except OSError:
            pass
    for handle in (_global_lock, _fingerprint_lock):
        if handle is not None:
            try:
                fcntl.flock(handle, fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                handle.close()
            except OSError:
                pass
    _global_lock = None
    _fingerprint_lock = None
    _fingerprint = ""
    _result_path = None
    _run_id = ""
    _exit_status = None
    _coalesced = False
    os.environ.pop(HELD_ENV, None)


def _after_fork_child() -> None:
    """Close inherited descriptors without unlocking the parent's open-file lock."""
    global _global_lock, _fingerprint_lock, _fingerprint, _result_path, _run_id
    global _exit_status, _coalesced
    for handle in (_global_lock, _fingerprint_lock):
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
    _global_lock = None
    _fingerprint_lock = None
    _fingerprint = ""
    _result_path = None
    _run_id = ""
    _exit_status = None
    _coalesced = False


def _acquire(config) -> None:
    global _global_lock, _fingerprint_lock, _fingerprint, _result_path, _run_id
    if _global_lock is not None or os.environ.get(HELD_ENV) or not _run_is_heavy(config):
        return

    lock_path = os.environ.get(LOCK_ENV, "").strip()
    if not lock_path:
        if _truthy(REQUIRED_ENV):
            raise pytest.UsageError(
                "Dashboard heavy-test serialization is required, but its host lock path is missing"
            )
        return

    fingerprint = _clean_run_fingerprint(config)
    if fingerprint:
        try:
            state = _state_dir()
            fingerprint_lock = _open_private_lock(state / (fingerprint + ".lock"))
            result_path = state / (fingerprint + ".json")
            before = _read_result(result_path)
            try:
                fcntl.flock(fingerprint_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                _say(config, "Identical clean pytest run is active; waiting for its result...")
                fcntl.flock(fingerprint_lock, fcntl.LOCK_EX)
                after = _read_result(result_path)
                if (
                    after != before
                    and after.get("fingerprint") == fingerprint
                    and after.get("status") == "passed"
                ):
                    fingerprint_lock.close()
                    _coalesced = True
                    pytest.exit(
                        "Identical clean in-flight pytest run passed; coalesced without rerunning",
                        returncode=0,
                    )
            _fingerprint_lock = fingerprint_lock
            _fingerprint = fingerprint
            _result_path = result_path
            _run_id = secrets.token_hex(16)
            _write_result(
                result_path,
                {
                    "fingerprint": fingerprint,
                    "run_id": _run_id,
                    "status": "running",
                    "started_at": time.time(),
                },
            )
        except pytest.exit.Exception:
            raise
        except (OSError, ValueError):
            # Unsafe/unavailable private state disables result reuse, never the
            # mandatory global resource guard.
            if _fingerprint_lock is not None:
                try:
                    _fingerprint_lock.close()
                except OSError:
                    pass
            _fingerprint_lock = None
            _fingerprint = ""
            _result_path = None
            _run_id = ""

    try:
        fd = _open_trusted_host_lock(lock_path)
        host_lock = os.fdopen(fd, "r")
        try:
            fcntl.flock(host_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            _say(config, "Another dashboard pytest run is active; waiting for the host test lock...")
            fcntl.flock(host_lock, fcntl.LOCK_EX)
        _global_lock = host_lock
        os.environ[HELD_ENV] = f"{os.getpid()}:{lock_path}"
    except OSError as exc:
        _release(record_result=False)
        raise pytest.UsageError(
            "Dashboard heavy-test serialization lock is unavailable or unsafe: " + lock_path
        ) from exc


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config) -> None:
    _acquire(config)


def pytest_sessionfinish(session, exitstatus) -> None:
    del session
    global _exit_status
    _exit_status = int(exitstatus)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config) -> None:
    del config
    _release(record_result=True)


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_child)
atexit.register(lambda: _release(record_result=False))
