"""The terminal shows every link whole, and the header and favicon say what matters.

2026-09-17: the /login link came off the dashboard terminal as
"...platform.claude.co m%2Foauth..." and the sign-in page rejected it. Claude
Code hard-cuts a token longer than a row at its right margin; the flow view
glued the pieces back with a space because it measured the margin off the
divider lines, which are wider than the text. These tests drive the page's own
JavaScript in node, lifted out of app.py by name.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

APP = Path(__file__).parent / "app.py"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not installed")

_TOP = re.compile(r"^(?:const|let|var|function|//)")
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")


def _js(seeds, provided=()):
    lines = APP.read_text().split("\n")
    starts, bodies = {}, {}
    for i, line in enumerate(lines):
        m = re.match(r"(?:const|function)\s+([A-Za-z_$][\w$]*)", line)
        if not m or m.group(1) in starts:
            continue
        j = i + 1
        while j < len(lines) and not _TOP.match(lines[j]):
            j += 1
        starts[m.group(1)] = i
        bodies[m.group(1)] = "\n".join(lines[i:j]).rstrip()
    need, seen = list(seeds), set()
    while need:
        n = need.pop()
        if n in seen or n not in bodies or n in provided:
            continue
        seen.add(n)
        for ident in _IDENT.findall(bodies[n]):
            if ident in bodies and ident not in seen:
                need.append(ident)
    src = "\n".join(bodies[n] for n in sorted(seen, key=lambda n: starts[n]))
    return src.replace("__CACHE_ALERT_LEAD__", "900").replace("__CACHE_PUSH_LEAD__", "600")


def node(seeds, body, pre=""):
    # Whatever the fixture declares itself is not lifted out of app.py as well.
    provided = set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)", pre))
    provided |= set(re.findall(r",\s*([A-Za-z_$][\w$]*)\s*=", pre))
    script = pre + "\n" + _js(seeds, provided) + "\n" + body
    p = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


LOGIN_URL = (
    "https://claude.com/cai/oauth/authorize?code=true&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e"
    "&response_type=code&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
    "&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference+user%3Asessions%3Aclaude_code"
    "+user%3Amcp_servers+user%3Afile_upload+user%3Aplugins&code_challenge=9RibNcxy4IXGZUQy1GMVAZF2Km4_"
    "-zG74QRFLPnJwqo&code_challenge_method=S256&state=ATPsHKtx9P3nWD12ZW7IiRip4NAVwVbuH-kRkJNwGoY"
)


def login_screen(width=80, indent=2):
    """The /login screen as the pane holds it: the link hard-cut at the text
    margin, every piece indented, dividers drawn to the pane edge."""
    room = width - 1 - indent
    pieces = [LOGIN_URL[i:i + room] for i in range(0, len(LOGIN_URL), room)]
    rows = ["─" * width, " Login", "",
            " " * indent + "Browser didn't open? Use the url below to sign in (c to copy)", ""]
    rows += [" " * indent + p for p in pieces]
    rows += ["", " " * indent + "Paste code here if prompted >", "", " " * indent + "Esc to cancel",
             "─" * width]
    return rows


JOIN = ["_joinCutLinks"]


@pytest.mark.parametrize("width", [60, 80, 120, 200])
def test_the_login_link_comes_back_as_one_unbroken_row(width):
    rows = login_screen(width)
    out = node(JOIN, "console.log(JSON.stringify(_joinCutLinks(%s,%d)))" % (json.dumps(rows), width))
    joined = [r for r in out if "https://" in r]
    assert len(joined) == 1
    assert joined[0].strip() == LOGIN_URL, "no space, no break, nothing lost"
    assert any("Paste code here" in r for r in out), "the rows after the link stay their own rows"
    assert any("Browser didn't open" in r for r in out)


def test_the_real_screen_the_old_flow_view_broke():
    """The widest rows on screen are the dividers, a column wider than the text."""
    rows = login_screen(80)
    js = ("const rows=%s;const j=_joinCutLinks(rows,80);"
          "const f=_unwrapRows(j,80);console.log(JSON.stringify(f.filter(r=>r.indexOf('https://')>=0)))"
          % json.dumps(rows))
    out = node(JOIN + ["_unwrapRows"], js)
    assert len(out) == 1 and LOGIN_URL in out[0] and " m%2F" not in out[0]


def test_a_link_that_ends_at_the_margin_keeps_the_space_before_the_next_word():
    width = 80
    text = "  Open https://example.com/reports/weekly/2026-09-17/summary-for-the-team.html"
    text = text + "x" * (width - 1 - len(text))          # the row ends exactly at the margin
    rows = [text, "  and check the totals against last week.", ""]
    out = node(JOIN, "console.log(JSON.stringify(_joinCutLinks(%s,%d)))" % (json.dumps(rows), width))
    assert out[:2] == [rows[0], rows[1]], "a short link is never cut, so this is a word wrap"


def test_a_link_that_stopped_short_is_left_alone():
    rows = ["  See https://example.com/a for details", "  https://example.com/b is the other one"]
    out = node(JOIN, "console.log(JSON.stringify(_joinCutLinks(%s,80)))" % json.dumps(rows))
    assert out == rows


def test_two_links_on_consecutive_rows_are_not_merged():
    a = "  https://example.com/" + "a" * 57
    rows = [a, "  https://example.com/second"]
    out = node(JOIN, "console.log(JSON.stringify(_joinCutLinks(%s,80)))" % json.dumps(rows))
    assert out == rows


def test_a_long_file_path_is_rejoined_too():
    path = "/home/nimrod_rotem/work/opentechnical/reports/2026-09-17/opentechnical_next_round_full_detail.md"
    room = 77
    rows = ["● Report:", "  " + path[:room], "  " + path[room:] + " is the full report."]
    out = node(JOIN, "console.log(JSON.stringify(_joinCutLinks(%s,80)))" % json.dumps(rows))
    assert out[1] == "  " + path + " is the full report."


def test_the_linkifier_makes_one_anchor_for_the_whole_link():
    rows = login_screen(80)
    js = ("const j=_joinCutLinks(%s,80);"
          "const html=_linkifyTerminalText(j.join('\\n'),1000000);"
          "console.log(JSON.stringify(html.match(/<a [^>]*>/g)))" % json.dumps(rows))
    anchors = node(JOIN + ["_linkifyTerminalText"], js, pre="const BASE='';")
    assert len(anchors) == 1
    href = re.search(r'href="([^"]+)"', anchors[0]).group(1).replace("&amp;", "&")
    assert href == LOGIN_URL


def test_both_views_join_links_before_anything_else():
    src = APP.read_text()
    body = src[src.index("function renderRawText(name,force){"):]
    body = body[:body.index("function rerenderAllRaw")]
    assert body.index("rows=_joinCutLinks(rows,st.paneWidth);") < body.index("if(flow){")
    assert "_linkifyTerminalText(rows.join('\\n'),1000000)" in body


# ── header: time until each usage window resets ─────────────────────────────

@pytest.mark.parametrize("secs,want", [
    (4 * 86400 + 3600, "4D"), (23 * 3600 + 1800, "23H"), (90 * 60, "1H"),
    (40 * 60 + 30, "40M"),   # + 30s: whole minutes, so the clock must not tick past one
    (20, "1M"), (-5, ""),
])
def test_reset_countdown_reads_like_4D_or_23H(secs, want):
    js = "console.log(JSON.stringify(_fmtResetShort(new Date(Date.now()+%d*1000).toISOString())))" % secs
    assert node(["_fmtResetShort"], js) == want


def test_the_header_has_a_reset_slot_after_each_percent():
    src = APP.read_text()
    for w in ("5h", "7d"):
        pct = src.index('id="nav-usage-%s-pct"' % w)
        assert src.index('id="nav-usage-%s-reset"' % w) > pct


# ── phone vs desktop default view ───────────────────────────────────────────

@pytest.mark.parametrize("narrow,simple,want", [
    (True, False, "chat"), (False, False, "raw"), (False, True, "chat"),
])
def test_a_phone_opens_chat_and_a_desktop_the_terminal(narrow, simple, want):
    pre = ("const MEMBER_SIMPLE=%s;const window={matchMedia:q=>({matches:%s})};"
           % ("true" if simple else "false", "true" if narrow else "false"))
    assert node(["_defaultSessionTab", "_allowedSessionTab"],
                "console.log(JSON.stringify([_defaultSessionTab(),_allowedSessionTab('bogus'),_allowedSessionTab('raw')]))",
                pre=pre) == [want, want, "raw"]


# ── favicon: any session's cache alert, not just the selected one ───────────

FAVICON_PRE = """
const link={href:''};
const document={getElementById:id=>id==='favicon'?link:null};
const lastStatus={};
let _faviconStatus='unknown',_faviconFlashOn=false,_faviconShown='';
"""


def test_the_favicon_flashes_for_a_session_you_are_not_looking_at():
    js = """
      sessions[1].last_turn_end=Date.now()/1000-50*60;   // beta: 10 minutes from cold
      lastStatus.alpha='busy';lastStatus.beta='idle';
      updateFavicon('busy');                              // alpha is the selected one
      const a=link.href;_paintFavicon();const b=link.href;_paintFavicon();const c=link.href;
      console.log(JSON.stringify({a,b,c}));
    """
    pre = FAVICON_PRE + ("const sessions=[{name:'alpha',activity_status:'busy',cache_ttl:3600,last_turn_end:0},"
                         "{name:'beta',activity_status:'idle',cache_ttl:3600,last_turn_end:0}];")
    out = node(["_paintFavicon", "updateFavicon", "_inCacheAlert", "_cacheSecondsLeft", "_faviconSvg"], js, pre=pre)
    assert out["a"] != out["b"] and out["a"] == out["c"], "it alternates: that is the flash"
    assert "f85149" not in out["a"] + out["b"], "not the selected session's busy red"


def test_without_an_alert_the_favicon_is_the_selected_sessions_status():
    js = """
      lastStatus.alpha='busy';
      updateFavicon('busy');const a=link.href;_paintFavicon();const b=link.href;
      console.log(JSON.stringify({a,b}));
    """
    pre = FAVICON_PRE + "const sessions=[{name:'alpha',activity_status:'busy',cache_ttl:3600,last_turn_end:0}];"
    out = node(["_paintFavicon", "updateFavicon", "_inCacheAlert", "_cacheSecondsLeft", "_faviconSvg"], js, pre=pre)
    assert out["a"] == out["b"] and "f85149" in out["a"]


def test_header_dots_flash_on_the_same_window():
    js = """
      sessions[0].last_turn_end=Date.now()/1000-50*60;
      const inWindow=_idleNudgeNavDotClass('alpha','idle');
      sessions[0].last_turn_end=Date.now()/1000-20*60;
      const early=_idleNudgeNavDotClass('alpha','idle');
      sessions[0].last_turn_end=Date.now()/1000-50*60;
      const busy=_idleNudgeNavDotClass('alpha','busy');
      console.log(JSON.stringify({inWindow,early,busy}));
    """
    pre = ("const lastStatus={};const _completedUnread={};"
           "const sessions=[{name:'alpha',activity_status:'idle',cache_ttl:3600,last_turn_end:0}];")
    out = node(["_idleNudgeNavDotClass", "_cacheSecondsLeft", "CACHE_ALERT_LEAD"], js, pre=pre)
    assert out["inWindow"].endswith(" expiring")
    assert "expiring" not in out["early"] and "expiring" not in out["busy"]


def test_the_attention_clock_runs_in_a_worker_so_a_hidden_tab_keeps_flashing():
    src = APP.read_text()
    body = src[src.index("function startAttentionClock(){"):]
    body = body[:body.index("\n}\n")]
    assert "new Worker(" in body and "setInterval(_attentionTick" in body
    assert "startAttentionClock();" in src
