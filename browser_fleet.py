"""Who owns every browser on this box, and which of them nobody owns any more.

WHY THIS EXISTS. Three different systems open Chrome around here and none of them
can see the other two:

* the dashboard's own browser sessions (``~/.claude-browser``, a slot per session,
  CDP on ``9222+slot``, watched over noVNC),
* the live consoles (``patent_panel`` and the trademark app), which launch a Chrome
  per filing into ``9400-9449`` -- often on ANOTHER host, reached by an
  ``ssh -L`` forward,
* whatever an agent starts for itself with Playwright or an MCP browser tool.

``browser_resource_guard`` already contains the damage of the third kind. What
nothing does is say who owns what, and that gap has a specific failure attached to
it: **a port number is not an identity**. An ssh forward outlives the browser it was
opened for, the remote port is handed to the next Chrome anybody launches on that
host, and a local port silently starts answering for a stranger's browser. The
viewer then shows, and the agent then drives, somebody else's session. Measured on
builder4 2026-09-04: two forwards reaching one remote Chrome, and a third whose
browser had been dead for a day.

So this module answers three questions and acts on exactly one of them:

1. What is running (``inventory``): every Chrome, every CDP forward, the profile,
   the owner, and the browser's own CDP uuid, which is the only stable identity.
2. Is anything wrong (``problems``): a dead forward, two forwards onto one browser,
   a forward no console claims, a disposable browser whose owner is gone.
3. Is a signed-in profile still signed in (``probe_login``). Reported only. Nothing
   is ever killed because a login probe said so: a probe that cannot reach a page
   returns "unknown", and a reaper keyed on a health check calls a failing job dead.

``reap`` acts only on ownership and liveness, never on a probe, kills by pid after
re-reading the process start ticks (a recycled pid is a different process), and
never runs a broad pattern kill: that has taken out unrelated agent sessions here
before.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import signal
import subprocess
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import browser_resource_guard as guard

HOME = Path.home()
CB_ROOT = HOME / ".claude-browser"
PROBE_CONFIG = CB_ROOT / "login_probes.json"

#  Profiles that are somebody's long-lived identity. These are never reaped, whatever
#  their idle time: the whole point of them is to outlive the session that used them.
#
#  Matched by SHAPE, not by this box's own $HOME. The same inventory is run against
#  other hosts, where the tenant home is a different path, and a rule written around
#  the local home quietly reclassified every resident browser on instance-3 as
#  "unknown" the first time this was pointed at it.
RESIDENT_PROFILE_RE = re.compile(
    r"/\.claude-browser/(?:profile|sessions/)|/\.ramp-browser/")
#  Profiles that are, by construction, throwaway: one filing, one console, one run,
#  or anything under /tmp, which does not survive a reboot in the first place.
#  A live console holds a claim on its own, so an unclaimed one is finished.
DISPOSABLE_PROFILE_RE = re.compile(
    r"^/tmp/|/\.tmux-dashboard/patents/browsers/")

#  -L <local>:<host>:<remote>. Written by launch_remote as -L <local>:127.0.0.1:<remote>.
FORWARD_RE = re.compile(r"-L\s*(\d+):([\w.\-]+):(\d+)")

DEFAULT_STALE_H = 6.0


# ---------------------------------------------------------------------------
# CDP, without a dependency on the app that launched the browser
# ---------------------------------------------------------------------------
def cdp_get(port: int, path: str, timeout: float = 3.0):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
        return json.load(r)


def browser_id(port: int, timeout: float = 3.0) -> str:
    """The uuid Chrome puts in its own websocket path.

    Unique to one running Chrome, so it survives navigation and new tabs and it
    CHANGES when the process does. This is the identity every check here compares,
    because the port is not one.
    """
    try:
        url = (cdp_get(port, "/json/version", timeout=timeout) or {}).get(
            "webSocketDebuggerUrl") or ""
    except Exception:                                             # noqa: BLE001
        return ""
    return url.rsplit("/", 1)[-1] if "/devtools/browser/" in url else ""


def targets(port: int, timeout: float = 3.0) -> List[dict]:
    try:
        rows = cdp_get(port, "/json", timeout=timeout) or []
    except Exception:                                             # noqa: BLE001
        return []
    return [{"id": t.get("id"), "title": t.get("title") or "",
             "url": t.get("url") or "", "ws": t.get("webSocketDebuggerUrl") or ""}
            for t in rows if t.get("type") == "page"]


# ---------------------------------------------------------------------------
# what is running
# ---------------------------------------------------------------------------
@dataclass
class Forward:
    """One ssh CDP port-forward: a local port that is really a remote browser."""
    pid: int
    local_port: int
    remote_host: str
    remote_port: int
    command: str = ""
    started: float = 0.0
    up: bool = False
    browser_id: str = ""
    claimed_by: str = ""
    problems: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["problems"] = list(self.problems)
        return d


@dataclass
class Browser:
    pid: int
    profile: str
    cdp_port: int
    headless: bool
    command: str = ""
    started: float = 0.0
    up: bool = False
    browser_id: str = ""
    owner: str = ""
    kind: str = ""                    # resident | disposable | unknown
    claimed_by: str = ""
    tabs: List[dict] = field(default_factory=list)
    profile_mtime: float = 0.0
    problems: List[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = dict(self.__dict__)
        d["problems"] = list(self.problems)
        d["tabs"] = list(self.tabs)
        return d


def parse_forwards(rows: Iterable[Tuple[int, str]]) -> List[Forward]:
    """Every ssh in `rows` that forwards a port, as a Forward.

    `rows` is (pid, command line) pairs, which is what /proc gives locally and what
    `pgrep -af` gives over ssh: the same parser then serves both boxes.
    """
    out: List[Forward] = []
    for pid, command in rows:
        #  The ssh itself, not the gcloud wrapper that spawned it: both carry the -L
        #  on their command line, and counting the parent as well would report every
        #  forward twice and offer to kill a pid whose death leaves the real one up.
        if guard._executable_name(command) not in ("ssh", "ssh.exe"):   # noqa: SLF001
            continue
        m = FORWARD_RE.search(command)
        if not m:
            continue
        out.append(Forward(pid=int(pid), local_port=int(m.group(1)),
                           remote_host=m.group(2), remote_port=int(m.group(3)),
                           command=command))
    return out


def classify_profile(profile: str) -> str:
    """resident (an identity worth keeping), disposable (one job), or unknown."""
    if not profile:
        return "unknown"
    if RESIDENT_PROFILE_RE.search(profile):
        return "resident"
    if DISPOSABLE_PROFILE_RE.search(profile):
        return "disposable"
    return "unknown"


def owner_of(profile: str) -> str:
    """A sentence about who a browser belongs to, read off its profile path.

    Agent-started browsers keep their profile under the Claude session scratchpad, so
    the path alone names the project and the conversation. The slug is Claude's own
    cwd encoding and is not reversible into a path, so it is shown verbatim rather
    than turned into something that looks real and is not.
    """
    m = re.search(r"/tmp/claude-\d+/(-[^/]+)/([0-9a-f-]{8,})/", profile or "")
    if m:
        project = re.sub(r"^-?home-[^-]+-[^-]+-", "", m.group(1))
        return "a Claude session in %s (%s)" % (project, m.group(2)[:8])
    if profile.startswith("/tmp/patent-browser-"):
        return "a live filing console (%s)" % Path(profile).name[len("patent-browser-"):]
    if profile.startswith("/tmp/pcdemo-"):
        return "a Patent Center demo run (%s)" % Path(profile).name
    #  Suffix rules, so the answer is the same whichever box the profile is on.
    if "/.claude-browser/sessions/" in profile:
        return "dashboard browser session '%s'" % Path(profile).parent.name
    if profile.endswith("/.claude-browser/profile"):
        return "the resident dashboard browser"
    if "/.ramp-browser/" in profile:
        return "the Ramp web session"
    if "/.tmux-dashboard/patents/browsers/" in profile:
        return "a patents filing browser (%s)" % Path(profile).name
    return ""


def read_claims() -> Dict[int, str]:
    """{local CDP port: who says it owns that port}, from the console state files.

    A claim is what makes an orphan an orphan. Reading the files rather than asking
    the services keeps this usable when a service is down, which is exactly when the
    leftovers pile up.
    """
    claims: Dict[int, str] = {}
    tm = HOME / ".trademark-filing" / "agent" / "state.json"
    with contextlib.suppress(Exception):
        b = (json.loads(tm.read_text("utf-8")) or {}).get("browser") or {}
        if b.get("port"):
            claims[int(b["port"])] = "trademark agent console (%s)" % (
                b.get("profile") or "no profile recorded")
    reg = HOME / ".tmux-dashboard" / "patents" / "browsers.json"
    with contextlib.suppress(Exception):
        for key, info in (json.loads(reg.read_text("utf-8")) or {}).items():
            port = int((info or {}).get("port") or key or 0)
            if port:
                claims[port] = "patents filing panel (%s)" % key
    with contextlib.suppress(Exception):
        slots = (json.loads((CB_ROOT / "slots.json").read_text("utf-8")) or {}).get("claims") or {}
        for slot, info in slots.items():
            with contextlib.suppress(Exception):
                claims[9222 + int(slot)] = "dashboard slot %s (%s)" % (
                    slot, (info or {}).get("sid") or "?")
    return claims


def _profile_mtime(profile: str) -> float:
    with contextlib.suppress(Exception):
        return Path(profile).stat().st_mtime
    return 0.0


def local_processes() -> List[Tuple[int, str]]:
    snap = guard.snapshot_processes()
    return [(pid, row.command) for pid, row in snap.items()]


def inventory(check_cdp: bool = True, stale_h: float = DEFAULT_STALE_H) -> dict:
    """Every browser and every CDP forward on this box, with its owner and identity."""
    snap = guard.snapshot_processes()
    claims = read_claims()
    browsers: List[Browser] = []
    for root in guard.browser_roots(snapshot=snap):
        profile = root.profile or ""
        b = Browser(pid=root.process.pid, profile=profile, cdp_port=root.cdp_port,
                    headless=root.headless, command=root.process.command,
                    started=guard_started(root.process.pid),
                    kind=classify_profile(profile), owner=owner_of(profile),
                    claimed_by=claims.get(root.cdp_port, ""),
                    profile_mtime=_profile_mtime(profile))
        if check_cdp and b.cdp_port:
            b.browser_id = browser_id(b.cdp_port)
            b.up = bool(b.browser_id)
            b.tabs = targets(b.cdp_port) if b.up else []
        browsers.append(b)

    forwards = parse_forwards([(pid, r.command) for pid, r in snap.items()])
    for f in forwards:
        f.started = guard_started(f.pid)
        f.claimed_by = claims.get(f.local_port, "")
        if check_cdp:
            f.browser_id = browser_id(f.local_port)
            f.up = bool(f.browser_id)

    problems = find_problems(browsers, forwards, snap, stale_h=stale_h)
    free_mb, free_pct = _mem()
    return {"host": os.uname().nodename, "at": time.time(),
            "memory_mb": {"available": round(free_mb), "total": round(free_mb / (free_pct / 100.0))
                          if free_pct else 0},
            "browsers": [b.as_dict() for b in browsers],
            "forwards": [f.as_dict() for f in forwards],
            "problems": problems}


def guard_started(pid: int) -> float:
    with contextlib.suppress(Exception):
        rec = guard._read_proc_record(pid)                        # noqa: SLF001
        if rec:
            return float(rec.start_ticks)
    return 0.0


# ---------------------------------------------------------------------------
# what is wrong
# ---------------------------------------------------------------------------
def port_collisions(rows: Sequence[dict]) -> List[dict]:
    """Two browsers that both asked for one CDP port.

    Only one of them can have it. The loser runs on with no debugging port at all,
    which is invisible until something connects to that port expecting the other
    browser and drives the wrong window. Live on instance-3 2026-09-04: the resident
    profile and the 'default' session profile both launched with 9222.
    """
    seen: Dict[int, List[dict]] = {}
    for r in rows:
        if r.get("cdp_port"):
            seen.setdefault(int(r["cdp_port"]), []).append(r)
    out = []
    for port, group in seen.items():
        if len(group) > 1 and len({r.get("profile") for r in group}) > 1:
            out.append({"kind": "port_collision", "port": port,
                        "pids": [r["pid"] for r in group],
                        "detail": "%d browsers asked for CDP port %d (%s). One of them "
                                  "did not get it, and anything connecting to that port "
                                  "reaches the other one."
                                  % (len(group), port,
                                     ", ".join(str(r.get("profile") or r["pid"]) for r in group))})
    return out


def find_problems(browsers: Sequence[Browser], forwards: Sequence[Forward],
                  snapshot: Optional[Dict[int, "guard.ProcRecord"]] = None,
                  stale_h: float = DEFAULT_STALE_H) -> List[dict]:
    """Ownership and liveness faults. Never a judgement about a login."""
    problems: List[dict] = []
    now = time.time()

    #  A forward is only useful while the browser behind it lives. One that answers
    #  nothing is litter; one that answers for a browser two forwards share is the
    #  dangerous case, because both consoles believe they have their own.
    by_remote: Dict[Tuple[str, int], List[Forward]] = {}
    by_id: Dict[str, List[Forward]] = {}
    for f in forwards:
        by_remote.setdefault((f.remote_host, f.remote_port), []).append(f)
        if f.browser_id:
            by_id.setdefault(f.browser_id, []).append(f)
    for f in forwards:
        if not f.up:
            f.problems.append("dead")
            problems.append({"kind": "forward_dead", "pid": f.pid,
                             "port": f.local_port,
                             "detail": "127.0.0.1:%d forwards to %s:%d and nothing "
                                       "answers there. Litter, and the remote port "
                                       "will be handed to the next browser somebody "
                                       "launches on that host."
                                       % (f.local_port, f.remote_host, f.remote_port)})
    for key, group in by_remote.items():
        if len(group) > 1:
            for f in group:
                f.problems.append("duplicate")
            problems.append({"kind": "forward_duplicate", "port": group[0].local_port,
                             "pids": [f.pid for f in group],
                             "detail": "ports %s all forward to %s:%d, so they are one "
                                       "browser wearing several numbers. Whoever holds "
                                       "the port nobody claims is driving another "
                                       "session's window."
                                       % (", ".join(str(f.local_port) for f in group),
                                          key[0], key[1])})
    for bid, group in by_id.items():
        if len(group) > 1 and len({(f.remote_host, f.remote_port) for f in group}) > 1:
            problems.append({"kind": "forward_same_browser", "pids": [f.pid for f in group],
                             "detail": "ports %s reach the SAME Chrome (%s) by different "
                                       "remote ports."
                                       % (", ".join(str(f.local_port) for f in group), bid[:8])})
    for f in forwards:
        if f.up and not f.claimed_by:
            problems.append({"kind": "forward_unclaimed", "pid": f.pid, "port": f.local_port,
                             "detail": "127.0.0.1:%d reaches a live browser on %s that no "
                                       "console state file claims."
                                       % (f.local_port, f.remote_host)})

    problems.extend(port_collisions([b.as_dict() for b in browsers]))

    for b in browsers:
        if b.kind != "disposable":
            continue
        idle_h = (now - b.profile_mtime) / 3600.0 if b.profile_mtime else 0.0
        if not b.claimed_by and idle_h >= stale_h:
            b.problems.append("stale")
            problems.append({"kind": "browser_stale", "pid": b.pid, "port": b.cdp_port,
                             "profile": b.profile, "idle_h": round(idle_h, 1),
                             "detail": "%s: a throwaway profile nobody claims, untouched "
                                       "for %.1f h."
                                       % (b.owner or b.profile, idle_h)})
    return problems


# ---------------------------------------------------------------------------
# is it still signed in
# ---------------------------------------------------------------------------
def load_probes() -> List[dict]:
    """Login probes, from ~/.claude-browser/login_probes.json.

    Each entry: {"name", "match" (substring of the profile path), "url",
    "signed_in": [markers], "signed_out": [markers]}. A probe that matches neither
    set answers "unknown", and unknown is a real answer: the alternative is a guard
    that reports a browser logged out because a page was slow.
    """
    with contextlib.suppress(Exception):
        data = json.loads(PROBE_CONFIG.read_text("utf-8"))
        if isinstance(data, list):
            return [p for p in data if isinstance(p, dict) and p.get("url")]
    return []


_TAG_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1>|<[^>]+>", re.S | re.I)


def _text_of(html: str) -> str:
    """Readable text out of markup, well enough to look for a marker in it.

    Not a parser: a login marker is a phrase on the page, and script bodies are
    dropped because half the world's pages carry the words "sign in" inside their
    analytics payload.
    """
    text = _TAG_RE.sub(" ", html or "")
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&")
            .replace("&lt;", "<").replace("&gt;", ">").replace("&#39;", "'"))
    return re.sub(r"\s+", " ", text).strip()[:6000]


async def _cookies(ws_url: str, wait_s: float) -> List[dict]:
    """Every cookie in the browser, over the BROWSER-level session.

    Why not open a tab and look at the page: on builder4's Chrome 152 a page session
    wedges after a navigation (Page.navigate never answers and the next call on that
    session times out), so a probe built on driving a tab cannot work here at all.
    Worse, on a SHARED browser it would be the very collision this module exists to
    report: somebody else's window, with a tab appearing in it.

    `Storage.getCookies` answers at browser level on the same Chrome that refuses
    everything else, verified 2026-09-04. `Network.getAllCookies` returns nothing
    there, so it is not a fallback.
    """
    from websockets.asyncio.client import connect as ws_connect        # local import
    async with ws_connect(ws_url, max_size=None, open_timeout=8) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Storage.getCookies", "params": {}}))
        deadline = time.time() + wait_s
        while time.time() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=max(1.0, deadline - time.time()))
            data = json.loads(raw)
            if data.get("id") == 1:
                return ((data.get("result") or {}).get("cookies") or [])
    raise TimeoutError("Storage.getCookies")


def browser_cookies(ws_url: str, wait_s: float = 12.0) -> List[dict]:
    return asyncio.run(_cookies(ws_url, wait_s))


def cookies_for(cookies: Sequence[dict], host: str) -> List[dict]:
    """The cookies a browser would send to `host`, matched the way a browser does:
    an exact host, or a domain cookie on any parent of it."""
    host = (host or "").lower().strip(".")
    out = []
    for c in cookies:
        dom = str(c.get("domain") or "").lower().lstrip(".")
        if not dom:
            continue
        if host == dom or host.endswith("." + dom):
            out.append(c)
    return out


def _fetch_as_browser(url: str, cookies: Sequence[dict], timeout: float = 20.0) -> Tuple[str, str]:
    """GET `url` carrying those cookies, with a real Chrome TLS fingerprint.

    Returns (final url, text). curl_cffi and not requests: python's handshake draws a
    403 from the same WAFs Chrome walks through, and a probe that reports "signed
    out" because the client was fingerprinted is worse than no probe.
    """
    jar = "; ".join("%s=%s" % (c.get("name"), c.get("value")) for c in cookies if c.get("name"))
    headers = {"Cookie": jar} if jar else {}
    try:
        from curl_cffi import requests as cffi                  # type: ignore
        r = cffi.get(url, headers=headers, impersonate="chrome", timeout=timeout,
                     allow_redirects=True)
        return str(r.url), _text_of(r.text)
    except ImportError:
        import urllib.request
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.geturl(), _text_of(resp.read().decode("utf-8", "replace"))


def probe_login(port: int, probe: dict, wait_s: float = 20.0) -> dict:
    """Ask ONE browser whether its session for a site is still good.

    What this actually tests is the cookie jar: the browser's own cookies, replayed
    over an HTTP request that looks like Chrome. That is the question worth asking
    before an agent takes a profile, and it costs no tab and no rendering. It is
    reported and never acted on: a probe that could not reach the site answers
    "unknown", which is a different thing from "logged out" and must stay different,
    because the re-auth an over-eager probe triggers is what gets an account locked.
    """
    name = probe.get("name") or probe.get("url") or "probe"
    try:
        version = cdp_get(port, "/json/version", timeout=4.0)
        ws_url = version.get("webSocketDebuggerUrl") or ""
    except Exception as exc:                                      # noqa: BLE001
        return {"name": name, "state": "unknown", "detail": "no CDP on %d: %s" % (port, exc)}
    if not ws_url:
        return {"name": name, "state": "unknown",
                "detail": "browser on %d has no websocket endpoint" % port}
    try:
        jar = browser_cookies(ws_url, wait_s=wait_s)
    except Exception as exc:                                      # noqa: BLE001
        return {"name": name, "state": "unknown",
                "detail": "could not read the cookie jar: %s" % str(exc)[:120]}
    from urllib.parse import urlsplit
    host = urlsplit(probe["url"]).hostname or ""
    mine = cookies_for(jar, host)
    if not mine:
        return {"name": name, "state": "unknown",
                "detail": "this browser holds no cookies for %s" % host}
    try:
        final_url, text = _fetch_as_browser(probe["url"], mine)
    except Exception as exc:                                      # noqa: BLE001
        return {"name": name, "state": "unknown",
                "detail": "fetch failed: %s" % str(exc)[:120]}
    low, low_url = text.lower(), (final_url or "").lower()
    for marker in probe.get("signed_out_url") or []:
        if marker.lower() in low_url:
            return {"name": name, "state": "signed_out",
                    "detail": "redirected to %s" % final_url[:120]}
    for marker in probe.get("signed_out") or []:
        if marker.lower() in low:
            return {"name": name, "state": "signed_out", "detail": "matched %r" % marker}
    for marker in probe.get("signed_in") or []:
        if marker.lower() in low:
            return {"name": name, "state": "ok", "detail": "matched %r (%d cookies)"
                                                           % (marker, len(mine))}
    return {"name": name, "state": "unknown",
            "detail": "neither marker in %d chars from %s" % (len(text), final_url[:80])}


def probe_all(inv: dict, wait_s: float = 20.0) -> List[dict]:
    out = []
    probes = load_probes()
    for b in inv.get("browsers", []):
        if not b.get("up"):
            continue
        for p in probes:
            if p.get("match") and p["match"] not in (b.get("profile") or ""):
                continue
            res = probe_login(b["cdp_port"], p, wait_s=wait_s)
            res.update({"port": b["cdp_port"], "profile": b.get("profile")})
            out.append(res)
    return out


# ---------------------------------------------------------------------------
# one identity, one holder at a time
# ---------------------------------------------------------------------------
#  A LEASE IS TAKEN ON AN IDENTITY, NOT ON A BROWSER. Two agents driving one
#  signed-in profile is how a site sees two sessions from one account, logs one of
#  them out and asks the other for a code. The identity is the scarce thing: the
#  google account, the USPTO account, the Ramp session. A browser is just the
#  process that happens to be holding it.
#
#  The file is shared by every dashboard on the box, like slots.json, because that
#  is the level the collision happens at.
LEASES_FILE = CB_ROOT / "leases.json"
DEFAULT_LEASE_S = 3600.0
#  Enough room for one more Chrome, judged the way browser_ladder judges it: these
#  boxes livelock rather than OOM-kill, so the refusal has to come before the launch.
MIN_FREE_MB = 900.0
MIN_FREE_PCT = 8.0
MAX_BROWSERS = int(os.environ.get("CB_MAX_BROWSERS", "6"))


def _mem() -> Tuple[float, float]:
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = float(v.strip().split()[0]) / 1024.0
        free = info.get("MemAvailable", 0.0)
        total = info.get("MemTotal", 1.0)
        return free, (free / total * 100.0 if total else 0.0)
    except Exception:                                             # noqa: BLE001
        return 0.0, 0.0


def afford_browser(running: int = -1) -> Tuple[bool, str]:
    """Can this box open one more browser right now?

    Admission control, rather than the 39-slot port map, which only ever said
    whether a NUMBER was free. A slot map cannot refuse anything, and the ladder's
    own guard covers only ladder runs, so the live consoles were launching into
    whatever was left.
    """
    mb, pct = _mem()
    if mb < MIN_FREE_MB or pct < MIN_FREE_PCT:
        return False, ("only %.0f MB (%.1f%%) of memory left: a Chrome here is how "
                       "this box livelocks, so the launch is refused, not risked"
                       % (mb, pct))
    if running < 0:
        try:
            running = len([b for b in guard.browser_roots() if b.cdp_port])
        except Exception:                                         # noqa: BLE001
            running = 0
    if running >= MAX_BROWSERS:
        return False, ("%d browsers are already open and the ceiling is %d "
                       "(CB_MAX_BROWSERS); close one or wait" % (running, MAX_BROWSERS))
    return True, ""


def egress_id(identity: str) -> str:
    """The proxy session an identity always uses.

    Derived from the identity and nothing else, so the same account is seen from the
    same exit every time. Rotating the exit under a live session is what triggers the
    re-auth and the 2FA storm; a per-slot or per-launch session id does exactly that
    by accident.
    """
    return "id-" + hashlib.sha1(identity.encode("utf-8")).hexdigest()[:10]


def _leases_read() -> dict:
    try:
        data = json.loads(LEASES_FILE.read_text("utf-8"))
        rows = data.get("leases") if isinstance(data, dict) else None
        return rows if isinstance(rows, dict) else {}
    except Exception:                                             # noqa: BLE001
        return {}


def _leases_write(rows: dict) -> None:
    with contextlib.suppress(Exception):
        LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = LEASES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"leases": rows}, indent=1), "utf-8")
        tmp.replace(LEASES_FILE)


@contextlib.contextmanager
def _leases_lock():
    """Exclusive across processes: two sessions asking at once is the whole point."""
    fh = None
    try:
        LEASES_FILE.parent.mkdir(parents=True, exist_ok=True)
        import fcntl
        fh = open(str(LEASES_FILE) + ".lock", "w")
        fcntl.flock(fh, fcntl.LOCK_EX)
        yield
    except Exception:                                             # noqa: BLE001
        yield
    finally:
        if fh:
            with contextlib.suppress(Exception):
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
                fh.close()


def _live(row: dict, now: float) -> bool:
    return float(row.get("expires") or 0) > now


def leases(now: Optional[float] = None) -> dict:
    """Every lease that has not expired. Expiry is not tidiness: a session dies
    without releasing anything, and an identity nobody can ever take again is worse
    than one taken twice."""
    now = time.time() if now is None else now
    return {k: v for k, v in _leases_read().items() if _live(v, now)}


def acquire(identity: str, holder: str, ttl_s: float = DEFAULT_LEASE_S,
            check_capacity: bool = True, now: Optional[float] = None) -> dict:
    """Take the lease on an identity, or say who has it and for how long."""
    now = time.time() if now is None else now
    if not identity or not holder:
        return {"ok": False, "reason": "identity and holder are both required"}
    with _leases_lock():
        rows = _leases_read()
        held = rows.get(identity)
        if held and _live(held, now) and held.get("holder") != holder:
            return {"ok": False, "reason": "held", "held_by": held.get("holder"),
                    "expires_in": round(float(held["expires"]) - now),
                    "detail": "%s is driving %s for another %d s. Wait or use another "
                              "identity: two agents on one signed-in profile is what "
                              "logs the account out."
                              % (held.get("holder"), identity,
                                 float(held["expires"]) - now)}
        if check_capacity and not (held and _live(held, now)):
            ok, why = afford_browser()
            if not ok:
                return {"ok": False, "reason": "capacity", "detail": why}
        row = {"identity": identity, "holder": holder, "acquired": now,
               "expires": now + float(ttl_s), "egress": egress_id(identity)}
        rows[identity] = row
        _leases_write(rows)
        return {"ok": True, "lease": row}


def renew(identity: str, holder: str, ttl_s: float = DEFAULT_LEASE_S,
          now: Optional[float] = None) -> dict:
    return acquire(identity, holder, ttl_s, check_capacity=False, now=now)


def release(identity: str, holder: str) -> dict:
    """Give it back. Only the holder may: a release by anyone else is how one agent
    takes a profile out from under another that is mid-form."""
    with _leases_lock():
        rows = _leases_read()
        held = rows.get(identity)
        if not held:
            return {"ok": True, "detail": "nothing held"}
        if held.get("holder") != holder:
            return {"ok": False, "reason": "not yours",
                    "held_by": held.get("holder")}
        rows.pop(identity, None)
        _leases_write(rows)
        return {"ok": True}


# ---------------------------------------------------------------------------
# clearing up
# ---------------------------------------------------------------------------
def reap_plan(inv: dict, stale_h: float = DEFAULT_STALE_H) -> List[dict]:
    """What could be killed, and the reason, in the order a human would agree with.

    Forwards first: a dead or duplicated forward is the thing that makes a browser
    point at a stranger, and killing one cannot lose work. Browsers second, and only
    throwaway profiles nobody claims.
    """
    plan: List[dict] = []
    for f in inv.get("forwards", []):
        if f.get("claimed_by"):
            continue
        if not f.get("up"):
            plan.append({"what": "forward", "pid": f["pid"], "port": f["local_port"],
                         "reason": "forwards to %s:%s and nothing answers"
                                   % (f["remote_host"], f["remote_port"])})
        elif "duplicate" in (f.get("problems") or []):
            plan.append({"what": "forward", "pid": f["pid"], "port": f["local_port"],
                         "reason": "duplicate forward onto %s:%s, and no console claims it"
                                   % (f["remote_host"], f["remote_port"])})
    for b in inv.get("browsers", []):
        if b.get("kind") != "disposable" or b.get("claimed_by"):
            continue
        if "stale" in (b.get("problems") or []):
            plan.append({"what": "browser", "pid": b["pid"], "port": b.get("cdp_port"),
                         "profile": b.get("profile"),
                         "reason": "throwaway profile, unclaimed, idle %.1f h"
                                   % ((time.time() - (b.get("profile_mtime") or 0)) / 3600.0)})
    return plan


def _kill(pid: int, start_ticks: float) -> bool:
    """SIGTERM one pid, but only if it is still the process we looked at.

    A pid is recycled in minutes on a busy box, and this function exists precisely to
    avoid the broad pattern kill that has taken out other people's sessions here.
    """
    if guard_started(pid) != start_ticks:
        return False
    with contextlib.suppress(Exception):
        os.kill(int(pid), signal.SIGTERM)
        return True
    return False


def reap(inv: dict, dry_run: bool = True, stale_h: float = DEFAULT_STALE_H) -> dict:
    plan = reap_plan(inv, stale_h=stale_h)
    starts = {}
    for row in inv.get("forwards", []) + inv.get("browsers", []):
        starts[row["pid"]] = row.get("started") or 0.0
    done = []
    for item in plan:
        if dry_run:
            done.append({**item, "killed": False, "dry_run": True})
            continue
        ok = _kill(item["pid"], starts.get(item["pid"], -1))
        done.append({**item, "killed": ok})
    return {"planned": len(plan), "killed": sum(1 for d in done if d.get("killed")),
            "actions": done, "dry_run": dry_run}


# ---------------------------------------------------------------------------
# another host, same parsers
# ---------------------------------------------------------------------------
#  No $ and no single quotes in here: the snippet is quoted once for the local shell
#  and once more by the remote login shell, and an awk program with $2 in it came
#  back empty every time because the far shell expanded it before awk ever saw it.
#  A separator that cannot appear in a process command line. "@@" can, and did: the
#  block count came back as seven and the memory reading was silently lost. It also
#  cannot start with #, which bash reads as a comment, so `echo #x` prints nothing
#  and every block silently merges into one.
REMOTE_SEP = "___browser_fleet___"
REMOTE_SNIPPET = (
    "pgrep -af -- --remote-debugging-port= | grep -v -- --type= ; echo %(s)s ; "
    "pgrep -af -- -L | grep ssh ; echo %(s)s ; "
    'free -m | tr -s " " | sed -n 2p | cut -d" " -f2,7') % {"s": REMOTE_SEP}


def parse_remote_browsers(lines: Iterable[str]) -> List[dict]:
    """`pgrep -af` lines from another box, as one row per browser.

    ONE BROWSER IS SEVERAL PROCESSES with the same command line: the launcher script
    or `dbus-run-session`, then Chrome itself. Reporting each of them is how a box
    with seven browsers reads as fourteen, which makes a memory problem look twice as
    bad as it is. The profile and the CDP port together identify the browser, so
    collapse on those and keep the real Chrome binary when it is one of the rows.
    """
    seen: Dict[Tuple[str, int], dict] = {}
    for line in lines:
        pid, _, cmd = line.strip().partition(" ")
        if not pid.isdigit() or not cmd:
            continue
        profile = guard._flag(cmd, "--user-data-dir")             # noqa: SLF001
        try:
            port = int(guard._flag(cmd, "--remote-debugging-port") or 0)   # noqa: SLF001
        except ValueError:
            port = 0
        row = {"pid": int(pid), "profile": profile, "cdp_port": port,
               "headless": "--headless" in cmd,
               "kind": classify_profile(profile), "owner": owner_of(profile),
               "is_chrome_binary": guard._executable_name(cmd) == "chrome"}    # noqa: SLF001
        key = (profile, port)
        if key not in seen or (row["is_chrome_binary"] and not seen[key]["is_chrome_binary"]):
            seen[key] = row
    return sorted(seen.values(), key=lambda r: r["pid"])


def remote_inventory(host: str, zone: str = "us-central1-b",
                     user: str = "nimrod_rotem", timeout: int = 120) -> dict:
    """The same questions, asked of another box over one ssh.

    Only what a command line can answer: the CDP identity checks need a socket, and
    tunnelling one to run a health check would be the very thing this module warns
    about.
    """
    out = subprocess.run(
        ["gcloud", "compute", "ssh", "%s@%s" % (user, host), "--zone", zone,
         "--command", "sudo -u %s bash -lc %s" % (user, shlex.quote(REMOTE_SNIPPET))],
        capture_output=True, text=True, timeout=timeout)
    blocks = (out.stdout or "").split(REMOTE_SEP)
    rows = parse_remote_browsers((blocks[0] if blocks else "").splitlines())
    fwd_rows = []
    if len(blocks) > 2:
        for line in blocks[-2].splitlines():
            pid, _, cmd = line.strip().partition(" ")
            if pid.isdigit():
                fwd_rows.append((int(pid), cmd))
    mem = (blocks[-1].strip().split() if len(blocks) > 2 else [])
    return {"host": host, "browsers": rows, "problems": port_collisions(rows),
            "forwards": [f.as_dict() for f in parse_forwards(fwd_rows)],
            "memory_mb": {"total": int(mem[0]), "available": int(mem[1])} if len(mem) == 2 else {},
            "stderr": (out.stderr or "")[-200:] if out.returncode else ""}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_human(inv: dict, probes: Optional[List[dict]] = None) -> None:
    print("%s  %d browser(s), %d forward(s)"
          % (inv.get("host", "?"), len(inv["browsers"]), len(inv["forwards"])))
    for b in inv["browsers"]:
        print("  chrome pid %-8s :%-5s %-10s %-6s %s"
              % (b["pid"], b["cdp_port"] or "-", b["kind"],
                 "up" if b["up"] else "down",
                 b["claimed_by"] or b["owner"] or b["profile"]))
        for t in b.get("tabs") or []:
            print("        tab %s" % (t["url"] or t["title"])[:110])
    for f in inv["forwards"]:
        print("  tunnel pid %-8s :%-5s -> %s:%-5s %-6s %s"
              % (f["pid"], f["local_port"], f["remote_host"], f["remote_port"],
                 "up" if f["up"] else "dead", f["claimed_by"] or "UNCLAIMED"))
    for p in inv["problems"]:
        print("  ! %-20s %s" % (p["kind"], p["detail"]))
    for r in probes or []:
        print("  login %-22s %-10s %s" % (r["name"][:22], r["state"], r["detail"][:70]))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Who owns every browser on this box.")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--probe", action="store_true", help="also ask each signed-in profile whether it still is")
    ap.add_argument("--reap", action="store_true", help="kill orphaned forwards and browsers")
    ap.add_argument("--yes", action="store_true", help="with --reap, actually kill")
    ap.add_argument("--stale-hours", type=float, default=DEFAULT_STALE_H)
    ap.add_argument("--lease", default="", metavar="IDENTITY",
                    help="take the lease on an identity (needs --holder)")
    ap.add_argument("--release", default="", metavar="IDENTITY",
                    help="give an identity back (needs --holder)")
    ap.add_argument("--holder", default="", help="who is asking: a session name or a job")
    ap.add_argument("--ttl", type=float, default=DEFAULT_LEASE_S,
                    help="lease length in seconds (default %d)" % DEFAULT_LEASE_S)
    ap.add_argument("--leases", action="store_true", help="who holds what right now")
    ap.add_argument("--egress", default="", metavar="IDENTITY",
                    help="the proxy session this identity should always use")
    ap.add_argument("--admit", action="store_true",
                    help="can this box afford one more browser: exit 0 yes, 1 no")
    ap.add_argument("--wall", default="", metavar="FILE",
                    help="write an HTML page showing every browser on the fleet")
    ap.add_argument("--host", default="", help="inventory another box over ssh")
    ap.add_argument("--zone", default="us-central1-b")
    args = ap.parse_args(list(argv) if argv is not None else None)

    if args.admit:
        ok, why = afford_browser()
        print("yes" if ok else "no: %s" % why)
        return 0 if ok else 1
    if args.lease or args.release:
        if not args.holder:
            print("--holder is required: a lease with no holder cannot be released")
            return 2
        res = (acquire(args.lease, args.holder, args.ttl) if args.lease
               else release(args.release, args.holder))
        print(json.dumps(res, indent=1) if args.json else _lease_line(res))
        return 0 if res.get("ok") else 1
    if args.egress:
        sid = egress_id(args.egress)
        print(sid if args.json else
              ("%s\n  python3 ~/.claude-browser/bin/proxy-ctl.py session add %s --country us\n"
               "  (pin the exit to the IDENTITY, never to the slot or the launch: a new "
               "exit under a live session is what asks for a code)" % (sid, sid)))
        return 0
    if args.leases:
        rows = leases()
        if args.json:
            print(json.dumps(rows, indent=1))
        elif not rows:
            print("no identity is leased")
        else:
            for ident, row in sorted(rows.items()):
                print("  %-28s %-24s %5d s left  egress %s"
                      % (ident, row.get("holder"), row["expires"] - time.time(),
                         row.get("egress")))
        return 0
    if args.wall:
        boxes = [inventory()]
        if args.host:
            boxes.append(remote_inventory(args.host, args.zone))
        Path(args.wall).write_text(wall_html(boxes, probe_all(boxes[0]) if args.probe else []),
                                   "utf-8")
        print(args.wall)
        return 0

    if args.host:
        inv = remote_inventory(args.host, args.zone)
        print(json.dumps(inv, indent=1) if args.json else _print_remote(inv))
        return 0

    inv = inventory(stale_h=args.stale_hours)
    probes = probe_all(inv) if args.probe else None
    if args.reap:
        res = reap(inv, dry_run=not args.yes, stale_h=args.stale_hours)
        inv["reap"] = res
    if args.json:
        print(json.dumps({**inv, "logins": probes}, indent=1))
    else:
        _print_human(inv, probes)
        if args.reap:
            for a in inv["reap"]["actions"]:
                print("  %s %s pid %s: %s"
                      % ("killed" if a.get("killed") else "would kill",
                         a["what"], a["pid"], a["reason"]))
            if not args.yes:
                print("  (dry run: add --yes to act)")
    return 0


def _esc(text) -> str:
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


#  Under this share of memory a box is close enough to trouble to say so on the
#  page. The shed-early rule fires at 6%; a warning that first appears at 6% is a
#  warning nobody can act on, because by then the box is already swapping.
PRESSURE_PCT = 15.0


def memory_pressure(mem: dict) -> str:
    """A sentence when a box is short of room for its browsers, "" when it is not."""
    try:
        available, total = float(mem.get("available") or 0), float(mem.get("total") or 0)
    except (TypeError, ValueError):
        return ""
    if not total or not available:
        return ""
    pct = available / total * 100.0
    if pct >= PRESSURE_PCT:
        return ""
    return ("%.0f MB usable of %.0f, %.1f%%. These boxes livelock rather than "
            "OOM-kill, so close a browser here before something else has to."
            % (available, total, pct))


def wall_html(boxes: Sequence[dict], probes: Sequence[dict] = ()) -> str:
    """One page showing every browser on the fleet, who owns it and what it is on.

    Deliberately not a live stream: a stream costs a core per viewer, and the
    question this answers is "who is driving what, and is anything stuck", which a
    table answers better than a wall of video. Click through to the console that owns
    a browser when you actually need to watch it.
    """
    now = time.strftime("%Y-%m-%d %H:%M:%S %Z")
    held = leases()
    out = ["<!doctype html><meta charset='utf-8'>",
           "<title>Browsers on the fleet</title>",
           "<style>",
           ":root{color-scheme:light dark;--bg:#fbfbfa;--fg:#1a1a19;--muted:#6b6b66;",
           "--line:#e3e3df;--card:#fff;--warn:#8a4b00;--warnbg:#fff4e5}",
           "@media (prefers-color-scheme:dark){:root{--bg:#16161a;--fg:#eceCe8;",
           "--muted:#9a9a94;--line:#2c2c31;--card:#1d1d22;--warn:#ffb870;--warnbg:#3a2a12}}",
           "body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 ui-sans-serif,",
           "system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:28px}",
           "h1{font-size:19px;margin:0 0 4px} h2{font-size:15px;margin:26px 0 8px}",
           ".muted{color:var(--muted);font-size:12.5px}",
           "table{border-collapse:collapse;width:100%;margin-top:8px;background:var(--card);",
           "border:1px solid var(--line);border-radius:8px;overflow:hidden}",
           "th,td{text-align:left;padding:7px 10px;border-bottom:1px solid var(--line);",
           "vertical-align:top;font-size:13px}",
           "th{font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.04em;",
           "color:var(--muted)} tr:last-child td{border-bottom:0}",
           "code{font:12px ui-monospace,SFMono-Regular,Menlo,monospace}",
           ".warn{background:var(--warnbg);color:var(--warn);padding:8px 10px;border-radius:6px;",
           "margin:6px 0;font-size:13px}",
           ".wrap{max-width:1100px;margin:0 auto}.tabs{color:var(--muted);font-size:12px}",
           "</style>",
           "<div class=wrap><h1>Browsers on the fleet</h1>",
           "<div class=muted>%s. Ownership comes from the profile path and the console "
           "state files; identity is the browser's own CDP uuid, never the port.</div>" % _esc(now)]
    for box in boxes:
        mem = box.get("memory_mb") or {}
        out.append("<h2>%s</h2>" % _esc(box.get("host", "?")))
        if mem:
            out.append("<div class=muted>%s MB free of %s</div>"
                       % (_esc(mem.get("available")), _esc(mem.get("total"))))
            #  SAY IT WHERE SOMEBODY LOOKS. These boxes livelock rather than
            #  OOM-kill, so the useful moment is while there is still room to close
            #  something, not at 6% when the DHCP lease is already going.
            pressure = memory_pressure(mem)
            if pressure:
                out.append("<div class=warn><b>memory</b> %s</div>" % _esc(pressure))
        for p in box.get("problems") or []:
            out.append("<div class=warn><b>%s</b> %s</div>"
                       % (_esc(p.get("kind")), _esc(p.get("detail"))))
        rows = box.get("browsers") or []
        if rows:
            out.append("<table><tr><th>pid</th><th>port</th><th>kind</th><th>owner</th>"
                       "<th>identity</th><th>open tabs</th></tr>")
            for b in rows:
                tabs = b.get("tabs") or []
                tab_text = "<br>".join(_esc((t.get("url") or t.get("title"))[:90])
                                       for t in tabs[:4]) or "<span class=muted>not asked</span>"
                out.append("<tr><td><code>%s</code></td><td><code>%s</code></td><td>%s</td>"
                           "<td>%s</td><td><code>%s</code></td><td class=tabs>%s</td></tr>"
                           % (_esc(b.get("pid")), _esc(b.get("cdp_port")), _esc(b.get("kind")),
                              _esc(b.get("claimed_by") or b.get("owner") or b.get("profile")),
                              _esc((b.get("browser_id") or "")[:8] or "-"), tab_text))
            out.append("</table>")
        fwds = box.get("forwards") or []
        if fwds:
            out.append("<table><tr><th>tunnel pid</th><th>local</th><th>reaches</th>"
                       "<th>state</th><th>claimed by</th></tr>")
            for f in fwds:
                out.append("<tr><td><code>%s</code></td><td><code>%s</code></td>"
                           "<td><code>%s:%s</code></td><td>%s</td><td>%s</td></tr>"
                           % (_esc(f.get("pid")), _esc(f.get("local_port")),
                              _esc(f.get("remote_host")), _esc(f.get("remote_port")),
                              "up" if f.get("up") else "dead",
                              _esc(f.get("claimed_by") or "nobody")))
            out.append("</table>")
    if probes:
        out.append("<h2>Sessions</h2><table><tr><th>identity</th><th>state</th>"
                   "<th>evidence</th></tr>")
        for r in probes:
            out.append("<tr><td>%s</td><td>%s</td><td class=muted>%s</td></tr>"
                       % (_esc(r.get("name")), _esc(r.get("state")), _esc(r.get("detail"))))
        out.append("</table>")
    out.append("<h2>Leases</h2>")
    if held:
        out.append("<table><tr><th>identity</th><th>holder</th><th>expires in</th>"
                   "<th>egress</th></tr>")
        for ident, row in sorted(held.items()):
            out.append("<tr><td>%s</td><td>%s</td><td>%d s</td><td><code>%s</code></td></tr>"
                       % (_esc(ident), _esc(row.get("holder")),
                          row["expires"] - time.time(), _esc(row.get("egress"))))
        out.append("</table>")
    else:
        out.append("<div class=muted>No identity is leased. One holder at a time per "
                   "signed-in account: two agents on one profile is what logs it out.</div>")
    out.append("</div>")
    return "\n".join(out)


def _lease_line(res: dict) -> str:
    if res.get("ok"):
        lease = res.get("lease")
        return ("held by %s until %s (egress %s)"
                % (lease["holder"], time.strftime("%H:%M:%S", time.localtime(lease["expires"])),
                   lease["egress"])) if lease else "released"
    return "refused: %s" % (res.get("detail") or res.get("reason") or "no reason given")


def _print_remote(inv: dict) -> str:
    lines = ["%s  %d browser(s), %d forward(s), %s MB free of %s"
             % (inv["host"], len(inv["browsers"]), len(inv["forwards"]),
                (inv.get("memory_mb") or {}).get("available", "?"),
                (inv.get("memory_mb") or {}).get("total", "?"))]
    for b in inv["browsers"]:
        lines.append("  chrome pid %-8s :%-5s %-10s %s"
                     % (b["pid"], b["cdp_port"] or "-", b["kind"],
                        b["owner"] or b["profile"]))
    for f in inv["forwards"]:
        lines.append("  tunnel pid %-8s :%-5s -> %s:%s"
                     % (f["pid"], f["local_port"], f["remote_host"], f["remote_port"]))
    for p in inv.get("problems") or []:
        lines.append("  ! %-20s %s" % (p["kind"], p["detail"]))
    return "\n".join(lines)


if __name__ == "__main__":
    raise SystemExit(main())
