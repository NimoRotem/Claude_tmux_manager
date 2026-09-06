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
import re
import secrets
import shlex
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

_PYTEST_SERIAL_LOCK_ENV = "TMUX_DASH_PYTEST_SERIAL_LOCK"
_PYTEST_GATE_REQUIRED_ENV = "TMUX_DASH_PYTEST_GATE_REQUIRED"
_PYTEST_ACCOUNT_ENV = "TMUX_DASH_PYTEST_ACCOUNT"
_PYTEST_PLUGIN = "tmux_dashboard_pytest_gate"


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
            # fsyncing only the file does not make the directory entry durable
            # across sudden power loss after os.replace().
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
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
        self.store = LockedJsonStore(path, lambda: {"version": 2, "sessions": {}})

    def register_active(
        self,
        session_name: str,
        *,
        cwd: str,
        owner_id: str,
        source: str = "session-create",
    ) -> dict[str, Any]:
        """Register a fresh tab generation and discard stale same-name roots."""
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("session owner is required")
        now = time.time()
        row = {
            "managed": True,
            "generation": secrets.token_hex(16),
            # Monotonic evidence for this exact generation. Passive views and
            # checkpoints must never clear it once an input path has set it.
            "had_conversation_input": False,
            "owner_id": owner[:256],
            "desired_state": "running",
            "parked": False,
            "virtual": False,
            "cwd": str(cwd or "")[:4096],
            "restore_on_startup": True,
            "last_checkpoint": now,
            "checkpoint_source": str(source or "session-create")[:64],
        }

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            value["version"] = 2
            value.setdefault("sessions", {})[session_name] = dict(row)
            return dict(row)

        _value, registered = self.store.update(mutate)
        return registered

    def touch(
        self,
        session_name: str,
        *,
        source: str = "dashboard",
        records_input: bool = False,
    ) -> dict[str, Any]:
        now = time.time()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            row.update({"last_interaction": now, "last_source": source[:64]})
            if records_input:
                row["had_conversation_input"] = True
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
        owner_id: str = "",
        expected_generation: str = "",
        expected_desired_states: set[str] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        owner = str(owner_id or "").strip()
        generation = str(expected_generation or "").strip()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            if generation and str(row.get("generation") or "") != generation:
                raise ValueError("session generation changed during park")
            if (
                expected_desired_states is not None
                and str(row.get("desired_state") or "") not in expected_desired_states
            ):
                raise ValueError("session state changed during park")
            recorded_owner = str(row.get("owner_id") or "")
            if owner and recorded_owner and recorded_owner != owner:
                raise ValueError("session owner changed during park")
            row.update(
                {
                    "parked": True,
                    "parked_at": now,
                    "park_reason": reason[:256],
                    "last_activity": float(last_activity or 0),
                    "cwd": cwd[:4096],
                    "virtual": bool(virtual),
                    "scrollback_file": scrollback_file[:4096],
                    "restore_on_startup": False,
                    "managed": True,
                    "desired_state": "parked",
                }
            )
            if owner:
                row["owner_id"] = owner[:256]
            row.setdefault("generation", secrets.token_hex(16))
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def mark_resumed(
        self,
        session_name: str,
        *,
        source: str = "dashboard",
        records_input: bool = False,
        owner_id: str = "",
        expected_generation: str = "",
        expected_desired_states: set[str] | None = None,
    ) -> dict[str, Any]:
        now = time.time()
        owner = str(owner_id or "").strip()
        generation = str(expected_generation or "").strip()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            if generation and str(row.get("generation") or "") != generation:
                raise ValueError("session generation changed during resume")
            if (
                expected_desired_states is not None
                and str(row.get("desired_state") or "") not in expected_desired_states
            ):
                raise ValueError("session state changed during resume")
            recorded_owner = str(row.get("owner_id") or "")
            if owner and recorded_owner and recorded_owner != owner:
                raise ValueError("session owner changed during resume")
            row.update(
                {
                    "parked": False,
                    "resumed_at": now,
                    "last_interaction": now,
                    "last_source": source[:64],
                    "virtual": False,
                    "restore_on_startup": True,
                    "managed": True,
                    "desired_state": "running",
                }
            )
            if records_input:
                row["had_conversation_input"] = True
            if owner:
                row["owner_id"] = owner[:256]
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def checkpoint_active(
        self,
        session_name: str,
        *,
        cwd: str,
        owner_id: str,
        resume_uuid: str = "",
        source: str = "controller",
        expected_generation: str = "",
    ) -> dict[str, Any]:
        """Persist enough owner-bound state to rebuild an active tmux tab.

        This is deliberately separate from ``touch``: a health/lifecycle
        checkpoint must not make an idle session look like the user interacted
        with it.  Parked tabs opt out of eager startup restore until explicitly
        resumed again.
        """
        normalized = str(resume_uuid or "").strip().lower()
        if normalized and not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            normalized,
        ):
            raise ValueError("invalid Codex resume UUID")
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("session owner is required")
        now = time.time()
        generation = str(expected_generation or "").strip()

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            if generation and str(row.get("generation") or "") != generation:
                raise ValueError("session generation changed during checkpoint")
            desired = str(row.get("desired_state") or "")
            if desired in {"parking", "parked", "deleting", "deleted"}:
                return dict(row)
            recorded_owner = str(row.get("owner_id") or "")
            if recorded_owner and recorded_owner != owner:
                raise ValueError("session owner changed during checkpoint")
            row.update(
                {
                    "parked": False,
                    "virtual": False,
                    "cwd": str(cwd or "")[:4096],
                    "owner_id": owner[:256],
                    "last_checkpoint": now,
                    "checkpoint_source": str(source or "controller")[:64],
                    "restore_on_startup": True,
                    "managed": True,
                    "desired_state": "running",
                }
            )
            row.setdefault("generation", secrets.token_hex(16))
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
        """Persist park/delete intent before mutating the volatile tmux pane."""
        if desired_state not in {"parking", "deleting"}:
            raise ValueError("invalid session lifecycle transition")
        owner = str(owner_id or "").strip()
        if not owner:
            raise ValueError("session owner is required")
        now = time.time()
        generation = str(expected_generation or "").strip()

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
                    "owner_id": owner[:256],
                    "managed": True,
                    "desired_state": desired_state,
                    "restore_on_startup": False,
                    "transition_at": now,
                }
            )
            if desired_state == "parking":
                row["parked"] = True
            row.setdefault("generation", secrets.token_hex(16))
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def set_resume_uuid(self, session_name: str, resume_uuid: str) -> dict[str, Any]:
        """Persist the exact root Codex thread used to recover this tab."""
        normalized = str(resume_uuid or "").strip().lower()
        if not re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            normalized,
        ):
            raise ValueError("invalid Codex resume UUID")

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            row["resume_uuid"] = normalized
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def clear_resume_uuid(
        self,
        session_name: str,
        *,
        owner_id: str,
        expected_generation: str,
    ) -> dict[str, Any]:
        """Forget a proven-empty bootstrap root without weakening lifecycle fences."""
        owner = str(owner_id or "").strip()
        generation = str(expected_generation or "").strip()
        if not owner or not re.fullmatch(r"[0-9a-f]{32}", generation):
            raise ValueError("owner and generation are required")

        def mutate(value: dict[str, Any]) -> dict[str, Any]:
            row = value.setdefault("sessions", {}).setdefault(session_name, {})
            if str(row.get("owner_id") or "") != owner:
                raise ValueError("session owner changed while clearing root")
            if str(row.get("generation") or "") != generation:
                raise ValueError("session generation changed while clearing root")
            if str(row.get("desired_state") or "") != "running":
                raise ValueError("session is not running while clearing root")
            row.pop("resume_uuid", None)
            return dict(row)

        _value, row = self.store.update(mutate)
        return row

    def get(self, session_name: str) -> dict[str, Any]:
        return dict(self.store.read().get("sessions", {}).get(session_name, {}))

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
        """Check an immutable lifecycle binding without mutating the registry."""
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

    def snapshot(self) -> dict[str, Any]:
        return self.store.read()


def _safe_unit_fragment(value: str) -> str:
    slug = "".join(ch if ch.isalnum() or ch in "_.-" else "-" for ch in value)[:80]
    digest = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
    return f"{slug or 'session'}-{digest}"


def _safe_slice_name(value: str) -> str:
    """Return a safe user-manager slice name for dashboard workloads."""
    candidate = (value or "").strip()
    if (
        candidate.endswith(".slice")
        and candidate not in {".slice", "-.slice"}
        and all(ch.isalnum() or ch in "_.-" for ch in candidate)
    ):
        return candidate
    return "codexworkload.slice"


def build_pytest_serialization_env_prefix(
    plugin_dir: str | Path, *, account: str = ""
) -> str:
    """Environment wrapper that serializes pytest from managed Codex sessions.

    PYTEST_PLUGINS works for both the ``pytest`` console script and
    ``python -m pytest``.  That closes the common bypass left by a PATH wrapper.
    Values are resolved here and quoted as complete ``env`` arguments. That
    preserves inherited paths containing whitespace without giving the target
    login shell another opportunity to split or reinterpret them.
    """

    if os.environ.get("TMUX_DASH_PYTEST_SERIALIZE", "1").lower() in {
        "0",
        "false",
        "no",
        "off",
    }:
        return ""
    lock_path = os.environ.get(
        "TMUX_DASH_PYTEST_LOCK_PATH",
        "/run/lock/tmux-dashboard/pytest-heavy.lock",
    )
    plugin_path = str(Path(plugin_dir).resolve())
    python_path = plugin_path
    if inherited := os.environ.get("PYTHONPATH", ""):
        python_path += os.pathsep + inherited
    plugins = _PYTEST_PLUGIN
    if inherited := os.environ.get("PYTEST_PLUGINS", ""):
        plugins += "," + inherited
    assignments = {
        _PYTEST_SERIAL_LOCK_ENV: lock_path,
        _PYTEST_GATE_REQUIRED_ENV: "1",
        _PYTEST_ACCOUNT_ENV: account,
        "PYTHONPATH": python_path,
        "PYTEST_PLUGINS": plugins,
    }
    return "env " + " ".join(
        shlex.quote(f"{key}={value}") for key, value in assignments.items()
    )


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


def default_codex_aggregate_cpu_quota_percent(cpu_count: int | None = None) -> int:
    """Reserve capacity across small hosts: 150% on 2 vCPU, 300% on 4+."""
    count = int(cpu_count if cpu_count is not None else (os.cpu_count() or 1))
    return max(100, min(300, 75 * max(1, count)))


def scoped_codex_command(
    session_name: str,
    command: str,
    *,
    memory_high_mb: int = 1536,
    memory_max_mb: int = 3072,
    tasks_max: int = 512,
    cpu_weight: int = 100,
    nice_level: int = 10,
    slice_name: str = "codexworkload.slice",
    aggregate_cpu_quota_percent: int | None = None,
    aggregate_cpu_weight: int = 25,
    aggregate_io_weight: int = 25,
    aggregate_memory_high_percent: int = 75,
    aggregate_memory_max_percent: int = 90,
    aggregate_tasks_max: int = 4096,
) -> str:
    """Return a burst-friendly systemd scope wrapper with a safe fallback.

    The fallback keeps session launch working on hosts without a user systemd
    manager.  A successful scoped command is never run a second time.
    """

    unit = f"codex-{_safe_unit_fragment(session_name)}-{int(time.time())}"
    inner = "exec " + command.strip()
    workload_slice = _safe_slice_name(slice_name)
    aggregate_quota = (
        default_codex_aggregate_cpu_quota_percent()
        if aggregate_cpu_quota_percent is None
        else max(1, int(aggregate_cpu_quota_percent))
    )
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
        f"--property=MemoryHigh={int(memory_high_mb)}M",
        f"--property=MemoryMax={int(memory_max_mb)}M",
        f"--property=TasksMax={int(tasks_max)}",
        f"--property=CPUWeight={max(1, min(int(cpu_weight), 10000))}",
        "--property=ManagedOOMMemoryPressure=kill",
        "--property=ManagedOOMMemoryPressureLimit=70%",
        "--property=ManagedOOMSwap=kill",
        "bash",
        "-lc",
        inner,
    )
    wrapped = shlex.join(run)
    core_slice_properties = [
        f"CPUQuota={aggregate_quota}%",
        f"CPUWeight={max(1, min(int(aggregate_cpu_weight), 10000))}",
        f"MemoryHigh={max(1, min(int(aggregate_memory_high_percent), 100))}%",
        f"MemoryMax={max(1, min(int(aggregate_memory_max_percent), 100))}%",
        f"TasksMax={max(1, int(aggregate_tasks_max))}",
    ]
    configure_core_slice = shlex.join(
        user_systemd_argv(
            "systemctl",
            "--user",
            "set-property",
            "--runtime",
            workload_slice,
            *core_slice_properties,
        )
    )
    configure_io_weight = shlex.join(
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
        f"{configure_core_slice} >/dev/null 2>&1 || true; "
        f"{configure_io_weight} >/dev/null 2>&1 || true; "
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
