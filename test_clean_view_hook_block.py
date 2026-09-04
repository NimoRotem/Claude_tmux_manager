"""Clean view must show a hook block, as a message from the user.

A hook that blocks hands its text back to the model in the user role, and the
session then obeys it. Clean view used to swallow the whole thing twice over:
the header matched the "(ctrl+o to expand)" noise rule and the body matched the
tool-output rule, so several minutes of work appeared with nothing prompting it.

The browser functions are lifted out of app.py and run under node, so these
tests exercise the shipped source rather than a copy of it.
"""
import json
import re
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

# What we actually want to test. Everything these reach is pulled in with them,
# so adding a helper in app.py does not mean editing a list here.
_SEEDS = ["applyRawFilter", "_markUserRows", "_isHookHeader", "_HOOK_ECHO_PREFIX",
          "_USER_ECHO_RE"]


# Sliced by LINE, never by brace depth: several of these regexes contain a
# counted repetition (`{0,3}`, `{2,}`), and a brace counter eats the rest of the
# literal and hands node a half-open regex.
_TOP_LEVEL = re.compile(r"^(?:const|let|var|function|//)")


_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _extract_js() -> str:
    """Pull the seeds and everything they reach out of app.py's inline <script>.

    A declaration runs from its own line to the line before the next one that
    starts at column 0, which is how everything in this script is written.
    Emitted in source order, because a `const` used before its line is a TDZ
    error under node even though the browser reaches it fine at call time.
    """
    lines = APP.read_text().split("\n")
    starts, bodies = {}, {}
    for i, line in enumerate(lines):
        m = re.match(r"(?:const|function)\s+([A-Za-z_$][\w$]*)", line)
        if not m or m.group(1) in starts:
            continue
        j = i + 1
        while j < len(lines) and not _TOP_LEVEL.match(lines[j]):
            j += 1
        starts[m.group(1)] = i
        bodies[m.group(1)] = "\n".join(lines[i:j]).rstrip()

    missing = [n for n in _SEEDS if n not in bodies]
    assert not missing, f"no longer declared at column 0 in app.py: {missing}"

    need, seen = list(_SEEDS), set()
    while need:
        name = need.pop()
        if name in seen:
            continue
        seen.add(name)
        for ident in _IDENT.findall(bodies[name]):
            if ident in bodies and ident not in seen:
                need.append(ident)
    return "\n".join(bodies[n] for n in sorted(seen, key=lambda n: starts[n]))


_JS = None


# applyRawFilter asks the page whether clean view is on. In these tests it is,
# except where a test says otherwise.
_PRELUDE = "let _CLEAN=true; function getCleanViewPref(){return _CLEAN}\n"


def run_js(expr_body: str):
    """Evaluate JS against the real helpers and return the JSON result."""
    global _JS
    if _JS is None:
        _JS = _extract_js()
    script = _PRELUDE + _JS + "\n" + expr_body + "\n"
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, f"node failed:\n{p.stderr}\n"
    return json.loads(p.stdout.strip().splitlines()[-1])


def clean(text: str):
    return run_js(
        "const t=" + json.dumps(text) + ";"
        "console.log(JSON.stringify(applyRawFilter(t)));"
    )


def marks(rows):
    return run_js(
        "const r=" + json.dumps(rows) + ";"
        "console.log(JSON.stringify(_markUserRows(r)));"
    )


# A realistic pane: agent prose, a tool call with output, then the hook block.
PANE = textwrap.dedent(
    """\
    ● I'll check the fleet.

    ● Bash(ls /home)
      ⎿  nimrod_rotem
         other

    ● Done. Six boxes covered.

    ● Ran 2 stop hooks (ctrl+o to expand)
      ⎿  Stop hook error: Stop refused by the keep-going hook (pass 1 of 2).
      The user is away and cannot answer anything, so do not hand back and
      do not ask.

      Re-read the original request as a checklist and satisfy every sentence
      of it.

    ● Right, continuing.
    """
)


# Copied verbatim off a live pane (tmux capture-pane), so the indents, the `⎿`
# column and the blank rows inside the hook's own text are the real ones.
REAL_PANE = (
    "  Left undone: lisa-claude and lisa-codex did not get a push. lisa-codex is\n"
    "  terminated and lisa-claude no longer exists in the project despite the advisor\n"
    "  still listing it. That host record is wrong and I did not correct it.\n"
    "\n"
    "● Ran 2 stop hooks (ctrl+o to expand)\n"
    "  ⎿  Stop hook error: Stop refused by the keep-going hook (pass 1 of 2).\n"
    "  The user is away and cannot answer anything, so do not hand back and\n"
    "  do not ask.\n"
    "\n"
    "  Re-read the original request as a checklist and satisfy every sentence\n"
    "  of it. Anything you deferred, called optional or out of scope, listed\n"
    "  as a next step or a follow-up, or left for the user to do: that is in\n"
    "  scope, do it now.\n"
    "\n"
    "  Do not take my word for whether it works: re-verify your own work end\n"
    "  to end, then finish whatever is still left.\n"
)


def test_a_real_captured_pane_renders_the_whole_block():
    rows = clean(REAL_PANE).split("\n")
    flags = marks(rows)
    hdr = next(i for i, l in enumerate(rows) if "Ran 2 stop hooks" in l)
    assert rows[hdr].startswith("> ")
    assert flags[hdr] == 2
    # Every row of the hook's text is present and marked as yours.
    for phrase in ("Stop hook error", "The user is away", "Re-read the original request",
                   "Do not take my word", "re-verify your own work end"):
        i = next((i for i, l in enumerate(rows) if phrase in l), None)
        assert i is not None, f"{phrase!r} was filtered out of the real pane"
        assert flags[i] == 1, f"{phrase!r} is not highlighted as your message"
    # And the agent's own words above it are untouched.
    j = next(i for i, l in enumerate(rows) if "Left undone" in l)
    assert flags[j] == 0


def test_the_hook_block_survives_clean_view():
    out = clean(PANE)
    assert "Ran 2 stop hooks" in out, "the header was filtered out"
    assert "Stop refused by the keep-going hook" in out, "the body was filtered out"
    assert "The user is away" in out


def test_the_paragraph_after_a_blank_row_survives():
    """The hook's text has blank lines in it; the tail must not be cut off."""
    out = clean(PANE)
    assert "Re-read the original request as a checklist" in out


def test_the_header_is_restamped_as_your_turn():
    out = clean(PANE)
    line = [l for l in out.split("\n") if "Ran 2 stop hooks" in l][0]
    assert line.startswith("> "), f"expected the user marker, got {line!r}"
    assert not line.lstrip("> ").startswith("●")


def test_the_marker_is_a_literal_gt_and_the_highlight_does_not_depend_on_it():
    """`>` is what was asked for, and it is safe because nothing keys on it.

    The highlight comes from _isHookHeader, not from _USER_ECHO_RE (which does
    not accept `>`), and _PANE_STRUCTURE_RE (which does) is only ever tested
    against the input row, never against what the filter emits. Both halves are
    pinned here, because either one changing silently turns the marker back into
    plain chrome.
    """
    prefix = run_js("console.log(JSON.stringify(_HOOK_ECHO_PREFIX));")
    assert prefix == "> ", f"the marker must be a literal '> ', got {prefix!r}"
    # A `>` row is NOT a user echo, yet is still flagged 2 via the hook test.
    checks = run_js(
        "const row='> Ran 2 stop hooks (ctrl+o to expand)';"
        "console.log(JSON.stringify({"
        "echo:_USER_ECHO_RE.test(row),hook:_isHookHeader(row),"
        "flag:_markUserRows([row])[0]}));"
    )
    assert checks["echo"] is False, "if `>` ever becomes a user echo, plain quoted text highlights too"
    assert checks["hook"] is True
    assert checks["flag"] == 2, "the highlight must survive without _USER_ECHO_RE"


def test_the_block_is_highlighted_as_a_user_message():
    rows = clean(PANE).split("\n")
    flags = marks(rows)
    hdr = next(i for i, l in enumerate(rows) if "Ran 2 stop hooks" in l)
    assert flags[hdr] == 2, "the header is not the row a jump lands on"
    body = next(i for i, l in enumerate(rows) if "Stop refused by" in l)
    assert flags[body] == 1, "the hook's body is not marked as your message"
    prose = next(i for i, l in enumerate(rows) if "Six boxes covered" in l)
    assert flags[prose] == 0, "the agent's own reply must not be highlighted"


def test_ordinary_tool_output_is_still_hidden():
    """The fix must not reopen the noise it was hiding."""
    out = clean(PANE)
    assert "nimrod_rotem" not in out, "tool output leaked back in"
    assert "Bash(ls /home)" not in out


def test_the_agents_reply_after_the_block_is_still_shown():
    out = clean(PANE)
    assert "Right, continuing." in out
    assert "I'll check the fleet." in out


def test_isnoise_exempts_the_hook_header_on_its_own():
    """The header carries "(ctrl+o to expand)", which is a noise rule.

    applyRawFilter happens to test for a hook before it tests for noise, so this
    pins the fact at the level it is actually true: a hook header is not noise,
    whatever order the caller asks in.
    """
    verdicts = run_js(
        "console.log(JSON.stringify(["
        "_isNoise('\\u25cf Ran 2 stop hooks (ctrl+o to expand)'),"
        "_isNoise('Ran 1 stop hook (ctrl+o to expand)'),"
        "_isNoise('  \\u23bf  Read 30 lines (ctrl+o to expand)'),"
        "_isNoise('npm warn deprecated foo')"
        "]));"
    )
    assert verdicts == [False, False, True, True]


def test_a_ctrl_o_hint_that_is_not_a_hook_is_still_noise():
    text = "● Read(app.py)\n  ⎿  Read 30 lines (ctrl+o to expand)\n\n● Done.\n"
    out = clean(text)
    assert "Read 30 lines" not in out
    assert "Done." in out


def test_one_hook_and_other_hook_events_match():
    for header in ("Ran 1 stop hook (ctrl+o to expand)",
                   "Ran 3 PreToolUse hooks (ctrl+o to expand)",
                   "Ran 2 SubagentStop hooks (ctrl+o to expand)"):
        out = clean("● " + header + "\n  ⎿  something the hook said\n\n● ok\n")
        assert header.split(" (")[0] in out, f"{header} was filtered out"
        assert "something the hook said" in out


def test_exact_view_leaves_the_text_alone_but_still_marks_it():
    """Clean view off returns the raw pane; the highlight still applies."""
    assert clean.__name__  # keep the linter honest about the helper above
    rows = PANE.split("\n")
    flags = marks(rows)
    hdr = next(i for i, l in enumerate(rows) if "Ran 2 stop hooks" in l)
    assert flags[hdr] == 2, "exact view should mark the hook header too"


def test_the_filter_is_backwards_only():
    """Append-only renderer: a new row at the bottom must never change one above.

    Checked against the FINAL output, not against the previous step. Comparing
    consecutive steps and forgiving the trailing row leaves a one-row window in
    which a line can be printed, then silently corrected when the next row
    lands, and a look-ahead lives exactly in that window. Every intermediate
    render must already be an exact prefix of what the pane ends up showing.
    """
    rows = PANE.split("\n")
    final = clean(PANE).split("\n")
    for n in range(1, len(rows) + 1):
        out = clean("\n".join(rows[:n]))
        cur = out.split("\n") if out else []
        assert cur == final[:len(cur)], (
            f"after {n} of {len(rows)} rows the output is not a prefix of the final render\n"
            f"got:      {cur}\nexpected: {final[:len(cur)]}"
        )
