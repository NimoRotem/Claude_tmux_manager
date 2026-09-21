"""A username is matched case insensitively, everywhere it is matched.

THE DEFECT THIS CATCHES: usernames are already unique case insensitively, because
the create path lowercases a name before deciding whether it is taken. Login then
compared case SENSITIVELY, so one real account written as "Nimo" on one box and
"nimo" on another could simply fail to be found, and the box answered as though
the account did not exist. builder1 carried the fix first; this pins it for every
box now that they all run one build.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

import app


def test_a_username_is_found_whatever_case_it_was_typed_in(monkeypatch):
    monkeypatch.setattr(app, "_load_users", lambda: [{"id": "u1", "username": "Nimo"}])
    for typed in ("Nimo", "nimo", "NIMO", "nImO"):
        found = app._find_user_by_username(typed)
        assert found and found["id"] == "u1", typed


def test_a_stored_username_in_any_case_still_matches(monkeypatch):
    monkeypatch.setattr(app, "_load_users", lambda: [{"id": "u1", "username": "NIMO"}])
    assert app._find_user_by_username("nimo")["id"] == "u1"


def test_a_different_name_is_still_a_miss(monkeypatch):
    monkeypatch.setattr(app, "_load_users", lambda: [{"id": "u1", "username": "Nimo"}])
    assert app._find_user_by_username("nimo2") is None
    assert app._find_user_by_username("") is None


def test_a_record_with_no_username_does_not_crash_the_lookup(monkeypatch):
    monkeypatch.setattr(app, "_load_users", lambda: [{"id": "u1"}, {"id": "u2", "username": "Nimo"}])
    assert app._find_user_by_username("nimo")["id"] == "u2"
