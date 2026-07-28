"""The dashboard's entry point into agentctx.

Kept separate from app.py so the toolkit stays usable on its own (and testable
without importing a 23k-line web app), and so app.py has exactly one import to
reason about.

Called at two moments:

* **session start** — render the backend's context tree into the config home
  that session will use, bridge memory both ways, and hand back the env the pane
  must export.
* **on demand** — when someone edits `core/`, re-render every home so live
  sessions pick the change up on their next turn.

Every function here is best-effort: a failure to render context must never stop
a session from launching. The dashboard logs it and carries on with whatever
config was already on disk.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

from . import memory, memory_sync, render

logger = logging.getLogger("agentctx")

ROOT = Path(__file__).resolve().parent
STATE = Path(os.environ.get("AGENTCTX_STATE") or (ROOT / "state"))
# The dashboard launches every session with its backend's full-access flags, so
# the rendered policy has to match. A mismatch is not a security posture, it is
# a hung pane: Claude stops on an acceptance prompt, Codex on an approval.
DEFAULT_LEVEL = os.environ.get("AGENTCTX_POLICY_LEVEL", "full-access")
EMIT = ROOT / "runtime" / "events" / "emit.sh"


def prepare_session(backend: str, home: Path, cwd: str | None, *,
                    session_name: str = "", level: str | None = None,
                    tier: str | None = None, render_env: dict | None = None) -> dict:
    """Render context + sync memory for one session. Never raises."""
    out = {"backend": backend, "home": str(home), "rendered": [], "memory": {}, "errors": []}
    scope = memory.encode_scope(cwd)
    try:
        digest = memory.digest(scope, extra_scope="global" if scope != "global" else None)
    except Exception as exc:
        digest, _ = "", out["errors"].append(f"digest: {exc}")
    try:
        result = render.render(backend, Path(home), level=level or DEFAULT_LEVEL, tier=tier,
                               memory_digest=digest, render_env=render_env)
        out["rendered"] = result.written
    except Exception as exc:
        logger.exception("agentctx render failed for %s", backend)
        out["errors"].append(f"render: {exc}")
    try:
        out["memory"] = memory_sync.sync_session(backend, Path(home), cwd)
    except Exception as exc:
        logger.exception("agentctx memory sync failed for %s", backend)
        out["errors"].append(f"memory: {exc}")
    return out


def session_env(backend: str, session_name: str, cwd: str | None = None) -> dict:
    """Env a session's pane must carry.

    `AGENTCTX_BACKEND` and `AGENTCTX_SESSION` are what make the event stream
    attributable — without them every hook line reads "unknown", which is the
    same as having no event stream at all.
    """
    return {
        "AGENTCTX_BACKEND": backend,
        "AGENTCTX_SESSION": session_name,
        "AGENTCTX_STATE": str(STATE),
        "AGENTCTX_SCOPE": memory.encode_scope(cwd),
        "AGENTCTX_HOME": str(ROOT.parent),
    }


def export_lines(backend: str, session_name: str, cwd: str | None = None) -> str:
    """The same env as a shell snippet, for typing into a tmux pane."""
    import shlex
    return " ".join(f"export {k}={shlex.quote(v)};"
                    for k, v in session_env(backend, session_name, cwd).items())


def rerender_all(homes: dict, **kwargs) -> list:
    """Re-render every backend home after a core/ edit."""
    results = []
    for backend, home in (homes or {}).items():
        try:
            results.append(render.render(backend, Path(home), **kwargs))
        except Exception:
            logger.exception("agentctx re-render failed for %s at %s", backend, home)
    return results


def summary() -> dict:
    """What the dashboard shows in Settings → Context."""
    skills = render.load_skills()
    return {
        "root": str(ROOT),
        "state": str(STATE),
        "skills": [{"name": s["name"], "description": s["description"]} for s in skills],
        "prompts": [p["name"] for p in render.load_prompts()],
        "ignore_patterns": len(render.load_ignore()),
        "policy_levels": list((render._read_yaml(render.CORE / "policy.yaml")
                               .get("levels") or {})),
        "runtime_tiers": list((render._read_yaml(render.CORE / "runtime.yaml")
                               .get("tiers") or {})),
        "memory_scopes": sorted(d.name for d in memory.MEM_ROOT.glob("*") if d.is_dir())
                         if memory.MEM_ROOT.exists() else [],
    }
