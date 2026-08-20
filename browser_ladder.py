#!/usr/bin/env python3
"""browser_ladder: get the page, at the cheapest rung that actually works.

The problem this exists to solve: agents on these boxes reached for a full
headed Chromium for everything, because that is the thing that always works,
and it is also the thing that pins two of four cores and 400 MB for a page that
`curl` would have returned in 180 ms. And it still got blocked, because a
datacenter IP running Chrome is not more convincing than a datacenter IP
running curl: the expensive rung is not automatically the effective one.

So: one router, six rungs, cold start, automatic escalation, nothing resident.

    L-1  internal    our own hosts, reached the way we own them
     L0  fetch       HTTP with a real Chrome TLS fingerprint      ~0 MB
     L1  light       happy-dom: a DOM and a JS engine, no pixels  ~50 MB
     L2  chromium    ephemeral headless Chromium, our own IP      ~300 MB
     L3  chromium+dc the same browser, different egress           + bandwidth
     L4  resident    the headed profile browser, residential exit, human takeover

Escalation is driven by what the page actually did, not by a guess made up
front. Each rung's answer is classified: ok / needs_js / challenge / blocked /
rate_limited / auth_required / error, and the classification picks the next
rung, including skipping one: a bot challenge cannot be solved by a DOM
runtime, so `challenge` at L0 jumps straight to L2 rather than wasting L1.

Everything above L1 is launched for one page and killed. Between runs this box
has no Chromium of the ladder's making resident at all, which is the point:
Chrome that exists only while a question is being answered cannot be the thing
that livelocks the machine at 3am.

What the ladder learns it writes down. A host that needed L3 yesterday starts
at L3 today instead of paying for two failures first, and every entry is
re-probed from the bottom once a day so a site that stopped being hostile stops
being expensive.

    browser-ctl get https://example.com            # or: python3 browser_ladder.py get URL
    browser-ctl get URL --max-level 1              # cheap check, never opens a browser
    browser-ctl get URL --level 4 --takeover       # go straight to the watchable browser
    browser-ctl get URL --json                     # the whole run record

The dashboard renders the same run record live at Settings -> Browser -> Ladder,
one panel for every rung, so "which rung is it on and why" is a thing you watch
rather than a thing you reconstruct afterwards.
"""
from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import hmac
import html as html_mod
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

HOME = Path(os.environ.get("CB_HOME") or Path.home())
CB_ROOT = HOME / ".claude-browser"
DASH_DIR = HOME / ".tmux-dashboard"
RUNS_DIR = DASH_DIR / "ladder_runs"
MEMORY_FILE = DASH_DIR / "ladder_memory.json"
CONFIG_FILE = CB_ROOT / "ladder.json"
LOCK_FILE = DASH_DIR / "ladder.lock"
ACTIVITY_FILE = DASH_DIR / "browser_activity.jsonl"
SESSIONS_FILE = DASH_DIR / "browser_sessions.json"
REPO_DIR = Path(__file__).resolve().parent
RUNNER_DIR = CB_ROOT / "bin"
NODE_MODULES = CB_ROOT / "node_modules"

# ---------------------------------------------------------------------------
# The rungs
# ---------------------------------------------------------------------------
INTERNAL, FETCH, LIGHT, CHROMIUM, CHROMIUM_DC, RESIDENT = -1, 0, 1, 2, 3, 4

LEVELS = {
    INTERNAL:    {"key": "internal",    "label": "INTERNAL",    "engine": "http+identity"},
    FETCH:       {"key": "fetch",       "label": "FETCH",       "engine": "curl-impersonate"},
    LIGHT:       {"key": "light",       "label": "LIGHT",       "engine": "happy-dom"},
    CHROMIUM:    {"key": "chromium",    "label": "CHROMIUM",    "engine": "chromium (direct)"},
    CHROMIUM_DC: {"key": "chromium+dc", "label": "DC PROXY",    "engine": "chromium (dc egress)"},
    RESIDENT:    {"key": "resident",    "label": "RESIDENTIAL", "engine": "resident chrome + residential"},
}
LEVEL_ORDER = [INTERNAL, FETCH, LIGHT, CHROMIUM, CHROMIUM_DC, RESIDENT]

# Verdicts. These are the whole interface between "what happened" and "what next".
OK = "ok"                       # we have the page
NEEDS_JS = "needs_js"           # a shell; the content is behind script execution
CHALLENGE = "challenge"         # an interstitial that wants a real browser
BLOCKED = "blocked"             # refused outright; usually the IP, sometimes the client
RATE_LIMITED = "rate_limited"   # too many from this egress
AUTH_REQUIRED = "auth_required" # needs credentials, not a better browser
NOT_FOUND = "not_found"         # 404/410: a better browser will not conjure the page
ERROR = "error"                 # transport failed
SKIPPED = "skipped"             # this rung does not apply (L-1 on someone else's site)
UNAVAILABLE = "unavailable"     # this rung is not installed / not affordable right now

TERMINAL_VERDICTS = {OK, NOT_FOUND}

DEFAULT_CONFIG = {
    # Hosts we own. Suffix-matched, so "grabo.com" covers "api.grabo.com".
    # Seeded from the advisor's domain list; keep it there, not here, when it
    # changes: `mcp__advisor__list_domains` is the source of truth.
    "internal_hosts": [
        "rotem.ai", "rotem.cc", "rotem.ac", "knowva.ai", "grabo.com", "grabo.cc",
        "grabo.tech", "grabo.tools", "grabo.id", "grabo.co.id", "dianao.tech",
        "dianaotech.com", "nemopowertools.com", "nemopower.de", "nemopowertools.cn",
        "nemograbo.com", "nebula-innovations.com", "nebula-bio.com",
        "nebulatoolsupply.com", "alphabell.com", "lisa.my", "iptorch.com",
        "lemad.ai", "phoneline.ai", "read.click", "words.help", "23andclaude.com",
        "aybmart.com", "industrialdictionary.com", "kinnerbuilder.com", "vacdrill.com",
        "zuo-shi-ping.com", "zuos-suction-cup.com", "electric-suction-cups.com",
        "gripster-max.com", "gripster-max.de", "iaoij.com", "d-dart.com",
        "divelight.org", "mlchart.com", "padsbot.com", "inventorbook.org",
        "lifewand-partners.com", "youremailcouldbebetter.com", "notercam.com",
        "testedpowertools.com", "sfbei.com", "gizmomaker.com", "gizmomaker.co.il",
        "cupidbox.co", "nimo.online", "dianao.tech", "advisor.rotem.ai",
    ],
    # host -> loopback port on THIS box. The one true L-1: it does not go past
    # nginx at all, so there is no auth wall and no WAF to look like a bot to.
    # `mcp__advisor__list_apps` has the port for every app we run.
    "internal_ports": {},
    # Signed identity for our own sites that are not on this box. The server side
    # has to check it for this to buy anything; until it does, this is a header
    # our own logs can filter on and nothing more.
    "identity_header": "X-Agent-Fetch",
    "identity_secret_env": "TMUX_DASH_SECRET",
    # L3 egress. WARP is free, is not a GCP range, and is already on the box.
    "dc_proxy": "socks5h://127.0.0.1:25344",
    "dc_proxy_playwright": "socks5://127.0.0.1:25344",
    "warp_bin": str(HOME / "bin" / "wireproxy"),
    "warp_conf": str(HOME / "_warp" / "wireproxy.conf"),
    "warp_port": 25344,
    "warp_idle_stop_s": 900,
    # L4 uses the resident browser with its own residential exit and profile.
    "resident_browser": "default",
    "impersonate": "chrome",
    "timeouts": {"internal": 20, "fetch": 25, "light": 25, "chromium": 60,
                 "chromium+dc": 75, "resident": 90},
    "run_deadline_s": 180,
    "reprobe_after_s": 86400,
    "memory_ttl_s": 2592000,
    "runs_kept": 60,
    "runs_kept_days": 7,
    # Guards. This box livelocks rather than OOM-kills, so a heavy rung that
    # cannot be afforded is refused, not attempted and hoped for.
    "min_free_mem_mb": 900,
    "min_free_mem_pct": 8.0,
    "heavy_lock_wait_s": 90,
}

_CONFIG_CACHE: Dict[str, Any] = {}


def config() -> dict:
    """Defaults, overlaid with ~/.claude-browser/ladder.json if it exists."""
    if _CONFIG_CACHE.get("_loaded"):
        return _CONFIG_CACHE["cfg"]
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    try:
        if CONFIG_FILE.exists():
            user = json.loads(CONFIG_FILE.read_text())
            if isinstance(user, dict):
                for k, v in user.items():
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    elif k == "internal_hosts" and isinstance(v, list):
                        cfg[k] = sorted(set(cfg[k]) | set(v))
                    else:
                        cfg[k] = v
    except Exception:
        pass
    _CONFIG_CACHE["cfg"] = cfg
    _CONFIG_CACHE["_loaded"] = True
    return cfg


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------
def _host_of(url: str) -> str:
    m = re.match(r"[a-zA-Z][a-zA-Z0-9+.-]*://([^/:?#]+)", str(url or ""))
    return (m.group(1) if m else "").lower().rstrip(".")


def _port_alive(port: int, host: str = "127.0.0.1", timeout: float = 0.4) -> bool:
    if not port:
        return False
    try:
        with socket.socket() as s:
            s.settimeout(timeout)
            return s.connect_ex((host, int(port))) == 0
    except Exception:
        return False


def _mem() -> Tuple[float, float]:
    """(available MB, available %). MemAvailable, not MemFree: page cache is
    reclaimable and counting it as used would refuse every heavy rung forever."""
    try:
        info = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            k, _, v = line.partition(":")
            info[k.strip()] = float(v.strip().split()[0])
        avail = info.get("MemAvailable", 0.0) / 1024.0
        total = info.get("MemTotal", 1.0) / 1024.0
        return avail, (avail / total * 100.0 if total else 0.0)
    except Exception:
        return 99999.0, 100.0


def _html_to_text(html: str) -> str:
    """Readable text from HTML. bs4 when it is importable (it is on the
    dashboard's interpreter), a strip-tags fallback when it is not, because this
    module also runs from whichever python3 an agent happens to have."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup  # type: ignore
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "template", "svg"]):
            tag.decompose()
        text = soup.get_text("\n")
    except Exception:
        text = re.sub(r"(?is)<(script|style|noscript|template|svg)\b.*?</\1>", " ", html)
        text = re.sub(r"(?s)<[^>]+>", "\n", text)
        text = html_mod.unescape(text)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _title_of(html: str) -> str:
    m = re.search(r"(?is)<title[^>]*>(.*?)</title>", html or "")
    return html_mod.unescape(m.group(1)).strip()[:300] if m else ""


def _links_of(html: str, base: str = "") -> List[dict]:
    out = []
    for m in re.finditer(r'(?is)<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', html or ""):
        href = html_mod.unescape(m.group(1)).strip()
        if not href or href.startswith(("javascript:", "#")):
            continue
        text = re.sub(r"(?s)<[^>]+>", " ", m.group(2))
        out.append({"href": href[:400], "text": html_mod.unescape(text).strip()[:120]})
        if len(out) >= 300:
            break
    return out


def _activity(event: str, **extra) -> None:
    """One line in the same trail the Browser tab already renders, so a ladder
    run and a human's browsing show up in one history rather than two."""
    row = {"ts": time.time(), "sid": extra.pop("sid", "ladder"), "event": event}
    row.update({k: v for k, v in extra.items() if v not in (None, "")})
    try:
        DASH_DIR.mkdir(parents=True, exist_ok=True)
        with ACTIVITY_FILE.open("a") as fh:
            fh.write(json.dumps(row, separators=(",", ":")) + "\n")
    except Exception:
        pass


# ---------------------------------------------------------------------------
# What happened, in one word
# ---------------------------------------------------------------------------
# Two tiers, and the difference between them is the whole reason this works.
#
# STRONG markers only ever appear on the interstitial itself. WEAK markers are
# the vendor's ordinary client script, which a protected site serves on EVERY
# page whether or not it is blocking you: so treating them as proof reads
# "similarweb.com loaded fine, and it uses DataDome" as "similarweb.com is
# challenging us", and sends a page that was already in hand all the way up to
# a residential browser. A weak marker counts only when the response also looks
# like a block: a blocking status, or a page with no content in it.
STRONG_CHALLENGE = [
    (r"cf[-_]chl_opt|__cf_chl|cf_chl_", "cloudflare"),
    (r"<title>\s*just a moment", "cloudflare"),
    (r"checking (?:if )?(?:the security of )?your (?:browser|connection)", "cloudflare"),
    (r"enable javascript and cookies to continue", "cloudflare"),
    (r"attention required!\s*\|\s*cloudflare", "cloudflare"),
    (r"incapsula incident id", "imperva"),
    (r"geo\.captcha-delivery\.com", "datadome"),
    (r"px-captcha|/px/captcha", "perimeterx"),
    (r"reference\s*#[0-9a-f.]{10,}[\s\S]{0,3000}access denied|"
     r"access denied[\s\S]{0,3000}reference\s*#[0-9a-f.]{10,}", "akamai"),
    (r"verify you are (?:a )?human|please verify you are human", "captcha-wall"),
]

WEAK_CHALLENGE = [
    (r"datadome", "datadome"),
    (r"awswaf|challenge\.js\?awswaf", "awswaf"),
    (r"perimeterx|_pxhd", "perimeterx"),
    (r"kpsdk", "kasada"),
    (r"_incapsula_resource", "imperva"),
    (r"cf-turnstile|challenges\.cloudflare\.com/turnstile", "cloudflare"),
    (r"g-recaptcha|h-captcha|hcaptcha\.com/captcha", "captcha-widget"),
    (r"akamaighost", "akamai"),
]

# Statuses at which a weak marker is worth believing.
BLOCKING_STATUSES = {401, 403, 405, 406, 429, 503}

# A page that is only a mount point. Not proof on its own: it has to come with
# almost no text: but each of these plus an empty body is an SPA shell.
SHELL_MARKERS = [
    r'id=["\']root["\']', r'id=["\']app["\']', r'id=["\']__next["\']',
    r"__NEXT_DATA__", r"data-reactroot", r"ng-app|ng-version", r"__NUXT__",
    r"window\.__INITIAL_STATE__", r"<div[^>]+data-server-rendered",
]

NOSCRIPT_NAG = re.compile(
    r"(?is)<noscript[^>]*>.{0,400}?(enable|turn on|requires?)\s+javascript", re.I)

HTMLISH = re.compile(r"text/html|application/xhtml", re.I)


def detect_challenge(body: str, headers: Dict[str, str], status: int = 0,
                     text_len: Optional[int] = None) -> Optional[str]:
    """Which bot wall is standing in the way, or None.

    `status` and `text_len` are what keep the weak tier honest: a DataDome
    script on a 200 with 40 kB of article text is a site that uses DataDome, not
    a site refusing us."""
    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    if "challenge" in h.get("cf-mitigated", "").lower():
        return "cloudflare"

    low = (body or "")[:300_000].lower()
    if not low:
        return None
    for pattern, name in STRONG_CHALLENGE:
        if re.search(pattern, low):
            return name

    if text_len is None:
        text_len = len(_html_to_text(body))
    looks_blocked = (status in BLOCKING_STATUSES) or text_len < 400
    if not looks_blocked:
        return None
    for pattern, name in WEAK_CHALLENGE:
        if re.search(pattern, low):
            return name
    return None


def classify(status: int, headers: Dict[str, str], body: str, text: str,
             content_type: str, level: int, error: str = "") -> Tuple[str, str]:
    """One verdict plus the sentence a human needs to agree with it."""
    if error:
        low = error.lower()
        if "timed out" in low or "timeout" in low:
            return ERROR, "the request timed out"
        if "certificate" in low or "ssl" in low:
            return ERROR, "TLS failed: " + error[:120]
        if "resolve" in low or "dns" in low or "name or service" in low:
            return ERROR, "the host does not resolve"
        return ERROR, error[:180]

    h = {str(k).lower(): str(v) for k, v in (headers or {}).items()}
    body = body or ""
    if text is None:
        text = _html_to_text(body)
    wall = detect_challenge(body, h, status, len((text or "").strip()))

    if status in (401,) or (status == 403 and h.get("www-authenticate")):
        return AUTH_REQUIRED, "the site wants credentials, not a different browser"
    if status == 429 or (status == 503 and h.get("retry-after") and not wall):
        return RATE_LIMITED, "too many requests from this egress (%s)" % status
    if wall and status in (0, 200, 202, 403, 429, 503, 401, 400, 406):
        return CHALLENGE, "%s interstitial, not the page" % wall
    if status in (404, 410):
        return NOT_FOUND, "the page is not there (%s): no rung fixes that" % status
    if status == 451:
        return BLOCKED, "blocked for legal reasons (451)"
    if status in (403, 406):
        return BLOCKED, "refused outright (%s) with no challenge to solve" % status
    if status and status >= 500:
        return ERROR, "the site returned %s" % status
    if status and not (200 <= status < 400):
        return ERROR, "unexpected status %s" % status

    # 2xx from here on. Is there actually a page in it?
    if content_type and not HTMLISH.search(content_type):
        return OK, "%s, taken as-is" % (content_type.split(";")[0] or "binary")

    n = len(text.strip())

    if level >= LIGHT:
        # Scripts have already run at these rungs, so a still-empty page is
        # empty, not pending: escalating for more JS would be superstition.
        if n < 40 and not re.search(r"(?is)<(img|video|canvas|svg|table|form)\b", body):
            return NEEDS_JS if level == LIGHT else ERROR, \
                "rendered, but the document has no content (%d chars)" % n
        return OK, "rendered (%d chars of text)" % n

    if n < 500:
        if NOSCRIPT_NAG.search(body):
            return NEEDS_JS, "the page says outright that it needs JavaScript"
        for pattern in SHELL_MARKERS:
            if re.search(pattern, body, re.I):
                return NEEDS_JS, "an app shell with %d chars of text: the content is in JS" % n
        if n < 40 and len(body) < 4000 and "<script" in body.lower():
            return NEEDS_JS, "almost nothing but script tags (%d chars of text)" % n
        if n == 0:
            return NEEDS_JS, "empty document body"
    return OK, "%d chars of text" % n


def next_level(level: int, verdict: str, max_level: int, tried: List[int]) -> Optional[int]:
    """Which rung answers this failure. Returning None ends the run.

    The jumps matter as much as the steps. A bot wall at L0 does not go to L1:
    a DOM runtime has no TLS handshake and no browser fingerprint to offer, so
    it fails the same way for 50 MB and a second. Equally, a rate limit is a
    problem with the *egress*, so it goes to the rung that changes the egress
    rather than climbing one engine at a time.
    """
    if verdict in TERMINAL_VERDICTS:
        return None

    if verdict == SKIPPED:
        target = FETCH
    elif verdict == NEEDS_JS:
        target = LIGHT if level < LIGHT else max(level + 1, CHROMIUM)
    elif verdict == CHALLENGE:
        target = CHROMIUM if level < CHROMIUM else level + 1
    elif verdict == BLOCKED:
        # The client already looks like Chrome at L0. If that is refused, one
        # real browser is worth a try (some walls set a cookie from JS), and
        # after that it is the address, not the client.
        target = CHROMIUM if level < CHROMIUM else level + 1
    elif verdict == RATE_LIMITED:
        target = CHROMIUM_DC if level < CHROMIUM_DC else RESIDENT
    elif verdict == AUTH_REQUIRED:
        # Worth exactly one real browser: a login wall sometimes reads as 401
        # only to a client that never ran the session-cookie script.
        target = CHROMIUM if level < CHROMIUM else None
    elif verdict == UNAVAILABLE:
        target = level + 1
    elif verdict == ERROR:
        target = CHROMIUM if level < CHROMIUM else level + 1
    else:
        target = level + 1

    if target is None:
        return None
    while target in tried and target <= max_level:
        target += 1
    if target > max_level or target not in LEVELS:
        return None
    return target


# ---------------------------------------------------------------------------
# What the ladder remembers
# ---------------------------------------------------------------------------
class Memory:
    """Per-host: the rung that worked, so the next cold start does not pay for
    the same two failures. Re-probed from the bottom once a day, because a site
    that has stopped fighting should stop costing a browser."""

    def __init__(self, path: Path = MEMORY_FILE):
        self.path = path

    def _read(self) -> dict:
        try:
            d = json.loads(self.path.read_text())
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _write(self, data: dict) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=1, sort_keys=True))
            tmp.replace(self.path)
        except Exception:
            pass

    def get(self, host: str) -> dict:
        return self._read().get(host.lower(), {}) if host else {}

    def start_level(self, host: str) -> Tuple[int, str]:
        """(rung to start at, why). INTERNAL when we know nothing: it costs one
        suffix comparison and falls through to L0 if the host is not ours."""
        cfg = config()
        row = self.get(host)
        if not row:
            return INTERNAL, ""
        age = time.time() - float(row.get("ts") or 0)
        if age > float(cfg["memory_ttl_s"]):
            return INTERNAL, ""
        lvl = int(row.get("level", INTERNAL))
        if age > float(cfg["reprobe_after_s"]):
            return INTERNAL, "re-probing from the bottom (last learned %s ago)" % _ago(age)
        if lvl <= FETCH:
            return INTERNAL, ""
        return lvl, "%s needed %s %s ago" % (host, LEVELS[lvl]["label"], _ago(age))

    def learn(self, host: str, level: int, verdict: str, why: str = "") -> None:
        if not host:
            return
        data = self._read()
        prev = data.get(host.lower(), {})
        data[host.lower()] = {
            "level": int(level),
            "verdict": verdict,
            "why": why[:200],
            "ts": time.time(),
            "hits": int(prev.get("hits") or 0) + 1,
            "prev_level": prev.get("level"),
        }
        # Bound it. A memory file nobody prunes is a memory file that eventually
        # costs more to read than the lookup saves.
        if len(data) > 4000:
            rows = sorted(data.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True)
            data = dict(rows[:3000])
        self._write(data)

    def forget(self, host: str = "") -> int:
        data = self._read()
        if not host:
            n = len(data)
            self._write({})
            return n
        return 1 if data.pop(host.lower(), None) is not None and (self._write(data) or True) else 0


def _ago(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 90:
        return "%ds" % int(seconds)
    if seconds < 5400:
        return "%dm" % int(seconds / 60)
    if seconds < 172800:
        return "%dh" % int(seconds / 3600)
    return "%dd" % int(seconds / 86400)


# ---------------------------------------------------------------------------
# Egress: the datacenter rung's proxy, and what any rung looks like from outside
# ---------------------------------------------------------------------------
_EGRESS_CACHE: Dict[str, Tuple[float, dict]] = {}


def egress_info(proxy: str = "", ttl: float = 600.0) -> dict:
    """{ip, org, country, datacenter} for a route. Cached: it is one more request
    on a residential link that bills by the gigabyte."""
    key = proxy or "direct"
    hit = _EGRESS_CACHE.get(key)
    if hit and time.time() - hit[0] < ttl:
        return hit[1]
    info: Dict[str, Any] = {"proxy": proxy or "", "route": "direct" if not proxy else "proxy"}
    try:
        sess = _fetch_session()
        kw: Dict[str, Any] = {"timeout": 12}
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        r = sess.get("https://ipinfo.io/json", **kw)
        d = r.json()
        org = str(d.get("org") or "")
        info.update({
            "ip": d.get("ip"), "org": org, "country": d.get("country"),
            "city": d.get("city"), "timezone": d.get("timezone"),
            "datacenter": bool(re.search(
                r"google|amazon|aws|microsoft|azure|digitalocean|linode|hetzner|ovh|"
                r"oracle|vultr|contabo|leaseweb|choopa|m247|datacamp", org, re.I)),
        })
    except Exception as exc:
        info["error"] = str(exc)[:160]
    _EGRESS_CACHE[key] = (time.time(), info)
    return info


def warp_running() -> bool:
    return _port_alive(int(config()["warp_port"]))


def warp_start(wait_s: float = 12.0) -> bool:
    """Bring the L3 egress up. WARP is free, is not in a GCP range, and is
    already installed here: which is the whole reason L3 is affordable enough
    to sit below the residential rung instead of beside it."""
    cfg = config()
    if warp_running():
        return True
    binary, conf = cfg["warp_bin"], cfg["warp_conf"]
    if not (Path(binary).exists() and Path(conf).exists()):
        return False
    # A config with no [Interface] is a half-finished install, not an egress.
    # Without this check wireproxy is spawned, fails, and the rung burns the
    # full 12s start timeout on every single L3 attempt before giving up.
    try:
        if "[Interface]" not in Path(conf).read_text() or "PrivateKey" not in Path(conf).read_text():
            return False
    except Exception:
        return False
    try:
        logf = open(CB_ROOT / "logs" / "warp.log", "ab", buffering=0)
    except Exception:
        logf = subprocess.DEVNULL
    try:
        subprocess.Popen([binary, "-c", conf], stdout=logf, stderr=logf,
                         stdin=subprocess.DEVNULL, start_new_session=True)
    except Exception:
        return False
    deadline = time.time() + wait_s
    while time.time() < deadline:
        if warp_running():
            _activity("L3 egress up (WARP)", sid="ladder")
            return True
        time.sleep(0.4)
    return False


def warp_stop() -> bool:
    """Kill it by the port it holds. Never `pkill wireproxy`: this box has had
    unrelated agent sessions killed by a broad pattern match before."""
    port = int(config()["warp_port"])
    if not _port_alive(port):
        return False
    try:
        out = subprocess.run(["lsof", "-ti", "tcp:%d" % port], capture_output=True,
                             text=True, timeout=10).stdout.split()
        for pid in out:
            with contextlib.suppress(Exception):
                os.kill(int(pid), 15)
        _activity("L3 egress down (WARP)", sid="ladder")
        return True
    except Exception:
        return False


def warp_reap_if_idle() -> None:
    """Stop the L3 egress when no run has needed it for a while. It is only
    ~15 MB, but 'leave nothing running' is the rule this whole module exists to
    keep, and an exception for the cheap thing is how the rule dies."""
    cfg = config()
    if not warp_running():
        return
    try:
        mark = DASH_DIR / "ladder_warp_used"
        last = mark.stat().st_mtime if mark.exists() else 0
        if time.time() - last > float(cfg["warp_idle_stop_s"]):
            warp_stop()
    except Exception:
        pass


def _warp_touch() -> None:
    with contextlib.suppress(Exception):
        DASH_DIR.mkdir(parents=True, exist_ok=True)
        (DASH_DIR / "ladder_warp_used").touch()


# ---------------------------------------------------------------------------
# Rung engines
# ---------------------------------------------------------------------------
_SESSION = {"s": None}

CHROME_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
    "Accept-Language": "en-US,en;q=0.9",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
}


def _fetch_session():
    """curl_cffi if it is importable, requests if it is not.

    This matters more than it looks. Plain requests/urllib present a Python TLS
    handshake, and Akamai and Cloudflare refuse that on fingerprint alone , 
    which is how a site that a browser reads fine came to be recorded here as
    'needs a browser'. curl_cffi replays Chrome's actual ClientHello, so L0 is
    refused for real reasons or not at all."""
    if _SESSION["s"] is not None:
        return _SESSION["s"]
    try:
        from curl_cffi import requests as cffi_requests  # type: ignore
        _SESSION["s"] = cffi_requests.Session(impersonate=config()["impersonate"])
        _SESSION["kind"] = "curl_cffi"
    except Exception:
        import requests  # type: ignore
        s = requests.Session()
        s.headers.update(dict(CHROME_HEADERS, **{
            "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36"}))
        _SESSION["s"] = s
        _SESSION["kind"] = "requests"
    return _SESSION["s"]


def fetch_kind() -> str:
    _fetch_session()
    return _SESSION.get("kind", "unknown")


def is_internal(host: str) -> Tuple[bool, str]:
    """Ours, or on our own network? Suffix match against the owned-domain list,
    plus anything that resolves inside the VPC or to this box."""
    if not host:
        return False, ""
    h = host.lower()
    for own in config()["internal_hosts"]:
        own = str(own).lower().lstrip(".")
        if h == own or h.endswith("." + own):
            return True, "%s is ours" % own
    try:
        ip = ipaddress.ip_address(socket.gethostbyname(h))
        if ip.is_loopback or ip.is_private:
            return True, "%s resolves to %s, inside our network" % (h, ip)
    except Exception:
        pass
    return False, ""


_NGINX_CACHE: Dict[str, Any] = {"mtime": 0.0, "routes": []}
_NGINX_DIRS = ["/etc/nginx/sites-enabled", "/etc/nginx/conf.d"]


def nginx_routes() -> List[dict]:
    """Every `server_name` + `location` on this box that proxies to a local port.

    This is what makes L-1 more than a header. If a URL we are asked for is
    served by nginx on this very machine, there is a loopback port behind it,
    and going straight there skips nginx, its Basic auth, its allowlist and any
    WAF in front of it. No credential, no fingerprint, no proxy: for our own
    sites the whole ladder collapses to one local request.

    The parse is deliberately shallow: brace depth, server_name, location,
    proxy_pass: because a full nginx grammar is not worth it and a route we
    fail to spot costs nothing but a fall through to L0."""
    newest = 0.0
    files = []
    for d in _NGINX_DIRS:
        p = Path(d)
        if not p.is_dir():
            continue
        for f in sorted(p.iterdir()):
            try:
                if f.is_file():
                    files.append(f)
                    newest = max(newest, f.stat().st_mtime)
            except Exception:
                continue
    if not files:
        return []
    if _NGINX_CACHE["routes"] and abs(_NGINX_CACHE["mtime"] - newest) < 0.5:
        return _NGINX_CACHE["routes"]

    routes: List[dict] = []
    loc_re = re.compile(r"^\s*location\s+(?:(=|\^~|~\*?)\s+)?(\S+)\s*\{")
    pass_re = re.compile(r"^\s*proxy_pass\s+(https?://[^;\s]+)\s*;")
    name_re = re.compile(r"^\s*server_name\s+([^;]+);")
    for f in files:
        try:
            lines = f.read_text(errors="replace").splitlines()
        except Exception:
            continue
        depth = 0
        server_depth = -1
        names: List[str] = []
        loc_stack: List[Tuple[int, str, str]] = []   # (depth, modifier, prefix)
        for line in lines:
            if line.lstrip().startswith("#"):
                continue
            m = loc_re.match(line)
            opened_loc = None
            if m and server_depth >= 0:
                opened_loc = (depth + 1, m.group(1) or "", m.group(2))
            elif re.match(r"^\s*server\s*\{", line):
                server_depth = depth + 1
                names = []
            m = name_re.match(line)
            if m and server_depth >= 0:
                names = [n.strip().lower() for n in m.group(1).split() if n.strip()]
            m = pass_re.match(line)
            if m and names and loc_stack:
                target = m.group(1)
                pm = re.match(r"https?://(?:127\.0\.0\.1|localhost|0\.0\.0\.0)"
                              r":(\d+)(/.*)?$", target)
                if pm:
                    _, mod, prefix = loc_stack[-1]
                    routes.append({
                        "names": names, "prefix": prefix, "modifier": mod,
                        "port": int(pm.group(1)),
                        # A proxy_pass with a path REPLACES the matched prefix;
                        # without one the original URI is passed through. Getting
                        # this backwards is a 404 that looks like the app is down.
                        "upstream_path": pm.group(2) or "",
                        "file": f.name,
                    })
            depth += line.count("{") - line.count("}")
            if opened_loc:
                loc_stack.append(opened_loc)
            while loc_stack and depth < loc_stack[-1][0]:
                loc_stack.pop()
            if server_depth >= 0 and depth < server_depth:
                server_depth = -1
                names = []
    _NGINX_CACHE.update(mtime=newest, routes=routes)
    return routes


def local_route(host: str, path: str) -> Optional[dict]:
    """The loopback port serving this exact URL on this box, if there is one.

    Longest matching location prefix wins, which is nginx's own rule for prefix
    locations and close enough for ours."""
    host = (host or "").lower()
    if not host:
        return None
    best = None
    for r in nginx_routes():
        if not any(host == n or (n.startswith("*.") and host.endswith(n[1:])) or n == "_"
                   for n in r["names"]):
            continue
        if r["modifier"].startswith("~"):
            continue     # regex locations: not worth guessing at
        prefix = r["prefix"]
        if r["modifier"] == "=" and path != prefix:
            continue
        if not path.startswith(prefix) and prefix != "/":
            continue
        if best is None or len(prefix) > len(best["prefix"]):
            best = r
    if not best:
        return None
    if best["upstream_path"]:
        rest = path[len(best["prefix"]):]
        target_path = best["upstream_path"].rstrip("/") + "/" + rest.lstrip("/")
    else:
        target_path = path
    return {"port": best["port"], "path": target_path or "/",
            "prefix": best["prefix"], "file": best["file"]}


def _identity_headers(method: str, path: str) -> Dict[str, str]:
    cfg = config()
    secret = os.environ.get(cfg["identity_secret_env"]) or ""
    hdr = {cfg["identity_header"]: "1", "X-Agent-Host": socket.gethostname()}
    if secret:
        ts = str(int(time.time()))
        msg = ("%s\n%s\n%s" % (ts, method.upper(), path)).encode()
        sig = hmac.new(secret.encode(), msg, hashlib.sha256).hexdigest()
        hdr["X-Agent-Ts"] = ts
        hdr["X-Agent-Sig"] = sig
    return hdr


def _run_node(script: Path, payload: dict, timeout: float, sandbox_env: bool = False,
              heap_mb: int = 0) -> dict:
    """Run one of the node rungs and bring back its JSON.

    `sandbox_env` strips the environment down to nothing worth stealing before
    handing the process a page's own JavaScript. There are API keys in this
    process's env; a hostile page's script should not be one `process.env` away
    from them."""
    if not script.exists():
        return {"ok": False, "why": "%s is not installed" % script.name}
    env = {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(CB_ROOT / "run") if sandbox_env else str(HOME),
        "NODE_PATH": str(NODE_MODULES),
        "NO_COLOR": "1",
    }
    if not sandbox_env:
        for k in ("DISPLAY", "TZ", "LANG", "LC_ALL"):
            if os.environ.get(k):
                env[k] = os.environ[k]
    cmd = ["node"]
    if heap_mb:
        cmd.append("--max-old-space-size=%d" % heap_mb)
    cmd.append(str(script))
    try:
        proc = subprocess.run(
            cmd, input=json.dumps(payload), capture_output=True, text=True,
            timeout=timeout, env=env, cwd=str(script.parent))
    except subprocess.TimeoutExpired:
        return {"ok": False, "why": "the %s rung did not answer in %ds" % (script.stem, timeout),
                "timeout": True}
    except FileNotFoundError:
        return {"ok": False, "why": "node is not on PATH"}
    out = (proc.stdout or "").strip()
    if not out:
        err = (proc.stderr or "").strip().splitlines()
        return {"ok": False, "why": (err[-1][:300] if err else "no output from %s" % script.name)}
    try:
        return json.loads(out)
    except Exception:
        return {"ok": False, "why": "unreadable output from %s" % script.name,
                "raw": out[:400]}


def ensure_runners() -> Dict[str, Path]:
    """Deploy the node rungs next to their node_modules.

    They are versioned here, in the repo, and copied into ~/.claude-browser/bin
    when they differ: the same write-if-changed shape the browser launcher
    already uses. ESM resolves modules by walking up from the *script's* own
    directory, so a runner left in the repo could never find happy-dom or
    playwright-core no matter what NODE_PATH said."""
    out = {}
    RUNNER_DIR.mkdir(parents=True, exist_ok=True)
    for src_name, dst_name in (("browser_ladder_light.mjs", "ladder-light.mjs"),
                               ("browser_ladder_chromium.mjs", "ladder-chromium.mjs")):
        src, dst = REPO_DIR / src_name, RUNNER_DIR / dst_name
        try:
            if src.exists() and (not dst.exists() or dst.read_text() != src.read_text()):
                shutil.copyfile(str(src), str(dst))
                dst.chmod(0o755)
        except Exception:
            pass
        out[dst_name] = dst
    return out


@contextlib.contextmanager
def heavy_lock(wait_s: float):
    """One heavy browser at a time, across every agent on the box.

    Two Playwright runs at once is how this machine got to 43 orphaned Chrome
    processes and 4.6 GB. The lock is advisory and file-based so it holds across
    processes, not just across threads in the dashboard."""
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+")
    deadline = time.time() + max(0.0, wait_s)
    got = False
    try:
        while True:
            try:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() >= deadline:
                    break
                time.sleep(0.5)
        yield got
    finally:
        if got:
            with contextlib.suppress(Exception):
                fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def afford_heavy() -> Tuple[bool, str]:
    cfg = config()
    mb, pct = _mem()
    if mb < float(cfg["min_free_mem_mb"]) or pct < float(cfg["min_free_mem_pct"]):
        return False, ("only %.0f MB (%.1f%%) of memory left: a Chromium here is how "
                       "this box livelocks, so the rung is refused, not risked" % (mb, pct))
    return True, ""


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------
class Run:
    """One question, and every rung it took to answer it."""

    def __init__(self, url: str, run_id: str = "", agent: str = "", note: str = "",
                 max_level: int = RESIDENT, only_level: Optional[int] = None,
                 start_level: Optional[int] = None, use_memory: bool = True,
                 takeover: bool = False, deadline_s: Optional[float] = None,
                 on_update: Optional[Callable[[dict], None]] = None):
        self.cfg = config()
        self.url = url
        self.host = _host_of(url)
        self.id = run_id or (time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        self.dir = RUNS_DIR / self.id
        self.agent = agent
        self.note = note
        self.max_level = int(max_level)
        self.only_level = only_level
        self.start_level = start_level
        self.use_memory = use_memory
        self.takeover = takeover
        self.deadline = time.time() + float(deadline_s or self.cfg["run_deadline_s"])
        self.on_update = on_update
        self.memory = Memory()
        self.steps: List[dict] = []
        self.started = time.time()
        self.finished: Optional[float] = None
        self.result: dict = {}
        self.content: dict = {}

    # -- persistence --------------------------------------------------------
    def record(self) -> dict:
        return {
            "id": self.id, "url": self.url, "host": self.host,
            "agent": self.agent, "note": self.note,
            "started": self.started, "finished": self.finished,
            "ms": int(((self.finished or time.time()) - self.started) * 1000),
            "running": self.finished is None,
            "max_level": self.max_level, "only_level": self.only_level,
            "steps": self.steps,
            "result": self.result,
            "fetch_engine": fetch_kind(),
        }

    def save(self) -> None:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            tmp = self.dir / "run.json.tmp"
            tmp.write_text(json.dumps(self.record(), indent=1, default=str))
            tmp.replace(self.dir / "run.json")
        except Exception:
            pass
        if self.on_update:
            with contextlib.suppress(Exception):
                self.on_update(self.record())

    def artifact(self, name: str, data, binary: bool = False) -> str:
        try:
            self.dir.mkdir(parents=True, exist_ok=True)
            p = self.dir / name
            if binary:
                p.write_bytes(data)
            else:
                p.write_text(data if isinstance(data, str) else json.dumps(data, default=str))
            return name
        except Exception:
            return ""

    # -- the ladder ---------------------------------------------------------
    def go(self) -> dict:
        _activity("ladder run started", sid="ladder", url=self.url,
                  agents=[self.agent] if self.agent else [], run=self.id)

        if self.only_level is not None:
            level: Optional[int] = self.only_level
            why_start = "asked for %s and nothing else" % LEVELS[self.only_level]["label"]
        elif self.start_level is not None:
            level, why_start = self.start_level, "asked to start at %s" % LEVELS[self.start_level]["label"]
        elif self.use_memory:
            level, why_start = self.memory.start_level(self.host)
        else:
            level, why_start = INTERNAL, ""
        if why_start:
            self.result["start_why"] = why_start
        if self.only_level is None and level > self.max_level:
            level = self.max_level
        self.save()

        tried: List[int] = []
        last: dict = {}
        while level is not None:
            if time.time() > self.deadline:
                last = {"verdict": ERROR, "why": "the run's overall deadline passed"}
                break
            tried.append(level)
            step = self._run_level(level)
            self.steps.append(step)
            self.save()
            last = step
            if step["verdict"] == OK:
                break
            if self.only_level is not None:
                break
            level = next_level(level, step["verdict"], self.max_level, tried)

        self.finished = time.time()
        won = next((s for s in self.steps if s["verdict"] == OK), None)
        self.result.update({
            "ok": bool(won),
            "verdict": (won or last).get("verdict", ERROR),
            "why": (won or last).get("why", ""),
            "level": (won or last).get("level"),
            "level_label": LEVELS.get((won or last).get("level", 0), {}).get("label", ""),
            "status": (won or last).get("status"),
            "final_url": (won or last).get("final_url") or self.url,
            "title": (won or last).get("title", ""),
            "levels_tried": tried,
            "ms": int((self.finished - self.started) * 1000),
        })
        if won:
            self.memory.learn(self.host, won["level"], OK, won.get("why", ""))
        elif self.steps:
            # Remember the ceiling too: a host that failed every rung should not
            # start at L-1 tomorrow and pay for the whole climb again.
            self.memory.learn(self.host, max(tried), last.get("verdict", ERROR),
                              last.get("why", ""))
        self.save()
        _activity("ladder run %s at %s" % ("succeeded" if won else "failed",
                                           self.result.get("level_label") or "?"),
                  sid="ladder", url=self.url, run=self.id,
                  reason=self.result.get("why", ""))
        prune_runs()
        warp_reap_if_idle()
        return self.record()

    def _step(self, level: int, **kw) -> dict:
        step = {"level": level, "key": LEVELS[level]["key"], "label": LEVELS[level]["label"],
                "engine": LEVELS[level]["engine"], "started": time.time(), "ms": 0,
                "status": 0, "verdict": ERROR, "why": "", "bytes": 0, "artifacts": {}}
        step.update(kw)
        return step

    def _run_level(self, level: int) -> dict:
        fn = {INTERNAL: self._l_internal, FETCH: self._l_fetch, LIGHT: self._l_light,
              CHROMIUM: self._l_chromium, CHROMIUM_DC: self._l_chromium_dc,
              RESIDENT: self._l_resident}[level]
        t0 = time.time()
        try:
            step = fn()
        except Exception as exc:  # a rung must never take the run down with it
            step = self._step(level, verdict=ERROR, why="%s: %s" % (type(exc).__name__, exc)[:200])
        step["ms"] = int((time.time() - t0) * 1000)
        step.setdefault("started", t0)
        return step

    # -- L-1 ---------------------------------------------------------------
    def _l_internal(self) -> dict:
        step = self._step(INTERNAL)
        ours, why = is_internal(self.host)
        if not ours:
            step.update(verdict=SKIPPED, why="not one of ours: nothing to sign in as")
            return step
        cfg = self.cfg
        path = re.sub(r"^[a-z]+://[^/]+", "", self.url, flags=re.I) or "/"
        headers = dict(CHROME_HEADERS)
        headers.update(_identity_headers("GET", path))
        target = self.url
        route = "signed identity to the public address"
        # Hand-configured map first (an app on this box that nginx does not
        # front), then whatever nginx is actually serving here.
        port, upstream_path = 0, path
        for k, v in (cfg.get("internal_ports") or {}).items():
            k = str(k).lower()
            if self.host == k or self.host.endswith("." + k):
                port = int(v)
                break
        if not port:
            hit = local_route(self.host, path)
            if hit:
                port, upstream_path = hit["port"], hit["path"]
                route = "loopback :%d via %s (%s)" % (port, hit["prefix"], hit["file"])
        if port and _port_alive(port):
            # The real prize: straight to the app on loopback. No nginx, no auth
            # wall, no WAF with an opinion about whether we look like a browser.
            target = "http://127.0.0.1:%d%s" % (port, upstream_path)
            headers["Host"] = self.host
            if route.startswith("signed"):
                route = "loopback :%d, behind nginx entirely" % port
        elif port:
            route = "loopback :%d is configured but nothing is listening" % port
            port = 0
        step["route"] = route
        step["why_internal"] = why
        got = self._http_get(target, headers, timeout=cfg["timeouts"]["internal"])
        if got.get("error"):
            step.update(verdict=SKIPPED, why="internal route failed (%s): trying it as an outsider"
                        % got["error"][:80])
            return step
        verdict, vwhy = classify(got["status"], got["headers"], got["body"], got["text"],
                                 got["content_type"], INTERNAL)
        if verdict in (AUTH_REQUIRED, BLOCKED):
            step.update(verdict=SKIPPED, status=got["status"],
                        why="ours, but it did not accept the agent identity (%s), "
                            "add the check server-side, or it is just L0 with a header"
                            % got["status"])
            return step
        self._absorb(step, got, INTERNAL, verdict, vwhy)
        return step

    # -- L0 ----------------------------------------------------------------
    def _l_fetch(self) -> dict:
        step = self._step(FETCH, engine=fetch_kind())
        got = self._http_get(self.url, dict(CHROME_HEADERS),
                             timeout=self.cfg["timeouts"]["fetch"])
        if got.get("error"):
            step.update(verdict=ERROR, why=got["error"][:200])
            return step
        verdict, vwhy = classify(got["status"], got["headers"], got["body"], got["text"],
                                 got["content_type"], FETCH)
        self._absorb(step, got, FETCH, verdict, vwhy)
        step["egress"] = egress_info("")
        return step

    def _http_get(self, url: str, extra_headers: dict, timeout: float, proxy: str = "") -> dict:
        """One HTTP request, with whatever fingerprint the session has.

        `extra_headers` is *extra* deliberately. curl_cffi's impersonation is not
        just a User-Agent: it is the header set and its order, matched to the
        ClientHello. Re-sending our own copy of Accept/Sec-Fetch-* would reorder
        them and undo the thing the rung is for. Only headers a browser would
        not have sent by itself belong here."""
        sess = _fetch_session()
        kw: Dict[str, Any] = {"timeout": timeout, "allow_redirects": True}
        if extra_headers:
            kw["headers"] = ({k: v for k, v in extra_headers.items()
                              if k not in CHROME_HEADERS}
                             if _SESSION.get("kind") == "curl_cffi" else dict(extra_headers))
        if proxy:
            kw["proxies"] = {"http": proxy, "https": proxy}
        try:
            r = sess.get(url, **kw)
        except Exception as exc:
            return {"error": "%s: %s" % (type(exc).__name__, exc)}
        raw = r.content or b""
        ctype = str(r.headers.get("content-type") or "")
        body = ""
        if HTMLISH.search(ctype) or "xml" in ctype.lower() or "json" in ctype.lower() \
                or ctype.startswith("text/") or not ctype:
            try:
                body = raw.decode(r.encoding or "utf-8", "replace")
            except Exception:
                body = raw.decode("utf-8", "replace")
        return {
            "status": r.status_code,
            "headers": {k: v for k, v in r.headers.items()},
            "body": body,
            "raw": raw,
            "text": _html_to_text(body) if HTMLISH.search(ctype) else body,
            "content_type": ctype,
            "final_url": str(r.url),
            "bytes": len(raw),
        }

    def _absorb(self, step: dict, got: dict, level: int, verdict: str, why: str) -> None:
        """Fold an HTTP answer into a step, and keep the bytes for the next rung
        and for the viewer."""
        step.update(status=got["status"], verdict=verdict, why=why,
                    bytes=got.get("bytes", 0), final_url=got.get("final_url", self.url),
                    title=_title_of(got.get("body", "")),
                    text_len=len(got.get("text") or ""),
                    content_type=got.get("content_type", ""))
        step["headers"] = {k: v for k, v in list((got.get("headers") or {}).items())[:40]}
        wall = detect_challenge(got.get("body", ""), got.get("headers", {}),
                                got.get("status", 0), len((got.get("text") or "").strip()))
        if wall:
            step["challenge"] = [wall]
        name = "l%s.body" % ("internal" if level < 0 else level)
        ctype = (got.get("content_type", "") or "").lower()
        if got.get("body"):
            ext = ".html" if HTMLISH.search(ctype) else (
                ".json" if "json" in ctype else ".txt")
            step["artifacts"]["body"] = self.artifact(name + ext, got["body"])
        elif got.get("raw"):
            ext = ".bin"
            for needle, suffix in (("pdf", ".pdf"), ("image/png", ".png"),
                                   ("image/jpeg", ".jpg"), ("image/webp", ".webp"),
                                   ("zip", ".zip"), ("csv", ".csv")):
                if needle in ctype:
                    ext = suffix
                    break
            step["artifacts"]["body"] = self.artifact(name + ext, got["raw"], binary=True)
        if verdict == OK:
            self.content = {
                "level": level, "status": got["status"], "url": got.get("final_url"),
                "content_type": ctype, "title": step.get("title", ""),
                "text": got.get("text") or "", "html": got.get("body") or "",
                "links": _links_of(got.get("body", "")),
            }
        # The bytes L1 will parse, so the light rung is genuinely one process and
        # not a second request to a site that already answered us.
        if got.get("body"):
            self._last_html = got["body"]
            self._last_url = got.get("final_url", self.url)

    # -- L1 ----------------------------------------------------------------
    def _l_light(self) -> dict:
        step = self._step(LIGHT)
        runners = ensure_runners()
        html = getattr(self, "_last_html", "")
        url = getattr(self, "_last_url", self.url)
        if not html:
            got = self._http_get(self.url, dict(CHROME_HEADERS),
                                 timeout=self.cfg["timeouts"]["fetch"])
            if got.get("error"):
                step.update(verdict=ERROR, why=got["error"][:200])
                return step
            html, url = got.get("body", ""), got.get("final_url", self.url)
            step["status"] = got["status"]
        if not html.strip():
            step.update(verdict=NEEDS_JS, why="nothing to render: no HTML came back")
            return step
        dom_path = self.dir / "l1.dom.html"
        self.dir.mkdir(parents=True, exist_ok=True)
        out = _run_node(runners["ladder-light.mjs"], {
            "url": url, "html": html, "timeoutMs": int(self.cfg["timeouts"]["light"] * 1000) - 3000,
            "domPath": str(dom_path),
            "ua": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/136.0.0.0 Safari/537.36",
        }, timeout=self.cfg["timeouts"]["light"], sandbox_env=True, heap_mb=256)

        step["console"] = (out.get("console") or [])[:60]
        step["requests"] = (out.get("requests") or [])[:60]
        if not out.get("ok"):
            step.update(verdict=UNAVAILABLE if "not installed" in str(out.get("why")) else ERROR,
                        why=out.get("why", "the light rung returned nothing"))
            return step
        dom = out.get("html") or ""
        text = out.get("text") or ""
        verdict, why = classify(step.get("status") or 200, {}, dom, text, "text/html", LIGHT)
        step.update(verdict=verdict, why=why, title=out.get("title", ""),
                    text_len=out.get("text_len", len(text)), bytes=len(dom),
                    final_url=url, status=step.get("status") or 200)
        if dom_path.exists():
            step["artifacts"]["dom"] = dom_path.name
        step["artifacts"]["console"] = self.artifact("l1.console.json", step["console"])
        if verdict == OK:
            self.content = {"level": LIGHT, "status": step["status"], "url": url,
                            "content_type": "text/html", "title": out.get("title", ""),
                            "text": text, "html": dom, "links": out.get("links") or []}
        return step

    # -- L2 / L3 -----------------------------------------------------------
    def _chromium(self, level: int, proxy: str = "", headed: bool = False) -> dict:
        step = self._step(level)
        afford, why = afford_heavy()
        if not afford:
            step.update(verdict=UNAVAILABLE, why=why)
            return step
        runners = ensure_runners()
        self.dir.mkdir(parents=True, exist_ok=True)
        shot = self.dir / ("l%d.jpg" % level)
        dom = self.dir / ("l%d.dom.html" % level)
        timeout = float(self.cfg["timeouts"][LEVELS[level]["key"]])
        display = self._headed_display() if headed else ""
        payload = {
            "url": self.url,
            "proxy": proxy or "",
            "timeoutMs": int(timeout * 1000) - 8000,
            "challengeWaitMs": 12000,
            "shotPath": str(shot),
            "domPath": str(dom),
            "blockMedia": True,
            "stealthPath": str(CB_ROOT / "extensions" / "stealth" / "stealth.js"),
            "headless": not bool(display),
            "display": display,
        }
        with heavy_lock(self.cfg["heavy_lock_wait_s"]) as got_lock:
            if not got_lock:
                step.update(verdict=UNAVAILABLE,
                            why="another browser rung is already running on this box, "
                                "one at a time is the rule that keeps Chrome off the ceiling")
                return step
            out = _run_node(runners["ladder-chromium.mjs"], payload, timeout=timeout + 15)
        return self._absorb_browser(step, out, level, proxy, shot, dom)

    def _absorb_browser(self, step: dict, out: dict, level: int, proxy: str,
                        shot: Path, dom: Path) -> dict:
        step["console"] = (out.get("console") or [])[:60]
        step["requests"] = (out.get("requests") or [])[:80]
        step["challenge"] = out.get("challenge") or []
        step["mode"] = out.get("mode") or ""
        step["stealth"] = out.get("stealth") or ""
        step["ua"] = out.get("ua") or ""
        step["egress"] = egress_info(self.cfg["dc_proxy"] if proxy else "") if level != RESIDENT \
            else self._resident_egress()
        if shot.exists():
            step["artifacts"]["screenshot"] = shot.name
        if dom.exists():
            step["artifacts"]["dom"] = dom.name
        if step["console"]:
            step["artifacts"]["console"] = self.artifact("l%d.console.json" % level, step["console"])
        if step["requests"]:
            step["artifacts"]["network"] = self.artifact("l%d.network.json" % level, step["requests"])
        if not out.get("ok"):
            step.update(verdict=ERROR, why=out.get("why", "the browser rung returned nothing"),
                        status=out.get("status") or 0)
            return step
        html, text = out.get("html") or "", out.get("text") or ""
        status = int(out.get("status") or 0)
        # The browser's own marker scan decided how long to wait; the verdict is
        # decided here, on the settled DOM, by the same tiered rule the cheap
        # rungs use: so a page that merely *ships* DataDome does not read as a
        # page that is being blocked by it.
        verdict, why = classify(status, out.get("headers") or {}, html, text,
                                "text/html", level)
        step.update(verdict=verdict, why=why)
        if verdict == CHALLENGE and step["challenge"]:
            step["why"] = "%s interstitial still up after waiting it out" % \
                ", ".join(sorted(set(step["challenge"])))
        elif verdict != CHALLENGE:
            # Markers seen mid-flight but gone by the end: it solved itself.
            if step["challenge"]:
                step["challenge_passed"] = step.pop("challenge")
                step["challenge"] = []
        step.update(status=status, title=out.get("title", ""), bytes=len(html),
                    text_len=out.get("text_len", len(text)),
                    final_url=out.get("url") or self.url,
                    headers={k: v for k, v in list((out.get("headers") or {}).items())[:40]})
        if step["verdict"] == OK:
            self.content = {"level": level, "status": status, "url": out.get("url"),
                            "content_type": "text/html", "title": out.get("title", ""),
                            "text": text, "html": html, "links": out.get("links") or [],
                            "screenshot": shot.name if shot.exists() else ""}
        return step

    def _headed_display(self) -> str:
        """The X display the resident browser already runs on, if it is up.

        A headed Chromium here is not a new desktop: it is one more window on an
        Xvfb that exists anyway, which means the L3 rung is watchable through the
        same noVNC viewer as L4 and costs nothing extra to make visible."""
        disp = self._resident_session().get("display")
        if disp in (None, ""):
            return ""
        return ":%s" % disp if Path("/tmp/.X11-unix/X%s" % disp).exists() else ""

    def _l_chromium(self) -> dict:
        # Headless: the cheap real browser. If it is the *browser* that a site
        # objects to rather than the address, L3 is where that gets fixed.
        return self._chromium(CHROMIUM, "", headed=False)

    def _l_chromium_dc(self) -> dict:
        if not warp_start():
            step = self._step(CHROMIUM_DC)
            step.update(verdict=UNAVAILABLE,
                        why="no datacenter egress available (WARP would not start), "
                            "skipping to the residential rung")
            return step
        _warp_touch()
        # Both variables move at this rung, deliberately: a different egress AND
        # a full windowed browser. Everything L2 could be refused for: the
        # HeadlessChrome brand, a missing window, the address: is different here.
        return self._chromium(CHROMIUM_DC, self.cfg["dc_proxy_playwright"], headed=True)

    # -- L4 ----------------------------------------------------------------
    def _resident_session(self) -> dict:
        want = self.cfg["resident_browser"]
        default = {"id": "default", "cdp_port": 9222, "vnc_port": 6080}
        try:
            raw = json.loads(SESSIONS_FILE.read_text())
            rows = raw.get("sessions") if isinstance(raw, dict) else raw
            for r in rows or []:
                if isinstance(r, dict) and r.get("id") == want:
                    return r
        except Exception:
            pass
        return default

    def _resident_egress(self) -> dict:
        """What the resident browser looks like from outside. It goes through
        its own relay port, so ask through that rather than assuming."""
        sess = self._resident_session()
        port = 0
        try:
            conf = json.loads((CB_ROOT / "proxy.json").read_text())
            if conf.get("enabled"):
                port = int(((conf.get("sessions") or {}).get(sess.get("id")) or {})
                           .get("local_port") or 0)
        except Exception:
            port = 0
        return egress_info("http://127.0.0.1:%d" % port if port else "")

    def _l_resident(self) -> dict:
        step = self._step(RESIDENT)
        sess = self._resident_session()
        cdp = int(sess.get("cdp_port") or 0)
        if not _port_alive(cdp):
            step.update(verdict=UNAVAILABLE,
                        why="the resident browser (%s, cdp %s) is not running: start it from "
                            "the Browser tab" % (sess.get("id"), cdp))
            return step
        runners = ensure_runners()
        self.dir.mkdir(parents=True, exist_ok=True)
        shot = self.dir / "l4.jpg"
        dom = self.dir / "l4.dom.html"
        timeout = float(self.cfg["timeouts"]["resident"])
        _activity("escalated to resident browser", sid=str(sess.get("id")), url=self.url,
                  run=self.id, reason=(self.steps[-1].get("why") if self.steps else "asked for"),
                  rung="resident")
        if self.takeover:
            self._start_stream(sess)
        out = _run_node(runners["ladder-chromium.mjs"], {
            "url": self.url,
            "cdp": "http://127.0.0.1:%d" % cdp,
            "timeoutMs": int(timeout * 1000) - 10000,
            "challengeWaitMs": 20000,
            "shotPath": str(shot),
            "domPath": str(dom),
            "blockMedia": True,
        }, timeout=timeout + 15)
        step = self._absorb_browser(step, out, RESIDENT, "residential", shot, dom)
        step["browser"] = sess.get("id")
        step["takeover_url"] = "/browser/%s/vnc.html" % sess.get("id") if self.takeover else ""
        return step

    def _start_stream(self, sess: dict) -> None:
        """Bring up the noVNC stream so a human can take the page over. Only on
        request: a stream nobody is watching is ~2% of a core, forever."""
        with contextlib.suppress(Exception):
            if _port_alive(int(sess.get("vnc_port") or 0)):
                return
            if sess.get("managed"):
                subprocess.run(["bash", str(CB_ROOT / "bin" / "browser-session.sh"), "vnc-start",
                                str(sess.get("id")), str(sess.get("display")),
                                str(sess.get("rfb_port")), str(sess.get("vnc_port"))],
                               capture_output=True, timeout=60)
            else:
                subprocess.run(["sudo", "-n", "systemctl", "start",
                                os.environ.get("CB_VNC_UNIT", "claude-vnc")],
                               capture_output=True, timeout=60)
            _activity("live view started for takeover", sid=str(sess.get("id")), run=self.id)


# ---------------------------------------------------------------------------
# Run store
# ---------------------------------------------------------------------------
def prune_runs() -> None:
    cfg = config()
    try:
        dirs = sorted([d for d in RUNS_DIR.iterdir() if d.is_dir()],
                      key=lambda d: d.stat().st_mtime)
    except Exception:
        return
    cutoff = time.time() - float(cfg["runs_kept_days"]) * 86400
    keep = int(cfg["runs_kept"])
    doomed = set(dirs[:-keep] if len(dirs) > keep else [])
    for d in dirs:
        try:
            if d in doomed or d.stat().st_mtime < cutoff:
                shutil.rmtree(str(d), ignore_errors=True)
        except Exception:
            continue


def list_runs(limit: int = 40) -> List[dict]:
    out = []
    try:
        dirs = sorted([d for d in RUNS_DIR.iterdir() if d.is_dir()],
                      key=lambda d: d.stat().st_mtime, reverse=True)[:limit]
    except Exception:
        return out
    for d in dirs:
        rec = load_run(d.name)
        if rec:
            out.append({k: rec.get(k) for k in
                        ("id", "url", "host", "agent", "note", "started", "finished",
                         "ms", "running", "result")})
    return out


def load_run(run_id: str) -> dict:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(run_id or ""))
    if not safe:
        return {}
    try:
        return json.loads((RUNS_DIR / safe / "run.json").read_text())
    except Exception:
        return {}


def run_artifact(run_id: str, name: str) -> Optional[Path]:
    safe = re.sub(r"[^A-Za-z0-9_-]", "", str(run_id or ""))
    fname = re.sub(r"[^A-Za-z0-9_.-]", "", str(name or "")).replace("..", "")
    if not safe or not fname:
        return None
    p = RUNS_DIR / safe / fname
    try:
        p = p.resolve()
        if RUNS_DIR.resolve() not in p.parents:
            return None
    except Exception:
        return None
    return p if p.exists() else None


# ---------------------------------------------------------------------------
# The one call everything else makes
# ---------------------------------------------------------------------------
def fetch(url: str, **kw) -> dict:
    """Run the ladder for one URL and return the whole run record.

    The answer, when there is one, is in record['result'] plus the `content`
    key: text, html, links, and the screenshot name when a browser rung took it.
    """
    if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
        url = "https://" + url.lstrip("/")
    r = Run(url, **kw)
    rec = r.go()
    rec["content"] = r.content
    return rec


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
_ARROW = "        \u2193"


def _fmt_ladder(rec: dict, colour: bool = True) -> str:
    """The trace, as the dashboard draws it and as an agent reads it."""
    def c(code, s):
        return "\033[%sm%s\033[0m" % (code, s) if colour else s
    lines = [rec.get("url", ""), "\u2500" * min(60, max(20, len(rec.get("url", "")) + 4))]
    for i, s in enumerate(rec.get("steps") or []):
        if i:
            lines.append(_ARROW)
        verdict = s.get("verdict", "")
        dot = c("32", "\u25cf") if verdict == OK else (
            c("33", "\u25cf") if verdict in (NEEDS_JS, SKIPPED, UNAVAILABLE) else c("31", "\u25cf"))
        head = "%s %-11s %6s ms" % (dot, s.get("label", ""), s.get("ms", 0))
        bits = []
        if s.get("status"):
            bits.append(str(s["status"]))
        if s.get("egress", {}).get("ip"):
            bits.append(s["egress"]["ip"])
        lines.append(head + ("   " + "  ".join(bits) if bits else ""))
        lines.append("    %s" % s.get("why", ""))
    res = rec.get("result") or {}
    lines.append(_ARROW)
    lines.append((c("32", "\u2713 DONE") + "  %s in %d ms" % (res.get("level_label", ""),
                                                             res.get("ms", 0)))
                 if res.get("ok") else
                 (c("31", "\u2717 FAILED") + "  %s: %s" % (res.get("verdict", ""),
                                                           res.get("why", ""))))
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="browser_ladder", description=__doc__.splitlines()[0],
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("get", help="fetch a URL at the cheapest rung that works")
    p.add_argument("url")
    p.add_argument("--max-level", type=int, default=RESIDENT,
                   help="ceiling: -1 internal, 0 fetch, 1 light, 2 chromium, 3 +dc, 4 residential")
    p.add_argument("--level", type=int, default=None, help="run exactly this rung, no escalation")
    p.add_argument("--start-level", type=int, default=None, help="start here, still escalates")
    p.add_argument("--no-memory", action="store_true", help="ignore what this host needed before")
    p.add_argument("--takeover", action="store_true", help="at L4, start the stream for a human")
    p.add_argument("--timeout", type=float, default=None, help="overall deadline, seconds")
    p.add_argument("--json", action="store_true", help="the whole run record")
    p.add_argument("--html", action="store_true", help="print HTML instead of text")
    p.add_argument("--quiet", action="store_true", help="content only, no ladder trace")
    p.add_argument("--note", default="", help="why you are fetching this, for the trail")
    p.add_argument("--max-chars", type=int, default=200_000)

    p = sub.add_parser("runs", help="recent ladder runs")
    p.add_argument("--limit", type=int, default=20)

    p = sub.add_parser("show", help="one run in full")
    p.add_argument("run_id")
    p.add_argument("--json", action="store_true")

    p = sub.add_parser("memory", help="what the ladder has learned per host")
    p.add_argument("--forget", default=None, metavar="HOST",
                   help="drop one host, or ALL")

    p = sub.add_parser("doctor", help="is every rung actually installed and affordable")

    args = ap.parse_args(argv)

    if args.cmd == "get":
        rec = fetch(args.url, max_level=args.max_level, only_level=args.level,
                    start_level=args.start_level, use_memory=not args.no_memory,
                    takeover=args.takeover, deadline_s=args.timeout,
                    agent=os.environ.get("CLAUDE_SESSION_NAME") or "", note=args.note)
        if args.json:
            print(json.dumps(rec, indent=2, default=str))
            return 0 if (rec.get("result") or {}).get("ok") else 1
        if not args.quiet:
            sys.stderr.write(_fmt_ladder(rec, colour=sys.stderr.isatty()) + "\n\n")
        content = rec.get("content") or {}
        body = content.get("html" if args.html else "text") or ""
        if body:
            print(body[:args.max_chars])
        elif not (rec.get("result") or {}).get("ok"):
            sys.stderr.write("no content: %s\n" % (rec.get("result") or {}).get("why", ""))
        return 0 if (rec.get("result") or {}).get("ok") else 1

    if args.cmd == "runs":
        for r in list_runs(args.limit):
            res = r.get("result") or {}
            print("%s  %-9s %-11s %6sms  %s" % (
                time.strftime("%m-%d %H:%M", time.localtime(r.get("started", 0))),
                "ok" if res.get("ok") else (res.get("verdict") or "running"),
                res.get("level_label", ""), r.get("ms", 0), (r.get("url") or "")[:60]))
        return 0

    if args.cmd == "show":
        rec = load_run(args.run_id)
        if not rec:
            sys.stderr.write("no such run\n")
            return 1
        print(json.dumps(rec, indent=2, default=str) if args.json
              else _fmt_ladder(rec, colour=sys.stdout.isatty()))
        return 0

    if args.cmd == "memory":
        mem = Memory()
        if args.forget:
            n = mem.forget("" if args.forget.upper() == "ALL" else args.forget)
            print("forgot %d host(s)" % n)
            return 0
        data = mem._read()
        for host, row in sorted(data.items(), key=lambda kv: kv[1].get("ts", 0), reverse=True):
            print("%-40s %-11s %-12s %s ago  %s" % (
                host[:40], LEVELS.get(row.get("level", 0), {}).get("label", "?"),
                row.get("verdict", ""), _ago(time.time() - row.get("ts", 0)),
                (row.get("why") or "")[:50]))
        if not data:
            print("nothing learned yet")
        return 0

    if args.cmd == "doctor":
        return _doctor()
    return 2


def doctor() -> List[dict]:
    """Is every rung actually installed, reachable and affordable right now?

    The dashboard renders this and so does the CLI. A rung that is quietly
    missing is worse than one that is loudly missing: the ladder just skips it
    and the next rung up gets the traffic, which looks like the site being
    hostile rather than a package not being installed."""
    cfg = config()
    runners = ensure_runners()
    mb, pct = _mem()
    rows = []
    routes = nginx_routes()
    rows.append({"level": INTERNAL, "name": "L-1 internal", "state": "ok",
                 "detail": "%d owned hosts; %d nginx route(s) on this box go straight to a "
                           "loopback port%s"
                           % (len(cfg["internal_hosts"]), len(routes),
                              ", %d mapped by hand" % len(cfg["internal_ports"])
                              if cfg.get("internal_ports") else "")})
    kind = fetch_kind()
    rows.append({"level": FETCH, "name": "L0  fetch",
                 "state": "ok" if kind == "curl_cffi" else "degraded",
                 "detail": kind if kind == "curl_cffi" else
                           "%s: python's TLS handshake, which Akamai and Cloudflare refuse on "
                           "fingerprint alone. pip install --user curl_cffi" % kind})
    have_hd = (NODE_MODULES / "happy-dom").exists()
    rows.append({"level": LIGHT,
                 "name": "L1  light",
                 "state": "ok" if (have_hd and runners["ladder-light.mjs"].exists()) else "missing",
                 "detail": "happy-dom %s" % ("present" if have_hd
                                             else "not installed in " + str(NODE_MODULES))})
    have_pw = (NODE_MODULES / "playwright-core").exists()
    chrome = sorted((HOME / ".cache" / "ms-playwright").glob("chromium-*/chrome-linux64/chrome"))
    rows.append({"level": CHROMIUM, "name": "L2  chromium",
                 "state": "ok" if (have_pw and chrome) else "missing",
                 "detail": "playwright-core %s, %d chromium build(s)"
                           % ("present" if have_pw else "missing", len(chrome))})
    warp = warp_running()
    conf_ok = False
    try:
        conf_ok = "[Interface]" in Path(cfg["warp_conf"]).read_text()
    except Exception:
        conf_ok = False
    rows.append({"level": CHROMIUM_DC,
                 "name": "L3  +dc egress",
                 "state": "ok" if (warp or conf_ok) else "missing",
                 "detail": "WARP " + (
                     "up on :%s" % cfg["warp_port"] if warp else
                     "installed, starts on demand" if conf_ok else
                     "binary present but no wireguard profile: run `wgcf register "
                     "--accept-tos && wgcf generate` in %s, then append a [Socks5] "
                     "BindAddress = 127.0.0.1:%s block"
                     % (Path(cfg["warp_conf"]).parent, cfg["warp_port"])
                     if Path(cfg["warp_bin"]).exists() else "not installed")})
    sess = Run("https://x/", run_id="doctor")._resident_session()
    sess_ok = _port_alive(int(sess.get("cdp_port") or 0))
    rows.append({"level": RESIDENT, "name": "L4  residential",
                 "state": "ok" if sess_ok else "down",
                 "detail": "resident browser '%s' %s"
                           % (cfg["resident_browser"],
                              "reachable over CDP" if sess_ok else "not running")})
    afford, why = afford_heavy()
    rows.append({"level": None, "name": "resources", "state": "ok" if afford else "tight",
                 "detail": "%.0f MB free (%.1f%%)%s" % (mb, pct, "" if afford else ", " + why)})
    with contextlib.suppress(Exception):
        shutil.rmtree(str(RUNS_DIR / "doctor"), ignore_errors=True)
    return rows


def _doctor() -> int:
    rows = doctor()
    width = max(len(r["name"]) for r in rows)
    for r in rows:
        print("%-*s  %-9s %s" % (width, r["name"], r["state"], r["detail"]))
    return 0 if all(r["state"] in ("ok", "tight") for r in rows) else 1


if __name__ == "__main__":
    sys.exit(main())
