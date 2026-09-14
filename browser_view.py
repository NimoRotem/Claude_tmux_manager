"""The lite live view: watch and drive any browser on the Browser tab without VNC.

WHY (2026-09-13). Nimo: *"novnc is very slow and heavy and copy/paste doesn't
work ... it must be light. but keeping the escalation ladder including if needed
residential proxies and real headed browser."*

noVNC costs three processes per watched browser (x11vnc polling a 1920x1080
framebuffer with no XDamage, websockify, the dashboard's own websocket proxy)
and pays for the whole desktop whether or not anything moved. Its clipboard is a
Latin-1 side panel.

This keeps everything the ladder depends on and changes only how a person sees
and touches it:

  * The browser is the SAME one: the headed Chrome on its Xvfb display, behind
    its own residential relay port. Nothing is relaunched, nothing about its
    fingerprint or its exit changes, and an agent driving it carries on.
  * Pixels come from `Page.startScreencast`: Chrome encodes the page viewport
    itself and sends a JPEG only when it changed, paced by what the viewer is
    doing (fast for a moment after input, slow otherwise). An idle page costs
    nothing on the wire. "Whole screen" mode grabs the X screen instead, for the
    few things that are not in the page: the tab strip, a native dialog, an open
    <select> menu.
  * Input is real X input through XTEST, the same way x11vnc delivers it, via
    `browser_view_x.py`. Only when a browser has no reachable display does it
    fall back to CDP input.
  * Copy and paste use the X clipboard, so ctrl-C in the viewer lands in your
    own clipboard and ctrl-V fires a genuine paste event in the page, any script.

The debugger session this opens enables the Page domain only. `Runtime.enable`
is the attach that challenge scripts can observe (console serialisation), and
nothing here needs it.

Frames go out as binary websocket messages: base64 in JSON was a third larger for
nothing.
"""
import asyncio
import base64
import contextlib
import html
import json
import re
import subprocess
import sys
import time
from pathlib import Path

from fastapi import Request, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse

import browser_live
import console_browser

try:                                             # websockets >= 14
    from websockets.asyncio.client import connect as ws_connect
except Exception:                                # pragma: no cover
    from websockets.client import connect as ws_connect  # type: ignore

HELPER = Path(__file__).resolve().with_name("browser_view_x.py")

# Keys the helper presses and releases by name. Everything else one character long
# is typed as a character. Kept in step with browser_view_x.NAMED_KEYS by a test.
NAMED_KEYS = frozenset([
    "Enter", "Backspace", "Tab", "Escape", "Delete", "Insert", "Home", "End",
    "PageUp", "PageDown", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown",
    "Shift", "Control", "Alt", "Meta", "CapsLock", "ContextMenu",
    *("F%d" % n for n in range(1, 13)),
])
MOUSE_BUTTONS = {"left": 1, "middle": 2, "right": 3}

# `fps` is the idle ceiling, `active` the rate for 2.5s after an input. A changed
# picture is the only thing ever sent, so on a still page both cost nothing.
PRESETS = {
    "sharp": {"label": "Sharp", "quality": 70, "w": 1440, "h": 900, "fps": 2.0, "active": 12.0},
    "balanced": {"label": "Balanced", "quality": 55, "w": 1152, "h": 720, "fps": 1.0, "active": 10.0},
    "light": {"label": "Data saver", "quality": 40, "w": 960, "h": 600, "fps": 0.5, "active": 5.0},
}
DEFAULT_PRESET = "balanced"

# Whole-screen mode polls the X screen, so it is paced much slower than the
# screencast: a grab of an unchanged 1920x1080 screen is a copy and a hash.
SCREEN_IDLE_S, SCREEN_ACTIVE_S = 1.0, 0.3

SELECTION_JS = ("(()=>{const a=document.activeElement;"
                "if(a&&typeof a.selectionStart==='number'&&a.selectionEnd>a.selectionStart)"
                "{return String(a.value).slice(a.selectionStart,a.selectionEnd)}"
                "return String(getSelection())})()")

VIEWERS: dict = {}


# ---------------------------------------------------------------------------
# which X display a browser really paints on
# ---------------------------------------------------------------------------
def display_for_port(port: int, fallback="") -> str:
    """The X display the browser listening on this CDP port really paints on.

    Not from the registry: that says what a slot was created with, and a browser
    restarted by hand or by another tool can be on another screen. XTEST input
    sent to the wrong screen goes nowhere with no error at all.

    Not from /proc/<pid>/environ either, which was the first attempt. Chrome
    overwrites its own environment block to set its process title: measured
    2026-09-13, all three Chromes on builder2 showed no DISPLAY there at all.

    So from the socket: the browser process holds a unix connection to its X
    server, whose end is bound to /tmp/.X11-unix/X<n>. The registry value is only
    the fallback for when the listening process cannot be seen at all."""
    with contextlib.suppress(Exception):
        pids = _listening_pids(port)
        if pids:
            # A browser we can see with no X connection is headless. Its input must
            # go through CDP: the registry's display has no window of it.
            return _x_display_of(pids)
    fb = str(fallback or "")
    if fb and not fb.startswith(":"):
        fb = ":" + fb
    return fb


def _x_display_of(pids: set) -> str:
    rows = subprocess.run(["ss", "-xpH"], capture_output=True, text=True, timeout=6).stdout
    mine, servers = set(), {}
    for line in rows.splitlines():
        m = re.match(r"\S+\s+\S+\s+\d+\s+\d+\s+(\S+)\s+(\d+)\s+(\S+)\s+(\d+)", line)
        if not m:
            continue
        local, inode, _peer, peer_inode = m.groups()
        x = re.search(r"/tmp/\.X11-unix/X(\d+)$", local)
        if x:
            servers[peer_inode] = ":" + x.group(1)
        elif any(("pid=%s," % pid) in line for pid in pids):
            mine.add(inode)
    for inode in mine:
        if inode in servers:
            return servers[inode]
    return ""


def _listening_pids(port: int) -> set:
    out = subprocess.run(["ss", "-ltnpH", "sport = :%d" % int(port)],
                         capture_output=True, text=True, timeout=4).stdout
    return set(re.findall(r"pid=(\d+)", out))


def proxy_for_port(port: int) -> str:
    """The --proxy-server the browser on this CDP port was really launched with,
    read from its command line. "" for a browser that goes out direct.

    Searched, not split on NULs: Chrome rewrites its argv area into one
    space-separated process title, so the arguments are no longer separate
    strings there (measured: split, every browser on builder2 read as direct)."""
    with contextlib.suppress(Exception):
        for pid in _listening_pids(port):
            raw = Path("/proc/%s/cmdline" % pid).read_bytes().replace(b"\0", b" ")
            m = re.search(rb"(?:^|\s)--proxy-server=(\S+)", raw)
            return m.group(1).decode() if m else ""
    return ""


def egress(port: int, conf: dict) -> dict:
    """What the viewer says about where this browser's traffic leaves from.

    The relay does NOT send everything through the residential exit: only the
    hosts in its `only` list take the paid ladder, the rest go direct. Saying
    "residential" without that sentence is how a working relay has been read as a
    dead one before, so the note always carries it."""
    proxy = proxy_for_port(port)
    if not proxy:
        return {"egress_label": "direct", "egress_kind": "direct",
                "egress_note": "No proxy: this box's own IP for every site."}
    m = re.search(r"127\.0\.0\.1:(\d+)", proxy)
    local = int(m.group(1)) if m else 0
    conf = conf or {}
    rungs = [str(r) for r in (conf.get("ladder") or [])]
    only = [str(h) for h in (conf.get("only") or [])]
    if not m:
        return {"egress_label": "proxy", "egress_kind": "relay", "egress_note": proxy}
    note = "Residential relay on :%d" % local
    if not conf.get("enabled", True):
        note += ", switched OFF in the relay config, so everything goes direct"
    elif rungs:
        note += ", ladder " + " > ".join(rungs)
    if only:
        note += (". Only %d listed sites use it (%s...); every other site goes direct from "
                 "this box." % (len(only), ", ".join(only[:5])))
    return {"egress_label": "relay :%d" % local, "egress_kind": "relay", "egress_note": note}


def _display_exists(display: str) -> bool:
    return bool(display) and Path("/tmp/.X11-unix/X%s" % display.lstrip(":").split(".")[0]).exists()


# ---------------------------------------------------------------------------
# the helper process, one per display
# ---------------------------------------------------------------------------
class XHelper:
    """Talks to browser_view_x.py. Shared by every viewer of one display and
    ended when the last of them leaves: a helper nobody is using is ours to stop."""

    _pool: dict = {}
    _locks: dict = {}

    def __init__(self, display: str):
        self.display = display
        self.proc = None
        self.refs = 0
        self.alive = False
        self.w = self.h = 0
        self._n = 0
        self._waiters: dict = {}
        self._reader = None

    @classmethod
    async def acquire(cls, display: str):
        if not _display_exists(display) or not HELPER.exists():
            return None
        lock = cls._locks.setdefault(display, asyncio.Lock())
        async with lock:
            helper = cls._pool.get(display)
            if helper is None or not helper.alive:
                helper = cls(display)
                if not await helper._start():
                    return None
                cls._pool[display] = helper
            helper.refs += 1
            return helper

    async def release(self) -> None:
        self.refs -= 1
        if self.refs > 0:
            return
        if self._pool.get(self.display) is self:
            self._pool.pop(self.display, None)
        await self.close()

    async def _start(self) -> bool:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                sys.executable, str(HELPER), self.display,
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=16 << 20)
            line = await asyncio.wait_for(self.proc.stdout.readline(), 10)
            ready = json.loads(line or b"{}")
        except Exception:                                          # noqa: BLE001
            await self.close()
            return False
        if not ready.get("ready") or not ready.get("xtest"):
            await self.close()
            return False
        self.w, self.h = int(ready.get("w") or 0), int(ready.get("h") or 0)
        self.alive = True
        self._reader = asyncio.create_task(self._read())
        return True

    async def _read(self) -> None:
        try:
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    break
                with contextlib.suppress(Exception):
                    msg = json.loads(line)
                    fut = self._waiters.pop(msg.get("id"), None)
                    if fut is not None and not fut.done():
                        fut.set_result(msg)
        finally:
            self.alive = False
            for fut in self._waiters.values():
                if not fut.done():
                    fut.set_result({"ok": False, "error": "helper exited"})
            self._waiters.clear()

    def send(self, op: str, **kw) -> bool:
        if not self.alive or self.proc is None or self.proc.stdin is None:
            return False
        kw["op"] = op
        try:
            self.proc.stdin.write((json.dumps(kw) + "\n").encode())
            return True
        except Exception:                                          # noqa: BLE001
            self.alive = False
            return False

    async def call(self, op: str, timeout: float = 4.0, **kw) -> dict:
        self._n += 1
        ident = self._n
        fut = asyncio.get_running_loop().create_future()
        self._waiters[ident] = fut
        if not self.send(op, id=ident, **kw):
            self._waiters.pop(ident, None)
            return {"ok": False, "error": "helper is not running"}
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": "helper did not answer"}
        finally:
            self._waiters.pop(ident, None)

    async def close(self) -> None:
        self.alive = False
        proc, self.proc = self.proc, None
        if proc is None:
            return
        with contextlib.suppress(Exception):
            proc.stdin.close()           # the helper releases held keys on EOF
        try:
            await asyncio.wait_for(proc.wait(), 3)
        except Exception:                                          # noqa: BLE001
            with contextlib.suppress(Exception):
                proc.kill()
        if self._reader is not None:
            self._reader.cancel()


# ---------------------------------------------------------------------------
# the cast
# ---------------------------------------------------------------------------
class ViewCast(console_browser.InteractiveCast):
    """The console's interactive cast, with X input, the X clipboard, a
    whole-screen mode, the page's JS dialogs brought to the viewer, and a capture
    source that follows what the page costs.

    WHERE THE PIXELS COME FROM, measured 2026-09-13 on builder2 against a headed
    Chrome 151 on Xvfb (software compositing), CPU of the watched browser:

        page            nothing watching   screencast   one frame a second
        still                  1%              1%              6%
        animating             12%            100%+            19%

    A screencast is free while nothing moves and very expensive while anything
    does, however slowly the frames are acked: Chrome reads back every compositor
    frame and only the sending is paced. One capture costs ~55 ms of CPU whatever
    the page does (see poll_loop for why it is not captureScreenshot). So the
    screencast runs while the page is still or while the person is using it, and a
    page that keeps changing with nobody touching it is polled at the idle rate
    instead, until it goes still or they touch it.
    """

    EXTRA_EVENTS = ("Page.javascriptDialogOpening", "Page.javascriptDialogClosed",
                    "Page.windowOpen")

    # Idle and still changing this many times inside the window: poll instead. The
    # window stretches with the idle interval, or a slow preset (one frame every two
    # seconds) could never see enough frames to notice, and stayed on the screencast
    # at 100% of a core: measured.
    ANIMATING_FRAMES, ANIMATING_WINDOW_S = 2, 6.0
    # Polled and unchanged this many captures in a row: the screencast is free again.
    STILL_POLLS = 4

    def __init__(self, ws_url: str, xh=None, preset: str = DEFAULT_PRESET, front: bool = False):
        p = PRESETS.get(preset) or PRESETS[DEFAULT_PRESET]
        super().__init__(ws_url, quality=p["quality"], fps=p["fps"], max_width=p["w"],
                         max_height=p["h"], active_fps=p["active"])
        self.x = xh
        self.front = bool(front)
        self.mode = "page"            # "page" or "screen" (the whole X screen)
        self.source = "cast"          # in page mode: "cast" or "poll"
        self._changes = []            # times of changed frames forwarded while idle
        self._front_at = 0.0
        self._origin = None
        self._origin_at = 0.0
        self._wheel = [0.0, 0.0]

    @property
    def x_input(self) -> bool:
        return bool(self.x is not None and self.x.alive)

    async def __aenter__(self):
        self._cdp = await ws_connect(self.ws_url, max_size=None, open_timeout=15)
        # Page only. See the module docstring for why not Runtime.
        await self._send("Page.enable")
        if self.front:
            # The person picked this tab, so it comes to the front, once, here.
            with contextlib.suppress(Exception):
                await self._send("Page.bringToFront")
            self._front_at = time.time()
        await self._make_visible()
        await self._start()
        return self

    async def _make_visible(self):
        """Paint while watched, without taking the front from anybody.

        Focus emulation, not bringToFront. A tab the dashboard's CPU guard froze
        stays "hidden" after it is thawed, even as the only tab in the window, and
        a hidden tab gives a screencast nothing and paints a blank page on the X
        screen too; measured, Page.bringToFront and Target.activateTarget did not
        wake it and focus emulation did. It is scoped to this debugger session and
        ends with it. bringToFront here would snatch the tab back from an agent
        every time it switched tabs; see _ensure_front for when that is right."""
        for method, params in (("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                               ("Page.setWebLifecycleState", {"state": "active"})):
            with contextlib.suppress(Exception):
                await self._send(method, params)

    async def _ensure_front(self):
        """Before real X input, make the tab on screen the tab being watched.

        Focus emulation lets a background tab paint, and X input goes to whatever
        tab is in front, so without this a click on the picture could land in a
        different tab an agent switched to. A person acting on the tab they can see
        is exactly when it should come forward. At most every 1.5 seconds."""
        if time.time() - self._front_at < 1.5:
            return
        self._front_at = time.time()
        with contextlib.suppress(Exception):
            await self.call("Page.bringToFront", timeout=2)

    async def _start(self):
        if self.mode == "page" and self.source == "cast":
            await super()._start()

    def _poke(self) -> None:
        super()._poke()
        self._changes.clear()
        if self.source == "poll":
            self.source = "cast"
            asyncio.ensure_future(self._restart_cast())

    async def _restart_cast(self):
        self._shown_digest = ""
        with contextlib.suppress(Exception):
            await self._start()

    def note_forwarded(self) -> None:
        """Called for each changed screencast frame sent to the viewer."""
        now = time.time()
        if now < self._active_until or self.mode != "page" or self.source != "cast":
            self._changes.clear()
            return
        window = max(self.ANIMATING_WINDOW_S, 3.5 * self._slow)
        self._changes = [t for t in self._changes if now - t < window] + [now]
        if len(self._changes) >= self.ANIMATING_FRAMES:
            self._changes.clear()
            self.source = "poll"
            asyncio.ensure_future(self._send("Page.stopScreencast"))

    async def poll_loop(self):
        """While polling, one frame per idle interval: start the screencast, take
        the first frame, stop it. The pump and sender forward that frame like any
        other, so this only decides WHEN Chrome captures.

        One-shot screencast rather than Page.captureScreenshot with a clip scale,
        which looks equivalent and is not. A scaled capture is done by resizing the
        page for the duration of the capture, and if the debugger connection closes
        mid-capture the resize is never undone: measured 2026-09-13, a 1911x988 page
        stayed at 764x382 after an interrupted 0.4 capture, and a new session's
        Emulation.clearDeviceMetricsOverride did not restore it. On a shared browser
        that is every agent's layout changed. A screencast scales in the capturer
        and leaves the page alone however it ends. Same cost: ~7% of a core at one
        frame a second, 60 ms from start to frame."""
        still = 0
        while True:
            if self.mode != "page" or self.source != "poll":
                still = 0
                await asyncio.sleep(0.25)
                continue
            started = time.time()
            frames, shown = self._frames, self._shown_digest
            with contextlib.suppress(Exception):
                await browser_live.Screencast._start(self)
                deadline = time.time() + 5
                while self._frames == frames and time.time() < deadline:
                    await asyncio.sleep(0.03)
                await self._send("Page.stopScreencast")
            await asyncio.sleep(0.1)          # let the sender take the frame
            if self.source != "poll":
                continue
            if self._frames > frames and self._latest_digest == shown:
                still += 1
                if still >= self.STILL_POLLS:
                    still = 0
                    self.source = "cast"
                    await self._restart_cast()
                    continue
            else:
                still = 0
            await asyncio.sleep(max(0.05, self._interval - (time.time() - started)))

    # ---- coordinates -------------------------------------------------------
    async def _viewport_origin(self):
        """Where the page viewport's top-left sits on the X screen.

        The window bounds come from the browser, the viewport size from the
        frame Chrome just sent; everything between them at the top is tabs and
        address bar. Cached for two seconds so a mouse move is not a round trip.
        """
        now = time.time()
        if self._origin and now - self._origin_at < 2.0:
            return self._origin
        vw = float(self._meta.get("deviceWidth") or 0)
        vh = float(self._meta.get("deviceHeight") or 0)
        try:
            win = await self.call("Browser.getWindowForTarget", timeout=3)
            b = win.get("bounds") or {}
            if not vw or not vh:
                css = (await self.call("Page.getLayoutMetrics", timeout=3)).get(
                    "cssVisualViewport") or {}
                vw, vh = float(css.get("clientWidth") or 0), float(css.get("clientHeight") or 0)
            # A window manager's frame is the same width left, right and bottom, and
            # everything else above the page is tabs and address bar. Without the
            # bottom border every click landed 3 px low under fluxbox.
            border = max(0.0, (float(b.get("width") or vw) - vw) / 2)
            ox = float(b.get("left") or 0) + border
            oy = float(b.get("top") or 0) + max(0.0, float(b.get("height") or vh) - vh - border)
            self._origin = (ox, oy, vw or 1.0, vh or 1.0)
            self._origin_at = now
        except Exception:                                          # noqa: BLE001
            if not self._origin:
                self._origin = (0.0, 0.0, vw or 1.0, vh or 1.0)
        return self._origin

    async def to_screen(self, nx: float, ny: float):
        nx, ny = max(0.0, min(float(nx), 1.0)), max(0.0, min(float(ny), 1.0))
        if self.mode == "screen":
            return int(nx * (self.x.w - 1)), int(ny * (self.x.h - 1))
        ox, oy, vw, vh = await self._viewport_origin()
        return int(round(ox + nx * vw)), int(round(oy + ny * vh))

    # ---- input -------------------------------------------------------------
    async def mouse(self, kind: str, nx: float, ny: float, button: str = "left",
                    buttons: int = 0, clicks: int = 1, modifiers: int = 0,
                    delta_y: float = 0.0, delta_x: float = 0.0):
        if self.mode == "page" and (not self.x_input or kind == "mouseWheel"):
            # A CDP wheel scrolls by exact pixels, which a trackpad needs; an X
            # wheel only knows notches. Wheel events are not what challenges score.
            return await super().mouse(kind, nx, ny, button, buttons, clicks,
                                       modifiers, delta_y, delta_x)
        if not self.x_input:
            return
        self._poke()
        sx, sy = await self.to_screen(nx, ny)
        if kind == "mouseMoved":
            self.x.send("move", x=sx, y=sy)
        elif kind in ("mousePressed", "mouseReleased"):
            if kind == "mousePressed" and self.mode == "page":
                await self._ensure_front()
            self.x.send("button", x=sx, y=sy, b=MOUSE_BUTTONS.get(button, 1),
                        down=kind == "mousePressed")
        elif kind == "mouseWheel":
            self._wheel[0] += float(delta_y)
            self._wheel[1] += float(delta_x)
            ny_, nx_ = int(self._wheel[0] / 60), int(self._wheel[1] / 60)
            self._wheel[0] -= ny_ * 60
            self._wheel[1] -= nx_ * 60
            if ny_ or nx_:
                # CDP's deltaY is "content moves up" positive; X button 5 is down.
                self.x.send("wheel", x=sx, y=sy, ny=ny_, nx=-nx_)

    async def key_event(self, kind: str, key: str, code: str = "", text: str = "",
                        modifiers: int = 0, repeat: bool = False):
        if not self.x_input:
            return await super().key_event(kind, key, code, text, modifiers, repeat)
        self._poke()
        if kind == "keyDown" and self.mode == "page":
            await self._ensure_front()
        if key in NAMED_KEYS:
            self.x.send("key", key=key, down=kind == "keyDown")
            return
        if kind != "keyDown" or len(key) != 1:
            return
        await self.type_text(key)

    async def type_text(self, value: str):
        """Characters typed one by one: ASCII as real keys, anything the US keymap
        has no key for (Hebrew, accents, emoji) inserted as text."""
        self._poke()
        if not self.x_input:
            return await super().insert_text(value)
        run = []
        for ch in value[:2000]:
            if 0x20 <= ord(ch) < 0x7f:
                if run:
                    await super().insert_text("".join(run))
                    run = []
                self.x.send("char", ch=ch)
            elif ch in "\r\n":
                self.x.send("key", key="Enter", down=True)
                self.x.send("key", key="Enter", down=False)
            else:
                run.append(ch)
        if run:
            await super().insert_text("".join(run))

    async def insert_text(self, value: str):
        """Paste: through the X clipboard so the page receives a real paste event."""
        self._poke()
        if self.x_input:
            await self._ensure_front()
            res = await self.x.call("paste", text=value[:1 << 20])
            if res.get("ok"):
                return
        await super().insert_text(value)

    async def copy_text(self, cut: bool = False) -> str:
        self._poke()
        if self.x_input:
            await self._ensure_front()
            res = await self.x.call("copy", cut=bool(cut), timeout=5)
            if res.get("ok"):
                return str(res.get("text") or "")
        with contextlib.suppress(Exception):
            res = await self.call("Runtime.evaluate",
                                  {"expression": SELECTION_JS, "returnByValue": True}, timeout=4)
            return str(((res.get("result") or {}).get("value")) or "")
        return ""

    def release_all(self):
        if self.x_input:
            self.x.send("release_all")

    async def answer_dialog(self, accept: bool, text: str = ""):
        params = {"accept": bool(accept)}
        if text:
            params["promptText"] = text
        with contextlib.suppress(Exception):
            await self._send("Page.handleJavaScriptDialog", params)

    # ---- whole-screen mode ---------------------------------------------------
    async def set_mode(self, mode: str):
        if mode == "screen" and not self.x_input:
            return False
        if mode == self.mode:
            return True
        self.mode = mode
        self._origin = None
        if mode == "screen":
            with contextlib.suppress(Exception):
                await self._send("Page.stopScreencast")
        else:
            self.source = "cast"
            self._shown_digest = ""
            await self._start()
        return True

    async def screen_loop(self, on_jpeg):
        """While in whole-screen mode, send a grab whenever the screen changed."""
        force = True
        while True:
            if self.mode != "screen" or not self.x_input:
                force = True
                await asyncio.sleep(0.3)
                continue
            res = await self.x.call("grab", w=self.max_width, h=self.max_height,
                                    q=self.quality, force=force, timeout=6)
            force = False
            if res.get("jpeg"):
                self._sent += 1
                self._bytes += len(res["jpeg"])
                await on_jpeg(res["jpeg"], {"deviceWidth": res.get("w"),
                                            "deviceHeight": res.get("h")})
            active = time.time() < self._active_until
            await asyncio.sleep(SCREEN_ACTIVE_S if active else SCREEN_IDLE_S)


# ---------------------------------------------------------------------------
# a viewer
# ---------------------------------------------------------------------------
async def serve(client_ws, ws_url: str, display: str = "", key: str = "",
                preset: str = DEFAULT_PRESET, front: bool = False, url: str = "") -> dict:
    """One websocket: frames out as binary, everything else as JSON text."""
    xh = await XHelper.acquire(display) if display else None
    VIEWERS[key] = VIEWERS.get(key, 0) + 1
    size = {"w": 0, "h": 0}
    try:
        async with ViewCast(ws_url, xh=xh, preset=preset, front=front) as cast:
            await client_ws.send_text(json.dumps({
                "t": "hello", "input": "x" if cast.x_input else "cdp",
                "screen": bool(cast.x_input), "url": url[:400]}))

            async def push(b64: str, meta: dict):
                w, h = meta.get("deviceWidth"), meta.get("deviceHeight")
                if (w, h) != (size["w"], size["h"]):
                    size["w"], size["h"] = w, h
                    await client_ws.send_text(json.dumps({"t": "size", "w": w, "h": h}))
                await client_ws.send_bytes(base64.b64decode(b64))

            async def on_frame(b64, meta):
                if cast.mode == "page":
                    await push(b64, meta)
                    cast.note_forwarded()

            async def on_event(method, params):
                # Runs inside the pump: never await cast.call() here, the pump is
                # the reader that call() waits on.
                if method == "Page.javascriptDialogOpening":
                    await client_ws.send_text(json.dumps({
                        "t": "dialog", "kind": params.get("type"),
                        "message": str(params.get("message") or "")[:2000],
                        "prompt": str(params.get("defaultPrompt") or "")[:500]}))
                elif method == "Page.javascriptDialogClosed":
                    await client_ws.send_text(json.dumps({"t": "dialog_closed"}))
                elif method == "Page.windowOpen":
                    await client_ws.send_text(json.dumps({"t": "popup",
                                                          "url": str(params.get("url"))[:400]}))
                else:
                    url = ((params.get("frame") or {}).get("url") or "")
                    if url:
                        cast._origin = None
                        await client_ws.send_text(json.dumps({"t": "nav", "url": url[:400]}))

            async def from_client():
                while True:
                    raw = await client_ws.receive_text()
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
                        await cast.key_event(m.get("k", "keyDown"), str(m.get("key", "")),
                                             code=m.get("code", ""), text=m.get("text", ""),
                                             modifiers=m.get("mod", 0), repeat=m.get("rep", False))
                    elif k == "text":
                        await cast.type_text(str(m.get("v", "")))
                    elif k == "paste":
                        await cast.insert_text(str(m.get("v", "")))
                    elif k == "copy":
                        text = await cast.copy_text(bool(m.get("cut")))
                        await client_ws.send_text(json.dumps({"t": "clip", "v": text,
                                                              "n": m.get("n")}))
                    elif k == "release":
                        cast.release_all()
                    elif k == "mode":
                        ok = await cast.set_mode("screen" if m.get("v") == "screen" else "page")
                        await client_ws.send_text(json.dumps({"t": "mode", "v": cast.mode,
                                                              "ok": ok}))
                    elif k == "dialog":
                        await cast.answer_dialog(bool(m.get("accept")), str(m.get("v") or ""))
                    elif k == "nav":
                        cast._poke()
                        await cast.navigate(str(m.get("v", "")))
                    elif k == "reload":
                        await cast.reload()
                    elif k == "hist":
                        await cast.history(int(m.get("v", -1)))
                    elif k == "ping":
                        await client_ws.send_text(json.dumps({"t": "pong", "s": cast.stats,
                                                              "source": cast.source}))

            tasks = {asyncio.create_task(cast.pump(on_frame, on_event)),
                     asyncio.create_task(from_client()),
                     asyncio.create_task(cast.screen_loop(push)),
                     asyncio.create_task(cast.poll_loop())}
            done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            for task in done:
                with contextlib.suppress(Exception):
                    task.result()
            cast.release_all()
            return cast.stats
    finally:
        VIEWERS[key] = max(0, VIEWERS.get(key, 1) - 1)
        if xh is not None:
            await xh.release()


# ---------------------------------------------------------------------------
# the exit this browser really uses
# ---------------------------------------------------------------------------
PROBE_URL = "about:blank#liteview-exit"
# api.ipify.org, not a richer service: it is on the relay's `only` list, so the
# answer is the exit a bot-walled site sees. A host off that list goes direct and
# reports this box's datacenter IP, which reads as "the proxy is down" when it is not.
IP_JS = ("fetch('https://api.ipify.org?format=json',{cache:'no-store'})"
         ".then(r=>r.text()).catch(e=>'ERR '+e)")


def _ip_owner(ip: str) -> dict:
    """Who owns an IP, asked from this server (the answer does not depend on the
    route). Best effort: a missing org just means the pill shows the address."""
    import urllib.request
    with contextlib.suppress(Exception):
        with urllib.request.urlopen("https://ipinfo.io/%s/json" % ip, timeout=4) as r:
            j = json.load(r)
            return {k: j.get(k) for k in ("city", "region", "country", "org")}
    return {}


async def exit_ip(port: int, timeout: float = 15.0) -> dict:
    """Ask the BROWSER where its traffic leaves from, from a background tab.

    Background, so the tab on screen and anything an agent is doing in it are not
    disturbed; about:blank, so no page's Content-Security-Policy can refuse the
    request and report a healthy network as dead; closed straight after."""
    started = time.time()
    try:
        ver = await asyncio.to_thread(browser_live.cdp_get, int(port), "/json/version", 4)
        bws = re.sub(r"ws://[^/]+/", "ws://127.0.0.1:%d/" % int(port),
                     ver.get("webSocketDebuggerUrl") or "")
        async with ws_connect(bws, max_size=None, open_timeout=8) as b:
            await b.send(json.dumps({"id": 1, "method": "Target.createTarget",
                                     "params": {"url": PROBE_URL, "background": True}}))
            target = ""
            while not target:
                msg = json.loads(await asyncio.wait_for(b.recv(), 8))
                if msg.get("id") == 1:
                    target = ((msg.get("result") or {}).get("targetId")) or "none"
            if target == "none":
                return {"ok": False, "error": "could not open a background tab"}
            try:
                page = "ws://127.0.0.1:%d/devtools/page/%s" % (int(port), target)
                async with ws_connect(page, max_size=None, open_timeout=8) as p:
                    await p.send(json.dumps({"id": 2, "method": "Runtime.evaluate", "params": {
                        "expression": IP_JS, "awaitPromise": True, "returnByValue": True,
                        "timeout": int(timeout * 1000)}}))
                    deadline = time.time() + timeout
                    while time.time() < deadline:
                        msg = json.loads(await asyncio.wait_for(
                            p.recv(), max(1.0, deadline - time.time())))
                        if msg.get("id") != 2:
                            continue
                        val = (((msg.get("result") or {}).get("result")) or {}).get("value")
                        ms = int((time.time() - started) * 1000)
                        if isinstance(val, str) and val.startswith("{"):
                            info = {"ip": json.loads(val).get("ip")}
                            info.update(await asyncio.to_thread(_ip_owner, info["ip"] or ""))
                            info.update(ok=True, ms=ms)
                            return info
                        return {"ok": False, "ms": ms,
                                "error": (val or "no answer from the browser")[:200]}
            finally:
                with contextlib.suppress(Exception):
                    await b.send(json.dumps({"id": 3, "method": "Target.closeTarget",
                                             "params": {"targetId": target}}))
    except Exception as e:                                         # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    return {"ok": False, "error": "timed out after %.0fs, which is what a dead proxy looks "
                                  "like" % timeout}


# ---------------------------------------------------------------------------
# the page
# ---------------------------------------------------------------------------
PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=5">
<title>__TITLE__</title>
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
 button.on{border-color:var(--accent);color:#cfe0ff}
 #url{flex:1 1 260px;min-width:150px;background:#0b0d0f;color:var(--fg);
      border:1px solid var(--line);border-radius:6px;padding:6px 9px;font:inherit}
 .pill{font-size:11px;padding:3px 8px;border-radius:99px;border:1px solid var(--line);
       color:var(--dim);white-space:nowrap;cursor:default}
 .pill.ok{color:var(--ok);border-color:#255f45}
 .pill.bad{color:var(--bad);border-color:#5f2b25}
 .pill.warn{color:var(--warn);border-color:#5f4a1c}
 #ip{cursor:pointer}
 #stage{position:relative;background:#000;margin:0 auto;max-width:1920px}
 #screen{display:block;width:100%;height:auto;-webkit-user-select:none;user-select:none;
      -webkit-touch-callout:none;cursor:default}
 #screen.armed{outline:2px solid var(--accent);outline-offset:-2px}
 #veil{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
      background:rgba(8,10,12,.86);color:var(--dim);text-align:center;padding:24px;
      font-size:14px;line-height:1.6;min-height:240px}
 #veil b{color:var(--fg);display:block;margin-bottom:6px;font-size:15px}
 #veil.hide{display:none}
 #kb{position:absolute;left:0;top:0;opacity:0;pointer-events:none;width:1px;height:1px;border:0}
 #dlg{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;
      justify-content:center;padding:16px;z-index:5}
 #dlg[hidden]{display:none}
 #dlg .box{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:16px;
      max-width:480px;width:100%}
 #dlg .msg{white-space:pre-wrap;margin:6px 0 12px;max-height:40vh;overflow:auto}
 #dlg input{width:100%;background:#0b0d0f;color:var(--fg);border:1px solid var(--line);
      border-radius:6px;padding:6px 9px;font:inherit;margin-bottom:12px}
 #dlg .row{display:flex;gap:8px;justify-content:flex-end}
 #toast{position:fixed;left:50%;bottom:18px;transform:translateX(-50%);background:#22262a;
      border:1px solid var(--line);border-radius:8px;padding:7px 12px;opacity:0;
      transition:opacity .2s;pointer-events:none;z-index:6}
 #toast.show{opacity:1}
 #clipbox{position:fixed;inset:0;background:rgba(0,0,0,.55);display:flex;align-items:center;
      justify-content:center;padding:16px;z-index:5}
 #clipbox[hidden]{display:none}
 #clipbox textarea{width:min(560px,100%);height:180px;background:#0b0d0f;color:var(--fg);
      border:1px solid var(--line);border-radius:8px;padding:8px;font:inherit}
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
  <button id="copy" title="Copy what is selected in the browser to your clipboard (ctrl-C)">Copy</button>
  <button id="paste" title="Paste your clipboard into the browser (ctrl-V)">Paste</button>
  <button id="mode" title="Whole screen shows the tab strip, native dialogs and open dropdown menus. It costs more, so it is off unless you need it.">Whole screen</button>
  <select id="qual" title="Picture quality against bandwidth"></select>
  <span id="egress" class="pill" title="Where this browser's traffic is routed"></span>
  <span id="ip" class="pill" title="Click to ask the browser for its exit IP">check exit</span>
  <span id="input" class="pill"></span>
  <span id="fps" class="pill">idle</span>
  <button id="vnc" title="Open the full noVNC desktop instead. Heavier; use it only if something here is not enough.">VNC</button>
</div>

<div id="stage">
  <canvas id="screen" aria-label="remote browser"></canvas>
  <textarea id="kb" autocapitalize="off" autocorrect="off" spellcheck="false"></textarea>
  <div id="veil"><div><b>Connecting</b>one moment</div></div>
</div>

<div class="foot">
  <span>Click the picture to take the keyboard. <code>ctrl-C</code> and <code>ctrl-V</code> copy and paste between the browser and your computer.</span>
  <span id="note"></span>
</div>

<div id="dlg" hidden><div class="box"><b id="dlgkind">The page asks</b>
  <div class="msg" id="dlgmsg"></div><input id="dlgin" hidden>
  <div class="row"><button id="dlgno">Cancel</button><button id="dlgok" class="on">OK</button></div></div></div>
<div id="clipbox" hidden><div><div style="margin-bottom:6px">Your browser would not let the page write to the clipboard. The text is selected: press ctrl-C, then click outside.</div><textarea id="cliptext" readonly></textarea></div></div>
<div id="toast"></div>

<script>
const BASE = __BASE__;
const VNC_URL = __VNC__;
const LIVE_API = __LIVE_API__;
const WSU = (location.protocol === "https:" ? "wss://" : "ws://") + location.host + BASE + "/ws";
const $ = id => document.getElementById(id);
const S = $("screen"), veil = $("veil"), kb = $("kb"), urlIn = $("url");
let ws = null, armed = false, tabId = "", frames = 0, bytes = 0;
let mode = "page", xInput = false, lastUrl = null;

function say(html, keep){ veil.innerHTML = "<div>" + html + "</div>"; veil.classList.remove("hide");
  if (!keep) setTimeout(()=>{ if (frames) veil.classList.add("hide"); }, 400); }
function toast(t){ const el = $("toast"); el.textContent = t; el.classList.add("show");
  clearTimeout(toast._t); toast._t = setTimeout(()=>el.classList.remove("show"), 1800); }
function api(p, o){ return fetch(BASE + "/api" + p, o).then(r => r.json()); }
function send(o){ if (ws && ws.readyState === 1) ws.send(JSON.stringify(o)); }
const MAC = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
// CDP's modifier bitmask: alt 1, ctrl 2, meta 4, shift 8. On a Mac, Command is sent
// as Control: the browser on the other end is Linux, where shortcuts are ctrl.
function mods(e){ return (e.altKey?1:0)|((e.ctrlKey||(MAC&&e.metaKey))?2:0)|(e.shiftKey?8:0); }
function pt(e){ const r = S.getBoundingClientRect();
  const t = (e.touches && e.touches[0]) || (e.changedTouches && e.changedTouches[0]) || e;
  return { x: (t.clientX - r.left) / r.width, y: (t.clientY - r.top) / r.height }; }

// ---- pointer. Press and release travel separately or you cannot drag or select.
const BTN = ["left","middle","right"];
let down = false, lastMove = 0;
S.addEventListener("mousemove", e => { const now = performance.now();
  if (!down && now - lastMove < 40) return; lastMove = now; const p = pt(e);
  send({t:"m", k:"mouseMoved", x:p.x, y:p.y, b: down?"left":"none", bs: down?1:0, mod: mods(e)}); });
S.addEventListener("mousedown", e => { e.preventDefault(); arm(); down = true; const p = pt(e);
  send({t:"m", k:"mousePressed", x:p.x, y:p.y, b:BTN[e.button]||"left", bs:1, c:e.detail||1, mod: mods(e)}); });
window.addEventListener("mouseup", e => { if (!down) return; down = false; const p = pt(e);
  send({t:"m", k:"mouseReleased", x:p.x, y:p.y, b:BTN[e.button]||"left", bs:0, c:e.detail||1, mod: mods(e)}); });
S.addEventListener("contextmenu", e => e.preventDefault());
S.addEventListener("wheel", e => { e.preventDefault(); const p = pt(e);
  const k = e.deltaMode === 1 ? 40 : 1;
  send({t:"m", k:"mouseWheel", x:p.x, y:p.y, dx:e.deltaX*k, dy:e.deltaY*k, mod: mods(e)});
}, {passive:false});

// ---- touch, so this works from a phone
S.addEventListener("touchstart", e => { arm(); const p = pt(e);
  send({t:"m", k:"mouseMoved", x:p.x, y:p.y, b:"none", bs:0});
  send({t:"m", k:"mousePressed", x:p.x, y:p.y, b:"left", bs:1, c:1}); }, {passive:true});
S.addEventListener("touchend", e => { const p = pt(e);
  send({t:"m", k:"mouseReleased", x:p.x, y:p.y, b:"left", bs:0, c:1}); }, {passive:true});

// ---- keyboard. The hidden textarea is what makes a phone keyboard appear at all.
function arm(){ if (!armed) { armed = true; S.classList.add("armed"); }
  try { kb.focus({preventScroll:true}); } catch(_) { kb.focus(); } }
function disarm(){ if (armed) send({t:"release"}); armed = false; S.classList.remove("armed"); }
kb.addEventListener("blur", disarm);
window.addEventListener("blur", () => send({t:"release"}));
document.addEventListener("visibilitychange", () => { if (document.hidden) send({t:"release"}); });
function keyName(e){ return (MAC && e.key === "Meta") ? "Control" : e.key; }
document.addEventListener("keydown", e => {
  if (!armed || e.target === urlIn) return;
  const chord = e.ctrlKey || (MAC && e.metaKey);
  const low = (e.key || "").toLowerCase();
  if (chord && low === "v") return;                         // let the paste event fire
  if (chord && (low === "c" || low === "x")) { e.preventDefault(); copyOut(low === "x"); return; }
  if (e.key === "Unidentified" || e.key === "Process" || e.isComposing) return;   // IME: see input
  e.preventDefault();
  send({t:"k", k:"keyDown", key:keyName(e), code:e.code, mod: mods(e), rep: e.repeat});
});
document.addEventListener("keyup", e => { if (!armed || e.target === urlIn) return;
  e.preventDefault(); send({t:"k", k:"keyUp", key:keyName(e), code:e.code, mod: mods(e)}); });
// A phone keyboard, dictation or an IME delivers text as input, with no usable key.
kb.addEventListener("input", () => { const v = kb.value; kb.value = ""; if (v) send({t:"text", v:v}); });
document.addEventListener("paste", e => { if (!armed) return; e.preventDefault();
  const v = (e.clipboardData || window.clipboardData).getData("text");
  if (v) { send({t:"paste", v:v}); toast("Pasted " + v.length + " characters"); } });

// ---- copy out. The clipboard write is started INSIDE the key or click handler, with
// a promise for the text, because browsers only allow a clipboard write during a
// user gesture and the text arrives from the server a moment later.
let copyN = 0; const copyWait = {};
function copyOut(cut){
  const n = ++copyN;
  const text = new Promise((res, rej) => { copyWait[n] = {res, rej};
    setTimeout(() => { if (copyWait[n]) { delete copyWait[n]; rej(new Error("timeout")); } }, 6000); });
  send({t:"copy", cut:!!cut, n:n});
  const nothing = () => toast("Nothing selected in the browser");
  const manual = t => { $("cliptext").value = t; $("clipbox").hidden = false; $("cliptext").select(); };
  if (navigator.clipboard && window.ClipboardItem) {
    try {
      const item = new ClipboardItem({"text/plain": text.then(t => {
        if (!t) throw new Error("empty"); return new Blob([t], {type:"text/plain"}); })});
      navigator.clipboard.write([item]).then(() => text.then(t => toast("Copied " + t.length + " characters")))
        .catch(() => text.then(t => { if (!t) return nothing();
          return navigator.clipboard.writeText(t).then(() => toast("Copied " + t.length + " characters"),
                                                       () => manual(t)); }).catch(nothing));
      return;
    } catch(_) {}
  }
  text.then(t => { if (!t) return nothing();
    (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject())
      .then(() => toast("Copied " + t.length + " characters"), () => manual(t)); }).catch(nothing);
}
$("clipbox").addEventListener("click", e => { if (e.target.id === "clipbox") $("clipbox").hidden = true; });
$("copy").onclick = () => copyOut(false);
$("paste").onclick = () => {
  if (!navigator.clipboard || !navigator.clipboard.readText) { toast("Use ctrl-V on the picture"); return; }
  navigator.clipboard.readText().then(v => { if (!v) return toast("Your clipboard is empty");
    send({t:"paste", v:v}); toast("Pasted " + v.length + " characters"); arm(); },
    () => toast("Clipboard access was refused: click the picture and press ctrl-V")); };

// ---- the page's own alert / confirm / prompt
function dialog(m){
  $("dlgkind").textContent = m.kind === "beforeunload" ? "Leave this page?" : "The page asks";
  $("dlgmsg").textContent = m.message || "";
  const inp = $("dlgin"); inp.hidden = m.kind !== "prompt"; inp.value = m.prompt || "";
  $("dlgno").hidden = m.kind === "alert";
  $("dlg").hidden = false; (m.kind === "prompt" ? inp : $("dlgok")).focus();
}
$("dlgok").onclick = () => { send({t:"dialog", accept:true, v:$("dlgin").value}); $("dlg").hidden = true; };
$("dlgno").onclick = () => { send({t:"dialog", accept:false}); $("dlg").hidden = true; };
$("dlgin").addEventListener("keydown", e => { if (e.key === "Enter") $("dlgok").onclick(); });

// ---- controls
$("back").onclick = () => send({t:"hist", v:-1});
$("fwd").onclick  = () => send({t:"hist", v:1});
$("rl").onclick   = () => send({t:"reload"});
urlIn.addEventListener("keydown", e => { if (e.key !== "Enter") return;
  let v = urlIn.value.trim(); if (!v) return;
  if (!/^[a-z]+:\/\//i.test(v)) v = (v.indexOf(" ") < 0 && v.indexOf(".") > 0)
      ? "https://" + v : "https://www.google.com/search?q=" + encodeURIComponent(v);
  send({t:"nav", v:v}); urlIn.blur(); });
$("newtab").onclick = () => api("/tab/new", {method:"POST"}).then(() => refresh(true, true));
$("closetab").onclick = () => { if (!tabId) return;
  api("/tab/close?target=" + encodeURIComponent(tabId), {method:"POST"})
    .then(() => { tabId = ""; refresh(true); }); };
$("tab").onchange = e => { tabId = e.target.value; connect(true); };
$("mode").onclick = () => send({t:"mode", v: mode === "screen" ? "page" : "screen"});
$("vnc").onclick = () => {
  if (!VNC_URL) { toast("No VNC for this browser"); return; }
  const w = window.open("about:blank", "_blank");
  fetch(LIVE_API, {method:"POST", headers:{"Content-Type":"application/json"}, body:'{"start":true}'})
    .then(r => r.json()).then(d => { if (d && d.ok === false) throw new Error(d.error || "refused");
      if (w) w.location = VNC_URL; })
    .catch(err => { if (w) w.close(); toast("VNC could not start: " + (err.message || err)); }); };
$("ip").onclick = checkIp;

function preset(){ return $("qual").value || "balanced"; }
$("qual").onchange = () => { try { localStorage.setItem("liteview.q", preset()); } catch(_){}
  connect(false); };

// ---- the stream
// Frames are drawn on a canvas from createImageBitmap, not put in an <img> as
// blob: URLs. The dashboard's Content-Security-Policy allows images from 'self' and
// data: only, so a blob: image is refused and the viewer showed a broken picture
// while frames were arriving. createImageBitmap is not an image load, and it
// decodes off the main thread as well.
const ctx = S.getContext("2d", {alpha: false});
let drawSeq = 0;
function paint(blob){
  const seq = ++drawSeq;
  const done = img => {
    if (seq === drawSeq) {
      if (S.width !== img.width || S.height !== img.height) { S.width = img.width; S.height = img.height; }
      ctx.drawImage(img, 0, 0); veil.classList.add("hide");
    }
    if (img.close) img.close();
  };
  if (window.createImageBitmap) { createImageBitmap(blob).then(done, () => {}); return; }
  const r = new FileReader();
  r.onload = () => { const im = new Image(); im.onload = () => done(im); im.src = r.result; };
  r.readAsDataURL(blob);
}
// `front` only when the person picked the tab. Every other connect watches the tab
// without bringing it forward, so a guess can never steal the front from an agent.
function connect(front){
  if (ws) { try { ws.onclose = null; ws.close(); } catch(_){} ws = null; }
  if (!tabId) return;
  frames = 0;
  const sock = new WebSocket(WSU + "?tab=" + encodeURIComponent(tabId) + "&q=" + preset() +
                             "&front=" + (front ? 1 : 0));
  sock.binaryType = "blob"; ws = sock;
  sock.onmessage = ev => {
    if (typeof ev.data !== "string") {
      frames++; bytes += ev.data.size; paint(ev.data); return;
    }
    const m = JSON.parse(ev.data);
    if (m.t === "hello") { xInput = !!m.input && m.input === "x";
      const el = $("input"); el.textContent = xInput ? "real keyboard and mouse" : "input via debugger";
      el.className = "pill " + (xInput ? "ok" : "warn");
      el.title = xInput ? "Input enters the X server like a physical keyboard and mouse."
                        : "This browser has no display this dashboard can reach, so input goes through the debugger.";
      $("mode").hidden = !m.screen; if (mode === "screen") send({t:"mode", v:"screen"});
      if (m.url && document.activeElement !== urlIn) urlIn.value = m.url; }
    else if (m.t === "nav") { if (document.activeElement !== urlIn) urlIn.value = m.url; }
    else if (m.t === "clip") { const w = copyWait[m.n]; if (w) { delete copyWait[m.n]; w.res(m.v || ""); } }
    else if (m.t === "mode") { mode = m.v; $("mode").classList.toggle("on", mode === "screen");
      $("mode").textContent = mode === "screen" ? "Page only" : "Whole screen"; }
    else if (m.t === "dialog") dialog(m);
    else if (m.t === "dialog_closed") $("dlg").hidden = true;
    else if (m.t === "popup") setTimeout(() => refresh(false, true), 700);
  };
  sock.onclose = () => { if (ws !== sock) return; if (frames) say("<b>Stream ended</b>reconnecting", true);
    setTimeout(() => { if (ws === sock && tabId) connect(false); }, 1500); };
  sock.onerror = () => {};
}

let lastF = 0, lastT = Date.now();
setInterval(() => { const now = Date.now();
  const f = ((frames - lastF) * 1000 / Math.max(1, now - lastT));
  lastF = frames; lastT = now;
  $("fps").textContent = frames ? (f.toFixed(1) + " fps  " + Math.round(bytes/1024) + " KB") : "idle";
}, 2000);

// ---- state
function refresh(hard, follow){
  return api("/state").then(d => {
    if (d.error) { say("<b>" + d.error + "</b>", true); return d; }
    document.title = (d.name || "Browser") + " live";
    const eg = $("egress"); eg.textContent = d.egress_label || "egress unknown";
    eg.className = "pill" + (d.egress_kind === "relay" ? " ok" : "");
    eg.title = d.egress_note || "";
    const ql = $("qual");
    if (!ql.options.length) { (d.quality_options||[]).forEach(o => {
        const op = document.createElement("option"); op.value = o.id;
        op.textContent = o.label; ql.appendChild(op); });
      let saved = null; try { saved = localStorage.getItem("liteview.q"); } catch(_){}
      ql.value = saved || d.quality_default; }
    $("vnc").hidden = !VNC_URL;
    const ids = (d.tabs||[]).map(t => t.id);
    const sel = $("tab");
    if (ids.join(",") !== sel.dataset.ids || hard) { sel.dataset.ids = ids.join(","); sel.innerHTML = "";
      (d.tabs||[]).forEach(t => { const o = document.createElement("option");
        o.value = t.id; o.textContent = (t.title || t.url || "tab").slice(0, 40);
        sel.appendChild(o); }); }
    if (!d.running) { tabId = ""; if (ws) { ws.onclose = null; ws.close(); ws = null; }
      say("<b>This browser is not running</b>start it from the Browser tab", true); return d; }
    // Follow the tab in front, the way a screen would: when our tab went hidden or a
    // popup opened. "active" is Chrome's most recently activated OR created tab, which
    // is usually the front one; connect(false) keeps a wrong guess harmless.
    let reconnect = hard;
    if (tabId && ids.indexOf(tabId) < 0) tabId = "";
    if (d.active && d.active !== tabId && (!tabId || follow)) {
      if (tabId) toast("Following the tab in front");
      tabId = d.active; reconnect = true; }
    if (tabId) { sel.value = tabId; if (!ws || reconnect) connect(false); }
    return d;
  }).catch(() => {});
}
function checkIp(){
  const el = $("ip"); el.className = "pill"; el.textContent = "asking the browser";
  api("/exit-ip").then(d => {
    if (d.ok) { el.className = "pill ok";
      el.textContent = d.ip + (d.country ? "  " + [d.city, d.country].filter(Boolean).join(", ") : "");
      el.title = "The exit a listed site sees. " + (d.org || "") + "  answered in " + d.ms + " ms";
      $("note").textContent = d.org ? "Exit: " + d.org : ""; }
    else { el.className = "pill bad"; el.textContent = "no internet on this exit";
      $("note").textContent = d.error || ""; } }).catch(() => { el.textContent = "check exit"; });
}

refresh(true);
setInterval(() => refresh(false), 5000);
</script></body></html>"""


def page_html(base: str, title: str, vnc_url: str = "", live_api: str = "") -> str:
    return (PAGE.replace("__BASE__", json.dumps(base))
                .replace("__VNC__", json.dumps(vnc_url or ""))
                .replace("__LIVE_API__", json.dumps(live_api or ""))
                .replace("__TITLE__", html.escape(title or "Browser") + " live"))


# ---------------------------------------------------------------------------
# mount
# ---------------------------------------------------------------------------
def _tabs(port: int) -> list:
    """Page tabs, without our own exit probe.

    /json/list is ordered most recently activated OR CREATED first. Measured
    2026-09-13: a tab opened with background=true goes to the top of the list while
    staying hidden. So the first row is only a guess at the tab in front; the
    viewer corrects it from the screencast's own visibility event."""
    return [t for t in browser_live.targets(port) if t.get("url") != PROBE_URL]


def same_origin(ws) -> bool:
    """A page on another site must not be able to open this socket with the
    person's cookie. Browsers always send Origin on a websocket handshake; a
    missing one is a non-browser client, which the cookie check still covers."""
    origin = ws.headers.get("origin") or ""
    if not origin:
        return True
    host = (ws.headers.get("x-forwarded-host") or ws.headers.get("host") or "").split(",")[0].strip()
    return bool(host) and re.sub(r"^https?://", "", origin).rstrip("/") == host


def mount(app, *, session, can_view, prefix, extras=None, proxy_conf=None) -> None:
    """Register /browser/{sid}/view on the dashboard.

    MUST be called before the dashboard's `/browser/{sid}/{path:path}` noVNC
    proxy is registered, or that catch-all answers /view and serves a 404 from
    websockify.

    session(sid) -> dict with cdp_port, display, name (empty when unknown)
    can_view(conn, s) -> bool, called for HTTP requests AND websockets, because
        the dashboard's HTTP middleware does not run for websockets
    prefix(request) -> the mount prefix of this request
    extras(s, prefix) -> {"vnc_url", "live_api"}
    proxy_conf() -> the relay config, for the egress note
    """
    extras = extras or (lambda s, p: {})
    proxy_conf = proxy_conf or (lambda: {})

    def _resolve(conn, sid):
        s = session(sid) or {}
        if not s:
            return None, JSONResponse({"error": "Unknown browser"}, status_code=404)
        if not can_view(conn, s):
            return None, JSONResponse({"error": "Not allowed"}, status_code=403)
        return s, None

    @app.get("/browser/{sid}/view")
    async def browser_view_page(sid: str, request: Request):
        s, err = _resolve(request, sid)
        if err is not None:
            return HTMLResponse("Not found" if err.status_code == 404 else "Not allowed",
                                status_code=err.status_code)
        pre = prefix(request)
        ex = extras(s, pre) or {}
        return HTMLResponse(page_html("%s/browser/%s/view" % (pre, sid), s.get("name") or sid,
                                      ex.get("vnc_url", ""), ex.get("live_api", "")),
                            headers={"Cache-Control": "no-store"})

    @app.get("/browser/{sid}/view/api/state")
    async def browser_view_state(sid: str, request: Request):
        s, err = _resolve(request, sid)
        if err is not None:
            return err
        port = int(s.get("cdp_port") or 0)
        running = bool(port) and await asyncio.to_thread(browser_live.is_up, port)
        rows = await asyncio.to_thread(_tabs, port) if running else []
        eg = await asyncio.to_thread(egress, port, proxy_conf() or {}) if running else {}
        return JSONResponse({
            "name": s.get("name") or sid, "running": running,
            "tabs": [{"id": t["id"], "title": t["title"], "url": t["url"]} for t in rows],
            "active": rows[0]["id"] if rows else "",
            "viewers": VIEWERS.get(sid, 0),
            "egress_label": eg.get("egress_label", ""), "egress_kind": eg.get("egress_kind", ""),
            "egress_note": eg.get("egress_note", ""),
            "quality_options": [{"id": k, "label": v["label"]} for k, v in PRESETS.items()],
            "quality_default": DEFAULT_PRESET})

    @app.post("/browser/{sid}/view/api/tab/new")
    async def browser_view_new_tab(sid: str, request: Request, url: str = "about:blank"):
        s, err = _resolve(request, sid)
        if err is not None:
            return err
        try:
            return JSONResponse(await asyncio.to_thread(
                console_browser._cdp_new_tab, int(s.get("cdp_port") or 0), url))
        except Exception as e:                                     # noqa: BLE001
            return JSONResponse({"ok": False, "error": str(e)[:300]}, status_code=400)

    @app.post("/browser/{sid}/view/api/tab/close")
    async def browser_view_close_tab(sid: str, request: Request, target: str = ""):
        s, err = _resolve(request, sid)
        if err is not None:
            return err
        if not re.fullmatch(r"[A-Za-z0-9]{8,64}", target or ""):
            return JSONResponse({"ok": False, "error": "bad target"}, status_code=400)

        def close():
            import urllib.request
            with contextlib.suppress(Exception):
                urllib.request.urlopen("http://127.0.0.1:%d/json/close/%s"
                                       % (int(s.get("cdp_port") or 0), target), timeout=6).read()
        await asyncio.to_thread(close)
        return JSONResponse({"ok": True})

    @app.get("/browser/{sid}/view/api/exit-ip")
    async def browser_view_exit_ip(sid: str, request: Request):
        s, err = _resolve(request, sid)
        if err is not None:
            return err
        return JSONResponse(await exit_ip(int(s.get("cdp_port") or 0)))

    @app.websocket("/browser/{sid}/view/ws")
    async def browser_view_ws(ws: WebSocket, sid: str, tab: str = "", q: str = DEFAULT_PRESET,
                              front: int = 0):
        s = session(sid) or {}
        if not s or not can_view(ws, s) or not same_origin(ws):
            await ws.close(code=1008)
            return
        port = int(s.get("cdp_port") or 0)
        rows = await asyncio.to_thread(_tabs, port) if port else []
        rows = [t for t in rows if t["id"] == tab] or rows
        if not rows:
            await ws.close(code=1011)
            return
        display = await asyncio.to_thread(display_for_port, port, s.get("display") or "")
        await ws.accept()
        try:
            await serve(ws, rows[0]["ws"], display=display, key=sid,
                        preset=q if q in PRESETS else DEFAULT_PRESET, front=bool(front),
                        url=rows[0].get("url") or "")
        except Exception:                                          # noqa: BLE001
            with contextlib.suppress(Exception):
                await ws.close()
