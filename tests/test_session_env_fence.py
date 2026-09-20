"""A launched session runs on its subscription and is handed no metered API key."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class _Created:
    returncode = 0
    stdout = "$7\tprobe"
    stderr = ""


def _launch_argv(monkeypatch, env):
    """Capture the argv `_create_exact_tmux_session` would hand tmux. -> list[str]"""
    seen = {}

    def fake_run(command, **kwargs):
        seen["argv"] = list(command)
        return _Created()

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "CODEX_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app._create_exact_tmux_session("probe")
    return seen["argv"]


def _fenced(argv):
    return [argv[i + 1] for i, arg in enumerate(argv) if arg == "-e"]


def test_a_metered_key_in_the_dashboard_env_is_fenced_out_of_the_pane(monkeypatch):
    #  THE DEFECT THIS CATCHES: the dashboard needs OPENAI_API_KEY for the summaries and the
    #  voice endpoints, and a pane used to inherit the whole environment with it, so an agent
    #  could bill a metered key instead of running on the subscription it was given. That failure
    #  is silent: nothing errors, it just costs money.
    argv = _launch_argv(monkeypatch, {
        "OPENAI_API_KEY": "sk-svcacct-pretend-live-key",
        "ANTHROPIC_API_KEY": "sk-ant-pretend-live-key",
    })
    fenced = _fenced(argv)
    assert "OPENAI_API_KEY=" in fenced
    assert "ANTHROPIC_API_KEY=" in fenced
    assert not [pair for pair in fenced if pair.split("=", 1)[1]], \
        "a fenced name is passed through EMPTY, which every consumer reads as absent"


def test_only_the_names_the_dashboard_actually_holds_are_fenced(monkeypatch):
    argv = _launch_argv(monkeypatch, {"CODEX_API_KEY": "sk-pretend"})
    assert _fenced(argv) == ["CODEX_API_KEY="]


def test_nothing_is_fenced_when_the_dashboard_holds_no_key(monkeypatch):
    assert "-e" not in _launch_argv(monkeypatch, {})
