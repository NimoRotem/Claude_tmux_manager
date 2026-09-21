from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import time
import unittest

import browser_resource_guard as guard


def _stat(
    pid: int,
    ppid: int,
    *,
    pgrp: int = 10,
    session: int = 10,
    start: int = 100,
    state: str = "S",
) -> str:
    # Fields begin at Linux proc field 3 after the closing parenthesis.  Pad
    # through field 22 (starttime), whose zero-based index is 19 here.
    fields = [state, str(ppid), str(pgrp), str(session)] + ["0"] * 15 + [str(start)]
    return f"{pid} (test process) " + " ".join(fields) + "\n"


class BrowserResourceGuardTests(unittest.TestCase):
    def _proc(self, rows):
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        for pid, ppid, command in rows:
            base = root / str(pid)
            base.mkdir()
            (base / "stat").write_text(_stat(pid, ppid, start=pid * 10))
            (base / "cmdline").write_bytes(command.replace(" ", "\0").encode())
            (base / "uid").write_text("1001")
            (base / "cgroup").write_text("0::/user.slice\n")
        self.addCleanup(tmp.cleanup)
        return root

    def test_scans_only_top_level_browser_processes(self):
        proc = self._proc([
            (10, 1, "/opt/google/chrome/chrome --user-data-dir=/tmp/a --remote-debugging-port=9333 --headless=new"),
            (11, 10, "/opt/google/chrome/chrome --type=renderer --user-data-dir=/tmp/a"),
            (12, 1, "node browser_gate.js"),
            (13, 1, "/opt/google/chrome/chrome_crashpad_handler --database=/tmp/crashes"),
        ])
        rows = guard.browser_roots(guard.snapshot_processes(proc), protected_roots=("/safe",))
        self.assertEqual([row.process.pid for row in rows], [10])
        self.assertEqual(rows[0].profile, "/tmp/a")
        self.assertEqual(rows[0].cdp_port, 9333)
        self.assertTrue(rows[0].headless)

    def test_protects_dashboard_profile_subtrees(self):
        proc = self._proc([
            (10, 1, "/opt/google/chrome/chrome --user-data-dir=/home/u/.claude-browser/profile"),
            (20, 1, "/opt/google/chrome/chrome --user-data-dir=/home/u/.claude-browser/sessions/s2/profile"),
            (30, 1, "/opt/google/chrome/chrome --user-data-dir=/tmp/agent/profile"),
        ])
        rows = guard.browser_roots(
            guard.snapshot_processes(proc),
            protected_roots=("/home/u/.claude-browser/profile", "/home/u/.claude-browser/sessions"),
        )
        self.assertEqual([row.protected for row in rows], [True, True, False])

    def test_descendants_are_deepest_first_and_exclude_root(self):
        proc = self._proc([
            (10, 1, "bash"),
            (11, 10, "node worker"),
            (12, 11, "chrome"),
            (13, 10, "sleep"),
        ])
        snapshot = guard.snapshot_processes(proc)
        ordered = guard.descendant_pids([10], snapshot)
        self.assertEqual(ordered[0], 12)
        self.assertEqual(set(ordered), {11, 12, 13})
        self.assertNotIn(10, ordered)

    def test_workload_root_climbs_gate_wrappers_but_not_pane_shell(self):
        proc = self._proc([
            (10, 1, "bash"),
            (11, 10, "bash -c timeout node browser_gate_v61.js"),
            (12, 11, "timeout 1d node browser_gate_v61.js"),
            (13, 12, "node browser_gate_v61.js"),
            (14, 13, "/opt/google/chrome/chrome --user-data-dir=/tmp/gate"),
        ])
        snapshot = guard.snapshot_processes(proc)
        self.assertEqual(guard._workload_root(14, snapshot), 11)

    def test_generic_playwright_parent_is_a_workload_boundary(self):
        proc = self._proc([
            (10, 1, "claude"),
            (11, 10, "node /app/playwright/driver.js"),
            (12, 11, "/opt/google/chrome/chrome --user-data-dir=/tmp/pw"),
        ])
        snapshot = guard.snapshot_processes(proc)
        self.assertEqual(guard._workload_root(12, snapshot), 11)

    def test_pid_identity_includes_start_ticks(self):
        proc = self._proc([(10, 1, "sleep")])
        row = guard.snapshot_processes(proc)[10]
        self.assertTrue(guard._same_process(row, proc))
        (proc / "10" / "stat").write_text(_stat(10, 1, start=100, state="Z"))
        self.assertFalse(guard._same_process(row, proc))
        (proc / "10" / "stat").write_text(_stat(10, 1, start=999))
        self.assertFalse(guard._same_process(row, proc))

    @unittest.skipUnless(os.environ.get("RUN_BROWSER_GUARD_INTEGRATION") == "1", "integration test")
    def test_terminates_root_owned_descendants(self):
        child = subprocess.Popen(["sudo", "-n", "sh", "-c", "sleep 300 & wait"])
        try:
            deadline = time.monotonic() + 3
            descendants = []
            while time.monotonic() < deadline:
                snapshot = guard.snapshot_processes()
                descendants = guard.descendant_pids([os.getpid()], snapshot)
                if child.pid in descendants and any(snapshot[pid].uid == 0 for pid in descendants):
                    break
                time.sleep(0.05)
            self.assertIn(child.pid, descendants)
            self.assertTrue(any(snapshot[pid].uid == 0 for pid in descendants))
            result = guard.terminate_descendants([os.getpid()], grace_s=0.5)
            self.assertTrue(result["ok"], result)
            child.wait(timeout=3)
            self.assertEqual(guard.descendant_pids([os.getpid()], guard.snapshot_processes()), [])
        finally:
            if child.poll() is None:
                subprocess.run(["sudo", "-n", "/bin/kill", "-KILL", "--", str(child.pid)],
                               capture_output=True, timeout=3)


if __name__ == "__main__":
    unittest.main()
