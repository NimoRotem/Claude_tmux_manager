"""Bridge between the shared store and each backend's native memory.

The shared store is authoritative. This module exists for the two directions
where a backend would otherwise strand knowledge:

*  **import** — a project that already has Claude-style
   `projects/<cwd>/memory/*.md` files, or a Codex `MEMORY.md`, gets pulled into
   the shared store once so nothing is lost on the way in.
*  **export** — the digest is pushed back out to whatever the backend actually
   reads, so even a session that ignores the MCP tool still sees the index.

The export asymmetry is the important part and is easy to get wrong:

    Claude  reads  <config home>/projects/<encoded cwd>/memory/MEMORY.md
    Codex   reads  <config home>/MEMORY.md           (one global file)

Writing a per-project memory file into a Codex home is a silent no-op — Codex
never opens that path. The dashboard's own editor writes there for both
backends, which is exactly why this bridge has to run for Codex sessions.
"""

from __future__ import annotations

import re
from pathlib import Path

from . import memory

BEGIN = "<!-- agentctx:memory -->"
END = "<!-- /agentctx:memory -->"


def _splice(existing: str, block: str) -> str:
    if BEGIN in existing and END in existing:
        head = existing.split(BEGIN, 1)[0]
        tail = existing.split(END, 1)[1]
        return head + BEGIN + "\n" + block.strip() + "\n" + END + tail
    return (BEGIN + "\n" + block.strip() + "\n" + END + "\n\n"
            + existing.lstrip("\n") if existing.strip()
            else BEGIN + "\n" + block.strip() + "\n" + END + "\n")


# ------------------------------------------------------------------- export

def export_to_backend(backend: str, home: Path, *, scope: str = "global") -> Path:
    """Put the shared index where this backend will actually read it."""
    home = Path(home)
    block = memory.digest(scope, extra_scope="global" if scope != "global" else None)

    if backend == "claude":
        # Per-project memory dir — the path Claude Code loads for this cwd.
        target = home / "projects" / scope / "memory" / "MEMORY.md"
    else:
        # Codex reads ONE global MEMORY.md; project scoping lives inside it.
        target = home / "MEMORY.md"

    target.parent.mkdir(parents=True, exist_ok=True)
    existing = target.read_text() if target.exists() else ""
    target.write_text(_splice(existing, block))
    return target


# ------------------------------------------------------------------- import

_CLAUDE_FM = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)


def import_claude_memories(home: Path, *, scope: str | None = None,
                           dry_run: bool = False) -> list[str]:
    """Pull `projects/*/memory/*.md` into the shared store."""
    home = Path(home)
    imported = []
    for mem_dir in sorted(home.glob("projects/*/memory")):
        project_scope = scope or mem_dir.parent.name
        for md in sorted(mem_dir.glob("*.md")):
            if md.name.upper() in ("MEMORY.MD", "INDEX.MD"):
                continue          # index files, not facts
            text = md.read_text()
            key, body = md.stem, text
            m = _CLAUDE_FM.match(text)
            if m:
                body = m.group(2).strip()
                for line in m.group(1).split("\n"):
                    if line.strip().startswith("name:"):
                        key = line.split(":", 1)[1].strip()
            if not dry_run:
                memory.write(key, body, scope=project_scope,
                             tags=["imported", "claude"], source=str(md))
            imported.append(f"{project_scope}/{key}")
    return imported


def import_codex_memory(home: Path, *, scope: str = "global",
                        dry_run: bool = False) -> list[str]:
    """Pull a Codex global MEMORY.md apart into individual facts.

    Codex keeps one file with `## heading` sections per topic; each section
    becomes one memory so the two stores have the same granularity.
    """
    path = Path(home) / "MEMORY.md"
    if not path.exists():
        return []
    text = path.read_text()
    if BEGIN in text:                     # our own exported block — never re-import
        text = text.split(BEGIN)[0] + text.split(END)[-1] if END in text else text.split(BEGIN)[0]

    imported = []
    for match in re.finditer(r"^##+\s+(.+?)\n(.*?)(?=\n##+\s|\Z)", text, re.S | re.M):
        key = match.group(1).strip()
        body = match.group(2).strip()
        if not body:
            continue
        if not dry_run:
            memory.write(key, body, scope=scope, tags=["imported", "codex"], source=str(path))
        imported.append(f"{scope}/{key}")
    return imported


def sync_session(backend: str, home: Path, cwd: str | None) -> dict:
    """One call per session start: import anything native, then export the index.

    Import first so a project's existing notes are in the store before the digest
    is built; otherwise the first render of a migrated project shows an empty
    memory section and the agent concludes there is no history.
    """
    scope = memory.encode_scope(cwd)
    imported = []
    if backend == "claude":
        imported = import_claude_memories(home, scope=scope)
    else:
        imported = import_codex_memory(home, scope=scope)
    target = export_to_backend(backend, home, scope=scope)
    return {"backend": backend, "scope": scope, "imported": imported,
            "digest_written_to": str(target)}
