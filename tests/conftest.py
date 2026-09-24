"""Shared test hooks. Every tmux server a test starts is on a socket named for this run, and is
killed here even when a test fails, so a test run never leaves a server (or an agent) behind."""
import os
import subprocess

RUN_SOCKET_PREFIX = "pytest-dash-%d-" % os.getpid()


def pytest_sessionfinish(session, exitstatus):
    base = "/tmp/tmux-%d" % os.getuid()
    try:
        names = os.listdir(base)
    except OSError:
        return
    for name in names:
        if name.startswith(RUN_SOCKET_PREFIX):
            subprocess.run(["tmux", "-L", name, "kill-server"], capture_output=True)
            try:
                os.unlink(os.path.join(base, name))     # the dead socket file
            except OSError:
                pass
