# Claude tmux Manager

A real-time web dashboard for monitoring and interacting with tmux sessions. Built for teams running AI coding agents (like Claude Code) across multiple terminals — see what each session is doing at a glance, send commands, and get AI-generated summaries of activity.

![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

## Features

- **Live session monitoring** — See all tmux sessions with busy/idle status indicators that update every 10 seconds
- **AI-powered summaries** — Three-tier LLM summaries using GPT-4o-mini:
  - **Project** — What this session is working on
  - **Progress** — What's been accomplished so far
  - **Realtime** — What's happening right now
- **Chat interface** — Send commands to terminals through a chat-style UI with message history
- **Session management** — Create and delete tmux sessions from the browser
- **Activity detection** — Recognizes Claude Code's spinner states, shell prompts, progress indicators, and "esc to interrupt" signals
- **Persistent chat history** — Messages survive page reloads and server restarts
- **Password protected** — Cookie-based login with configurable credentials
- **Mobile friendly** — Responsive design that works on phones and tablets
- **Single file** — The entire app is one Python file with embedded HTML/CSS/JS

## Quick Start

### Prerequisites

- Python 3.9+
- tmux
- An OpenAI API key

### Install

```bash
git clone https://github.com/NimoRotem/Claude_tmux_manager.git
cd Claude_tmux_manager

pip install -r requirements.txt

cp .env.example .env
# Edit .env with your OpenAI API key and desired credentials
```

### Run

```bash
# Load environment variables and start
export $(grep -v '^#' .env | xargs)
python3 app.py
```

The dashboard will be available at `http://localhost:8501/tmux/`.

### Run with systemd / supervisor (production)

Copy and edit the included supervisor config:

```bash
sudo cp supervisor.conf /etc/supervisor/conf.d/tmux-dashboard.conf
sudo nano /etc/supervisor/conf.d/tmux-dashboard.conf
# Update: paths, user, and environment variables (API key, credentials)

sudo supervisorctl reread
sudo supervisorctl update
```

### Reverse proxy (nginx)

Add this to your nginx site config:

```nginx
location /tmux/ {
    proxy_pass http://127.0.0.1:8501/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

To serve at a different path, also set `TMUX_DASH_ROOT_PATH` to match:

```bash
TMUX_DASH_ROOT_PATH=/my-dashboard
```

## Configuration

All configuration is via environment variables:

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (required) | OpenAI API key for LLM summaries |
| `TMUX_DASH_USER` | `admin` | Login username |
| `TMUX_DASH_PASS` | (empty) | Login password (empty = no auth) |
| `TMUX_DASH_SECRET` | (auto) | Cookie signing secret. Auto-generated if not set, but login sessions will invalidate on server restart |
| `TMUX_DASH_PORT` | `8501` | Server port |
| `TMUX_DASH_ROOT_PATH` | `/tmux` | URL base path (must match your reverse proxy config) |
| `TMUX_DASH_NEW_SESSION_CMD` | (empty) | Command to auto-run in new sessions (e.g. `claude`) |

## How It Works

### Activity Detection

The dashboard reads the bottom of each tmux pane to determine session state:

- **Busy** — Detected via spinner patterns (`Generating...`, `Thinking...`), the `esc to interrupt` status bar, or active progress indicators
- **Idle** — Detected via shell prompts (`$`, `>`, `❯`), Claude Code completion messages (`Sautéed for Xs`), or tip text
- **Unknown** — When neither busy nor idle patterns match

### LLM Summaries

Three independent summary tiers are cached with different TTLs:

- **Project description** — Generated once per session, never auto-expires
- **Progress** — Regenerated every 10 minutes
- **Realtime** — Regenerated every 60 seconds and on status changes (busy→idle)

The realtime summary feeds into the chat as assistant messages. A similarity-based dedup (>70% word overlap) prevents near-identical messages from piling up on repeated refreshes.

### Chat Messages

User commands sent through the chat input are:
1. Sent to the tmux session via `tmux send-keys`
2. Stored in the chat history as user messages
3. Persisted to `~/.tmux-dashboard/messages.json`

AI responses are appended when the realtime summary updates (typically after a busy→idle transition).

## Architecture

```
Browser  ──→  nginx (/tmux/)  ──→  FastAPI (uvicorn :8501)  ──→  tmux
                                        │
                                        ├── OpenAI API (summaries)
                                        ├── tmux CLI (capture-pane, send-keys)
                                        └── ~/.tmux-dashboard/messages.json
```

Single-file architecture: `app.py` contains the FastAPI backend, all HTML/CSS/JS (embedded as a template string), and the LLM integration. No build step, no frontend toolchain, no database.

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Dashboard UI |
| `POST` | `/login` | Authenticate (form POST) |
| `GET` | `/api/sessions` | List all sessions with summaries |
| `GET` | `/api/status` | Lightweight status poll (no LLM calls) |
| `POST` | `/api/sessions/{name}/refresh` | Regenerate realtime summary |
| `POST` | `/api/sessions/{name}/refresh-all` | Regenerate all three tiers |
| `GET` | `/api/sessions/{name}/raw` | Raw terminal scrollback |
| `POST` | `/api/sessions/{name}/send` | Send command to session |
| `POST` | `/api/sessions/create` | Create new tmux session |
| `DELETE` | `/api/sessions/{name}` | Kill a tmux session |

## License

MIT
