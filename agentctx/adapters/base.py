"""What every backend adapter must answer."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path


def interpolate(value: str, env: dict | None = None) -> str:
    """Expand ${VAR} from the render environment. Unset vars become ''."""
    env = env if env is not None else os.environ
    return re.sub(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", lambda m: env.get(m.group(1), ""), str(value))


def clean_env(env: dict, render_env: dict | None = None) -> dict:
    """Interpolate an MCP server's env and drop entries that resolve to nothing.

    An empty credential is worse than a missing one: the server starts, the tool
    appears in the agent's list, and every call fails with an auth error that
    reads like the service is down.
    """
    out = {}
    for k, v in (env or {}).items():
        resolved = interpolate(v, render_env)
        if resolved:
            out[k] = resolved
    return out


@dataclass
class RenderedFile:
    """One file the renderer will write, and why."""
    path: Path
    content: str
    mode: int = 0o644
    note: str = ""


class BackendAdapter:
    key: str = ""
    label: str = ""
    context_filename: str = ""
    prompts_dirname: str = ""
    skills_dirname: str = "skills"
    settings_filename: str = ""
    skills_are_autoloaded: bool = False

    # ---- 1. instructions --------------------------------------------------
    def context_path(self, home: Path) -> Path:
        return home / self.context_filename

    def instructions_preamble(self, skills: list[dict]) -> str:
        """Backend-specific text prepended/appended to the shared body."""
        return ""

    # ---- 2. skills --------------------------------------------------------
    def skills_root(self, home: Path) -> Path:
        return home / self.skills_dirname

    # ---- 3. prompts -------------------------------------------------------
    def prompts_root(self, home: Path) -> Path:
        return home / self.prompts_dirname

    def rewrite_prompt_args(self, body: str) -> str:
        """Both backends take $ARGUMENTS / $1..$9; override if that changes."""
        return body

    # ---- 4/5/9. settings, policy, runtime ---------------------------------
    def render_settings(self, home: Path, *, policy: dict, tier: dict,
                        mcp: dict, ignore: list[str], render_env: dict) -> list[RenderedFile]:
        raise NotImplementedError

    # ---- 7. events --------------------------------------------------------
    def render_event_hooks(self, home: Path, emit_script: Path) -> list[RenderedFile]:
        return []

    # ---- 12. ignore -------------------------------------------------------
    def ignore_instruction(self, patterns: list[str]) -> str:
        """Prose that lands in the context file, for the side that cannot enforce."""
        listed = "\n".join(f"- `{p}`" for p in patterns)
        return (
            "## Paths to leave alone\n\n"
            "Do not read, write, search or list the following. Secrets must never "
            "reach a transcript, and the generated trees below waste the context "
            "window without answering anything.\n\n"
            f"{listed}\n"
        )
