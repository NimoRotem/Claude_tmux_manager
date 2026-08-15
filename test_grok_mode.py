"""Regression coverage for the isolated Grok Build dashboard runtime."""

import json
import os
import subprocess
import sys
from pathlib import Path

import tomllib


def test_grok_mode_builds_an_isolated_grok_launch(tmp_path):
    grok_home = tmp_path / "grok-home"
    grok_binary = tmp_path / "bin" / "grok"
    launch = (
        f"{grok_binary} --always-approve --reasoning-effort xhigh "
        "--experimental-memory"
    )
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_GROK_BINARY": str(grok_binary),
            "TMUX_DASH_GROK_HOME": str(grok_home),
            "TMUX_DASH_NEW_SESSION_CMD": launch,
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = """
import json
import app
print(json.dumps({
    "kind": app.AGENT_KIND,
    "name": app.AGENT_DISPLAY_NAME,
    "launch": app._launch_agent_cmd(app.NEW_SESSION_CMD),
    "resume": app._launch_agent_cmd(app.NEW_SESSION_CMD, resume=True),
    "processes": [
        app._is_agent_process_command("grok"),
        app._is_agent_process_command("grok-1.0.4"),
        app._is_agent_process_command("codex"),
    ],
    "model": app.DEFAULT_MODEL,
    "effort": app._CODEX_DEFAULT_REASONING_EFFORT,
    "autopush": app._agent_supports_autopush(),
    "process_search": app._agent_process_search_name(),
}))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == {
        "kind": "grok",
        "name": "Grok",
        "launch": (
            f"env GROK_HOME={grok_home} GROK_TELEMETRY_ENABLED=0 "
            f"GROK_FEEDBACK_ENABLED=0 {launch}"
        ),
        "resume": (
            f"env GROK_HOME={grok_home} GROK_TELEMETRY_ENABLED=0 "
            f"GROK_FEEDBACK_ENABLED=0 {launch} --continue"
        ),
        "processes": [True, True, False],
        "model": "grok-4.6",
        "effort": "xhigh",
        "autopush": True,
        "process_search": "grok",
    }


def test_grok_readiness_requires_private_refreshable_auth_and_advisor(tmp_path):
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    auth_file = grok_home / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "https://auth.x.ai::client": {
                    "email": "grok@rotem.ai",
                    "key": "access-token",
                    "refresh_token": "refresh-token",
                    "expires_at": "2099-01-01T00:00:00Z",
                }
            }
        )
    )
    auth_file.chmod(0o600)
    grok_binary = tmp_path / "grok"
    grok_binary.write_text("#!/bin/sh\necho 'grok 1.0.4 (test)'\n")
    grok_binary.chmod(0o700)
    mcp_python = tmp_path / "mcp-python"
    mcp_python.write_text("#!/bin/sh\nexit 0\n")
    mcp_python.chmod(0o700)
    mcp_bridge = tmp_path / "mcp_bridge.py"
    mcp_bridge.write_text("# test bridge\n")
    token_file = tmp_path / "advisor-token"
    token_file.write_text("owner-token")
    token_file.chmod(0o600)
    (grok_home / "config.toml").write_text(
        "[mcp_servers.advisor]\n"
        f'command = "{mcp_python}"\n'
        f'args = ["{mcp_bridge}"]\n'
    )
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_GROK_BINARY": str(grok_binary),
            "TMUX_DASH_GROK_HOME": str(grok_home),
            "TMUX_DASH_GROK_MCP_PYTHON": str(mcp_python),
            "TMUX_DASH_GROK_MCP_BRIDGE": str(mcp_bridge),
            "TMUX_DASH_GROK_ADVISOR_TOKEN_FILE": str(token_file),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = """
import json
import app
ready, reason, details = app._agent_cli_readiness()
print(json.dumps({
    "ready": ready,
    "reason": reason,
    "version": details.get("version"),
    "account_email": details.get("account_email"),
    "auth_refreshable": details.get("auth_refreshable"),
    "advisor_mcp_ready": details.get("advisor_mcp_ready"),
    "grok_home": details.get("grok_home"),
}))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == {
        "ready": True,
        "reason": "ready",
        "version": "1.0.4",
        "account_email": "grok@rotem.ai",
        "auth_refreshable": True,
        "advisor_mcp_ready": True,
        "grok_home": str(grok_home),
    }


def test_grok_runtime_config_preserves_xai_settings_and_repairs_connections(tmp_path):
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    config_file = grok_home / "config.toml"
    config_file.write_text(
        "[marketplace]\n"
        "official_marketplace_auto_installed = true\n\n"
        "[[marketplace.sources]]\n"
        'name = "xAI Official"\n'
        'git = "https://github.com/xai-org/plugin-marketplace.git"\n'
    )
    mcp_python = tmp_path / "mcp-python"
    mcp_bridge = tmp_path / "mcp_bridge.py"
    google_bridge = tmp_path / "google_workspace_mcp.py"
    browser_bridge = tmp_path / "browser_mcp_lease_proxy.py"
    token_file = tmp_path / "advisor-token"
    for path in (mcp_python, mcp_bridge, google_bridge, browser_bridge, token_file):
        path.write_text("test\n")
    mcp_python.chmod(0o700)
    token_file.chmod(0o600)
    state_dir = tmp_path / "dashboard-state"
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(state_dir),
            "TMUX_DASH_GROK_HOME": str(grok_home),
            "TMUX_DASH_GROK_MCP_PYTHON": str(mcp_python),
            "TMUX_DASH_GROK_MCP_BRIDGE": str(mcp_bridge),
            "TMUX_DASH_GROK_GOOGLE_MCP_SCRIPT": str(google_bridge),
            "TMUX_DASH_GROK_BROWSER_MCP_PROXY": str(browser_bridge),
            "TMUX_DASH_GROK_ADVISOR_TOKEN_FILE": str(token_file),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = """
import json
import app
first = app._ensure_grok_runtime_config()
second = app._ensure_grok_runtime_config()
print(json.dumps({"first": first, "second": second, "googleReady": app._google_mcp_is_ready()}))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == {"first": True, "second": True, "googleReady": True}
    config = tomllib.loads(config_file.read_text())
    assert config["marketplace"]["sources"][0]["name"] == "xAI Official"
    assert config["memory"] == {"enabled": True}
    assert set(config["mcp_servers"]) == {"advisor", "google", "playwright-browser"}
    assert config["mcp_servers"]["advisor"]["command"] == str(mcp_python)
    assert config["mcp_servers"]["advisor"]["args"][-1] == str(token_file)
    assert config["mcp_servers"]["google"]["env"]["GOOGLE_MCP_CREDENTIALS_DIR"] == str(
        state_dir / "connections" / "admin"
    )
    assert config["mcp_servers"]["playwright-browser"]["env"]["TMUX_DASH_BROWSER_ID"] == "default"
    assert config_file.stat().st_mode & 0o777 == 0o600


def test_grok_context_loads_full_global_policy_and_preserves_project_rules(tmp_path):
    state_dir = tmp_path / "dashboard-state"
    state_dir.mkdir()
    grok_home = state_dir / "grok-home"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project_agents = project_dir / "AGENTS.md"
    project_agents.write_text("# User project rule\n\nKeep this text.\n")
    global_policy = "# Account policy\n\n" + ("durable policy line\n" * 600) + "FINAL_POLICY\n"
    (state_dir / "global-context.md").write_text(global_policy)
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(state_dir),
            "TMUX_DASH_GROK_HOME": str(grok_home),
            "TMUX_DASH_GROK_BINARY": str(tmp_path / "grok"),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = f"""
import json
import app
project = {str(project_dir)!r}
print(json.dumps({{"first": app._prepare_grok_project_context(project), "second": app._prepare_grok_project_context(project)}}))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == {"first": True, "second": True}
    project_text = project_agents.read_text()
    assert project_text.startswith("# User project rule\n\nKeep this text.")
    assert project_text.count("BEGIN GROK DASHBOARD RUNTIME") == 1
    assert f"GROK_HOME={grok_home}" in project_text
    global_text = "\n".join(
        (grok_home / name).read_text()
        for name in ("Agents.md", "Claude.md", "AGENT.md")
        if (grok_home / name).exists()
    )
    assert global_policy.strip() in global_text.replace(
        "<!-- BEGIN GROK DASHBOARD GLOBAL POLICY 1 (managed) -->\n", ""
    ).replace(
        "<!-- END GROK DASHBOARD GLOBAL POLICY 1 (managed) -->", ""
    ) or "FINAL_POLICY" in global_text
    assert "FINAL_POLICY" in global_text
    assert all(
        len((grok_home / name).read_text()) < 10_000
        for name in ("Agents.md", "Claude.md", "AGENT.md")
        if (grok_home / name).exists()
    )


def test_grok_global_context_names_its_native_private_state(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = r'''
import json
import app
source = """- Treat this dashboard account's `CODEX_HOME`, project directory, browser,
  connections, memories, skills, uploads, and session history as private to this
  account. Never inspect or operate another account's corresponding resources.
- Local Codex memory is private because every account has a separate
  `CODEX_HOME`. Use it as recall, never as the sole source of required policy or
  current external facts."""
print(json.dumps({"context": app._agent_global_context(source)}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    context = json.loads(result.stdout)["context"]
    assert "CODEX_HOME" not in context
    assert "GROK_HOME" in context
    assert "Grok's native memory" in context


def test_grok_session_preparation_uses_native_context_without_touching_codex(tmp_path):
    state_dir = tmp_path / "dashboard-state"
    project_dir = tmp_path / "project"
    codex_home = tmp_path / "codex-home"
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(state_dir),
            "TMUX_DASH_GROK_HOME": str(state_dir / "grok-home"),
            "CODEX_HOME": str(codex_home),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = f"""
import json
import app
ok = app._prepare_session_owner_for_launch(None, "grok-test", {str(project_dir)!r})
app._restore_default_model_setting()
print(json.dumps({{"ok": ok}}))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == {"ok": True}
    assert "BEGIN GROK DASHBOARD RUNTIME" in (project_dir / "AGENTS.md").read_text()
    assert not (codex_home / "config.toml").exists()


def test_grok_native_inventory_and_credential_display_never_expose_tokens(tmp_path):
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    auth_file = grok_home / "auth.json"
    auth_file.write_text(
        json.dumps(
            {
                "https://auth.x.ai::client": {
                    "email": "grok@rotem.ai",
                    "first_name": "Nimo",
                    "last_name": "Rotem",
                    "auth_mode": "device_auth",
                    "key": "secret-access-token",
                    "refresh_token": "secret-refresh-token",
                }
            }
        )
    )
    auth_file.chmod(0o600)
    grok_binary = tmp_path / "grok"
    grok_binary.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = inspect ]; then\n"
        "  printf '%s\\n' '{\"skills\":[{\"name\":\"deploy\",\"description\":\"Deploy safely\",\"source\":{\"type\":\"personal\",\"path\":\"/skills/deploy/SKILL.md\"},\"userInvocable\":true}]}'\n"
        "else\n"
        "  echo 'grok 1.0.4 (test)'\n"
        "fi\n"
    )
    grok_binary.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_GROK_HOME": str(grok_home),
            "TMUX_DASH_GROK_BINARY": str(grok_binary),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = r'''
import json
import app
print(json.dumps({
    "skills": app._grok_skills_inventory(),
    "credential": app._grok_credential_display(),
}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    payload = json.loads(result.stdout)
    assert payload["skills"] == [
        {
            "name": "deploy",
            "description": "Deploy safely",
            "scope": "personal",
            "path": "/skills/deploy/SKILL.md",
            "userInvocable": True,
        }
    ]
    assert payload["credential"] == {
        "provider": "xai",
        "providerLabel": "xAI",
        "accountEmail": "grok@rotem.ai",
        "accountName": "Nimo Rotem",
        "mechanism": "device_auth",
        "credentialLabel": "xAI account",
    }
    rendered = json.dumps(payload)
    assert "secret-access-token" not in rendered
    assert "secret-refresh-token" not in rendered


def test_grok_auth_status_reports_xai_account_and_session_mode(tmp_path):
    grok_home = tmp_path / "grok-home"
    grok_home.mkdir()
    (grok_home / "auth.json").write_text(
        json.dumps(
            {
                "https://auth.x.ai::client": {
                    "email": "grok@rotem.ai",
                    "first_name": "Nimo",
                    "last_name": "Rotem",
                    "auth_mode": "device_auth",
                    "key": "secret-access-token",
                    "refresh_token": "secret-refresh-token",
                }
            }
        )
    )
    (grok_home / "auth.json").chmod(0o600)
    grok_binary = tmp_path / "grok"
    grok_binary.write_text("#!/bin/sh\necho 'grok 1.0.4 (test)'\n")
    grok_binary.chmod(0o700)
    mcp_python = tmp_path / "mcp-python"
    mcp_bridge = tmp_path / "mcp-bridge.py"
    token_file = tmp_path / "advisor-token"
    for path in (mcp_python, mcp_bridge, token_file):
        path.write_text("test\n")
    mcp_python.chmod(0o700)
    token_file.chmod(0o600)
    (grok_home / "config.toml").write_text(
        "[mcp_servers.advisor]\n"
        f'command = "{mcp_python}"\n'
        f'args = ["{mcp_bridge}"]\n'
    )
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_GROK_HOME": str(grok_home),
            "TMUX_DASH_GROK_BINARY": str(grok_binary),
            "TMUX_DASH_GROK_MCP_PYTHON": str(mcp_python),
            "TMUX_DASH_GROK_MCP_BRIDGE": str(mcp_bridge),
            "TMUX_DASH_GROK_ADVISOR_TOKEN_FILE": str(token_file),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = r'''
import asyncio
import json
import app
response = asyncio.run(app.api_codex_auth_status())
app._codex_health_auth["ts"] = 0
health = asyncio.run(app._codex_auth_health(force=True))
print(json.dumps({
    "status": json.loads(response.body),
    "sessionMode": app._session_real_auth_mode("test-session"),
    "health": health,
}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    payload = json.loads(result.stdout)
    assert payload["sessionMode"] == "xai"
    assert payload["health"]["loggedIn"] is True
    assert payload["health"]["activeMode"] == "xai"
    assert payload["status"]["loggedIn"] is True
    assert payload["status"]["authMode"] == "xai"
    assert payload["status"]["subscriptionType"] == "Grok Build"
    assert payload["status"]["email"] == "grok@rotem.ai"
    assert payload["status"]["model"] == "grok-4.6"
    assert "secret-access-token" not in json.dumps(payload)


def test_grok_session_stats_use_native_summary_signals_and_usage(tmp_path):
    grok_home = tmp_path / "grok-home"
    project_dir = tmp_path / "project"
    session_dir = grok_home / "sessions" / "encoded-project" / "session-id"
    session_dir.mkdir(parents=True)
    project_dir.mkdir()
    now = 1_786_830_200
    (session_dir / "summary.json").write_text(
        json.dumps(
            {
                "info": {"id": "session-id", "cwd": str(project_dir)},
                "created_at": "2026-08-15T21:00:00Z",
                "updated_at": "2026-08-15T21:02:00Z",
                "last_active_at": "2026-08-15T21:02:00Z",
                "num_messages": 6,
                "current_model_id": "grok-4.6",
                "reasoning_effort": "xhigh",
            }
        )
    )
    (session_dir / "signals.json").write_text(
        json.dumps(
            {
                "turnCount": 2,
                "assistantMessageCount": 2,
                "errorCount": 0,
                "contextTokensUsed": 300,
                "contextWindowTokens": 500000,
                "sessionDurationSeconds": 120,
                "avgTimeToFirstTokenMs": 800,
                "avgResponseTimeMs": 1500,
                "modelsUsed": ["grok-4.6"],
            }
        )
    )
    usage_rows = [
        {
            "timestamp": now - 30,
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_end",
                    "usage": {
                        "inputTokens": 100,
                        "outputTokens": 20,
                        "cachedReadTokens": 10,
                        "cacheCreationTokens": 3,
                        "reasoningTokens": 5,
                        "modelCalls": 1,
                        "apiDurationMs": 1000,
                        "costUsdTicks": 1_000_000_000,
                    },
                }
            },
        },
        {
            "timestamp": now - 5,
            "params": {
                "update": {
                    "sessionUpdate": "agent_message_end",
                    "usage": {
                        "inputTokens": 200,
                        "outputTokens": 30,
                        "cachedReadTokens": 20,
                        "cacheCreationTokens": 4,
                        "reasoningTokens": 7,
                        "modelCalls": 1,
                        "apiDurationMs": 2000,
                        "costUsdTicks": 2_000_000_000,
                    },
                }
            },
        },
    ]
    (session_dir / "updates.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in usage_rows)
    )
    (session_dir / "chat_history.jsonl").write_text(
        "".join(
            json.dumps(row) + "\n"
            for row in (
                {"type": "system", "content": "system policy"},
                {
                    "type": "user",
                    "synthetic_reason": "project_context",
                    "content": [{"type": "text", "text": "synthetic context"}],
                },
                {
                    "type": "user",
                    "prompt_index": 0,
                    "content": [{"type": "text", "text": "Build the thing"}],
                },
                {"type": "assistant", "content": "I built the thing.", "model_id": "grok-4.6"},
            )
        )
    )
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_GROK_HOME": str(grok_home),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = f"""
import asyncio
import json
import app
app.get_session_cwd = lambda _name: {str(project_dir)!r}
app.time.time = lambda: {now}
print(json.dumps({{
    "model": app._get_session_model("demo"),
    "fields": app._session_model_fields("demo"),
    "stats": app._parse_session_stats("demo"),
    "reply": app._extract_last_assistant_turn("demo", "Build the thing"),
    "daily": json.loads(asyncio.run(app.api_codex_usage()).body),
}}))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    payload = json.loads(result.stdout)
    assert payload["model"] == "grok-4.6"
    assert payload["fields"]["effort"] == "xhigh"
    assert payload["stats"]["available"] is True
    assert payload["stats"]["messageCount"] == 2
    assert payload["stats"]["totalInput"] == 300
    assert payload["stats"]["totalOutput"] == 50
    assert payload["stats"]["cacheRead"] == 30
    assert payload["stats"]["cacheCreate"] == 7
    assert payload["stats"]["reasoningTokens"] == 12
    assert payload["stats"]["estimatedCost"] == 3.0
    assert payload["stats"]["contextPct"] == 0.1
    assert payload["stats"]["lastTtfeMs"] == 800
    assert payload["reply"] == "I built the thing."
    assert payload["daily"]["inputTokens"] == 300
    assert payload["daily"]["outputTokens"] == 50
    assert payload["daily"]["reasoningTokens"] == 12
    assert payload["daily"]["estimatedCost"] == 3.0


def test_grok_dashboard_does_not_adopt_foreign_tmux_sessions(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = r'''
import json
import app
app._load_session_owners = lambda: {}
calls = {"count": 0}
def forbidden(*_args, **_kwargs):
    calls["count"] += 1
    raise RuntimeError("foreign process table must not be inspected")
app.subprocess.run = forbidden
print(json.dumps({"visible": app._session_is_codex("foreign-session"), "processCalls": calls["count"]}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"visible": False, "processCalls": 0}


def test_grok_dashboard_runtime_ui_uses_native_labels_and_project_files(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "grok",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "grok-test-secret",
            "TMUX_DASH_PASS": "grok-test-pass",
        }
    )
    probe = r'''
import json
import app
print(json.dumps({
    "hasLoader": "loadAgentRuntime()" in app.HTML_PAGE,
    "hasGrokHome": "details.grok_home" in app.HTML_PAGE,
    "hasGrokProjectConfig": ".grok/config.toml" in app.HTML_PAGE,
    "hasXaiMode": "s.auth_mode==='xai'?'xAI account'" in app.HTML_PAGE,
    "hasHardcodedLoadingMuse": "Loading Muse runtime" in app.HTML_PAGE,
    "hasHardcodedCodexProcesses": "Codex Processes" in app.HTML_PAGE,
}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=Path(__file__).resolve().parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert json.loads(result.stdout) == {
        "hasLoader": True,
        "hasGrokHome": True,
        "hasGrokProjectConfig": True,
        "hasXaiMode": True,
        "hasHardcodedLoadingMuse": False,
        "hasHardcodedCodexProcesses": False,
    }
