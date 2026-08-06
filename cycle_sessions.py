#!/usr/bin/env python3
"""Cycle every session off the pre-hardening shared login.

Sessions started before the per-account UNIX users existed still run as
nimrod_rotem, which means their agent still reaches the GCE metadata server and
with it the project-owner service account. Restarting them is the only way to
move them: a running process cannot change uid.

This uses the dashboard's own park/resume lifecycle rather than killing tmux
outright, so each session keeps its scrollback, its cwd and its Codex rollout,
and resumes exactly where it was. Sessions that are busy, being watched, or
running autonomous work are skipped and reported, never interrupted.

  cycle_sessions.py plan     list what would happen
  cycle_sessions.py run      park, drop the stale tmux session, mark virtual
  cycle_sessions.py relaunch <name>...   bring named sessions back up now
"""
import asyncio
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/home/nimrod_rotem/tmux-dashboard")
import app  # noqa: E402

LIFECYCLE = Path("/home/nimrod_rotem/.tmux-dashboard/session-lifecycle.json")


def tmux_sessions() -> list[str]:
    r = subprocess.run(
        ["tmux", "ls", "-F", "#{session_name}"], capture_output=True, text=True
    )
    return [s for s in r.stdout.split("\n") if s.strip()]


def codex_owner(session: str) -> str:
    """The UNIX user this session's codex process runs as, "" if none.

    Walk the pane's descendants rather than its direct child: the launch goes
    pane bash -> systemd-run -> sudo (root) -> codex (the account), so the first
    child reports root and tells you nothing.
    """
    r = subprocess.run(
        ["tmux", "list-panes", "-t", session, "-F", "#{pane_pid}"],
        capture_output=True, text=True,
    )
    pane = r.stdout.strip().split("\n")[0] if r.stdout.strip() else ""
    if not pane:
        return ""
    seen, frontier, owners = set(), [pane], []
    while frontier:
        pid = frontier.pop()
        if pid in seen:
            continue
        seen.add(pid)
        kids = subprocess.run(
            ["pgrep", "-P", pid], capture_output=True, text=True
        ).stdout.split()
        frontier.extend(kids)
        info = subprocess.run(
            ["ps", "-o", "user:32=,args=", "-p", pid], capture_output=True, text=True
        ).stdout.strip()
        if info and "codex" in info and "--yolo" in info:
            owners.append(info.split()[0])
    for o in owners:
        if o.startswith("gx-"):
            return o
    return owners[0] if owners else ""


def cwd_of(session: str) -> str:
    try:
        return app.get_session_cwd(session) or ""
    except Exception:
        return ""


def plan() -> None:
    print(f"{'session':<28}{'runs as':<16}{'action'}")
    for s in sorted(tmux_sessions()):
        who = codex_owner(s) or "(no codex)"
        # Same rule run() uses: anything not already on a gx- account gets cycled.
        action = "already isolated, leave" if who.startswith("gx-") else "cycle"
        print(f"{s:<28}{who:<16}{action}")


def run() -> None:
    cycled, skipped, already = [], [], []
    for s in sorted(tmux_sessions()):
        who = codex_owner(s)
        if who and who.startswith("gx-"):
            already.append(s)
            continue
        cwd = cwd_of(s)
        result = asyncio.run(app._park_session_local(s, 0.0))
        if not result.get("ok"):
            skipped.append((s, result.get("skipped") or result.get("error") or "?"))
            continue
        subprocess.run(["tmux", "kill-session", "-t", s], capture_output=True)
        asyncio.run(
            asyncio.to_thread(
                app._session_lifecycle.mark_parked,
                s,
                reason="cycled onto per-account UNIX users",
                last_activity=0.0,
                cwd=cwd,
                virtual=True,
            )
        )
        cycled.append(s)
    print(f"cycled ({len(cycled)}): {', '.join(cycled) or '-'}")
    print(f"already isolated ({len(already)}): {', '.join(already) or '-'}")
    for s, why in skipped:
        print(f"SKIPPED {s}: {why}")


def relaunch(names: list[str]) -> None:
    for name in names:
        r = asyncio.run(app._resume_parked_session(name, source="admin-cycle"))
        print(f"{name}: resumed={r.get('resumed')} ok={r.get('ok')} {r.get('error','')}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "plan"
    if cmd == "plan":
        plan()
    elif cmd == "run":
        run()
    elif cmd == "relaunch":
        relaunch(sys.argv[2:])
    else:
        print(__doc__)
        sys.exit(2)
