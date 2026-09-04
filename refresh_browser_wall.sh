#!/usr/bin/env bash
# Regenerate the fleet browser wall and republish it to rotem.ai/artifact.
# A snapshot that is an hour old is worse than no page, because it is believed.
set -uo pipefail
export HOME="${HOME:-/home/nimrod_rotem/builder4-home}"
PY="$HOME/venv/bin/python"
REPO="$HOME/tmux-dashboard"
OUT="$HOME/.claude-browser/logs/browser-wall.html"
#  trim the log: two lines a run for ever is still unbounded, and a cron that
#  fills a disk is a worse outage than the one it was watching for.
LOG="$HOME/.claude-browser/logs/wall.log"
[ -f "$LOG" ] && [ "$(stat -c %s "$LOG")" -gt 1000000 ] && tail -c 200000 "$LOG" > "$LOG.tmp" && mv "$LOG.tmp" "$LOG"
cd "$REPO" || exit 1
"$PY" browser_fleet.py --wall "$OUT" --host instance-3 --probe >/dev/null 2>&1 || exit 1
[ -s "$OUT" ] || exit 1
#  artifact-publish lives on builder only, so the page is carried there as base64
#  over one ssh rather than copied with scp, which needs a writable path on both
#  ends and a key this cron does not have.
B64=$(base64 -w0 "$OUT")
gcloud compute ssh nimrod_rotem@builder --zone us-central1-b --command \
  "sudo -u nimrod_rotem bash -lc 'echo $B64 | base64 -d > /tmp/browser-wall.html && ~/bin/artifact-publish /tmp/browser-wall.html --slug browser-wall --title \"Browsers on the fleet\"'" \
  2>&1 | tail -2
