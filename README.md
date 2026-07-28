# Claude tmux Manager

A single-file FastAPI web app that turns every `tmux` session on your box into a fully manageable workspace in the browser — built specifically for orchestrating multiple concurrent **Claude Code** sessions.

![Claude tmux Manager dashboard](screenshots/tmux-screenshot.png)

## What it does

Claude Code runs beautifully inside a `tmux` session, but once you start juggling five or ten of them across different projects, the terminal stops scaling. This dashboard gives you:

- A **tabbed view of every session** with live terminal output and a parallel chat transcript
- **AI-generated titles, descriptions, and progress summaries** (OpenAI) so you know what each session is doing at a glance
- **System stats**, per-session cost, token usage, idle detection, context-window warnings, and activity sparklines
- **Keyboard-first navigation** — rename, snooze, duplicate, reorder, mark-done, send-to-all, interrupt, cycle sessions, and more
- **File upload**, **CLAUDE.md viewer/editor** (home-dir-scoped, path-traversal protected), **sticky notes**, **message bookmarks**, **quick-reply templates**, toast notifications, and sound alerts
- **Hardened auth** — HMAC session cookie, rate-limited login, CSP/HSTS/Permissions-Policy headers, session-name validation before any shell call

It's a single Python file with no database — everything persists as JSON under `~/.tmux-dashboard/`.

## Prerequisites

- Python 3.9+
- `tmux` installed and on `PATH`
- OpenAI API key (optional — required for LLM summaries)
- Nginx or another reverse proxy (recommended for HTTPS)

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Run the server
TMUX_DASH_USER=admin TMUX_DASH_PASS=yourpassword python3 app.py
```

The dashboard is then available at `http://localhost:8501/`.

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TMUX_DASH_USER` | No | `admin` | Login username |
| `TMUX_DASH_PASS` | **Yes (in production)** | *(empty)* | Login password. **If unset, auth is disabled — all endpoints are publicly accessible.** |
| `TMUX_DASH_SECRET` | No | *(random on start)* | HMAC secret for session tokens. Set a stable value to survive restarts. |
| `OPENAI_API_KEY` | No | *(none)* | OpenAI key for LLM-generated titles, descriptions, and summaries. Without it, LLM features are disabled. |
| `TMUX_DASH_ROOT` | No | `/tmux` | URL root path when served behind a reverse proxy sub-path. |
| `PORT` | No | `8501` | TCP port to listen on. |

> **Security note**: Always set `TMUX_DASH_PASS` in production. Without it, the auto-respond and send-command endpoints can execute arbitrary keystrokes in any tmux session.

## Data Storage

All persistent data is stored in `~/.tmux-dashboard/` (permissions: `700`):

| File | Description |
|------|-------------|
| `messages.json` | Per-session chat message history |
| `notes.json` | AI-extracted session notes |
| `anthropic_api_key` | Encrypted-at-rest API key (chmod 600) |

## Running via Supervisor

Example `/etc/supervisor/conf.d/tmux-dashboard.conf`:

```ini
[program:tmux-dashboard]
command=python3 /home/youruser/tmux-dashboard/app.py
directory=/home/youruser/tmux-dashboard
user=youruser
environment=TMUX_DASH_PASS="%(ENV_TMUX_DASH_PASS)s",TMUX_DASH_SECRET="%(ENV_TMUX_DASH_SECRET)s"
autostart=true
autorestart=true
stdout_logfile=/var/log/tmux-dashboard.log
stderr_logfile=/var/log/tmux-dashboard.log
```

## Nginx Configuration

```nginx
location /tmux/ {
    proxy_pass http://127.0.0.1:8501;
    proxy_set_header X-Forwarded-Proto $scheme;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header Host $host;
}
```

The `X-Forwarded-Proto: https` header is required for the `Strict-Transport-Security` and `Secure` cookie flag to activate.

## Development

```bash
# Run tests
make test

# Lint
make lint

# Lint + auto-fix
make lint-fix

# All common commands
make help
```

## Backup & Recovery

All persistent session data lives in `~/.tmux-dashboard/`. Back it up regularly:

```bash
# Backup data directory
make backup-data

# Verify JSON files are not corrupted
make restore-check

# Backup app.py before risky changes
make backup
```


**Corrupted state**: If `~/.tmux-dashboard/*.json` becomes corrupt (server crash during write), restore from a backup. You can also delete the corrupt file — the app recreates it on next write with empty state.

**Secret rotation**: If `TMUX_DASH_SECRET` changes, all existing auth cookies become invalid. Users will be redirected to the login page. This is intentional — rotate by restarting with a new secret.

## Upgrading openai SDK (v1 → v2)

> **Status**: Deferred. openai 2.x is a major version with breaking changes. Human review required before upgrading.

Current usage in `app.py` (all standard, minimal API surface):

- `openai.AsyncOpenAI(api_key=...)` — client init
- `client.chat.completions.create(model, messages, max_tokens, temperature)` — single call pattern
- `resp.choices[0].message.content` — response access
- `resp.usage.total_tokens` — token counting

**Migration steps** (when ready):

1. Install: `pip install openai==2.*`
2. Run tests: `make test` (expect failures if any API changed)
3. Check openai v2 migration guide for any response schema changes
4. Verify `resp.usage` attribute names (may be `completion_tokens` vs `total_tokens`)
5. Check error types — `openai.OpenAIError` subclasses may have changed
6. Re-run tests and fix any failures before deploying

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/sessions-fast` | Session list (cached, no LLM calls) — used by the UI |
| GET | `/api/status` | Per-session activity status only (lightweight) |
| GET | `/api/stats` | System stats (CPU, memory, disk, uptime) |
| GET | `/api/health` | Health check (tmux, OpenAI key, data directory accessibility) |
| POST | `/api/sessions/create` | Create a new tmux session |
| DELETE | `/api/sessions/{name}` | Kill a tmux session |
| POST | `/api/sessions/{name}/refresh` | Force LLM refresh for one session |
| POST | `/api/sessions/{name}/send` | Send a command to a session |
| POST | `/api/sessions/{name}/interrupt` | Send Escape key to a session |
| POST | `/api/sessions/{name}/send-keys` | Send raw tmux key sequences |
| POST | `/api/sessions/{name}/upload` | Upload a file to the session's CWD |
| GET/POST | `/api/sessions/{name}/claude-md` | View/edit CLAUDE.md files |
| GET | `/api/sessions/{name}/stats` | Token usage and cost stats |
| POST | `/api/auth/api-key` | Store/clear Anthropic API key |
| GET | `/api/auth/claude-status` | Claude Code OAuth status |
| POST | `/api/auth/logout` | Revoke Claude Code OAuth session |
| GET | `/api/auth/usage` | Today's Claude Code token usage |

---

## PGS Pipeline Tooling (23andClaude)

This repo also contains tooling scripts used to manage the **Polygenic Score (PGS) pipeline** for the [23andClaude](https://github.com/NimoRotem/23andClaude) genomics app running on `genom-beast-gpu` (34.135.47.236). These scripts live here because they were developed and run from the tmux dashboard's Claude Code sessions.

### Background

The 23andClaude app computes Polygenic Risk Scores for uploaded genome files (VCF/BAM/CRAM). Each PGS test:

1. **Scores** the sample using `plink2 --score` against a PGS Catalog scoring file
2. **Computes a percentile** by comparing the sample's score against a reference population distribution (1000 Genomes Phase 3, EUR ancestry, GRCh38 build)
3. **Reports** the result with a confidence level and optional AI interpretation

The percentile step requires **precomputed reference statistics** (mean and standard deviation of scores across 633 EUR samples). Without these stats, the percentile cannot be reliably computed.

### The Problem We Solved

The pipeline originally had two methods for computing percentiles:

- **Precomputed stats** — Fixed EUR reference mean/std from pre-scored 1000 Genomes data. Stable and reproducible.
- **Dynamic scoring** — Re-scored the 1000G panel on-the-fly using only the variants that matched the sample. Unstable: the same sample could get different percentiles across runs because the reference distribution changed with each variant subset.

We overhauled the pipeline in 6 patches:

1. **Audit** (`pgs_stats_audit.py`) — Inventoried all 269 unique PGS IDs, classified each as `precomputed_ok`, `precomputed_stale`, or `missing` based on available reference stats files
2. **Hard-fail dynamic scoring** — Removed the unstable dynamic fallback from `_compute_percentile()`. PGS without precomputed stats now return `method=unavailable` instead of unreliable percentiles
3. **Test gating** — PGS tests without valid stats are disabled at startup via an audit overlay. Disabled tests are hidden from the UI and skipped in "Run All"
4. **Confidence tagging** — Every PGS result now includes `confidence: "high"|"low"` with reasons (missing stats, low match rate, build mismatch, sanity gates)
5. **UI confidence display** — Low-confidence badge on results, details in report modal, AI interpretation explicitly warns about low-confidence results
6. **Registry rebuild** — Replaced the PGS test list with 270 curated entries across 10 categories

### Scripts

#### `generate_pgs_stats.py`

Generates precomputed EUR GRCh38 reference statistics for PGS IDs that lack them. Scores the full 1000 Genomes Phase 3 reference panel with each PGS scoring file.

```bash
# On genom-beast-gpu (34.135.47.236):
cd /home/nimrod_rotem/simple-genomics

# Generate stats for specific PGS IDs
python3 generate_pgs_stats.py PGS002012 PGS002231 PGS003573

# Generate stats for all IDs in the TARGET_PGS_IDS list
python3 generate_pgs_stats.py
```

**How it works:**
1. Downloads the PGS Catalog scoring file for each ID
2. Converts it to plink2 format with ref-panel-compatible variant IDs (`chr:pos:ref:alt`), emitting both allele orientations
3. Runs `plink2 --score` against the 1000G panel (3,202 samples total, 633 EUR)
4. Extracts EUR sample SCORE1_AVG values, computes mean/std/median/min/max
5. Saves to `/data/pgs2/ref_panel_stats/{pgs_id}_EUR_GRCh38.json`

**Key detail:** Reference panel variant IDs use the format `1:751133:C:CGT` (chr:pos:ref:alt). The script emits both `{chr}:{pos}:{oa}:{ea}` and `{chr}:{pos}:{ea}:{oa}` orientations so plink2 can match either one.

#### `rebuild_pgs_registry.py`

Parses `pgs_reorganized.md` and rebuilds the PGS section of `test_registry.py` on the genomics server.

```bash
# On genom-beast-gpu:
cd /home/nimrod_rotem/simple-genomics
python3 rebuild_pgs_registry.py
```

Replaces everything between `# -- PGS - Cancer` and `# -- Monogenic` in `test_registry.py` with entries parsed from the markdown. Backs up the original, runs syntax check and import verification, and rolls back if anything fails.

#### `pgs_stats_audit.py` (on remote server)

Audits all PGS tests against available precomputed stats. Produces `pgs_stats_audit.json` which the app loads at startup to gate tests.

```bash
# On genom-beast-gpu:
cd /home/nimrod_rotem/simple-genomics
python3 pgs_stats_audit.py
```

### Data Files

| File | Description |
|------|-------------|
| `pgs_reorganized.md` | 270 curated PGS test definitions across 10 categories, in markdown format |
| `pgs_curated_list.md` | Compact reference list of high-impact PGS IDs with traits |

### Current Status (April 2026)

- **270 PGS tests** registered across 10 categories
- **41 PGS IDs** have precomputed EUR GRCh38 reference stats (`precomputed_ok`)
- **228 PGS IDs** still missing stats — these are disabled in the UI until stats are generated
- Stats generation takes ~2-5 minutes per PGS ID using `generate_pgs_stats.py`

### Architecture

```
genom-beast-gpu (34.135.47.236)
├── /home/nimrod_rotem/simple-genomics/
│   ├── app.py              — FastAPI web app (UI + API)
│   ├── runners.py          — PGS scoring pipeline + _compute_percentile()
│   ├── test_registry.py    — 370 tests (270 PGS + 14 rsID + 86 non-PGS)
│   ├── pgs_stats_audit.py  — Audit script → pgs_stats_audit.json
│   ├── generate_pgs_stats.py — Reference stats generator
│   └── rebuild_pgs_registry.py — Registry rebuilder from markdown
├── /data/pgs2/
│   ├── ref_panel_stats/    — 48 precomputed stats JSON files
│   └── scoring_files/      — Downloaded PGS Catalog scoring files
└── /data/pgs2/ref_panel/   — 1000G Phase 3 plink2 pgen/pvar/psam files
```

## License

MIT — see source header in `app.py` for attribution.
