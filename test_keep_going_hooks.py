"""The keep-going Stop hook is retired; the dashboard now strips it.

Whether a session carries on after it looks done is the Auto-push control's job
(off / basic / full) and nothing else. A member session runs with its own
CLAUDE_CONFIG_DIR, so a registration already written into one of those would
otherwise sit there forever: removal has to be active, not just a stop to
installing. The AskUserQuestion denial stays, because that is a separate
standing preference and Auto-push does not cover it.
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
    _remove_keep_going_hooks,
)

STOP_CMD = {"hooks": [{"type": "command", "command": "python3 /x/hooks/keep_going.py"}]}
RESET_CMD = {"hooks": [{"type": "command", "command": "python3 /x/hooks/keep_going_reset.py"}]}
OTHER_CMD = {"hooks": [{"type": "command", "command": "/x/hooks/somebody-elses.sh"}]}


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "cfg"
    d.mkdir()
    return d


def write(cfg, settings):
    (cfg / "settings.json").write_text(json.dumps(settings))


def read(cfg):
    return json.loads((cfg / "settings.json").read_text())


def test_a_config_carrying_the_hook_is_cleaned(cfg):
    write(cfg, {"hooks": {"Stop": [STOP_CMD], "SubagentStop": [STOP_CMD],
                          "UserPromptSubmit": [RESET_CMD]}})
    _remove_keep_going_hooks(cfg)
    s = read(cfg)
    assert "hooks" not in s, f"nothing should be left to register: {s.get('hooks')!r}"
    assert "keep_going" not in json.dumps(s)


def test_an_unrelated_hook_on_the_same_event_survives(cfg):
    write(cfg, {"hooks": {"Stop": [STOP_CMD, OTHER_CMD]}})
    _remove_keep_going_hooks(cfg)
    s = read(cfg)
    assert s["hooks"]["Stop"] == [OTHER_CMD]
    assert "keep_going" not in json.dumps(s)


def test_hooks_for_other_events_are_untouched(cfg):
    write(cfg, {"hooks": {"Stop": [STOP_CMD], "PreToolUse": [OTHER_CMD]}})
    _remove_keep_going_hooks(cfg)
    s = read(cfg)
    assert s["hooks"] == {"PreToolUse": [OTHER_CMD]}


def test_no_empty_event_list_is_left_behind(cfg):
    """An empty list is not harmless: it reads as "this event is configured"."""
    write(cfg, {"hooks": {"Stop": [STOP_CMD], "SubagentStop": [STOP_CMD]}})
    _remove_keep_going_hooks(cfg)
    s = read(cfg)
    assert "hooks" not in s
    assert s.get("hooks", {}) == {}


def test_the_askuserquestion_denial_is_kept(cfg):
    write(cfg, {"hooks": {"Stop": [STOP_CMD]}})
    _remove_keep_going_hooks(cfg)
    assert read(cfg)["permissions"]["deny"] == ["AskUserQuestion"]


def test_the_denial_is_not_duplicated_on_reapply(cfg):
    write(cfg, {"permissions": {"deny": ["AskUserQuestion"]}})
    _remove_keep_going_hooks(cfg)
    _remove_keep_going_hooks(cfg)
    assert read(cfg)["permissions"]["deny"] == ["AskUserQuestion"]


def test_unrelated_settings_survive(cfg):
    write(cfg, {"model": "claude-opus-5", "env": {"FOO": "1"},
                "hooks": {"Stop": [STOP_CMD]},
                "permissions": {"defaultMode": "bypassPermissions", "allow": ["*"]}})
    _remove_keep_going_hooks(cfg)
    s = read(cfg)
    assert s["model"] == "claude-opus-5"
    assert s["env"] == {"FOO": "1"}
    assert s["permissions"]["defaultMode"] == "bypassPermissions"
    assert s["permissions"]["allow"] == ["*"]


def test_running_it_twice_changes_nothing_the_second_time(cfg):
    write(cfg, {"hooks": {"Stop": [STOP_CMD], "PreToolUse": [OTHER_CMD]}})
    _remove_keep_going_hooks(cfg)
    once = read(cfg)
    _remove_keep_going_hooks(cfg)
    assert read(cfg) == once


def test_a_config_that_never_had_it_is_fine(cfg):
    write(cfg, {"hooks": {"PreToolUse": [OTHER_CMD]}})
    _remove_keep_going_hooks(cfg)
    assert read(cfg)["hooks"] == {"PreToolUse": [OTHER_CMD]}


def test_a_missing_settings_file_is_created_not_fatal(cfg):
    _remove_keep_going_hooks(cfg)
    s = read(cfg)
    assert "hooks" not in s
    assert s["permissions"]["deny"] == ["AskUserQuestion"]


@pytest.mark.parametrize("body", ["not json at all", '"a string"', "[1,2,3]", "null"])
def test_a_corrupt_or_wrong_shaped_settings_file_is_repaired(cfg, body):
    (cfg / "settings.json").write_text(body)
    _remove_keep_going_hooks(cfg)
    assert read(cfg)["permissions"]["deny"] == ["AskUserQuestion"]


def test_wrong_shaped_hooks_value_does_not_crash(cfg):
    write(cfg, {"hooks": {"Stop": "not-a-list", "SubagentStop": [STOP_CMD]}})
    _remove_keep_going_hooks(cfg)
    s = read(cfg)
    assert "SubagentStop" not in s.get("hooks", {})


def test_the_member_bootstrap_calls_the_remover_not_an_installer():
    """The call site has to have moved too, or member dirs keep the hook."""
    import pathlib
    src = pathlib.Path(__file__).with_name("app.py").read_text()
    assert "_install_keep_going_hooks" not in src, "the installer is still referenced"
    assert src.count("_remove_keep_going_hooks(d)") == 1, "the bootstrap must call the remover"


def test_the_canonical_paths_still_name_the_two_scripts():
    """Kept so the stripper can recognise a registration by script name."""
    assert KEEP_GOING_HOOK.name == "keep_going.py"
    assert KEEP_GOING_RESET_HOOK.name == "keep_going_reset.py"


def test_autopush_is_the_remaining_control():
    from app import AUTOPUSH_MODES, AUTOPUSH_DEFAULT
    assert set(AUTOPUSH_MODES) == {"off", "basic", "full"}
    assert AUTOPUSH_DEFAULT in AUTOPUSH_MODES
