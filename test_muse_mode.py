"""Regression coverage for the isolated Muse dashboard runtime mode."""

import json
import os
import subprocess
import sys

from runtime_control import scoped_codex_command


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
    muse_binary = tmp_path / "muse"
    muse_binary.write_text("#!/bin/sh\necho 'Muse Code 0.1.0 (test)'\n")
    muse_binary.chmod(0o700)
    env = os.environ.copy()
    env.update(
        {
            "TMUX_DASH_AGENT": "muse",
            "TMUX_DASH_STATE_DIR": str(tmp_path / "dashboard-state"),
            "TMUX_DASH_MUSE_BINARY": str(muse_binary),
            "TMUX_DASH_MUSE_CONFIG_HOME": str(config_home),
            "TMUX_DASH_MUSE_DATA_HOME": str(tmp_path / "muse-data"),
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


def test_muse_ui_omits_codex_only_controls_and_auto_typing(tmp_path):
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
        "autopush": "off",
        "codex_controls": False,
        "admin_tabs": ["runtime", "history", "browser", "apis"],
        "member_tabs": ["runtime", "history", "browser"],
    }


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
