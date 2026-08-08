"""Regression coverage for the isolated Muse dashboard runtime mode."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

from runtime_control import scoped_codex_command


def test_muse_deploy_enables_admin_google_login():
    config = (
        Path(__file__).resolve().parent
        / "deploy"
        / "muse-dashboard.supervisor.conf"
    ).read_text()

    assert (
        'TMUX_DASH_ADMIN_GOOGLE_EMAIL="nimrod.rotem@gmail.com"' in config,
        'TMUX_DASH_GOOGLE_DOMAINS="grabo.com,nemopowertools.com"' in config,
    ) == (True, True)


def test_state_directory_can_be_isolated_per_deployment(tmp_path):
    state_dir = tmp_path / "muse-state"
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_STATE_DIR": str(state_dir),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )

    result = subprocess.run(
        [sys.executable, "-c", "import app; print(app.MESSAGES_DIR)"],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(state_dir)


def test_muse_mode_builds_an_isolated_muse_launch(tmp_path):
    state_dir = tmp_path / "dashboard-state"
    config_home = tmp_path / "muse-config"
    data_home = tmp_path / "muse-data"
    muse_binary = tmp_path / "bin" / "muse"
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(state_dir),
            "TMUX_DASH_MUSE_BINARY": str(muse_binary),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(config_home),
            "TMUX_DASH_MUSE_DATA_HOME": str(data_home),
            "TMUX_DASH_NEW_SESSION_CMD": f"{muse_binary} --yolo",
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
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
        app._is_agent_process_command("muse"),
        app._is_agent_process_command("muse-bin-0.1.0-R708.1"),
        app._is_agent_process_command("codex"),
    ],
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

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    expected_prefix = (
        f"env MUSE_NO_AUTO_UPDATE=1 XDG_CONFIG_HOME={config_home} "
        f"XDG_DATA_HOME={data_home}"
    )
    assert data["kind"] == "muse"
    assert data["name"] == "Muse"
    assert data["launch"] == f"{expected_prefix} {muse_binary} --yolo"
    assert data["resume"] == (
        f"{expected_prefix} {muse_binary} --yolo resume --last"
    )
    assert data["processes"] == [True, True, False]


def test_session_launcher_routes_through_muse_backend(tmp_path):
    config_home = tmp_path / "muse-config"
    data_home = tmp_path / "muse-data"
    muse_binary = tmp_path / "bin" / "muse"
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_BINARY": str(muse_binary),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(config_home),
            "TMUX_DASH_MUSE_DATA_HOME": str(data_home),
            "TMUX_DASH_NEW_SESSION_CMD": f"{muse_binary} --yolo",
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
import app
app._session_launch_identity_prefix = lambda _name: "owner-env"
app._session_unix_account_prefix = lambda _name, launch: launch
app.scoped_codex_command = lambda _name, launch, **_kwargs: launch
print(app._session_launch_command("muse-test", app.NEW_SESSION_CMD))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == (
        f"owner-env env MUSE_NO_AUTO_UPDATE=1 XDG_CONFIG_HOME={config_home} "
        f"XDG_DATA_HOME={data_home} {muse_binary} --yolo"
    )


def test_muse_readiness_requires_the_isolated_credential(tmp_path):
    config_home = tmp_path / "muse-config"
    auth_file = config_home / "muse" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text('{"provider":"meta"}')
    auth_file.chmod(0o600)
    (config_home / "muse" / "settings.json").write_text(json.dumps({
        "schema_version": 1,
        "mcp_servers": {"advisor": {"transport": "stdio"}},
    }))
    muse_binary = tmp_path / "muse"
    muse_binary.write_text("#!/bin/sh\necho 'Muse Code 0.1.0 (test)'\n")
    muse_binary.chmod(0o700)
    mcp_python = tmp_path / "mcp-python"
    mcp_python.write_text("#!/bin/sh\nexit 0\n")
    mcp_python.chmod(0o700)
    mcp_bridge = tmp_path / "muse_mcp_bridge.py"
    mcp_bridge.write_text("# test bridge\n")
    token_file = tmp_path / "advisor-token"
    token_file.write_text("owner-token")
    token_file.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_BINARY": str(muse_binary),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(config_home),
            "TMUX_DASH_MUSE_DATA_HOME": str(tmp_path / "muse-data"),
            "TMUX_DASH_MUSE_MCP_PYTHON": str(mcp_python),
            "TMUX_DASH_MUSE_MCP_BRIDGE": str(mcp_bridge),
            "TMUX_DASH_MUSE_ADVISOR_TOKEN_FILE": str(token_file),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
import json
import app
ready, reason, details = app._agent_cli_readiness()
print(json.dumps({"ready": ready, "reason": reason, "details": details}))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["ready"] is True
    assert data["reason"] == "ready"
    assert data["details"]["version"] == "0.1.0"
    assert data["details"]["auth_configured"] is True
    assert data["details"]["advisor_mcp_ready"] is True


def test_health_check_recognizes_the_versioned_muse_process(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
from types import SimpleNamespace
import app
app.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="100\\n")
app._process_tree_snapshot = lambda: ({"100": ["101"]}, {"100": "bash", "101": "muse-bin-0.1.0-R708.1"})
print(app._is_codex_running("muse-test"))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"


def test_dashboard_auth_can_load_private_file_credentials(tmp_path):
    password_file = tmp_path / "dashboard-password"
    secret_file = tmp_path / "dashboard-secret"
    password_file.write_text("private-pass\n")
    secret_file.write_text("stable-signing-secret\n")
    password_file.chmod(0o600)
    secret_file.chmod(0o600)
    env = os.environ.copy()
    env.pop("TMUX_DASH_PASS", None)
    env.pop("TMUX_DASH_SECRET", None)
    env.update(
        {
            "TMUX_DASH_PASS_FILE": str(password_file),
            "TMUX_DASH_SECRET_FILE": str(secret_file),
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
        }
    )

    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import app; print(app.AUTH_PASS); print(app.AUTH_SECRET)",
        ],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["private-pass", "stable-signing-secret"]


def test_muse_session_metadata_uses_muse_provider_and_defaults(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_DEFAULT_MODEL": "muse-spark-1.2-contributor",
            "TMUX_DASH_DEFAULT_REASONING_EFFORT": "high",
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
            "CODEX_HOME": str(tmp_path / "unrelated-codex-home"),
        }
    )
    probe = """
import json
import app
print(json.dumps({
    "auth_mode": app._session_real_auth_mode("muse-test"),
    "fields": app._session_model_fields("muse-test"),
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

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data == {
        "auth_mode": "meta",
        "fields": {
            "model": "muse-spark-1.2-contributor",
            "model_pending": "",
            "effort": "high",
        },
    }


def test_runtime_scope_can_use_a_muse_unit_prefix():
    command = scoped_codex_command(
        "muse-test",
        "muse --yolo",
        unit_prefix="muse",
    )

    assert "--unit=muse-muse-test-" in command
    assert "--unit=codex-" not in command


def test_muse_ui_keeps_codex_only_controls_separate_but_enables_auto_push(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
import json
import app
print(json.dumps({
    "autopush": app._get_autopush_mode("muse-test"),
    "autopush_supported": app._agent_supports_autopush(),
    "codex_controls": app._agent_supports_codex_controls(),
    "admin_tabs": [row["id"] for row in app._settings_tab_defs(True)],
    "member_tabs": [row["id"] for row in app._settings_tab_defs(False)],
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

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "autopush": "basic",
        "autopush_supported": True,
        "codex_controls": False,
        "admin_tabs": ["runtime", "history", "browser", "apis"],
        "member_tabs": ["runtime", "history", "browser"],
    }


def test_idle_muse_composer_is_not_reported_as_working(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import json
from types import SimpleNamespace
import app

responses = iter([
    SimpleNamespace(returncode=0, stdout="muse-bin-0.1.0-R708.1:123\n"),
    SimpleNamespace(returncode=0, stdout="""Muse Code

── Voice input (Alt + v to start) ───────────────────────────────────── ⟩
  muse-spark-1.2-contributor · high · /tmp/project · YOLO
"""),
])
app.subprocess.run = lambda *_args, **_kwargs: next(responses)
print(json.dumps(app._detect_activity_raw("muse-test")))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "status": "idle",
        "command": "muse-bin-0.1.0-R708.1",
        "detail": "",
    }


def test_idle_muse_composer_on_its_own_line_is_not_reported_as_working(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import json
from types import SimpleNamespace
import app
responses = iter([
    SimpleNamespace(returncode=0, stdout="muse-bin-0.1.0-R708.1:123\n"),
    SimpleNamespace(returncode=0, stdout="""Muse Code 0.1.0
── Voice input (Alt + v to start) ──────────────────────────────
⟩
────────────────────────────────────────────────────────────────
  muse-spark-1.2-contributor · high · /tmp/project · YOLO
"""),
])
app.subprocess.run = lambda *_args, **_kwargs: next(responses)
print(json.dumps(app._detect_activity_raw("muse-test")))
'''
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "idle"


def test_muse_autopush_recognizes_real_menu_and_protects_typed_drafts(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import json
import app
menu = """◆ Request user input Confirm (0s)
  Please choose Yes or No:
  › 1. Yes                Proceed with Yes
    2. No                 Proceed with No
    3. None of the above  Optionally, add details in notes (tab).
  Enter to select · ↑/↓ to move · Tab for an optional note
── Voice input (Alt + v to start) ──────────────────────────────
⟩
"""
print(json.dumps({
    "prompt": app._detect_interactive_prompt(menu),
    "options": app._parse_menu_options(menu),
    "empty_draft": app._has_pending_user_input("⟩\n"),
    "typed_draft": app._has_pending_user_input("⟩ draft text\n"),
}))
'''
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "prompt": "selection_prompt",
        "options": [[[1, "Yes                Proceed with Yes"],
                     [2, "No                 Proceed with No"],
                     [3, "None of the above  Optionally, add details in notes (tab)."]], 0],
        "empty_draft": False,
        "typed_draft": True,
    }


def test_muse_autopush_llm_uses_meta_fallback_without_openai_key(tmp_path):
    env = os.environ.copy()
    env.pop("OPENAI_API_KEY", None)
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import asyncio
import json
import app
calls = []
async def fallback(system_prompt, user_content, max_tokens, response_format):
    calls.append({
        "system": system_prompt,
        "user": user_content,
        "max_tokens": max_tokens,
        "response_format": response_format,
    })
    return '{"action":"wait"}'
app.client = None
app._muse_meta_llm_call = fallback
result = asyncio.run(app.llm_call("system", "screen", 160, {"type":"json_object"}))
print(json.dumps({"result": result, "calls": calls}))
'''
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "result": '{"action":"wait"}',
        "calls": [{
            "system": "system",
            "user": "screen",
            "max_tokens": 160,
            "response_format": {"type": "json_object"},
        }],
    }


def test_new_agent_pane_starts_without_typing_launch_commands(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    project_dir = tmp_path / "project"
    probe = f'''
import json
from types import SimpleNamespace
import app

calls = []
def run(argv, **kwargs):
    calls.append(argv)
    return SimpleNamespace(returncode=0, stdout="", stderr="")
app.subprocess.run = run
ok = app._launch_agent_pane(
    "muse-test",
    {str(project_dir)!r},
    "muse --yolo",
    {{"DASH_USER": "admin", "DASH_SESSION": "muse-test"}},
)
print(json.dumps({{"ok": ok, "calls": calls}}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["ok"] is True
    assert data["calls"] == [[
        "tmux",
        "respawn-pane",
        "-k",
        "-t",
        "muse-test",
        "-c",
        str(project_dir),
        "-e",
        "DASH_SESSION=muse-test",
        "-e",
        "DASH_USER=admin",
        "muse --yolo",
    ]]
    assert all("send-keys" not in call for call in data["calls"])


def test_muse_usage_uses_runtime_catalog_prices_and_never_double_counts(tmp_path):
    data_home = tmp_path / "muse-data"
    catalog_dir = data_home / "muse" / "model-catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "meta.json").write_text(json.dumps({
        "schema_version": 1,
        "provider_id": "meta",
        "rows": [{
            "model_id": "muse-spark-1.2-contributor",
            "provider_id": "meta",
            "context_limit": 1000000,
            "cost": {"input": "0.10", "output": "0.20", "cached": "0.002", "currency": "USD"},
        }],
    }))
    session_file = tmp_path / "session.jsonl"
    events = [
        {
            "recorded_at": 1_786_208_100_000_000,
            "payload_type": "runtime.session.metadata",
            "payload": {"record": {"model_id": "muse-spark-1.2-contributor", "provider_id": "meta"}},
        },
        {
            "recorded_at": 1_786_208_101_000_000,
            "payload_type": "runtime.session",
            "payload": {"kind": "run", "event": {
                "kind": "model_completed",
                "duration_ms": 2000,
                "model": "muse-spark-1.2-contributor",
                "usage": {
                    "input_tokens": 1000,
                    "output_tokens": 200,
                    "cached_tokens": 600,
                    "cache_read_tokens": 600,
                    "cache_write_tokens": 0,
                    "reasoning_tokens": 50,
                },
            }},
        },
        {
            "recorded_at": 1_786_208_102_000_000,
            "payload_type": "runtime.session",
            "payload": {"kind": "task", "event": {
                "kind": "status",
                "task_id": "model-task",
                "message": "rate limited by provider on attempt 2/10",
                "details": {"facets": [{
                    "kind": "external_attempt",
                    "attempt": 2,
                    "max_attempts": 10,
                    "error_kind": "rate_limited",
                    "operation": "model.response",
                    "system": "meta",
                }]},
            }},
        },
    ]
    session_file.write_text("".join(json.dumps(event) + "\n" for event in events))
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_DATA_HOME": str(data_home),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = f'''
import json
import app
print(json.dumps(app._parse_muse_usage_files([{str(session_file)!r}], now_epoch=1786208102.5)))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["available"] is True
    assert data["totalInput"] == 1000
    assert data["totalOutput"] == 200
    assert data["cacheRead"] == 600
    assert data["reasoningTokens"] == 50
    assert data["totalTokens"] == 1200
    # (400 fresh input * $0.10/M) + (600 cached * $0.002/M) + (200 output * $0.20/M)
    assert data["estimatedCost"] == 0.0000812
    assert data["recentOutputRate"] == 6000
    assert data["requestsPerMinute"] == 1
    assert data["rateStatus"] == "rate_limited"
    assert data["providerStatus"] == "Rate limited · retry 2/10"
    assert data["retryCount"] == 1


def test_muse_auth_display_identifies_key_without_exposing_it(tmp_path):
    config_home = tmp_path / "muse-config"
    auth_file = config_home / "muse" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text(json.dumps({
        "schema_version": 1,
        "providers": {"meta": {
            "api_key": "LLM_SECRET_VALUE_1234",
            "access_token": "ACCESS_SECRET_VALUE_5678",
            "mechanism": "oauth",
            "obtained_via": "device_code",
            "user_email": "nimo@example.com",
            "user_full_name": "Nimrod Rotem",
            "api_base_url": "https://api.meta.ai/v1",
        }},
    }))
    auth_file.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(config_home),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = "import json, app; print(json.dumps(app._muse_credential_display()))"

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "LLM_SECRET_VALUE_1234" not in result.stdout
    assert "ACCESS_SECRET_VALUE_5678" not in result.stdout
    data = json.loads(result.stdout)
    assert data == {
        "provider": "meta",
        "providerLabel": "Meta Model API",
        "accountEmail": "nimo@example.com",
        "accountName": "Nimrod Rotem",
        "mechanism": "oauth",
        "obtainedVia": "device_code",
        "credentialLabel": "Meta API key ····1234",
        "keySuffix": "1234",
        "apiHost": "api.meta.ai",
    }


def test_muse_session_logs_are_bound_to_the_exact_tmux_pane(tmp_path):
    data_home = tmp_path / "muse-data"
    root = data_home / "muse" / "sessions" / "2026" / "08" / "08" / "root-session"
    child = root / "subagent" / "child-session"
    other = data_home / "muse" / "sessions" / "2026" / "08" / "08" / "other-session"
    child.mkdir(parents=True)
    other.mkdir(parents=True)
    (root / "session.jsonl").write_text(json.dumps({
        "recorded_at": 1_786_208_100_000_000,
        "payload_type": "runtime.session.route_facts",
        "payload": {"record": {"tmux_pane": "$42:@42.%42", "cwd": "/tmp/right"}},
    }) + "\n")
    (child / "session.jsonl").write_text("{}\n")
    (other / "session.jsonl").write_text(json.dumps({
        "recorded_at": 1_786_208_200_000_000,
        "payload_type": "runtime.session.route_facts",
        "payload": {"record": {"tmux_pane": "$43:@43.%43", "cwd": "/tmp/wrong"}},
    }) + "\n")
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_DATA_HOME": str(data_home),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
import json
from types import SimpleNamespace
import app
app.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(returncode=0, stdout="%42\\n", stderr="")
print(json.dumps(app._find_muse_session_jsonl_files("muse-test")))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == [
        str(root / "session.jsonl"),
        str(child / "session.jsonl"),
    ]


def test_muse_runtime_settings_connect_advisor_without_persisting_its_token(tmp_path):
    state_dir = tmp_path / "dashboard-state"
    home = Path.home()
    config_home = tmp_path / "muse-config"
    settings_file = config_home / "muse" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({
        "schema_version": 1,
        "tui": {"voice_enabled": True},
    }))
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(state_dir),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(config_home),
            "TMUX_DASH_MUSE_MCP_PYTHON": "/opt/mcp/bin/python",
            "TMUX_DASH_MUSE_MCP_BRIDGE": "/opt/muse/muse_mcp_bridge.py",
            "TMUX_DASH_MUSE_GOOGLE_MCP_SCRIPT": "/opt/muse/google_workspace_mcp.py",
            "TMUX_DASH_MUSE_BROWSER_MCP_PROXY": "/opt/muse/browser_mcp_lease_proxy.py",
            "TMUX_DASH_MUSE_ADVISOR_TOKEN_FILE": "/opt/private/advisor-token",
            "ADVISOR_TOKEN": "NEVER_WRITE_THIS_TOKEN",
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
import json
import app
print(app._ensure_muse_runtime_settings())
print((app.MUSE_CONFIG_HOME / "muse" / "settings.json").read_text())
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "True"
    assert "NEVER_WRITE_THIS_TOKEN" not in result.stdout
    settings = json.loads("\n".join(result.stdout.splitlines()[1:]))
    assert settings["tui"] == {"voice_enabled": True}
    assert settings["mcp_servers"]["advisor"] == {
        "transport": "stdio",
        "command": "/opt/mcp/bin/python",
        "args": [
            "/opt/muse/muse_mcp_bridge.py",
            "--url",
            "https://advisor.rotem.ai/mcp",
            "--bearer-env",
            "ADVISOR_TOKEN",
            "--bearer-file",
            "/opt/private/advisor-token",
        ],
        "env": {"PYTHONUNBUFFERED": "1"},
        "framing": "line_delimited_json",
    }
    assert settings["mcp_servers"]["google"] == {
        "transport": "stdio",
        "command": "/opt/mcp/bin/python",
        "args": ["/opt/muse/google_workspace_mcp.py"],
        "env": {
            "GOOGLE_MCP_CREDENTIALS_DIR": str(state_dir / "connections" / "admin"),
            "GOOGLE_OAUTH_CLIENT_FILE": str(state_dir / "google_oauth_client.json"),
            "PYTHONUNBUFFERED": "1",
        },
        "framing": "line_delimited_json",
    }
    assert settings["mcp_servers"]["playwright-browser"] == {
        "transport": "stdio",
        "command": "/opt/mcp/bin/python",
        "args": ["/opt/muse/browser_mcp_lease_proxy.py"],
        "env": {
            "PYTHONUNBUFFERED": "1",
            "TMUX_DASH_BROWSER_CDP_PORT": "9222",
            "TMUX_DASH_BROWSER_ID": "default",
            "TMUX_DASH_BROWSER_OUTPUT_DIR": str(home / ".playwright-mcp" / "default"),
            "TMUX_DASH_CONTROLLER_SOCKET": str(state_dir / "controller.sock"),
            "TMUX_DASH_HOST_HOME": str(home),
        },
        "framing": "line_delimited_json",
    }
    assert settings_file.stat().st_mode & 0o777 == 0o600


def test_muse_project_context_preserves_project_rules_and_adds_runtime_contract(tmp_path):
    state_dir = tmp_path / "dashboard-state"
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    agents_file = project_dir / "AGENTS.md"
    agents_file.write_text("# Project rules\n\nKeep this instruction.\n")
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(state_dir),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(tmp_path / "muse-config"),
            "TMUX_DASH_MUSE_DATA_HOME": str(tmp_path / "muse-data"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = f'''
import app
app.GLOBAL_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
app.GLOBAL_CONTEXT_FILE.write_text("# MUSE account policy\\n\\nUse the connected advisor MCP.\\n")
print(app._prepare_muse_project_context({str(project_dir)!r}))
print(({str(agents_file)!r} and app.Path({str(agents_file)!r}).read_text()))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[0] == "True"
    content = "\n".join(result.stdout.splitlines()[1:])
    assert "Keep this instruction." in content
    assert "BEGIN MUSE DASHBOARD RUNTIME" in content
    assert "Use the connected advisor MCP." in content
    assert "read_memory" in content
    assert "MUSE_CONFIG_HOME" in content


def test_muse_global_context_migrates_codex_only_memory_paths(tmp_path):
    state_dir = tmp_path / "dashboard-state"
    state_dir.mkdir()
    global_context = state_dir / "global-context.md"
    global_context.write_text("""# MUSE account policy

- Treat this dashboard account's `CODEX_HOME`, project directory, browser,
  connections, memories, skills, uploads, and session history as private to this
  account. Never inspect or operate another account's corresponding resources.
- Local Codex memory is private because every account has a separate
  `CODEX_HOME`. Use it as recall, never as the sole source of required policy or
  current external facts.
""")
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(state_dir),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
import app
print(app._read_global_context())
"""
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "CODEX_HOME" not in result.stdout
    assert "MUSE_CONFIG_HOME" in result.stdout
    assert "MUSE_DATA_HOME" in result.stdout
    assert "read_memory" in result.stdout


def test_create_session_uses_out_of_band_pane_launch():
    source = Path(__file__).resolve().parent.joinpath("app.py").read_text()
    function = source.split("async def api_create_session", 1)[1].split(
        "@app.delete(\"/api/sessions/{session_name}\")", 1
    )[0]

    assert "_launch_agent_pane(" in function
    assert '["tmux", "send-keys"' not in function


def test_muse_auth_usage_endpoint_returns_tokens_cost_and_live_rate(tmp_path):
    data_home = tmp_path / "muse-data"
    catalog_dir = data_home / "muse" / "model-catalog"
    session_dir = data_home / "muse" / "sessions" / "2026" / "08" / "08" / "session"
    catalog_dir.mkdir(parents=True)
    session_dir.mkdir(parents=True)
    (catalog_dir / "meta.json").write_text(json.dumps({
        "schema_version": 1,
        "provider_id": "meta",
        "rows": [{
            "model_id": "muse-spark-1.2-contributor",
            "provider_id": "meta",
            "cost": {"input": "0.10", "output": "0.20", "cached": "0.002", "currency": "USD"},
        }],
    }))
    now_us = int(time.time() * 1_000_000)
    (session_dir / "session.jsonl").write_text(json.dumps({
        "recorded_at": now_us,
        "payload_type": "runtime.session",
        "payload": {"kind": "run", "event": {
            "kind": "model_completed",
            "duration_ms": 2000,
            "model": "muse-spark-1.2-contributor",
            "usage": {
                "input_tokens": 1000,
                "output_tokens": 200,
                "cached_tokens": 600,
                "reasoning_tokens": 50,
            },
        }},
    }) + "\n")
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_DATA_HOME": str(data_home),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
import asyncio
import json
import app
response = asyncio.run(app.api_codex_usage())
print(response.body.decode())
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["messages"] == 1
    assert data["inputTokens"] == 1000
    assert data["outputTokens"] == 200
    assert data["cacheReadTokens"] == 600
    assert data["reasoningTokens"] == 50
    assert data["totalTokens"] == 1200
    assert data["estimatedCost"] == 0.0000812
    assert data["recentOutputRate"] == 6000
    assert data["requestsPerMinute"] == 1
    assert data["providerStatus"] == "Connected · no rate limit detected"


def test_muse_auth_status_includes_safe_account_and_runtime_capabilities(tmp_path):
    config_home = tmp_path / "muse-config"
    auth_file = config_home / "muse" / "auth.json"
    auth_file.parent.mkdir(parents=True)
    auth_file.write_text(json.dumps({
        "providers": {"meta": {
            "api_key": "LLM_SECRET_VALUE_9876",
            "user_email": "owner@example.com",
        }},
    }))
    auth_file.chmod(0o600)
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(config_home),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
import asyncio
import json
import app
app._agent_cli_readiness = lambda: (True, "ready", {
    "version": "0.1.0",
    "auth_configured": True,
    "skills_total": 20,
    "skills_user": 10,
    "mcp_servers": ["advisor"],
    "memory_tools": ["read_memory", "add_memory", "edit_memory"],
})
response = asyncio.run(app.api_codex_auth_status())
print(response.body.decode())
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert "LLM_SECRET_VALUE_9876" not in result.stdout
    data = json.loads(result.stdout)
    assert data["email"] == "owner@example.com"
    assert data["credential"]["credentialLabel"] == "Meta API key ····9876"
    assert data["details"]["skills_total"] == 20
    assert data["details"]["mcp_servers"] == ["advisor"]
    assert data["details"]["memory_tools"] == ["read_memory", "add_memory", "edit_memory"]


def test_muse_dashboard_hides_unowned_muse_tmux_sessions(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = """
from types import SimpleNamespace
import app
app.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
    returncode=0, stdout="100\\tmuse-bin-0.1.0-R708.1\\n"
)
app._process_tree_snapshot = lambda: ({}, {"100": "muse-bin-0.1.0-R708.1"})
owners = {}
app._load_session_owners = lambda: owners
print(app._session_is_codex("foreign-muse"))
owners["owned-muse"] = "admin"
print(app._session_is_codex("owned-muse"))
"""

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["False", "True"]


def test_muse_dashboard_exposes_usage_autopush_and_skills_ui(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import json
import app
page = app.HTML_PAGE
print(json.dumps({
    "stats_visible": "${AGENT_KIND==='muse'?'':`<div class=\"tier\" style=\"margin-top:12px\" id=\"stats-tier-" not in page,
    "autopush_visible": "${AGENT_KIND==='muse'?'':`<div style=\"padding:4px 16px 2px" not in page,
    "skills_visible": "${(MEMBER_SIMPLE||AGENT_KIND==='muse')?'':`<div class=\"tab-more-item ${tab==='skills'?'active':''}\"" not in page,
    "usage_fetched": "if(AGENT_KIND!=='muse'){\n    try{\n      const usageResp" not in page,
    "stats_polled": "if(AGENT_KIND!=='muse'){\n      startStatsPolling" not in page,
    "native_label": "bundled with ${AGENT_NAME}; always available" in page,
}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "stats_visible": True,
        "autopush_visible": True,
        "skills_visible": True,
        "usage_fetched": True,
        "stats_polled": True,
        "native_label": True,
    }


def test_muse_google_connections_use_native_mcp_and_write_capable_scopes(tmp_path):
    config_home = tmp_path / "muse-config"
    settings_file = config_home / "muse" / "settings.json"
    settings_file.parent.mkdir(parents=True)
    settings_file.write_text(json.dumps({
        "schema_version": 1,
        "mcp_servers": {"google": {"transport": "stdio"}},
    }))
    mcp_python = tmp_path / "mcp-python"
    mcp_python.write_text("#!/bin/sh\nexit 0\n")
    mcp_python.chmod(0o700)
    google_script = tmp_path / "google_workspace_mcp.py"
    google_script.write_text("# test Google MCP\n")
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(config_home),
            "TMUX_DASH_MUSE_MCP_PYTHON": str(mcp_python),
            "TMUX_DASH_MUSE_GOOGLE_MCP_SCRIPT": str(google_script),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import asyncio
import json
import app

calls = []
app._ensure_muse_runtime_settings = lambda: calls.append("muse") or True
app._ensure_google_mcp = lambda *_args: calls.append("codex")
app._current_user = lambda _request: {"id": "admin", "role": "admin"}
response = asyncio.run(app.api_connections(object()))
app._write_google_mcp({"id": "admin", "role": "admin"}, "drive")
print(json.dumps({
    "scopes": {service: app._google_scopes_for_service(service)
               for service in ("drive", "gmail", "calendar")},
    "connections": json.loads(response.body),
    "calls": calls,
}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["scopes"] == {
        "drive": ["https://www.googleapis.com/auth/drive"],
        "gmail": ["https://www.googleapis.com/auth/gmail.modify"],
        "calendar": ["https://www.googleapis.com/auth/calendar"],
    }
    assert data["connections"]["mcp_ready"] is True
    assert data["calls"] == ["muse"]


def test_muse_admin_can_open_google_connections_from_runtime_ui(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import json
import app
page = app.HTML_PAGE
print(json.dumps({
    "admin_nav_visible": "body[data-agent=\"muse\"] .muse-connection" in page,
    "runtime_button": "Manage Google connections" in page,
    "agent_copy": "Give ${esc(AGENT_NAME)} access" in page,
}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "admin_nav_visible": True,
        "runtime_button": True,
        "agent_copy": True,
    }


def test_muse_skills_use_native_account_root_and_inventory(tmp_path):
    home = tmp_path / "home"
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import json
from types import SimpleNamespace
import app
app.subprocess.run = lambda *_args, **_kwargs: SimpleNamespace(
    returncode=0,
    stdout=json.dumps({"skills": [
        {"name": "read-files", "description": "Bundled reader", "scope": "built-in"},
        {"name": "team-style", "description": "Team conventions", "scope": "user"},
    ]}),
    stderr="",
)
print(json.dumps({
    "root": str(app._account_skills_dir({"username": "admin", "role": "admin"})),
    "trash": str(app._account_skill_trash_dir({"username": "admin", "role": "admin"})),
    "inventory": app._muse_skills_inventory(),
}))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["root"] == str(home / ".agents" / "skills")
    assert data["trash"] == str(
        tmp_path / "dashboard-state" / "muse-config" / "muse" / ".skill-trash"
    )
    assert data["inventory"] == [
        {
            "name": "read-files",
            "description": "Bundled reader",
            "scope": "built-in",
        },
        {
            "name": "team-style",
            "description": "Team conventions",
            "scope": "user",
        },
    ]


def test_fresh_muse_session_is_never_auto_nudged(tmp_path):
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_SECRET": "muse-test-secret",
            "TMUX_DASH_PASS": "muse-test-pass",
        }
    )
    probe = r'''
import app
print(app._looks_like_fresh_claude_session("""Muse Code

── Voice input (Alt + v to start) ─────────────────────────────── ⟩
  muse-spark-1.2-contributor · high · /tmp/project · YOLO
"""))
'''

    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=os.path.dirname(__file__),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "True"
