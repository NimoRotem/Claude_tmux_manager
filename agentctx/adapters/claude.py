"""Claude Code adapter.

Claude autoloads skills (name + description) and reads them on demand, so the
rendered CLAUDE.md carries no skills index — adding one would duplicate what the
harness already provides and burn context. Settings are JSON; MCP servers live in
a separate `.mcp.json`; permissions are per-tool allow/deny lists; the thinking
budget stands in for Codex's reasoning effort.
"""

from __future__ import annotations

import json
from pathlib import Path

from .base import BackendAdapter, RenderedFile, clean_env


class ClaudeAdapter(BackendAdapter):
    key = "claude"
    label = "Claude Code"
    context_filename = "CLAUDE.md"
    prompts_dirname = "commands"
    settings_filename = "settings.json"
    skills_are_autoloaded = True

    def instructions_preamble(self, skills: list[dict]) -> str:
        # Deliberately empty: Claude discovers skills itself. See the class
        # docstring — this asymmetry with Codex is the whole reason skills are
        # stored once and indexed differently.
        return ""

    def render_settings(self, home: Path, *, policy: dict, tier: dict,
                        mcp: dict, ignore: list[str], render_env: dict) -> list[RenderedFile]:
        out: list[RenderedFile] = []

        claude_policy = (policy or {}).get("claude", {}) or {}
        claude_tier = (tier or {}).get("claude", {}) or {}

        permissions = {}
        if claude_policy.get("default_mode"):
            permissions["defaultMode"] = claude_policy["default_mode"]
        if claude_policy.get("allow"):
            permissions["allow"] = list(claude_policy["allow"])
        # The ignore list is enforceable here, so enforce it as well as saying it.
        # Not at full access though: a deny rule beside `allow: ["*"]` reopens the
        # acceptance prompt this level exists to avoid, and the prose in the
        # context file still carries the same instruction.
        deny = list(claude_policy.get("deny") or [])
        if claude_policy.get("default_mode") != "bypassPermissions":
            deny += [f"Read({p})" for p in ignore if not p.startswith("/home/*")]
        if deny:
            permissions["deny"] = deny

        settings = {}
        if permissions:
            settings["permissions"] = permissions
        if claude_tier.get("model"):
            settings["model"] = claude_tier["model"]
        env_block = dict(claude_policy.get("env") or {})
        budget = claude_tier.get("thinking_budget")
        if budget:
            # Claude expresses depth as a token budget; Codex as an enum. The
            # tier name is the only thing that maps — see core/runtime.yaml.
            env_block["MAX_THINKING_TOKENS"] = str(budget)
        if env_block:
            settings["env"] = env_block

        out.append(RenderedFile(
            path=home / self.settings_filename,
            content=json.dumps(settings, indent=2) + "\n",
            note="model, thinking budget and permission rules",
        ))

        servers = {}
        for name, spec in (mcp or {}).items():
            entry = {"command": spec["command"], "args": list(spec.get("args") or [])}
            env = clean_env(spec.get("env"), render_env)
            if env:
                entry["env"] = env
            if spec.get("cwd"):
                entry["cwd"] = spec["cwd"]
            servers[name] = entry
        out.append(RenderedFile(
            path=home / ".mcp.json",
            content=json.dumps({"mcpServers": servers}, indent=2) + "\n",
            note=f"{len(servers)} MCP server(s)",
        ))
        return out

    def render_event_hooks(self, home: Path, emit_script: Path) -> list[RenderedFile]:
        """Claude's hooks are the richer of the two event sources.

        We wire the three that carry information Codex's single `notify` program
        cannot: before a tool runs, when the agent asks for input, and when a
        turn ends. The emitted vocabulary is identical on both sides — see
        runtime/events/emit.sh — so downstream consumers never branch.
        """
        hooks = {
            "PreToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": f"{emit_script} tool.start"}]}],
            "PostToolUse": [{"matcher": "*", "hooks": [
                {"type": "command", "command": f"{emit_script} tool.end"}]}],
            "Notification": [{"hooks": [
                {"type": "command", "command": f"{emit_script} agent.waiting"}]}],
            "Stop": [{"hooks": [
                {"type": "command", "command": f"{emit_script} turn.end"}]}],
        }
        return [RenderedFile(
            path=home / "settings.hooks.json",
            content=json.dumps({"hooks": hooks}, indent=2) + "\n",
            note="merged into settings.json by the renderer",
        )]
