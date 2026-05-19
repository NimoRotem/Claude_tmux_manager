# PR archive (pre-recreate, 2026-05-19)

Original repo had Claude/Anthropic Co-Authored-By trailers in `refs/pull/*/head`.
Refs/pull are server-managed by GitHub and cannot be force-pushed, so the repo was
deleted and recreated to fully purge them. PR titles + bodies preserved below.

## #1 — Add real-time streaming, slash commands, interrupt, and Key Info extraction

- state: **closed** (merged)
- base: `main` ← head: `feature/realtime-streaming-and-notes`
- author: @NimoRotem  •  created: 2026-03-23T15:28:41Z  •  closed: 2026-03-23T15:38:31Z
- merge commit: `682c4d5bac03f7a92c20e696f1aba0740bc6b42a`

## Summary
- **Real-time raw output**: Raw Output tab now auto-updates every 2s via delta polling (`/raw-tail` endpoint returns only new lines)
- **Slash command dropdown**: Fixed `overflow:hidden` clipping the `/` menu; 8 Claude commands available
- **Interrupt button**: Red "Stop" button appears when session is busy, sends Escape to tmux pane
- **Key Info extraction**: New LLM-powered section in Info tab that extracts credentials, URLs, stack, services, project structure, uploaded files, and dev notes — persists to disk and accumulates across refreshes
- **Layout improvements**: Removed duplicate title row, moved status badges into tab bar to save vertical space

## Test plan
- [ ] Open dashboard, select a session, switch to Raw Output tab — output should stream automatically
- [ ] Click `/` button — dropdown with 8 slash commands should appear
- [ ] While Claude is busy, verify red "Stop" button appears and interrupts on click
- [ ] Go to Info tab, click "Full" — Key Info section should populate with extracted data
- [ ] Restart supervisor — notes should persist from `~/.tmux-dashboard/notes.json`

## #2 — Fix false idle detection, auto-answer Claude questions

- state: **closed** (merged)
- base: `main` ← head: `feature/activity-detection-and-auto-answer`
- author: @NimoRotem  •  created: 2026-03-26T19:04:22Z  •  closed: 2026-03-26T19:04:25Z
- merge commit: `2ac21f27d57edebab5c887d07feeda760b79dc68`

## Summary
- Fixed sessions with "local agents still running" being incorrectly marked as idle
- Auto-answers Claude's numbered question prompts by picking the most autonomous option
- Extracted `_send_option()` helper for key-sending reuse

## Test plan
- [ ] Session with "Cooked for 3m · 2 local agents still running" should show as busy
- [ ] When Claude asks numbered questions after planning, dashboard auto-selects the autonomous option
- [ ] Permission prompts ("Yes, and bypass") still auto-approved as before

## #3 — Fix false-positive busy detection with stability check

- state: **closed** (merged)
- base: `main` ← head: `fix/activity-detection-stability`
- author: @NimoRotem  •  created: 2026-03-26T19:18:28Z  •  closed: 2026-03-26T19:18:30Z
- merge commit: `fcfe5978b906771e45db4e523d2e10f2c52cad74`

## Summary
- Expanded `COMPLETION_RE` to catch Claude's verb-duration completion messages (e.g. `✻ Sautéed for 4m 47s`) that were falsely triggering busy detection
- Removed `·` (middle dot) from `SPINNER_ICONS` — it appears in normal Claude output text as a separator
- Added **content stability detection**: hashes pane content between polls; if unchanged for 20+ seconds with no active interrupt prompt, overrides to idle. Real work produces output and real spinners animate — static content means the session is done.

## Test plan
- [ ] Open knowva.ai/tmux/ and verify sessions with completion messages like "✻ Sautéed for 4m 47s" show as idle
- [ ] Verify actively working sessions (with real spinners/thinking) still show as busy
- [ ] Wait 20+ seconds after a session finishes — verify content stability check kicks in

## #4 — Add hysteresis to prevent activity status flickering

- state: **closed** (merged)
- base: `main` ← head: `fix/activity-detection-hysteresis`
- author: @NimoRotem  •  created: 2026-03-28T19:06:40Z  •  closed: 2026-03-28T19:06:42Z
- merge commit: `8a621f64ac48cc14bf5f28d45423e17c12b23685`

## Summary
- Wrapped `detect_activity()` with asymmetric debounce to prevent rapid busy/idle flickering
- **idle → busy**: immediate (1 reading) — work starts are detected instantly  
- **busy → idle**: requires 3 consecutive idle readings (~30s at 10s polling interval)
- This filters transient idle blips that happen between Claude Code tool calls or streaming chunks

## Test plan
- [ ] Open knowva.ai/tmux/ with an active session — status should stay "busy" without blips
- [ ] When a session finishes, it should switch to "idle" after ~30 seconds of consistent idle signals
- [ ] Starting new work should immediately flip to "busy"

## #5 — Major UX overhaul: 8 dashboard improvements

- state: **closed** (merged)
- base: `main` ← head: `feature/dashboard-ux-overhaul`
- author: @NimoRotem  •  created: 2026-04-01T02:11:03Z  •  closed: 2026-04-01T02:11:24Z
- merge commit: `9c3fff7a694902bfca20dd219cf85dfefb49192c`

## Summary

- **Collapsible key bar** — terminal keys (Esc, Ctrl+C, etc.) and slash commands (/clear, /cost, /usage...) unified in a single toggle bar, available in both Terminal and Chat tabs
- **Terminal first** — renamed "Raw Output" to "Terminal", made it the default tab
- **Compact nav** — session tabs show only names (e.g. "tmux", "worker2"), no LLM titles
- **Fast startup** — new `/api/sessions-fast` endpoint returns cached data instantly; LLM summaries load in background
- **Smoother terminal** — 1s polling (was 2s), smooth scroll for deltas, instant scroll positioning on full loads
- **Auth mode toggle** — per-session Subscription/API Key switch in Info tab, reads key from ~/CLAUDE.md as fallback
- **Resizable terminal** — drag handle below terminal output, height persisted in localStorage
- **Better chat summaries** — prompt includes last 10 messages for context, focuses on URLs/IPs/errors/actions, 2x token budget

Also adds `/api/sessions/{name}/send-keys` for raw terminal key input (Escape, Ctrl+C, etc.).

## Test plan

- [ ] Load dashboard — should render within ~1 second
- [ ] Terminal tab is default, auto-refreshes at 1s
- [ ] Switching sessions starts at bottom (no scroll animation)
- [ ] Nav shows only session names
- [ ] "Keys & Commands" bar is collapsed, expands on click
- [ ] Slash commands work from the key bar
- [ ] Drag resize handle on terminal output
- [ ] Toggle auth mode in Info tab
- [ ] Chat summaries include concrete details

## #6 — Major update: autonomous modes, performance, and UI

- state: **closed** (merged)
- base: `main` ← head: `feature/autonomous-modes-and-ui-improvements`
- author: @NimoRotem  •  created: 2026-04-02T20:22:19Z  •  closed: 2026-04-02T20:22:39Z
- merge commit: `55da94a4c5425db2b99ddfa7b28cfe5d8b977402`

## Summary
- **Away Mode**: Autonomous skill-based task execution with phased prompts (study → select → execute → report)
- **Go Nuts Mode**: Continuous build/feature generation loop that monitors idle sessions and sends build pings
- **Watchdog**: Background monitor that detects stalled/zombie autonomous sessions and auto-restarts them
- **Persistence**: Autonomous mode toggle state survives server restarts via JSON state file
- **Performance**: All `subprocess.run()` calls wrapped in `asyncio.to_thread()` with 20-worker thread pool — eliminates 28-37s API response spikes, now consistent 200-700ms
- **Terminal display**: Stable line counting (`history_size + pane_height`) and hardened frontend delta dedup to prevent duplicate lines
- **Mobile UI**: Terminal half-height viewport, expand button removed, upload moved to collapsible keys panel, Stop button moved to top controls bar
- **Info tab**: Refresh endpoint now populates all fields after restart, parallel lazy refresh for faster initial load
- **Misc**: Idle status simplified, bracketed paste toggle, CLAUDE.md editor overlay

## Test plan
- [x] Verify dashboard loads on all servers
- [x] Toggle Away Mode on/off, verify persistence across restart
- [x] Toggle Go Nuts Mode on/off, verify no re-enable on reload
- [x] Check terminal tab loads in <1s (no blocking spikes)
- [x] Verify Info tab populates after server restart
- [x] Mobile: verify terminal is half-height, only Send button visible in cmd-bar

## #7 — Add OOM/crash recovery for autonomous modes

- state: **closed** (merged)
- base: `main` ← head: `feature/oom-crash-recovery`
- author: @NimoRotem  •  created: 2026-04-03T04:52:51Z  •  closed: 2026-04-03T04:52:56Z
- merge commit: `7199ab559ae4fc120f24b2b1174011f0760eccbc`

## Summary
- Adds Claude Code process detection (`_is_claude_running()`) to distinguish OOM'd/crashed sessions (bare shell) from live Claude Code
- Auto-restarts Claude Code when crash detected, before sending any prompts
- Watchdog now detects dead Claude during stall checks and triggers immediate recovery
- All continuous loops (away mode + go nuts) verify Claude is alive before each ping cycle
- Periodic state saves after each successful cycle ensure state survives unexpected crashes

## Context
Claude Code sessions were crashing with JS heap OOM errors during long autonomous mode runs. When this happened, the tmux pane fell back to bash, but the dashboard couldn't distinguish this from Claude being idle — it would keep sending prompts to the bare shell or endlessly loop.

## Test plan
- [ ] Verify dashboard starts cleanly on all 3 servers
- [ ] Toggle away mode on a session, kill Claude Code manually (`kill -9`), verify watchdog detects and restarts it
- [ ] Verify state persists across dashboard restarts with autonomous modes active

## #8 — Revert OOM/crash recovery (broke things)

- state: **closed** (merged)
- base: `main` ← head: `revert/oom-crash-recovery`
- author: @NimoRotem  •  created: 2026-04-03T23:20:09Z  •  closed: 2026-04-03T23:20:17Z
- merge commit: `9b6926656c79b0c28c6afbc4ba151b1b5a0cf763`

## Summary
- Reverts PR #7 (OOM/crash recovery for autonomous modes)
- The changes in that PR broke the tmux dashboard

## Test plan
- [ ] Verify knowva.ai/tmux loads correctly
- [ ] Verify pane activity detection works
- [ ] Verify autonomous modes function without the crash recovery logic

## #9 — Revert to stable PR#4 state (remove go nuts + filter sessions)

- state: **closed** (merged)
- base: `main` ← head: `revert/to-pr4-stable`
- author: @NimoRotem  •  created: 2026-04-03T23:25:32Z  •  closed: 2026-04-03T23:25:38Z
- merge commit: `356540e590bffe4b08364f5d92d3196b73f96c5e`

## Summary
- Restores app.py to the state after PR #4 (hysteresis fix) — last known good version
- Removes PR #5: compact nav / filter sessions UX overhaul
- Removes PR #6: go nuts mode, away mode, autonomous modes, OOM recovery

## Test plan
- [ ] knowva.ai/tmux loads correctly
- [ ] Session nav shows sessions with titles
- [ ] Terminal tab works
- [ ] No autonomous mode buttons present

## #10 — Sync app.py from production and add CLAUDE.md

- state: **closed** (closed without merge)
- base: `main` ← head: `sync-app-and-claude-md`
- author: @NimoRotem  •  created: 2026-04-27T02:48:09Z  •  closed: 2026-04-27T02:51:00Z
- merge commit: `ea128424b8c9d861f52065ac72ff25802b91b36e`

## Summary
- Sync app.py from /home/nimrod_rotem/tmux-dashboard/app.py (production): -1679/+674 lines after dead-code/legacy cleanup
- Add CLAUDE.md with architecture, deploy, and auth notes

## Test plan
- [ ] Diff app.py against running production at rotem.ai/build/ to confirm parity
- [ ] Verify CLAUDE.md reflects current supervisor service name and port

## #11 — Add /tmp watchdog to prevent tmpfs exhaustion

- state: **closed** (merged)
- base: `main` ← head: `add-tmp-watchdog`
- author: @NimoRotem  •  created: 2026-05-14T03:39:07Z  •  closed: 2026-05-14T03:39:19Z
- merge commit: `61ba8e6282940715326d4f8fc8fdb5e02ae10e3d`

## Summary
- `/tmp` on this host is a 512MB tmpfs; when it fills, bash and any tool that writes temp files silently fail with exit code 1 and no output.
- Adds `_tmp_watchdog_loop()` to the app lifespan alongside the existing watchdogs. It polls `shutil.disk_usage` every 120s and prunes stale entries when usage crosses 75% (1h age cutoff) or 90% (10m age cutoff).
- Protected prefixes are never touched: `.X11-unix`, `.ICE-unix`, `claude-*`, `tsx-*`, `tmux-*`, `systemd-*`, `node-compile-cache`, `data-gym-cache`, `vscode-*`. Files owned by other users are also skipped.

## Test plan
- [x] `python3 -m py_compile app.py` succeeds
- [x] Restarted via supervisor; log shows `[tmp_watchdog] INFO: Tmp watchdog started — interval=120s warn=75% critical=90%`
- [ ] Force `/tmp` above 75% with throwaway files and confirm cleanup runs without touching protected dirs

## #12 — fix: upload-delete X button + codax/claude session filtering

- state: **closed** (merged)
- base: `main` ← head: `fix/upload-delete-button`
- author: @NimoRotem  •  created: 2026-05-19T18:27:14Z  •  closed: 2026-05-19T18:27:28Z
- merge commit: `4e6bc7f39db353bb546d87f07ae8b45317936756`

## Summary
- Rebuild uploaded-file list via DOM APIs with direct `addEventListener` bindings instead of inline `onclick` strings. Inline onclick was silently failing for at least one user (zero `DELETE /api/sessions/*/uploads/*` hits across many uploads). The new version also gives immediate visual feedback (X → "…") and surfaces fetch failures via chat bubbles + console warnings.
- Filter tmux sessions whose pane is running codex out of the claude dashboard (they belong to the codax dashboard at /codax). Checks `/proc/<pid>/comm` for pane descendants; 5s cache.
- Picks up pre-existing uncommitted work from prior sessions: skill-roles UI, qa-output static serve, profile env re-export, output-signature caching.

## Companion nginx fix (not in this repo)
The upload-delete fix required an nginx change on instance-3 to actually work end-to-end. The regex `location ~ ^/tmux/(.+\.md)$` was catching `DELETE /tmux/api/sessions/X/uploads/foo.md` and serving as static → 405. Fixed by inserting a higher-priority `location ^~ /tmux/api/` block before the .md regex in `/etc/nginx/sites-available/knowva.ai.conf`. Same pattern exists for `/codax/` — audit if codax adds DELETE endpoints with file-extension paths.

## Test plan
- [x] Backend `DELETE /api/sessions/{name}/uploads/{filename}` returns `{"ok":true}` via curl on 127.0.0.1:8501
- [x] End-to-end `DELETE https://knowva.ai/tmux/api/sessions/knowva/uploads/__nginx_delete_test__.md` returns 200; file removed from disk
- [x] Frontend X button now triggers the fetch (confirmed by user's browser console errors before the nginx fix)
- [x] Python `ast.parse` + `py_compile` pass on app.py
- [x] Served JS passes `node --check`
- [ ] Builder mirror (rotem.ai/build/) updated after merge

