"""Claude Code 2.1.280+ trust dialog: a launched pane must never sit on it.

THE DEFECT THIS CATCHES (2026-09-24): the CLI now draws its workspace trust
dialog as an UNNUMBERED menu whose highlighted default is "No, exit"
(2.1.141 drew "1. Yes, I trust this folder" first). The auto-responder only
recognised numbered menus, so it never answered, and a dispatcher that typed its
brief plus Enter into the fresh pane picked "No, exit" and killed claude. Lisa's
solver jobs 951 and 962 on builder2 died that way. Two guards:
  1. every launch exports CLAUDE_CODE_SANDBOXED=1, which the CLI honours to skip
     the dialog (from the process env only, not settings.json env);
  2. the auto-responder reads the unnumbered menu and walks to the Yes row.
"""
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

import app

PANES = Path(__file__).parent / "panes"


def _pane(name):
    return (PANES / name).read_text()


def test_every_launch_prefix_skips_the_trust_dialog(monkeypatch):
    for tok in ("", "sk-ant-oat01-x"):
        monkeypatch.setattr(app, "_load_longlived_token", lambda t=tok: t)
        monkeypatch.setattr(app, "_plan_credential_is_static", lambda: False)
        assert "export CLAUDE_CODE_SANDBOXED=1;" in app._claude_launch_env_prefix()


def test_the_unnumbered_trust_dialog_is_seen_as_a_menu():
    for name in ("trust_dialog_settings_2_1_281.txt", "trust_dialog_home_2_1_281.txt"):
        text = _pane(name)
        assert app._detect_interactive_prompt(text) == "selection_prompt", name
        options, selected = app._parse_menu_options(text)
        assert options == [(1, "No, exit"), (2, "Yes, I trust this folder")], name
        assert selected == 0, name


def test_the_pick_is_yes_never_the_highlighted_exit():
    options, selected = app._parse_menu_options(_pane("trust_dialog_settings_2_1_281.txt"))
    offline = app._pick_menu_option_offline(options, selected)
    assert offline == 2
    # Whatever the model proposes, "No, exit" is refused.
    assert app._safe_menu_choice(options, 1) == 2
    assert app._safe_menu_choice(options, None) == 2


def test_the_chat_input_box_is_not_a_menu():
    box = (
        "  Some earlier answer text.\n\n"
        "────────────────────────────────────────\n"
        "❯ test it in the browser\n"
        "────────────────────────────────────────\n"
        "  ⏵⏵ bypass permissions on (shift+tab to cycle)\n"
    )
    assert app._detect_interactive_prompt(box) is None
    assert app._parse_unnumbered_menu(box) is None


def test_a_numbered_menu_still_parses_as_before():
    text = (
        " Do you want to proceed?\n"
        " ❯ 1. Yes\n"
        "   2. Yes, and don't ask again\n"
        "   3. No, and tell Claude what to do differently\n"
    )
    options, selected = app._parse_menu_options(text)
    assert [n for n, _ in options] == [1, 2, 3] and selected == 0


def test_seed_trust_writes_once_and_keeps_other_projects(tmp_path, monkeypatch):
    cj = tmp_path / ".claude.json"
    cj.write_text(json.dumps({"projects": {"/other": {"hasTrustDialogAccepted": True}}}))
    monkeypatch.setattr(app, "_profile_claude_json", lambda cfg: cj)
    monkeypatch.setattr(app, "_backup_before_dashboard_write", lambda p: None)
    app._seed_trust(tmp_path, "/work")
    d = json.loads(cj.read_text())
    assert d["projects"]["/work"]["hasTrustDialogAccepted"] is True
    assert d["projects"]["/other"]["hasTrustDialogAccepted"] is True
    before = cj.stat().st_mtime_ns
    os.utime(cj, ns=(before - 10**9, before - 10**9))
    app._seed_trust(tmp_path, "/work")
    assert cj.stat().st_mtime_ns == before - 10**9, "an already-trusted cwd must not rewrite the file"
