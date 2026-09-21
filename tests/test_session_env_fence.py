"""A launched session runs on its subscription and is handed no metered API key."""
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import app


class _Created:
    returncode = 0
    stdout = "$7\tprobe"
    stderr = ""


def _launch(monkeypatch, env, tmux_has_e=True):
    """Run `_create_exact_tmux_session` against a stubbed tmux.

    -> (new-session argv, every argv it ran, the env each was spawned with)
    """
    runs, envs = [], []

    def fake_run(command, **kwargs):
        runs.append(list(command))
        envs.append(kwargs.get("env"))
        return _Created()

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    monkeypatch.setattr(app, "_tmux_supports_new_session_e", lambda: tmux_has_e)
    for name in app.FENCED_ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    app._create_exact_tmux_session("probe")
    new_session = [argv for argv in runs if argv[1:2] == ["new-session"]][0]
    return new_session, runs, envs


def _launch_argv(monkeypatch, env, tmux_has_e=True):
    """Capture the argv `_create_exact_tmux_session` would hand tmux. -> list[str]"""
    return _launch(monkeypatch, env, tmux_has_e)[0]


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


def test_every_name_is_fenced_even_the_ones_this_process_does_not_hold(monkeypatch):
    #  THE DEFECT THIS CATCHES: fencing only the names in the DASHBOARD's own
    #  environment reads like a fence and is dead code against the case that
    #  actually happens. Measured on the Codex line 2026-09-20: a tmux SERVER's
    #  global environment carried a 167-character key while the dashboard process
    #  did not, so os.environ said there was nothing to fence and the pane
    #  inherited it. The server outlives the dashboard, so its environment is not
    #  ours to infer. Send every name every time.
    argv = _launch_argv(monkeypatch, {"CODEX_API_KEY": "sk-pretend"})
    assert sorted(_fenced(argv)) == sorted(n + "=" for n in app.FENCED_ENV_NAMES)


def test_the_per_job_voice_and_tasks_keys_are_fenced_too(monkeypatch):
    #  Splitting the box's one key into a voice key and a small-tasks key adds two more
    #  names a pane must not inherit. Missing them would have quietly re-opened the hole
    #  the moment the split landed.
    argv = _launch_argv(monkeypatch, {"OPENAI_VOICE_KEY": "sk-voice-pretend",
                                      "OPENAI_TASKS_KEY": "sk-tasks-pretend"})
    fenced = _fenced(argv)
    assert "OPENAI_VOICE_KEY=" in fenced and "OPENAI_TASKS_KEY=" in fenced
    assert "OPENAI_VOICE_KEY" in app.FENCED_ENV_NAMES, \
        "the list itself is the fence: a name missing from it is never sent"
    assert "OPENAI_TASKS_KEY" in app.FENCED_ENV_NAMES


def test_the_fence_still_goes_up_when_the_dashboard_holds_no_key_at_all(monkeypatch):
    #  The old version of this test asserted the opposite, that an empty
    #  environment means no fence. That is exactly the hole above.
    assert sorted(_fenced(_launch_argv(monkeypatch, {}))) == \
        sorted(n + "=" for n in app.FENCED_ENV_NAMES)


def test_an_old_tmux_gets_the_fence_without_the_flag_it_does_not_have(monkeypatch):
    #  THE DEFECT THIS CATCHES: `new-session -e` arrived in tmux 3.2, and builder1 is
    #  Debian 11 with 3.1c. Every create there answered "tmux: unknown option -- e",
    #  so the box could not open a single session. The fence still has to hold on the
    #  old flag set, or the fix would be to hand the pane a metered key.
    new_session, runs, _ = _launch(monkeypatch, {
        "OPENAI_API_KEY": "sk-svcacct-pretend-live-key",
        "ANTHROPIC_API_KEY": "sk-ant-pretend-live-key",
    }, tmux_has_e=False)
    assert "-e" not in new_session, "3.1c rejects the whole command if -e appears"
    emptied = {argv[3]: argv[4] for argv in runs if argv[1:3] == ["set-environment", "-g"]}
    assert emptied == {n: "" for n in app.FENCED_ENV_NAMES}


def test_the_tmux_client_itself_carries_no_key(monkeypatch):
    #  If no server is running yet, the client forks one, and the server hands its own
    #  environment to every pane it ever starts. Emptying the names on the client is
    #  what stops one badly timed first launch from leaking to everything after it.
    _, runs, envs = _launch(monkeypatch, {"OPENAI_API_KEY": "sk-svcacct-pretend-live-key"})
    assert envs and all(env is not None for env in envs), "every tmux call must pass an env"
    for env in envs:
        assert env.get("OPENAI_API_KEY") == ""


def test_the_tmux_version_gate_reads_the_real_strings(monkeypatch):
    for version, supported in (("tmux 3.1c\n", False), ("tmux 3.2\n", True),
                               ("tmux 3.3a\n", True), ("tmux 2.8\n", False),
                               ("", False)):
        monkeypatch.setattr(app, "_tmux_new_session_e", {})
        monkeypatch.setattr(app.subprocess, "run",
                            lambda *a, **k: type("R", (), {"stdout": version})())
        assert app._tmux_supports_new_session_e() is supported, version


def _set_auth_mode(monkeypatch, mode, stored_key="sk-ant-pretend-live-key"):
    """Call the auth-mode route against a pane nothing may be typed into.
    -> (status, body dict, list of tmux argv the route tried to run)"""
    typed = []

    def fake_run(command, **kwargs):
        typed.append(list(command))
        return _Created()

    monkeypatch.setattr(app.subprocess, "run", fake_run)
    monkeypatch.setattr(app, "_find_session", lambda name: (None, {"name": name}))
    monkeypatch.setattr(app, "_stored_anthropic_key", stored_key, raising=False)
    resp = asyncio.run(app.api_set_auth_mode("probe", app.AuthModeBody(mode=mode)))
    return resp.status_code, json.loads(bytes(resp.body).decode()), typed


def test_api_key_auth_mode_is_refused_and_types_nothing_into_the_pane(monkeypatch):
    #  THE DEFECT THIS CATCHES: this route used to send-keys `export ANTHROPIC_API_KEY=<key>`
    #  into the live pane, and when no key was stored it went looking for an sk-ant- literal
    #  in the instruction file and used that. Either way the session stopped running on the
    #  plan it was given and started billing, and the control was gated on owning the session,
    #  not on being an admin. A refusal, not a silent downgrade, so a caller finds out.
    status, body, typed = _set_auth_mode(monkeypatch, "api")
    assert status == 409
    assert not typed, "no tmux command may run for an api-mode request"
    assert "plan" in body["error"].lower()
    assert "sk-ant" not in json.dumps(body)


def test_subscription_mode_still_clears_a_stray_key_from_the_pane(monkeypatch):
    status, body, typed = _set_auth_mode(monkeypatch, "subscription")
    assert status == 200 and body["mode"] == "subscription"
    assert any("unset ANTHROPIC_API_KEY" in arg for argv in typed for arg in argv)


def test_the_dashboard_refuses_to_store_an_anthropic_key(monkeypatch):
    #  A stored key was the switch behind every metered path here: the launch banner, the
    #  one-time config prime, the login watchdog's key mode, and the per-session toggle.
    saved = []
    monkeypatch.setattr(app, "_save_anthropic_key", lambda key: saved.append(key))
    resp = asyncio.run(app.api_set_claude_key(app.SetApiKey(apiKey="sk-ant-pretend-live-key")))
    assert resp.status_code == 409
    assert not saved, "nothing may reach the key file"


def test_clearing_the_stored_key_still_works(monkeypatch):
    cleared = []
    monkeypatch.setattr(app, "_clear_anthropic_key", lambda: cleared.append(True))
    resp = asyncio.run(app.api_set_claude_key(app.SetApiKey(apiKey="  ")))
    assert resp.status_code == 200 and cleared


def test_arming_the_api_key_helper_writes_nothing(monkeypatch, tmp_path):
    #  The settings.json `apiKeyHelper` authenticated whether or not a plan was usable,
    #  which is exactly how a metered fallback bills for hours without looking wrong.
    app._set_api_key_helper(tmp_path)
    assert not (tmp_path / "settings.json").exists()
