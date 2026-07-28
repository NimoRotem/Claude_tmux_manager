"""The shared memory store (abstraction 6).

Neither backend's native memory feature is used, on purpose:

* Claude Code keeps one memory directory per project
  (`projects/<encoded cwd>/memory/MEMORY.md` plus sibling topic files).
* Codex keeps a single global `MEMORY.md` with project-scoped entries and a
  background consolidation pipeline.

Those are different shapes, not different spellings, so anything written into
one is invisible to the other and a session that switches backend loses its
history. Instead both read a pinned digest from their rendered context file and
do all reads and writes through the `memory` MCP server over this store.

Layout, under AGENTCTX_STATE/memory/:

    <scope>/<slug>.md      one fact per file, with frontmatter
    <scope>/INDEX.md       generated: one line per fact (this is the digest)

`scope` is the project the memory belongs to (an encoded cwd), or `global`.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, asdict
from pathlib import Path

STATE = Path(os.environ.get("AGENTCTX_STATE")
             or (Path(__file__).resolve().parent / "state"))
MEM_ROOT = STATE / "memory"
MAX_DIGEST_CHARS = 4000

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def encode_scope(cwd: str | None) -> str:
    """Project scope key. Mirrors the encoding both CLIs use for project dirs."""
    if not cwd:
        return "global"
    return (str(cwd).replace("/", "-").replace("_", "-").strip("-") or "global")


def slugify(key: str) -> str:
    slug = _SLUG_RE.sub("-", (key or "").strip().lower()).strip("-")
    return slug[:80] or "note"


@dataclass
class Memory:
    key: str
    scope: str
    body: str
    tags: list
    updated_at: float
    source: str = ""

    def to_markdown(self) -> str:
        fm = {
            "key": self.key,
            "scope": self.scope,
            "tags": self.tags,
            "updated_at": self.updated_at,
            "source": self.source,
        }
        return ("---\n" + json.dumps(fm, indent=2) + "\n---\n\n"
                + self.body.strip() + "\n")

    @classmethod
    def from_markdown(cls, path: Path) -> "Memory | None":
        try:
            text = path.read_text()
        except Exception:
            return None
        m = re.match(r"^---\n(.*?)\n---\n(.*)$", text, re.S)
        if not m:
            return cls(key=path.stem, scope=path.parent.name, body=text.strip(),
                       tags=[], updated_at=path.stat().st_mtime)
        try:
            fm = json.loads(m.group(1))
        except Exception:
            fm = {}
        return cls(
            key=fm.get("key", path.stem),
            scope=fm.get("scope", path.parent.name),
            body=m.group(2).strip(),
            tags=list(fm.get("tags") or []),
            updated_at=float(fm.get("updated_at") or path.stat().st_mtime),
            source=fm.get("source", ""),
        )


def _scope_dir(scope: str) -> Path:
    d = MEM_ROOT / (scope or "global")
    d.mkdir(parents=True, exist_ok=True)
    return d


def write(key: str, body: str, *, scope: str = "global", tags: list | None = None,
          source: str = "") -> Memory:
    """Create or replace one memory. One fact per file, always."""
    mem = Memory(key=key, scope=scope or "global", body=body,
                 tags=list(tags or []), updated_at=time.time(), source=source)
    path = _scope_dir(mem.scope) / f"{slugify(key)}.md"
    path.write_text(mem.to_markdown())
    reindex(mem.scope)
    return mem


def read(key: str, *, scope: str = "global") -> Memory | None:
    path = _scope_dir(scope) / f"{slugify(key)}.md"
    return Memory.from_markdown(path) if path.exists() else None


def delete(key: str, *, scope: str = "global") -> bool:
    path = _scope_dir(scope) / f"{slugify(key)}.md"
    if path.exists():
        path.unlink()
        reindex(scope)
        return True
    return False


def listing(scope: str = "global") -> list[Memory]:
    out = []
    for path in sorted(_scope_dir(scope).glob("*.md")):
        if path.name == "INDEX.md":
            continue
        mem = Memory.from_markdown(path)
        if mem:
            out.append(mem)
    return sorted(out, key=lambda m: m.updated_at, reverse=True)


def search(query: str, *, scope: str | None = None, limit: int = 20) -> list[dict]:
    """Substring search over keys, tags and bodies. Ranked, not fuzzy.

    Deliberately dumb: an agent that can read the index and then open the two or
    three files that matter beats a ranking function nobody can predict.
    """
    q = (query or "").strip().lower()
    scopes = [scope] if scope else [d.name for d in MEM_ROOT.glob("*") if d.is_dir()]
    hits = []
    for sc in scopes:
        for mem in listing(sc):
            hay_key = mem.key.lower()
            hay_tags = " ".join(mem.tags).lower()
            hay_body = mem.body.lower()
            score = 0
            if q in hay_key:
                score += 10
            if q in hay_tags:
                score += 5
            if q in hay_body:
                score += 1
            if score:
                hits.append((score, mem))
    hits.sort(key=lambda pair: (-pair[0], -pair[1].updated_at))
    return [{"score": s, **asdict(m)} for s, m in hits[:limit]]


def reindex(scope: str) -> Path:
    """Rewrite INDEX.md — the file the digest is built from."""
    d = _scope_dir(scope)
    lines = [f"# Memory index — {scope}", ""]
    for mem in listing(scope):
        first = (mem.body.strip().split("\n", 1)[0] or "").strip()
        if len(first) > 160:
            first = first[:157] + "…"
        tags = f" _[{', '.join(mem.tags)}]_" if mem.tags else ""
        lines.append(f"- **{mem.key}**{tags} — {first}")
    path = d / "INDEX.md"
    path.write_text("\n".join(lines) + "\n")
    return path


def digest(scope: str = "global", *, extra_scope: str | None = None,
           max_chars: int = MAX_DIGEST_CHARS) -> str:
    """The pinned block that goes into the rendered context file.

    Only ever an index plus a pointer — never the memories themselves. The whole
    point is that the context file stays small and the agent pulls what it needs
    through the tool.
    """
    scopes = [s for s in (scope, extra_scope) if s]
    body = []
    for sc in scopes:
        items = listing(sc)
        if not items:
            continue
        body.append(f"### {sc}")
        for mem in items:
            first = (mem.body.strip().split("\n", 1)[0] or "").strip()
            if len(first) > 140:
                first = first[:137] + "…"
            body.append(f"- `{mem.key}` — {first}")
        body.append("")
    if not body:
        listing_text = "_Nothing remembered for this project yet._"
    else:
        listing_text = "\n".join(body).strip()
        if len(listing_text) > max_chars:
            listing_text = listing_text[:max_chars].rsplit("\n", 1)[0] + "\n- …(truncated)"

    return (
        "## Memory\n\n"
        "This is an index, not the content. Read a memory with "
        "`memory_read(key)` and search with `memory_search(query)` before "
        "assuming something is not recorded. Write durable facts with "
        "`memory_write(key, body)` — never by editing files under "
        "`state/memory/` directly, and never into your own backend's native "
        "memory, because the other backend cannot see that.\n\n"
        f"{listing_text}\n"
    )
