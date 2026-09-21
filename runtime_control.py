"""Atomic runtime state used by the dashboard lifecycle controller."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

_PYTEST_PLUGIN = "tmux_dashboard_pytest_gate"


class LockedJsonStore:
    """Small JSON store with process-safe updates and atomic replacement."""

    def __init__(self, path: Path, default_factory: Callable[[], dict[str, Any]]):
        self.path = path
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self.default_factory = default_factory

    def _read_unlocked(self) -> dict[str, Any]:
        try:
            value = json.loads(self.path.read_text())
            return value if isinstance(value, dict) else self.default_factory()
        except (OSError, TypeError, ValueError):
            return self.default_factory()

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(
            prefix=self.path.name + ".",
            suffix=".tmp",
            dir=self.path.parent,
        )
        try:
            with os.fdopen(fd, "w") as handle:
                json.dump(value, handle, indent=2, sort_keys=True)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def read(self) -> dict[str, Any]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            try:
                return self._read_unlocked()
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def update(
        self,
        mutate: Callable[[dict[str, Any]], Any],
    ) -> tuple[dict[str, Any], Any]:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with self.lock_path.open("a+") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            try:
                value = self._read_unlocked()
                result = mutate(value)
                self._write_unlocked(value)
                return value, result
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)


class SessionLifecycleStore:
    """Generation-bound owner and restart intent for dashboard tmux sessions."""

    _UUID_RE = re.compile(
        r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
    )

    def __init__(self, path: Path):
        self.store = LockedJsonStore(path, lambda: {"version": 2, "sessions": {}})

    @classmethod
    def _resume_uuid(cls, value: str) -> str:
        normalized = str(value or "").strip().lower()
        if normalized and not cls._UUID_RE.fullmatch(normalized):
            raise ValueError("invalid Claude resume UUID")
        return normalized

    def register_active(
        self,
        session_name: str,
        *,
        cwd: str,
        owner_id: str,
        resume_uuid: str = "",
        source: str = "session-create",
    ) -> dict[str, Any]:
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("session owner is required")
        now = time.time()
        row = {
            "managed": True,
            "generation": secrets.token_hex(16),
            "owner_id": owner[:256],
            "desired_state": "running",
            "cwd": str(cwd or "")[:4096],
            "restore_on_startup": True,
            "last_checkpoint": now,
            "checkpoint_source": str(source or "session-create")[:64],
        }
        normalized = self._resume_uuid(resume_uuid)
        if normalized:
            row["resume_uuid"] = normalized

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            value["version"] = 2
            value.setdefault("sessions", {})[session_name] = dict(row)
            return dict(row)

        _value, registered = self.store.update(mutate)
        return registered

    def checkpoint_active(
        self,
        session_name: str,
        *,
        cwd: str,
        owner_id: str,
        resume_uuid: str = "",
        source: str = "dashboard",
        expected_generation: str = "",
    ) -> dict[str, Any]:
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("session owner is required")
        normalized = self._resume_uuid(resume_uuid)
        now = time.time()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            if expected_generation and row.get("generation") != expected_generation:
                raise ValueError("session generation changed during checkpoint")
            recorded_owner = str(row.get("owner_id") or "")
            if recorded_owner and recorded_owner != owner:
                raise ValueError("session owner changed during checkpoint")
            if str(row.get("desired_state") or "") in {"deleting", "deleted"}:
                return dict(row)
            row.update(
                {
                    "managed": True,
                    "generation": row.get("generation") or secrets.token_hex(16),
                    "owner_id": owner[:256],
                    "desired_state": "running",
                    "cwd": str(cwd or "")[:4096],
                    "restore_on_startup": True,
                    "last_checkpoint": now,
                    "checkpoint_source": str(source or "dashboard")[:64],
                }
            )
            if normalized:
                row["resume_uuid"] = normalized
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def begin_transition(
        self,
        session_name: str,
        *,
        owner_id: str,
        desired_state: str,
        expected_generation: str = "",
        expected_desired_states: set[str] | None = None,
    ) -> dict[str, Any]:
        """Persist destructive intent before changing the volatile tmux state."""
        if desired_state != "deleting":
            raise ValueError("invalid session lifecycle transition")
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("session owner is required")
        generation = str(expected_generation or "").strip()
        now = time.time()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            if generation and str(row.get("generation") or "") != generation:
                raise ValueError("session generation changed during transition")
            if (
                expected_desired_states is not None
                and str(row.get("desired_state") or "") not in expected_desired_states
            ):
                raise ValueError("session state changed during transition")
            recorded_owner = str(row.get("owner_id") or "")
            if recorded_owner and recorded_owner != owner:
                raise ValueError("session owner changed during transition")
            row.update(
                {
                    "managed": True,
                    "owner_id": owner[:256],
                    "desired_state": desired_state,
                    "restore_on_startup": False,
                    "transition_at": now,
                }
            )
            row.setdefault("generation", secrets.token_hex(16))
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def matches(
        self,
        session_name: str,
        *,
        generation: str = "",
        owner_id: str = "",
        desired_states: set[str] | None = None,
        resume_uuid: str = "",
        restore_on_startup: bool | None = None,
    ) -> bool:
        """Check an immutable lifecycle binding without changing the registry."""
        row = self.get(session_name)
        if not row:
            return False
        if generation and str(row.get("generation") or "") != str(generation):
            return False
        if owner_id and str(row.get("owner_id") or "") != str(owner_id):
            return False
        if desired_states is not None and str(row.get("desired_state") or "") not in desired_states:
            return False
        if resume_uuid and str(row.get("resume_uuid") or "").lower() != str(resume_uuid).lower():
            return False
        if restore_on_startup is not None and bool(row.get("restore_on_startup")) is not restore_on_startup:
            return False
        return True

    def remove(
        self,
        session_name: str,
        *,
        expected_generation: str = "",
        owner_id: str = "",
    ) -> bool:
        generation = str(expected_generation or "").strip()
        owner = str(owner_id or "").strip()

        def mutate(value: dict[str, Any]) -> bool:
            sessions = value.setdefault("sessions", {})
            row = sessions.get(session_name)
            if not isinstance(row, dict):
                return False
            if generation and str(row.get("generation") or "") != generation:
                return False
            if owner and str(row.get("owner_id") or "") != owner:
                return False
            sessions.pop(session_name, None)
            return True

        _value, removed = self.store.update(mutate)
        return bool(removed)

    def get(self, session_name: str) -> dict[str, Any]:
        return dict(self.store.read().get("sessions", {}).get(session_name, {}))

    def snapshot(self) -> dict[str, Any]:
        return self.store.read()


def _safe_unit_fragment(value: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in value)[:80]
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{slug or 'session'}-{digest}"


def _safe_slice_name(value: str) -> str:
    candidate = str(value or "").strip()
    if (
        candidate.endswith(".slice")
        and candidate not in {".slice", "-.slice"}
        and all(ch.isalnum() or ch in "_.-" for ch in candidate)
    ):
        return candidate
    return "builder4-agents.slice"


def user_systemd_argv(*command: str) -> list[str]:
    """Address the user manager explicitly from Supervisor and tmux shells."""
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


def build_pytest_gate_env_prefix(
    plugin_dir: str | Path,
    *,
    account: str = "builder4",
) -> str:
    """Inject the immutable pytest plugin into every managed Claude shell."""
    lock_path = os.environ.get(
        "TMUX_DASH_PYTEST_LOCK_PATH",
        "/run/lock/builder4/pytest-heavy.lock",
    )
    python_path = str(Path(plugin_dir).resolve())
    if inherited := os.environ.get("PYTHONPATH", ""):
        python_path += os.pathsep + inherited
    plugins = _PYTEST_PLUGIN
    if inherited := os.environ.get("PYTEST_PLUGINS", ""):
        plugins += "," + inherited
    assignments = {
        "TMUX_DASH_PYTEST_SERIAL_LOCK": lock_path,
        "TMUX_DASH_PYTEST_GATE_REQUIRED": "1",
        "TMUX_DASH_PYTEST_ACCOUNT": account,
        "PYTHONPATH": python_path,
        "PYTEST_PLUGINS": plugins,
    }
    return "env " + " ".join(
        shlex.quote(f"{key}={value}") for key, value in assignments.items()
    )


def scoped_agent_command(
    session_name: str,
    command: str,
    *,
    memory_high_mb: int = 6144,
    memory_max_mb: int = 8192,
    tasks_max: int = 768,
    cpu_weight: int = 100,
    nice_level: int = 5,
    slice_name: str = "builder4-agents.slice",
    aggregate_cpu_quota_percent: int = 400,
    aggregate_cpu_weight: int = 25,
    aggregate_io_weight: int = 25,
    aggregate_memory_high_percent: int = 55,
    aggregate_memory_max_percent: int = 70,
    aggregate_tasks_max: int = 4096,
) -> str:
    """Wrap one agent in a user scope and cap only the builder4 aggregate."""
    unit = f"builder4-agent-{_safe_unit_fragment(session_name)}-{int(time.time())}"
    workload_slice = _safe_slice_name(slice_name)
    effective_nice = max(0, min(int(nice_level), 19))
    run = user_systemd_argv(
        "systemd-run",
        "--user",
        "--scope",
        "--quiet",
        "--collect",
        f"--unit={unit}",
        f"--slice={workload_slice}",
        f"--nice={effective_nice}",
        f"--property=MemoryHigh={max(1, int(memory_high_mb))}M",
        f"--property=MemoryMax={max(1, int(memory_max_mb))}M",
        f"--property=TasksMax={max(1, int(tasks_max))}",
        f"--property=CPUWeight={max(1, min(int(cpu_weight), 10000))}",
        "--property=ManagedOOMMemoryPressure=kill",
        "--property=ManagedOOMMemoryPressureLimit=70%",
        "--property=ManagedOOMSwap=kill",
        "bash",
        "-lc",
        "exec " + command.strip(),
    )
    core_properties = [
        f"CPUQuota={max(1, int(aggregate_cpu_quota_percent))}%",
        f"CPUWeight={max(1, min(int(aggregate_cpu_weight), 10000))}",
        f"MemoryHigh={max(1, min(int(aggregate_memory_high_percent), 100))}%",
        f"MemoryMax={max(1, min(int(aggregate_memory_max_percent), 100))}%",
        f"TasksMax={max(1, int(aggregate_tasks_max))}",
    ]
    set_core = shlex.join(
        user_systemd_argv(
            "systemctl",
            "--user",
            "set-property",
            "--runtime",
            workload_slice,
            *core_properties,
        )
    )
    set_io = shlex.join(
        user_systemd_argv(
            "systemctl",
            "--user",
            "set-property",
            "--runtime",
            workload_slice,
            f"IOWeight={max(1, min(int(aggregate_io_weight), 10000))}",
        )
    )
    manager_check = shlex.join(
        user_systemd_argv("systemctl", "--user", "show-environment")
    )
    fallback = f"exec nice -n {effective_nice} " + command.strip()
    return (
        "if command -v systemd-run >/dev/null 2>&1 && "
        f"{manager_check} >/dev/null 2>&1; then "
        f"{set_core} >/dev/null 2>&1 || true; "
        f"{set_io} >/dev/null 2>&1 || true; "
        f"{shlex.join(run)}; else {fallback}; fi"
    )
