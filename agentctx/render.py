"""core/ → .claude/* and ~/.codex/*.

One source tree, two rendered trees. The shared instruction body is never
forked: a backend difference lives in `core/instructions/_backend/<key>.md` and
is appended, or it lives in an adapter as a translation rule. If you find
yourself about to copy a paragraph into both backend files, that is the bug.

Everything written here is marked as managed and rewritten in place. Files the
dashboard does not own (a user's own AGENTS.md content, the rest of a
config.toml) are preserved: managed content sits between markers, and a
non-managed file is merged rather than replaced.
"""

from __future__ import annotations

import json
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from .adapters import ADAPTERS, get_adapter
from .adapters.base import interpolate

ROOT = Path(__file__).resolve().parent          # the agentctx package
HOME = ROOT.parent                              # the app root (importable parent)
CORE = ROOT / "core"
STATE = Path(os.environ.get("AGENTCTX_STATE") or (ROOT / "state"))
EMIT_SCRIPT = ROOT / "runtime" / "events" / "emit.sh"

BEGIN = "<!-- agentctx:managed — do not edit between these markers -->"
END = "<!-- /agentctx:managed -->"


# --------------------------------------------------------------------------- io

def _read_yaml(path: Path) -> dict:
    """Small YAML subset loader.

    PyYAML is not guaranteed on every host this deploys to and these files are
    ours, so we parse the subset we actually use: nested mappings, block lists of
    scalars, inline `[a, b]` lists, and quoted or bare scalars. Anything fancier
    belongs in Python, not in config.
    """
    try:
        import yaml  # noqa: PLC0415
        return yaml.safe_load(path.read_text()) or {}
    except ImportError:
        pass

    root: dict = {}
    stack = [(-1, root)]
    pending_list = None
    for raw in path.read_text().split("\n"):
        line = raw.split("#", 1)[0].rstrip() if not _in_quotes(raw, "#") else raw.rstrip()
        if not line.strip():
            continue
        indent = len(line) - len(line.lstrip())
        text = line.strip()

        if text.startswith("- "):
            if pending_list is None:
                continue
            pending_list.append(_scalar(text[2:].strip()))
            continue

        pending_list = None
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]

        if ":" not in text:
            continue
        key, _, value = text.partition(":")
        key, value = key.strip(), value.strip()
        if not value:
            child: dict = {}
            parent[key] = child
            stack.append((indent, child))
            # A key with no value may open a mapping OR a list; decide lazily.
            parent[key] = child
            pending_list = _ListProxy(parent, key, child)
        else:
            parent[key] = _scalar(value)
    return _collapse(root)


class _ListProxy(list):
    """Turns `key:` followed by `- item` lines into a real list, lazily."""

    def __init__(self, parent, key, placeholder):
        super().__init__()
        self._parent, self._key, self._placeholder = parent, key, placeholder

    def append(self, item):
        if self._parent.get(self._key) is self._placeholder:
            self._parent[self._key] = self
        super().append(item)


def _collapse(node):
    if isinstance(node, _ListProxy):
        return list(node)
    if isinstance(node, dict):
        return {k: _collapse(v) for k, v in node.items()}
    return node


def _in_quotes(line: str, ch: str) -> bool:
    idx = line.find(ch)
    if idx < 0:
        return False
    return line[:idx].count('"') % 2 == 1


def _scalar(value: str):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [_scalar(v) for v in _split_inline(inner)] if inner else []
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    low = value.lower()
    if low in ("true", "false"):
        return low == "true"
    if low in ("null", "~", ""):
        return None
    if re.fullmatch(r"-?\d+", value):
        return int(value)
    if re.fullmatch(r"-?\d+\.\d+", value):
        return float(value)
    return value


def _split_inline(inner: str) -> list:
    out, depth, cur, quote = [], 0, "", ""
    for ch in inner:
        if quote:
            cur += ch
            if ch == quote:
                quote = ""
            continue
        if ch in "\"'":
            quote = ch
            cur += ch
        elif ch in "[{":
            depth += 1
            cur += ch
        elif ch in "]}":
            depth -= 1
            cur += ch
        elif ch == "," and depth == 0:
            out.append(cur.strip())
            cur = ""
        else:
            cur += ch
    if cur.strip():
        out.append(cur.strip())
    return out


# ------------------------------------------------------------------- core reads

def load_skills() -> list[dict]:
    """Every skill in core/skills, with its frontmatter description."""
    out = []
    root = CORE / "skills"
    for skill_md in sorted(root.glob("*/SKILL.md")):
        text = skill_md.read_text()
        name = skill_md.parent.name
        desc = ""
        fm = re.match(r"^---\n(.*?)\n---\n", text, re.S)
        if fm:
            for line in fm.group(1).split("\n"):
                if line.startswith("name:"):
                    name = line.split(":", 1)[1].strip()
                elif line.startswith("description:"):
                    desc = line.split(":", 1)[1].strip()
        out.append({"name": name, "description": desc, "dir": skill_md.parent,
                    "path": str(skill_md)})
    return out


def load_prompts() -> list[dict]:
    out = []
    for md in sorted((CORE / "prompts").glob("*.md")):
        out.append({"name": md.stem, "body": md.read_text(), "src": md})
    return out


def load_ignore() -> list[str]:
    path = CORE / "ignore.txt"
    if not path.exists():
        return []
    return [ln.strip() for ln in path.read_text().split("\n")
            if ln.strip() and not ln.strip().startswith("#")]


def shared_instructions() -> str:
    """The whole shared body, in filename order, with no backend content."""
    parts = []
    for md in sorted((CORE / "instructions").glob("*.md")):
        parts.append(md.read_text().rstrip())
    return "\n\n".join(parts)


def backend_instructions(key: str) -> str:
    path = CORE / "instructions" / "_backend" / f"{key}.md"
    return path.read_text().rstrip() if path.exists() else ""


def mcp_servers(render_env: dict) -> dict:
    data = _read_yaml(CORE / "mcp.yaml")
    servers = data.get("servers") or {}
    out = {}
    for name, spec in servers.items():
        gate = spec.get("enabled_when_env")
        if gate and not render_env.get(gate):
            # A server whose credential is absent is left out entirely rather
            # than registered and broken — see clean_env() for the same rule
            # applied one level down.
            continue
        resolved = dict(spec)
        resolved["command"] = interpolate(spec.get("command", ""), render_env)
        resolved["args"] = [interpolate(a, render_env) for a in (spec.get("args") or [])]
        if spec.get("cwd"):
            resolved["cwd"] = interpolate(spec["cwd"], render_env)
        out[name] = resolved
    return out


def policy_level(level: str | None = None) -> tuple[str, dict, list[str]]:
    data = _read_yaml(CORE / "policy.yaml")
    name = level or data.get("default_level") or "workspace-write"
    levels = data.get("levels") or {}
    if name not in levels:
        raise KeyError(f"unknown policy level {name!r}; known: {', '.join(levels)}")
    return name, levels[name], list(data.get("always_confirm") or [])


def runtime_tier(tier: str | None = None) -> tuple[str, dict]:
    data = _read_yaml(CORE / "runtime.yaml")
    name = tier or data.get("default_tier") or "default"
    tiers = data.get("tiers") or {}
    if name not in tiers:
        raise KeyError(f"unknown runtime tier {name!r}; known: {', '.join(tiers)}")
    return name, tiers[name]


# ----------------------------------------------------------------- the renderer

@dataclass
class RenderResult:
    backend: str
    home: Path
    written: list[str]
    skipped: list[str]


def _managed_block(body: str) -> str:
    return f"{BEGIN}\n{body.strip()}\n{END}\n"


def _merge_managed(path: Path, body: str) -> str:
    """Replace our block in an existing file, keeping everything else."""
    existing = path.read_text() if path.exists() else ""
    block = _managed_block(body)
    if BEGIN in existing and END in existing:
        head = existing.split(BEGIN, 1)[0]
        tail = existing.split(END, 1)[1]
        return head + block + tail.lstrip("\n")
    return block + ("\n" + existing.lstrip("\n") if existing.strip() else "")


def _merge_toml(path: Path, managed: str) -> str:
    """Same idea for TOML, using comment markers."""
    begin, end = "# >>> agentctx managed >>>", "# <<< agentctx managed <<<"
    existing = path.read_text() if path.exists() else ""
    block = f"{begin}\n{managed.strip()}\n{end}\n"
    if begin in existing and end in existing:
        head = existing.split(begin, 1)[0]
        tail = existing.split(end, 1)[1]
        return head + block + tail.lstrip("\n")
    return block + ("\n" + existing.lstrip("\n") if existing.strip() else "")


def render(backend: str, home: Path, *, level: str | None = None,
           tier: str | None = None, memory_digest: str = "",
           render_env: dict | None = None, link_skills: bool = True) -> RenderResult:
    """Write one backend's whole context tree. Idempotent."""
    adapter = get_adapter(backend)
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ)
    env.setdefault("AGENTCTX_ROOT", str(ROOT))
    env.setdefault("AGENTCTX_HOME", str(HOME))
    env.setdefault("AGENTCTX_STATE", str(STATE))
    if render_env:
        env.update(render_env)

    written, skipped = [], []
    skills = load_skills()
    ignore = load_ignore()
    level_name, policy, always_confirm = policy_level(level)
    tier_name, tier_cfg = runtime_tier(tier)

    # --- 1. instructions ---------------------------------------------------
    sections = [shared_instructions()]
    preamble = adapter.instructions_preamble(skills)
    if preamble:
        sections.append(preamble)
    if always_confirm:
        sections.append("## Always confirm before\n\n"
                        + "\n".join(f"- {item}" for item in always_confirm))
    if ignore:
        sections.append(adapter.ignore_instruction(ignore))
    if memory_digest:
        sections.append(memory_digest)
    backend_body = backend_instructions(adapter.key)
    if backend_body:
        sections.append(backend_body)

    ctx_path = adapter.context_path(home)
    ctx_path.write_text(_merge_managed(ctx_path, "\n\n".join(s for s in sections if s.strip())))
    written.append(str(ctx_path))

    # --- 2. skills ---------------------------------------------------------
    skills_root = adapter.skills_root(home)
    skills_root.mkdir(parents=True, exist_ok=True)
    for skill in skills:
        dest = skills_root / skill["dir"].name
        if dest.is_symlink() or dest.exists():
            if dest.is_symlink() and dest.resolve() == skill["dir"].resolve():
                skipped.append(str(dest))
                continue
            if dest.is_symlink() or dest.is_file():
                dest.unlink()
            else:
                shutil.rmtree(dest)
        if link_skills:
            # Symlink so editing core/skills/<name>/SKILL.md is live for every
            # backend and every member at once. Bodies are stored exactly once.
            dest.symlink_to(skill["dir"])
        else:
            shutil.copytree(skill["dir"], dest)
        written.append(str(dest))

    # --- 3. prompts --------------------------------------------------------
    prompts_root = adapter.prompts_root(home)
    prompts_root.mkdir(parents=True, exist_ok=True)
    for prompt in load_prompts():
        dest = prompts_root / f"{prompt['name']}.md"
        dest.write_text(adapter.rewrite_prompt_args(prompt["body"]))
        written.append(str(dest))

    # --- 4/5/9. settings, policy, runtime ----------------------------------
    servers = mcp_servers(env)
    for rendered in adapter.render_settings(home, policy=policy, tier=tier_cfg,
                                            mcp=servers, ignore=ignore, render_env=env):
        if rendered.path.suffix == ".toml":
            rendered.path.write_text(_merge_toml(rendered.path, rendered.content))
        elif rendered.path.name == "settings.json":
            rendered.path.write_text(_merge_json_settings(rendered.path, rendered.content))
        else:
            rendered.path.write_text(rendered.content)
        written.append(str(rendered.path))

    # --- 7. events ---------------------------------------------------------
    for rendered in adapter.render_event_hooks(home, EMIT_SCRIPT):
        if rendered.path.name == "settings.hooks.json":
            target = home / "settings.json"
            target.write_text(_merge_json_settings(target, rendered.content))
            written.append(str(target))
        elif rendered.path.name == "notify.toml":
            target = home / "config.toml"
            body = "\n".join(l for l in rendered.content.split("\n")
                             if l.strip() and not l.startswith("#"))
            target.write_text(_merge_toml_notify(target, body))
            written.append(str(target))

    (home / ".agentctx.json").write_text(json.dumps({
        "backend": adapter.key, "policy_level": level_name, "runtime_tier": tier_name,
        "skills": [s["name"] for s in skills], "mcp_servers": sorted(servers),
        "source": str(CORE),
    }, indent=2) + "\n")

    return RenderResult(adapter.key, home, written, skipped)


def _merge_json_settings(path: Path, new_content: str) -> str:
    """Deep-merge managed JSON settings into whatever is already there."""
    try:
        existing = json.loads(path.read_text()) if path.exists() else {}
    except Exception:
        existing = {}
    incoming = json.loads(new_content)

    def merge(a: dict, b: dict) -> dict:
        out = dict(a)
        for k, v in b.items():
            out[k] = merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
        return out

    return json.dumps(merge(existing, incoming), indent=2) + "\n"


def _merge_toml_notify(path: Path, line: str) -> str:
    """`notify` is a top-level key; replace it in place if present."""
    existing = path.read_text() if path.exists() else ""
    if re.search(r"^notify\s*=", existing, re.M):
        return re.sub(r"^notify\s*=.*$", line.strip(), existing, count=1, flags=re.M)
    return (existing.rstrip("\n") + "\n" + line.strip() + "\n") if existing.strip() else line


def render_all(homes: dict[str, Path], **kwargs) -> list[RenderResult]:
    """Render every backend named in `homes` ({"claude": path, "codex": path})."""
    return [render(key, home, **kwargs) for key, home in homes.items() if key in ADAPTERS]
