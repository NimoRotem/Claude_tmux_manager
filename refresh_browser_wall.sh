#!/usr/bin/env bash
# Regenerate the fleet browser wall and republish it to rotem.ai/artifact.
# A snapshot that is an hour old is worse than no page, because it is believed.
#
# DO NOT TRUST THE INHERITED HOME. cron sets HOME from /etc/passwd, which on a
# tenant box is the SHARED account home, not this tenant's: the first scheduled
# run died on `cd /home/nimrod_rotem/tmux-dashboard: No such file or directory`
# while the same script worked by hand. Everything here is derived from where the
# script itself lives, and HOME is then set to match so gcloud finds its own
# credentials rather than the neighbour's.
set -uo pipefail
SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export HOME="$(dirname "$SELF_DIR")"
PY="$HOME/venv/bin/python"
REPO="$SELF_DIR"
OUT="$HOME/.claude-browser/logs/browser-wall.html"
LOG="$HOME/.claude-browser/logs/wall.log"
mkdir -p "$HOME/.claude-browser/logs"
#  trim the log: two lines a run for ever is still unbounded, and a cron that
#  fills a disk is a worse outage than the one it was watching for.
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
