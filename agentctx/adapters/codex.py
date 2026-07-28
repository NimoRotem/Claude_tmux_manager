"""Codex adapter.

Two things differ in kind, not just in spelling:

* **Skills are not discovered.** Codex loads AGENTS.md and nothing else, so the
  renderer injects an index table (name | description | absolute path) plus an
  explicit instruction to read the SKILL.md in full before acting. The skill
  bodies are never inlined — that is what blows up a context window.
* **Everything is one TOML file.** Model, reasoning effort, sandbox mode,
  approval policy and MCP servers all live in config.toml, and the dashboard
  only owns part of it, so this adapter emits managed top-level keys and MCP
  tables that the renderer merges into whatever else is in there.
"""

from __future__ import annotations

from pathlib import Path

from .base import BackendAdapter, RenderedFile, clean_env


def _toml_escape(value: str) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _toml_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_toml_value(v) for v in value) + "]"
    return f'"{_toml_escape(value)}"'


class CodexAdapter(BackendAdapter):
    key = "codex"
    label = "Codex"
    context_filename = "AGENTS.md"
    prompts_dirname = "prompts"
    settings_filename = "config.toml"
    skills_are_autoloaded = False

    def instructions_preamble(self, skills: list[dict]) -> str:
        """The skills index Codex needs and Claude does not."""
        if not skills:
            return ""
        rows = "\n".join(
            f"| `{s['name']}` | {s['description']} | `{s['path']}` |" for s in skills
        )
        return (
            "## Skills\n\n"
            "These are procedures written for this machine. The description is a "
            "routing hint only — when a task matches one, **read that `SKILL.md` "
            "in full before acting on it**.\n\n"
            "| Skill | Use it when | Read |\n"
            "|---|---|---|\n"
            f"{rows}\n"
        )

    def render_settings(self, home: Path, *, policy: dict, tier: dict,
                        mcp: dict, ignore: list[str], render_env: dict) -> list[RenderedFile]:
        codex_policy = (policy or {}).get("codex", {}) or {}
        codex_tier = (tier or {}).get("codex", {}) or {}

        managed = {}
        if codex_tier.get("model"):
            managed["model"] = codex_tier["model"]
        if codex_tier.get("model_reasoning_effort"):
            managed["model_reasoning_effort"] = codex_tier["model_reasoning_effort"]
        if codex_policy.get("sandbox_mode"):
            managed["sandbox_mode"] = codex_policy["sandbox_mode"]
        if codex_policy.get("approval_policy"):
            managed["approval_policy"] = codex_policy["approval_policy"]

        # TOML is position-sensitive: every key after a [table] header belongs to
        # that table. So managed SCALARS and managed TABLES cannot live in one
        # block — writing the block at the top swallowed the file's own
        # top-level keys into our last MCP env table ("invalid type: boolean
        # false, expected a string"), and writing it at the bottom would swallow
        # ours into the user's. They are emitted separately; the renderer puts
        # scalars first and tables last.
        scalars = [
            "# Managed by agentctx — edits between the markers are overwritten.",
            "# Source: agentctx/core/{runtime,policy,mcp}.yaml",
        ]
        for key, value in managed.items():
            scalars.append(f"{key} = {_toml_value(value)}")

        lines = []
        for name, spec in (mcp or {}).items():
            lines.append(f"[mcp_servers.{name}]")
            lines.append(f"command = {_toml_value(spec['command'])}")
            if spec.get("args"):
                lines.append(f"args = {_toml_value(list(spec['args']))}")
            if spec.get("startup_timeout_s"):
                lines.append(f"startup_timeout_sec = {_toml_value(int(spec['startup_timeout_s']))}")
            env = clean_env(spec.get("env"), render_env)
            if env:
                lines.append(f"[mcp_servers.{name}.env]")
                for k, v in env.items():
                    lines.append(f"{k} = {_toml_value(v)}")
            lines.append("")

        return [
            RenderedFile(
                path=home / self.settings_filename,
                content="\n".join(scalars),
                note="managed top-level keys (model, effort, sandbox, approval)",
            ),
            RenderedFile(
                path=home / "config.mcp.toml",
                content="\n".join(lines),
                note="managed [mcp_servers.*] tables — appended after all scalars",
            ),
        ]

    def render_event_hooks(self, home: Path, emit_script: Path) -> list[RenderedFile]:
        """Codex has one notify program where Claude has four hook points.

        So the same event vocabulary arrives coarser here: `notify` fires on the
        events Codex chooses to surface, and the TUI-regex fallback in
        detect.toml covers what it does not. Consumers must not assume the two
        backends emit the same *number* of events, only the same names.
        """
        return [RenderedFile(
            path=home / "notify.toml",
            content=(
                "# Managed by agentctx. Merged into config.toml by the renderer.\n"
                f'notify = ["{emit_script}", "codex.notify"]\n'
            ),
            note="single notify program (Codex has no per-tool hooks)",
        )]

    def ignore_instruction(self, patterns: list[str]) -> str:
        """Codex has no ignore file, so the prose IS the enforcement."""
        base = super().ignore_instruction(patterns)
        return base + (
            "\nThere is no ignore file backing this on your side — the list above "
            "is the whole mechanism. Treat it as a hard rule, including for "
            "`grep`, `rg`, `find` and shell globs, not only for file reads.\n"
        )
