#!/usr/bin/env python3
"""Transparent stdio MCP proxy that leases Chrome only for real tool calls.

Codex keeps MCP server processes alive for an entire session. Treating that
process (or its persistent CDP socket) as browser activity prevents Chrome from
ever being reclaimed. This proxy forwards MCP JSONL unchanged but acquires a
renewable dashboard lease only while a ``tools/call`` request is in flight.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path


def _host_home() -> Path:
    """The dashboard's home, not the session account's.

    Sessions now run as their own UNIX user, so $HOME points at that account.
    The controller socket, the browser profiles and the shared playwright CLI
    all live in the dashboard owner's home and are reached explicitly.
    """
    import os
    return Path(os.environ.get("TMUX_DASH_HOST_HOME") or Path.home())
from typing import Any

CONTROLLER_SOCKET = Path(
    os.environ.get(
        "TMUX_DASH_CONTROLLER_SOCKET",
        str(_host_home() / ".tmux-dashboard" / "controller.sock"),
    )
)
BROWSER_ID = os.environ.get("TMUX_DASH_BROWSER_ID", "default")
try:
    BROWSER_CDP_PORT = int(os.environ.get("TMUX_DASH_BROWSER_CDP_PORT", "9222"))
except ValueError:
    BROWSER_CDP_PORT = 9222
BROWSER_OUTPUT_DIR = Path(
    os.environ.get(
        "TMUX_DASH_BROWSER_OUTPUT_DIR",
        str(_host_home() / ".playwright-mcp" / BROWSER_ID),
    )
)
LEASE_TTL = max(60, int(os.environ.get("TMUX_DASH_BROWSER_LEASE_TTL", "300")))


def _rpc(message: dict[str, Any], timeout: float = 45) -> dict[str, Any]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.settimeout(timeout)
        client.connect(str(CONTROLLER_SOCKET))
        client.sendall(json.dumps(message, separators=(",", ":")).encode() + b"\n")
        chunks = bytearray()
        while not chunks.endswith(b"\n"):
            part = client.recv(65536)
            if not part:
                raise ConnectionError("dashboard controller closed the connection")
            chunks.extend(part)
            if len(chunks) > 4 * 1024 * 1024:
                raise ValueError("dashboard controller response is too large")
    value = json.loads(chunks.decode("utf-8", "replace"))
    return value if isinstance(value, dict) else {"ok": False}


def _request_id_for_browser_call(payload: object) -> object | None:
    if not isinstance(payload, dict) or payload.get("method") != "tools/call":
        return None
    request_id = payload.get("id")
    if request_id is None:
        return None
    params = payload.get("params") or {}
    name = str(params.get("name") or "") if isinstance(params, dict) else ""
    # This proxy is dedicated to Playwright MCP. The prefix check keeps future
    # non-browser housekeeping tools from unnecessarily waking Chrome.
    return request_id if name.startswith("browser_") else None


class LeaseRegistry:
    def __init__(self):
        self._tokens: dict[object, str] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def acquire(self, request_id: object, tool_name: str) -> None:
        try:
            response = _rpc(
                {
                    "op": "browser_acquire",
                    "browser_id": BROWSER_ID,
                    "kind": "agent",
                    "owner": f"playwright-mcp:{os.getpid()}:{tool_name}"[:128],
                    "ttl": LEASE_TTL,
                    "mode": "headless",
                }
            )
            token = str((response.get("lease") or {}).get("token") or "")
            if response.get("ok") and token:
                with self._lock:
                    self._tokens[request_id] = token
            else:
                print(
                    f"browser lease unavailable: {response.get('error', 'unknown error')}",
                    file=sys.stderr,
                    flush=True,
                )
        except Exception as exc:
            # Forward the MCP request even if the controller is restarting; the
            # upstream returns its normal CDP error instead of losing protocol.
            print(f"browser lease controller unavailable: {exc}", file=sys.stderr, flush=True)

    def release(self, request_id: object) -> None:
        with self._lock:
            token = self._tokens.pop(request_id, "")
        if token:
            try:
                _rpc({"op": "browser_release", "token": token}, timeout=10)
            except Exception:
                pass

    def renew_loop(self) -> None:
        while not self._stop.wait(max(30, LEASE_TTL // 2)):
            with self._lock:
                tokens = list(self._tokens.values())
            for token in tokens:
                try:
                    _rpc(
                        {"op": "browser_renew", "token": token, "ttl": LEASE_TTL},
                        timeout=10,
                    )
                except Exception:
                    pass

    def close(self) -> None:
        self._stop.set()
        with self._lock:
            request_ids = list(self._tokens)
        for request_id in request_ids:
            self.release(request_id)


def main() -> int:
    upstream_command = sys.argv[1:] or [
        "node",
        str(_host_home() / ".claude-browser" / "node_modules" / "@playwright" / "mcp" / "cli.js"),
        "--cdp-endpoint",
        f"http://127.0.0.1:{BROWSER_CDP_PORT}",
        "--output-dir",
        str(BROWSER_OUTPUT_DIR),
    ]
    upstream = subprocess.Popen(
        upstream_command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
    )
    assert upstream.stdin is not None
    assert upstream.stdout is not None
    assert upstream.stderr is not None
    leases = LeaseRegistry()

    def client_to_upstream() -> None:
        try:
            for line in sys.stdin.buffer:
                try:
                    payload = json.loads(line)
                    request_id = _request_id_for_browser_call(payload)
                    if request_id is not None:
                        params = payload.get("params") or {}
                        leases.acquire(request_id, str(params.get("name") or "browser"))
                except (ValueError, TypeError):
                    pass
                upstream.stdin.write(line)
                upstream.stdin.flush()
        finally:
            try:
                upstream.stdin.close()
            except Exception:
                pass

    def copy_stderr() -> None:
        for chunk in iter(lambda: upstream.stderr.read(65536), b""):
            sys.stderr.buffer.write(chunk)
            sys.stderr.buffer.flush()

    threading.Thread(target=client_to_upstream, daemon=True).start()
    threading.Thread(target=copy_stderr, daemon=True).start()
    threading.Thread(target=leases.renew_loop, daemon=True).start()

    try:
        for line in upstream.stdout:
            try:
                response = json.loads(line)
                if isinstance(response, dict) and response.get("id") is not None:
                    leases.release(response["id"])
            except (ValueError, TypeError):
                pass
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        return upstream.wait()
    finally:
        leases.close()
        if upstream.poll() is None:
            upstream.terminate()
            try:
                upstream.wait(timeout=5)
            except subprocess.TimeoutExpired:
                upstream.kill()


if __name__ == "__main__":
    raise SystemExit(main())
