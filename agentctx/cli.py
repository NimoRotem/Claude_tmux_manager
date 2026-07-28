"""agentctx — render, sync, inspect and run.

    python3 -m agentctx.cli render --backend codex --home ~/.codex
    python3 -m agentctx.cli render --all
    python3 -m agentctx.cli sync   --backend claude --home ~/.claude --cwd /srv/app
    python3 -m agentctx.cli status
    python3 -m agentctx.cli exec   --backend codex "summarise this repo"
    python3 -m agentctx.cli session --project app --backend claude --repo /srv/app
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from . import exec as agent_exec
from . import memory, memory_sync, render, session
from .adapters import ADAPTERS

ROOT = Path(__file__).resolve().parent
DEFAULT_HOMES = {
    "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")),
    "codex": Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")),
}


def cmd_render(args) -> int:
    targets = ({k: DEFAULT_HOMES[k] for k in ADAPTERS} if args.all
               else {args.backend: Path(args.home or DEFAULT_HOMES[args.backend])})
    scope = memory.encode_scope(args.cwd)
    digest = memory.digest(scope, extra_scope="global" if scope != "global" else None)
    for backend, home in targets.items():
        result = render.render(backend, home, level=args.level, tier=args.tier,
                               memory_digest=digest, link_skills=not args.copy_skills)
        print(f"{backend}: {len(result.written)} file(s) → {home}")
        if args.verbose:
            for path in result.written:
                print(f"   {path}")
    return 0


def cmd_sync(args) -> int:
    """Import native memory, then push the shared digest back out."""
    backends = list(ADAPTERS) if args.all else [args.backend]
    for backend in backends:
        home = Path(args.home) if args.home else DEFAULT_HOMES[backend]
        if not home.exists():
            print(f"{backend}: {home} does not exist — skipped")
            continue
        result = memory_sync.sync_session(backend, home, args.cwd)
        print(f"{backend}: imported {len(result['imported'])}, "
              f"digest → {result['digest_written_to']}")
        if args.verbose and result["imported"]:
            for key in result["imported"]:
                print(f"   {key}")
    return 0


def cmd_status(args) -> int:
    out = {"root": str(ROOT), "state": str(render.STATE), "backends": {}}
    for backend in ADAPTERS:
        home = DEFAULT_HOMES[backend]
        script = ROOT / "auth" / f"{backend}.sh"
        auth = {"valid": False, "detail": "auth script missing"}
        if script.exists():
            proc = subprocess.run(["bash", str(script), "status"],
                                  capture_output=True, text=True, timeout=60)
            try:
                auth = json.loads(proc.stdout.strip() or "{}")
            except json.JSONDecodeError:
                auth = {"valid": False, "detail": proc.stderr.strip()[:200]}
        rendered = home / ".agentctx.json"
        out["backends"][backend] = {
            "home": str(home),
            "cli_installed": bool(_which(_cli_name(backend))),
            "rendered": json.loads(rendered.read_text()) if rendered.exists() else None,
            "auth": auth,
        }
    out["sessions"] = session.list_agent_sessions()
    out["memory_scopes"] = sorted(d.name for d in memory.MEM_ROOT.glob("*") if d.is_dir())
    print(json.dumps(out, indent=2))
    return 0


def _cli_name(backend: str) -> str:
    """The executable, which is not always the adapter key."""
    return {"claude": "claude", "codex": "codex"}.get(backend, backend)


def _which(cli: str) -> str:
    import shutil
    return shutil.which(cli) or ""


def cmd_exec(args) -> int:
    def on_event(evt):
        if args.stream:
            print(json.dumps(evt), flush=True)
    result = agent_exec.run(args.backend, args.prompt, model=args.model,
                            effort=args.effort, cwd=args.cwd or "", on_event=on_event)
    if not args.stream:
        print(result.final_message)
    if not result.ok:
        print(f"[exit {result.returncode}] {result.stderr}", file=sys.stderr)
    return 0 if result.ok else 1


def cmd_session(args) -> int:
    ws = session.ensure_worktree(Path(args.repo), args.project, args.backend,
                                 user=args.user)
    name = session.session_name(args.project, args.backend)
    print(json.dumps({"session": name, "path": str(ws.path), "branch": ws.branch,
                      "created": ws.created}, indent=2))
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="agentctx", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("render", help="core/ → a backend's config tree")
    p.add_argument("--backend", choices=list(ADAPTERS), default="codex")
    p.add_argument("--home")
    p.add_argument("--all", action="store_true", help="render every backend")
    p.add_argument("--level", help="policy level (see core/policy.yaml)")
    p.add_argument("--tier", help="runtime tier (see core/runtime.yaml)")
    p.add_argument("--cwd", help="project dir, for the memory digest scope")
    p.add_argument("--copy-skills", action="store_true",
                   help="copy skills instead of symlinking them")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_render)

    p = sub.add_parser("sync", help="bridge native memory ⇄ the shared store")
    p.add_argument("--backend", choices=list(ADAPTERS), default="codex")
    p.add_argument("--home")
    p.add_argument("--all", action="store_true")
    p.add_argument("--cwd")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("status", help="what is installed, rendered and authenticated")
    p.set_defaults(func=cmd_status)

    p = sub.add_parser("exec", help="one headless turn, normalised")
    p.add_argument("prompt")
    p.add_argument("--backend", choices=list(ADAPTERS), default="codex")
    p.add_argument("--model", default="")
    p.add_argument("--effort", default="")
    p.add_argument("--cwd", default="")
    p.add_argument("--stream", action="store_true", help="emit normalised events as JSON")
    p.set_defaults(func=cmd_exec)

    p = sub.add_parser("session", help="worktree + session name for a project/backend")
    p.add_argument("--project", required=True)
    p.add_argument("--backend", choices=list(ADAPTERS), default="codex")
    p.add_argument("--repo", required=True)
    p.add_argument("--user", default=os.environ.get("AGENT_USER", "agent"))
    p.set_defaults(func=cmd_session)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
