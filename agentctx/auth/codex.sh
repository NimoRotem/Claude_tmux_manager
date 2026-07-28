#!/usr/bin/env bash
# Codex credentials (abstraction 8). Same four verbs as claude.sh; nothing else
# is shared between them.

set -uo pipefail
VERB="${1:-status}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
AUTH="$CODEX_HOME/auth.json"

json_status() {
  python3 - "$AUTH" <<'PY'
import json, os, sys, time
from pathlib import Path

path = Path(sys.argv[1])
out = {"backend": "codex", "mode": "none", "valid": False,
       "expires_at": 0, "detail": ""}
try:
    data = json.loads(path.read_text())
    mode = data.get("auth_mode") or ""
    if mode == "apikey" or data.get("OPENAI_API_KEY"):
        out.update(mode="apikey", valid=bool(data.get("OPENAI_API_KEY")),
                   detail="stored API key")
    elif data.get("tokens"):
        out.update(mode="chatgpt", valid=True, detail="ChatGPT plan login")
        # last_refresh is the only freshness signal Codex writes out. A very old
        # one is not proof of expiry, so report it rather than guessing.
        last = data.get("last_refresh") or ""
        if last:
            out["detail"] += f" (last refresh {last})"
except FileNotFoundError:
    out["detail"] = "no auth.json"
except Exception as exc:
    out["detail"] = f"unreadable: {exc}"

if not out["valid"] and os.environ.get("OPENAI_API_KEY"):
    out.update(mode="apikey", valid=True, detail="OPENAI_API_KEY in env")

print(json.dumps(out))
sys.exit(0 if out["valid"] else 1)
PY
}

case "$VERB" in
  status)
    json_status
    ;;
  login)
    echo "Starting Codex login. Open the URL it prints and approve it." >&2
    exec codex login
    ;;
  refresh)
    # Codex refreshes its ChatGPT tokens through the app-server rather than the
    # CLI. `codex login status` performs (and persists) a refresh as a side
    # effect, which is the only supported way to force one.
    if [ ! -f "$AUTH" ]; then
      echo "no credential to refresh" >&2; exit 1
    fi
    codex login status >/dev/null 2>&1 && json_status
    ;;
  export-env)
    [ -n "${OPENAI_API_KEY:-}" ] && echo "OPENAI_API_KEY=$OPENAI_API_KEY"
    echo "CODEX_HOME=$CODEX_HOME"
    ;;
  *)
    echo "usage: $0 {status|login|refresh|export-env}" >&2; exit 2
    ;;
esac
