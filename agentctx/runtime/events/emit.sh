#!/usr/bin/env bash
# One event vocabulary, three sources (abstraction 7).
#
#   Claude  hooks in settings.json  -> PreToolUse / PostToolUse / Notification / Stop
#   Codex   notify in config.toml   -> one program, coarser
#   either  pipe-pane regex         -> the TUI fallback, adapters/*/detect.toml
#
# Everything downstream reads this one stream and never learns which source a
# line came from. The sources are NOT equally rich — Claude emits per-tool
# events, Codex does not — so consumers may rely on the event NAMES but never on
# the count or the ordering being identical between backends.
#
# Usage:  emit.sh <event> [detail...]
# Events: turn.start turn.end tool.start tool.end agent.waiting agent.idle
#         session.launched session.exit codex.notify
#
# Writes one JSON object per line to $AGENTCTX_EVENTS (default: state/events.jsonl).
# Hooks are on the critical path of a turn: this must never block or fail the
# caller, so every failure exits 0.

set -uo pipefail

EVENT="${1:-unknown}"
shift || true
DETAIL="${*:-}"

STATE_DIR="${AGENTCTX_STATE:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)/state}"
EVENTS_FILE="${AGENTCTX_EVENTS:-$STATE_DIR/events.jsonl}"
mkdir -p "$(dirname "$EVENTS_FILE")" 2>/dev/null || exit 0

# Claude passes hook context as JSON on stdin; Codex passes argv. Read stdin only
# when there is something there, so a hook without input does not hang the turn.
STDIN_JSON=""
if [ ! -t 0 ]; then
  STDIN_JSON="$(timeout 1 cat 2>/dev/null || true)"
fi

json_escape() {
  python3 -c 'import json,sys; sys.stdout.write(json.dumps(sys.stdin.read()))' 2>/dev/null \
    || printf '""'
}

TS="$(date +%s)"
SESSION="${AGENTCTX_SESSION:-${TMUX_PANE:-unknown}}"
BACKEND="${AGENTCTX_BACKEND:-unknown}"
CWD="$(pwd)"

{
  printf '{"ts":%s,"event":"%s","backend":"%s","session":"%s","cwd":%s,"detail":%s,"raw":%s}\n' \
    "$TS" "$EVENT" "$BACKEND" "$SESSION" \
    "$(printf '%s' "$CWD" | json_escape)" \
    "$(printf '%s' "$DETAIL" | json_escape)" \
    "$(printf '%s' "$STDIN_JSON" | json_escape)"
} >> "$EVENTS_FILE" 2>/dev/null

# A hook must not change what the agent does. Always succeed.
exit 0
