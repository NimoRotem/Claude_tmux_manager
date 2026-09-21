"""users.json must survive a concurrent save.

On 2026-09-11 codex.lisa.my lost two of its three admin accounts. Nothing
deleted them: `_save_users` truncated users.json before writing it, a request
read the file inside that window, got zero bytes, and the "file is unreadable,
re-seed an admin from the env vars" recovery path saved its lone replacement
over everybody. Saves are atomic now and the recovery path no longer overwrites
what it could not read. These tests fail on the code as it stood that morning.
"""
import json
import os
import threading
import time

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

import app


THREE = [
    {"id": "admin", "username": "Nimo", "password_salt": "s1", "password_hash": "h1", "role": "admin"},
    {"id": "u_mich", "username": "Michiel", "password_salt": "s2", "password_hash": "h2", "role": "admin"},
    {"id": "u_mon", "username": "Monica", "password_salt": "s3", "password_hash": "h3", "role": "admin"},
]
ALL_THREE = ["Michiel", "Monica", "Nimo"]


@pytest.fixture
def store(tmp_path, monkeypatch):
    """Point the user store at a scratch directory."""
    monkeypatch.setattr(app, "MESSAGES_DIR", tmp_path)
    monkeypatch.setattr(app, "USERS_FILE", tmp_path / "users.json")
    monkeypatch.setattr(app, "USERS_BACKUP_FILE", tmp_path / "users.json.bak")
    return tmp_path


def names(users):
    return sorted(u["username"] for u in users)


def on_disk(path):
    return names(json.loads(path.read_text())["users"])


def test_a_reader_during_a_save_never_sees_an_empty_file(store):
    """The incident itself: readers and writers at once, nobody loses an account."""
    app._save_users(THREE)
    stop = threading.Event()
    seen = []

    def writer():
        while not stop.is_set():
            app._save_users(THREE)

    def reader():
        while not stop.is_set():
            seen.append(names(app._load_users()))

    threads = [threading.Thread(target=writer) for _ in range(3)]
    threads += [threading.Thread(target=reader) for _ in range(3)]
    for t in threads:
        t.start()
    time.sleep(3)
    stop.set()
    for t in threads:
        t.join()

    assert seen, "the reader threads never ran"
    lost = [s for s in seen if s != ALL_THREE]
    assert not lost, f"{len(lost)} of {len(seen)} reads lost users, e.g. {lost[:3]}"
    assert on_disk(store / "users.json") == ALL_THREE


def test_an_empty_users_file_recovers_from_the_backup(store):
    app._save_users(THREE)
    app._save_users(THREE)  # the second save is what leaves a good .bak
    (store / "users.json").write_text("")

    assert names(app._load_users()) == ALL_THREE
    assert on_disk(store / "users.json") == ALL_THREE


def test_a_broken_live_file_never_poisons_the_backup(store):
    app._save_users(THREE)
    app._save_users(THREE)
    (store / "users.json").write_text("{ truncated")
    app._save_users(THREE[:1])

    assert on_disk(store / "users.json.bak") == ALL_THREE


def test_an_unreadable_file_is_kept_not_destroyed(store):
    (store / "users.json").write_text('{"users": [{"id": "u_x", "username": "Salvageable"')

    assert [u["username"] for u in app._load_users()] == [app.AUTH_USER]  # seeded from env
    kept = list(store.glob("users.json.unreadable-*"))
    assert kept, "the unreadable file was overwritten with no copy left"
    assert "Salvageable" in kept[0].read_text()


def test_a_genuine_first_run_still_seeds_the_env_admin(store):
    seeded = app._load_users()

    assert [u["username"] for u in seeded] == [app.AUTH_USER]
    assert app._verify_password(seeded[0], app.AUTH_PASS)
    assert oct((store / "users.json").stat().st_mode)[-3:] == "600"


def test_an_empty_user_list_is_never_written(store):
    app._save_users(THREE)
    app._save_users([])

    assert names(app._load_users()) == ALL_THREE
