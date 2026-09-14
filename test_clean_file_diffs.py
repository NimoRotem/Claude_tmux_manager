"""Clean view keeps conversation text while hiding Codex file-change previews."""
import pytest

from test_terminal_render import _run


WRAPPED_HEADER = (
    "• Added .work/terminal-refinements/review/ops/builder5-terminal-refinements/\n"
    "validation.md (+22 -0)"
)
DIFF = """     1 +# Validation, 2026-09-07
     2 +
     3 +950 tests passed with two existing dependency deprecation warnings. Regr
       ession cases cover repeated names, ownership, lifecycle races, pending a
       utomatic labels, user-message preservation, background heredoc suppressi
       on, visible controls in both account modes and views.
     4 +
     5 +Authenticated browser QA passed at 1440 x 1000 and 390 x 844:
     6 +
     7 +- A real right-click opened Rename session. Three successive names saved
       , including literal angle brackets; reload preserved the latest label an
       d the stable session route."""


def test_clean_view_hides_the_reported_wrapped_validation_patch():
    pane = WRAPPED_HEADER + "\n" + DIFF + "\n\n• The change is ready.\n› Thank you."
    assert _run(pane)["clean"] == ["• The change is ready.", "› Thank you."]


@pytest.mark.parametrize("header", [
    WRAPPED_HEADER,
    "• Edited a/very/long/path/to/the/project/file.py (+12\n-3)",
    "• Edit README",
    "• Added 1 file",
])
@pytest.mark.parametrize("row", ["     1 +# Title", "     1 +const x=1;", "     1 -old_call()", "     1 +", "     1 -"])
def test_file_diff_sign_does_not_require_a_space_before_content(header, row):
    assert _run(header + "\n" + row + "\n• Done.")["clean"] == ["• Done."]


def test_clean_view_keeps_a_user_pasted_validation_patch():
    rows = ["› Please explain this patch:", *WRAPPED_HEADER.removeprefix("• ").splitlines(), *DIFF.splitlines(), "• Here is the explanation."]
    assert _run("\n".join(rows))["clean"] == rows


def test_clean_view_keeps_an_intentional_fenced_patch_example():
    rows = ["• This is a patch example:", "```text", *WRAPPED_HEADER.splitlines(), *DIFF.splitlines(), "```", "• That is how the format works."]
    assert _run("\n".join(rows))["clean"] == rows


def test_raw_view_preserves_the_entire_file_diff():
    pane = WRAPPED_HEADER + "\n" + DIFF
    assert _run(pane, clean_view=False)["clean"] == pane.splitlines()


@pytest.mark.parametrize("prefix", ["", "Earlier messages are available — press ctrl+t to view the full transcript\n"])
def test_clean_view_hides_diff_when_its_header_is_outside_the_capture(prefix):
    diff = "\n".join([
        "86 +    return {'figure':index+1,'view':j['name']}",
        "     87 +            'bounds_pt':[tx+b[0]*scale,ty+b[1]*scale]",
        "     88 +",
        "     89 +def make(indices,filename):",
        "       wrapped source from the same edit",
        "    110 +    else:",
    ])
    pane = prefix + diff + "\n\n• The drawings are ready.\n› Thanks."
    assert _run(pane)["clean"] == ["• The drawings are ready.", "› Thanks."]
    assert _run(pane, clean_view=False)["clean"] == pane.splitlines()


def test_ansi_colored_edit_header_and_rows_are_hidden():
    pane = "\x1b[32m• Edited drawing.py (+3 -1)\x1b[0m\n" + DIFF + "\n• Ready."
    assert _run(pane)["clean"] == ["• Ready."]


@pytest.mark.parametrize("prefix", ["› Explain this:", "• Here is an example:"])
def test_numbered_changes_inside_conversation_stay_visible(prefix):
    pane = prefix + "\n     86 +one\n     87 -two\n• Done."
    assert _run(pane)["clean"] == pane.splitlines()

def test_background_terminal_commands_are_hidden():
    pane = "• Waited for background terminal · cat > script.py <<'PY'\nprint('tool body')\nPY\n• Done."
    assert _run(pane)["clean"] == ["• Done."]
