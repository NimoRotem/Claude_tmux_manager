"""Run the status-poll recovery suite that lives in test_status_poll.js.

The page's JavaScript is the only thing that ever moves a session from busy back
to idle, so it is worth testing, and it is testable: the suite pulls the real
block straight out of ``app.py`` and drives it in a Node VM with a stubbed
fetch. Keeping the wrapper here means ``make test`` covers it too.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

JS_SUITE = Path(__file__).with_suffix(".js")


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_status_poll_recovery():
    proc = subprocess.run(["node", str(JS_SUITE)], capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "all 5 passed" in proc.stdout
