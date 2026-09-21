"""The X half of the lite browser view: real input, the real clipboard, a still of
the whole screen. One small process per X display, spoken to over stdin/stdout.

WHY A SEPARATE PROCESS. Xlib's default I/O error handler calls exit(). If this
ran inside the dashboard, an Xvfb that died while a viewer was open would take the
whole dashboard down with it, every user at once. Out here it takes one helper,
and the viewer falls back to CDP input on the next event.

WHY X INPUT AND NOT CDP INPUT. The top rung of the ladder is a person solving a
challenge in a real headed Chrome on a residential exit. `Input.dispatchMouseEvent`
works, but it arrives through the debugger, and the challenge pages that score a
debugger are exactly the pages a human is called in for. XTEST is what x11vnc
itself uses: the event enters the X server, Chrome receives it from the OS like
any keyboard or mouse, and nothing in the page can tell it from a person at a desk.

WHY THE X CLIPBOARD. noVNC's copy and paste goes through a side panel, Latin-1
only, and in practice nobody finds it. Owning the X CLIPBOARD selection and then
pressing ctrl-V fires a genuine paste event in the page, so every site's own paste
handler runs, rich editors included. Copy is the reverse: ctrl-C in Chrome, then
ask the X server for what Chrome put there.

Protocol, one JSON object per line.
  in : {"op": "move", "x": 10, "y": 20}            fire and forget
       {"id": 7, "op": "clip_get"}                  anything with an id is answered
  out: {"ready": true, "w": 1920, "h": 1080}        once, first
       {"id": 7, "ok": true, "text": "..."}
Needs only libX11 and libXtst, which every box that runs Chrome already has, and
Pillow for the screen grab.
"""
import base64
import ctypes
import ctypes.util
import hashlib
import io
import json
import os
import select
import sys
import time

c_ulong, c_int, c_uint, c_void_p, c_char_p = (ctypes.c_ulong, ctypes.c_int, ctypes.c_uint,
                                             ctypes.c_void_p, ctypes.c_char_p)

# ---------------------------------------------------------------------------
# libX11 / libXtst
# ---------------------------------------------------------------------------
_X = None
_XT = None


def _lib(name, fallback):
    return ctypes.CDLL(ctypes.util.find_library(name) or fallback)


def _bind():
    global _X, _XT
    if _X is not None:
        return
    x = _lib("X11", "libX11.so.6")
    t = _lib("Xtst", "libXtst.so.6")
    sig = {
        "XOpenDisplay": (c_void_p, [c_char_p]),
        "XDefaultRootWindow": (c_ulong, [c_void_p]),
        "XDefaultScreen": (c_int, [c_void_p]),
        "XDisplayWidth": (c_int, [c_void_p, c_int]),
        "XDisplayHeight": (c_int, [c_void_p, c_int]),
        "XConnectionNumber": (c_int, [c_void_p]),
        "XPending": (c_int, [c_void_p]),
        "XNextEvent": (c_int, [c_void_p, c_void_p]),
        "XFlush": (c_int, [c_void_p]),
        "XSync": (c_int, [c_void_p, c_int]),
        "XInternAtom": (c_ulong, [c_void_p, c_char_p, c_int]),
        "XCreateSimpleWindow": (c_ulong, [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint,
                                          c_uint, c_ulong, c_ulong]),
        "XSelectInput": (c_int, [c_void_p, c_ulong, ctypes.c_long]),
        "XSetSelectionOwner": (c_int, [c_void_p, c_ulong, c_ulong, c_ulong]),
        "XGetSelectionOwner": (c_ulong, [c_void_p, c_ulong]),
        "XConvertSelection": (c_int, [c_void_p, c_ulong, c_ulong, c_ulong, c_ulong, c_ulong]),
        "XGetWindowProperty": (c_int, [c_void_p, c_ulong, c_ulong, ctypes.c_long, ctypes.c_long,
                                       c_int, c_ulong, ctypes.POINTER(c_ulong),
                                       ctypes.POINTER(c_int), ctypes.POINTER(c_ulong),
                                       ctypes.POINTER(c_ulong),
                                       ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte))]),
        "XChangeProperty": (c_int, [c_void_p, c_ulong, c_ulong, c_ulong, c_int, c_int,
                                    c_void_p, c_int]),
        "XDeleteProperty": (c_int, [c_void_p, c_ulong, c_ulong]),
        "XSendEvent": (c_int, [c_void_p, c_ulong, c_int, ctypes.c_long, c_void_p]),
        "XFree": (c_int, [c_void_p]),
        "XKeysymToKeycode": (ctypes.c_ubyte, [c_void_p, c_ulong]),
        "XStringToKeysym": (c_ulong, [c_char_p]),
        "XkbKeycodeToKeysym": (c_ulong, [c_void_p, c_uint, c_int, c_int]),
        "XGetImage": (c_void_p, [c_void_p, c_ulong, c_int, c_int, c_uint, c_uint, c_ulong, c_int]),
        "XDestroyImage": (c_int, [c_void_p]),
        "XSetErrorHandler": (c_void_p, [c_void_p]),
    }
    for name, (res, args) in sig.items():
        fn = getattr(x, name)
        fn.restype, fn.argtypes = res, args
    for name, (res, args) in {
        "XTestQueryExtension": (c_int, [c_void_p, ctypes.POINTER(c_int), ctypes.POINTER(c_int),
                                        ctypes.POINTER(c_int), ctypes.POINTER(c_int)]),
        "XTestFakeMotionEvent": (c_int, [c_void_p, c_int, c_int, c_int, c_ulong]),
        "XTestFakeButtonEvent": (c_int, [c_void_p, c_uint, c_int, c_ulong]),
        "XTestFakeKeyEvent": (c_int, [c_void_p, c_uint, c_int, c_ulong]),
    }.items():
        fn = getattr(t, name)
        fn.restype, fn.argtypes = res, args
    _X, _XT = x, t


# Event layouts (LP64). XEvent is a union padded to 24 longs.
class XEvent(ctypes.Union):
    _fields_ = [("type", c_int), ("pad", ctypes.c_long * 24)]


class XSelectionRequestEvent(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int),
                ("display", c_void_p), ("owner", c_ulong), ("requestor", c_ulong),
                ("selection", c_ulong), ("target", c_ulong), ("property", c_ulong),
                ("time", c_ulong)]


class XSelectionEvent(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int),
                ("display", c_void_p), ("requestor", c_ulong), ("selection", c_ulong),
                ("target", c_ulong), ("property", c_ulong), ("time", c_ulong)]


class XPropertyEvent(ctypes.Structure):
    _fields_ = [("type", c_int), ("serial", c_ulong), ("send_event", c_int),
                ("display", c_void_p), ("window", c_ulong), ("atom", c_ulong),
                ("time", c_ulong), ("state", c_int)]


class XImage(ctypes.Structure):
    _fields_ = [("width", c_int), ("height", c_int), ("xoffset", c_int), ("format", c_int),
                ("data", c_void_p), ("byte_order", c_int), ("bitmap_unit", c_int),
                ("bitmap_bit_order", c_int), ("bitmap_pad", c_int), ("depth", c_int),
                ("bytes_per_line", c_int), ("bits_per_pixel", c_int)]


PROPERTY_NOTIFY, SELECTION_CLEAR, SELECTION_REQUEST, SELECTION_NOTIFY = 28, 29, 30, 31
PROPERTY_CHANGE_MASK = 1 << 22
XA_ATOM, XA_STRING = 4, 31
Z_PIXMAP = 2
ALL_PLANES = (1 << 64) - 1

# Browser `KeyboardEvent.key` names to X keysym names. Anything one character long
# is typed as that character instead, see Display.char().
NAMED_KEYS = {
    "Enter": "Return", "Backspace": "BackSpace", "Tab": "Tab", "Escape": "Escape",
    "Delete": "Delete", "Insert": "Insert", "Home": "Home", "End": "End",
    "PageUp": "Prior", "PageDown": "Next",
    "ArrowLeft": "Left", "ArrowRight": "Right", "ArrowUp": "Up", "ArrowDown": "Down",
    "Shift": "Shift_L", "Control": "Control_L", "Alt": "Alt_L", "Meta": "Super_L",
    "CapsLock": "Caps_Lock", "ContextMenu": "Menu",
    **{"F%d" % n: "F%d" % n for n in range(1, 13)},
}

# The most text one paste will carry. Chrome asks for the whole selection in one
# property and this helper does not speak INCR on the serving side.
MAX_CLIP_BYTES = 1 << 20


class Display:
    def __init__(self, name: str):
        _bind()
        # A protocol error (a window that vanished between two calls, say) must be
        # logged, not fatal. The default handler exits the process.
        self._on_error = ctypes.CFUNCTYPE(c_int, c_void_p, c_void_p)(lambda d, e: 0)
        _X.XSetErrorHandler(ctypes.cast(self._on_error, c_void_p))
        self.dpy = _X.XOpenDisplay(name.encode())
        if not self.dpy:
            raise RuntimeError("cannot open display %s" % name)
        scr = _X.XDefaultScreen(self.dpy)
        self.root = _X.XDefaultRootWindow(self.dpy)
        self.w = _X.XDisplayWidth(self.dpy, scr)
        self.h = _X.XDisplayHeight(self.dpy, scr)
        a, b, c, d = c_int(), c_int(), c_int(), c_int()
        self.xtest = bool(_XT.XTestQueryExtension(self.dpy, ctypes.byref(a), ctypes.byref(b),
                                                  ctypes.byref(c), ctypes.byref(d)))
        self.win = _X.XCreateSimpleWindow(self.dpy, self.root, 0, 0, 1, 1, 0, 0, 0)
        _X.XSelectInput(self.dpy, self.win, PROPERTY_CHANGE_MASK)
        atom = lambda s: _X.XInternAtom(self.dpy, s.encode(), 0)       # noqa: E731
        self.CLIPBOARD, self.UTF8 = atom("CLIPBOARD"), atom("UTF8_STRING")
        self.TARGETS, self.TEXT, self.INCR = atom("TARGETS"), atom("TEXT"), atom("INCR")
        self.TEXT_PLAIN_UTF8 = atom("text/plain;charset=utf-8")
        self.TEXT_PLAIN = atom("text/plain")
        self.PROP = atom("LITEVIEW_CLIP")
        self.clip = b""                 # what we serve while we own CLIPBOARD
        self.held = {}                  # keysym name -> keycode, for keys pressed and not released
        self.buttons = set()
        self._last_grab = ""
        _X.XFlush(self.dpy)

    # ---- input ---------------------------------------------------------------
    def move(self, x, y):
        _XT.XTestFakeMotionEvent(self.dpy, -1, int(x), int(y), 0)

    def button(self, b, down):
        b = int(b)
        _XT.XTestFakeButtonEvent(self.dpy, b, 1 if down else 0, 0)
        (self.buttons.add if down else self.buttons.discard)(b)

    def wheel(self, x, y, notches_y=0, notches_x=0):
        self.move(x, y)
        for count, up, down in ((notches_y, 4, 5), (notches_x, 6, 7)):
            b = up if count > 0 else down
            for _ in range(min(abs(int(count)), 20)):
                _XT.XTestFakeButtonEvent(self.dpy, b, 1, 0)
                _XT.XTestFakeButtonEvent(self.dpy, b, 0, 0)

    def _keycode(self, keysym_name):
        ks = _X.XStringToKeysym(keysym_name.encode())
        return _X.XKeysymToKeycode(self.dpy, ks) if ks else 0

    def key(self, name, down):
        """A named key, pressed or released. Returns False if X has no such key."""
        sym = NAMED_KEYS.get(name)
        if not sym:
            return False
        if down:
            kc = self._keycode(sym)
            if not kc:
                return False
            _XT.XTestFakeKeyEvent(self.dpy, kc, 1, 0)
            self.held[sym] = kc
        else:
            kc = self.held.pop(sym, 0) or self._keycode(sym)
            if kc:
                _XT.XTestFakeKeyEvent(self.dpy, kc, 0, 0)
        return True

    def _shift_held(self):
        return [kc for sym, kc in self.held.items() if sym.startswith("Shift")]

    def char(self, ch):
        """Type one printable ASCII character with a real key.

        Uppercase and punctuation need Shift, and Shift may already be down because
        the person is holding it. So Shift is pressed only when it is needed and not
        held, and released for the one key when it is held and not wanted. Returns
        False for anything the keymap has no key for; the caller inserts that as
        text instead."""
        if len(ch) != 1 or not (0x20 <= ord(ch) < 0x7f):
            return False
        ks = ord(ch)
        kc = _X.XKeysymToKeycode(self.dpy, ks)
        if not kc:
            return False
        need_shift = _X.XkbKeycodeToKeysym(self.dpy, kc, 0, 0) != ks
        if need_shift and _X.XkbKeycodeToKeysym(self.dpy, kc, 0, 1) != ks:
            return False
        held = self._shift_held()
        shift_kc = self._keycode("Shift_L")
        if need_shift and not held:
            _XT.XTestFakeKeyEvent(self.dpy, shift_kc, 1, 0)
        elif held and not need_shift:
            for k in held:
                _XT.XTestFakeKeyEvent(self.dpy, k, 0, 0)
        _XT.XTestFakeKeyEvent(self.dpy, kc, 1, 0)
        _XT.XTestFakeKeyEvent(self.dpy, kc, 0, 0)
        if need_shift and not held:
            _XT.XTestFakeKeyEvent(self.dpy, shift_kc, 0, 0)
        elif held and not need_shift:
            for k in held:
                _XT.XTestFakeKeyEvent(self.dpy, k, 1, 0)
        return True

    def chord(self, letter):
        """ctrl+<letter>, leaving Control exactly as the person has it."""
        had = "Control_L" in self.held or "Control_R" in self.held
        ctrl = self._keycode("Control_L")
        if not had:
            _XT.XTestFakeKeyEvent(self.dpy, ctrl, 1, 0)
        kc = self._keycode(letter)
        _XT.XTestFakeKeyEvent(self.dpy, kc, 1, 0)
        _XT.XTestFakeKeyEvent(self.dpy, kc, 0, 0)
        if not had:
            _XT.XTestFakeKeyEvent(self.dpy, ctrl, 0, 0)

    def release_all(self):
        """Let go of everything. A viewer that loses focus with ctrl held never
        sends the keyup, and a Control stuck down in X turns every later click into
        ctrl-click, which opens links in new tabs."""
        for sym, kc in list(self.held.items()):
            _XT.XTestFakeKeyEvent(self.dpy, kc, 0, 0)
        self.held.clear()
        for b in list(self.buttons):
            _XT.XTestFakeButtonEvent(self.dpy, b, 0, 0)
        self.buttons.clear()

    # ---- clipboard -----------------------------------------------------------
    def clip_set(self, text):
        self.clip = text.encode("utf-8")[:MAX_CLIP_BYTES]
        _X.XSetSelectionOwner(self.dpy, self.CLIPBOARD, self.win, 0)
        return _X.XGetSelectionOwner(self.dpy, self.CLIPBOARD) == self.win

    def _serve(self, ev):
        req = ctypes.cast(ctypes.byref(ev), ctypes.POINTER(XSelectionRequestEvent)).contents
        prop = req.property or req.target
        if req.target == self.TARGETS:
            atoms = (c_ulong * 5)(self.TARGETS, self.UTF8, self.TEXT_PLAIN_UTF8,
                                  XA_STRING, self.TEXT)
            _X.XChangeProperty(self.dpy, req.requestor, prop, XA_ATOM, 32, 0,
                               ctypes.cast(atoms, c_void_p), 5)
        elif req.target in (self.UTF8, self.TEXT_PLAIN_UTF8, self.TEXT_PLAIN):
            buf = ctypes.create_string_buffer(self.clip, len(self.clip))
            _X.XChangeProperty(self.dpy, req.requestor, prop, req.target, 8, 0,
                               ctypes.cast(buf, c_void_p), len(self.clip))
        elif req.target in (XA_STRING, self.TEXT):
            data = self.clip.decode("utf-8", "replace").encode("latin-1", "replace")
            buf = ctypes.create_string_buffer(data, len(data))
            _X.XChangeProperty(self.dpy, req.requestor, prop, XA_STRING, 8, 0,
                               ctypes.cast(buf, c_void_p), len(data))
        else:
            prop = 0
        note = XEvent()
        sel = ctypes.cast(ctypes.byref(note), ctypes.POINTER(XSelectionEvent)).contents
        sel.type, sel.requestor, sel.selection = SELECTION_NOTIFY, req.requestor, req.selection
        sel.target, sel.property, sel.time = req.target, prop, req.time
        _X.XSendEvent(self.dpy, req.requestor, 0, 0, ctypes.byref(note))
        _X.XFlush(self.dpy)

    def _read_prop(self, delete=True):
        typ, fmt, n, after = c_ulong(), c_int(), c_ulong(), c_ulong()
        data = ctypes.POINTER(ctypes.c_ubyte)()
        _X.XGetWindowProperty(self.dpy, self.win, self.PROP, 0, MAX_CLIP_BYTES * 4, int(delete),
                              0, ctypes.byref(typ), ctypes.byref(fmt), ctypes.byref(n),
                              ctypes.byref(after), ctypes.byref(data))
        raw = b""
        if data:
            if fmt.value == 8:
                raw = ctypes.string_at(data, n.value)
            _X.XFree(data)
        return typ.value, raw

    def clip_get(self, timeout=1.5):
        owner = _X.XGetSelectionOwner(self.dpy, self.CLIPBOARD)
        if not owner:
            return ""
        if owner == self.win:
            return self.clip.decode("utf-8", "replace")
        for target in (self.UTF8, XA_STRING):
            _X.XDeleteProperty(self.dpy, self.win, self.PROP)
            _X.XConvertSelection(self.dpy, self.CLIPBOARD, target, self.PROP, self.win, 0)
            _X.XFlush(self.dpy)
            got = self.wait_for(lambda ev: ev.type == SELECTION_NOTIFY, timeout)
            if not got:
                return ""
            sel = ctypes.cast(ctypes.byref(got), ctypes.POINTER(XSelectionEvent)).contents
            if not sel.property:
                continue
            typ, raw = self._read_prop()
            if typ == self.INCR:
                raw = self._read_incr(timeout)
            enc = "utf-8" if target == self.UTF8 else "latin-1"
            return raw.decode(enc, "replace")
        return ""

    def _read_incr(self, timeout):
        """A large clipboard arrives in pieces: each new value of the property is the
        next chunk, and an empty one ends it."""
        chunks, total = [], 0
        while total < MAX_CLIP_BYTES:
            got = self.wait_for(lambda ev: ev.type == PROPERTY_NOTIFY and ctypes.cast(
                ctypes.byref(ev), ctypes.POINTER(XPropertyEvent)).contents.state == 0, timeout)
            if not got:
                break
            _, raw = self._read_prop()
            if not raw:
                break
            chunks.append(raw)
            total += len(raw)
        return b"".join(chunks)

    def wait_for(self, match, timeout):
        """Handle X events until one matches or time runs out. Events that do not
        match are still handled, so a paste request arriving while we wait for a
        copy is answered rather than dropped."""
        deadline = time.time() + timeout
        fd = _X.XConnectionNumber(self.dpy)
        while True:
            while _X.XPending(self.dpy):
                ev = XEvent()
                _X.XNextEvent(self.dpy, ctypes.byref(ev))
                if match(ev):
                    return ev
                self.handle(ev)
            left = deadline - time.time()
            if left <= 0:
                return None
            select.select([fd], [], [], left)

    def settle(self, seconds):
        self.wait_for(lambda ev: False, seconds)

    def copy(self, letter="c"):
        """ctrl-C (or ctrl-X) in the browser, then read what it put on the clipboard.

        Chrome takes the selection asynchronously, so reading straight after the key
        returns the PREVIOUS clipboard. Wait for the owner to change, or a short
        fixed time when Chrome already owned it (a second copy does not change the
        owner)."""
        before = _X.XGetSelectionOwner(self.dpy, self.CLIPBOARD)
        self.chord("x" if letter == "x" else "c")
        _X.XFlush(self.dpy)
        deadline = time.time() + 0.6
        self.settle(0.12)
        while time.time() < deadline and _X.XGetSelectionOwner(self.dpy, self.CLIPBOARD) in (
                0, self.win) and before in (0, self.win):
            self.settle(0.05)
        return self.clip_get()

    def handle(self, ev):
        if ev.type == SELECTION_REQUEST:
            self._serve(ev)
        elif ev.type == SELECTION_CLEAR:
            self.clip = b""

    # ---- the whole screen ----------------------------------------------------
    def grab(self, max_w=1280, max_h=800, quality=50, force=False):
        """A JPEG of the entire X screen: tab strip, address bar, native dialogs,
        the open <select> menu the page screencast cannot see. Returns None when
        nothing changed since the last grab, so a static screen costs a hash."""
        from PIL import Image
        ptr = _X.XGetImage(self.dpy, self.root, 0, 0, self.w, self.h, ALL_PLANES, Z_PIXMAP)
        if not ptr:
            return None
        try:
            img = ctypes.cast(ptr, ctypes.POINTER(XImage)).contents
            if img.bits_per_pixel != 32:
                return None
            size = img.bytes_per_line * img.height
            raw = ctypes.string_at(img.data, size)
            digest = hashlib.blake2b(raw, digest_size=12).hexdigest()
            if digest == self._last_grab and not force:
                return None
            self._last_grab = digest
            pic = Image.frombuffer("RGB", (img.width, img.height), raw, "raw", "BGRX",
                                   img.bytes_per_line, 1)
        finally:
            _X.XDestroyImage(ptr)
        scale = min(1.0, float(max_w) / pic.width, float(max_h) / pic.height)
        if scale < 1.0:
            pic = pic.resize((max(1, int(pic.width * scale)), max(1, int(pic.height * scale))),
                             Image.BILINEAR)
        out = io.BytesIO()
        pic.save(out, "JPEG", quality=int(quality))
        return base64.b64encode(out.getvalue()).decode("ascii")


# ---------------------------------------------------------------------------
# the loop
# ---------------------------------------------------------------------------
def _dispatch(d: Display, msg: dict):
    op = msg.get("op")
    if op == "move":
        d.move(msg["x"], msg["y"])
    elif op == "button":
        d.move(msg["x"], msg["y"])
        d.button(msg.get("b", 1), bool(msg.get("down")))
    elif op == "wheel":
        d.wheel(msg["x"], msg["y"], msg.get("ny", 0), msg.get("nx", 0))
    elif op == "key":
        return {"ok": d.key(msg.get("key", ""), bool(msg.get("down")))}
    elif op == "char":
        return {"ok": d.char(msg.get("ch", ""))}
    elif op == "release_all":
        d.release_all()
    elif op == "paste":
        ok = d.clip_set(str(msg.get("text", "")))
        if ok:
            d.chord("v")
        return {"ok": ok}
    elif op == "clip_set":
        return {"ok": d.clip_set(str(msg.get("text", "")))}
    elif op == "clip_get":
        return {"ok": True, "text": d.clip_get()}
    elif op == "copy":
        return {"ok": True, "text": d.copy("x" if msg.get("cut") else "c")}
    elif op == "grab":
        jpeg = d.grab(msg.get("w", 1280), msg.get("h", 800), msg.get("q", 50),
                      bool(msg.get("force")))
        return {"ok": True, "jpeg": jpeg, "w": d.w, "h": d.h}
    elif op == "info":
        return {"ok": True, "w": d.w, "h": d.h, "xtest": d.xtest}
    else:
        return {"ok": False, "error": "unknown op %r" % op}
    return None


def main(display: str) -> int:
    out = sys.stdout
    try:
        d = Display(display)
    except Exception as e:                                         # noqa: BLE001
        out.write(json.dumps({"ready": False, "error": str(e)}) + "\n")
        out.flush()
        return 1
    out.write(json.dumps({"ready": True, "w": d.w, "h": d.h, "xtest": d.xtest}) + "\n")
    out.flush()
    xfd = _X.XConnectionNumber(d.dpy)
    buf = b""
    while True:
        while _X.XPending(d.dpy):
            ev = XEvent()
            _X.XNextEvent(d.dpy, ctypes.byref(ev))
            d.handle(ev)
        ready, _, _ = select.select([0, xfd], [], [], 30)
        if 0 not in ready:
            continue
        chunk = os.read(0, 1 << 16)
        if not chunk:                       # the dashboard went away: so do we
            d.release_all()
            _X.XFlush(d.dpy)
            return 0
        buf += chunk
        while b"\n" in buf:
            line, buf = buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except Exception:
                continue
            try:
                res = _dispatch(d, msg)
            except Exception as e:                                 # noqa: BLE001
                res = {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
            _X.XFlush(d.dpy)
            if msg.get("id") is not None:
                reply = dict(res or {"ok": True})
                reply["id"] = msg["id"]
                out.write(json.dumps(reply) + "\n")
                out.flush()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else os.environ.get("DISPLAY", ":0")))
