"""A browser a human can actually log in with: CDP screencast, not VNC.

WHY THIS EXISTS (2026-09-05). Nimo: *"novnc gets stuck, is very slow and still
blocked by Meta usually."* Two of those three turned out to be one fault and the
third was never true.

Measured on builder2 the day this was written, with a real headed Chrome and no
proxy: `https://www.facebook.com/login/` renders its login form and
`business.facebook.com` renders its sign-in page. **Meta does not block this
box.** What blocked it was our own plumbing: the shared browser is launched with
`--proxy-server=http://127.0.0.1:3128`, that relay forwards to Decodo, and the
Decodo subscription is dead. Both the metered gateway and all ten static ISP
ports refuse connections. A dead HTTP proxy does not fail a page, it hangs it, so
every site looked like it was refusing us and the browser looked "stuck".

So this module gives a browser whose egress is DIRECT by default, and which says
out loud which exit it is on and whether that exit can reach the internet. An
egress that cannot is the one failure that must never again look like a website's
fault.

The speed half is the transport. noVNC pushes the whole X framebuffer through an
X server, a VNC encoder and a websocket proxy, and pays that cost per viewer
whether or not anything moved. `Page.startScreencast` has Chrome encode the page
viewport itself and emit a JPEG only when it changes: an idle page costs nothing
on the wire, and a busy one costs a fraction of VNC. The frame logic is
`browser_live.Screencast`, already load-bearing for the patents filing panel, so
it is subclassed rather than copied: everything here is the interactive half it
does not have (mouse press and release as separate events, so drag and text
selection work; real key events with modifiers; paste).

The profile is PERSISTENT and per browser. That is the point of the thing: he
signs in to Meta once, here, and the session is still there for an agent tomorrow.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import time
from pathlib import Path

from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse

import browser_live

STATE_ROOT = Path(os.environ.get("CONSOLE_BROWSER_DIR",
                                 Path.home() / ".tmux-dashboard" / "console-browser"))
STATE_FILE = STATE_ROOT / "state.json"
PORT_RANGE = range(9460, 9492)
DISPLAY_RANGE = range(120, 152)

# A real desktop Chrome on Linux. Deliberately not this box's own build string:
# what matters is that it is an ordinary headed desktop UA, and 1440x900 is an
# ordinary laptop rather than the 1280x720 that reads as a bot.
CONSOLE_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/149.0.0.0 Safari/537.36")
WIN_W, WIN_H = 1440, 900

# Where a browser can send its traffic. `direct` is the default and the only one
# that is known good: see the module docstring. The relay entry is kept because
# the plumbing is still there and a site that blocks GCP is a real thing, but it
# is never chosen for you, and the exit-IP probe below is what tells you whether
# it is alive today.
EGRESS = {
    "direct": {"label": "Direct", "arg": None,
               "note": "this box's own IP. Fastest, and Meta accepts it."},
    "relay": {"label": "Residential relay", "arg": "--proxy-server=http://127.0.0.1:3128",
              "note": "the Decodo relay on :3128. Verify the exit IP before trusting it."},
    "warp": {"label": "Cloudflare WARP", "arg": "--proxy-server=socks5://127.0.0.1:25344",
             "note": "free, not a GCP range. Only present if wireproxy is running."},
}
DEFAULT_EGRESS = "direct"

# Nothing watching and nothing happening: close the tabs, keep the cookies. The
# browser stays signed in but stops costing a core.
IDLE_PARK_S = float(os.environ.get("CONSOLE_BROWSER_IDLE_PARK_S", "1800"))


# ---------------------------------------------------------------------------
# state
# ---------------------------------------------------------------------------
def _load() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {}


def _save(state: dict) -> None:
    STATE_ROOT.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_FILE)


def _free_port() -> int:
    for port in PORT_RANGE:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free CDP port in %s" % PORT_RANGE)


def _free_display() -> str:
    for n in DISPLAY_RANGE:
        if not Path("/tmp/.X11-unix/X%d" % n).exists():
            return ":%d" % n
    raise RuntimeError("no free X display")


def _alive(pid) -> bool:
    try:
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# the browser
# ---------------------------------------------------------------------------
def _start_xvfb(display: str) -> int:
    """A headed Chrome needs a display. Headless is not an option here: it
    advertises HeadlessChrome, it paints differently, and the whole point of this
    browser is to look like the laptop of the person signing in."""
    proc = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "%dx%dx24" % (WIN_W, WIN_H),
         "-nolisten", "tcp", "-ac"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    sock = "/tmp/.X11-unix/X%s" % display.lstrip(":")
    deadline = time.time() + 15
    while time.time() < deadline:
        if Path(sock).exists():
            return proc.pid
        if proc.poll() is not None:
            raise RuntimeError("Xvfb %s exited immediately" % display)
        time.sleep(0.2)
    raise RuntimeError("Xvfb %s never came up" % display)


def status(name: str = "default") -> dict:
    st = _load().get(name) or {}
    up = bool(st.get("port")) and browser_live.is_up(int(st["port"]))
    return {"name": name, "running": up, "port": st.get("port"),
            "display": st.get("display"), "pid": st.get("pid"),
            "egress": st.get("egress") or DEFAULT_EGRESS,
            "profile": st.get("profile"),
            "started_at": st.get("started_at"),
            "last_seen": st.get("last_seen")}


def start(name: str = "default", egress: str = "") -> dict:
    """Bring the console browser up, or return the one already running.

    The profile directory is keyed by name and never deleted, so every login
    survives a restart, an egress change and a reboot of this app.
    """
    if not browser_live.CHROME:
        raise RuntimeError("no Chrome on this host")
    state = _load()
    st = state.get(name) or {}
    egress = egress or st.get("egress") or DEFAULT_EGRESS
    if egress not in EGRESS:
        raise ValueError("unknown egress %r" % egress)

    if st.get("port") and browser_live.is_up(int(st["port"])) and \
            (st.get("egress") or DEFAULT_EGRESS) == egress:
        st["last_seen"] = time.time()
        state[name] = st
        _save(state)
        return status(name)

    if st.get("port"):
        stop(name, keep_profile=True)
        state = _load()

    profile = STATE_ROOT / name / "profile"
    profile.mkdir(parents=True, exist_ok=True)
    display = _free_display()
    xvfb_pid = _start_xvfb(display)
    port = _free_port()

    args = [browser_live.CHROME,
            "--user-data-dir=%s" % profile,
            "--remote-debugging-port=%d" % port,
            "--remote-allow-origins=*",
            "--no-first-run", "--no-default-browser-check",
            "--disable-dev-shm-usage",
            "--disable-features=Translate,OptimizationHints",
            "--window-position=0,0",
            "--window-size=%d,%d" % (WIN_W, WIN_H),
            "--user-agent=%s" % CONSOLE_UA,
            "about:blank"]
    arg = EGRESS[egress]["arg"]
    if arg:
        args.insert(1, arg)
    else:
        # Explicit, not merely absent. Chrome otherwise inherits http_proxy from
        # the environment, and this app's own environment has carried one.
        args.insert(1, "--no-proxy-server")

    env = dict(os.environ, DISPLAY=display)
    env.pop("http_proxy", None)
    env.pop("https_proxy", None)
    log = (STATE_ROOT / name / "chrome.log").open("ab")
    proc = subprocess.Popen(args, stdout=log, stderr=subprocess.STDOUT,
                            env=env, start_new_session=True)
    deadline = time.time() + 30
    while time.time() < deadline:
        if browser_live.is_up(port):
            break
        if proc.poll() is not None:
            raise RuntimeError("Chrome exited immediately; see %s"
                               % (STATE_ROOT / name / "chrome.log"))
        time.sleep(0.4)
    else:
        raise RuntimeError("Chrome did not open CDP on port %d" % port)

    state[name] = {"port": port, "pid": proc.pid, "xvfb_pid": xvfb_pid,
                   "display": display, "profile": str(profile), "egress": egress,
                   "started_at": time.time(), "last_seen": time.time()}
    _save(state)
    return status(name)


def stop(name: str = "default", keep_profile: bool = True) -> dict:
    """Kill this console's Chrome and its X server, and nothing else.

    Every kill is matched on the profile path or on a pid we wrote down
    ourselves, never on a pattern like "chrome": these boxes are shared and a
    broad match has taken out other people's sessions before.
    """
    state = _load()
    st = state.get(name) or {}
    if st.get("port"):
        with contextlib.suppress(Exception):
            browser_live.shutdown(int(st["port"]), st.get("profile") or "",
                                  remove_profile=not keep_profile)
    for key in ("pid", "xvfb_pid"):
        pid = st.get(key)
        if pid and _alive(pid):
            with contextlib.suppress(Exception):
                os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    if not keep_profile and st.get("profile"):
        shutil.rmtree(st["profile"], ignore_errors=True)
    state[name] = {"egress": st.get("egress") or DEFAULT_EGRESS,
                   "profile": st.get("profile")}
    _save(state)
    return {"ok": True, "name": name}


def touch(name: str = "default") -> None:
    state = _load()
    if name in state:
        state[name]["last_seen"] = time.time()
        _save(state)


def park_if_idle(name: str = "default", viewers: int = 0) -> bool:
    """Close the tabs of a browser nobody has watched for a while.

    Not a kill: the profile, and therefore every session cookie, stays. It gives
    back the two cores a headed Chrome holds without costing the login that made
    the browser worth having.
    """
    st = _load().get(name) or {}
    if viewers or not st.get("port"):
        return False
    if time.time() - float(st.get("last_seen") or 0) < IDLE_PARK_S:
        return False
    if not browser_live.is_up(int(st["port"])):
        return False
    open_tabs = browser_live.targets(int(st["port"]))
    if len(open_tabs) <= 1 and (open_tabs and open_tabs[0]["url"] in ("about:blank", "")):
        return False
    _park(int(st["port"]), name)
    return True


def _park(port: int, name: str = "default") -> int:
    """Leave one blank tab and nothing else. Returns how many were closed.

    Cookies live in the profile on disk, not in the tab, so nothing signed in is
    lost. A blank tab is opened only if there is not one already: parking used to
    open one unconditionally and so added a tab every time it ran.

    /json/close answers "Target is closing" and returns before the tab is gone,
    so a listing taken straight afterwards still shows it. Anything checking the
    result has to wait, which is what the settle loop below is for.
    """
    import urllib.request
    probe = (_load().get(name) or {}).get("probe_target")
    rows = [t for t in browser_live.targets(port) if t["id"] != probe]
    keep = next((t["id"] for t in rows if t["url"] in ("about:blank", "")), None)
    if keep is None:
        with contextlib.suppress(Exception):
            keep = _cdp_new_tab(port, "about:blank").get("id")
    doomed = [t["id"] for t in rows if t["id"] != keep]
    for ident in doomed:
        with contextlib.suppress(Exception):
            urllib.request.urlopen("http://127.0.0.1:%d/json/close/%s" % (port, ident),
                                   timeout=4).read()
    deadline = time.time() + 5
    while doomed and time.time() < deadline:
        live = {t["id"] for t in browser_live.targets(port)}
        if not (set(doomed) & live):
            break
        time.sleep(0.3)
    return len(doomed)


# ---------------------------------------------------------------------------
# tabs
# ---------------------------------------------------------------------------
def _cdp_new_tab(port: int, url: str = "about:blank") -> dict:
    """Open a tab through the CDP HTTP endpoint.

    PUT, not POST. Chrome has required PUT on /json/new since 111 and answers a
    POST with 405 Method Not Allowed, which surfaces as a 500 from whatever asked
    for the tab.
    """
    import urllib.parse
    import urllib.request
    req = urllib.request.Request(
        "http://127.0.0.1:%d/json/new?%s" % (port, urllib.parse.quote(url, safe="")),
        method="PUT")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.load(r)


def tabs(name: str = "default", include_probe: bool = False) -> list:
    st = _load().get(name) or {}
    if not st.get("port"):
        return []
    # Asking what is open counts as using it. Without this only a live viewer
    # refreshed the idle timer, so the reaper closed the tabs of an agent that was
    # driving the browser over CDP: the work vanished mid-task with no error.
    touch(name)
    rows = browser_live.targets(int(st["port"]))
    if include_probe:
        return rows
    probe = st.get("probe_target")
    return [t for t in rows if t["id"] != probe]


def _probe_tab(name: str = "default") -> dict:
    """A blank tab kept aside purely for the exit-IP check.

    The check used to run in whatever tab was on screen, and that made it a liar.
    facebook.com serves a Content-Security-Policy that forbids connecting to
    anything outside Meta, so the probe's own fetch was refused by the PAGE and
    reported back as "no internet on this exit" while the browser was in perfect
    health. Warning someone off a working network is worse than not checking.
    `about:blank` carries no policy, so the answer is about the network again.
    """
    state = _load()
    st = state.get(name) or {}
    port = int(st.get("port") or 0)
    if not port:
        return {}
    ident = st.get("probe_target")
    for t in browser_live.targets(port):
        if t["id"] == ident:
            return t
    made = _cdp_new_tab(port, "about:blank")
    st["probe_target"] = made.get("id")
    state[name] = st
    _save(state)
    for t in browser_live.targets(port):
        if t["id"] == made.get("id"):
            return t
    return {}


def new_tab(name: str = "default", url: str = "about:blank") -> dict:
    touch(name)
    st = _load().get(name) or {}
    if not st.get("port"):
        raise RuntimeError("browser is not running")
    return _cdp_new_tab(int(st["port"]), url)


def close_tab(name: str, target_id: str) -> dict:
    import urllib.request
    st = _load().get(name) or {}
    if not st.get("port"):
        raise RuntimeError("browser is not running")
    with contextlib.suppress(Exception):
        urllib.request.urlopen("http://127.0.0.1:%d/json/close/%s"
                               % (int(st["port"]), target_id), timeout=6).read()
    return {"ok": True}


# ---------------------------------------------------------------------------
# "can this egress reach the internet at all"
# ---------------------------------------------------------------------------
async def exit_ip(name: str = "default", timeout: float = 12.0) -> dict:
    """Ask the BROWSER what its exit IP is, not this process.

    The distinction is the whole point. This app talks to the internet directly;
    Chrome may be behind a proxy that died three weeks ago. Asking anything other
    than the browser itself reports the health of the wrong network path, which is
    exactly the mistake that left a dead proxy in place while pages "hung".
    """
    st = _load().get(name) or {}
    if not st.get("port") or not browser_live.is_up(int(st["port"])):
        return {"ok": False, "error": "browser is not running"}
    probe = _probe_tab(name)
    if not probe:
        return {"ok": False, "error": "could not open a tab to ask from"}
    expr = ("fetch('https://api.ipify.org?format=json',{cache:'no-store'})"
            ".then(r=>r.text()).catch(e=>'ERR '+e)")
    try:
        from websockets.asyncio.client import connect as ws_connect
    except Exception:                                        # pragma: no cover
        from websockets.client import connect as ws_connect  # type: ignore
    started = time.time()
    try:
        async with ws_connect(probe["ws"], max_size=None, open_timeout=8) as c:
            await c.send(json.dumps({"id": 1, "method": "Runtime.enable"}))
            await c.send(json.dumps({"id": 2, "method": "Runtime.evaluate",
                                     "params": {"expression": expr, "awaitPromise": True,
                                                "returnByValue": True, "timeout": int(timeout * 1000)}}))
            deadline = time.time() + timeout
            while time.time() < deadline:
                raw = await asyncio.wait_for(c.recv(), timeout=max(1.0, deadline - time.time()))
                msg = json.loads(raw)
                if msg.get("id") != 2:
                    continue
                res = ((msg.get("result") or {}).get("result") or {})
                val = res.get("value")
                ms = int((time.time() - started) * 1000)
                if isinstance(val, str) and val.startswith("{"):
                    with contextlib.suppress(Exception):
                        return {"ok": True, "ip": json.loads(val).get("ip"), "ms": ms,
                                "egress": st.get("egress") or DEFAULT_EGRESS}
                return {"ok": False, "ms": ms, "egress": st.get("egress") or DEFAULT_EGRESS,
                        "error": (val or "no answer from the page")[:200]}
    except Exception as e:
        return {"ok": False, "egress": st.get("egress") or DEFAULT_EGRESS,
                "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    return {"ok": False, "egress": st.get("egress") or DEFAULT_EGRESS,
            "error": "timed out after %.0fs, which is what a dead proxy looks like" % timeout}


# ---------------------------------------------------------------------------
# the interactive half
# ---------------------------------------------------------------------------
# Keys that carry no text. Anything not listed is treated as a printable
# character and sent with `text`, which is what makes ordinary typing work.
_VK = {
    "Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27, "Delete": 46,
    "ArrowDown": 40, "ArrowUp": 38, "ArrowLeft": 37, "ArrowRight": 39,
    "Home": 36, "End": 35, "PageUp": 33, "PageDown": 34,
    "Shift": 16, "Control": 17, "Alt": 18, "Meta": 91, "CapsLock": 20,
    "F1": 112, "F2": 113, "F3": 114, "F4": 115, "F5": 116, "F6": 117,
    "F7": 118, "F8": 119, "F9": 120, "F10": 121, "F11": 122, "F12": 123,
}

# Punctuation, on a US layout. `ord(ch)` is NOT the virtual key code and using it
# silently eats characters: ord(".") is 46, which is Delete, so an email address
# typed into the viewer arrived as "nimo@testcom" with the dot missing and no
# error anywhere. Measured against the real Facebook login form.
_PUNCT_VK = {
    " ": 32, ";": 186, ":": 186, "=": 187, "+": 187, ",": 188, "<": 188,
    "-": 189, "_": 189, ".": 190, ">": 190, "/": 191, "?": 191,
    "`": 192, "~": 192, "[": 219, "{": 219, "\\": 220, "|": 220,
    "]": 221, "}": 221, "'": 222, '"': 222,
    "!": 49, "@": 50, "#": 51, "$": 52, "%": 53, "^": 54, "&": 55,
    "*": 56, "(": 57, ")": 48,
}


def _printable_vk(ch: str) -> int:
    """The US-layout virtual key code for one printable character, or 0.

    0 is a safe answer: with `text` set, Chrome inserts the character anyway, and
    a WRONG code is far worse than none because it fires some other key's
    behaviour."""
    if ch.isalpha() and ch.isascii():
        return ord(ch.upper())
    if ch.isdigit():
        return ord(ch)
    return _PUNCT_VK.get(ch, 0)


class InteractiveCast(browser_live.Screencast):
    """`Screencast` plus the events a person needs.

    The parent sends a click as press-then-release in one go, which is right for
    an agent and wrong for a human: with no separate press, release and move you
    cannot drag, you cannot select text, and a native menu that opens on
    mousedown closes again before you see it. Modifiers are carried too, so
    shift-click and ctrl-A behave.
    """

    def __init__(self, ws_url: str, quality: int = 55, fps: float = 5.0,
                 max_width: int = 1152, max_height: int = 720, active_fps: float = 20.0):
        super().__init__(ws_url, quality=quality, max_width=max_width,
                         max_height=max_height, every_nth=1, fps=fps)
        self._fast = 1.0 / max(1.0, min(float(active_fps), 30.0))
        self._active_until = 0.0

    # Pace by what the person is doing, not by a constant.
    #
    # Measured on this box before this existed, at a flat 15 fps: sitting on
    # facebook.com with nobody touching it cost 107 KB/s, and business.facebook.com
    # cost 542 KB/s. Neither page is static; both animate for ever, so frame
    # de-duplication alone never fires and an untouched viewer streams video of a
    # spinner. Now the fast rate is spent only in the ~2.5s after a real input,
    # where responsiveness is the whole product, and an unattended viewer drops to
    # a rate that costs a fraction of that.
    @property
    def _interval(self) -> float:
        return self._fast if time.time() < self._active_until else self._slow

    @_interval.setter
    def _interval(self, value: float) -> None:
        # The parent sets this from `fps` in __init__; that becomes the IDLE rate.
        self._slow = value

    def _poke(self) -> None:
        self._active_until = time.time() + 2.5

    async def mouse(self, kind: str, nx: float, ny: float, button: str = "left",
                    buttons: int = 0, clicks: int = 1, modifiers: int = 0,
                    delta_y: float = 0.0, delta_x: float = 0.0):
        self._poke()
        x, y = self._to_page(nx, ny)
        params = {"type": kind, "x": x, "y": y, "modifiers": int(modifiers),
                  "button": button, "buttons": int(buttons)}
        if kind in ("mousePressed", "mouseReleased"):
            params["clickCount"] = max(1, min(int(clicks), 3))
        if kind == "mouseWheel":
            params["deltaX"] = float(delta_x)
            params["deltaY"] = float(delta_y)
        await self._send("Input.dispatchMouseEvent", params)

    async def key_event(self, kind: str, key: str, code: str = "", text: str = "",
                        modifiers: int = 0, repeat: bool = False):
        self._poke()
        vk = _VK.get(key)
        printable = vk is None and len(key) == 1
        if vk is None:
            vk = _printable_vk(key) if printable else 0
        params = {"type": kind, "key": key, "code": code or key,
                  "modifiers": int(modifiers), "autoRepeat": bool(repeat),
                  "windowsVirtualKeyCode": vk, "nativeVirtualKeyCode": vk}
        # A keyDown with no `text` produces the event but not the character, so a
        # password typed into the viewer arrives as an empty field. Ctrl and Meta
        # are the exception: there `text` would insert the letter as well as
        # firing the shortcut, so ctrl-A would select all and then type an "a".
        if kind == "keyDown" and printable and not (int(modifiers) & 0b0110):
            params["text"] = text or key
        elif kind == "keyDown":
            # No text on this one, so it is a rawKeyDown. Sending it as "keyDown"
            # with no text makes Chrome synthesise an empty char event that some
            # forms treat as input.
            params["type"] = "rawKeyDown"
        await self._send("Input.dispatchKeyEvent", params)

    async def insert_text(self, value: str):
        self._poke()
        await self._send("Input.insertText", {"text": value[:20000]})

    async def reload(self):
        self._poke()
        await self._send("Page.reload", {"ignoreCache": False})

    async def history(self, delta: int):
        """Back and forward.

        Deliberately `history.go()` rather than Page.getNavigationHistory: that
        call needs its REPLY, and the only reader of this socket is the frame
        pump. Waiting for it here steals frames out of the pump and freezes the
        viewer on whatever was last painted.
        """
        await self._send("Runtime.evaluate",
                         {"expression": "history.go(%d)" % int(delta)})


# ---------------------------------------------------------------------------
# the bridge between one viewer and one tab
# ---------------------------------------------------------------------------
VIEWERS: dict = {}


# What the viewer can ask for. `balanced` is the default: 1152 wide is a 20 percent
# downscale of the 1440 the page is actually laid out at, which is invisible on a
# form and roughly halves the bytes.
PRESETS = {
    "sharp": {"label": "Sharp", "quality": 72, "w": 1440, "h": 900, "fps": 6.0},
    "balanced": {"label": "Balanced", "quality": 55, "w": 1152, "h": 720, "fps": 5.0},
    "light": {"label": "Data saver", "quality": 38, "w": 960, "h": 600, "fps": 3.0},
}
DEFAULT_PRESET = "balanced"


async def serve(client_ws, ws_url: str, name: str = "default",
                preset: str = DEFAULT_PRESET) -> dict:
    """One websocket carries frames out and every input event back."""
    p = PRESETS.get(preset) or PRESETS[DEFAULT_PRESET]
    VIEWERS[name] = VIEWERS.get(name, 0) + 1
    touch(name)
    try:
        async with InteractiveCast(ws_url, quality=p["quality"], fps=p["fps"],
                                   max_width=p["w"], max_height=p["h"]) as cast:

            async def on_frame(data, meta):
                await client_ws.send_text(json.dumps(
                    {"t": "f", "d": data, "w": meta.get("deviceWidth"),
                     "h": meta.get("deviceHeight")}))

            async def on_event(method, params):
                url = ((params.get("frame") or {}).get("url") or "")
                if url:
                    await client_ws.send_text(json.dumps({"t": "nav", "url": url[:400]}))

            async def from_client():
                while True:
                    raw = await client_ws.receive_text()
                    touch(name)
                    try:
                        m = json.loads(raw)
                    except Exception:
                        continue
                    k = m.get("t")
                    if k == "m":
                        await cast.mouse(m.get("k", "mouseMoved"), m.get("x", 0), m.get("y", 0),
                                         button=m.get("b", "left"), buttons=m.get("bs", 0),
                                         clicks=m.get("c", 1), modifiers=m.get("mod", 0),
                                         delta_x=m.get("dx", 0), delta_y=m.get("dy", 0))
                    elif k == "k":
                        await cast.key_event(m.get("k", "keyDown"), m.get("key", ""),
                                             code=m.get("code", ""), text=m.get("text", ""),
                                             modifiers=m.get("mod", 0), repeat=m.get("rep", False))
                    elif k == "paste":
                        await cast.insert_text(str(m.get("v", "")))
                    elif k == "nav":
                        cast._poke()
                        await cast.navigate(str(m.get("v", "")))
                    elif k == "reload":
                        await cast.reload()
                    elif k == "hist":
                        await cast.history(int(m.get("v", -1)))
                    elif k == "ping":
                        await client_ws.send_text(json.dumps({"t": "pong",
                                                              "s": cast.stats}))

            pump = asyncio.create_task(cast.pump(on_frame, on_event))
            reader = asyncio.create_task(from_client())
            done, pending = await asyncio.wait({pump, reader},
                                               return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for task in done:
                with contextlib.suppress(Exception):
                    task.result()
            return cast.stats
    finally:
        VIEWERS[name] = max(0, VIEWERS.get(name, 1) - 1)
        touch(name)


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
QUICK_LINKS = [
    ("Business settings", "https://business.facebook.com/settings/security?business_id=512732349540925"),
    ("Meta Business Suite", "https://business.facebook.com/"),
    ("Facebook", "https://www.facebook.com/"),
    ("Google account", "https://myaccount.google.com/"),
    ("Twilio", "https://console.twilio.com/"),
]

PAGE = """<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5">
<title>Console browser</title>
<style>
 :root{--bg:#101214;--fg:#e9eaec;--dim:#8e959d;--line:#262b30;--card:#171a1d;
       --ok:#3fb27f;--bad:#e0685a;--warn:#d9a441;--accent:#4c8dff}
 *{box-sizing:border-box}
 body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 -apple-system,
      BlinkMacSystemFont,"Segoe UI",Helvetica,Arial,sans-serif}
 .bar{display:flex;gap:6px;align-items:center;padding:7px 9px;background:var(--card);
      border-bottom:1px solid var(--line);flex-wrap:wrap}
 button,select{background:#22262a;color:var(--fg);border:1px solid var(--line);
      border-radius:6px;padding:5px 9px;font:inherit;cursor:pointer}
 button:hover{background:#2c3237}
 button:disabled{opacity:.45;cursor:default}
 #url{flex:1 1 260px;min-width:150px;background:#0b0d0f;color:var(--fg);
      border:1px solid var(--line);border-radius:6px;padding:6px 9px;font:inherit}
 .pill{font-size:11px;padding:3px 8px;border-radius:99px;border:1px solid var(--line);
       color:var(--dim);white-space:nowrap}
 .pill.ok{color:var(--ok);border-color:#255f45}
 .pill.bad{color:var(--bad);border-color:#5f2b25}
 .pill.warn{color:var(--warn);border-color:#5f4a1c}
 .links{display:flex;gap:6px;padding:6px 9px;flex-wrap:wrap;border-bottom:1px solid var(--line)}
 .links a{color:var(--dim);text-decoration:none;font-size:12px;border:1px solid var(--line);
      border-radius:99px;padding:3px 10px}
 .links a:hover{color:var(--fg);border-color:#3a4249}
 #stage{position:relative;background:#000;margin:0 auto;max-width:1440px}
 #screen{display:block;width:100%;height:auto;image-rendering:auto;
      -webkit-user-select:none;user-select:none;-webkit-touch-callout:none}
 #screen.armed{outline:2px solid var(--accent);outline-offset:-2px}
 #veil{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
      background:rgba(8,10,12,.86);color:var(--dim);text-align:center;padding:24px;
      font-size:14px;line-height:1.6}
 #veil b{color:var(--fg);display:block;margin-bottom:6px;font-size:15px}
 #veil.hide{display:none}
 #kb{position:absolute;opacity:0;pointer-events:none;width:1px;height:1px;border:0}
 .foot{display:flex;gap:12px;padding:6px 10px;color:var(--dim);font-size:11px;flex-wrap:wrap}
 code{background:#0b0d0f;border:1px solid var(--line);border-radius:4px;padding:1px 5px}
</style></head><body>

<div class="bar">
  <button id="back" title="Back">&#8592;</button>
  <button id="fwd" title="Forward">&#8594;</button>
  <button id="rl" title="Reload">&#8635;</button>
  <input id="url" placeholder="type an address and press Enter" spellcheck="false"
         autocapitalize="off" autocorrect="off">
  <select id="tab" title="Tab"></select>
  <button id="newtab" title="New tab">+</button>
  <button id="closetab" title="Close this tab">&#215;</button>
  <select id="egress" title="Where this browser's traffic leaves from"></select>
  <select id="qual" title="Picture quality against bandwidth"></select>
  <span id="ip" class="pill">exit unknown</span>
  <span id="fps" class="pill">idle</span>
  <button id="power">Stop</button>
</div>

<div class="links" id="links"></div>

<div id="stage">
  <img id="screen" alt="remote browser">
  <textarea id="kb" autocapitalize="off" autocorrect="off" spellcheck="false"></textarea>
  <div id="veil"><div><b>Starting the browser</b>one moment</div></div>
</div>

<div class="foot">
  <span>Click the picture once to take the keyboard. Paste works with <code>ctrl-V</code>.</span>
  <span id="note"></span>
</div>

<script>
const BASE = __BASE__;
const WSU = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + BASE + "/ws";
const S = document.getElementById("screen"), veil = document.getElementById("veil");
const kb = document.getElementById("kb"), urlIn = document.getElementById("url");
let ws = null, armed = false, tabId = "", frames = 0, bytes = 0, lastNav = "";

function say(html, keep){ veil.innerHTML = "<div>" + html + "</div>"; veil.classList.remove("hide");
  if (!keep) setTimeout(()=>{ if (frames) veil.classList.add("hide"); }, 400); }
function api(p, o){ return fetch(BASE + "/api" + p, o).then(r => r.json()); }

// ---- modifiers, CDP's bitmask: alt 1, ctrl 2, meta 4, shift 8
function mods(e){ return (e.altKey?1:0)|(e.ctrlKey?2:0)|(e.metaKey?4:0)|(e.shiftKey?8:0); }
function send(o){ if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
function pt(e){ const r = S.getBoundingClientRect();
  const t = (e.touches && e.touches[0]) || (e.changedTouches && e.changedTouches[0]) || e;
  return { x: (t.clientX - r.left) / r.width, y: (t.clientY - r.top) / r.height }; }

// ---- pointer. Press and release travel separately or you cannot drag or select.
const BTN = ["left","middle","right"];
let down = false;
S.addEventListener("mousemove", e => { const p = pt(e);
  send({t:"m", k:"mouseMoved", x:p.x, y:p.y, b: down?"left":"none", bs: down?1:0, mod: mods(e)}); });
S.addEventListener("mousedown", e => { e.preventDefault(); arm(); down = true; const p = pt(e);
  send({t:"m", k:"mousePressed", x:p.x, y:p.y, b:BTN[e.button]||"left", bs:1,
        c:e.detail||1, mod: mods(e)}); });
window.addEventListener("mouseup", e => { if (!down && e.target !== S) return; down = false;
  const p = pt(e); send({t:"m", k:"mouseReleased", x:p.x, y:p.y, b:BTN[e.button]||"left",
        bs:0, c:e.detail||1, mod: mods(e)}); });
S.addEventListener("contextmenu", e => e.preventDefault());
S.addEventListener("wheel", e => { e.preventDefault(); const p = pt(e);
  send({t:"m", k:"mouseWheel", x:p.x, y:p.y, dx:-e.deltaX, dy:-e.deltaY, mod: mods(e)});
}, {passive:false});

// ---- touch, so this is usable from a phone
S.addEventListener("touchstart", e => { arm(); const p = pt(e);
  send({t:"m", k:"mouseMoved", x:p.x, y:p.y, b:"none", bs:0});
  send({t:"m", k:"mousePressed", x:p.x, y:p.y, b:"left", bs:1, c:1}); }, {passive:true});
S.addEventListener("touchend", e => { const p = pt(e);
  send({t:"m", k:"mouseReleased", x:p.x, y:p.y, b:"left", bs:0, c:1}); }, {passive:true});

// ---- keyboard. The hidden textarea is what makes a phone keyboard appear at all.
function arm(){ if (armed) return; armed = true; S.classList.add("armed");
  try { kb.focus({preventScroll:true}); } catch(_) { kb.focus(); } }
function disarm(){ armed = false; S.classList.remove("armed"); }
kb.addEventListener("blur", disarm);
document.addEventListener("keydown", e => {
  if (!armed || e.target === urlIn) return;
  if (e.key === "v" && (e.ctrlKey || e.metaKey)) return;      // let the paste event fire
  e.preventDefault();
  send({t:"k", k:"keyDown", key:e.key, code:e.code, text:e.key.length===1?e.key:"",
        mod: mods(e), rep: e.repeat});
});
document.addEventListener("keyup", e => { if (!armed || e.target === urlIn) return;
  e.preventDefault(); send({t:"k", k:"keyUp", key:e.key, code:e.code, mod: mods(e)}); });
document.addEventListener("paste", e => { if (!armed) return; e.preventDefault();
  const v = (e.clipboardData || window.clipboardData).getData("text");
  if (v) send({t:"paste", v:v}); });

// ---- controls
document.getElementById("back").onclick = () => send({t:"hist", v:-1});
document.getElementById("fwd").onclick  = () => send({t:"hist", v:1});
document.getElementById("rl").onclick   = () => send({t:"reload"});
urlIn.addEventListener("keydown", e => { if (e.key !== "Enter") return;
  let v = urlIn.value.trim(); if (!v) return;
  if (!/^[a-z]+:\\/\\//i.test(v)) v = (v.indexOf(" ") < 0 && v.indexOf(".") > 0)
      ? "https://" + v : "https://www.google.com/search?q=" + encodeURIComponent(v);
  send({t:"nav", v:v}); });

document.getElementById("newtab").onclick = () =>
  api("/tab/new", {method:"POST"}).then(() => refresh(true));
document.getElementById("closetab").onclick = () => { if (!tabId) return;
  api("/tab/close?target=" + encodeURIComponent(tabId), {method:"POST"})
    .then(() => { tabId = ""; refresh(true); }); };
document.getElementById("tab").onchange = e => { tabId = e.target.value; connect(); };
document.getElementById("power").onclick = () => {
  const running = document.getElementById("power").textContent === "Stop";
  say("<b>" + (running ? "Stopping" : "Starting") + "</b>one moment", true);
  api(running ? "/stop" : "/start", {method:"POST"}).then(() => refresh(true)); };
document.getElementById("egress").onchange = e => {
  say("<b>Restarting on " + e.target.value + "</b>your logins are kept", true);
  api("/egress?v=" + encodeURIComponent(e.target.value), {method:"POST"})
    .then(() => { tabId = ""; refresh(true); }); };

// ---- the stream
function preset(){ return document.getElementById("qual").value || "balanced"; }
document.getElementById("qual").onchange = () => {
  try { localStorage.setItem("console.q", preset()); } catch(_){}
  connect(); };

function connect(){
  if (ws) { try { ws.close(); } catch(_){} ws = null; }
  if (!tabId) return;
  frames = 0;
  ws = new WebSocket(WSU + "?tab=" + encodeURIComponent(tabId) + "&q=" + preset());
  ws.onmessage = ev => { const m = JSON.parse(ev.data);
    if (m.t === "f") { frames++; bytes += m.d.length * 0.75;
      S.src = "data:image/jpeg;base64," + m.d; veil.classList.add("hide"); }
    else if (m.t === "nav") { lastNav = m.url;
      if (document.activeElement !== urlIn) urlIn.value = m.url; } };
  ws.onclose = () => { if (frames) say("<b>Stream ended</b>reconnecting", true);
    setTimeout(() => { if (tabId) connect(); }, 1500); };
  ws.onerror = () => {};
}

let lastF = 0, lastT = Date.now();
setInterval(() => { const now = Date.now();
  const f = Math.round((frames - lastF) * 1000 / Math.max(1, now - lastT));
  lastF = frames; lastT = now;
  document.getElementById("fps").textContent =
    frames ? (f + " fps  " + Math.round(bytes/1024) + " KB") : "idle";
}, 2000);

// ---- state
function refresh(hard){
  return api("/state").then(d => {
    const pw = document.getElementById("power");
    pw.textContent = d.running ? "Stop" : "Start";
    const eg = document.getElementById("egress");
    if (!eg.options.length) { (d.egress_options||[]).forEach(o => {
        const op = document.createElement("option"); op.value = o.id;
        op.textContent = o.label; op.title = o.note; eg.appendChild(op); }); }
    eg.value = d.egress;
    const ql = document.getElementById("qual");
    if (!ql.options.length) { (d.quality_options||[]).forEach(o => {
        const op = document.createElement("option"); op.value = o.id;
        op.textContent = o.label; ql.appendChild(op); });
      let saved = null; try { saved = localStorage.getItem("console.q"); } catch(_){}
      ql.value = saved || d.quality_default; }
    const sel = document.getElementById("tab");
    const want = (d.tabs||[]).map(t => t.id).join(",");
    if (want !== sel.dataset.ids || hard) { sel.dataset.ids = want; sel.innerHTML = "";
      (d.tabs||[]).forEach(t => { const o = document.createElement("option");
        o.value = t.id; o.textContent = (t.title || t.url || "tab").slice(0, 40);
        sel.appendChild(o); }); }
    if (!d.running) { tabId = ""; if (ws) { ws.close(); ws = null; }
      say("<b>The browser is stopped</b>press Start", true); return d; }
    if (!tabId && d.tabs && d.tabs.length) { tabId = d.tabs[0].id; }
    if (tabId) { sel.value = tabId; if (!ws || hard) connect(); }
    return d;
  });
}
function checkIp(){
  const el = document.getElementById("ip");
  el.className = "pill"; el.textContent = "checking exit";
  api("/exit-ip").then(d => {
    if (d.ok) { el.className = "pill ok"; el.textContent = d.ip + "  " + d.ms + " ms";
      document.getElementById("note").textContent = ""; }
    else { el.className = "pill bad"; el.textContent = "no internet on this exit";
      document.getElementById("note").textContent = d.error || ""; } });
}
(function links(){ const box = document.getElementById("links");
  __LINKS__.forEach(([t,u]) => { const a = document.createElement("a");
    a.textContent = t; a.href = "#"; a.onclick = e => { e.preventDefault();
      urlIn.value = u; send({t:"nav", v:u}); }; box.appendChild(a); }); })();

api("/start", {method:"POST"}).then(() => refresh(true)).then(checkIp)
  .catch(() => say("<b>Could not start the browser</b>check the app log", true));
setInterval(refresh, 6000);
setInterval(checkIp, 60000);
</script></body></html>"""


def page_html(root_path: str = "") -> str:
    """Substitution by replace(), not by %, because the page is mostly CSS and
    every `width:100%` in it is a format specifier to the other one."""
    return (PAGE.replace("__BASE__", json.dumps((root_path or "") + "/console"))
                .replace("__LINKS__", json.dumps(QUICK_LINKS)))


# ---------------------------------------------------------------------------
# mount
# ---------------------------------------------------------------------------
def _reaper() -> None:
    """Park a browser nobody is watching.

    A daemon thread rather than a startup task on purpose: the dashboard already
    owns its lifespan, and the reaper must keep running whether or not anybody
    ever opens the page. Without it an abandoned headed Chrome holds two cores on
    a shared box for as long as the app is up, which is how this fleet has lost
    boxes before.
    """
    while True:
        time.sleep(120)
        with contextlib.suppress(Exception):
            park_if_idle("default", VIEWERS.get("default", 0))


def mount(app, *, auth_ok=None) -> None:
    """Register the console browser on the dashboard app.

    `auth_ok(websocket) -> bool` is passed in because HTTP middleware does not
    run for websockets: every other route on this app is gated by the dashboard's
    auth middleware, and a stream of a fully signed-in Chrome is the last thing
    that should be the exception.
    """
    import threading
    threading.Thread(target=_reaper, name="console-browser-reaper", daemon=True).start()

    # `Request` and `WebSocket` are imported at module scope on purpose. This file
    # uses `from __future__ import annotations`, so FastAPI resolves every handler
    # annotation as a STRING against the module globals: a name imported inside
    # this function is invisible there, and `request: Request` silently becomes a
    # required query parameter. The page then 422s instead of rendering.
    @app.get("/console")
    async def console_page(request: Request):
        return HTMLResponse(page_html(request.scope.get("root_path", "")))

    @app.get("/console/api/state")
    async def console_state():
        st = status()
        st["tabs"] = tabs() if st["running"] else []
        st["viewers"] = VIEWERS.get("default", 0)
        st["egress_options"] = [{"id": k, "label": v["label"], "note": v["note"]}
                                for k, v in EGRESS.items()]
        st["quality_options"] = [{"id": k, "label": v["label"]} for k, v in PRESETS.items()]
        st["quality_default"] = DEFAULT_PRESET
        return JSONResponse(st)

    @app.post("/console/api/start")
    async def console_start():
        try:
            return JSONResponse(await asyncio.to_thread(start))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=500)

    @app.post("/console/api/stop")
    async def console_stop():
        return JSONResponse(await asyncio.to_thread(stop))

    @app.post("/console/api/egress")
    async def console_egress(v: str = DEFAULT_EGRESS):
        try:
            return JSONResponse(await asyncio.to_thread(start, "default", v))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=400)

    @app.post("/console/api/tab/new")
    async def console_new_tab(url: str = "about:blank"):
        try:
            return JSONResponse(await asyncio.to_thread(new_tab, "default", url))
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=400)

    @app.post("/console/api/tab/close")
    async def console_close_tab(target: str):
        return JSONResponse(await asyncio.to_thread(close_tab, "default", target))

    @app.get("/console/api/exit-ip")
    async def console_exit_ip():
        return JSONResponse(await exit_ip())

    @app.websocket("/console/ws")
    async def console_ws(ws: WebSocket, tab: str = "", q: str = DEFAULT_PRESET):
        if auth_ok is not None and not auth_ok(ws):
            await ws.close(code=1008)
            return
        rows = [t for t in tabs() if t["id"] == tab] or tabs()
        if not rows:
            await ws.close(code=1011)
            return
        await ws.accept()
        try:
            await serve(ws, rows[0]["ws"], preset=q)
        except Exception:
            with contextlib.suppress(Exception):
                await ws.close()
