"""Headless invocation (abstraction 10).

    claude -p "<prompt>" --output-format stream-json
    codex exec "<prompt>" --json

Same idea, different flags and completely different JSON schemas. This wrapper
takes a prompt and returns a normalised result plus a stream of events in the
same vocabulary emit.sh uses (abstraction 7), so a caller writes one loop and
gets either backend.

Normalised event names: turn.start · tool.start · tool.end · agent.message ·
turn.end · error. Anything a backend emits that does not map is passed through
as `raw` with the original payload attached, rather than dropped — a schema this
volatile will change again, and silently swallowing new event types is how a
caller ends up waiting forever for a turn.end that arrived under a new name.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class ExecResult:
    backend: str
    ok: bool
    final_message: str
    events: list = field(default_factory=list)
    returncode: int = 0
    stderr: str = ""


def _cmd(backend: str, prompt: str, *, model: str = "", effort: str = "",
         cwd: str = "", extra: list | None = None) -> list:
    if backend == "claude":
        cmd = ["claude", "-p", prompt, "--output-format", "stream-json",
               "--verbose", "--dangerously-skip-permissions"]
        if model:
            cmd += ["--model", model]
    elif backend == "codex":
        cmd = ["codex", "exec", "--json", "--skip-git-repo-check",
               "--dangerously-bypass-approvals-and-sandbox"]
        if model:
            cmd += ["--model", model]
        if effort:
            cmd += ["-c", f"model_reasoning_effort={effort}"]
        if cwd:
            cmd += ["-C", cwd]
        cmd.append(prompt)
    else:
        raise KeyError(f"unknown backend {backend!r}")
    return cmd + list(extra or [])


def _norm_claude(obj: dict) -> dict | None:
    """Claude's stream-json → the shared vocabulary."""
    kind = obj.get("type")
    if kind == "system" and obj.get("subtype") == "init":
        return {"event": "turn.start", "detail": obj.get("session_id", "")}
    if kind == "assistant":
        blocks = ((obj.get("message") or {}).get("content")) or []
        texts = [b.get("text", "") for b in blocks if b.get("type") == "text"]
        tools = [b for b in blocks if b.get("type") == "tool_use"]
        if tools:
            return {"event": "tool.start", "detail": tools[0].get("name", ""),
                    "input": tools[0].get("input")}
        if texts:
            return {"event": "agent.message", "detail": "\n".join(texts).strip()}
        return None
    if kind == "user":
        blocks = ((obj.get("message") or {}).get("content")) or []
        if any(b.get("type") == "tool_result" for b in blocks):
            return {"event": "tool.end", "detail": ""}
        return None
    if kind == "result":
        return {"event": "turn.end",
                "detail": obj.get("result", ""),
                "is_error": bool(obj.get("is_error")),
                "usage": obj.get("usage")}
    return {"event": "raw", "detail": kind or "", "raw": obj}


def _norm_codex(obj: dict) -> dict | None:
    """Codex's exec --json → the shared vocabulary."""
    kind = obj.get("type") or obj.get("msg", {}).get("type") or ""
    payload = obj.get("msg") if isinstance(obj.get("msg"), dict) else obj

    if kind in ("session_configured", "thread.started", "task_started"):
        return {"event": "turn.start", "detail": payload.get("session_id", "")}
    if kind in ("exec_command_begin", "mcp_tool_call_begin", "patch_apply_begin",
                "item.started"):
        return {"event": "tool.start",
                "detail": payload.get("command") or payload.get("tool") or kind}
    if kind in ("exec_command_end", "mcp_tool_call_end", "patch_apply_end",
                "item.completed"):
        return {"event": "tool.end", "detail": payload.get("exit_code", "")}
    if kind in ("agent_message", "item.agent_message"):
        return {"event": "agent.message",
                "detail": (payload.get("message") or payload.get("text") or "").strip()}
    if kind in ("task_complete", "turn.completed", "thread.completed"):
        return {"event": "turn.end", "detail": payload.get("last_agent_message", ""),
                "usage": payload.get("usage")}
    if kind in ("error", "stream_error", "turn.failed"):
        return {"event": "error", "detail": payload.get("message") or str(payload)}
    if kind == "token_count":
        return {"event": "raw", "detail": "token_count", "raw": payload}
    return {"event": "raw", "detail": kind, "raw": obj}


NORMALISERS = {"claude": _norm_claude, "codex": _norm_codex}


def run(backend: str, prompt: str, *, model: str = "", effort: str = "",
        cwd: str = "", timeout: int = 3000, env: dict | None = None,
        on_event=None) -> ExecResult:
    """Run one headless turn. Streams normalised events to `on_event` if given."""
    cmd = _cmd(backend, prompt, model=model, effort=effort, cwd=cwd)
    norm = NORMALISERS[backend]
    run_env = dict(os.environ)
    run_env.setdefault("AGENTCTX_BACKEND", backend)
    if env:
        run_env.update(env)

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=cwd or None, env=run_env)
    events, final = [], ""
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                # Not every line is JSON on either backend (banners, warnings).
                continue
            evt = norm(obj)
            if not evt:
                continue
            events.append(evt)
            if evt["event"] in ("agent.message", "turn.end") and evt.get("detail"):
                final = evt["detail"]
            if on_event:
                on_event(evt)
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        return ExecResult(backend, False, final, events, 124, "timed out")

    stderr = proc.stderr.read() if proc.stderr else ""
    ok = proc.returncode == 0 and not any(e["event"] == "error" for e in events)
    return ExecResult(backend, ok, final.strip(), events, proc.returncode, stderr.strip())


def shell_command(backend: str, prompt: str, **kwargs) -> str:
    """The command line, for logging or for handing to tmux."""
    return " ".join(shlex.quote(c) for c in _cmd(backend, prompt, **kwargs))
