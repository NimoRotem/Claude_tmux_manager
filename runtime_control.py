"""Cross-process runtime state and resource controls for the dashboard.

The web API runs with multiple workers while one controller owns lifecycle
decisions.  These small file-backed stores provide atomic, flock-protected state
without putting credentials or user content in the control plane.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Callable


class LockedJsonStore:
    """Atomic JSON state shared by API and controller processes."""

    def __init__(self, path: Path, default_factory: Callable[[], dict[str, Any]]):
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.default_factory = default_factory

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else self.default_factory()
        except (OSError, ValueError, TypeError):
            return self.default_factory()

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            prefix=self.path.name + ".", suffix=".tmp", dir=str(self.path.parent)
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, self.path)
        finally:
            try:
                os.unlink(tmp_name)
            except FileNotFoundError:
                pass

    def read(self) -> dict[str, Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                return self._read_unlocked()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def update(self, mutate: Callable[[dict[str, Any]], Any]) -> tuple[dict[str, Any], Any]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                value = self._read_unlocked()
                result = mutate(value)
                self._write_unlocked(value)
                return value, result
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class BrowserLeaseStore:
    def __init__(self, path: Path):
        self.store = LockedJsonStore(path, lambda: {"version": 1, "leases": {}})

    @staticmethod
    def _prune(value: dict[str, Any], now: float) -> None:
        leases = value.setdefault("leases", {})
        for token in list(leases):
            if float((leases.get(token) or {}).get("expires_at", 0)) <= now:
                leases.pop(token, None)

    def acquire(
        self, browser_id: str, *, kind: str, owner: str = "", ttl: int = 300
    ) -> dict[str, Any]:
        now = time.time()
        ttl = max(30, min(int(ttl), 3600))
        token = secrets.token_urlsafe(24)

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            self._prune(value, now)
            lease = {
                "token": token,
                "browser_id": browser_id,
                "kind": (kind or "agent")[:32],
                "owner": (owner or "")[:128],
                "created_at": now,
                "last_seen": now,
                "expires_at": now + ttl,
            }
            value.setdefault("leases", {})[token] = lease
            return dict(lease)

        _value, lease = self.store.update(mutate)
        return lease

    def renew(self, token: str, ttl: int = 300) -> dict[str, Any] | None:
        now = time.time()
        ttl = max(30, min(int(ttl), 3600))

        def mutate(value: dict[str, Any]) -> dict[str, Any] | None:
            self._prune(value, now)
            lease = value.setdefault("leases", {}).get(token)
            if not isinstance(lease, dict):
                return None
            lease["last_seen"] = now
            lease["expires_at"] = now + ttl
            return dict(lease)

        _value, lease = self.store.update(mutate)
        return lease

    def release(self, token: str) -> bool:
        def mutate(value: dict[str, Any]) -> bool:
            return value.setdefault("leases", {}).pop(token, None) is not None

        _value, released = self.store.update(mutate)
        return bool(released)

    def release_browser(self, browser_id: str) -> int:
        def mutate(value: dict[str, Any]) -> int:
            leases = value.setdefault("leases", {})
            tokens = [
                token
                for token, lease in leases.items()
                if str((lease or {}).get("browser_id") or "") == browser_id
            ]
            for token in tokens:
                leases.pop(token, None)
            return len(tokens)

        _value, count = self.store.update(mutate)
        return int(count)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()

        def mutate(value: dict[str, Any]) -> None:
            self._prune(value, now)

        value, _ = self.store.update(mutate)
        leases = list(value.get("leases", {}).values())
        by_browser: dict[str, int] = {}
        for lease in leases:
            browser_id = str(lease.get("browser_id") or "")
            by_browser[browser_id] = by_browser.get(browser_id, 0) + 1
        return {
            "active": len(leases),
            "by_browser": by_browser,
            "leases": leases,
            "at": now,
        }

    def active_for(self, browser_id: str) -> int:
        return int(self.snapshot()["by_browser"].get(browser_id, 0))


class SessionLifecycleStore:
    def __init__(self, path: Path):
        self.store = LockedJsonStore(path, lambda: {"version": 1, "sessions": {}})

    def touch(self, session_name: str, *, source: str = "dashboard") -> dict[str, Any]:
        now = time.time()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            row.update({"last_interaction": now, "last_source": source[:64]})
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def mark_parked(
        self,
        session_name: str,
        *,
        reason: str,
        last_activity: float,
        cwd: str = "",
        virtual: bool = False,
        scrollback_file: str = "",
    ) -> dict[str, Any]:
        now = time.time()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            row.update(
                {
                    "parked": True,
                    "parked_at": now,
                    "park_reason": reason[:256],
                    "last_activity": float(last_activity or 0),
                    "cwd": cwd[:4096],
                    "virtual": bool(virtual),
                    "scrollback_file": scrollback_file[:4096],
                }
            )
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def mark_resumed(self, session_name: str, *, source: str = "dashboard") -> dict[str, Any]:
        now = time.time()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            row.update(
                {
                    "parked": False,
                    "resumed_at": now,
                    "last_interaction": now,
                    "last_source": source[:64],
                    "virtual": False,
                }
            )
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def get(self, session_name: str) -> dict[str, Any]:
        return dict(self.store.read().get("sessions", {}).get(session_name, {}))

    def remove(self, session_name: str) -> bool:
        def mutate(value: dict[str, Any]) -> bool:
            return value.setdefault("sessions", {}).pop(session_name, None) is not None

        _value, removed = self.store.update(mutate)
        return bool(removed)

    def snapshot(self) -> dict[str, Any]:
        return self.store.read()


def _safe_unit_fragment(value: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in value)[:80]
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{slug or 'session'}-{digest}"


def user_systemd_argv(*command: str) -> list[str]:
    """Run a user-manager command with an explicit bus from non-login services.

    Supervisor and long-lived tmux servers commonly lack XDG_RUNTIME_DIR even
    when lingering is enabled.  Without these two variables ``systemd-run
    --user`` silently takes the compatibility fallback and the limits never
    apply.
    """

    runtime_dir = os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    public_bus = Path(runtime_dir) / "bus"
    bus_path = public_bus if public_bus.exists() else Path(runtime_dir) / "systemd" / "private"
    bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS") or f"unix:path={bus_path}"
    return [
        "env",
        f"XDG_RUNTIME_DIR={runtime_dir}",
        f"DBUS_SESSION_BUS_ADDRESS={bus}",
        *command,
    ]


def scoped_codex_command(
    session_name: str,
    command: str,
    *,
    memory_high_mb: int = 1536,
    memory_max_mb: int = 3072,
    tasks_max: int = 512,
    cpu_weight: int = 100,
) -> str:
    """Return a burst-friendly systemd scope wrapper with a safe fallback.

    The fallback keeps session launch working on hosts without a user systemd
    manager.  A successful scoped command is never run a second time.
    """

    unit = f"codex-{_safe_unit_fragment(session_name)}-{int(time.time())}"
    inner = "exec " + command.strip()
    run = user_systemd_argv(
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        f"--property=MemoryHigh={int(memory_high_mb)}M",
        f"--property=MemoryMax={int(memory_max_mb)}M",
        f"--property=TasksMax={int(tasks_max)}",
        f"--property=CPUWeight={int(cpu_weight)}",
        "--property=ManagedOOMMemoryPressure=kill",
        "--property=ManagedOOMMemoryPressureLimit=70%",
        "--property=ManagedOOMSwap=kill",
        "bash",
        "-lc",
        inner,
    )
    wrapped = shlex.join(run)
    manager_check = shlex.join(
        user_systemd_argv("systemctl", "--user", "show-environment")
    )
    fallback = "exec " + command.strip()
    return (
        "if command -v systemd-run >/dev/null 2>&1 && "
        f"{manager_check} >/dev/null 2>&1; then "
        f"{wrapped}; else {fallback}; fi"
    )


def browser_unit_name(browser_id: str) -> str:
    return f"browser-{_safe_unit_fragment(browser_id)}.scope"


def browser_start_argv(
    launcher: str,
    browser: dict[str, Any],
    *,
    mode: str,
    memory_high_mb: int = 1024,
    memory_max_mb: int = 2048,
    tasks_max: int = 384,
    cpu_weight: int = 50,
) -> list[str]:
    base = [
        "bash",
        launcher,
        "start",
        str(browser["id"]),
        str(browser["display"]),
        str(browser["rfb_port"]),
        str(browser["vnc_port"]),
        str(browser["cdp_port"]),
        "headed" if mode == "headed" else "headless",
    ]
    return user_systemd_argv(
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={browser_unit_name(str(browser['id']))}",
        f"--property=MemoryHigh={int(memory_high_mb)}M",
        f"--property=MemoryMax={int(memory_max_mb)}M",
        f"--property=TasksMax={int(tasks_max)}",
        f"--property=CPUWeight={int(cpu_weight)}",
        "--property=ManagedOOMMemoryPressure=kill",
        "--property=ManagedOOMMemoryPressureLimit=70%",
        "--property=ManagedOOMSwap=kill",
        *base,
    )
