"""Uploads die with their session, and orphaned upload dirs are swept after 14 days.

THE DEFECT THIS CATCHES: files uploaded into a session stayed in
~/.tmux-dashboard/uploads/ forever after the session was deleted (287 MB and 111
dirs on builder1, 2026-09-23). The sweep must never take a LIVE session's files,
including a durable session restored under a new #{session_created}, and must
delete nothing when tmux cannot be asked who is alive.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

import pytest

import app

DAY = 86400


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    d = tmp_path / "uploads"
    d.mkdir()
    monkeypatch.setattr(app, "UPLOADS_DIR", d)
    monkeypatch.setattr(app, "PENDING_UPLOADS_FILE", tmp_path / "pending_uploads.json")
    return d


def _mk(root, name, age_days=0, files=("a.png",)):
    p = root / name
    p.mkdir()
    for f in files:
        (p / f).write_bytes(b"x" * 10)
    t = time.time() - age_days * DAY
    for f in files:
        os.utime(p / f, (t, t))
    os.utime(p, (t, t))
    return p


def _live(monkeypatch, names):
    monkeypatch.setattr(app, "_live_tmux_session_names", lambda: None if names is None else set(names))


def test_delete_removes_the_instance_dir_the_legacy_dir_and_the_pending_queue(uploads):
    _mk(uploads, "shot-1789000000")
    _mk(uploads, "shot")
    _mk(uploads, "other-1789000001")
    app.PENDING_UPLOADS_FILE.write_text(json.dumps({
        "shot-1789000000": [{"path": "x"}], "other-1789000001": [{"path": "y"}]}))
    gone = app._remove_session_uploads("shot", "shot-1789000000")
    assert sorted(gone) == ["shot", "shot-1789000000"]
    assert not (uploads / "shot-1789000000").exists()
    assert not (uploads / "shot").exists()
    assert (uploads / "other-1789000001").exists()
    assert list(json.loads(app.PENDING_UPLOADS_FILE.read_text())) == ["other-1789000001"]


def test_delete_never_leaves_the_uploads_dir(uploads, tmp_path):
    outside = tmp_path / "keepme"
    outside.mkdir()
    (uploads / "evil").symlink_to(outside, target_is_directory=True)
    assert app._remove_session_uploads("evil", "../keepme") == []
    assert app._remove_session_uploads("evil", "evil") == []
    assert outside.exists()


def test_prune_removes_only_old_orphans(uploads, monkeypatch):
    _mk(uploads, "dead-1780000000", age_days=20)          # orphan, old -> removed
    _mk(uploads, "legacy-bare", age_days=30)              # orphan legacy, old -> removed
    _mk(uploads, "fresh-1789000000", age_days=2)          # orphan, recent -> kept
    _mk(uploads, "alive-1780000000", age_days=40)         # live name, old key -> kept
    _mk(uploads, "my_proj-1780000000", age_days=40)       # live "my proj" (sanitised) -> kept
    _mk(uploads, "codexy", age_days=40)                   # live Codex session, bare -> kept
    touched = _mk(uploads, "touched-1780000000", age_days=40)
    (touched / "new.pdf").write_bytes(b"y")               # one new file keeps the dir
    _live(monkeypatch, {"alive", "my proj", "codexy"})
    res = app._prune_orphan_uploads(14)
    assert sorted(res["removed"]) == ["dead-1780000000", "legacy-bare"]
    assert res["freed_bytes"] == 20
    left = sorted(p.name for p in uploads.iterdir())
    assert left == ["alive-1780000000", "codexy", "fresh-1789000000",
                    "my_proj-1780000000", "touched-1780000000"]


def test_prune_deletes_nothing_when_tmux_cannot_be_asked(uploads, monkeypatch):
    _mk(uploads, "dead-1780000000", age_days=90)
    _live(monkeypatch, None)
    res = app._prune_orphan_uploads(14)
    assert res["removed"] == [] and res.get("skipped")
    assert (uploads / "dead-1780000000").exists()


def test_prune_with_no_tmux_server_treats_everything_as_orphaned(uploads, monkeypatch):
    _mk(uploads, "dead-1780000000", age_days=90)
    _live(monkeypatch, set())
    assert app._prune_orphan_uploads(14)["removed"] == ["dead-1780000000"]


def test_retention_zero_disables_the_sweep(uploads, monkeypatch):
    _mk(uploads, "dead-1780000000", age_days=90)
    _live(monkeypatch, set())
    assert app._prune_orphan_uploads(0)["removed"] == []
    assert (uploads / "dead-1780000000").exists()


def test_prune_skips_a_symlink_and_drops_pending_entries(uploads, tmp_path, monkeypatch):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "f").write_bytes(b"z")
    old = time.time() - 90 * DAY
    os.utime(target / "f", (old, old))
    (uploads / "link").symlink_to(target, target_is_directory=True)
    _mk(uploads, "dead-1780000000", age_days=90)
    app.PENDING_UPLOADS_FILE.write_text(json.dumps({"dead-1780000000": [{"path": "p"}]}))
    _live(monkeypatch, set())
    res = app._prune_orphan_uploads(14)
    assert res["removed"] == ["dead-1780000000"]
    assert (target / "f").exists()
    assert json.loads(app.PENDING_UPLOADS_FILE.read_text()) == {}


def test_live_names_parse_real_tmux_answers(monkeypatch):
    class R:
        def __init__(self, rc, out="", err=""):
            self.returncode, self.stdout, self.stderr = rc, out, err
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R(0, "a\nb c\n"))
    assert app._live_tmux_session_names() == {"a", "b c"}
    monkeypatch.setattr(app.subprocess, "run",
                        lambda *a, **k: R(1, err="no server running on /tmp/tmux-1001/default"))
    assert app._live_tmux_session_names() == set()
    monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: R(1, err="error connecting"))
    assert app._live_tmux_session_names() is None
