"""Agent sessions that live on their OWN tmux socket, and the reaper that ends them.

House rule (infra, "An agent you spawn is yours to end"): an app that starts a claude or codex
session owns its lifetime. Its own socket, a reaper that ends a session idle over an hour, and the
SERVER killed when its last session goes. Idle is timed from #{window_activity}, because
#{session_activity} never moves.

WHY THIS EXISTS. The lisa-autofix spawner and the cron-autofix dispatcher on instance-3 opened
their fix sessions on the DEFAULT socket. The dashboard checkpoints every live session there into
its lifecycle registry, and restores any registered session that goes missing. So when the
spawner's reaper ended a stale session with a raw `tmux kill-session`, the dashboard brought it
back inside 20 seconds with a fresh Claude on the old conversation. 13 lisa-fix sessions from Aug 24
to Sep 23 and 2 cronfix self-tests were still holding about 1.7 GB on 2026-09-24.

Now both spawners open sessions here, on SOCKET, which the dashboard never adopts, and the
dashboard shows them read-only on its /autofix page. A leftover session on the default socket is
still reaped, through the dashboard's DELETE /api/sessions/{name} when the dashboard has it
registered, so the registry and tmux agree.

Used by ~/lisa-autofix/spawn_fix_session.py and ~/cron-autofix/cron_autofix.py (they import it
from this checkout), and by app.py for the watch page. Standard library only.

A launcher with no loop of its own to reap from (a manual script, a cron job that is switched off)
starts a WATCHER with the session: `spawn_watcher(name, socket)` or from a shell
`python3 agent_socket.py watch --socket S --name N [--idle SEC] [--max-age SEC] &`. It ends the
session once it is idle (or older than --max-age) and exits when the session is gone.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

TMUX = shutil.which("tmux") or "/usr/bin/tmux"
# The socket every autofix-style agent lives on. One name for all of them, so the dashboard has
# one place to look and `tmux -L autofix ls` shows everything a spawner has open.
SOCKET = os.environ.get("AGENT_TMUX_SOCKET", "autofix")
DEFAULT = "default"
IDLE_REAP_SEC = 3600

# Environment every agent launch line starts with. A builder session runs on the plan, never a
# metered key, so the keys are passed through EMPTY (same prefix the dashboard uses for its own
# launches). CLAUDE_CODE_EFFORT_LEVEL is unset so --effort on the command line is what counts.
AGENT_ENV_PREFIX = ("export ANTHROPIC_API_KEY= OPENAI_API_KEY= OPENAI_VOICE_KEY= "
                    "OPENAI_TASKS_KEY= CODEX_API_KEY=; unset CLAUDE_CODE_EFFORT_LEVEL; ")

DASH_URL = os.environ.get("TMUX_DASH_LOCAL_URL", "http://127.0.0.1:8501")
LIFECYCLE_FILE = Path(os.environ.get(
    "TMUX_DASH_LIFECYCLE_FILE",
    str(Path.home() / ".tmux-dashboard" / "session-lifecycle.json")))
SUPERVISOR_CONF = os.environ.get("TMUX_DASH_SUPERVISOR_CONF",
                                 "/etc/supervisor/conf.d/tmux-dashboard.conf")


class _Failed:
    returncode, stdout, stderr = 1, "", ""


def _is_default(socket) -> bool:
    return socket in (None, "", DEFAULT)


def tmux(socket, *args, timeout=30):
    argv = [TMUX] if _is_default(socket) else [TMUX, "-L", str(socket)]
    try:
        return subprocess.run(argv + [str(a) for a in args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return _Failed()


def list_sessions(socket=SOCKET) -> list[dict]:
    """Every session on `socket`: name, last WINDOW activity (max over windows), attached, created.

    An empty list when the server is not running."""
    r = tmux(socket, "list-windows", "-a", "-F",
             "#{session_name}\t#{window_activity}\t#{session_attached}\t#{session_created}")
    out: dict[str, dict] = {}
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 4 or not parts[0]:
            continue
        name = parts[0]
        try:
            act = float(parts[1])
        except ValueError:
            act = 0.0
        row = out.setdefault(name, {"name": name, "activity": 0.0, "attached": False,
                                    "created": 0.0, "socket": socket or DEFAULT})
        row["activity"] = max(row["activity"], act)
        row["attached"] = row["attached"] or (parts[2].isdigit() and int(parts[2]) > 0)
        try:
            row["created"] = float(parts[3])
        except ValueError:
            pass
    return list(out.values())


def activity(socket=SOCKET) -> dict[str, float]:
    """{session name: epoch of its most recent window activity}."""
    return {s["name"]: s["activity"] for s in list_sessions(socket)}


def has_session(name, socket=SOCKET) -> bool:
    return tmux(socket, "has-session", "-t", "=" + name).returncode == 0


def new_session(name, cwd, socket=SOCKET, command=None) -> bool:
    """Open a detached session. With `command`, the session runs it instead of a shell and ends
    when it exits (the test-mode dummy uses this)."""
    args = ["new-session", "-d", "-s", name, "-c", cwd]
    if command:
        args.append(command)
    return tmux(socket, *args).returncode == 0


def kill_server_if_empty(socket=SOCKET) -> bool:
    """End a private server that has no sessions left. Never touches the default socket."""
    if _is_default(socket):
        return False
    r = tmux(socket, "list-sessions", "-F", "#{session_name}")
    if r.returncode != 0:
        return False                      # no server at all
    if (r.stdout or "").strip():
        return False
    return tmux(socket, "kill-server").returncode == 0


# --------------------------------------------------------------------------- dashboard side

def dashboard_registered(name) -> bool:
    """True when the dashboard's lifecycle registry wants `name` running (so it would restore it)."""
    try:
        rows = json.loads(LIFECYCLE_FILE.read_text()).get("sessions", {})
    except (OSError, ValueError):
        return False
    row = rows.get(name)
    return isinstance(row, dict) and str(row.get("desired_state") or "") == "running"


def _supervisor_env() -> dict:
    """KEY=value pairs from the dashboard's supervisor `environment=` line.

    That file is the credential the running dashboard was started with, readable by this user on
    instance-3, so nothing here carries a password of its own."""
    try:
        text = Path(SUPERVISOR_CONF).read_text()
    except OSError:
        return {}
    env = {}
    for line in text.splitlines():
        if not line.startswith("environment="):
            continue
        body = line[len("environment="):]
        i = 0
        while i < len(body):
            eq = body.find("=", i)
            if eq < 0:
                break
            key = body[i:eq].strip().lstrip(",").strip()
            j = eq + 1
            if j < len(body) and body[j] == '"':
                end = body.find('"', j + 1)
                end = len(body) if end < 0 else end
                env[key] = body[j + 1:end]
                i = end + 1
            else:
                end = body.find(",", j)
                end = len(body) if end < 0 else end
                env[key] = body[j:end]
                i = end
            if i < len(body) and body[i] == ",":
                i += 1
    return env


def _running_dashboard_env() -> dict:
    """The environment of the dashboard process that is actually serving, read from /proc.

    Preferred over the supervisor file because the two can disagree: supervisord keeps the config
    it last loaded, so an edit to the file does nothing until `supervisorctl update`. Measured on
    instance-3 2026-09-24: the file's TMUX_DASH_PASS was not the one the live process ran with, and
    a login built from the file was refused."""
    app_py = str(Path(__file__).resolve().parent / "app.py")
    uid = os.getuid()
    for pid in os.listdir("/proc"):
        if not pid.isdigit():
            continue
        try:
            if os.stat(f"/proc/{pid}").st_uid != uid:
                continue
            argv = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
            if not any(a.decode("utf-8", "replace") == app_py for a in argv):
                continue
            raw = Path(f"/proc/{pid}/environ").read_bytes().split(b"\0")
        except OSError:
            continue
        env = {}
        for item in raw:
            k, sep, v = item.decode("utf-8", "replace").partition("=")
            if sep:
                env[k] = v
        if env.get("TMUX_DASH_PASS"):
            return env
    return {}


def _dash_credentials() -> tuple[str, str]:
    """(user, password) for the local dashboard. No password lives in any caller.

    The running dashboard's own env wins. This process's env comes second, NOT first: a shell
    inside a dashboard tmux pane inherits TMUX_DASH_PASS from whenever that tmux server started,
    and on instance-3 2026-09-24 that inherited copy was stale and the login was refused. The
    supervisor file is the last resort."""
    for source in (_running_dashboard_env, lambda: dict(os.environ), _supervisor_env):
        env = source()
        if env.get("TMUX_DASH_PASS"):
            return (env.get("TMUX_DASH_USER") or "admin"), env["TMUX_DASH_PASS"]
    return "admin", ""


def dashboard_delete(name, log=print, timeout=60) -> bool:
    """End `name` through the dashboard, so its registry drops the row with the session."""
    user, pw = _dash_credentials()
    jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    ua = {"User-Agent": "agent-socket-reaper/1"}
    try:
        if pw:
            data = urllib.parse.urlencode({"username": user, "password": pw}).encode()
            opener.open(urllib.request.Request(DASH_URL + "/login", data=data, headers=ua),
                        timeout=20).read()
        req = urllib.request.Request(
            DASH_URL + "/api/sessions/" + urllib.parse.quote(name, safe=""),
            method="DELETE", headers=ua)
        with opener.open(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
        try:
            ok = bool(json.loads(body).get("ok"))
        except ValueError:           # the login page came back: the credential did not work
            ok = False
        if not ok:
            log(f"dashboard DELETE {name}: unexpected answer {body[:200]!r}")
        return ok
    except Exception as e:  # noqa: BLE001  (HTTPError, URLError, timeouts: fall back to tmux)
        log(f"dashboard DELETE {name} failed ({e}); falling back to tmux kill-session")
        return False


def end_session(name, socket=SOCKET, log=print) -> str:
    """End one session and say how: 'dashboard', 'tmux' or 'gone'.

    On the default socket a session the dashboard has registered goes through its DELETE API, or
    the dashboard restores it. Anything else is a plain kill-session, and a private server left
    with no sessions is killed with it."""
    how = "gone"
    if _is_default(socket) and dashboard_registered(name) and dashboard_delete(name, log=log):
        how = "dashboard"
    elif tmux(socket, "kill-session", "-t", "=" + name).returncode == 0:
        how = "tmux"
    if not _is_default(socket):
        kill_server_if_empty(socket)
    return how


def reap(prefixes, idle_sec=IDLE_REAP_SEC, sockets=(SOCKET, DEFAULT), keep=(), log=print,
         dry=False, now=None) -> list[tuple[str, str, float, str]]:
    """End EVERY session whose name starts with one of `prefixes` and whose windows have been
    quiet for `idle_sec` or more. Returns [(socket, name, idle seconds, how)].

    A session somebody is attached to is left alone: a human reading it is not idle. The default
    socket is swept too, for sessions opened there before the move to SOCKET."""
    if isinstance(prefixes, str):
        prefixes = (prefixes,)
    prefixes = tuple(prefixes)
    now = time.time() if now is None else now
    done = []
    for sock in sockets:
        for s in list_sessions(sock):
            name = s["name"]
            if not name.startswith(prefixes) or name in keep or s["attached"]:
                continue
            idle = now - s["activity"]
            if idle < idle_sec:
                continue
            if dry:
                log(f"[dry-run] would end idle session '{name}' on socket {sock} "
                    f"(idle {idle / 3600.0:.1f}h)")
                done.append((sock, name, idle, "dry-run"))
                continue
            how = end_session(name, socket=sock, log=log)
            log(f"ended idle session '{name}' on socket {sock} (idle {idle / 3600.0:.1f}h) via {how}")
            done.append((sock, name, idle, how))
        if not dry and not _is_default(sock):
            kill_server_if_empty(sock)
    return done


def capture(name, socket=SOCKET, lines=200) -> str:
    r = tmux(socket, "capture-pane", "-p", "-J", "-t", "=" + name + ":", "-S", f"-{int(lines)}",
             timeout=5)
    return r.stdout or ""


def describe(socket=SOCKET, now=None) -> list[dict]:
    """list_sessions plus what each session's active pane is running and where, and its idle
    seconds. For the dashboard's read-only watch page."""
    now = time.time() if now is None else now
    panes = {}
    r = tmux(socket, "list-panes", "-a", "-F",
             "#{session_name}\t#{pane_active}\t#{pane_current_command}\t#{pane_current_path}",
             timeout=5)
    for line in (r.stdout or "").splitlines():
        parts = line.split("\t")
        if len(parts) == 4 and (parts[1] == "1" or parts[0] not in panes):
            panes[parts[0]] = {"cmd": parts[2], "cwd": parts[3]}
    out = []
    for s in list_sessions(socket):
        row = dict(s)
        row.update(panes.get(s["name"], {"cmd": "", "cwd": ""}))
        row["idle_s"] = max(0, int(now - s["activity"]))
        out.append(row)
    out.sort(key=lambda x: -x["created"])
    return out


# --------------------------------------------------------------------------- per-session watcher

def watch(name, socket=SOCKET, idle_sec=IDLE_REAP_SEC, max_age_sec=None, poll=60, log=print,
          now=time.time, sleep=time.sleep) -> str:
    """Block until `name` is gone. End it once its windows have been quiet for `idle_sec`, or once
    it is older than `max_age_sec` when that is set; a session somebody is attached to is left
    alone. Returns how it ended: 'dashboard', 'tmux' or 'gone' (ended by someone else)."""
    while True:
        rows = [s for s in list_sessions(socket) if s["name"] == name]
        if not rows:
            if not _is_default(socket):
                kill_server_if_empty(socket)
            return "gone"
        s, t = rows[0], now()
        idle, age = t - s["activity"], t - s["created"]
        if not s["attached"] and (idle >= idle_sec or (max_age_sec and age >= max_age_sec)):
            how = end_session(name, socket=socket, log=log)
            why = f"idle {idle / 60.0:.0f} min" if idle >= idle_sec else f"age {age / 60.0:.0f} min"
            log(f"watcher ended '{name}' on socket {socket} ({why}) via {how}")
            return how
        sleep(poll)


def spawn_watcher(name, socket=SOCKET, idle_sec=IDLE_REAP_SEC, max_age_sec=None, poll=60,
                  logfile=None) -> int:
    """Start `watch` for one session as a detached process that outlives the launcher.
    Returns its pid."""
    argv = [sys.executable, str(Path(__file__).resolve()), "watch", "--socket", str(socket),
            "--name", name, "--idle", str(int(idle_sec)), "--poll", str(int(poll))]
    if max_age_sec:
        argv += ["--max-age", str(int(max_age_sec))]
    out = open(logfile, "a") if logfile else subprocess.DEVNULL
    try:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT,
                                start_new_session=True, close_fds=True).pid
    finally:
        if logfile:
            out.close()


def _main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="End idle agent sessions on a tmux socket.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("watch", help="watch one session until it ends")
    w.add_argument("--socket", default=SOCKET)
    w.add_argument("--name", required=True)
    w.add_argument("--idle", type=int, default=IDLE_REAP_SEC)
    w.add_argument("--max-age", type=int, default=0)
    w.add_argument("--poll", type=int, default=60)
    r = sub.add_parser("reap", help="end every idle session with one of these prefixes, once")
    r.add_argument("prefixes", nargs="+")
    r.add_argument("--socket", default=SOCKET)
    r.add_argument("--idle", type=int, default=IDLE_REAP_SEC)
    r.add_argument("--dry-run", action="store_true")
    a = ap.parse_args(argv)
    stamp = lambda m: print(time.strftime("%Y-%m-%dT%H:%M:%S ") + m, flush=True)  # noqa: E731
    if a.cmd == "watch":
        watch(a.name, a.socket, a.idle, a.max_age or None, a.poll, log=stamp)
    else:
        reap(a.prefixes, a.idle, sockets=(a.socket, DEFAULT), log=stamp, dry=a.dry_run)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
