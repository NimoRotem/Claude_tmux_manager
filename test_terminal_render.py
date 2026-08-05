"""The terminal renderer's parsing rules, run as JavaScript.

Everything the terminal does to a pane before it paints it — cutting the live
status line out, dropping the pane's chrome, undoing the CLI's hard wraps,
diffing one paint against the last — is regex-heavy JS living inside app.py's
HTML_PAGE. It is fitted to what Codex actually draws, and a stray character in
one of those patterns is the difference between a clean transcript and a page
that eats the conversation.

So the rules are exercised here for real: the JS is lifted out of app.py,
evaluated in node against stub DOM globals, and driven with panes captured off
a live session. Skipped when node is not installed.
"""

import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")


# A pane the way tmux hands it over: the launcher's shell line, the start-up
# banner, a turn, and the chrome Codex parks at the foot of every frame — twice,
# because the frame that scrolled away left its own copy behind in the history.
PANE = """\
nimrod_rotem@grabo-tech:~$ if [ -f /home/nimrod_rotem/.codex-user-u_36f0/auth.json ]; then exec e
nv CODEX_HOME=/home/nimrod_rotem/.codex-user-u_36f0 ADVISOR_TOKEN="$(cat /home/nimrod_rotem/.code
x-user-u_36f0/advisor-token 2>/dev/null)" env -u OPENAI_API_KEY codex --yolo; fi
╭─────────────────────────────────────────────────╮
│ >_ OpenAI Codex (v0.146.0)                      │
│                                                 │
│ model:       gpt-5.6-sol max   /model to change │
│ directory:   /tmp/termqa                        │
│ permissions: YOLO mode                          │
╰─────────────────────────────────────────────────╯

  Tip: Use /status to see the current model, approvals, and token usage.

› Use /skills to list available skills

  gpt-5.6-sol max · /tmp/termqa
› Open the sheet and tell me what changed

• Ran test ! -e .codex-scratch.md && echo clean
  │ ss -ltn '( sport = :9234 )' | tail -n +2
  └ clean

• Done — I submitted an Editor access request for susie@nemopowertools.com.
  Google confirmed “Request sent.”

  You’ll become an editor after the file owner approves it. Open the spreadsheet
  (https://docs.google.com/spreadsheets/d/1I5LiisH_i6ICM8ibZu4UNfAUe8oObwbE/edit
?gid=1321488188).

─ Worked for 3m 28s ────────────────────────────────────────────────────────────

• Working (23s • esc to interrupt) · 1 background terminal running · /ps to view

› Use /skills to list available skills

  gpt-5.6-sol max · /tmp/termqa
"""

# The harness: pull the terminal half of the inline script out of app.py, run it
# with just enough DOM to load, and answer one question per call. The triple
# quote that opens HTML_PAGE is assembled rather than written out, so that this
# file can hold the pattern that matches it.
DRIVER = r"""
const fs=require('fs'), vm=require('vm');
const src=fs.readFileSync(process.argv[1],'utf8');
const Q='"'.repeat(3);
const html=src.match(new RegExp('^HTML_PAGE = r'+Q+'([\\s\\S]*?)^'+Q,'m'))[1];
const js=html.match(/<script[^>]*>([\s\S]*?)<\/script>/)[1];
const region=js.slice(js.indexOf('function getCleanViewPref(){'), js.indexOf('// ── Freeze ─'));
const noop=()=>{};
const ctx=vm.createContext({
  console, sessions:[], selectedSession:null,
  localStorage:{getItem:k=>(k==='terminalCleanView'?process.argv[3]:null), setItem:noop},
  document:{addEventListener:noop,getElementById:()=>null},
  window:{addEventListener:noop,getSelection:()=>null,performance:{now:()=>0}},
  setInterval:noop, setTimeout:noop, clearTimeout:noop,
  getComputedStyle:()=>({fontSize:'13px',paddingLeft:'12px',paddingRight:'12px'}),
});
vm.runInContext(region, ctx);
const pane=fs.readFileSync(process.argv[2],'utf8');
const split=ctx.splitLiveTail(pane.split('\n'));
const filtered=ctx.applyRawFilter(split.body.join('\n'));
process.stdout.write(JSON.stringify({
  live: split.live,
  body: split.body,
  clean: filtered.split('\n'),
  flow: ctx._unwrapRows(filtered.split('\n'), 80),
  diff_append: ctx._lineDiff(['a','b','c'], ['a','b','c','d']),
  diff_same: ctx._lineDiff(['a','b'], ['a','b']),
  diff_middle: ctx._lineDiff(['a','b','c'], ['a','B','c']),
}));
"""


def _run(pane: str, clean_view: bool = True) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix="term-render-"))
    driver = tmp / "driver.js"
    driver.write_text(DRIVER)
    pane_file = tmp / "pane.txt"
    pane_file.write_text(pane)
    out = subprocess.run(
        [NODE, str(driver), str(APP), str(pane_file), "true" if clean_view else "false"],
        capture_output=True, text=True, timeout=60,
    )
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture(scope="module")
def rendered():
    return _run(PANE)


def test_live_status_is_read_off_the_spinner_row(rendered):
    live = rendered["live"]
    assert live["seen"] is True
    assert live["verb"] == "Working"
    assert live["sec"] == 23
    assert live["esc"] is True
    assert "background terminal running" in live["note"]


def test_live_status_row_never_reaches_the_transcript(rendered):
    assert not any("esc to interrupt" in l for l in rendered["body"])


def test_prose_that_merely_mentions_seconds_is_not_a_status_row():
    prose = [
        "• I’m running the command exactly as provided; it should return after about 50 seconds.",
        "• Ran bash -lc \"sleep 50\"",
        "• Working (3s • esc to interrupt)",
    ]
    out = _run("\n".join(prose))
    assert len(out["body"]) == 2, out["body"]
    assert out["live"]["sec"] == 3


def test_completion_rule_becomes_the_idle_reading():
    out = _run("• done\n─ Worked for 50m 51s ────────────────────")
    assert out["live"]["done"] is True
    assert out["live"]["sec"] == 50 * 60 + 51


def test_clean_view_drops_every_kind_of_chrome(rendered):
    text = "\n".join(rendered["clean"])
    assert "CODEX_HOME=" not in text                      # the launcher's shell line
    assert "OpenAI Codex (v0.146.0)" not in text          # the start-up banner
    assert "Use /skills to list available skills" not in text   # composer placeholder
    assert "gpt-5.6-sol max ·" not in text           # model + cwd footer
    assert "Worked for 3m 28s" not in text                # the completion rule
    assert not re.search(r"^─{18,}$", text, re.M)    # bare rules
    assert "Ran test ! -e" not in text                    # the tool call itself


def test_clean_view_keeps_what_was_actually_said(rendered):
    text = "\n".join(rendered["clean"])
    assert "Open the sheet and tell me what changed" in text     # the user's message
    assert "I submitted an Editor access request" in text
    assert "Request sent" in text


def test_a_message_you_sent_is_not_mistaken_for_the_placeholder():
    # Both are drawn with `›`; only the one sitting above the footer is chrome.
    out = _run("› a real message\n• a reply\n› Use /skills to list available skills\n\n  gpt-5.6-sol max · /tmp/x")
    text = "\n".join(out["clean"])
    assert "a real message" in text
    assert "Use /skills" not in text


def test_a_markdown_bullet_is_not_mistaken_for_the_footer():
    out = _run("• notes\n- Ops · /var/log\n  docs · /home/x")
    text = "\n".join(out["clean"])
    assert "- Ops · /var/log" in text
    assert "docs · /home/x" in text


def test_raw_view_passes_the_pane_through_untouched():
    out = _run(PANE, clean_view=False)
    assert "CODEX_HOME=" in "\n".join(out["clean"])
    # ...but the live row is still cut, in both views.
    assert not any("esc to interrupt" in l for l in out["body"])


def test_flow_mode_rejoins_a_url_cut_at_the_margin(rendered):
    joined = "\n".join(rendered["flow"])
    assert "edit?gid=1321488188" in joined
    assert not re.search(r"^\?gid=", joined, re.M)


def test_flow_mode_leaves_structure_alone():
    # Two short list items are not one paragraph, however close to the margin
    # the first one ends.
    out = _run("• head\n  - Item one.\n  - Item two.")
    assert not any("Item one." in l and "Item two." in l for l in out["flow"]), out["flow"]


def test_flow_mode_keeps_a_rendered_table_intact():
    table = [
        "┌──────────┬────────┐",
        "│ channel  │ visits │",
        "├──────────┼────────┤",
        "│ direct   │ 1200   │",
        "└──────────┴────────┘",
    ]
    out = _run("\n".join(table))
    assert out["flow"] == table, out["flow"]


def test_the_paint_rewrites_only_what_moved(rendered):
    assert rendered["diff_append"] == {"from": 3, "remove": 0, "insert": 1}
    assert rendered["diff_same"] == {"from": 2, "remove": 0, "insert": 0}
    assert rendered["diff_middle"] == {"from": 1, "remove": 1, "insert": 1}
