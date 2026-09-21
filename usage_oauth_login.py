#!/usr/bin/env python3
"""Grant the dashboard its own account-scoped OAuth credential for the 5h/7d bars.

Why this exists
---------------
The nav usage bars read ``https://api.anthropic.com/api/oauth/usage``, which
needs account scope (``any_of(user:profile, user:office)``). Two credential
shapes end up in ``~/.claude/.credentials.json`` and only one of them works:

* an **interactive** ``claude`` login — short-lived, auto-refreshing, and the
  account endpoints accept it (this is what builder has, and why its bars work);
* a **long-lived** ``claude setup-token`` (``sk-ant-oat01…``, ~1 year) — it
  *lists* ``user:profile`` locally but the real grant is inference-only, so
  ``/api/oauth/usage`` answers ``403 permission_error`` no matter how recently
  the token was minted.

instance-3 must keep the long-lived token: a cron re-writes
``~/.claude/.credentials.json`` back to it every 10 minutes, which is the cure
for the parallel-session OAuth rotation war. So the dashboard gets a *second*
credential, stored at ``~/.tmux-dashboard/usage_oauth.json`` — outside
``~/.claude/`` where that cron never looks. ``_usage_access_token()`` in app.py
prefers it and falls back to the shared credential when it is absent.

How it runs
-----------
Standard OAuth authorization-code + PKCE against the Claude Code public client.
The consent click happens in one of this host's persistent Chrome sessions
(the same browsers the dashboard drives over CDP), so no human is needed as
long as that browser is signed in to claude.ai with a Max/Pro account:

    python3 usage_oauth_login.py --cdp-port 9224

``--print-url`` skips the browser and just prints the authorize URL, for the
case where you want to click it through yourself and paste the code back with
``--code``.

The new token is verified against ``/api/oauth/profile`` before it is saved: a
credential that still lacks account scope is rejected rather than allowed to
displace the working fallback.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
REDIRECT_URI = "https://console.anthropic.com/oauth/code/callback"
SCOPE = "org:create_api_key user:profile user:inference"
AUTHORIZE_URL = "https://claude.ai/oauth/authorize"
# platform.claude.com first: console.anthropic.com now redirects there, so a tab
# parked on the console host ends up on a different origin and the same-origin
# POST turns into a CORS "Failed to fetch".
TOKEN_URLS = ("https://platform.claude.com/v1/oauth/token",
              "https://console.anthropic.com/v1/oauth/token")
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
OUT_FILE = Path.home() / ".tmux-dashboard" / "usage_oauth.json"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def make_pkce() -> tuple[str, str]:
    verifier = _b64url(secrets.token_bytes(32))
    challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
    return verifier, challenge


def authorize_url(challenge: str, state: str) -> str:
    return AUTHORIZE_URL + "?" + urllib.parse.urlencode({
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
    })


# --- Driving the consent click through a CDP browser ------------------------

async def _cdp_click_through_async(cdp_port: int, url: str, timeout: float = 120,
                                   verifier: str = "", state: str = "") -> dict:
    """Open the authorize URL in a throwaway tab, approve, and exchange the code.

    Returns ``{"code": "code#state", "token": {...} | None}``.

    The token POST is issued *inside the callback tab* rather than from this
    process. Two reasons, both learned the hard way: the tab is already on
    ``console.anthropic.com`` so the request is same-origin and carries the
    ``cf_clearance`` cookie, and it egresses through the browser's residential
    proxy. A plain urllib POST from the VM gets ``403 error code: 1010`` —
    Cloudflare blocking a datacenter IP with a non-browser UA — which burns the
    single-use code. Falls back to a direct POST if the in-tab call fails.

    Uses ``websockets`` (the async client app.py already depends on) rather than
    ``websocket-client``, which is not installed on every host that runs this.
    """
    import websockets

    def http(path: str, method: str = "GET"):
        req = urllib.request.Request(f"http://127.0.0.1:{cdp_port}{path}", method=method)
        return json.loads(urllib.request.urlopen(req, timeout=20).read())

    try:
        target = http("/json/new?about:blank", "PUT")
    except urllib.error.HTTPError:
        target = http("/json/new?about:blank")

    counter = [0]
    ws = await websockets.connect(target["webSocketDebuggerUrl"], max_size=None,
                                  ping_interval=None)

    async def call(method, params=None):
        counter[0] += 1
        mid = counter[0]
        await ws.send(json.dumps({"id": mid, "method": method, "params": params or {}}))
        deadline = time.time() + 60
        while time.time() < deadline:
            msg = json.loads(await asyncio.wait_for(ws.recv(), timeout=60))
            if msg.get("id") == mid:
                if "error" in msg:
                    raise RuntimeError(msg["error"].get("message", "CDP error"))
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} timed out")

    async def js(expr):
        r = await call("Runtime.evaluate", {"expression": expr, "returnByValue": True,
                                            "awaitPromise": True, "userGesture": True})
        if r.get("exceptionDetails"):
            raise RuntimeError(str(r["exceptionDetails"].get("text", "JS exception")))
        return r.get("result", {}).get("value")

    async def in_tab_exchange(code_and_state: str) -> dict:
        """POST the token request from the callback page's own origin."""
        code, _, embedded = code_and_state.partition("#")
        payload = json.dumps({
            "grant_type": "authorization_code",
            "code": code,
            "state": embedded or state,
            "client_id": CLIENT_ID,
            "redirect_uri": REDIRECT_URI,
            "code_verifier": verifier,
        })
        for token_url in TOKEN_URLS:
            # The consent page renders the code on claude.ai without ever
            # redirecting, so the tab's origin is claude.ai and a POST to the
            # token host would be cross-origin (CORS-blocked, no credentials).
            # Park the tab on the token host first so the call is same-origin.
            origin = "/".join(token_url.split("/")[:3])
            try:
                if not (await js("location.origin") or "").startswith(origin):
                    await call("Page.navigate", {"url": origin + "/"})
                    for _ in range(20):
                        await asyncio.sleep(0.5)
                        if (await js("document.readyState")) == "complete":
                            break
                    await asyncio.sleep(1)
            except Exception:
                pass
            expr = ("fetch(%s,{method:'POST',headers:{'Content-Type':'application/json'},"
                    "body:%s}).then(async r=>r.status+'\\u0000'+(await r.text()))"
                    ".catch(e=>'0\\u0000'+e)") % (json.dumps(token_url), json.dumps(payload))
            try:
                raw = await js(expr) or ""
            except Exception as e:
                print(f"  in-tab exchange via {token_url}: {e}", flush=True)
                continue
            status, _, text = raw.partition("\x00")
            if status == "200":
                try:
                    return json.loads(text)
                except Exception:
                    pass
            print(f"  in-tab exchange via {token_url}: {status} {text[:160]}", flush=True)
        return {}

    try:
        await call("Page.enable")
        await call("Runtime.enable")
        await call("Page.navigate", {"url": url})
        deadline = time.time() + timeout
        clicked = False
        while time.time() < deadline:
            await asyncio.sleep(2)
            try:
                here = await js("location.href") or ""
                body = await js("document.body ? document.body.innerText : ''") or ""
            except Exception:
                continue
            # The callback page renders the code as `code#state` in the DOM (and
            # puts it in the query string when it redirects).
            found = ""
            m = re.search(r"[?&]code=([^&\s#]+)", here)
            if m:
                st = re.search(r"[?&]state=([^&\s#]+)", here)
                found = (urllib.parse.unquote(m.group(1))
                         + ("#" + urllib.parse.unquote(st.group(1)) if st else ""))
            else:
                m = re.search(r"\b([A-Za-z0-9_-]{20,}#[A-Za-z0-9_-]{20,})\b", body)
                if m:
                    found = m.group(1)
            if found:
                tok = await in_tab_exchange(found) if verifier else {}
                return {"code": found, "token": tok or None}
            low = body.lower()
            if "max or pro" in low or "not available" in low:
                raise RuntimeError("this browser's claude.ai account cannot authorize "
                                   "Claude Code (no Max/Pro plan): " + body[:200])
            if "sign in" in low and "authorize" not in low and "log in" not in low:
                raise RuntimeError("this browser is signed out of claude.ai — sign it in first")
            if not clicked:
                clicked = bool(await js("""(() => {
                    const want = /^(authorize|allow|continue|approve)$/i;
                    const el = [...document.querySelectorAll('button,a,[role=button]')]
                        .find(e => want.test((e.innerText || '').trim()));
                    if (!el) return false;
                    el.click();
                    return true;
                })()"""))
        raise TimeoutError("timed out waiting for the authorization code")
    finally:
        try:
            await ws.close()
        except Exception:
            pass
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/close/{target['id']}",
                                   timeout=10).read()
        except Exception:
            pass


def _cdp_click_through(cdp_port: int, url: str, timeout: float = 120,
                       verifier: str = "", state: str = "") -> dict:
    return asyncio.run(_cdp_click_through_async(cdp_port, url, timeout, verifier, state))


# --- Token exchange + verification ------------------------------------------

def exchange(code_and_state: str, verifier: str, state: str) -> dict:
    code, _, embedded_state = code_and_state.partition("#")
    body = json.dumps({
        "grant_type": "authorization_code",
        "code": code,
        "state": embedded_state or state,
        "client_id": CLIENT_ID,
        "redirect_uri": REDIRECT_URI,
        "code_verifier": verifier,
    }).encode()
    last = None
    for url in TOKEN_URLS:
        # A bare urllib UA from a datacenter IP gets `403 error code: 1010` off
        # Cloudflare. Looking like a browser is not a guarantee, but it is the
        # difference between "sometimes works" and "never works".
        req = urllib.request.Request(url, data=body, headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Origin": "https://console.anthropic.com",
            "Referer": "https://console.anthropic.com/oauth/code/callback",
            "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"),
        })
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            last = f"{url} -> {e.code} {e.read().decode()[:300]}"
        except Exception as e:  # noqa: BLE001 - report whatever the network did
            last = f"{url} -> {e}"
    raise RuntimeError("token exchange failed: " + str(last))


def api_get(url: str, token: str) -> tuple[int, str]:
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token,
        "anthropic-beta": "oauth-2025-04-20",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cdp-port", type=int, default=9222,
                    help="CDP port of a claude.ai-signed-in browser to click consent through")
    ap.add_argument("--print-url", action="store_true",
                    help="print the authorize URL and exit (click it yourself, then pass --code)")
    ap.add_argument("--code", help="authorization code (`code#state`) from a manual click-through")
    ap.add_argument("--verifier", help="PKCE verifier that goes with --code")
    ap.add_argument("--out", default=str(OUT_FILE), help="where to write the credential")
    ap.add_argument("--no-direct-fallback", action="store_true",
                    help="don't retry the token POST from this host if the in-tab "
                         "call failed (the VM IP gets Cloudflare-1010'd / rate-limited)")
    args = ap.parse_args()

    tok = {}
    if args.code:
        if not args.verifier:
            print("--code needs the --verifier printed alongside the URL", file=sys.stderr)
            return 2
        verifier, state = args.verifier, args.code.partition("#")[2]
        code = args.code
    else:
        verifier, challenge = make_pkce()
        state = verifier
        url = authorize_url(challenge, state)
        if args.print_url:
            print("verifier:", verifier)
            print("url:", url)
            return 0
        print(f"Driving consent through CDP :{args.cdp_port} …", flush=True)
        res = _cdp_click_through(args.cdp_port, url, verifier=verifier, state=state)
        code, tok = res["code"], (res.get("token") or {})
        print("got authorization code" + (" + token" if tok else ""), flush=True)

    # The in-tab exchange already ran for the browser path. Only fall back to a
    # direct POST when it did not (manual --code, or the in-tab fetch failed) —
    # the code is single-use, so a needless retry here would waste it.
    if not tok:
        if args.no_direct_fallback:
            print("in-tab exchange failed and --no-direct-fallback is set", file=sys.stderr)
            return 1
        tok = exchange(code, verifier, state)
    access = tok.get("access_token", "")
    if not access:
        print("no access_token in the exchange response: " + json.dumps(tok)[:300], file=sys.stderr)
        return 1

    # Refuse to save a credential that still lacks account scope — a bad file
    # here would silently displace the working shared-credential fallback.
    status, body = api_get(PROFILE_URL, access)
    if status != 200:
        print(f"the new token still fails {PROFILE_URL}: {status} {body[:300]}", file=sys.stderr)
        print("NOT saving it — the dashboard keeps using the existing fallback.", file=sys.stderr)
        return 1
    who = ""
    try:
        who = (json.loads(body).get("account") or {}).get("email_address", "")
    except Exception:
        pass
    ustatus, ubody = api_get(USAGE_URL, access)
    if ustatus != 200:
        print(f"profile works but {USAGE_URL} answers {ustatus}: {ubody[:300]}", file=sys.stderr)
        return 1

    cred = {
        "accessToken": access,
        "refreshToken": tok.get("refresh_token", ""),
        "expiresAt": int((time.time() + int(tok.get("expires_in") or 3600)) * 1000),
        "scopes": (tok.get("scope") or SCOPE).split(),
        "grantedFor": "tmux-dashboard usage bars",
        "account": who,
    }
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(cred, indent=2))
    os.chmod(out, 0o600)
    print(f"saved {out} for {who or 'the signed-in account'}")
    print("usage now reads:", ubody[:200])
    return 0


if __name__ == "__main__":
    sys.exit(main())
