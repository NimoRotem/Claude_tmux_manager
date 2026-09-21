"""The report-style block a role profile's CLAUDE.md carries.

A role dir is rewritten from the stored profile record on every materialize, and
that record is refreshed from `_PROFILE_PRESETS` unless the profile is marked
`edited`. So the block has to be applied at write time, and it has to survive the
editor reading the file back and saving it into the record again.
"""
import importlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
app = importlib.import_module("app")


def test_block_is_appended_to_a_persona():
    out = app._with_report_style("# QA Agent\n\n## Flake protocol\nDon't merge.\n")
    assert "# QA Agent" in out
    assert app._REPORT_STYLE_BEGIN in out and app._REPORT_STYLE_END in out
    assert "Under 200 words" in out


def test_empty_persona_gets_only_the_block():
    out = app._with_report_style("")
    assert out.startswith(app._REPORT_STYLE_BEGIN)
    assert out.count(app._REPORT_STYLE_BEGIN) == 1
    assert app._with_report_style(None) == out


def test_applying_twice_does_not_stack():
    once = app._with_report_style("# UI Expert\n")
    twice = app._with_report_style(once)
    assert once == twice
    assert twice.count(app._REPORT_STYLE_BEGIN) == 1


def test_round_trip_through_the_editor_keeps_one_copy():
    """Read the file, save it back into the record, materialize again."""
    on_disk = app._with_report_style("# Researcher\nCite sources.\n")
    record = app._strip_report_style(on_disk)          # what the PUT stores
    assert app._REPORT_STYLE_BEGIN not in record
    assert record == "# Researcher\nCite sources."
    assert app._with_report_style(record) == on_disk


def test_stripping_removes_several_stacked_copies():
    stacked = app._with_report_style("") + app._with_report_style("")
    assert app._strip_report_style(stacked) == ""


def test_the_block_obeys_its_own_rules():
    em_dash = chr(0x2014)  # by codepoint, so a repo-wide grep for one stays clean
    assert em_dash not in app._REPORT_STYLE, "the block must not contain an em dash"
    assert "|" not in app._REPORT_STYLE, "the block must not contain a pipe"


def test_the_default_profile_file_is_left_alone():
    """~/.claude/CLAUDE.md is the account's own file, not a role dir we own."""
    src = Path(app.__file__).with_name("app.py").read_text()
    i = src.index("_backup_before_dashboard_write(claudemd_path)")
    write = src[i:i + 400]
    assert "is_default" in write, "the default profile must skip the managed block"
