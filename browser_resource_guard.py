"""Host-level containment and teardown for browser workloads.

The dashboard's normal browser sessions are intentionally excluded.  Every
other Chrome/Chromium process tree is placed in one cgroup-v2 CPU bucket so a
browser gate started by an agent (including one started through ``sudo`` or an
SSH command) cannot consume the whole machine.

All privileged operations use fixed commands and numeric process IDs.  PIDs
are revalidated with their Linux start ticks immediately before every signal,
which prevents a recycled PID from becoming a different kill target.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from typing import Dict, Iterable, Optional, Sequence


_PROC_ROOT = Path("/proc")
_DEFAULT_CGROUP = Path("/sys/fs/cgroup/tmux-dashboard-unmanaged-browsers")
_CPU_PERIOD_US = 100_000
_DEFAULT_CPU_PERCENT = 150  # 1.5 cores shared by every unmanaged browser.
_DEFAULT_INTERVAL_S = 10.0
_BROWSER_NAMES = ("chrome", "chromium")
_BROWSER_EXECUTABLES = {"chrome", "google-chrome", "google-chrome-stable", "chromium", "chromium-browser"}
_STRONG_WORKLOAD_RE = re.compile(
    r"(?:browser[-_ ]?(?:gate|agent|session|worker)|playwright|puppeteer|"
    r"selenium|chromedriver|headless[-_ ]?browser)",
    re.IGNORECASE,
)
_WRAPPER_NAMES = {"bash", "sh", "dash", "sudo", "timeout", "env", "setsid", "dbus-run-session"}


@dataclass(frozen=True)
class ProcRecord:
    pid: int
    ppid: int
    pgrp: int
    session: int
    start_ticks: int
    uid: int
    state: str
    command: str


@dataclass(frozen=True)
class BrowserRoot:
    process: ProcRecord
    profile: str
    cdp_port: int
    headless: bool
    protected: bool


def _read_proc_record(pid: int, proc_root: Path = _PROC_ROOT) -> Optional[ProcRecord]:
    try:
        base = proc_root / str(int(pid))
        fields = (base / "stat").read_text().rsplit(") ", 1)[-1].split()
        command = (base / "cmdline").read_bytes().decode("utf-8", "replace").replace("\0", " ").strip()
        uid = (base.stat().st_uid if proc_root == _PROC_ROOT else int((base / "uid").read_text()))
        return ProcRecord(
            pid=int(pid),
            state=fields[0],
            ppid=int(fields[1]),
            pgrp=int(fields[2]),
            session=int(fields[3]),
            start_ticks=int(fields[19]),
            uid=uid,
            command=command,
        )
    except (FileNotFoundError, ProcessLookupError, PermissionError, ValueError, IndexError, OSError):
        return None


def snapshot_processes(proc_root: Path = _PROC_ROOT) -> Dict[int, ProcRecord]:
    """Read a race-tolerant process snapshot from procfs."""
    rows: Dict[int, ProcRecord] = {}
    try:
        entries = list(proc_root.iterdir())
    except OSError:
        return rows
    for entry in entries:
        if not entry.name.isdigit():
            continue
        row = _read_proc_record(int(entry.name), proc_root)
        if row is not None:
            rows[row.pid] = row
    return rows


def _flag(command: str, name: str) -> str:
    match = re.search(r"(?:^|\s)" + re.escape(name) + r"=(?:\"([^\"]*)\"|'([^']*)'|(\S+))", command)
    if not match:
        return ""
    return next((part for part in match.groups() if part is not None), "")


def _executable_name(command: str) -> str:
    first = command.split(None, 1)[0] if command else ""
    return Path(first).name.lower()


def is_browser_command(command: str) -> bool:
    """True when ``command`` runs a browser binary itself, not via a wrapper.

    A launcher such as ``sudo -u someone google-chrome --user-data-dir=…`` carries
    every browser flag, so anything matching on flags alone will pick it up. This
    guard only ever acts on a real browser binary, so callers that choose which
    pid to report have to agree with it — reporting the wrapper's pid instead is
    what made "stop this browser" fail with "browser process is gone or changed".
    """
    return _executable_name(command) in _BROWSER_EXECUTABLES


def _is_browser_root(row: ProcRecord) -> bool:
    name = _executable_name(row.command)
    has_control_flag = (
        "--user-data-dir=" in row.command
        or "--remote-debugging-port=" in row.command
        or "--remote-debugging-pipe" in row.command
    )
    return name in _BROWSER_EXECUTABLES and "--type=" not in row.command and has_control_flag


def protected_profile_roots(home: Optional[Path] = None) -> tuple[str, ...]:
    """Profiles owned by the dashboard and therefore exempt from containment."""
    base = (home or Path.home()).resolve()
    roots = [base / ".claude-browser" / "profile", base / ".claude-browser" / "sessions"]
    for raw in os.environ.get("CB_RESOURCE_GUARD_PROTECTED_PROFILES", "").split(":"):
        if raw.strip():
            roots.append(Path(raw.strip()).expanduser())
    return tuple(os.path.realpath(str(path)) for path in roots)


def _profile_is_protected(profile: str, roots: Sequence[str]) -> bool:
    if not profile:
        return False
    candidate = os.path.realpath(profile)
    for root in roots:
        if candidate == root or candidate.startswith(root.rstrip("/") + "/"):
            return True
    return False


def browser_roots(
    snapshot: Optional[Dict[int, ProcRecord]] = None,
    *,
    protected_roots: Optional[Sequence[str]] = None,
) -> list[BrowserRoot]:
    """Return one row for each top-level Chrome/Chromium browser process."""
    rows = snapshot if snapshot is not None else snapshot_processes()
    protected_roots = tuple(protected_roots or protected_profile_roots())
    found: list[BrowserRoot] = []
    for row in rows.values():
        if not _is_browser_root(row):
            continue
        profile = _flag(row.command, "--user-data-dir")
        port_raw = _flag(row.command, "--remote-debugging-port")
        try:
            cdp_port = int(port_raw)
        except (TypeError, ValueError):
            cdp_port = 0
        found.append(BrowserRoot(
            process=row,
            profile=profile,
            cdp_port=cdp_port,
            headless="--headless" in row.command,
            protected=_profile_is_protected(profile, protected_roots),
        ))
    return sorted(found, key=lambda item: item.process.pid)


def descendant_pids(roots: Iterable[int], snapshot: Dict[int, ProcRecord]) -> list[int]:
    """All descendants of ``roots``, ordered deepest-first for safe teardown."""
    root_set = {int(pid) for pid in roots if int(pid) > 1}
    children: Dict[int, list[int]] = {}
    for row in snapshot.values():
        children.setdefault(row.ppid, []).append(row.pid)
    depths: Dict[int, int] = {}
    frontier = [(pid, 0) for pid in root_set]
    seen = set(root_set)
    while frontier:
        parent, depth = frontier.pop()
        for child in children.get(parent, []):
            if child in seen:
                continue
            seen.add(child)
            depths[child] = depth + 1
            frontier.append((child, depth + 1))
    return sorted(depths, key=lambda pid: (depths[pid], pid), reverse=True)


def _workload_root(browser_pid: int, snapshot: Dict[int, ProcRecord]) -> int:
    """Climb to a dedicated browser runner, never through a generic shell.

    This catches ``timeout node browser_gate.js`` and similar launchers while
    stopping before a tmux pane shell, SSH daemon, supervisor, or Claude itself.
    """
    selected = int(browser_pid)
    current = snapshot.get(selected)
    while current and current.ppid > 1:
        parent = snapshot.get(current.ppid)
        if parent is None:
            break
        parent_name = _executable_name(parent.command)
        strong = bool(_STRONG_WORKLOAD_RE.search(parent.command))
        wrapper_of_strong = (
            parent_name in _WRAPPER_NAMES
            and any(name in parent.command.lower() for name in _BROWSER_NAMES)
        )
        if not (strong or wrapper_of_strong):
            break
        selected = parent.pid
        current = parent
    return selected


def _cgroup_path() -> Path:
    raw = Path(os.environ.get("CB_UNMANAGED_CGROUP", str(_DEFAULT_CGROUP)))
    path = raw.resolve(strict=False)
    root = Path("/sys/fs/cgroup")
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise ValueError("CB_UNMANAGED_CGROUP must be below /sys/fs/cgroup") from exc
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", path.name):
        raise ValueError("unsafe cgroup name")
    return path


def cpu_quota_percent() -> int:
    try:
        value = int(os.environ.get("CB_UNMANAGED_CPU_PERCENT", str(_DEFAULT_CPU_PERCENT)))
    except ValueError:
        value = _DEFAULT_CPU_PERCENT
    return max(25, min(value, 300))


def _pid_cgroup(pid: int, proc_root: Path = _PROC_ROOT) -> str:
    try:
        for line in (proc_root / str(int(pid)) / "cgroup").read_text().splitlines():
            if line.startswith("0::"):
                return line[3:]
    except (OSError, ValueError):
        pass
    return ""


def is_limited(pid: int, proc_root: Path = _PROC_ROOT) -> bool:
    try:
        expected = "/" + str(_cgroup_path().relative_to("/sys/fs/cgroup"))
    except ValueError:
        return False
    return _pid_cgroup(pid, proc_root) == expected


def _privileged(command: Sequence[str], *, timeout: float = 10.0) -> subprocess.CompletedProcess:
    argv = list(command)
    if os.geteuid() != 0:
        argv = ["sudo", "-n", *argv]
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


def _apply_cgroup(cgroup: Path, quota_us: int, pids: Sequence[int]) -> subprocess.CompletedProcess:
    """Create/update the cgroup and atomically move the supplied numeric PIDs."""
    script = r'''set -eu
cg="$1"
quota="$2"
period="$3"
shift 3
mkdir -p "$cg"
printf '%s %s\n' "$quota" "$period" > "$cg/cpu.max"
for pid in "$@"; do
    case "$pid" in (*[!0-9]*|'') continue;; esac
    if [ -d "/proc/$pid" ]; then
        printf '%s\n' "$pid" > "$cg/cgroup.procs" 2>/dev/null || true
    fi
done
'''
    argv = ["/bin/sh", "-c", script, "browser-resource-guard", str(cgroup),
            str(int(quota_us)), str(_CPU_PERIOD_US), *(str(int(pid)) for pid in pids)]
    return _privileged(argv, timeout=15.0)


def guard_status() -> dict:
    cgroup = _cgroup_path()
    result = {
        "supported": Path("/sys/fs/cgroup/cgroup.controllers").exists(),
        "active": False,
        "cgroup": str(cgroup),
        "cpu_percent": cpu_quota_percent(),
        "cpu_cores": cpu_quota_percent() / 100.0,
        "processes": 0,
    }
    try:
        result["active"] = cgroup.is_dir() and (cgroup / "cpu.max").exists()
        result["cpu_max"] = (cgroup / "cpu.max").read_text().strip()
        result["processes"] = len((cgroup / "cgroup.procs").read_text().splitlines())
        stats = {}
        for line in (cgroup / "cpu.stat").read_text().splitlines():
            key, value = line.split(None, 1)
            stats[key] = int(value)
        result["throttled_usec"] = stats.get("throttled_usec", 0)
        result["nr_throttled"] = stats.get("nr_throttled", 0)
    except OSError as exc:
        if result["active"]:
            result["error"] = str(exc)
    return result


def enforce_unmanaged_quota() -> dict:
    """Move every unmanaged browser tree into the shared CPU quota cgroup."""
    snapshot = snapshot_processes()
    roots = browser_roots(snapshot)
    unmanaged = [item for item in roots if not item.protected]
    pids: set[int] = set()
    for item in unmanaged:
        pids.add(item.process.pid)
        pids.update(descendant_pids([item.process.pid], snapshot))
    cgroup = _cgroup_path()
    quota_us = cpu_quota_percent() * _CPU_PERIOD_US // 100
    wanted_cpu_max = f"{quota_us} {_CPU_PERIOD_US}"
    needs_move = [pid for pid in sorted(pids) if not is_limited(pid)]
    try:
        current_cpu_max = (cgroup / "cpu.max").read_text().strip()
    except OSError:
        current_cpu_max = ""
    changed = bool(needs_move or current_cpu_max != wanted_cpu_max or not cgroup.is_dir())
    if changed:
        result = _apply_cgroup(cgroup, quota_us, sorted(pids))
        if result.returncode != 0:
            message = (result.stderr or result.stdout or "cgroup update failed").strip()
            raise RuntimeError(message)
    return {
        "ok": True,
        "changed": changed,
        "browser_count": len(unmanaged),
        "protected_count": len(roots) - len(unmanaged),
        "process_count": len(pids),
        "moved": len(needs_move),
        **guard_status(),
    }


_sudo_cache = {"ts": 0.0, "ok": False}


def can_control_privileged_processes() -> bool:
    if os.geteuid() == 0:
        return True
    now = time.monotonic()
    if now - _sudo_cache["ts"] < 60:
        return bool(_sudo_cache["ok"])
    try:
        result = subprocess.run(["sudo", "-n", "true"], capture_output=True, timeout=3)
        ok = result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        ok = False
    _sudo_cache.update({"ts": now, "ok": ok})
    return ok


def _same_process(identity: ProcRecord, proc_root: Path = _PROC_ROOT) -> bool:
    current = _read_proc_record(identity.pid, proc_root)
    # A zombie has exited and consumes no CPU; only its parent can reap the
    # bookkeeping entry, so waiting or sending SIGKILL again cannot help.
    return (current is not None and current.state != "Z"
            and current.start_ticks == identity.start_ticks)


def _signal_identities(identities: Sequence[ProcRecord], sig: signal.Signals) -> None:
    live_rows = [row for row in identities if _same_process(row)]
    if not live_rows:
        return
    command = ["/bin/kill", f"-{sig.name}", "--", *(str(row.pid) for row in live_rows)]
    if any(row.uid != os.geteuid() for row in live_rows):
        result = _privileged(command)
    else:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        # kill returns non-zero if one process exits during the call. Revalidate;
        # only report a failure if any original identity is still present.
        remaining = [row.pid for row in identities if _same_process(row)]
        if remaining:
            raise RuntimeError((result.stderr or result.stdout or f"failed to signal {remaining}").strip())


def terminate_descendants(root_pids: Iterable[int], *, grace_s: float = 2.0) -> dict:
    """TERM then KILL every current descendant, leaving the roots themselves.

    The caller must validate that ``root_pids`` are its tmux pane PIDs.  The
    whole descendant set is snapshotted before TERM, so children that become
    orphaned during shutdown remain known and are still reaped.
    """
    roots = [int(pid) for pid in root_pids if int(pid) > 1]
    snapshot = snapshot_processes()
    ordered = descendant_pids(roots, snapshot)
    identities = [snapshot[pid] for pid in ordered if pid in snapshot]
    _signal_identities(identities, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_s)
    while identities and time.monotonic() < deadline:
        identities = [row for row in identities if _same_process(row)]
        if identities:
            time.sleep(0.05)
    # Catch children created after the first snapshot while the pane roots are
    # still alive, then force only identities that are demonstrably unchanged.
    later = snapshot_processes()
    known = {(row.pid, row.start_ticks): row for row in identities}
    for pid in descendant_pids(roots, later):
        row = later.get(pid)
        if row is not None:
            known[(row.pid, row.start_ticks)] = row
    survivors = list(known.values())
    _signal_identities(survivors, signal.SIGKILL)
    remaining = [row.pid for row in survivors if _same_process(row)]
    return {"ok": not remaining, "targeted": len(ordered), "killed": len(survivors), "remaining": remaining}


def stop_browser_workload(
    browser_pid: int,
    *,
    expected_started: float = 0.0,
    expected_start_ticks: int = 0,
    grace_s: float = 3.0,
) -> dict:
    """Stop a freshly revalidated unmanaged browser and its dedicated runner."""
    snapshot = snapshot_processes()
    browser = snapshot.get(int(browser_pid))
    if browser is None:
        return {"ok": False, "error": "browser process is gone or changed"}
    if not _is_browser_root(browser):
        # Say which process was refused: passing a launcher wrapper's pid here
        # used to report the browser as "gone" while it kept running.
        return {"ok": False,
                "error": f"pid {browser.pid} is not a browser process "
                         f"({_executable_name(browser.command) or 'unknown'})"}
    if expected_start_ticks and browser.start_ticks != int(expected_start_ticks):
        return {"ok": False, "error": "browser PID was recycled; refresh and try again"}
    if expected_started:
        boot_time = 0.0
        try:
            for line in Path("/proc/stat").read_text().splitlines():
                if line.startswith("btime "):
                    boot_time = float(line.split()[1])
                    break
            actual_started = boot_time + browser.start_ticks / float(os.sysconf("SC_CLK_TCK"))
        except (OSError, ValueError):
            actual_started = 0.0
        if not actual_started or abs(actual_started - float(expected_started)) > 1.0:
            return {"ok": False, "error": "browser PID was recycled; refresh and try again"}
    item = next((row for row in browser_roots(snapshot) if row.process.pid == browser.pid), None)
    if item is None:
        return {"ok": False, "error": "browser process is gone or changed"}
    if item.protected:
        return {"ok": False, "error": "refusing to stop a dashboard-managed browser"}
    workload_root = _workload_root(browser.pid, snapshot)
    root_identity = snapshot.get(workload_root)
    if root_identity is None:
        return {"ok": False, "error": "workload root disappeared"}
    ordered = descendant_pids([workload_root], snapshot)
    identities = [snapshot[pid] for pid in ordered if pid in snapshot]
    identities.append(root_identity)
    _signal_identities(identities, signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, grace_s)
    while time.monotonic() < deadline:
        live = [row for row in identities if _same_process(row)]
        if not live:
            break
        time.sleep(0.05)
    survivors = [row for row in identities if _same_process(row)]
    _signal_identities(survivors, signal.SIGKILL)
    remaining = [row.pid for row in survivors if _same_process(row)]
    return {
        "ok": not remaining,
        "browser_pid": browser.pid,
        "workload_root": workload_root,
        "targeted": len(identities),
        "forced": len(survivors),
        "remaining": remaining,
        **({"error": f"could not stop pids {remaining}"} if remaining else {}),
    }


async def watchdog(*, logger: Optional[logging.Logger] = None, interval_s: Optional[float] = None) -> None:
    """Continuously enforce the shared quota; intended as an app lifespan task."""
    log = logger or logging.getLogger(__name__)
    try:
        interval = float(interval_s or os.environ.get("CB_RESOURCE_GUARD_INTERVAL_S", _DEFAULT_INTERVAL_S))
    except ValueError:
        interval = _DEFAULT_INTERVAL_S
    interval = max(3.0, min(interval, 300.0))
    last_error = ""
    while True:
        try:
            report = await asyncio.to_thread(enforce_unmanaged_quota)
            if report.get("changed"):
                log.info(
                    "Browser resource guard: %d unmanaged browser(s), %d process(es), "
                    "moved %d into shared %.2f-core CPU quota",
                    report["browser_count"], report["process_count"], report["moved"],
                    report["cpu_cores"],
                )
            last_error = ""
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a guard failure must never take down the dashboard
            message = f"{type(exc).__name__}: {exc}"
            if message != last_error:
                log.error("Browser resource guard failed: %s", message)
                last_error = message
        await asyncio.sleep(interval)
