"""Tests for the keep-going hooks installed into member Claude config dirs.

A member session runs with its own CLAUDE_CONFIG_DIR and never reads the owner's
~/.claude/settings.json, so this installer is the only thing standing between a
member and a session that stops half-done waiting on someone who is away.
"""
import json
import os

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-not-real")

from app import (  # noqa: E402
    KEEP_GOING_HOOK,
    KEEP_GOING_RESET_HOOK,
    _install_keep_going_hooks,
)


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A member config dir, with the hook scripts present on disk."""
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    stop = hooks / "keep_going.py"
    reset = hooks / "keep_going_reset.py"
    stop.write_text("#!/usr/bin/env python3\n")
    reset.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr("app.KEEP_GOING_HOOK", stop)
    monkeypatch.setattr("app.KEEP_GOING_RESET_HOOK", reset)
    monkeypatch.setattr(
        "app._KEEP_GOING_EVENTS",
        (("Stop", stop), ("SubagentStop", stop), ("UserPromptSubmit", reset)),
    )
    d = tmp_path / "cfg"
    d.mkdir()
    return d


def _settings(cfg):
    return json.loads((cfg / "settings.json").read_text())


def _commands(settings, event):
    return [h["command"] for entry in settings["hooks"][event] for h in entry["hooks"]]


def test_installs_all_three_events_and_the_denial(cfg):
    _install_keep_going_hooks(cfg)
    s = _settings(cfg)
    for event in ("Stop", "SubagentStop", "UserPromptSubmit"):
        assert len(_commands(s, event)) == 1, event
    assert "keep_going.py" in _commands(s, "Stop")[0]
    assert "keep_going_reset.py" in _commands(s, "UserPromptSubmit")[0]
    assert s["permissions"]["deny"] == ["AskUserQuestion"]


def test_reapplying_does_not_stack_duplicates(cfg):
    """Member setup re-runs on every session, so this runs many times per dir."""
    for _ in range(4):
        _install_keep_going_hooks(cfg)
    s = _settings(cfg)
    for event in ("Stop", "SubagentStop", "UserPromptSubmit"):
        assert len(_commands(s, event)) == 1, event
    assert s["permissions"]["deny"].count("AskUserQuestion") == 1


def test_preserves_the_sandbox_hook_and_other_settings(cfg):
    """The sandbox guard is installed into the same file moments earlier."""
    (cfg / "settings.json").write_text(json.dumps({
        "hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
            {"type": "command", "command": "python3 /x/sandbox_guard.py"}]}]},
        "permissions": {"deny": ["WebFetch"], "allow": ["Bash(ls:*)"]},
        "model": "opus",
    }))
    _install_keep_going_hooks(cfg)
    s = _settings(cfg)
    assert "sandbox_guard" in _commands(s, "PreToolUse")[0]
    assert s["model"] == "opus"
    assert s["permissions"]["allow"] == ["Bash(ls:*)"]
    assert set(s["permissions"]["deny"]) == {"WebFetch", "AskUserQuestion"}


def test_keeps_an_unrelated_stop_hook(cfg):
    """Only our own entry is replaced; someone else's Stop hook survives."""
    (cfg / "settings.json").write_text(json.dumps({
        "hooks": {"Stop": [{"hooks": [
            {"type": "command", "command": "bash /x/notify.sh"}]}]},
    }))
    _install_keep_going_hooks(cfg)
    cmds = _commands(_settings(cfg), "Stop")
    assert len(cmds) == 2
    assert any("notify.sh" in c for c in cmds)
    assert any("keep_going.py" in c for c in cmds)


def test_corrupt_settings_file_is_replaced_not_fatal(cfg):
    (cfg / "settings.json").write_text("{not json at all")
    _install_keep_going_hooks(cfg)
    assert len(_commands(_settings(cfg), "Stop")) == 1


@pytest.mark.parametrize("shape", ['{"hooks": []}', '{"permissions": "nope"}',
                                   '{"hooks": {"Stop": "nope"}}'])
def test_wrong_shaped_settings_are_repaired(cfg, shape):
    (cfg / "settings.json").write_text(shape)
    _install_keep_going_hooks(cfg)
    s = _settings(cfg)
    assert len(_commands(s, "Stop")) == 1
    assert "AskUserQuestion" in s["permissions"]["deny"]


def test_does_nothing_when_the_scripts_are_missing(cfg, monkeypatch, tmp_path):
    """Registering an absent command logs a hook error on every stop instead."""
    monkeypatch.setattr("app.KEEP_GOING_HOOK", tmp_path / "gone.py")
    _install_keep_going_hooks(cfg)
    assert not (cfg / "settings.json").exists()


def test_installed_for_a_member_even_with_team_mode_off(tmp_path, monkeypatch):
    """The regression this exists for.

    The per-user config dir is created for every non-admin user regardless of
    TEAM_MODE, and the boxes that actually have members run with TEAM_MODE unset.
    Gating the install on team mode leaves it inert exactly where the gap is real,
    so drive the real bootstrap with team mode OFF and require the hook anyway.
    """
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    hooks = home / ".claude" / "hooks"
    hooks.mkdir(parents=True)
    stop = hooks / "keep_going.py"
    reset = hooks / "keep_going_reset.py"
    stop.write_text("#!/usr/bin/env python3\n")
    reset.write_text("#!/usr/bin/env python3\n")
    monkeypatch.setattr("app.KEEP_GOING_HOOK", stop)
    monkeypatch.setattr("app.KEEP_GOING_RESET_HOOK", reset)
    monkeypatch.setattr(
        "app._KEEP_GOING_EVENTS",
        (("Stop", stop), ("SubagentStop", stop), ("UserPromptSubmit", reset)),
    )
    monkeypatch.setattr("app.TEAM_MODE", False)

    from app import _ensure_user_claude_config_dir, _user_claude_config_dir

    user = {"id": "u_test123", "username": "member", "role": "user"}
    _ensure_user_claude_config_dir(user)

    s = json.loads((_user_claude_config_dir(user) / "settings.json").read_text())
    assert "keep_going.py" in _commands(s, "Stop")[0]
    assert "keep_going.py" in _commands(s, "SubagentStop")[0]
    assert "keep_going_reset.py" in _commands(s, "UserPromptSubmit")[0]
    assert "AskUserQuestion" in s["permissions"]["deny"]


def test_admin_config_dir_is_untouched_by_the_member_bootstrap(tmp_path, monkeypatch):
    """Admin uses ~/.claude directly; the bootstrap must not rewrite it."""
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    from app import _ensure_user_claude_config_dir

    _ensure_user_claude_config_dir({"id": "admin", "username": "Nimo", "role": "admin"})
    assert not (home / ".claude" / "settings.json").exists()


def test_canonical_hook_paths_point_at_the_real_files():
    """Guards the deployed install: these are what member dirs are pointed at."""
    assert KEEP_GOING_HOOK.name == "keep_going.py"
    assert KEEP_GOING_RESET_HOOK.name == "keep_going_reset.py"
    assert KEEP_GOING_HOOK.parent == KEEP_GOING_RESET_HOOK.parent
