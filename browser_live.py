"""Watch the filing agent's browser live, over CDP screencast rather than VNC.

noVNC streams the whole X framebuffer through an X server, a VNC encoder and a
websocket proxy. For watching one Chrome tab that is all waste: Chrome will encode
the page viewport itself and push a JPEG only when something changed.
`Page.startScreencast` does exactly that, so the bytes on the wire are a fraction of
VNC's, latency is one frame rather than a round trip, there is no X server in the
path at all, and it works against a HEADLESS Chrome, which noVNC cannot do. That
last point matters: the fees.uspto.gov payment step has to run in a clean
proxy-free Chrome, and it is the step most worth watching.

Frames flow out, clicks and keystrokes flow back in, over one websocket.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import socket
import subprocess
import time
import urllib.request
import uuid
from pathlib import Path

try:                                             # websockets >= 14
    from websockets.asyncio.client import connect as ws_connect
except Exception:                                # pragma: no cover
    from websockets.client import connect as ws_connect  # type: ignore

CHROME = (shutil.which("google-chrome") or shutil.which("google-chrome-stable")
          or shutil.which("chromium") or shutil.which("chromium-browser"))
PROFILE_ROOT = Path(os.environ.get("PATENT_DATA_DIR",
                                   Path.home() / ".tmux-dashboard" / "patents")) / "browsers"
PORT_RANGE = range(9400, 9450)


# --------------------------------------------------------------------------
# a dedicated Chrome per filing
# --------------------------------------------------------------------------
def _free_port() -> int:
    for port in PORT_RANGE:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("no free CDP port in %s" % PORT_RANGE)


def cdp_get(port: int, path: str, timeout: float = 6.0):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=timeout) as r:
        return json.load(r)


def is_up(port: int) -> bool:
    try:
        cdp_get(port, "/json/version", timeout=2.5)
        return True
    except Exception:
        return False


def launch(label: str, headless: bool = True, port: int = 0) -> dict:
    """Start a Chrome that belongs to this filing and nothing else.

    --no-proxy-server is not optional. The shared browser on these boxes sits
    behind the Decodo residential relay, and that relay 407s captcha-sdk.awswaf.com,
    which is the script fees.uspto.gov wraps its payment POST in. The symptom is a
    "Processing..." spinner that never resolves and no HTTP request at all.
    """
    if not CHROME:
        raise RuntimeError("no Chrome on this host")
    port = port or _free_port()
    profile = PROFILE_ROOT / label
    profile.mkdir(parents=True, exist_ok=True)
    args = [CHROME,
            "--no-proxy-server",
            "--user-data-dir=%s" % profile,
            "--remote-debugging-port=%d" % port,
            "--remote-allow-origins=*",
            "--no-first-run", "--no-default-browser-check",
            "--disable-dev-shm-usage", "--disable-background-networking",
            "--disable-features=Translate,OptimizationHints",
            "--window-size=1440,960", "about:blank"]
    if headless:
        args.insert(1, "--headless=new")
    log = profile / "chrome.log"
    proc = subprocess.Popen(args, stdout=log.open("ab"), stderr=subprocess.STDOUT,
                            start_new_session=True)
    deadline = time.time() + 25
    while time.time() < deadline:
        if is_up(port):
            break
        if proc.poll() is not None:
            raise RuntimeError("Chrome exited immediately; see %s" % log)
        time.sleep(0.4)
    else:
        raise RuntimeError("Chrome did not open CDP on port %d" % port)
    return {"port": port, "pid": proc.pid, "profile": str(profile),
            "headless": headless, "log": str(log)}


def shutdown(port: int, profile: str = "", remove_profile: bool = False) -> dict:
    """Close the browser we opened. Never touches another session's Chrome."""
    killed = False
    try:
        version = cdp_get(port, "/json/version", timeout=3)
        ws = version.get("webSocketDebuggerUrl")
        if ws:
            asyncio.run(_browser_close(ws))
            killed = True
    except Exception:
        pass
    if not killed and profile:
        # Match on the profile path, which is unique to this filing, so a broad
        # pkill can never reach an unrelated agent's browser.
        try:
            out = subprocess.run(["pgrep", "-f", "--", "user-data-dir=%s" % profile],
                                 capture_output=True, text=True, timeout=10).stdout
            for pid in [p for p in out.split() if p.isdigit()]:
                with contextlib.suppress(Exception):
                    os.kill(int(pid), signal.SIGTERM)
                    killed = True
        except Exception:
            pass
    if remove_profile and profile:
        shutil.rmtree(profile, ignore_errors=True)
    return {"ok": True, "killed": killed, "port": port}


async def _browser_close(ws_url: str):
    async with ws_connect(ws_url, max_size=None, open_timeout=10) as ws:
        await ws.send(json.dumps({"id": 1, "method": "Browser.close"}))
        with contextlib.suppress(Exception):
            await asyncio.wait_for(ws.recv(), timeout=4)


def targets(port: int) -> list:
    try:
        rows = cdp_get(port, "/json/list")
    except Exception:
        return []
    out = []
    for t in rows:
        if t.get("type") != "page":
            continue
        ws = t.get("webSocketDebuggerUrl") or ""
        # Through an ssh tunnel Chrome still advertises its own (remote) port, so
        # rewrite it to the port we can actually reach.
        ws = re.sub(r"ws://127\.0\.0\.1:\d+/", "ws://127.0.0.1:%d/" % port, ws)
        out.append({"id": t.get("id"), "title": (t.get("title") or "")[:90],
                    "url": (t.get("url") or "")[:200], "ws": ws})
    return out


# --------------------------------------------------------------------------
# a browser on another host, reached through an ssh tunnel
# --------------------------------------------------------------------------
REMOTE_HOST = os.environ.get("PATENT_BROWSER_HOST", "instance-3")
REMOTE_ZONE = os.environ.get("PATENT_BROWSER_ZONE", "us-central1-b")
REMOTE_USER = os.environ.get("PATENT_BROWSER_USER", "nimrod_rotem")

_REMOTE_LAUNCH = r"""
set -e
port=0
for p in $(seq 9400 9449); do
  if ! (exec 3<>/dev/tcp/127.0.0.1/$p) 2>/dev/null; then port=$p; break; fi
done
[ "$port" = 0 ] && { echo "NOPORT"; exit 1; }
prof=/tmp/patent-browser-%(label)s
rm -rf "$prof"; mkdir -p "$prof"
# A headed browser needs a display. instance-3 keeps several Xvfb screens up; take
# the first one that answers rather than assuming a number.
if [ -z "%(headless)s" ]; then
  for d in $DISPLAY :100 :101 :102 :103 :104 :99 :150; do
    [ -n "$d" ] && [ -S "/tmp/.X11-unix/X${d#:}" ] && { export DISPLAY=$d; break; }
  done
  [ -z "$DISPLAY" ] && { echo "NODISPLAY"; exit 1; }
fi
nohup google-chrome %(headless)s --no-proxy-server --user-data-dir="$prof"   --remote-debugging-port=$port --remote-allow-origins='*' --no-first-run   --no-default-browser-check --disable-dev-shm-usage --window-size=1440,960   about:blank >"$prof/chrome.log" 2>&1 &
for i in $(seq 1 60); do
  if curl -s --max-time 2 "http://127.0.0.1:$port/json/version" >/dev/null; then
    echo "PORT=$port"; echo "PROFILE=$prof"; exit 0
  fi
  sleep 0.5
done
echo "TIMEOUT"; exit 1
"""


def launch_remote(label: str, headless: bool = True, host: str = "", zone: str = "") -> dict:
    """Start Chrome on another box and tunnel its CDP port to this one.

    Why this exists: Chrome 152 on the patents box never answers Network.setCookie
    or Storage.setCookies over CDP, headless or headed, so the USPTO device-trust
    cookies cannot be injected locally. instance-3 runs Chrome 149, where it works,
    and it already holds the device-trust file. So the agent and the panel stay
    here and only the browser lives there, behind a plain ssh port-forward.
    """
    host = host or REMOTE_HOST
    zone = zone or REMOTE_ZONE
    # The profile path is the handle shutdown_remote kills by, so it MUST be unique
    # per launch. It used to be /tmp/patent-browser-<label>, which meant two runs
    # against the same application shared one path: closing the stale one killed
    # the live one's browser out from under it, mid-filing, with no error anywhere.
    safe = ("".join(c for c in label if c.isalnum() or c in "-_") or "filing")
    safe = "%s-%s" % (safe[:40], uuid.uuid4().hex[:8])
    script = _REMOTE_LAUNCH % {"label": safe,
                               "headless": "--headless=new" if headless else ""}
    out = subprocess.run(
        ["gcloud", "compute", "ssh", "%s@%s" % (REMOTE_USER, host), "--zone", zone,
         "--command", "sudo -u %s bash -s" % REMOTE_USER],
        input=script, capture_output=True, text=True, timeout=180)
    remote_port = remote_profile = ""
    for line in (out.stdout or "").splitlines():
        if line.startswith("PORT="):
            remote_port = line.split("=", 1)[1].strip()
        elif line.startswith("PROFILE="):
            remote_profile = line.split("=", 1)[1].strip()
    if not remote_port:
        raise RuntimeError("remote Chrome did not start on %s: %s"
                           % (host, ((out.stdout or "") + (out.stderr or ""))[-300:]))
    local_port = _free_port()
    tunnel = subprocess.Popen(
        ["gcloud", "compute", "ssh", "%s@%s" % (REMOTE_USER, host), "--zone", zone,
         "--tunnel-through-iap" if os.environ.get("PATENT_BROWSER_IAP") else "--",
         "-N", "-L", "%d:127.0.0.1:%s" % (local_port, remote_port)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.time() + 40
    while time.time() < deadline:
        if is_up(local_port):
            break
        if tunnel.poll() is not None:
            raise RuntimeError("ssh tunnel to %s exited" % host)
        time.sleep(0.5)
    else:
        with contextlib.suppress(Exception):
            tunnel.terminate()
        raise RuntimeError("tunnel to %s:%s never came up" % (host, remote_port))
    return {"port": local_port, "remote_port": int(remote_port), "host": host,
            "zone": zone, "profile": remote_profile, "headless": headless,
            "tunnel_pid": tunnel.pid, "remote": True}


def shutdown_remote(info: dict) -> dict:
    """Close the remote Chrome and drop the tunnel."""
    host, zone = info.get("host") or REMOTE_HOST, info.get("zone") or REMOTE_ZONE
    profile = info.get("profile") or ""
    if profile:
        cmd = ("pids=$(pgrep -f -- \"user-data-dir=%s\" || true); "
               "for p in $pids; do kill $p 2>/dev/null || true; done; rm -rf %s"
               % (profile, profile))
        with contextlib.suppress(Exception):
            subprocess.run(["gcloud", "compute", "ssh", "%s@%s" % (REMOTE_USER, host),
                            "--zone", zone, "--command",
                            "sudo -u %s bash -lc %s" % (REMOTE_USER, shlex.quote(cmd))],
                           capture_output=True, text=True, timeout=120)
    pid = info.get("tunnel_pid")
    if pid:
        with contextlib.suppress(Exception):
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    return {"ok": True, "port": info.get("port")}


# --------------------------------------------------------------------------
# the bridge
# --------------------------------------------------------------------------
class Screencast:
    """One viewer attached to one tab.

    Two rate facts, both measured rather than assumed:
      * every frame must be acked or Chrome stops after the first;
      * headless Chrome does NOT only send on change. It happily produced 150
        frames in 2.5 seconds on a blank page, about 360 KB/s, which would be
        worse than the noVNC this replaces.
    So frames are acked immediately to keep the stream alive, but forwarded to the
    viewer at most `fps` times a second, and only when the image actually differs
    from the one already on screen. An idle page then costs nothing on the wire.
    """

    def __init__(self, ws_url: str, quality: int = 55, max_width: int = 1280,
                 max_height: int = 800, every_nth: int = 1, fps: float = 6.0):
        self.ws_url = ws_url
        self.quality = max(20, min(int(quality), 90))
        self.max_width = max(320, min(int(max_width), 1920))
        self.max_height = max(240, min(int(max_height), 1200))
        # everyNthFrame must stay 1. Chrome only generates a frame when the page
        # changes, so on a static page there is exactly ONE, and "every 2nd frame"
        # of one frame is none at all: the viewer stays black on an idle page.
        self.every_nth = max(1, min(int(every_nth), 8))
        self._cdp = None
        self._next_id = 0
        self._meta = {}
        self._frames = 0          # frames Chrome sent
        self._sent = 0            # frames forwarded to the viewer
        self._bytes = 0           # bytes forwarded
        self._interval = 1.0 / max(0.5, min(float(fps), 20.0))
        self._latest = None
        self._latest_digest = ""
        self._shown_digest = ""
        self._new_frame = asyncio.Event()

    async def __aenter__(self):
        self._cdp = await ws_connect(self.ws_url, max_size=None, open_timeout=15)
        await self._send("Page.enable")
        await self._send("Runtime.enable")
        # Headless Chrome treats a tab that has never painted as hidden, and a
        # hidden tab emits no screencast frames at all: you get a black viewer on
        # an idle page and it looks like the bridge is broken. Marking the page
        # active first, and nudging it if the first frame does not arrive, is what
        # makes the very first paint show up.
        await self._make_visible()
        await self._start()
        return self

    async def _make_visible(self):
        """Convince a headless tab it is on screen, or it never paints.

        Measured on instance-3, Chrome 149, one tab, nothing else open: with none
        of this, Page.screencastVisibilityChanged reports visible=false and the
        tab emits ZERO frames, for ever, however many times you restart the
        screencast. Emulation.setFocusEmulationEnabled flips it to visible=true
        and the frames start; Page.bringToFront does too. Both are sent because
        a navigation can drop either one, and neither costs anything.
        """
        for method, params in (("Emulation.setFocusEmulationEnabled", {"enabled": True}),
                               ("Page.bringToFront", None),
                               ("Page.setWebLifecycleState", {"state": "active"})):
            with contextlib.suppress(Exception):
                await self._send(method, params)

    async def _start(self):
        await self._send("Page.startScreencast", {
            "format": "jpeg", "quality": self.quality,
            "maxWidth": self.max_width, "maxHeight": self.max_height,
            "everyNthFrame": self.every_nth})

    async def _renudge(self):
        """After a navigation, provoke a paint if the new page is already static."""
        before = self._frames
        for delay in (0.6, 1.2, 2.5):
            await asyncio.sleep(delay)
            if self._frames > before:
                return
            await self._make_visible()
            with contextlib.suppress(Exception):
                await self._send("Input.dispatchMouseEvent",
                                 {"type": "mouseWheel", "x": 8, "y": 8,
                                  "deltaX": 0, "deltaY": 0})
                await self._start()

    async def _first_frame_watchdog(self):
        """Chrome only sends a frame when the page changes. Provoke the first one."""
        for delay in (1.2, 2.0, 3.0, 5.0):
            await asyncio.sleep(delay)
            if self._frames:
                return
            await self._make_visible()
            with contextlib.suppress(Exception):
                await self._send("Input.dispatchMouseEvent",
                                 {"type": "mouseWheel", "x": 8, "y": 8,
                                  "deltaX": 0, "deltaY": 0})
                await self._start()

    async def __aexit__(self, *exc):
        with contextlib.suppress(Exception):
            await self._send("Page.stopScreencast")
        with contextlib.suppress(Exception):
            await self._cdp.close()

    async def _send(self, method: str, params: dict = None):
        self._next_id += 1
        await self._cdp.send(json.dumps({"id": self._next_id, "method": method,
                                         "params": params or {}}))
        return self._next_id

    async def _sender(self, on_frame):
        """At most one frame per interval, and only if the picture changed."""
        while True:
            await self._new_frame.wait()
            self._new_frame.clear()
            frame = self._latest
            if frame and self._latest_digest != self._shown_digest:
                self._shown_digest = self._latest_digest
                self._sent += 1
                self._bytes += len(frame[0])
                await on_frame(frame[0], frame[1])
            await asyncio.sleep(self._interval)

    async def pump(self, on_frame, on_event=None):
        """Forward frames until the CDP socket closes."""
        watchdog = asyncio.create_task(self._first_frame_watchdog())
        sender = asyncio.create_task(self._sender(on_frame))
        try:
            await self._pump(on_frame, on_event)
        finally:
            for task in (watchdog, sender):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task

    async def _pump(self, on_frame, on_event=None):
        async for raw in self._cdp:
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            method = msg.get("method")
            if method == "Page.screencastFrame":
                params = msg["params"]
                self._meta = params.get("metadata") or {}
                self._frames += 1
                with contextlib.suppress(Exception):
                    await self._send("Page.screencastFrameAck",
                                     {"sessionId": params["sessionId"]})
                data = params.get("data", "")
                self._latest = (data, self._meta)
                self._latest_digest = hashlib.blake2b(
                    data.encode("ascii", "ignore"), digest_size=12).hexdigest()
                self._new_frame.set()
            elif method == "Page.screencastVisibilityChanged":
                if (msg.get("params") or {}).get("visible"):
                    with contextlib.suppress(Exception):
                        await self._start()
                else:
                    # Gone hidden: without this the stream is simply over.
                    await self._make_visible()
                    with contextlib.suppress(Exception):
                        await self._start()
            elif method in ("Page.frameNavigated", "Page.loadEventFired"):
                # A cross-document navigation tears the screencast down with the
                # old document, and Chrome does not tell you: the socket stays
                # open, acks keep working, and not one further frame arrives. The
                # viewer then sits frozen on the previous page for ever, which is
                # exactly when you most want to see what the agent is doing.
                # Measured: attach, navigate, 1 frame in 25 seconds. So restart it
                # on every navigation, and clear the digest so the first frame of
                # the new page is forwarded even if it happens to encode the same.
                if method == "Page.loadEventFired" or not (
                        msg.get("params") or {}).get("frame", {}).get("parentId"):
                    self._shown_digest = ""
                    await self._make_visible()
                    with contextlib.suppress(Exception):
                        await self._start()
                    asyncio.create_task(self._renudge())
                if on_event:
                    await on_event(method, msg.get("params") or {})

    # ---- input passthrough -------------------------------------------------
    def _to_page(self, nx: float, ny: float):
        """Normalised viewer coordinates back to page coordinates."""
        w = float(self._meta.get("deviceWidth") or self.max_width)
        h = float(self._meta.get("deviceHeight") or self.max_height)
        return max(0.0, min(nx, 1.0)) * w, max(0.0, min(ny, 1.0)) * h

    async def click(self, nx: float, ny: float, button: str = "left", clicks: int = 1):
        x, y = self._to_page(nx, ny)
        await self._send("Input.dispatchMouseEvent",
                         {"type": "mouseMoved", "x": x, "y": y, "button": "none"})
        for kind in ("mousePressed", "mouseReleased"):
            await self._send("Input.dispatchMouseEvent",
                             {"type": kind, "x": x, "y": y, "button": button,
                              "clickCount": clicks})

    async def scroll(self, nx: float, ny: float, dy: float):
        x, y = self._to_page(nx, ny)
        await self._send("Input.dispatchMouseEvent",
                         {"type": "mouseWheel", "x": x, "y": y,
                          "deltaX": 0, "deltaY": float(dy)})

    async def text(self, value: str):
        await self._send("Input.insertText", {"text": value[:4000]})

    async def key(self, name: str):
        codes = {"Enter": 13, "Backspace": 8, "Tab": 9, "Escape": 27,
                 "ArrowDown": 40, "ArrowUp": 38, "ArrowLeft": 37, "ArrowRight": 39}
        code = codes.get(name, 0)
        for kind in ("rawKeyDown", "keyUp"):
            await self._send("Input.dispatchKeyEvent",
                             {"type": kind, "key": name, "code": name,
                              "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code})

    async def navigate(self, url: str):
        await self._send("Page.navigate", {"url": url})

    @property
    def stats(self):
        return {"received": self._frames, "sent": self._sent,
                "kb": round(self._bytes * 0.75 / 1024, 1)}


async def bridge(client_ws, ws_url: str, quality: int = 55, interactive: bool = True):
    """Glue a FastAPI WebSocket to one tab. Returns when either side hangs up."""
    async with Screencast(ws_url, quality=quality) as cast:

        async def on_frame(data, meta):
            await client_ws.send_text(json.dumps({"t": "frame", "d": data,
                                                  "w": meta.get("deviceWidth"),
                                                  "h": meta.get("deviceHeight")}))

        async def on_event(method, params):
            url = ((params.get("frame") or {}).get("url") or "")
            await client_ws.send_text(json.dumps({"t": "nav", "url": url[:300]}))

        async def from_client():
            while True:
                raw = await client_ws.receive_text()
                if not interactive:
                    continue
                try:
                    msg = json.loads(raw)
                except Exception:
                    continue
                kind = msg.get("t")
                if kind == "click":
                    await cast.click(msg.get("x", 0), msg.get("y", 0),
                                     clicks=int(msg.get("clicks", 1)))
                elif kind == "scroll":
                    await cast.scroll(msg.get("x", 0.5), msg.get("y", 0.5),
                                      msg.get("dy", 0))
                elif kind == "text":
                    await cast.text(str(msg.get("v", "")))
                elif kind == "key":
                    await cast.key(str(msg.get("v", "")))
                elif kind == "nav":
                    await cast.navigate(str(msg.get("v", "")))

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
