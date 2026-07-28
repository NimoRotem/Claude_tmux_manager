#!/usr/bin/env bash
# Claude credentials (abstraction 8).
#
# The interface is shared — status | login | refresh | export-env — and the
# implementation is not shared with anything. Login flows and token storage have
# nothing in common between backends, so the runtime only ever calls the four
# verbs and never learns how either one works.
#
#   status      -> JSON on stdout: {"backend","mode","valid","expires_at","detail"}
#                  exit 0 if usable, 1 if not
#   login       -> start an interactive login; prints the URL a human must open
#   refresh     -> renew a token that can be renewed without a human; exit 1 if not
#   export-env  -> KEY=VALUE lines to eval before launching the CLI

set -uo pipefail
VERB="${1:-status}"
CLAUDE_HOME="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
CREDS="$CLAUDE_HOME/.credentials.json"

json_status() {
  python3 - "$CREDS" <<'PY'
import json, sys, time
from pathlib import Path

path = Path(sys.argv[1])
out = {"backend": "claude", "mode": "none", "valid": False,
       "expires_at": 0, "detail": ""}
try:
    data = json.loads(path.read_text())
    oauth = data.get("claudeAiOauth") or {}
    token = oauth.get("accessToken") or ""
    expires = int(oauth.get("expiresAt") or 0)
    if token:
        out["mode"] = "oauth"
        out["expires_at"] = expires
        # expiresAt is in ms. A token inside its last minute is treated as
        # expired: launching with it just fails one turn later.
        out["valid"] = expires == 0 or expires > (time.time() + 60) * 1000
        out["detail"] = oauth.get("subscriptionType") or ""
except FileNotFoundError:
    out["detail"] = "no credentials file"
except Exception as exc:
    out["detail"] = f"unreadable: {exc}"

if not out["valid"] and __import__("os").environ.get("ANTHROPIC_API_KEY"):
    out.update(mode="apikey", valid=True, detail="ANTHROPIC_API_KEY in env")

print(json.dumps(out))
sys.exit(0 if out["valid"] else 1)
PY
}

case "$VERB" in
  status)
    json_status
    ;;
  login)
    # Interactive by nature: prints a URL a human opens. The dashboard captures
    # this pane and surfaces the URL — see api_auth_setup_start in app.py.
    echo "Starting Claude login. Open the URL it prints, approve, paste the code back." >&2
    exec claude /login
    ;;
  refresh)
    # An OAuth credential with a refresh token renews without a human; a
    # setup-token cannot be refreshed at all, so say so instead of looping.
    if [ ! -f "$CREDS" ]; then
      echo "no credential to refresh" >&2; exit 1
    fi
    if ! python3 -c "
import json,sys
d=json.load(open('$CREDS')).get('claudeAiOauth',{})
sys.exit(0 if d.get('refreshToken') else 1)"; then
      echo "this credential has no refresh token (setup-token); a human must log in again" >&2
      exit 1
    fi
    # Claude Code refreshes on its own at request time; a no-op probe is enough
    # to make it do so now rather than mid-turn.
    claude -p "ok" --max-turns 1 >/dev/null 2>&1 && json_status
    ;;
  export-env)
    [ -n "${ANTHROPIC_API_KEY:-}" ] && echo "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
    echo "CLAUDE_CONFIG_DIR=$CLAUDE_HOME"
    ;;
  *)
    echo "usage: $0 {status|login|refresh|export-env}" >&2; exit 2
    ;;
esac
