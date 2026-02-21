# Vibe Coder

A multi-user web app that lets anyone build websites and apps by chatting with Claude Code. Each user gets an isolated workspace with a public URL — describe what you want, AI builds it, and it's instantly live.

Live at **https://dianao.tech/**

![Python 3.9+](https://img.shields.io/badge/python-3.9+-blue.svg)
![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)

## Features

- **Chat to build** — Users describe what they want in plain English and Claude Code writes, runs, and deploys it
- **One session per user** — Each user gets a single isolated tmux session with Claude Code ready to go
- **Public URLs** — Everything a user builds is served at `dianao.tech/username/`
- **User isolation** — Separate workspace directories, no cross-user visibility
- **Multi-user auth** — Signup/login with SQLite-backed accounts and PBKDF2 password hashing
- **AI-powered summaries** — Three-tier LLM summaries (project, progress, realtime) using GPT-4o-mini
- **Chat interface** — Send messages to Claude Code through a chat UI with AI-generated status updates
- **Terminal view** — Raw terminal output tab for direct command access
- **File upload** — Upload files directly to the workspace via the chat bar
- **Auto-approve** — Automatically handles Claude Code permission prompts
- **Activity detection** — Live busy/idle status with spinner detection
- **Dynamic favicon** — Tab icon changes color based on session status
- **Security** — Blocks dotfiles, CLAUDE.md, .git, node_modules from public serving; prevents path traversal

## How It Works

1. User signs up at `dianao.tech/signup` — picks a username (becomes their URL)
2. A workspace directory is created at `/var/vibe-coder/users/<username>/`
3. A tmux session launches with Claude Code in the workspace
4. User types instructions in the chat — Claude Code builds the project
5. Files in the workspace are served at `dianao.tech/<username>/`

## Quick Start

### Prerequisites

- Python 3.9+
- tmux
- Claude Code CLI (`claude`)
- An OpenAI API key (for LLM summaries)

### Install

```bash
pip install -r requirements.txt
```

### Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | (required) | OpenAI API key for LLM summaries |
| `VIBE_PORT` | `8501` | Server port |
| `VIBE_SECRET` | (auto) | Cookie signing secret (set for persistent sessions across restarts) |
| `VIBE_NEW_SESSION_CMD` | `claude --dangerously-skip-permissions` | Command to run in new tmux sessions |
| `VIBE_USERS_DIR` | `/var/vibe-coder/users` | Base directory for user workspaces |
| `VIBE_DATA_DIR` | `/var/vibe-coder/data` | Database and message storage |
| `VIBE_DOMAIN` | `dianao.tech` | Domain for public URLs |

### Run

```bash
export OPENAI_API_KEY="sk-..."
python3 app.py
```

### Production (supervisor + nginx)

See `supervisor-vibe-coder.conf` and `nginx-vibe-coder.conf` for example configs.

## Architecture

```
Browser  -->  nginx (dianao.tech)  -->  FastAPI (uvicorn :8501)
                                             |
                                             +-- SQLite (users DB)
                                             +-- tmux CLI (per-user sessions)
                                             +-- OpenAI API (summaries)
                                             +-- /var/vibe-coder/users/<username>/
                                                  (public file serving)
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| `GET` | `/` | Landing page |
| `GET/POST` | `/login` | Login |
| `GET/POST` | `/signup` | Signup |
| `GET` | `/logout` | Logout |
| `GET` | `/app` | Dashboard (auth required) |
| `GET` | `/api/session` | Get user's session data with summaries |
| `GET` | `/api/status` | Lightweight activity poll |
| `POST` | `/api/session/refresh` | Regenerate realtime summary |
| `POST` | `/api/session/refresh-all` | Regenerate all three tiers |
| `GET` | `/api/session/raw` | Raw terminal scrollback |
| `POST` | `/api/session/send` | Send command to session |
| `POST` | `/api/session/upload` | Upload file to workspace |
| `GET` | `/{username}/{path}` | Serve user's public files |

## License

MIT
