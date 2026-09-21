#!/usr/bin/env python3
"""Apply the 2026-09-21 usage-freshness and live-status patch to a dashboard app.py.

Three things, all reported as broken from builder5:

1. The 5-hour usage number could be an hour stale, because the backend cached the
   Anthropic usage payload for 3600s and the page polled it every 600s on top.
   A box whose 5h window was fully spent read 32%. Server cache drops to 60s and
   the page polls every 60s.

2. The header now carries the SEVEN-DAY window only, with "Reset: 4d" on its own
   line above the bar instead of a bare "4D" beside it. The 5-hour bar moves into
   the account dropdown, under Today's Usage.

3. A session that went from working to idle stayed red until the page was
   reloaded. The ten-second status poll wrote the pill's DOM and nothing else, so
   `sessions[i].activity_status` stayed frozen at what the page load saw and every
   later re-render painted from it. Also: polls could pile up behind a slow
   /api/status, and a backgrounded tab (timers throttled to ~1/min) never caught
   up when it was looked at again.

Every copy of app.py in the fleet is a slightly different generation, so this
matches on exact anchors, is idempotent (a patch already applied is skipped), and
reports per-patch what it did. It never touches anything but app.py.

    python3 patch_usage_and_status_20260921.py /path/to/app.py [--dry-run]
"""
from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import time

# (label, [(old, new), ...]). The alternatives exist because the fleet runs several
# generations of this file: builder5, builder2, builder2a and claude.lisa.my have a
# newer status pill (_pillInner / lastDetail / _isCompacting) than builder1,
# builder3 and builder4. The first alternative whose anchor is present wins; each
# `new` doubles as the already-applied marker for its own variant.
#
# `only_if` marks a patch that applies only to a file containing that substring,
# and is SKIPPED, not failed, elsewhere: builder2a runs sessions through a model
# gateway, and only that copy needs the guard that goes with it.
#
# `applied_if` is a second already-applied marker, for a patch whose own `new`
# text a LATER patch then edits: without it a re-run sees neither the marker nor
# the anchor and reports a patch that is in fact in place.
PATCHES: list[tuple[str, list[tuple[str, str]], str, str]] = []


def patch(label: str, old: str, new: str, only_if: str = "",
          applied_if: str = "") -> None:
    PATCHES.append((label, [(old, new)], only_if, applied_if))


def patch_any(label: str, *pairs: tuple[str, str], only_if: str = "",
              applied_if: str = "") -> None:
    PATCHES.append((label, list(pairs), only_if, applied_if))


# ── 1. Backend: the usage payload was cached for an hour ─────────────────────
patch(
    "backend: ANTHROPIC_LIMITS_TTL constant",
    '''_anthropic_limits_cache: dict = {"ts": 0, "data": None, "fp": "",
                                 "retry_after": 0, "retry_fp": "", "last_error": ""}''',
    '''_anthropic_limits_cache: dict = {"ts": 0, "data": None, "fp": "",
                                 "retry_after": 0, "retry_fp": "", "last_error": ""}
# How long a fetched payload is served before we go back upstream. This was an
# HOUR, which is a fifth of the 5-hour window: a box whose 5h limit was fully
# spent sat on screen at 32% until someone reloaded long enough after the fact,
# and the bar people use to decide whether to start a session was the one number
# they could not trust. A minute costs at most 60 upstream calls an hour, and the
# 429 handler above still backs off hard if Anthropic objects.
ANTHROPIC_LIMITS_TTL = 60''',
)

patch(
    "backend: usage endpoint docstring",
    '''    Cached for 1 hour per the user-facing requirement (poll hourly while
    sessions are active). On upstream failure, returns the last good payload.
    The cache is keyed on the current token, so a login switch busts it at once
    instead of showing a previous account's usage for up to an hour.
    """''',
    '''    Cached for ANTHROPIC_LIMITS_TTL seconds. On upstream failure, returns the
    last good payload. The cache is keyed on the current token, so a login switch
    busts it at once instead of showing a previous account's usage.
    """''',
)

patch(
    "backend: serve-cache window 3600s -> ANTHROPIC_LIMITS_TTL",
    '''    # Serve cache only if fresh (<1h), same token, AND none of its windows have
    # reset since — otherwise a rolled-over window (e.g. the 7-day limit) keeps
    # showing its pre-reset peak (100%) for up to an hour after it dropped to ~0.
    if (now - _anthropic_limits_cache["ts"] < 3600
            and _anthropic_limits_cache["data"]''',
    '''    # Serve cache only if fresh, same token, AND none of its windows have reset
    # since — otherwise a rolled-over window (e.g. the 7-day limit) keeps showing
    # its pre-reset peak (100%) after it has dropped back to ~0.
    if (now - _anthropic_limits_cache["ts"] < ANTHROPIC_LIMITS_TTL
            and _anthropic_limits_cache["data"]''',
)

# ── 2. Header: 7-day only, with "Reset: 4d" above the bar ────────────────────
_RESET_CSS = '''/* Time until the window resets, "Reset: 4d". In the header it is its own line
   ABOVE the 7-day bar; in the account dropdown it trails the 5-hour bar. Hidden
   while empty, because Anthropic reports no reset time for a window nothing has
   been spent in yet and a bare "Reset:" reads as a broken widget. */
.nav-usage-reset{color:#6e7681;font-size:.6rem;font-weight:600;text-align:left;font-variant-numeric:tabular-nums;white-space:nowrap}
.nav-usage-reset:empty{display:none}'''
_PCT_CSS = ('.nav-usage-pct{color:#8b949e;font-size:.6rem;font-weight:600;width:26px;'
            'text-align:right;font-variant-numeric:tabular-nums}')

patch_any(
    "css: .nav-usage-reset is a line, not a suffix",
    # Most copies already have a reset rule to rewrite.
    ('''/* Time until the window resets, "4D" or "23H", the same small type as the percent. */
.nav-usage-reset{color:#6e7681;font-size:.6rem;font-weight:600;min-width:22px;text-align:left;font-variant-numeric:tabular-nums}
.nav-usage-reset:empty{display:none}''',
     _RESET_CSS),
    # builder2a's release predates the reset countdown entirely, so there is
    # nothing to rewrite: add the rule after the percentage it sits beside.
    (_PCT_CSS, _PCT_CSS + "\n" + _RESET_CSS),
)

_HEADER_NEW = '''  <!-- The header carries the SEVEN-DAY window only: "Reset: 4d" on the top line,
       the bar and its percentage under it. That is the number you plan a week
       around, and it changes slowly enough to be worth permanent eyeline. The
       5-hour window moved into the account dropdown, under Today's Usage, next to
       the token counts it belongs with. -->
  <span class="nav-usage no-data" id="nav-usage">
    <span class="nav-usage-reset" id="nav-usage-7d-reset" title=""></span>
    <span class="nav-usage-item" id="nav-usage-7d-wrap" title="Anthropic 7-day limit">
      <span class="nav-usage-label">7d</span>
      <span class="nav-usage-bar"><span class="nav-usage-fill" id="nav-usage-7d-fill" style="width:0%"></span></span>
      <span class="nav-usage-pct" id="nav-usage-7d-pct">&mdash;</span>
    </span>
  </span>'''

patch_any(
    "html: drop the 5h item from the header, put Reset above the 7d bar",
    ('''  <span class="nav-usage no-data" id="nav-usage">
    <span class="nav-usage-item" id="nav-usage-5h-wrap" title="Anthropic 5-hour limit">
      <span class="nav-usage-label">5h</span>
      <span class="nav-usage-bar"><span class="nav-usage-fill" id="nav-usage-5h-fill" style="width:0%"></span></span>
      <span class="nav-usage-pct" id="nav-usage-5h-pct">&mdash;</span>
      <span class="nav-usage-reset" id="nav-usage-5h-reset"></span>
    </span>
    <span class="nav-usage-item" id="nav-usage-7d-wrap" title="Anthropic 7-day limit">
      <span class="nav-usage-label">7d</span>
      <span class="nav-usage-bar"><span class="nav-usage-fill" id="nav-usage-7d-fill" style="width:0%"></span></span>
      <span class="nav-usage-pct" id="nav-usage-7d-pct">&mdash;</span>
      <span class="nav-usage-reset" id="nav-usage-7d-reset"></span>
    </span>
  </span>''', _HEADER_NEW),
    # builder2a: same two rows, no reset spans in them yet.
    ('''  <span class="nav-usage no-data" id="nav-usage">
    <span class="nav-usage-item" id="nav-usage-5h-wrap" title="Anthropic 5-hour limit">
      <span class="nav-usage-label">5h</span>
      <span class="nav-usage-bar"><span class="nav-usage-fill" id="nav-usage-5h-fill" style="width:0%"></span></span>
      <span class="nav-usage-pct" id="nav-usage-5h-pct">&mdash;</span>
    </span>
    <span class="nav-usage-item" id="nav-usage-7d-wrap" title="Anthropic 7-day limit">
      <span class="nav-usage-label">7d</span>
      <span class="nav-usage-bar"><span class="nav-usage-fill" id="nav-usage-7d-fill" style="width:0%"></span></span>
      <span class="nav-usage-pct" id="nav-usage-7d-pct">&mdash;</span>
    </span>
  </span>''', _HEADER_NEW),
)

_RESET_JS = '''// "4d", "23h", "40m": whole units left until the window resets.
function _fmtResetShort(iso){
  if(!iso)return'';
  const ms=new Date(iso)-new Date();
  if(!(ms>0))return'';
  const mins=Math.floor(ms/60000);
  if(mins<60)return Math.max(1,mins)+'m';
  const hrs=Math.floor(mins/60);
  if(hrs<24)return hrs+'h';
  return Math.floor(hrs/24)+'d';
}
let _usageResets={fh:'',sd:''};
function _paintUsageResets(){
  // The 5-hour row only exists while the account dropdown is open; getElementById
  // returning null for it is the normal case, not a failure.
  [['auth-usage-5h-reset',_usageResets.fh,'5-hour'],['nav-usage-7d-reset',_usageResets.sd,'7-day']]
    .forEach(([id,iso,label])=>{
      const el=document.getElementById(id);
      if(!el)return;
      const short=_fmtResetShort(iso);
      const txt=short?('Reset: '+short):'';
      if(el.textContent!==txt)el.textContent=txt;
      el.title=short?('The '+label+' window resets '+_fmtResetTime(iso)):'';
    });
}'''

patch_any(
    "js: lowercase reset units and the 'Reset: ' prefix",
    # builder2a has no reset countdown at all: introduce the helpers ahead of
    # _fmtResetTime, which every copy does have. Listed first because its anchor
    # is the narrower of the two.
    ('''let _usageLimitsTimer=null;
function _fmtResetTime(iso){''',
     'let _usageLimitsTimer=null;\n' + _RESET_JS
     + '\nsetInterval(_paintUsageResets,60000);\nfunction _fmtResetTime(iso){'),
    ('''// "4D", "23H", "40M": whole units left until the window resets, for the header.
function _fmtResetShort(iso){
  if(!iso)return'';
  const ms=new Date(iso)-new Date();
  if(!(ms>0))return'';
  const mins=Math.floor(ms/60000);
  if(mins<60)return Math.max(1,mins)+'M';
  const hrs=Math.floor(mins/60);
  if(hrs<24)return hrs+'H';
  return Math.floor(hrs/24)+'D';
}
let _usageResets={fh:'',sd:''};
function _paintUsageResets(){
  [['nav-usage-5h-reset',_usageResets.fh,'5-hour'],['nav-usage-7d-reset',_usageResets.sd,'7-day']]
    .forEach(([id,iso,label])=>{
      const el=document.getElementById(id);
      if(!el)return;
      const txt=_fmtResetShort(iso);
      if(el.textContent!==txt)el.textContent=txt;
      el.title=txt?('The '+label+' window resets '+_fmtResetTime(iso)):'';
    });
}''',
    '''// "4d", "23h", "40m": whole units left until the window resets.
function _fmtResetShort(iso){
  if(!iso)return'';
  const ms=new Date(iso)-new Date();
  if(!(ms>0))return'';
  const mins=Math.floor(ms/60000);
  if(mins<60)return Math.max(1,mins)+'m';
  const hrs=Math.floor(mins/60);
  if(hrs<24)return hrs+'h';
  return Math.floor(hrs/24)+'d';
}
let _usageResets={fh:'',sd:''};
function _paintUsageResets(){
  // The 5-hour row only exists while the account dropdown is open; getElementById
  // returning null for it is the normal case, not a failure.
  [['auth-usage-5h-reset',_usageResets.fh,'5-hour'],['nav-usage-7d-reset',_usageResets.sd,'7-day']]
    .forEach(([id,iso,label])=>{
      const el=document.getElementById(id);
      if(!el)return;
      const short=_fmtResetShort(iso);
      const txt=short?('Reset: '+short):'';
      if(el.textContent!==txt)el.textContent=txt;
      el.title=short?('The '+label+' window resets '+_fmtResetTime(iso)):'';
    });
}'''),
)

# The shared tail of the split: everything from the helper preamble down to the
# staleNote line, which is where the moved body begins.
_SPLIT_NEW = '''// Last good payload. Kept because the 5-hour row now lives in the account
// dropdown, which is rebuilt every time it is opened: without this it would show
// an empty bar until the next poll came round.
let _usageLimitsData=null;
// Every id prefix a bar is painted into. The 5-hour pair exists only while the
// account dropdown is open and the tools mirror is phone-only, so a null lookup
// here is the normal case, not a failure.
const _USAGE_5H_IDS=['auth-usage-5h','tools-usage-5h'];
const _USAGE_7D_IDS=['nav-usage-7d','tools-usage-7d'];
// The bars stay on screen either way — only their state changes. `has-data` used
// to gate `display`, so one bad answer made them vanish entirely and the feature
// looked deleted.
function _setUsageHasData(has,why){
  const wrap=document.getElementById('nav-usage');
  const toolsWrap=document.getElementById('nav-tools-usage');
  if(wrap){wrap.classList.toggle('has-data',has);wrap.classList.toggle('no-data',!has)}
  if(toolsWrap){toolsWrap.classList.toggle('has-data',has);toolsWrap.classList.toggle('no-data',!has)}
  if(has)return;
  _usageResets={fh:'',sd:''};_paintUsageResets();
  const t='Anthropic usage unavailable — '+(why||'no data yet.');
  _USAGE_5H_IDS.concat(_USAGE_7D_IDS).forEach(p=>{
    const pct=document.getElementById(p+'-pct'); if(pct)pct.textContent='—';
    const fill=document.getElementById(p+'-fill'); if(fill)fill.style.width='0%';
    const w=document.getElementById(p+'-wrap'); if(w)w.title=t;
  });
}
// Paint a payload onto whichever bars are currently in the DOM. Split out from
// the fetch so opening the account dropdown repaints its 5-hour row at once.
function _applyUsageLimits(data){
  if(!data||(!data.five_hour&&!data.seven_day))return;
  _setUsageHasData(true);
  const wrap=document.getElementById('nav-usage');
  if(wrap)wrap.classList.toggle('stale',!!data.stale);
  const fh=data.five_hour||{};
  const sd=data.seven_day||{};
  _usageResets={fh:fh.resets_at||'',sd:sd.resets_at||''};
  _paintUsageResets();
  const fhPct=Math.round(Number(fh.utilization)||0);
  const sdPct=Math.round(Number(sd.utilization)||0);
  const staleNote=data.stale?' · last known value (upstream unreachable)':'';'''

patch_any(
    "js: split the usage paint out of the fetch, retarget the 5h ids",
    # builder2a first: its copy opens with a model-gateway branch, which is
    # preserved as _usageBarsDisabled() and called back from the fetch below.
    ('''async function refreshUsageLimits(){
  if(MODEL_GATEWAY){
    ['nav-usage','nav-tools-usage'].forEach(id=>{const el=document.getElementById(id);if(el)el.classList.add('disabled')});
    return;
  }
  if(MEMBER_SIMPLE) return;  // members don't see usage bars
  const wrap=document.getElementById('nav-usage');
  const toolsWrap=document.getElementById('nav-tools-usage');
  if(!wrap)return;
  // The bars stay on screen either way — only their state changes. `has-data`
  // used to gate `display`, so one bad answer made them vanish entirely and the
  // feature looked deleted.
  const setHasData=(has,why)=>{
    wrap.classList.toggle('has-data',has);
    wrap.classList.toggle('no-data',!has);
    if(toolsWrap){toolsWrap.classList.toggle('has-data',has);toolsWrap.classList.toggle('no-data',!has)}
    if(!has){
      const t='Anthropic usage unavailable — '+(why||'no data yet.');
      ['nav-usage-5h','nav-usage-7d','tools-usage-5h','tools-usage-7d'].forEach(p=>{
        const pct=document.getElementById(p+'-pct'); if(pct)pct.textContent='—';
        const fill=document.getElementById(p+'-fill'); if(fill)fill.style.width='0%';
      });
      ['nav-usage-5h-wrap','nav-usage-7d-wrap','tools-usage-5h-wrap','tools-usage-7d-wrap']
        .forEach(id=>{const el=document.getElementById(id); if(el)el.title=t});
    }
  };
  try{
    const resp=await fetch(BASE+'/api/usage/limits');
    // Only blank the bars if we have never had data. Once they are showing,
    // a transient failure should leave the last known values on screen.
    if(!resp.ok){
      let err='', g=null;
      try{ const j=await resp.json(); err=j.error||''; g=j.grant||null; }catch(e){}
      let why=_usageErrText(err);
      if(g && !g.have_credential && g.retry_in_s>0)
        why += ' The dashboard is re-granting it by itself — next attempt in '+Math.ceil(g.retry_in_s/60)+' min.';
      if(!wrap.classList.contains('has-data'))setHasData(false,why);
      _scheduleUsageRetry();return;
    }
    const data=await resp.json();
    if(!data||(!data.five_hour&&!data.seven_day)){setHasData(false,_usageErrText(data&&data.error));_scheduleUsageRetry();return}
    setHasData(true);
    wrap.classList.toggle('stale',!!data.stale);
    if(data.stale)_scheduleUsageRetry();
    const fh=data.five_hour||{};
    const sd=data.seven_day||{};
    const fhPct=Math.round(Number(fh.utilization)||0);
    const sdPct=Math.round(Number(sd.utilization)||0);
    _applyUsageStyle(document.getElementById('nav-usage-5h-fill'),Number(fh.utilization)||0);
    _applyUsageStyle(document.getElementById('nav-usage-7d-fill'),Number(sd.utilization)||0);
    _applyUsageStyle(document.getElementById('tools-usage-5h-fill'),Number(fh.utilization)||0);
    _applyUsageStyle(document.getElementById('tools-usage-7d-fill'),Number(sd.utilization)||0);
    [['nav-usage-5h-pct',fhPct],['nav-usage-7d-pct',sdPct],
     ['tools-usage-5h-pct',fhPct],['tools-usage-7d-pct',sdPct]].forEach(([id,v])=>{
      const el=document.getElementById(id); if(el)el.textContent=v+'%';
    });
    const staleNote=data.stale?' · last known value (upstream unreachable)':'';''',
     '''// This box can route sessions through a model gateway instead of the plan, and
// then the plan's own windows mean nothing, so the bars are hidden rather than
// shown at zero. Its own function so the fetch below reads like every other box's.
function _usageBarsDisabled(){
  if(typeof MODEL_GATEWAY==='undefined'||!MODEL_GATEWAY)return false;
  ['nav-usage','nav-tools-usage'].forEach(id=>{const el=document.getElementById(id);if(el)el.classList.add('disabled')});
  return true;
}
''' + _SPLIT_NEW),
    ('''async function refreshUsageLimits(){
  if(MEMBER_SIMPLE) return;  // members don't see usage bars
  const wrap=document.getElementById('nav-usage');
  const toolsWrap=document.getElementById('nav-tools-usage');
  if(!wrap)return;
  // The bars stay on screen either way — only their state changes. `has-data`
  // used to gate `display`, so one bad answer made them vanish entirely and the
  // feature looked deleted.
  const setHasData=(has,why)=>{
    wrap.classList.toggle('has-data',has);
    wrap.classList.toggle('no-data',!has);
    if(toolsWrap){toolsWrap.classList.toggle('has-data',has);toolsWrap.classList.toggle('no-data',!has)}
    if(!has){
      _usageResets={fh:'',sd:''};_paintUsageResets();
      const t='Anthropic usage unavailable — '+(why||'no data yet.');
      ['nav-usage-5h','nav-usage-7d','tools-usage-5h','tools-usage-7d'].forEach(p=>{
        const pct=document.getElementById(p+'-pct'); if(pct)pct.textContent='—';
        const fill=document.getElementById(p+'-fill'); if(fill)fill.style.width='0%';
      });
      ['nav-usage-5h-wrap','nav-usage-7d-wrap','tools-usage-5h-wrap','tools-usage-7d-wrap']
        .forEach(id=>{const el=document.getElementById(id); if(el)el.title=t});
    }
  };
  try{
    const resp=await fetch(BASE+'/api/usage/limits');
    // Only blank the bars if we have never had data. Once they are showing,
    // a transient failure should leave the last known values on screen.
    if(!resp.ok){
      let err='', g=null;
      try{ const j=await resp.json(); err=j.error||''; g=j.grant||null; }catch(e){}
      let why=_usageErrText(err);
      if(g && !g.have_credential && g.retry_in_s>0)
        why += ' The dashboard is re-granting it by itself — next attempt in '+Math.ceil(g.retry_in_s/60)+' min.';
      if(!wrap.classList.contains('has-data'))setHasData(false,why);
      _scheduleUsageRetry();return;
    }
    const data=await resp.json();
    if(!data||(!data.five_hour&&!data.seven_day)){setHasData(false,_usageErrText(data&&data.error));_scheduleUsageRetry();return}
    setHasData(true);
    wrap.classList.toggle('stale',!!data.stale);
    if(data.stale)_scheduleUsageRetry();
    const fh=data.five_hour||{};
    const sd=data.seven_day||{};
    _usageResets={fh:fh.resets_at||'',sd:sd.resets_at||''};
    _paintUsageResets();
    const fhPct=Math.round(Number(fh.utilization)||0);
    const sdPct=Math.round(Number(sd.utilization)||0);
    _applyUsageStyle(document.getElementById('nav-usage-5h-fill'),Number(fh.utilization)||0);
    _applyUsageStyle(document.getElementById('nav-usage-7d-fill'),Number(sd.utilization)||0);
    _applyUsageStyle(document.getElementById('tools-usage-5h-fill'),Number(fh.utilization)||0);
    _applyUsageStyle(document.getElementById('tools-usage-7d-fill'),Number(sd.utilization)||0);
    [['nav-usage-5h-pct',fhPct],['nav-usage-7d-pct',sdPct],
     ['tools-usage-5h-pct',fhPct],['tools-usage-7d-pct',sdPct]].forEach(([id,v])=>{
      const el=document.getElementById(id); if(el)el.textContent=v+'%';
    });
    const staleNote=data.stale?' · last known value (upstream unreachable)':'';''',
     _SPLIT_NEW),
)

# The block between staleNote and fhTitle moved from a try{} inside the fetch to
# the top level of _applyUsageLimits, so it is one indent level too deep. Purely
# cosmetic, but it is the difference between the hand-edited copy and every
# script-patched one, and that drift is what costs the next reader an hour.
patch(
    "js: re-indent the block the split moved into _applyUsageLimits",
    '''    // Which account, and when it was read. Without these a live 0% is
    // indistinguishable from a dead widget — which is exactly how it reads
    // after a box is signed in to a second, unused account.
    const acct=data.account||{};
    const acctNote=acct.email?' · account '+acct.email+(acct.plan?' ('+acct.plan+')':''):'';
    let readNote='';
    if(data.fetched_at){
      const mins=Math.max(0,Math.round((Date.now()/1000-data.fetched_at)/60));
      readNote=' · read '+(mins<1?'just now':mins<60?mins+'m ago':Math.floor(mins/60)+'h ago');
    }
    // Where the number came from, and whether spend has moved to overage credits
    // (the 7-day window being spent is exactly when that starts to cost money).
    const srcNote=data.source==='ratelimit_headers'
      ? ' · read from the rate-limit headers on a 1-token probe (the account usage API needs a credential this host does not have)'
      : '';
    const ovNote=(data.overage&&data.overage.in_use)
      ? ' · ⚠ plan window spent — running on overage credits'+
        (typeof data.overage.utilization==='number'?' ('+Math.round(data.overage.utilization)+'% of those used)':'')
      : '';
    // Anthropic returns resets_at null for a window nothing has been spent in
    // yet, so "resets " used to trail off into blank space — which reads as a
    // broken widget at exactly the moment the honest answer is "this window just
    // rolled over". Say that instead.
    const _when=(blk,pct)=>{
      const t=_fmtResetTime(blk.resets_at);
      if(t)return'resets '+t;
      return pct?'reset time not reported':'window just rolled over, nothing spent in it yet';
    };''',
    '''  // Which account, and when it was read. Without these a live 0% is
  // indistinguishable from a dead widget — which is exactly how it reads
  // after a box is signed in to a second, unused account.
  const acct=data.account||{};
  const acctNote=acct.email?' · account '+acct.email+(acct.plan?' ('+acct.plan+')':''):'';
  let readNote='';
  if(data.fetched_at){
    const mins=Math.max(0,Math.round((Date.now()/1000-data.fetched_at)/60));
    readNote=' · read '+(mins<1?'just now':mins<60?mins+'m ago':Math.floor(mins/60)+'h ago');
  }
  // Where the number came from, and whether spend has moved to overage credits
  // (the 7-day window being spent is exactly when that starts to cost money).
  const srcNote=data.source==='ratelimit_headers'
    ? ' · read from the rate-limit headers on a 1-token probe (the account usage API needs a credential this host does not have)'
    : '';
  const ovNote=(data.overage&&data.overage.in_use)
    ? ' · ⚠ plan window spent — running on overage credits'+
      (typeof data.overage.utilization==='number'?' ('+Math.round(data.overage.utilization)+'% of those used)':'')
    : '';
  // Anthropic returns resets_at null for a window nothing has been spent in
  // yet, so "resets " used to trail off into blank space — which reads as a
  // broken widget at exactly the moment the honest answer is "this window just
  // rolled over". Say that instead.
  const _when=(blk,pct)=>{
    const t=_fmtResetTime(blk.resets_at);
    if(t)return'resets '+t;
    return pct?'reset time not reported':'window just rolled over, nothing spent in it yet';
  };''',
)

patch(
    "js: paint both windows from the id lists, and refetch separately",
    '''    const fhTitle='Anthropic 5-hour limit · '+fhPct+'% used · '+_when(fh,fhPct)+acctNote+readNote+staleNote+srcNote+ovNote;
    const sdTitle='Anthropic 7-day limit · '+sdPct+'% used · '+_when(sd,sdPct)+acctNote+readNote+staleNote+srcNote+ovNote;
    const fhWrap=document.getElementById('nav-usage-5h-wrap');
    const sdWrap=document.getElementById('nav-usage-7d-wrap');
    if(fhWrap)fhWrap.title=fhTitle;
    if(sdWrap)sdWrap.title=sdTitle;
    const tfh=document.getElementById('tools-usage-5h-wrap');
    const tsd=document.getElementById('tools-usage-7d-wrap');
    if(tfh)tfh.title=fhTitle;
    if(tsd)tsd.title=sdTitle;
  }catch(e){
    /* keep last known display */
  }
}''',
    '''  const fhTitle='Anthropic 5-hour limit · '+fhPct+'% used · '+_when(fh,fhPct)+acctNote+readNote+staleNote+srcNote+ovNote;
  const sdTitle='Anthropic 7-day limit · '+sdPct+'% used · '+_when(sd,sdPct)+acctNote+readNote+staleNote+srcNote+ovNote;
  [[_USAGE_5H_IDS,Number(fh.utilization)||0,fhPct,fhTitle],
   [_USAGE_7D_IDS,Number(sd.utilization)||0,sdPct,sdTitle]].forEach(([ids,util,pct,title])=>{
    ids.forEach(p=>{
      _applyUsageStyle(document.getElementById(p+'-fill'),util);
      const pe=document.getElementById(p+'-pct'); if(pe)pe.textContent=pct+'%';
      const w=document.getElementById(p+'-wrap'); if(w)w.title=title;
    });
  });
}
async function refreshUsageLimits(){
  if(MEMBER_SIMPLE) return;  // members don't see usage bars
  const wrap=document.getElementById('nav-usage');
  if(!wrap)return;
  try{
    const resp=await fetch(BASE+'/api/usage/limits');
    // Only blank the bars if we have never had data. Once they are showing,
    // a transient failure should leave the last known values on screen.
    if(!resp.ok){
      let err='', g=null;
      try{ const j=await resp.json(); err=j.error||''; g=j.grant||null; }catch(e){}
      let why=_usageErrText(err);
      if(g && !g.have_credential && g.retry_in_s>0)
        why += ' The dashboard is re-granting it by itself — next attempt in '+Math.ceil(g.retry_in_s/60)+' min.';
      if(!wrap.classList.contains('has-data'))_setUsageHasData(false,why);
      _scheduleUsageRetry();return;
    }
    const data=await resp.json();
    if(!data||(!data.five_hour&&!data.seven_day)){
      _setUsageHasData(false,_usageErrText(data&&data.error));_scheduleUsageRetry();return;
    }
    if(data.stale)_scheduleUsageRetry();
    _usageLimitsData=data;
    _applyUsageLimits(data);
  }catch(e){
    /* keep last known display */
  }
}''',
    # On the gateway copy the guard patch below edits the function head this
    # patch just wrote, so its own marker no longer matches on a re-run.
    applied_if="  if(_usageBarsDisabled()) return;  // sessions are on a gateway, not the plan",
)

# Only the copy that has a model gateway needs to consult it. Elsewhere the
# function does not exist and the guard would be dead weight.
patch(
    "js: keep the model-gateway guard in front of the usage fetch",
    '''async function refreshUsageLimits(){
  if(MEMBER_SIMPLE) return;  // members don't see usage bars''',
    '''async function refreshUsageLimits(){
  if(_usageBarsDisabled()) return;  // sessions are on a gateway, not the plan
  if(MEMBER_SIMPLE) return;  // members don't see usage bars''',
    only_if="function _usageBarsDisabled(){",
)

patch(
    "js: poll the usage API every minute, not every ten",
    '''  refreshUsageLimits();
  // Poll every 10 min, not hourly. Upstream is still hit at most once an hour —
  // the backend cache enforces that — but the cache is keyed on the token, so a
  // login switch busts it, and an hourly page timer meant the bars kept showing
  // the PREVIOUS account's numbers for up to an hour after the switch.
  _usageLimitsTimer=setInterval(refreshUsageLimits,600*1000);''',
    '''  refreshUsageLimits();
  // Every minute, matching the backend's own cache window (ANTHROPIC_LIMITS_TTL),
  // so what is on screen is never more than about two minutes behind the account.
  // It was ten minutes on top of an hour-long server cache, which is how a box
  // with its 5-hour window fully spent kept reading 32%.
  _usageLimitsTimer=setInterval(refreshUsageLimits,60*1000);''',
)

patch(
    "js: the 5h bar, in the account dropdown under Today's Usage",
    '''function renderUsageHtml(){
  if(!_usageCache||!_usageCache.totalTokens)return '';
  const u=_usageCache;
  const total=u.totalTokens;''',
    '''// The plan's 5-hour rolling window, as a bar with its own reset countdown. It
// lives here rather than in the header: it is the number you check when you are
// deciding whether there is room to start something now, which is a moment you
// open this panel anyway. Values are painted by _applyUsageLimits once this
// markup is in the DOM, so all the HTML has to supply is the shape and the ids.
function renderFiveHourLimitHtml(){
  return '<div class="nav-tools-usage-row" id="auth-usage-5h-wrap" title="Anthropic 5-hour limit">'
    +'<span class="nav-tools-usage-label">5h</span>'
    +'<span class="nav-tools-usage-bar"><span class="nav-usage-fill" id="auth-usage-5h-fill" style="width:0%"></span></span>'
    +'<span class="nav-tools-usage-pct" id="auth-usage-5h-pct">&mdash;</span>'
    +'<span class="nav-usage-reset" id="auth-usage-5h-reset"></span>'
    +'</div>';
}

function renderUsageHtml(){
  // The 5-hour bar renders whether or not any tokens were recorded today: a box
  // that has spent nothing still needs to be able to see that, and this block
  // used to be dropped whole when the count was zero.
  const head='<hr class="auth-divider">'
    +'<div class="auth-title" style="margin-bottom:4px">Today\\'s Usage</div>'
    +renderFiveHourLimitHtml();
  if(!_usageCache||!_usageCache.totalTokens)
    return head+'<p class="auth-hint" style="margin-top:8px">No tokens recorded yet today.</p>';
  const u=_usageCache;
  const total=u.totalTokens;''',
)

patch(
    "js: Today's Usage rows hang off the shared head",
    '''  return '<hr class="auth-divider">'
    +'<div class="auth-title" style="margin-bottom:4px">Today\\'s Usage</div>'
    +'<div class="auth-row"><span class="auth-row-label">Messages</span><span class="auth-row-value">'+u.messages+'</span></div>\'''',
    '''  return head
    +'<div class="auth-row" style="margin-top:8px"><span class="auth-row-label">Messages</span><span class="auth-row-value">'+u.messages+'</span></div>\'''',
)

_HINT_OLD = '''    +'<p class="auth-hint" style="margin-top:6px">Usage resets on a 5-hour rolling window.</p>';
}

function renderAuthPanel(){
  const el=document.getElementById('auth-dropdown-content');
  if(!_authCache){el.innerHTML='<div class="auth-title">Loading...</div>';return}
'''
_HINT_NEW = '''    +'<p class="auth-hint" style="margin-top:6px">Token counts are for today, UTC. The 5h bar above is the plan\\'s own rolling window.</p>';
}

function renderAuthPanel(){
  const el=document.getElementById('auth-dropdown-content');
  if(!_authCache){el.innerHTML='<div class="auth-title">Loading...</div>';return}
'''
# Deliberately worded and positioned exactly as already deployed, so a re-run on
# a box that has it recognises it instead of trying to apply it twice.
_REPAINT = '''  // Repaint the 5-hour bar as soon as the markup above lands, and ask for a fresh
  // read. Without the first call the bar would sit at 0% until the next minute
  // tick, which is indistinguishable from an unspent window.
  setTimeout(()=>{_applyUsageLimits(_usageLimitsData);refreshUsageLimits()},0);'''

patch_any(
    "js: repaint the dropdown's 5h bar the moment it opens",
    # Most copies go straight on to build the panel.
    (_HINT_OLD + "  const usageHtml=renderUsageHtml();",
     _HINT_NEW + "  const usageHtml=renderUsageHtml();\n" + _REPAINT),
    # builder2a has a model-gateway branch before that line; insert above it so
    # the repaint happens whichever branch the panel takes.
    (_HINT_OLD, _HINT_NEW + _REPAINT + "\n"),
)

# ── 3. Live status: working -> idle without a page reload ────────────────────
patch(
    "js: the status poll writes the fresh status back onto the session",
    '''      trackSessionStatus(st.name,st.activity_status);
      updateStatusPill(st.name,st.activity_status,st.activity_detail);
      if(st.name===selectedSession)updateFavicon(st.activity_status);
      // Update model badge in nav
      const si=sessions.findIndex(s=>s.name===st.name);
      if(si>=0){
        let navChanged=false;''',
    '''      trackSessionStatus(st.name,st.activity_status);
      // Write the fresh status back onto the session object BEFORE anything
      // repaints from it. This poll used to update the pill's DOM and nothing
      // else, leaving `sessions[i].activity_status` frozen at whatever the page
      // load saw — so every later re-render (switching session or tab, the nav
      // dots, the favicon, the chat "Working..." line, the Stop button) painted a
      // status that could be hours old, and a session that had gone idle stayed
      // red until someone reloaded the page. That was the whole bug.
      const si=sessions.findIndex(s=>s.name===st.name);
      if(si>=0){
        sessions[si].activity_status=st.activity_status;
        sessions[si].activity_detail=st.activity_detail||'';
      }
      updateStatusPill(st.name,st.activity_status,st.activity_detail);
      if(st.name===selectedSession)updateFavicon(st.activity_status);
      // Update model badge in nav
      if(si>=0){
        let navChanged=false;''',
)

_SYNC_CHAT_TYPING = '''// The chat view's "Working..." line. Reconciled from whatever status the caller
// has, because it used to be added and removed only by updateCard — which runs
// off a full session refresh, not off the ten-second status poll, so a turn that
// finished left the three bouncing dots on screen indefinitely.
function _syncChatTyping(name,status){
  const chatEl=document.getElementById('chat-'+name);
  if(!chatEl)return;
  const existing=chatEl.querySelector('.chat-typing');
  if(status==='busy'&&!existing){
    const typing=document.createElement('div');
    typing.className='chat-typing';
    typing.innerHTML='<span class="typing-dot-group"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span> Working...';
    chatEl.appendChild(typing);
    chatEl.scrollTop=chatEl.scrollHeight;
  }else if(status!=='busy'&&existing){
    existing.remove();
  }
}
'''
_PILL_TAIL_OLD = '''  toggleInterruptButtons(name,status==='busy');
  const navDot=document.getElementById('nav-dot-'+name);
  if(navDot)navDot.className=_idleNudgeNavDotClass(name,status);
}'''
_PILL_TAIL_NEW = '''  toggleInterruptButtons(name,status==='busy');
  const navDot=document.getElementById('nav-dot-'+name);
  if(navDot)navDot.className=_idleNudgeNavDotClass(name,status);
  _syncChatTyping(name,status);
  // The live strip under the terminal runs its own clock off busy/idle, so it has
  // to move on the poll as well, not only on a full refresh.
  try{if(typeof updateLiveBar==='function')updateLiveBar(name)}catch(e){}
}'''

# The older pill body (builder1, builder3, builder4) writes its own innerHTML;
# the newer one (builder2, builder2a, builder5, claude.lisa.my) delegates to
# _pillInner and records lastDetail. Both get the same tail.
_PILL_OLD_BODY = '''function updateStatusPill(name,status,detail){
  const pill=document.getElementById('status-'+name);
  if(pill){
    // Amber steady = working only to hold the cache. Amber blinking = idle with
    // the cache about to lapse, which is the one that wants you at the keyboard.
    pill.className='status-pill '+(status||'unknown')+_pillCacheClasses(name,status);
    pill.innerHTML='<span class="status-dot"></span><span class="status-label">'+statusLabel(status)+'</span>'
      +(detail&&status!=='busy'?'<span style="font-weight:400;opacity:.7"> &middot; '+esc(detail)+'</span>':'');
  }
'''
_PILL_NEW_BODY = '''function updateStatusPill(name,status,detail){
  lastDetail[name]=detail;
  const pill=document.getElementById('status-'+name);
  if(pill){
    // Amber steady = working only to hold the cache. Amber blinking = idle with
    // the cache about to lapse, which is the one that wants you at the keyboard.
    pill.className='status-pill '+(status||'unknown')+_pillCacheClasses(name,status)
      +(_isCompacting(detail)?' compacting':'');
    pill.innerHTML=_pillInner(status,detail);
  }
'''

patch_any(
    "js: updateStatusPill also reconciles the chat typing line and live strip",
    (_PILL_OLD_BODY + _PILL_TAIL_OLD,
     _SYNC_CHAT_TYPING + _PILL_OLD_BODY + _PILL_TAIL_NEW),
    (_PILL_NEW_BODY + _PILL_TAIL_OLD,
     _SYNC_CHAT_TYPING + _PILL_NEW_BODY + _PILL_TAIL_NEW),
)

patch(
    "js: updateCard defers to the poll's status and stops duplicating the sync",
    '''  updateStatusPill(s.name,s.activity_status,s.activity_detail);
  // Busy/idle drives the live strip's dots and whether its clock is running.
  updateLiveBar(s.name);

  // Update typing indicator
  const chatEl=document.getElementById('chat-'+s.name);
  if(chatEl){
    const existing=chatEl.querySelector('.chat-typing');
    if(s.activity_status==='busy'&&!existing){
      const typing=document.createElement('div');
      typing.className='chat-typing';
      typing.innerHTML='<span class="typing-dot-group"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span> Working...';
      chatEl.appendChild(typing);
      chatEl.scrollTop=chatEl.scrollHeight;
    }else if(s.activity_status!=='busy'&&existing){
      existing.remove();
    }
  }
}''',
    '''  // lastStatus is what the ten-second poll writes, so it is never behind this
  // payload; s.activity_status is only a fallback for a session the poll has not
  // reached yet. Preferring it stops a slow /refresh reply from dragging the pill
  // back to a status the session has already left.
  const fresh=lastStatus[s.name]||s.activity_status;
  // updateStatusPill also reconciles the chat typing line and the live strip.
  updateStatusPill(s.name,fresh,s.activity_detail);
}''',
)

patch(
    "js: render() builds the pill, Stop button and favicon off the live status",
    '''  if(s.messages && s.messages.length) mergeChatMessages(s.name, s.messages);
  // Update favicon to match selected session
  updateFavicon(s.activity_status);
''',
    '''  if(s.messages && s.messages.length) mergeChatMessages(s.name, s.messages);
  // The ten-second poll is the freshest reading there is, so the pill, the chat
  // typing line, the Stop button and the favicon are all built off it. Building
  // them off s.activity_status alone is what made switching to a tab show a red
  // "Working" for a session that finished long ago.
  const liveStatus=lastStatus[s.name]||s.activity_status;
  // Update favicon to match selected session
  updateFavicon(liveStatus);
''',
)

patch_any(
    "js: the header pill markup uses liveStatus",
    ('''        <span class="status-pill ${esc(s.activity_status)}" id="status-${s.name}">
          <span class="status-dot"></span>
          <span class="status-label">${statusLabel(s.activity_status)}</span>
          ${s.activity_detail&&s.activity_status!=='busy'?'<span style="font-weight:400;opacity:.7"> &middot; '+esc(s.activity_detail)+'</span>':''}
        </span>''',
     '''        <span class="status-pill ${esc(liveStatus)}" id="status-${s.name}">
          <span class="status-dot"></span>
          <span class="status-label">${statusLabel(liveStatus)}</span>
          ${s.activity_detail&&liveStatus!=='busy'?'<span style="font-weight:400;opacity:.7"> &middot; '+esc(s.activity_detail)+'</span>':''}
        </span>'''),
    ('''        <span class="status-pill ${esc(s.activity_status)}${_isCompacting(s.activity_detail)?' compacting':''}" id="status-${s.name}">
          ${_pillInner(s.activity_status,s.activity_detail)}
        </span>''',
     '''        <span class="status-pill ${esc(liveStatus)}${_isCompacting(s.activity_detail)?' compacting':''}" id="status-${s.name}">
          ${_pillInner(liveStatus,s.activity_detail)}
        </span>'''),
)

patch(
    "js: the Stop button uses liveStatus",
    '''        <button class="btn btn-stop ${s.activity_status==='busy'?'visible':''}" id="interrupt-hdr-${s.name}"''',
    '''        <button class="btn btn-stop ${liveStatus==='busy'?'visible':''}" id="interrupt-hdr-${s.name}"''',
)

patch(
    "js: the chat typing line uses liveStatus",
    '''          ${renderChatBubbles(s.name)}
          ${s.activity_status==='busy'?'<div class="chat-typing">''',
    '''          ${renderChatBubbles(s.name)}
          ${liveStatus==='busy'?'<div class="chat-typing">''',
)

patch(
    "js: the nav tab dot uses the polled status",
    '''        <span class="${esc(_idleNudgeNavDotClass(s.name,s.activity_status))}" id="nav-dot-${s.name}"></span>''',
    '''        <span class="${esc(_idleNudgeNavDotClass(s.name,lastStatus[s.name]||s.activity_status))}" id="nav-dot-${s.name}"></span>''',
)

patch(
    "js: one status poll in flight at a time",
    '''async function pollStatus(){
  try{
    const resp=await fetch(BASE+'/api/status');
    _checkBuild(resp);''',
    '''// /api/status walks every session's tmux pane, so on a box with a dozen sessions
// it can outlast its own ten-second tick. setInterval does not wait, so the polls
// used to queue behind each other and the whole page's status fell further and
// further behind. One in flight at a time; a skipped tick costs ten seconds, a
// pile-up costs minutes.
let _pollInFlight=false;
async function pollStatus(){
  if(_pollInFlight)return;
  _pollInFlight=true;
  try{await _pollStatusOnce()}finally{_pollInFlight=false}
}
async function _pollStatusOnce(){
  try{
    const resp=await fetch(BASE+'/api/status');
    _checkBuild(resp);''',
)

patch(
    "js: catch up the instant the tab is looked at again",
    '''function startStatusPolling(){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(pollStatus,10000);
}''',
    '''function startStatusPolling(){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(pollStatus,10000);
}

// A hidden tab has its timers throttled by the browser to about once a minute,
// and nothing caught up on return — so coming back to the dashboard showed a
// status up to a minute stale, which is exactly what "it only updates when I
// reload the page" looks like. Poll the moment the tab is looked at again, and
// re-read the usage bars with it.
document.addEventListener('visibilitychange',function(){
  if(document.visibilityState!=='visible')return;
  try{pollStatus()}catch(e){}
  try{refreshUsageLimits()}catch(e){}
});''',
)

patch(
    "comment: the phone strip carries the 7d bar now",
    '''// badge, the 5h/7d usage bars and the CPU/RAM readout are all glance-only, so on
// a narrow screen they move to a strip at the very bottom of the page — below
// the terminal and the upload area.
//
// The ELEMENTS are moved, not cloned. Every poller writes by id
// (#nav-usage-5h-fill, #nav-server-stats, #nav-browser-badge…), so a copy would
// have meant either dead widgets or a second set of updaters to keep in step.''',
    '''// badge, the 7d usage bar and the CPU/RAM readout are all glance-only, so on a
// narrow screen they move to a strip at the very bottom of the page — below the
// terminal and the upload area.
//
// The ELEMENTS are moved, not cloned. Every poller writes by id
// (#nav-usage-7d-fill, #nav-server-stats, #nav-browser-badge…), so a copy would
// have meant either dead widgets or a second set of updaters to keep in step.''',
)


def _check_inline_js(src: str) -> str:
    """'' if the page's inline <script> parses, else the node error.

    The whole UI is one 500KB inline script inside a Python string literal, so a
    stray brace is invisible to `ast.parse` and only shows up as a dashboard that
    renders a blank page. Pull HTML_PAGE out of the AST (no import — importing
    app.py on a live box touches its state) and hand the script to node.
    """
    import ast
    import os
    import re
    import tempfile

    node = shutil.which("node") or shutil.which("nodejs")
    if not node:
        return ""  # nothing to check with; the python gate still applies
    html = None
    for node_ in ast.parse(src).body:
        if isinstance(node_, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "HTML_PAGE" for t in node_.targets):
            if isinstance(node_.value, ast.Constant) and isinstance(node_.value.value, str):
                html = node_.value.value
    if html is None:
        return "HTML_PAGE string literal not found — cannot check the inline JS"
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        return "no inline <script> found in HTML_PAGE"
    tmp = tempfile.mkdtemp()
    for i, body in enumerate(blocks):
        for placeholder, value in (("__ROOT_PATH__", ""), ("__SIMPLE__", "false"),
                                   ("__CACHE_PUSH_LEAD__", "60"),
                                   ("__CACHE_ALERT_LEAD__", "300"),
                                   ("__BRAND__", "test")):
            body = body.replace(placeholder, value)
        p = os.path.join(tmp, "block%d.js" % i)
        with open(p, "w") as fh:
            fh.write(body)
        r = subprocess.run([node, "--check", p], capture_output=True, text=True)
        if r.returncode != 0:
            return r.stderr.strip()[:2000]
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="path to the dashboard app.py")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    path = pathlib.Path(args.target)
    src = path.read_text()
    original = src

    applied, already, missing, skipped = [], [], [], []
    for label, alternatives, only_if, applied_if in PATCHES:
        if only_if and only_if not in src:
            skipped.append(label)
            continue
        if applied_if and applied_if in src:
            already.append(label)
            continue
        if any(new in src for _, new in alternatives):
            already.append(label)
            continue
        hits = [(old, new, src.count(old)) for old, new in alternatives]
        usable = [(old, new) for old, new, n in hits if n == 1]
        if not usable:
            dupes = [n for _, _, n in hits if n > 1]
            missing.append((label, "anchor found %d times, refusing" % dupes[0]
                            if dupes else "anchor not found (%d variant(s) tried)"
                            % len(alternatives)))
            continue
        old, new = usable[0]
        src = src.replace(old, new, 1)
        applied.append(label)

    for label in skipped:
        print("  n/a on this copy        %s" % label)
    for label in already:
        print("  skip (already applied)  %s" % label)
    for label in applied:
        print("  applied                 %s" % label)
    for label, why in missing:
        print("  MISSING                 %s  (%s)" % (label, why))

    if missing:
        print("\n%d patch(es) could not be applied — nothing written." % len(missing))
        return 2
    if src == original:
        print("\nAlready fully patched; nothing to do.")
        return 0
    if args.dry_run:
        print("\n--dry-run: %d patch(es) would be applied." % len(applied))
        return 0

    # Syntax-gate both languages before anything replaces a running file.
    import ast
    try:
        ast.parse(src)
    except SyntaxError as e:
        print("\nPatched source does not parse (%s) — nothing written." % e)
        return 3
    js_err = _check_inline_js(src)
    if js_err:
        print("\nPatched inline JavaScript does not parse — nothing written:\n%s" % js_err)
        return 4

    backup = path.with_suffix(".py.bak-usagestatus-%s" % time.strftime("%Y%m%d%H%M%S"))
    shutil.copy2(path, backup)
    path.write_text(src)
    print("\n%d patch(es) applied. python + inline JS both parse. Backup: %s"
          % (len(applied), backup))
    return 0


if __name__ == "__main__":
    sys.exit(main())
