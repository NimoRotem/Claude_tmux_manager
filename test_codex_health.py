"""Tests for the Codex launch fix, health watchdog, and per-account usage."""
import json
import os
import time

import pytest

os.environ.setdefault("TMUX_DASH_SECRET", "test-secret-key-for-testing")
os.environ.setdefault("TMUX_DASH_PASS", "testpass")
os.environ.setdefault("TMUX_DASH_USER", "admin")

import app


# The pane text a member session really showed while this bug was live.
BROKEN_PANE = """\
_36f0fc4837a8f21b ADVISOR_TOKEN="$(cat /home/nimrod_rotem/.codex-user-u_36f0fc48\
37a8f21b/advisor-token 2>/dev/null)" env -u OPENAI_API_KEY codex --yolo -c mcp_s\
ervers.openaiDeveloperDocs.enabled=false'; else exec env CODEX_HOME=/home/nimrod\
_rotem/.codex-user-u_36f0fc4837a8f21b env -u OPENAI_API_KEY codex --yolo; fi
Error loading config.toml: invalid transport
in `mcp_servers.openaiDeveloperDocs`

nimrod_rotem@grabo-tech:~/web-projects/guy/9kt6a$ hi
-bash: hi: command not found
nimrod_rotem@grabo-tech:~/web-projects/guy/9kt6a$
"""

HEALTHY_PANE = """\
╭─────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.146.0)                      │
╰─────────────────────────────────────────────────╯
› Explain this codebase
"""

CONTINUATION_PANE = """\
nimrod_rotem@grabo-tech:~/web-projects/emilie/1$ https://docs.google.com/x  Don't
>
"""


# --- the root cause: the -c override must follow the config, not the code ----

def _write_config(tmp_path, body):
    home = tmp_path / "codex-home"
    home.mkdir(exist_ok=True)
    (home / "config.toml").write_text(body)
    return home


def test_mcp_server_names_parsed_from_config(tmp_path):
    home = _write_config(tmp_path, """
model = "gpt-5.6-sol"
[mcp_servers.openaiDeveloperDocs]
url = "https://developers.openai.com/mcp"
[mcp_servers.google]
command = "python"
[mcp_servers.google.env]
KEY = "value"
[mcp_servers.playwright-browser]
command = "python3"
[projects."/home/x"]
trust_level = "trusted"
""")
    names = app._codex_home_mcp_servers(home)
    assert names == {"openaiDeveloperDocs", "google", "playwright-browser"}


def test_no_docs_override_when_home_does_not_declare_it(tmp_path):
    """The exact failure: a member home has no openaiDeveloperDocs server, so
    the override would create an invalid one and Codex refuses to start."""
    home = _write_config(tmp_path, '[mcp_servers.advisor]\nurl = "https://a/mcp"\n')
    out = app._launch_codex_cmd("codex --yolo", pin_model=False, codex_home=home)
    assert "openaiDeveloperDocs" not in out


def test_docs_override_kept_when_home_declares_it(tmp_path):
    home = _write_config(
        tmp_path,
        '[mcp_servers.openaiDeveloperDocs]\nurl = "https://developers.openai.com/mcp"\n',
    )
    out = app._launch_codex_cmd("codex --yolo", pin_model=False, codex_home=home)
    assert "-c mcp_servers.openaiDeveloperDocs.enabled=false" in out


def test_inherited_override_is_stripped_for_a_home_without_the_server(tmp_path):
    """A stored session command must not smuggle the flag into a member home."""
    home = _write_config(tmp_path, '[mcp_servers.advisor]\nurl = "https://a/mcp"\n')
    stored = "codex --yolo -c mcp_servers.openaiDeveloperDocs.enabled=false"
    out = app._launch_codex_cmd(stored, pin_model=False, codex_home=home)
    assert "openaiDeveloperDocs" not in out
    assert "codex --yolo" in out


def test_missing_config_file_means_no_override(tmp_path):
    out = app._launch_codex_cmd(
        "codex --yolo", pin_model=False, codex_home=tmp_path / "nope"
    )
    assert "openaiDeveloperDocs" not in out


def test_explicit_resume_uses_thread_id_and_cwd_instead_of_last(tmp_path):
    home = _write_config(tmp_path, "")
    thread_id = "01a020d4-d4e0-75a3-b832-b830e6f4fd87"
    recovered_cwd = tmp_path / "recovered workspace"
    recovered_cwd.mkdir()

    out = app._launch_codex_cmd(
        "codex --yolo",
        pin_model=False,
        resume=True,
        resume_uuid=thread_id,
        resume_cwd=str(recovered_cwd),
        codex_home=home,
    )

    assert f"resume -C '{recovered_cwd}'" in out
    assert thread_id in out
    assert "--last" not in out
    assert "--yolo" in out


@pytest.mark.parametrize(
    ("thread_id", "cwd"),
    [("--last", "/tmp"), ("01a020d4-d4e0-75a3-b832-b830e6f4fd87", "")],
)
def test_exact_resume_fails_closed_for_invalid_identity_or_cwd(
    tmp_path, thread_id, cwd
):
    home = _write_config(tmp_path, "")

    with pytest.raises(ValueError):
        app._launch_codex_cmd(
            "codex --yolo",
            pin_model=False,
            resume=True,
            resume_uuid=thread_id,
            resume_cwd=cwd,
            codex_home=home,
        )


def test_config_cache_follows_edits(tmp_path):
    home = _write_config(tmp_path, '[mcp_servers.advisor]\nurl = "https://a/mcp"\n')
    assert "openaiDeveloperDocs" not in app._codex_home_mcp_servers(home)
    time.sleep(0.01)
    (home / "config.toml").write_text(
        '[mcp_servers.advisor]\nurl = "https://a/mcp"\n'
        '[mcp_servers.openaiDeveloperDocs]\nurl = "https://d/mcp"\n'
    )
    assert "openaiDeveloperDocs" in app._codex_home_mcp_servers(home)


# --- detecting the dead session ---------------------------------------------

def test_config_error_is_recognised_as_a_start_failure():
    assert app._looks_like_codex_start_failure(BROKEN_PANE)
    # It is NOT an OOM crash, which is why the old matcher missed it entirely.
    assert not app._looks_like_crash(BROKEN_PANE)


def test_broken_pane_is_recoverable_and_healthy_pane_is_not():
    assert app._looks_like_bare_shell(BROKEN_PANE)
    assert app._codex_is_down_recoverably(BROKEN_PANE)
    assert not app._looks_like_bare_shell(HEALTHY_PANE)
    assert not app._codex_is_down_recoverably(HEALTHY_PANE)


def test_launch_line_alone_is_enough_to_recover():
    pane = (
        "exec env CODEX_HOME=/home/nimrod_rotem/.codex-user-u_x codex --yolo\n"
        "nimrod_rotem@grabo-tech:~/p$ \n"
    )
    assert app._codex_launch_was_attempted(pane)
    assert app._codex_is_down_recoverably(pane)


def test_plain_shell_with_no_codex_history_is_left_alone():
    pane = "nimrod_rotem@grabo-tech:~$ ls\nREADME.md\nnimrod_rotem@grabo-tech:~$ \n"
    assert app._looks_like_bare_shell(pane)
    assert not app._codex_is_down_recoverably(pane)


def test_typed_input_blocks_a_relaunch():
    pane = "nimrod_rotem@grabo-tech:~/p$ git status --short"
    assert app._shell_has_pending_input(pane)


def test_bash_continuation_prompt_counts_as_a_stuck_shell():
    assert app._looks_like_stuck_shell(CONTINUATION_PANE)
    assert not app._looks_like_stuck_shell(HEALTHY_PANE)
    assert not app._shell_has_pending_input(CONTINUATION_PANE)


def test_failure_excerpt_names_the_real_error():
    excerpt = app._codex_failure_excerpt(BROKEN_PANE)
    assert "invalid transport" in excerpt
    assert "config.toml" in excerpt
    assert "$" not in excerpt.split("/")[0]  # no shell prompt leaked in


# --- alerts ------------------------------------------------------------------

@pytest.fixture
def alert_store(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "CODEX_ALERTS_FILE", tmp_path / "codex-alerts.json")
    yield


def test_alert_is_recorded_and_persisted(alert_store):
    app._record_codex_alert("s1", "codex-not-running", "invalid transport", username="guy")
    rows, _auth = app._codex_alerts_snapshot()
    assert len(rows) == 1
    assert rows[0]["session_name"] == "s1"
    assert rows[0]["username"] == "guy"
    assert rows[0]["count"] == 1
    stored = json.loads(app.CODEX_ALERTS_FILE.read_text())
    assert stored["alerts"][0]["kind"] == "codex-not-running"


def test_repeat_alerts_fold_into_one_row(alert_store):
    for _ in range(4):
        app._record_codex_alert("s1", "codex-not-running", "boom")
    rows, _auth = app._codex_alerts_snapshot()
    assert len(rows) == 1
    assert rows[0]["count"] == 4


def test_resolving_closes_open_alerts(alert_store):
    app._record_codex_alert("s1", "codex-not-running", "boom")
    app._resolve_codex_alerts("s1", "relaunched")
    rows, _auth = app._codex_alerts_snapshot()
    assert rows[0]["resolved"] is True
    assert app._codex_alerts_snapshot(include_resolved=False)[0] == []


def test_alerts_written_by_one_process_are_read_by_another(alert_store):
    """The watchdog runs in the controller process; the API is a separate
    worker. Only the file is shared, so a read must not depend on memory."""
    app._record_codex_alert("s1", "codex-not-running", "boom")
    rows, _auth = app._codex_alerts_snapshot()
    assert len(rows) == 1          # read straight back off disk
    app.CODEX_ALERTS_FILE.write_text(json.dumps({"alerts": [], "auth": {}}))
    rows, _auth = app._codex_alerts_snapshot()
    assert rows == []              # and reflects an external write immediately


def test_legacy_bare_list_file_is_still_readable(alert_store):
    app.CODEX_ALERTS_FILE.write_text(json.dumps(
        [{"id": "x", "session_name": "s9", "kind": "old", "detail": "d",
          "count": 1, "first_ts": 1, "last_ts": 1, "resolved": False}]))
    rows, auth = app._codex_alerts_snapshot()
    assert rows[0]["session_name"] == "s9"
    assert auth == {}


def test_auth_state_is_published_for_the_api_workers(alert_store):
    app._record_codex_alert("s1", "codex-not-running", "boom")
    app._publish_codex_auth_state({"loggedIn": False, "reason": "revoked"})
    rows, auth = app._codex_alerts_snapshot()
    assert auth["loggedIn"] is False
    assert auth["reason"] == "revoked"
    assert len(rows) == 1          # publishing auth must not drop alerts


# --- per-account usage -------------------------------------------------------

def _rollout(home, name, records):
    day = home / "sessions" / "2026" / "08" / "03"
    day.mkdir(parents=True, exist_ok=True)
    path = day / f"rollout-{name}.jsonl"
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
    return path


def _token_event(ts, inp, out, cached=0, reasoning=0):
    return {
        "timestamp": ts,
        "type": "event_msg",
        "payload": {
            "type": "token_count",
            "info": {
                "total_token_usage": {"input_tokens": 999999},
                "last_token_usage": {
                    "input_tokens": inp,
                    "output_tokens": out,
                    "cached_input_tokens": cached,
                    "reasoning_output_tokens": reasoning,
                },
            },
        },
    }


def test_token_usage_sums_turn_deltas_not_running_totals(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _rollout(home, "a", [
        {"type": "turn_context", "payload": {"model": "gpt-5.6-sol"}},
        _token_event("2026-08-03T05:00:00Z", 100, 10),
        _token_event("2026-08-03T06:00:00Z", 200, 20, cached=50, reasoning=5),
    ])
    totals = _token_usage_windows(home, "2026-08-03T00:00:00+00:00")
    assert totals["today"]["inputTokens"] == 300
    assert totals["today"]["outputTokens"] == 30
    assert totals["today"]["cacheReadTokens"] == 50
    assert totals["today"]["reasoningTokens"] == 5
    # cached input is a SUBSET of input and reasoning a subset of output, so the
    # total is input + output — exactly Codex's own total_tokens, not 385.
    assert totals["today"]["totalTokens"] == 330
    assert totals["today"]["turns"] == 2


def _token_usage_windows(home, today_cutoff):
    return app._token_usage_for_home(
        home, {"today": today_cutoff, "week": "2026-07-27T00:00:00+00:00"}
    )


def test_records_before_the_cutoff_are_excluded(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    _rollout(home, "a", [
        _token_event("2026-07-30T05:00:00Z", 100, 10),   # in the week, not today
        _token_event("2026-08-03T05:00:00Z", 7, 3),      # today
    ])
    totals = _token_usage_windows(home, "2026-08-03T00:00:00+00:00")
    assert totals["today"]["totalTokens"] == 10
    assert totals["week"]["totalTokens"] == 120


def test_missing_sessions_dir_is_zero_not_an_error(tmp_path):
    totals = _token_usage_windows(tmp_path / "nothing-here", "2026-08-03T00:00:00+00:00")
    assert totals["today"]["totalTokens"] == 0
    assert totals["week"]["turns"] == 0


def test_prompt_counts_split_today_from_the_week(tmp_path, monkeypatch):
    audit = tmp_path / "prompt-history.jsonl"
    now = time.time()
    rows = [
        {"id": "1", "ts": now - 6 * 86400, "user_id": "u_a", "prompt": "old"},
        {"id": "2", "ts": now - 60, "user_id": "u_a", "prompt": "recent"},
        {"id": "3", "ts": now - 30, "user_id": "u_b", "prompt": "recent"},
        {"id": "4", "ts": now - 30 * 86400, "user_id": "u_a", "prompt": "ancient"},
    ]
    audit.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
    monkeypatch.setattr(app, "PROMPT_AUDIT_FILE", audit)
    counts = app._prompt_counts_by_user({
        "today": now - 3600,
        "week": now - 7 * 86400,
    })
    assert counts["u_a"]["today"] == 1
    assert counts["u_a"]["week"] == 2       # the 30-day-old row is excluded
    assert counts["u_b"]["today"] == 1


def test_codex_total_matches_the_rollouts_own_total_tokens():
    """A real token_count payload: total_tokens is input + output, proving
    cached input and reasoning output are subsets, not extras."""
    last = {
        "input_tokens": 181800, "cached_input_tokens": 180992,
        "cache_write_input_tokens": 0, "output_tokens": 380,
        "reasoning_output_tokens": 265, "total_tokens": 182180,
    }
    assert last["input_tokens"] + last["output_tokens"] == last["total_tokens"]


def test_cached_input_is_billed_at_the_cache_rate():
    """A turn that is almost entirely cache reads must not cost full price."""
    mostly_cached = app._codex_turn_cost(181800, 380, 180992, "gpt-5.6-sol")
    all_fresh = app._codex_turn_cost(181800, 380, 0, "gpt-5.6-sol")
    assert mostly_cached < all_fresh / 5
    # 808 fresh + 180992 cached in, 380 out
    expected = (808 * 1.25 + 180992 * 0.125 + 380 * 10.0) / 1e6
    assert abs(mostly_cached - expected) < 1e-9


# --- watchdog interlocks -----------------------------------------------------

def test_auth_probe_is_floored_even_when_forced(monkeypatch):
    """A persistent outage must not spawn a Codex app-server every sweep."""
    calls = []

    def fake_probe(home, validate):
        calls.append(home)
        return {"loggedIn": True, "activeMode": "chatgpt"}

    monkeypatch.setattr(app, "_ensure_codex_auth_with_fallback", fake_probe)
    monkeypatch.setattr(app, "_publish_codex_auth_state", lambda a: None)
    monkeypatch.setattr(app, "_codex_health_auth",
                        {"ts": time.time() - 30, "loggedIn": True, "reason": ""})

    import asyncio
    # 30s old and forced: inside the 120s floor, so no probe.
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        app._codex_auth_health(force=True))
    assert calls == []

    app._codex_health_auth["ts"] = time.time() - 200
    asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        app._codex_auth_health(force=True))
    assert len(calls) == 1        # past the floor, forced: probes


def test_health_and_crash_loops_use_the_same_cooldown_window():
    """The interlock only works if neither loop can act inside the other's
    cooldown, so the two constants must stay aligned."""
    assert app._CODEX_HEALTH_COOLDOWN >= app._CRASH_RECOVERY_INTERVAL
    assert app._CODEX_HEALTH_COOLDOWN == app._CRASH_RECOVERY_COOLDOWN


# --- the shared relaunch guard ------------------------------------------------

STARTING_PANE = """\
nimrod_rotem@grabo-tech:~/p$ exec env CODEX_HOME=/home/nimrod_rotem/.codex-user-u_x codex --yolo
"""


def test_starting_session_is_never_relaunched_into():
    """Codex takes seconds to appear in the process tree, so a poll can catch a
    session that is still booting. The pane then shows the launch command still
    sitting on the prompt line and nothing after it — which reads as pending
    input, and blocks the relaunch. Without that, a watchdog would type a second
    launch line into a booting TUI."""
    assert app._codex_launch_was_attempted(STARTING_PANE)
    # The echoed command makes this look like a prompt AND like typed input;
    # it is the pending-input check that saves us, not the shell check.
    assert app._looks_like_bare_shell(STARTING_PANE)
    assert app._shell_has_pending_input(STARTING_PANE)
    assert not app._pane_is_recoverable_shell(STARTING_PANE)


def test_booting_session_that_cleared_the_screen_is_left_alone():
    """Once Codex takes the alternate screen the pane is no longer a prompt."""
    pane = ("nimrod_rotem@grabo-tech:~/p$ exec env CODEX_HOME=/home/x codex --yolo\n"
            "  Loading...\n")
    assert not app._looks_like_bare_shell(pane)
    assert not app._pane_is_recoverable_shell(pane)


def test_recoverable_shell_accepts_the_real_broken_pane():
    assert app._pane_is_recoverable_shell(BROKEN_PANE)


def test_recoverable_shell_rejects_a_running_tui():
    assert not app._pane_is_recoverable_shell(HEALTHY_PANE)


def test_recoverable_shell_rejects_a_deliberate_shell():
    pane = "nimrod_rotem@grabo-tech:~$ ls\nREADME.md\nnimrod_rotem@grabo-tech:~$ \n"
    assert not app._pane_is_recoverable_shell(pane)


def test_recoverable_shell_rejects_half_typed_input():
    pane = ("exec env CODEX_HOME=/home/x/.codex-user-u_x codex --yolo\n"
            "Error loading config.toml: invalid transport\n"
            "nimrod_rotem@grabo-tech:~/p$ git status --short")
    assert not app._pane_is_recoverable_shell(pane)


def test_recoverable_shell_accepts_a_stuck_continuation(): 
    pane = ("exec env CODEX_HOME=/home/x/.codex-user-u_x codex --yolo\n"
            "Error loading config.toml: invalid transport\n"
            "nimrod_rotem@grabo-tech:~/p$ echo \"unterminated\n>\n")
    assert app._pane_is_recoverable_shell(pane)


def test_recoverable_shell_rejects_empty_output():
    assert not app._pane_is_recoverable_shell("")
    assert not app._pane_is_recoverable_shell("   \n\n")


# --- one accounting rule everywhere ------------------------------------------

def test_estimate_cost_matches_the_codex_turn_cost():
    """Both usage views must price a turn identically, or Stats shows two
    different numbers for the same work."""
    args = (181800, 380, 180992)
    assert app._estimate_cost(*args, 265, "gpt-5.6-sol") == app._codex_turn_cost(*args, "gpt-5.6-sol")


def test_reasoning_is_not_billed_twice():
    """reasoning_output_tokens is a subset of output_tokens, so passing it must
    not change the price."""
    with_reasoning = app._estimate_cost(1000, 500, 0, 400, "gpt-5.6-sol")
    without = app._estimate_cost(1000, 500, 0, 0, "gpt-5.6-sol")
    assert with_reasoning == without
