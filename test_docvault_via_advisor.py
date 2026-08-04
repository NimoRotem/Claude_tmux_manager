"""The document vault is reached through the advisor, never with a local key.

Every member's config.toml used to carry the vault's bearer key and call
https://grabo.cc/docvault-mcp/mcp directly, so ~150,000 company documents
(payroll runs, bank paperwork, passport scans) were reachable with no check on
who was asking. The key now lives only on the advisor, which applies the
caller's permission group to the query and to what the fetch returned.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import app as app_module

MEMBER = {
    "id": "u_member",
    "username": "member@example.com",
    "role": "user",
    "group": "engineers",
    "google_email": "member@grabo.com",
}


def _written_config(tmp_path: Path, *, with_advisor: bool = True) -> str:
    # The advisor block is written only for an account that already has its own
    # advisor token, which is what makes the call arrive as that person.
    if with_advisor:
        (tmp_path / "advisor-token").write_text("member-token\n")
    app_module._configure_member_codex_isolation(tmp_path, MEMBER)
    return (tmp_path / "config.toml").read_text()


def test_a_member_config_carries_no_vault_key(tmp_path):
    text = _written_config(tmp_path)

    assert "DOCVAULT_MCP_KEY" not in text
    assert "mcp_servers.docvault" not in text
    assert "docvault-mcp/mcp" not in text


def test_the_advisor_is_still_wired_so_the_vault_stays_reachable(tmp_path):
    """Removing the direct route only works if the gated route is there."""
    text = _written_config(tmp_path)

    assert "[mcp_servers.advisor]" in text
    assert 'bearer_token_env_var = "ADVISOR_TOKEN"' in text


def test_a_config_written_by_an_older_build_is_cleaned_on_the_next_sync(tmp_path):
    stale = tmp_path / "config.toml"
    stale.write_text(
        "[mcp_servers.advisor]\n"
        'url = "https://advisor.rotem.ai/mcp"\n\n'
        "# BEGIN GRABO DOCVAULT MCP (managed)\n"
        "[mcp_servers.docvault]\n"
        'command = "/usr/bin/python3"\n\n'
        "[mcp_servers.docvault.env]\n"
        'DOCVAULT_MCP_KEY = "1666e6a69ecc7061e0000822da13ce7d2640c40a2e1f2bdb"\n'
        "# END GRABO DOCVAULT MCP\n"
    )

    text = _written_config(tmp_path)

    assert "DOCVAULT_MCP_KEY" not in text
    assert "1666e6a" not in text
    tomllib.loads(text)


def test_strip_managed_block_leaves_the_rest_of_the_file_alone():
    text = (
        "[a]\nx = 1\n\n"
        "# BEGIN GRABO DOCVAULT MCP (managed)\n[mcp_servers.docvault]\n"
        "# END GRABO DOCVAULT MCP\n\n"
        "[b]\ny = 2\n"
    )

    out = app_module._strip_managed_block(text, "GRABO DOCVAULT MCP")

    assert "docvault" not in out
    parsed = tomllib.loads(out)
    assert parsed["a"]["x"] == 1 and parsed["b"]["y"] == 2
