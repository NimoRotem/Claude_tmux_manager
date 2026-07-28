"""Backend adapters: everything the dashboard has to know that differs between
Claude Code and Codex, in one place.

The dashboard itself is backend-agnostic. tmux management, the terminal
WebSocket, chat, notes, skills, project publishing, team mode, the watchdogs and
the browser governor behave identically no matter which CLI is in the pane. What
genuinely differs is a short list of seams:

    launch · resume · liveness · TUI parsing · response extraction · transcripts
    usage windows · auth · config home · context filename · settings format
    model + reasoning effort · ignore rules

Each seam is declared here as data (or as a small pure function) and looked up
through ``get_provider(key)``. ``app.py`` holds the implementations that need its
own module state and dispatches on ``provider.key``; nothing outside this module
should branch on the string "codex" or "claude".

Selection order for a session, most specific first:
  1. the live process tree in its tmux pane (authoritative — see app._session_provider)
  2. the backend recorded when the dashboard created the session
  3. DEFAULT_PROVIDER (TMUX_DASH_AGENT, else sniffed from TMUX_DASH_NEW_SESSION_CMD)
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class ProviderSpec:
    # --- identity -----------------------------------------------------------
    key: str                     # "claude" | "codex" — the only stable handle
    label: str                   # human name, for UI copy
    cli: str                     # executable name on PATH

    # --- 1. instructions / context file -------------------------------------
    context_filename: str        # CLAUDE.md | AGENTS.md
    config_home_env: str         # CLAUDE_CONFIG_DIR | CODEX_HOME
    config_home_default: Path
    user_config_prefix: str      # per-member isolated home, ~/<prefix><user id>

    # --- 2/3. skills + reusable prompts -------------------------------------
    skills_dirname: str          # <config home>/skills for both today
    prompts_dirname: str         # commands (Claude) | prompts (Codex)
    skills_are_autoloaded: bool  # Claude discovers them; Codex needs an index

    # --- 4/5/9. settings, policy, runtime tuning ----------------------------
    settings_format: str         # "json" | "toml"
    settings_filename: str       # settings.json | config.toml
    mcp_filename: str            # .mcp.json | config.toml (same file as settings)
    supports_effort: bool        # Codex has model_reasoning_effort; Claude does not

    # --- 1. launch ----------------------------------------------------------
    full_access_flags: str       # the "just do it" flag set used by the dashboard
    model_flag: str              # --model
    default_model: str

    # --- 2. resume ----------------------------------------------------------
    resume_last: str             # continue the most recent conversation in a cwd
    resume_by_id: str            # continue one exact conversation ("{id}" slot)

    # --- 3. liveness --------------------------------------------------------
    process_names: tuple         # /proc/<pid>/comm values that mean "running"
    process_argv_re: str         # argv fallback for wrapper scripts

    # --- 4. TUI parsing -----------------------------------------------------
    busy_markers: tuple          # substrings that mean "a turn is in flight"
    tool_output_markers: tuple   # line prefixes for tool RESULT blocks
    tool_header_re: str          # tool-call header lines ("Bash(", "Ran ", ...)
    idle_hint_re: str            # the CLI's own idle footer/tip

    # --- 6. transcripts -----------------------------------------------------
    transcript_globs: tuple      # relative to the config home

    # --- 7/8. usage + auth --------------------------------------------------
    usage_kind: str              # "anthropic_oauth" | "codex_app_server"
    auth_kind: str               # "anthropic" | "chatgpt"
    api_key_env: str             # ANTHROPIC_API_KEY | OPENAI_API_KEY
    api_key_prefix: str          # sk-ant- | sk-
    credentials_filename: str

    # --- 12. ignore rules ---------------------------------------------------
    ignore_filename: str
    ignore_is_enforced: bool     # Claude enforces a deny list; Codex needs prose

    # --- misc ---------------------------------------------------------------
    docs_url: str = ""
    aliases: tuple = field(default_factory=tuple)

    # ---------------------------------------------------------------- helpers
    def config_home(self, env: dict | None = None) -> Path:
        env = env if env is not None else os.environ
        return Path(env.get(self.config_home_env) or self.config_home_default)

    def user_config_home(self, user_id: str) -> Path:
        return Path.home() / f"{self.user_config_prefix}{user_id}"

    def launch_cmd(self, base: str = "", model: str = "", effort: str = "",
                   full_access: bool = True) -> str:
        """Build the command the dashboard types into a fresh tmux pane."""
        out = (base or "").strip() or self.cli
        if full_access and not _has_access_flag(out, self):
            out += " " + self.full_access_flags
        if model and self.model_flag not in out and " -m " not in f" {out} ":
            out += f" {self.model_flag} {shlex.quote(model)}"
        if effort and self.supports_effort and "reasoning" not in out:
            out += f" -c model_reasoning_effort={shlex.quote(effort)}"
        return out

    def resume_cmd(self, base: str = "", session_id: str = "", model: str = "") -> str:
        """Reattach to a conversation. An exact id is always preferred: sessions
        share a cwd all the time here, and "most recent in this directory" then
        picks up somebody else's work — the bug that made `codex resume --last`
        and Claude's bare `--continue` both unsafe."""
        tmpl = self.resume_by_id if session_id else self.resume_last
        out = tmpl.format(cli=self.cli, id=shlex.quote(session_id or ""))
        for flag in self.full_access_flags.split():
            if flag in (base or "") and flag not in out:
                out += " " + flag
        if not _has_access_flag(out, self):
            out += " " + self.full_access_flags
        if model and self.model_flag not in out:
            out += f" {self.model_flag} {shlex.quote(model)}"
        return out

    def is_process(self, comm: str, argv: str) -> bool:
        """Does this /proc entry represent the agent itself?"""
        if (comm or "").strip().lower() in self.process_names:
            return True
        return bool(re.search(self.process_argv_re, (argv or "").lower()))

    def looks_busy(self, pane_text: str) -> bool:
        low = (pane_text or "").lower()
        return any(m in low for m in self.busy_markers)

    def context_path(self, config_home: Path) -> Path:
        return Path(config_home) / self.context_filename

    def memory_path(self, config_home: Path) -> Path:
        """Where the agent actually READS long-term memory from.

        These differ in shape, not just in name: Claude Code keeps one memory
        directory per project (``projects/<encoded cwd>/memory/MEMORY.md``),
        Codex keeps a single global ``MEMORY.md`` whose entries are scoped
        internally. Callers that want "the file this agent will read" ask here;
        callers that want the dashboard's own per-project store use
        ``project_memory_dir`` and let the sync layer bridge the two.
        """
        return Path(config_home) / "MEMORY.md"

    def project_memory_dir(self, config_home: Path, encoded_cwd: str) -> Path:
        return Path(config_home) / "projects" / encoded_cwd / "memory"

    def reads_project_memory(self) -> bool:
        """True when the CLI itself loads projects/<cwd>/memory/MEMORY.md.

        Codex does not — it reads one global MEMORY.md — so writing there and
        assuming Codex will pick it up is a silent no-op. The dashboard keeps
        the per-project store either way and syncs it into whatever the backend
        does read.
        """
        return self.key == "claude"


def _has_access_flag(cmd: str, spec: "ProviderSpec") -> bool:
    cmd = f" {cmd} "
    for flag in ("--dangerously-skip-permissions", "--dangerously-bypass-approvals-and-sandbox",
                 "--yolo", "--sandbox", " -s "):
        if flag in cmd:
            return True
    return False


CLAUDE = ProviderSpec(
    key="claude",
    label="Claude Code",
    cli="claude",
    context_filename="CLAUDE.md",
    config_home_env="CLAUDE_CONFIG_DIR",
    config_home_default=Path.home() / ".claude",
    user_config_prefix=".claude-user-",
    skills_dirname="skills",
    prompts_dirname="commands",
    skills_are_autoloaded=True,
    settings_format="json",
    settings_filename="settings.json",
    mcp_filename=".mcp.json",
    supports_effort=False,
    full_access_flags="--dangerously-skip-permissions",
    model_flag="--model",
    default_model=os.environ.get("TMUX_DASH_CLAUDE_MODEL", "claude-opus-5[1m]"),
    resume_last="{cli} --continue",
    resume_by_id="{cli} --resume {id}",
    process_names=("claude", "node"),
    process_argv_re=r"(?:^|[/\s])claude(?:\s|$)",
    busy_markers=("esc to interrupt",),
    tool_output_markers=("⎿",),
    tool_header_re=(r"^(?:(?:Bash|BashOutput|Fetch|WebFetch|Read|Edit|MultiEdit|Write|NotebookEdit|"
                    r"Update|Grep|Glob|Task|Search|WebSearch|TodoWrite|Kill|Add)\s*\(|mcp__[^(\s]+\s*\()"),
    idle_hint_re=r"Tip:.*claude",
    transcript_globs=("projects/*/*.jsonl", "projects/*/subagents/*.jsonl",
                      "projects/*/*/subagents/*.jsonl"),
    usage_kind="anthropic_oauth",
    auth_kind="anthropic",
    api_key_env="ANTHROPIC_API_KEY",
    api_key_prefix="sk-ant-",
    credentials_filename=".credentials.json",
    ignore_filename=".claudeignore",
    ignore_is_enforced=True,
    docs_url="https://docs.claude.com/en/docs/claude-code",
    aliases=("claude-code", "cc", "anthropic"),
)

CODEX = ProviderSpec(
    key="codex",
    label="Codex",
    cli="codex",
    context_filename="AGENTS.md",
    config_home_env="CODEX_HOME",
    config_home_default=Path.home() / ".codex",
    user_config_prefix=".codex-user-",
    skills_dirname="skills",
    prompts_dirname="prompts",
    skills_are_autoloaded=False,
    settings_format="toml",
    settings_filename="config.toml",
    mcp_filename="config.toml",
    supports_effort=True,
    full_access_flags="--dangerously-bypass-approvals-and-sandbox",
    model_flag="--model",
    default_model=os.environ.get("TMUX_DASH_CODEX_MODEL", "gpt-5.6-sol"),
    resume_last="{cli} resume --last",
    resume_by_id="{cli} resume {id}",
    process_names=("codex",),
    process_argv_re=r"(?:^|[/\s])codex(?:\s|$)",
    busy_markers=("esc to interrupt", "worked for ", "tokens used"),
    tool_output_markers=("└", "│"),
    tool_header_re=r"^(?:Ran|Edited|Explored|Called|Read|Searched|Applied)\s",
    idle_hint_re=r"Tip:.*codex",
    transcript_globs=("sessions/**/rollout-*.jsonl",),
    usage_kind="codex_app_server",
    auth_kind="chatgpt",
    api_key_env="OPENAI_API_KEY",
    api_key_prefix="sk-",
    credentials_filename="auth.json",
    ignore_filename=".codexignore",
    ignore_is_enforced=False,
    docs_url="https://developers.openai.com/codex/cli",
    aliases=("openai", "gpt"),
)

PROVIDERS: dict[str, ProviderSpec] = {p.key: p for p in (CLAUDE, CODEX)}
PROVIDER_KEYS = tuple(PROVIDERS)


def get_provider(key: str | None) -> ProviderSpec:
    """Resolve a provider by key or alias; falls back to the default backend."""
    if not key:
        return DEFAULT_PROVIDER
    k = str(key).strip().lower()
    if k in PROVIDERS:
        return PROVIDERS[k]
    for spec in PROVIDERS.values():
        if k in spec.aliases or k == spec.cli or k == spec.label.lower():
            return spec
    return DEFAULT_PROVIDER


def provider_for_command(cmd: str | None) -> ProviderSpec | None:
    """Sniff a launch command ("codex --yolo", "claude --model x") for a backend.

    Returns None when the command names neither CLI, so callers can fall back
    rather than silently guessing wrong.
    """
    if not cmd:
        return None
    low = f" {cmd.strip().lower()} "
    for spec in PROVIDERS.values():
        if re.search(rf"(?:^|[/\s]){re.escape(spec.cli)}(?:\s|$)", low):
            return spec
    return None


def _default_provider() -> ProviderSpec:
    explicit = os.environ.get("TMUX_DASH_AGENT", "").strip().lower()
    if explicit:
        return get_provider(explicit) if explicit in PROVIDERS or any(
            explicit in s.aliases for s in PROVIDERS.values()) else CODEX
    sniffed = provider_for_command(os.environ.get("TMUX_DASH_NEW_SESSION_CMD", ""))
    return sniffed or CODEX


DEFAULT_PROVIDER: ProviderSpec = _default_provider()


def enabled_providers() -> list[ProviderSpec]:
    """Backends this host can actually launch (the CLI is on PATH).

    A dashboard on a box with only one of the two still works; the session
    creation UI just offers one backend.
    """
    import shutil as _shutil
    out = [p for p in PROVIDERS.values() if _shutil.which(p.cli)]
    return out or [DEFAULT_PROVIDER]


def public_dict(spec: ProviderSpec) -> dict:
    """The subset the frontend needs to label and configure a session."""
    return {
        "key": spec.key,
        "label": spec.label,
        "cli": spec.cli,
        "context_filename": spec.context_filename,
        "supports_effort": spec.supports_effort,
        "default_model": spec.default_model,
        "settings_format": spec.settings_format,
        "docs_url": spec.docs_url,
    }
