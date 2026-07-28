"""Sessions and workspaces (abstraction 11) — fully backend-agnostic.

    tmux session   agent:<project>:<backend>
    working dir    a dedicated git worktree per session

Worktree isolation is mandatory, not a nicety. Two agents in one working tree
will interleave edits, stage each other's files and produce commits neither of
them intended; the two CLIs are no different from two people in that respect.
The one-line rule: **the two tools must never share a working directory.**

Nothing here knows what a backend is beyond putting its name in the session name
and passing it through — which is why this module has no adapter import.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

SESSION_RE = re.compile(r"^agent:(?P<project>[A-Za-z0-9_.-]+):(?P<backend>[a-z]+)$")


def session_name(project: str, backend: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", project).strip("-") or "work"
    return f"agent:{safe}:{backend}"


def parse_session(name: str) -> dict | None:
    m = SESSION_RE.match(name or "")
    return m.groupdict() if m else None


@dataclass
class Workspace:
    path: Path
    branch: str
    repo: Path | None
    created: bool


def _run(cmd: list, cwd: str | Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None,
                          capture_output=True, text=True, timeout=120)


def _is_repo(path: Path) -> bool:
    return _run(["git", "rev-parse", "--git-dir"], cwd=path).returncode == 0


def ensure_worktree(repo: Path, project: str, backend: str, *,
                    base: str = "", user: str = "agent") -> Workspace:
    """Give this session its own worktree and branch, or reuse the existing one.

    Branch name is `<user>/<project>-<backend>`, so two backends working the same
    project never land on the same branch — which is the other half of the
    isolation rule: separate directories are not enough if both commit to one
    branch.
    """
    repo = Path(repo).resolve()
    if not _is_repo(repo):
        # Not a repo: there is nothing to isolate, so the directory is the
        # workspace. Say so rather than pretending a worktree was made.
        return Workspace(path=repo, branch="", repo=None, created=False)

    branch = f"{user}/{project}-{backend}"
    target = repo.parent / f"{repo.name}-{backend}-{project}"

    if target.exists():
        return Workspace(path=target, branch=branch, repo=repo, created=False)

    base_ref = base or _default_branch(repo)
    result = _run(["git", "worktree", "add", "-b", branch, str(target), base_ref], cwd=repo)
    if result.returncode != 0:
        # Most often the branch already exists from an earlier session; attach to
        # it instead of failing the session launch.
        result = _run(["git", "worktree", "add", str(target), branch], cwd=repo)
    if result.returncode != 0:
        raise RuntimeError(f"could not create worktree: {result.stderr.strip()}")
    return Workspace(path=target, branch=branch, repo=repo, created=True)


def _default_branch(repo: Path) -> str:
    out = _run(["git", "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD"], cwd=repo)
    if out.returncode == 0 and out.stdout.strip():
        return out.stdout.strip().split("/", 1)[-1]
    for candidate in ("main", "master"):
        if _run(["git", "rev-parse", "--verify", candidate], cwd=repo).returncode == 0:
            return candidate
    return "HEAD"


def remove_worktree(workspace: Workspace, *, force: bool = False) -> bool:
    """Drop a worktree once its session is gone. Refuses to discard real work."""
    if not workspace.repo or not workspace.path.exists():
        return False
    dirty = _run(["git", "status", "--porcelain"], cwd=workspace.path).stdout.strip()
    if dirty and not force:
        return False
    result = _run(["git", "worktree", "remove", str(workspace.path)]
                  + (["--force"] if force else []), cwd=workspace.repo)
    return result.returncode == 0


def list_agent_sessions() -> list[dict]:
    """Every managed session, with its project and backend parsed out."""
    out = _run(["tmux", "list-sessions", "-F", "#{session_name}\t#{session_path}"])
    if out.returncode != 0:
        return []
    rows = []
    for line in out.stdout.strip().split("\n"):
        if not line.strip():
            continue
        name, _, path = line.partition("\t")
        parsed = parse_session(name)
        if parsed:
            rows.append({"session": name, "path": path, **parsed})
    return rows
