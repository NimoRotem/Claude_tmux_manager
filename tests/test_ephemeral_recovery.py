"""Durable recovery leaves the dashboard's own scaffolding alone.

THE DEFECT THIS CATCHES: the dashboard opens short-lived tmux sessions of its own,
`_authsetup` for the token flow and `prime_<pid>` to accept the bypass warning in a
fresh config dir. Recovery recreates whatever it holds a row for, so a scaffold
session that got checkpointed came back from the dead every 20 seconds after its
owner killed it, and on a box where the recreate could not succeed it wrote a page
of errors into the log on the same cycle instead. claude.lisa.my had this fix and
the other five boxes did not.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

import app


def test_the_dashboards_own_scaffolding_is_ephemeral():
    assert app._is_ephemeral_session("_authsetup")
    assert app._is_ephemeral_session("prime_12345")
    assert app._is_ephemeral_session(app.PRIME_SESSION_PREFIX + "1")


def test_a_real_tab_is_not():
    for name in ("jevip", "crmticket", "builder4", "primer", "authprobe-clean"):
        assert not app._is_ephemeral_session(name), name


def test_the_prime_script_and_the_recovery_guard_agree_on_the_prefix():
    #  The script names its session `prime_$$`. If someone renames one without the
    #  other, recovery silently starts adopting scaffolding again.
    assert 'S="%s$$"' % app.PRIME_SESSION_PREFIX in app._PRIME_SCRIPT


def test_recovery_never_offers_a_scaffold_session_as_a_candidate(monkeypatch):
    row = {"managed": True, "desired_state": "running", "restore_on_startup": True}

    class _Store:
        @staticmethod
        def snapshot():
            return {"sessions": {"realtab": dict(row), "_authsetup": dict(row),
                                 "prime_999": dict(row)}}

    monkeypatch.setattr(app, "SESSION_LIFECYCLE", _Store)
    names = [c["name"] for c in app._durable_session_candidates(set())]
    assert names == ["realtab"]
