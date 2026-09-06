"""Focused provisioning coverage for browser IPC and the immutable pytest hook."""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import provision_accounts


@pytest.fixture
def hook_paths(tmp_path, monkeypatch):
    source = tmp_path / "runtime_hooks" / "tmux_dashboard_pytest_gate.py"
    source.parent.mkdir()
    source.write_bytes(b"VALUE = 'source'\n")
    target_dir = tmp_path / "installed"
    target = target_dir / "tmux_dashboard_pytest_gate.py"
    monkeypatch.setattr(provision_accounts, "PYTEST_HOOK_SOURCE", source)
    monkeypatch.setattr(provision_accounts, "PYTEST_HOOK_DIR", target_dir)
    monkeypatch.setattr(provision_accounts, "PYTEST_HOOK_TARGET", target)
    monkeypatch.setattr(provision_accounts, "PYTEST_HOOK_OWNER_UID", os.geteuid())
    monkeypatch.setattr(provision_accounts, "PYTEST_HOOK_OWNER_GID", os.getegid())
    return source, target_dir, target


def test_pytest_hook_install_is_atomic_and_verified(hook_paths, monkeypatch):
    source, target_dir, target = hook_paths
    target_dir.mkdir()
    target.write_bytes(b"old content\n")
    target.chmod(0o444)
    observed = {}
    real_replace = os.replace

    def replace(temporary, destination):
        temporary = Path(temporary)
        destination = Path(destination)
        observed["same_directory"] = temporary.parent == target_dir
        observed["temporary_mode"] = stat.S_IMODE(temporary.stat().st_mode)
        observed["old_visible"] = target.read_bytes() == b"old content\n"
        real_replace(temporary, destination)

    monkeypatch.setattr(provision_accounts.os, "replace", replace)
    result = provision_accounts.install_pytest_hook()

    assert observed == {
        "same_directory": True,
        "temporary_mode": 0o444,
        "old_visible": True,
    }
    assert target.read_bytes() == source.read_bytes()
    assert stat.S_IMODE(target.stat().st_mode) == 0o444
    assert stat.S_IMODE(target_dir.stat().st_mode) == 0o755
    assert provision_accounts.pytest_hook_status()[0]
    assert any("verified sha256=" in line for line in result)


@pytest.mark.parametrize("damage", ["symlink", "writable", "content", "owner"])
def test_pytest_hook_verifier_rejects_unsafe_install(hook_paths, damage, monkeypatch):
    source, _target_dir, target = hook_paths
    provision_accounts.install_pytest_hook()

    if damage == "symlink":
        target.unlink()
        target.symlink_to(source)
    elif damage == "writable":
        target.chmod(0o644)
    elif damage == "content":
        target.chmod(0o644)
        target.write_bytes(b"different\n")
        target.chmod(0o444)
    else:
        monkeypatch.setattr(
            provision_accounts, "PYTEST_HOOK_OWNER_UID", os.geteuid() + 1
        )

    valid, _detail = provision_accounts.pytest_hook_status()
    assert not valid


def test_pytest_hook_dry_run_does_not_create_target(hook_paths):
    _source, target_dir, target = hook_paths
    result = provision_accounts.install_pytest_hook(dry=True)
    assert not target_dir.exists()
    assert not target.exists()
    assert any("would install pytest hook" in line for line in result)


@pytest.mark.parametrize("valid", [True, False])
def test_verify_reports_hook_status_even_without_account_mapping(monkeypatch, valid):
    monkeypatch.setattr(
        provision_accounts,
        "pytest_hook_status",
        lambda: (valid, "checked hook"),
    )
    monkeypatch.setattr(provision_accounts, "load_map", lambda: {})
    result = provision_accounts.verify()
    assert result[0] == f"  [{'PASS' if valid else 'FAIL'}] pytest hook: checked hook"
    assert result[-1] == "nothing provisioned"


def test_browser_ipc_group_provisions_owner_and_account_members(
    tmp_path, monkeypatch
):
    dashboard = tmp_path / ".tmux-dashboard"
    dashboard.mkdir()
    socket_path = dashboard / "controller.sock"
    socket_path.write_bytes(b"")
    calls = []
    ownership = []
    monkeypatch.setattr(provision_accounts, "DASH", dashboard)
    monkeypatch.setattr(
        provision_accounts.pwd,
        "getpwnam",
        lambda name: SimpleNamespace(pw_name=name),
    )
    monkeypatch.setattr(
        provision_accounts,
        "sh",
        lambda *args, **kwargs: calls.append(args) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        provision_accounts.shutil,
        "chown",
        lambda path, **kwargs: ownership.append((Path(path), kwargs)),
    )

    provision_accounts.configure_browser_ipc_group(["gx-member"])

    assert ("usermod", "-aG", "gxauth", "nimrod_rotem") in calls
    assert ("usermod", "-aG", "gxauth", "gx-member") in calls
    assert stat.S_IMODE(dashboard.stat().st_mode) == 0o710
    assert stat.S_IMODE(socket_path.stat().st_mode) == 0o660
    assert [path for path, _kwargs in ownership] == [dashboard, socket_path]
