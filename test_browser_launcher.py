"""The launcher script app.py writes, exercised as a script.

It is a string inside a 30k-line module, which is exactly the kind of thing that
rots unnoticed. These tests run the real text through bash.
"""
from __future__ import annotations

import contextlib
import http.server
import os
import signal
import socket
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

import app


class _CDP(http.server.BaseHTTPRequestHandler):
    def do_GET(self):                                            # noqa: N802
        body = b'{"Browser":"Chrome/152","webSocketDebuggerUrl":"ws://x/devtools/browser/i"}'
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_a):                                  # noqa: D102
        return


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class LauncherGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        bin_dir = self.home / ".claude-browser" / "bin"
        bin_dir.mkdir(parents=True)
        #  chrome-common.sh is sourced before anything happens. A stub keeps the
        #  test to the guard and off the host's real browser stack.
        (bin_dir / "chrome-common.sh").write_text(
            'CB_ROOT="$HOME/.claude-browser"\nCB_BIN="$CB_ROOT/bin"\n'
            'CB_SCREEN_W=1920\nCB_SCREEN_H=1080\n'
            'cb_vnc_start() { :; }\ncb_chrome_env() { :; }\n'
            'cb_chrome_flags() { echo --stub; }\n')
        self.script = bin_dir / "browser-session.sh"
        self.script.write_text(app._BROWSER_LAUNCHER_SCRIPT)     # noqa: SLF001
        self.script.chmod(0o755)

    def _run(self, *args):
        env = dict(os.environ, HOME=str(self.home), PATH=os.environ["PATH"])
        return subprocess.run(["bash", str(self.script), *args], env=env,
                              capture_output=True, text=True, timeout=60)

    def test_it_refuses_a_cdp_port_that_already_answers(self):
        """Two browsers on one port do not collide loudly: one takes IPv4, the
        other IPv6, and clients then reach whichever their resolver prefers."""
        port = _free_port()
        srv = http.server.HTTPServer(("127.0.0.1", port), _CDP)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        out = self._run("start", "s7", "107", "5907", "6087", str(port))
        self.assertEqual(out.returncode, 3, out.stdout + out.stderr)
        self.assertIn("already answers CDP on %d" % port, out.stdout)

    def test_the_guard_runs_before_anything_is_created(self):
        port = _free_port()
        srv = http.server.HTTPServer(("127.0.0.1", port), _CDP)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.shutdown)
        self._run("start", "s8", "108", "5908", "6088", str(port))
        self.assertFalse((self.home / ".claude-browser" / "sessions" / "s8" / "profile").exists())

    def test_usage_is_refused_without_an_id(self):
        self.assertEqual(self._run("start").returncode, 2)


class LauncherSyncTests(unittest.TestCase):
    def test_the_on_disk_copy_is_rewritten_when_it_differs(self):
        """Write-if-missing froze old flags on disk for months. This is the
        write-if-changed behaviour that replaced it."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "bin" / "browser-session.sh"
            original = app.BROWSER_LAUNCHER
            app.BROWSER_LAUNCHER = str(target)
            try:
                target.parent.mkdir(parents=True)
                target.write_text("stale\n")
                app._ensure_browser_launcher()                    # noqa: SLF001
                self.assertEqual(target.read_text(), app._BROWSER_LAUNCHER_SCRIPT)  # noqa: SLF001
                self.assertTrue(os.access(target, os.X_OK))
            finally:
                app.BROWSER_LAUNCHER = original


class LauncherHappyPathTests(LauncherGuardTests):
    """A free port must still start. A guard that refuses everything is not a guard."""

    def setUp(self):
        super().setUp()
        self.stub_bin = self.home / "stub-bin"
        self.stub_bin.mkdir()
        for name in ("Xvfb", "fluxbox", "google-chrome-stable", "dbus-run-session",
                     "x11vnc", "websockify"):
            p = self.stub_bin / name
            #  Xvfb has to leave the socket the script waits 10 s for.
            body = ('#!/bin/sh\nmkdir -p /tmp/.X11-unix; touch "/tmp/.X11-unix/X107"; '
                    'sleep 30\n') if name == "Xvfb" else "#!/bin/sh\nsleep 30\n"
            p.write_text(body)
            p.chmod(0o755)

    def _run(self, *args):
        env = dict(os.environ, HOME=str(self.home),
                   PATH="%s:%s" % (self.stub_bin, os.environ["PATH"]))
        return subprocess.run(["bash", str(self.script), *args], env=env,
                              capture_output=True, text=True, timeout=90)

    def _kill_started(self, sid: str):
        """By the pids the launcher wrote, never by a pattern: a broad pkill on
        these boxes has taken out unrelated agent sessions."""
        pid_file = self.home / ".claude-browser" / "sessions" / sid / "pids"
        for line in pid_file.read_text().split() if pid_file.exists() else []:
            with contextlib.suppress(Exception):
                os.kill(int(line), signal.SIGTERM)

    def test_a_free_port_starts(self):
        port = _free_port()
        self.addCleanup(self._kill_started, "s7")
        self.addCleanup(lambda: Path("/tmp/.X11-unix/X107").unlink(missing_ok=True))
        out = self._run("start", "s7", "107", "5907", "6087", str(port))
        self.assertEqual(out.returncode, 0, out.stdout + out.stderr)
        self.assertIn("started session s7", out.stdout)
        self.assertTrue((self.home / ".claude-browser" / "sessions" / "s7" / "profile").exists())


if __name__ == "__main__":
    unittest.main()
