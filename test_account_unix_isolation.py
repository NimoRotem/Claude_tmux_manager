"""Each member session must run as its own UNIX user, not the shared login.

Every account used to share nimrod_rotem, so one `cat` read another account's
advisor token and with it that account's advisor permissions. These cover the
launch-path half of the fix; provision_accounts.py verify covers the kernel half.
"""
import json

import pytest

import app


@pytest.fixture
def mapped(tmp_path, monkeypatch):
    """Point the account map at a temp file and return a writer for it."""
    path = tmp_path / "account-unix-users.json"
    monkeypatch.setattr(app, "_account_unix_map_file", lambda: path)
    app._ACCOUNT_UNIX_USABLE.clear()

    def write(mapping):
        path.write_text(json.dumps(mapping))

    return write


MEMBER = {"id": "u_abc", "username": "someone", "role": "user", "group": "engineers"}
ADMIN = {"id": "admin", "username": "Nimo", "role": "admin"}


def test_member_with_a_provisioned_account_gets_it(mapped, monkeypatch):
    mapped({"u_abc": "gx-someone"})
    monkeypatch.setattr(app, "_unix_account_usable", lambda name: True)
    assert app._account_unix_user(MEMBER) == "gx-someone"


def test_admin_never_drops_into_a_session_account(mapped, monkeypatch):
    mapped({"admin": "gx-nimo"})
    monkeypatch.setattr(app, "_unix_account_usable", lambda name: True)
    assert app._account_unix_user(ADMIN) == ""


def test_unmapped_member_falls_back_to_the_shared_login(mapped, monkeypatch):
    mapped({"u_other": "gx-other"})
    monkeypatch.setattr(app, "_unix_account_usable", lambda name: True)
    assert app._account_unix_user(MEMBER) == ""


def test_an_unusable_account_does_not_break_the_launch(mapped, monkeypatch):
    """A half-done migration must degrade to today's behaviour, not a dead pane."""
    mapped({"u_abc": "gx-someone"})
    monkeypatch.setattr(app, "_unix_account_usable", lambda name: False)
    assert app._account_unix_user(MEMBER) == ""


def test_a_missing_map_file_does_not_raise(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "_account_unix_map_file", lambda: tmp_path / "absent.json")
    app._ACCOUNT_UNIX_USABLE.clear()
    assert app._account_unix_user(MEMBER) == ""


def test_the_kill_switch_restores_the_old_launch(mapped, monkeypatch):
    mapped({"u_abc": "gx-someone"})
    monkeypatch.setattr(app, "_unix_account_usable", lambda name: True)
    monkeypatch.setenv("TMUX_DASH_PER_USER_UNIX", "0")
    assert app._account_unix_user(MEMBER) == ""


def test_launch_drops_privilege_and_cds_inside_the_account(mapped, monkeypatch, tmp_path):
    """The cd belongs to the account: the shared parent shell cannot enter that tree."""
    mapped({"u_abc": "gx-someone"})
    monkeypatch.setattr(app, "_unix_account_usable", lambda name: True)
    monkeypatch.setattr(app, "_user_for_session", lambda name: MEMBER)
    project = tmp_path / "proj"
    monkeypatch.setattr(app, "_member_session_project_dir", lambda u, s: project)

    wrapped = app._session_unix_account_prefix("sess", "codex --yolo")

    assert wrapped.startswith("sudo -n -u gx-someone -H bash -lc ")
    assert "cd -- " in wrapped
    assert str(project) in wrapped
    # The MCP proxy still has to find the dashboard owner's home.
    assert "TMUX_DASH_HOST_HOME=" in wrapped
    # The original command survives the quoting intact.
    assert "codex --yolo" in wrapped


def test_shared_login_sessions_are_left_exactly_as_they_were(mapped, monkeypatch):
    mapped({})
    monkeypatch.setattr(app, "_user_for_session", lambda name: MEMBER)
    assert app._session_unix_account_prefix("sess", "codex --yolo") == "codex --yolo"


def test_member_codex_home_keeps_the_group_the_dashboard_needs(tmp_path, monkeypatch):
    """Forcing 0700/0600 here locks the dashboard out of the config it writes."""
    cfg_dir = tmp_path / ".codex-user-u_abc"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("model = 'x'\n")
    monkeypatch.setattr(app, "_account_unix_user_for_config_dir", lambda d: "gx-someone")

    app._set_member_codex_permissions(cfg_dir)

    assert cfg_dir.stat().st_mode & 0o777 == 0o770
    assert cfg_dir.stat().st_mode & 0o2000, "setgid keeps new files in the account group"
    assert (cfg_dir / "config.toml").stat().st_mode & 0o777 == 0o660
    assert not (cfg_dir / "config.toml").stat().st_mode & 0o007, "no access for other accounts"


def test_shared_login_codex_home_stays_fully_private(tmp_path, monkeypatch):
    cfg_dir = tmp_path / ".codex-user-u_abc"
    cfg_dir.mkdir()
    (cfg_dir / "config.toml").write_text("model = 'x'\n")
    monkeypatch.setattr(app, "_account_unix_user_for_config_dir", lambda d: "")

    app._set_member_codex_permissions(cfg_dir)

    assert cfg_dir.stat().st_mode & 0o777 == 0o700
    assert (cfg_dir / "config.toml").stat().st_mode & 0o777 == 0o600
