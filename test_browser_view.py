"""What the lite live view got wrong on the way, pinned so it cannot again.

Every case here was measured on builder2 on 2026-09-13 against a real headed
Chrome 151 on Xvfb, and every one was silent: no error, just a browser that cost a
core, a click that landed in the wrong place, or a page whose layout changed for
every agent using it.

  * Restarting the screencast on "visible" is a loop. Chrome answers every
    startScreencast with a fresh visible=true: 200 restarts in under 5 seconds,
    the watched browser at 130% of a core on a page that was not moving.
  * Acking every frame at once let Chrome encode 60 frames a second that were
    then thrown away as duplicates. Hidden on the wire, paid for on the box.
  * A 0.5 fps preset could never collect enough frames to be called animating,
    so it stayed on the screencast at 100% of a core.
  * Chrome overwrites its environment block and flattens argv, so DISPLAY is not
    in /proc/<pid>/environ and --proxy-server is not a NUL-separated argument.
  * A window manager's frame is on the bottom too: without it clicks landed 3 px
    low under fluxbox.
  * A clip-scaled captureScreenshot that is interrupted leaves the page resized.

No browser and no X server: these read the code, the tables and canned outputs.
Run: python3 -m pytest test_browser_view.py
"""
import asyncio
import json
import re
from pathlib import Path

import browser_live
import browser_view as bv
import browser_view_x as bvx
import console_browser

HERE = Path(__file__).resolve().parent


# --- the pump ----------------------------------------------------------------
class FakeCDP:
    """A CDP socket that plays back messages and records what was sent."""

    def __init__(self, messages):
        self.messages = [json.dumps(m) for m in messages]
        self.sent = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self.messages:
            raise StopAsyncIteration
        await asyncio.sleep(0)
        return self.messages.pop(0)

    def methods(self, name):
        return [m for m in self.sent if m.get("method") == name]


def _vis(visible):
    return {"method": "Page.screencastVisibilityChanged", "params": {"visible": visible}}


def _run_pump(cast, messages):
    cast._cdp = FakeCDP(messages)

    async def go():
        await cast._pump(lambda *a: asyncio.sleep(0), None)
    asyncio.run(go())
    return cast._cdp


def test_visible_events_do_not_restart_the_screencast():
    """The loop, as one assertion: fifty visible=true events, zero restarts."""
    cast = browser_live.Screencast("ws://unused")
    cdp = _run_pump(cast, [_vis(True)] * 50)
    assert cdp.methods("Page.startScreencast") == []


def test_visible_after_hidden_restarts_once():
    cast = browser_live.Screencast("ws://unused")
    cdp = _run_pump(cast, [_vis(False), _vis(True), _vis(True), _vis(True)])
    # one from the hidden handler (make visible, start), one for coming back
    assert len(cdp.methods("Page.startScreencast")) == 2


def test_the_viewer_paces_its_acks_and_the_agent_bridge_does_not():
    assert console_browser.InteractiveCast.PACE_ACKS is True
    assert bv.ViewCast.PACE_ACKS is True
    assert browser_live.Screencast.PACE_ACKS is False


def test_a_paced_frame_is_not_acked_by_the_pump():
    cast = bv.ViewCast("ws://unused")
    frame = {"method": "Page.screencastFrame",
             "params": {"sessionId": 7, "data": "AAAA", "metadata": {}}}
    cdp = _run_pump(cast, [frame])
    assert cdp.methods("Page.screencastFrameAck") == []
    assert cast._pending_ack == 7


def test_replies_reach_call_through_the_pump():
    cast = browser_live.Screencast("ws://unused")

    async def go():
        cast._cdp = FakeCDP([])
        fut = asyncio.ensure_future(cast.call("Browser.getVersion", timeout=2))
        await asyncio.sleep(0)
        ident = cast._cdp.sent[-1]["id"]
        cast._cdp.messages.append(json.dumps({"id": ident, "result": {"product": "x"}}))
        await cast._pump(lambda *a: asyncio.sleep(0), None)
        return await fut
    assert asyncio.run(go()) == {"product": "x"}


# --- choosing where pixels come from -----------------------------------------
def _cast_with_stub_send(preset="balanced"):
    cast = bv.ViewCast("ws://unused", preset=preset)
    cast.sent = []

    async def send(method, params=None):
        cast.sent.append(method)
    cast._send = send
    return cast


def test_an_idle_changing_page_moves_to_polling():
    async def go():
        cast = _cast_with_stub_send()
        for _ in range(bv.ViewCast.ANIMATING_FRAMES):
            cast.note_forwarded()
        await asyncio.sleep(0)
        return cast
    cast = asyncio.run(go())
    assert cast.source == "poll"
    assert "Page.stopScreencast" in cast.sent


def test_the_slowest_preset_can_still_be_called_animating():
    """At 0.5 fps the frames are two seconds apart; the window must hold enough."""
    slow = bv.PRESETS["light"]["fps"]
    cast = bv.ViewCast("ws://unused", preset="light")
    window = max(cast.ANIMATING_WINDOW_S, 3.5 * cast._slow)
    assert window >= (cast.ANIMATING_FRAMES - 1) / slow + 1


def test_input_brings_the_screencast_back():
    async def go():
        cast = _cast_with_stub_send()
        cast.source = "poll"
        cast._poke()
        await asyncio.sleep(0.01)
        return cast
    cast = asyncio.run(go())
    assert cast.source == "cast"
    assert "Page.startScreencast" in cast.sent


def test_changes_while_the_person_is_active_do_not_count():
    async def go():
        cast = _cast_with_stub_send()
        cast._poke()
        for _ in range(10):
            cast.note_forwarded()
        return cast
    assert asyncio.run(go()).source == "cast"


def test_nothing_here_asks_chrome_for_a_scaled_capture():
    """An interrupted clip-scaled capture leaves the page resized for everyone."""
    for name in ("browser_view.py", "app.py"):
        src = (HERE / name).read_text(encoding="utf-8")
        for m in re.finditer(r"captureScreenshot[\s\S]{0,400}", src):
            assert '"scale"' not in m.group(0), name


def test_the_debugger_session_never_enables_runtime():
    src = (HERE / "browser_view.py").read_text(encoding="utf-8")
    body = src.split("class ViewCast")[1].split("\nasync def serve")[0]
    assert "Runtime.enable" not in body.replace('"Runtime.enable" is', "")


# --- coordinates ----------------------------------------------------------------
def test_clicks_account_for_the_window_frame():
    """fluxbox on builder2: window 1919x1079 at top -19, page 1911x988."""
    cast = bv.ViewCast("ws://unused")
    cast._meta = {"deviceWidth": 1911, "deviceHeight": 988}

    async def call(method, params=None, timeout=5.0):
        return {"bounds": {"left": 0, "top": -19, "width": 1919, "height": 1079}}
    cast.call = call
    ox, oy, vw, vh = asyncio.run(cast._viewport_origin())
    assert (ox, vw, vh) == (4.0, 1911.0, 988.0)
    assert oy == -19 + 1079 - 988 - 4


def test_no_frame_no_offset():
    cast = bv.ViewCast("ws://unused")
    cast._meta = {"deviceWidth": 1919, "deviceHeight": 992}

    async def call(method, params=None, timeout=5.0):
        return {"bounds": {"left": 0, "top": 0, "width": 1919, "height": 1079}}
    cast.call = call
    ox, oy, _, _ = asyncio.run(cast._viewport_origin())
    assert (ox, oy) == (0.0, 87.0)


# --- reading a browser's process ----------------------------------------------
SS_X = """\
u_str ESTAB 0 0 @/tmp/.X11-unix/X187 179238288 * 179238287
u_str ESTAB 0 0 @/tmp/.X11-unix/X101 30726866 * 30725873
u_str ESTAB 0 0 * 179238287 * 179238288 users:(("chrome",pid=4242,fd=50))
u_str ESTAB 0 0 * 30725873 * 30726866 users:(("chrome",pid=999,fd=50))
"""


def test_display_comes_from_the_x_socket_not_the_environment(monkeypatch):
    class R:
        stdout = SS_X
    monkeypatch.setattr(bv.subprocess, "run", lambda *a, **k: R())
    assert bv._x_display_of({"4242"}) == ":187"
    assert bv._x_display_of({"999"}) == ":101"
    assert bv._x_display_of({"1"}) == ""


def test_a_visible_process_with_no_x_connection_is_headless(monkeypatch):
    monkeypatch.setattr(bv, "_listening_pids", lambda port: {"77"})
    monkeypatch.setattr(bv, "_x_display_of", lambda pids: "")
    assert bv.display_for_port(9222, fallback=99) == ""


def test_the_registry_display_is_only_a_fallback(monkeypatch):
    monkeypatch.setattr(bv, "_listening_pids", lambda port: set())
    assert bv.display_for_port(9222, fallback=99) == ":99"


def test_proxy_is_found_in_a_flattened_command_line(monkeypatch, tmp_path):
    flat = tmp_path / "cmdline"
    flat.write_bytes(b"/opt/google/chrome/chrome --user-data-dir=/p --proxy-server=http://127.0.0.1:3128 "
                     b"--no-first-run\0")
    monkeypatch.setattr(bv, "_listening_pids", lambda port: {"5"})
    real = bv.Path
    monkeypatch.setattr(bv, "Path", lambda p: flat if str(p).startswith("/proc/") else real(p))
    assert bv.proxy_for_port(9222) == "http://127.0.0.1:3128"


def test_the_egress_note_says_most_sites_go_direct(monkeypatch):
    monkeypatch.setattr(bv, "proxy_for_port", lambda port: "http://127.0.0.1:3128")
    eg = bv.egress(9222, {"enabled": True, "ladder": ["a", "b"], "only": ["linkedin.com"]})
    assert eg["egress_label"] == "relay :3128"
    assert "every other site goes direct" in eg["egress_note"]
    monkeypatch.setattr(bv, "proxy_for_port", lambda port: "")
    assert bv.egress(9222, {})["egress_kind"] == "direct"


def test_the_exit_check_asks_a_host_the_relay_carries():
    """A host off the relay's list goes direct and reads as a dead proxy."""
    conf = json.loads((Path.home() / ".claude-browser" / "proxy.json").read_text()) \
        if (Path.home() / ".claude-browser" / "proxy.json").exists() else {"only": ["api.ipify.org"]}
    host = re.search(r"https://([^/?']+)", bv.IP_JS).group(1)
    only = conf.get("only") or []
    assert not only or any(host == h or host.endswith("." + h) for h in only)


# --- the page and the routes ----------------------------------------------------
def test_named_keys_agree_between_the_two_halves():
    assert set(bv.NAMED_KEYS) == set(bvx.NAMED_KEYS)


def test_the_page_renders_with_nothing_left_to_substitute():
    html = bv.page_html("/browser/default/view", "Main <browser>", "/vnc", "/api/live")
    assert "__" not in re.sub(r"__proto__", "", html)
    assert "Main &lt;browser&gt; live" in html


def test_frames_are_not_shown_as_blob_images():
    """The dashboard's CSP is img-src 'self' data:. A blob: image is refused
    silently and the viewer shows a broken picture while frames arrive."""
    app_src = (HERE / "app.py").read_text(encoding="utf-8")
    csp = re.search(r'"img-src ([^;"]*)', app_src)
    assert csp and "blob:" not in csp.group(1)
    assert "createObjectURL" not in bv.PAGE
    assert "createImageBitmap" in bv.PAGE and '<canvas id="screen"' in bv.PAGE


def test_a_cross_site_websocket_is_refused():
    class WS:
        def __init__(self, headers):
            self.headers = headers
    assert bv.same_origin(WS({"host": "builder2.rotem.ai", "origin": "https://builder2.rotem.ai"}))
    assert not bv.same_origin(WS({"host": "builder2.rotem.ai", "origin": "https://evil.example"}))
    assert bv.same_origin(WS({"host": "builder2.rotem.ai"}))


def test_the_view_is_registered_before_the_novnc_catch_all():
    """/browser/{sid}/{path:path} matches /view too and would send it to websockify."""
    src = (HERE / "app.py").read_text(encoding="utf-8")
    mount = src.index("browser_view.mount(")
    catch_all = src.index('@app.api_route("/browser/{sid}/{path:path}"')
    assert mount < catch_all


def test_the_ladder_hands_a_person_the_lite_view():
    src = (HERE / "browser_ladder.py").read_text(encoding="utf-8")
    assert '"/browser/%s/view"' in src
    assert "vnc-start" not in src


def test_helper_rejects_an_unknown_op_without_touching_x():
    assert bvx._dispatch(None, {"op": "nope"})["ok"] is False
