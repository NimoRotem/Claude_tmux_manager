from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Dict, Optional

from fastapi import FastAPI, Request, Response, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from pydantic import BaseModel
import openai
import uvicorn

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PORT = int(os.environ.get("VIBE_PORT", "8501"))
NEW_SESSION_CMD = os.environ.get("VIBE_NEW_SESSION_CMD", "claude --dangerously-skip-permissions")
AUTH_SECRET = os.environ.get("VIBE_SECRET", secrets.token_hex(32))
USERS_BASE = os.environ.get("VIBE_USERS_DIR", "/var/vibe-coder/users")
DATA_DIR = os.environ.get("VIBE_DATA_DIR", "/var/vibe-coder/data")
DOMAIN = os.environ.get("VIBE_DOMAIN", "dianao.tech")

# Ensure directories exist
Path(USERS_BASE).mkdir(parents=True, exist_ok=True)
Path(DATA_DIR).mkdir(parents=True, exist_ok=True)

client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

app = FastAPI()

# ---------------------------------------------------------------------------
# Database — SQLite for user accounts
# ---------------------------------------------------------------------------
DB_PATH = os.path.join(DATA_DIR, "vibe-coder.db")


def _get_db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            salt TEXT NOT NULL,
            created_at REAL NOT NULL
        )
    """)
    conn.commit()
    conn.close()


init_db()


# ---------------------------------------------------------------------------
# Password hashing (PBKDF2 — stdlib, no extra deps)
# ---------------------------------------------------------------------------
def _hash_password(password: str, salt: str = "") -> tuple[str, str]:
    if not salt:
        salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return dk.hex(), salt


def _verify_password(password: str, stored_hash: str, salt: str) -> bool:
    dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 200_000)
    return hmac.compare_digest(dk.hex(), stored_hash)


# ---------------------------------------------------------------------------
# Token auth (cookie-based HMAC)
# ---------------------------------------------------------------------------
def _make_token(username: str) -> str:
    sig = hmac.new(AUTH_SECRET.encode(), username.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{username}:{sig}"


def _check_token(token: str) -> Optional[str]:
    """Return username if valid, else None."""
    if not token or ":" not in token:
        return None
    username, sig = token.split(":", 1)
    expected = hmac.new(AUTH_SECRET.encode(), username.encode(), hashlib.sha256).hexdigest()[:24]
    if hmac.compare_digest(sig, expected):
        return username
    return None


def get_current_user(request: Request) -> Optional[str]:
    token = request.cookies.get("vibe_auth")
    return _check_token(token)


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------
def create_user(username: str, password: str) -> bool:
    """Create a new user. Returns True on success, False if username taken."""
    pw_hash, salt = _hash_password(password)
    try:
        conn = _get_db()
        conn.execute(
            "INSERT INTO users (username, password_hash, salt, created_at) VALUES (?, ?, ?, ?)",
            (username, pw_hash, salt, time.time()),
        )
        conn.commit()
        conn.close()
    except sqlite3.IntegrityError:
        return False

    # Create user directory
    user_dir = os.path.join(USERS_BASE, username)
    Path(user_dir).mkdir(parents=True, exist_ok=True)

    # Write a CLAUDE.md so Claude Code knows the context
    claude_md = os.path.join(user_dir, "CLAUDE.md")
    if not os.path.exists(claude_md):
        Path(claude_md).write_text(
            f"# Vibe Coder Workspace\n\n"
            f"You are building a project for user '{username}'.\n"
            f"Everything in this directory is served publicly at: https://{DOMAIN}/{username}/\n\n"
            f"## Rules\n"
            f"- Put your web-facing files (HTML, CSS, JS, images) in this directory\n"
            f"- An index.html in this directory will be the homepage at https://{DOMAIN}/{username}/\n"
            f"- You can create subdirectories for organization\n"
            f"- Do NOT access files outside this directory\n"
            f"- Do NOT run commands that affect the server (no sudo, no systemctl, etc.)\n"
            f"- Focus on building the user's project\n"
        )
    return True


def authenticate_user(username: str, password: str) -> bool:
    conn = _get_db()
    row = conn.execute("SELECT password_hash, salt FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    if not row:
        return False
    return _verify_password(password, row["password_hash"], row["salt"])


def user_exists(username: str) -> bool:
    conn = _get_db()
    row = conn.execute("SELECT 1 FROM users WHERE username = ?", (username,)).fetchone()
    conn.close()
    return row is not None


def get_user_dir(username: str) -> str:
    return os.path.join(USERS_BASE, username)


# ---------------------------------------------------------------------------
# Auth middleware — protect /app and /api routes
# ---------------------------------------------------------------------------
PUBLIC_PATHS = frozenset({"/", "/login", "/signup", "/health"})
PUBLIC_PREFIXES = ("/static/",)


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    path = request.url.path

    # Public routes
    if path in PUBLIC_PATHS:
        return await call_next(request)
    for prefix in PUBLIC_PREFIXES:
        if path.startswith(prefix):
            return await call_next(request)
    # Login/signup POST
    if path in ("/login", "/signup"):
        return await call_next(request)

    # Protected routes: /app, /api/*
    if path.startswith("/app") or path.startswith("/api/"):
        user = get_current_user(request)
        if not user:
            return RedirectResponse(url="/login", status_code=303)
        request.state.username = user
        return await call_next(request)

    # Everything else: user public files (/<username>/...)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Tmux session management — one session per user
# ---------------------------------------------------------------------------
SESSION_PREFIX = "vc-"


def _session_name(username: str) -> str:
    return f"{SESSION_PREFIX}{username}"


def session_exists(username: str) -> bool:
    name = _session_name(username)
    try:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True, text=True, timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def create_user_session(username: str) -> bool:
    name = _session_name(username)
    user_dir = get_user_dir(username)
    Path(user_dir).mkdir(parents=True, exist_ok=True)

    if session_exists(username):
        return True

    try:
        result = subprocess.run(
            ["tmux", "new-session", "-d", "-s", name, "-c", user_dir],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return False

        # Launch the configured command (e.g. claude)
        if NEW_SESSION_CMD:
            subprocess.run(
                ["tmux", "send-keys", "-t", name, "-l", NEW_SESSION_CMD],
                capture_output=True, text=True, timeout=5,
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", name, "Enter"],
                capture_output=True, text=True, timeout=5,
            )
        return True
    except Exception:
        return False


def capture_pane_full(username: str) -> str:
    name = _session_name(username)
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p", "-S", "-"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def capture_pane_recent(username: str, lines: int = 80) -> str:
    name = _session_name(username)
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", name, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5,
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def get_session_cwd(username: str) -> str:
    name = _session_name(username)
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", name, "-p", "#{pane_current_path}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return get_user_dir(username)


# ---------------------------------------------------------------------------
# Activity detection
# ---------------------------------------------------------------------------
_auto_approve_sent: Dict[str, float] = {}


def _check_auto_approve(username: str, visible: str):
    name = _session_name(username)
    last = _auto_approve_sent.get(name, 0)
    if time.time() - last < 10:
        return

    lines = visible.split("\n")
    option2_line = -1
    selected_line = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.search(r'2\.\s+Yes.*bypass', stripped):
            option2_line = i
        if stripped.startswith('\u276f') or stripped.startswith('>'):
            selected_line = i

    if option2_line < 0 or selected_line < 0:
        return

    downs = option2_line - selected_line
    if downs < 0:
        return

    try:
        for _ in range(downs):
            subprocess.run(
                ["tmux", "send-keys", "-t", name, "Down"],
                capture_output=True, text=True, timeout=3,
            )
        subprocess.run(
            ["tmux", "send-keys", "-t", name, "Enter"],
            capture_output=True, text=True, timeout=3,
        )
        _auto_approve_sent[name] = time.time()
    except Exception:
        pass


def detect_activity(username: str) -> dict:
    name = _session_name(username)
    info = {"status": "unknown", "command": "", "detail": ""}
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", name, "-p",
             "#{pane_current_command}:#{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode != 0:
            return info

        parts = result.stdout.strip().split(":")
        cmd = parts[0] if parts else ""
        info["command"] = cmd

        try:
            vis = subprocess.run(
                ["tmux", "capture-pane", "-t", name, "-p"],
                capture_output=True, text=True, timeout=5,
            )
            visible = vis.stdout if vis.returncode == 0 else ""
        except Exception:
            visible = ""

        _check_auto_approve(username, visible)

        all_lines = visible.split("\n")
        while all_lines and not all_lines[-1].strip():
            all_lines.pop()

        bottom = all_lines[-6:] if len(all_lines) >= 6 else all_lines
        bottom_text = "\n".join(bottom)
        window = all_lines[-20:] if len(all_lines) >= 20 else all_lines

        has_esc_to_interrupt = "esc to interrupt" in bottom_text

        spinner_found = False
        spinner_label = ""
        for line in window:
            stripped = line.strip()
            m = re.match(r'^[✶✽\*☆◆●]\s+(\w+ing)[…\.]+', stripped)
            if m:
                if "Done" in stripped:
                    continue
                spinner_found = True
                verb = m.group(1)
                verb_labels = {
                    "Generating": "Generating",
                    "Processing": "Processing",
                    "Running": "Running",
                }
                spinner_label = verb_labels.get(verb, "Thinking")
                break
            if re.match(r'^[✶✽\*]\s+.*\d+s\s*·\s*↓', stripped):
                spinner_found = True
                spinner_label = "Processing"
                break

        if spinner_found:
            info["status"] = "busy"
            info["detail"] = spinner_label
            return info

        if has_esc_to_interrupt:
            info["status"] = "busy"
            info["detail"] = "Working"
            return info

        last_line = bottom[-1].strip() if bottom else ""
        shell_cmds = {"bash", "zsh", "sh", "fish", "tmux"}
        if cmd.lower() in shell_cmds:
            if re.search(r'[\$#%>]\s*$', last_line) or not last_line:
                info["status"] = "idle"
                info["detail"] = "Shell prompt"
            else:
                info["status"] = "busy"
                info["detail"] = cmd
        elif cmd.lower() in ("claude", "node"):
            info["status"] = "idle"
            info["detail"] = "Waiting for input"
        else:
            info["status"] = "busy"
            info["detail"] = cmd
    except Exception:
        pass
    return info


# ---------------------------------------------------------------------------
# LLM summaries (three-tier cache)
# ---------------------------------------------------------------------------
cache: Dict[str, dict] = {}

MESSAGES_DIR = Path(DATA_DIR) / "messages"
MESSAGES_DIR.mkdir(parents=True, exist_ok=True)


def _messages_file(username: str) -> Path:
    return MESSAGES_DIR / f"{username}.json"


def _load_user_messages(username: str) -> list:
    fp = _messages_file(username)
    try:
        if fp.exists():
            return json.loads(fp.read_text())
    except Exception:
        pass
    return []


def _save_user_messages(username: str, messages: list):
    try:
        _messages_file(username).write_text(json.dumps(messages))
    except Exception:
        pass


DESCRIPTION_TTL = 0
PROGRESS_TTL = 600
REALTIME_TTL = 60


async def llm_call(system_prompt: str, user_content: str, max_tokens: int = 200) -> str:
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"(error: {e})"


async def get_title_and_description(username: str, full_output: str) -> tuple:
    lines = full_output.split("\n")
    early = "\n".join(lines[:150])
    mid_start = len(lines) // 3
    middle = "\n".join(lines[mid_start:mid_start + 80])
    context = f"=== EARLIEST OUTPUT ===\n{early}\n\n=== MIDDLE ===\n{middle}"
    truncated = context[:4000]

    title_coro = llm_call(
        "Given terminal output, produce a SHORT title (3-6 words) naming the project. "
        "Return ONLY the title, no quotes.",
        f"workspace '{username}':\n\n{truncated}",
        max_tokens=30,
    )
    desc_coro = llm_call(
        "Summarize what this workspace is building. ONE short sentence. Be specific. "
        "Under 20 words. No filler.",
        f"workspace '{username}':\n\n{truncated}",
        max_tokens=60,
    )
    title, description = await asyncio.gather(title_coro, desc_coro)
    return title, description


async def get_progress(username: str, full_output: str) -> str:
    lines = full_output.split("\n")
    total = len(lines)
    slices = [("BEGINNING", "\n".join(lines[:60]))]
    if total > 200:
        q1 = total // 4
        slices.append(("QUARTER", "\n".join(lines[q1:q1 + 50])))
    if total > 300:
        mid = total // 2
        slices.append(("MIDDLE", "\n".join(lines[mid:mid + 50])))
    slices.append(("RECENT", "\n".join(lines[-60:])))
    context = "\n\n".join(f"=== {label} ===\n{text}" for label, text in slices)
    return await llm_call(
        "Summarize what was accomplished so far. 1-3 short sentences. "
        "Use 'we' and past tense. Under 40 words. No filler.",
        f"workspace '{username}' history:\n\n{context[:5000]}",
        max_tokens=100,
    )


async def get_realtime(username: str) -> str:
    recent = capture_pane_recent(username, 80)
    activity = detect_activity(username)
    hint = f"[Session is {activity['status'].upper()}"
    if activity["detail"]:
        hint += f" - {activity['detail']}"
    hint += "]"
    return await llm_call(
        "Summarize the CURRENT STEP. 1-2 sentences. Use 'we'. "
        "If BUSY: describe what's happening. If IDLE: what was accomplished. "
        "Under 30 words.",
        f"{hint}\n\nworkspace '{username}' latest:\n\n{recent[-3000:]}",
        max_tokens=100,
    )


def _msg_similarity(a: str, b: str) -> float:
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _append_assistant_msg(username: str, entry: dict, text: str, ts: float):
    msgs = entry.setdefault("messages", [])
    for m in reversed(msgs):
        if m["role"] == "assistant":
            if m["text"] == text or _msg_similarity(m["text"], text) > 0.7:
                return
            break
    msgs.append({"role": "assistant", "text": text, "ts": ts})
    _save_user_messages(username, msgs)


async def get_session_data(username: str, force_all: bool = False) -> dict:
    now = time.time()
    entry = cache.get(username, {})
    if "messages" not in entry:
        entry["messages"] = _load_user_messages(username)

    need_description = force_all or "description" not in entry
    need_progress = force_all or "progress" not in entry or (now - entry.get("progress_at", 0)) >= PROGRESS_TTL

    full_output = None
    if need_description or need_progress:
        full_output = capture_pane_full(username)

    tasks = {}
    if need_description and full_output:
        tasks["title_desc"] = get_title_and_description(username, full_output)
    if need_progress and full_output:
        tasks["progress"] = get_progress(username, full_output)
    if force_all or "realtime" not in entry or (now - entry.get("realtime_at", 0)) >= REALTIME_TTL:
        tasks["realtime"] = get_realtime(username)

    if tasks:
        results = await asyncio.gather(*tasks.values())
        result_map = dict(zip(tasks.keys(), results))
        if "title_desc" in result_map:
            title, description = result_map["title_desc"]
            entry["title"] = title
            entry["description"] = description
            entry["description_at"] = now
        if "progress" in result_map:
            entry["progress"] = result_map["progress"]
            entry["progress_at"] = now
        if "realtime" in result_map:
            entry["realtime"] = result_map["realtime"]
            entry["realtime_at"] = now
            _append_assistant_msg(username, entry, result_map["realtime"], now)

    cache[username] = entry
    return entry


def build_session_response(username: str, data: dict) -> dict:
    activity = detect_activity(username)
    return {
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "description_at": data.get("description_at", 0),
        "progress": data.get("progress", ""),
        "progress_at": data.get("progress_at", 0),
        "realtime": data.get("realtime", ""),
        "realtime_at": data.get("realtime_at", 0),
        "messages": data.get("messages", []),
        "activity_status": activity["status"],
        "activity_command": activity["command"],
        "activity_detail": activity["detail"],
        "public_url": f"https://{DOMAIN}/{username}/",
    }


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def landing_page():
    return LANDING_HTML


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML


@app.post("/login")
async def do_login(request: Request):
    form = await request.form()
    username = (form.get("username", "") or "").strip().lower()
    password = form.get("password", "") or ""
    if not username or not password:
        return RedirectResponse(url="/login?err=missing", status_code=303)
    if not authenticate_user(username, password):
        return RedirectResponse(url="/login?err=invalid", status_code=303)
    # Ensure tmux session exists
    create_user_session(username)
    token = _make_token(username)
    resp = RedirectResponse(url="/app", status_code=303)
    resp.set_cookie("vibe_auth", token, max_age=86400 * 30, httponly=True, samesite="lax")
    return resp


@app.get("/signup", response_class=HTMLResponse)
async def signup_page():
    return SIGNUP_HTML


@app.post("/signup")
async def do_signup(request: Request):
    form = await request.form()
    username = (form.get("username", "") or "").strip().lower()
    password = form.get("password", "") or ""

    if not username or not password:
        return RedirectResponse(url="/signup?err=missing", status_code=303)
    if len(username) < 3 or len(username) > 24:
        return RedirectResponse(url="/signup?err=length", status_code=303)
    if not re.match(r'^[a-z0-9_-]+$', username):
        return RedirectResponse(url="/signup?err=format", status_code=303)
    if len(password) < 6:
        return RedirectResponse(url="/signup?err=weakpw", status_code=303)
    # Reserve system paths
    reserved = {"app", "api", "login", "signup", "logout", "health", "static", "admin", "www", "assets"}
    if username in reserved:
        return RedirectResponse(url="/signup?err=reserved", status_code=303)

    if not create_user(username, password):
        return RedirectResponse(url="/signup?err=taken", status_code=303)

    # Auto-login
    create_user_session(username)
    token = _make_token(username)
    resp = RedirectResponse(url="/app", status_code=303)
    resp.set_cookie("vibe_auth", token, max_age=86400 * 30, httponly=True, samesite="lax")
    return resp


@app.get("/logout")
async def logout():
    resp = RedirectResponse(url="/", status_code=303)
    resp.delete_cookie("vibe_auth")
    return resp


@app.get("/health")
async def health():
    return JSONResponse({"status": "ok"})


# ---------------------------------------------------------------------------
# App route (main dashboard — auth required)
# ---------------------------------------------------------------------------
@app.get("/app", response_class=HTMLResponse)
async def app_page(request: Request):
    username = request.state.username
    # Ensure session exists
    create_user_session(username)
    return APP_HTML.replace("__USERNAME__", username).replace("__DOMAIN__", DOMAIN)


# ---------------------------------------------------------------------------
# API routes (all scoped to authenticated user)
# ---------------------------------------------------------------------------

@app.get("/api/session")
async def api_session(request: Request):
    username = request.state.username
    if not session_exists(username):
        create_user_session(username)
    data = await get_session_data(username)
    return JSONResponse(build_session_response(username, data))


@app.get("/api/status")
async def api_status(request: Request):
    username = request.state.username
    activity = detect_activity(username)
    return JSONResponse({
        "activity_status": activity["status"],
        "activity_detail": activity["detail"],
    })


@app.post("/api/session/refresh")
async def api_refresh(request: Request):
    username = request.state.username
    now = time.time()
    entry = cache.get(username, {})
    if "messages" not in entry:
        entry["messages"] = _load_user_messages(username)
    entry["realtime"] = await get_realtime(username)
    entry["realtime_at"] = now
    _append_assistant_msg(username, entry, entry["realtime"], now)
    cache[username] = entry
    return JSONResponse(build_session_response(username, entry))


@app.post("/api/session/refresh-all")
async def api_refresh_all(request: Request):
    username = request.state.username
    entry = await get_session_data(username, force_all=True)
    return JSONResponse(build_session_response(username, entry))


@app.get("/api/session/raw")
async def api_raw(request: Request):
    username = request.state.username
    raw = capture_pane_full(username)
    activity = detect_activity(username)
    return JSONResponse({
        "raw": raw,
        "lines": len(raw.split("\n")),
        "activity_status": activity["status"],
        "activity_command": activity["command"],
        "activity_detail": activity["detail"],
    })


class SendCommand(BaseModel):
    command: str


@app.post("/api/session/send")
async def api_send(request: Request, body: SendCommand):
    username = request.state.username
    name = _session_name(username)
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", name, "-l", body.command],
            capture_output=True, text=True, timeout=5,
        )
        subprocess.run(
            ["tmux", "send-keys", "-t", name, "Enter"],
            capture_output=True, text=True, timeout=5,
        )
        now = time.time()
        entry = cache.setdefault(username, {})
        if "messages" not in entry:
            entry["messages"] = _load_user_messages(username)
        entry["messages"].append({"role": "user", "text": body.command, "ts": now})
        _save_user_messages(username, entry["messages"])
        return JSONResponse({"ok": True, "sent": body.command})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/session/upload")
async def api_upload(request: Request, file: UploadFile = File(...)):
    username = request.state.username
    cwd = get_session_cwd(username)
    # Security: ensure cwd is within user dir
    user_dir = get_user_dir(username)
    if not os.path.realpath(cwd).startswith(os.path.realpath(user_dir)):
        cwd = user_dir

    filename = os.path.basename(file.filename or "upload")
    if not filename or filename.startswith("."):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    dest = os.path.join(cwd, filename)
    try:
        content = await file.read()
        with open(dest, "wb") as f:
            f.write(content)
        size_kb = len(content) / 1024
        note = f"Uploaded {filename} ({size_kb:.1f} KB) to {cwd}"
        now = time.time()
        entry = cache.setdefault(username, {})
        if "messages" not in entry:
            entry["messages"] = _load_user_messages(username)
        entry["messages"].append({"role": "user", "text": note, "ts": now})
        _save_user_messages(username, entry["messages"])
        return JSONResponse({"ok": True, "path": dest, "size": len(content)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# ---------------------------------------------------------------------------
# Public file serving — dianao.tech/<username>/<path>
# ---------------------------------------------------------------------------
BLOCKED_PATTERNS = {".env", ".git", ".claude", "node_modules", "__pycache__", ".ssh", ".bash_history", "CLAUDE.md"}


def _is_blocked_path(path_parts: list[str]) -> bool:
    for part in path_parts:
        if part.startswith("."):
            return True
        if part in BLOCKED_PATTERNS:
            return True
    return False


@app.get("/{username}/{file_path:path}")
async def serve_user_file(username: str, file_path: str = ""):
    # Check user exists
    if not user_exists(username):
        return HTMLResponse("<h1>404 - User not found</h1>", status_code=404)

    user_dir = get_user_dir(username)
    if not file_path or file_path == "/":
        file_path = "index.html"

    # Security: block sensitive paths
    parts = file_path.split("/")
    if _is_blocked_path(parts):
        return HTMLResponse("<h1>403 - Forbidden</h1>", status_code=403)

    full_path = os.path.join(user_dir, file_path)
    real_path = os.path.realpath(full_path)

    # Security: prevent path traversal
    if not real_path.startswith(os.path.realpath(user_dir)):
        return HTMLResponse("<h1>403 - Forbidden</h1>", status_code=403)

    # If it's a directory, look for index.html
    if os.path.isdir(real_path):
        index_path = os.path.join(real_path, "index.html")
        if os.path.isfile(index_path):
            return FileResponse(index_path)
        return HTMLResponse("<h1>404 - Not found</h1>", status_code=404)

    if os.path.isfile(real_path):
        return FileResponse(real_path)

    return HTMLResponse("<h1>404 - Not found</h1>", status_code=404)


# Also handle the bare username path (no trailing slash)
@app.get("/{username}")
async def serve_user_root(username: str):
    # Don't intercept known routes
    if username in {"favicon.ico", "robots.txt"}:
        return HTMLResponse("", status_code=404)
    return await serve_user_file(username, "")


# ---------------------------------------------------------------------------
# HTML Pages
# ---------------------------------------------------------------------------

LANDING_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Vibe Coder — Build apps with AI, instantly live</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>⚡</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0f;color:#e1e4e8;min-height:100vh}
.hero{min-height:100vh;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:40px 20px;text-align:center;position:relative;overflow:hidden}
.hero::before{content:'';position:absolute;top:-50%;left:-50%;width:200%;height:200%;background:radial-gradient(ellipse at center,rgba(99,102,241,.08) 0%,transparent 70%);animation:glow 8s ease-in-out infinite alternate}
@keyframes glow{0%{transform:translate(0,0)}100%{transform:translate(5%,3%)}}
.logo{font-size:3.5rem;font-weight:800;background:linear-gradient(135deg,#818cf8,#c084fc,#f472b6);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:16px;position:relative;z-index:1}
.tagline{font-size:1.4rem;color:#9ca3af;max-width:600px;line-height:1.6;margin-bottom:48px;position:relative;z-index:1}
.tagline em{color:#c084fc;font-style:normal;font-weight:600}
.cta-group{display:flex;gap:16px;position:relative;z-index:1;flex-wrap:wrap;justify-content:center}
.cta{display:inline-flex;align-items:center;gap:8px;padding:14px 32px;border-radius:12px;font-size:1rem;font-weight:600;text-decoration:none;transition:all .2s}
.cta-primary{background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none}
.cta-primary:hover{transform:translateY(-2px);box-shadow:0 8px 30px rgba(99,102,241,.4)}
.cta-secondary{background:transparent;color:#c084fc;border:1px solid #c084fc44}
.cta-secondary:hover{background:#c084fc11;transform:translateY(-2px)}
.features{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:24px;max-width:900px;margin:60px auto 0;padding:0 20px;position:relative;z-index:1}
.feature{background:#12121a;border:1px solid #1e1e2e;border-radius:16px;padding:28px;text-align:left}
.feature-icon{font-size:1.5rem;margin-bottom:12px}
.feature h3{color:#f0f6fc;font-size:1rem;margin-bottom:8px}
.feature p{color:#6b7280;font-size:.9rem;line-height:1.5}
.how{max-width:700px;margin:80px auto 0;text-align:center;position:relative;z-index:1;padding:0 20px}
.how h2{font-size:1.8rem;color:#f0f6fc;margin-bottom:32px}
.steps{display:flex;flex-direction:column;gap:20px;text-align:left}
.step{display:flex;gap:16px;align-items:flex-start}
.step-num{width:36px;height:36px;border-radius:50%;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;display:flex;align-items:center;justify-content:center;font-weight:700;font-size:.9rem;flex-shrink:0}
.step-text h4{color:#e5e7eb;margin-bottom:4px}
.step-text p{color:#6b7280;font-size:.9rem}
.footer{text-align:center;padding:40px 20px;color:#4b5563;font-size:.8rem;position:relative;z-index:1}
@media(max-width:640px){.logo{font-size:2.5rem}.tagline{font-size:1.1rem}}
</style></head>
<body>
<div class="hero">
  <div class="logo">vibe coder</div>
  <p class="tagline">Describe what you want. <em>AI builds it.</em> Your app goes live instantly at its own URL. No setup, no servers, no code needed.</p>
  <div class="cta-group">
    <a href="/signup" class="cta cta-primary">Start Building</a>
    <a href="/login" class="cta cta-secondary">Log In</a>
  </div>

  <div class="features">
    <div class="feature">
      <div class="feature-icon">💬</div>
      <h3>Chat to build</h3>
      <p>Describe your app in plain English. Claude Code writes, runs, and deploys it for you in real time.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">🌐</div>
      <h3>Instantly live</h3>
      <p>Everything you build gets a public URL immediately. Share your creation with anyone, anywhere.</p>
    </div>
    <div class="feature">
      <div class="feature-icon">🔒</div>
      <h3>Your own sandbox</h3>
      <p>Each user gets an isolated workspace. Build freely without worrying about breaking anything.</p>
    </div>
  </div>

  <div class="how">
    <h2>How it works</h2>
    <div class="steps">
      <div class="step">
        <div class="step-num">1</div>
        <div class="step-text">
          <h4>Sign up in seconds</h4>
          <p>Pick a username and password. That's it — your workspace is ready.</p>
        </div>
      </div>
      <div class="step">
        <div class="step-num">2</div>
        <div class="step-text">
          <h4>Tell Claude what to build</h4>
          <p>Type a message like "Build me a portfolio site with a dark theme" and watch it happen.</p>
        </div>
      </div>
      <div class="step">
        <div class="step-num">3</div>
        <div class="step-text">
          <h4>See it live</h4>
          <p>Your project is immediately available at dianao.tech/your-username — share it with the world.</p>
        </div>
      </div>
    </div>
  </div>

  <div class="footer">
    <p>Powered by Claude Code &middot; Built with vibes</p>
  </div>
</div>
</body></html>"""

LOGIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Log In — Vibe Coder</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>⚡</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0f;color:#e1e4e8;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#12121a;border:1px solid #1e1e2e;border-radius:16px;padding:36px;width:380px;max-width:calc(100vw - 32px)}
.brand{font-size:1.4rem;font-weight:700;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:4px}
.subtitle{color:#6b7280;font-size:.85rem;margin-bottom:24px}
.field{margin-bottom:16px}
.field label{display:block;font-size:.8rem;color:#9ca3af;margin-bottom:6px;font-weight:500}
.field input{width:100%;background:#0a0a0f;border:1px solid #2d2d3d;border-radius:8px;color:#e6edf3;padding:11px 14px;font-size:.95rem;outline:none;transition:border-color .15s}
.field input:focus{border-color:#6366f1}
.err{color:#f87171;font-size:.8rem;margin-bottom:12px;display:none;padding:8px 12px;background:#f8717111;border-radius:8px}
.err.show{display:block}
.submit{width:100%;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none;padding:12px;border-radius:8px;cursor:pointer;font-size:.95rem;font-weight:600;transition:transform .15s,box-shadow .15s}
.submit:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(99,102,241,.3)}
.links{margin-top:20px;text-align:center;font-size:.85rem;color:#6b7280}
.links a{color:#818cf8;text-decoration:none}
.links a:hover{text-decoration:underline}
</style></head>
<body>
<form class="card" method="POST" action="/login">
  <div class="brand">vibe coder</div>
  <p class="subtitle">Welcome back. Log in to your workspace.</p>
  <div class="err" id="err"></div>
  <div class="field"><label>Username</label><input name="username" autocomplete="username" autofocus></div>
  <div class="field"><label>Password</label><input name="password" type="password" autocomplete="current-password"></div>
  <button class="submit" type="submit">Log In</button>
  <div class="links">Don't have an account? <a href="/signup">Sign up</a></div>
</form>
<script>
const p=new URLSearchParams(location.search);
const msgs={invalid:'Invalid username or password.',missing:'Please fill in all fields.'};
const e=p.get('err');
if(e&&msgs[e]){const el=document.getElementById('err');el.textContent=msgs[e];el.classList.add('show')}
</script>
</body></html>"""

SIGNUP_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Sign Up — Vibe Coder</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>⚡</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0f;color:#e1e4e8;min-height:100vh;display:flex;align-items:center;justify-content:center}
.card{background:#12121a;border:1px solid #1e1e2e;border-radius:16px;padding:36px;width:380px;max-width:calc(100vw - 32px)}
.brand{font-size:1.4rem;font-weight:700;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-bottom:4px}
.subtitle{color:#6b7280;font-size:.85rem;margin-bottom:24px}
.field{margin-bottom:16px}
.field label{display:block;font-size:.8rem;color:#9ca3af;margin-bottom:6px;font-weight:500}
.field input{width:100%;background:#0a0a0f;border:1px solid #2d2d3d;border-radius:8px;color:#e6edf3;padding:11px 14px;font-size:.95rem;outline:none;transition:border-color .15s}
.field input:focus{border-color:#6366f1}
.hint{font-size:.75rem;color:#4b5563;margin-top:4px}
.err{color:#f87171;font-size:.8rem;margin-bottom:12px;display:none;padding:8px 12px;background:#f8717111;border-radius:8px}
.err.show{display:block}
.submit{width:100%;background:linear-gradient(135deg,#6366f1,#8b5cf6);color:#fff;border:none;padding:12px;border-radius:8px;cursor:pointer;font-size:.95rem;font-weight:600;transition:transform .15s,box-shadow .15s}
.submit:hover{transform:translateY(-1px);box-shadow:0 4px 20px rgba(99,102,241,.3)}
.url-preview{margin-top:12px;background:#0a0a0f;border:1px solid #2d2d3d;border-radius:8px;padding:10px 14px;font-size:.85rem;color:#6b7280;font-family:'SF Mono',monospace}
.url-preview span{color:#c084fc}
.links{margin-top:20px;text-align:center;font-size:.85rem;color:#6b7280}
.links a{color:#818cf8;text-decoration:none}
.links a:hover{text-decoration:underline}
</style></head>
<body>
<form class="card" method="POST" action="/signup">
  <div class="brand">vibe coder</div>
  <p class="subtitle">Pick a username — it becomes your URL.</p>
  <div class="err" id="err"></div>
  <div class="field">
    <label>Username</label>
    <input name="username" id="username" autocomplete="username" autofocus
      placeholder="your-name" oninput="updatePreview()">
    <div class="hint">3-24 characters. Letters, numbers, dashes, underscores.</div>
  </div>
  <div class="url-preview">Your site: dianao.tech/<span id="preview">___</span></div>
  <div class="field" style="margin-top:16px">
    <label>Password</label>
    <input name="password" type="password" autocomplete="new-password" placeholder="6+ characters">
  </div>
  <button class="submit" type="submit">Create Account</button>
  <div class="links">Already have an account? <a href="/login">Log in</a></div>
</form>
<script>
function updatePreview(){
  const v=document.getElementById('username').value.toLowerCase().replace(/[^a-z0-9_-]/g,'');
  document.getElementById('preview').textContent=v||'___';
}
const p=new URLSearchParams(location.search);
const msgs={
  taken:'Username already taken. Try another.',
  missing:'Please fill in all fields.',
  length:'Username must be 3-24 characters.',
  format:'Username can only contain letters, numbers, dashes, underscores.',
  weakpw:'Password must be at least 6 characters.',
  reserved:'That username is reserved. Try another.'
};
const e=p.get('err');
if(e&&msgs[e]){const el=document.getElementById('err');el.textContent=msgs[e];el.classList.add('show')}
</script>
</body></html>"""

APP_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>Vibe Coder — Workspace</title>
<link rel="icon" id="favicon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><text y='14' font-size='14'>⚡</text></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0a0a0f;color:#e1e4e8;min-height:100vh;display:flex;flex-direction:column}

/* Top bar */
.top-bar{background:#12121a;border-bottom:1px solid #1e1e2e;padding:0 20px;display:flex;align-items:center;height:52px;flex-shrink:0}
.top-brand{font-size:.95rem;font-weight:700;background:linear-gradient(135deg,#818cf8,#c084fc);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;margin-right:16px}
.top-url{font-size:.8rem;color:#6b7280;font-family:'SF Mono','Fira Code',monospace;background:#0a0a0f;padding:5px 12px;border-radius:6px;border:1px solid #1e1e2e;display:flex;align-items:center;gap:8px}
.top-url a{color:#818cf8;text-decoration:none}
.top-url a:hover{text-decoration:underline}
.copy-btn{background:none;border:none;color:#6b7280;cursor:pointer;font-size:.75rem;padding:2px 6px;border-radius:4px;transition:color .15s}
.copy-btn:hover{color:#c084fc}
.top-spacer{flex:1}
.top-status{display:flex;align-items:center;gap:8px;font-size:.8rem;color:#6b7280;margin-right:16px}
.status-dot{width:8px;height:8px;border-radius:50%;flex-shrink:0}
.status-dot.busy{background:#f87171;animation:pulse 1.5s ease-in-out infinite}
.status-dot.idle{background:#34d399}
.status-dot.unknown{background:#a78bfa}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
.top-user{font-size:.8rem;color:#9ca3af;margin-right:12px}
.top-logout{background:#1e1e2e;color:#9ca3af;border:1px solid #2d2d3d;padding:5px 14px;border-radius:6px;cursor:pointer;font-size:.8rem;text-decoration:none;transition:color .15s}
.top-logout:hover{color:#f87171;border-color:#f8717144}

/* Main layout */
.workspace{flex:1;display:flex;flex-direction:column;max-width:1000px;width:100%;margin:0 auto;padding:16px 20px}

/* Project header */
.project-header{margin-bottom:12px}
.project-title{font-size:1.3rem;font-weight:600;color:#f0f6fc}
.project-desc{color:#6b7280;font-size:.9rem;margin-top:4px}

/* Tabs */
.tab-bar{display:flex;border-bottom:1px solid #1e1e2e;margin-bottom:0}
.tab{padding:10px 20px;font-size:.85rem;font-weight:500;color:#6b7280;cursor:pointer;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;user-select:none}
.tab:hover{color:#c9d1d9}
.tab.active{color:#818cf8;border-bottom-color:#818cf8}
.tab-content{display:none;flex-direction:column;flex:1;min-height:0}
.tab-content.active{display:flex}

/* Chat */
.chat-wrap{display:flex;flex-direction:column;flex:1;min-height:0}
.chat-messages{flex:1;overflow-y:auto;padding:16px 0;display:flex;flex-direction:column;gap:12px;min-height:120px;max-height:calc(100vh - 260px)}
.chat-messages::-webkit-scrollbar{width:6px}
.chat-messages::-webkit-scrollbar-track{background:transparent}
.chat-messages::-webkit-scrollbar-thumb{background:#2d2d3d;border-radius:3px}
.chat-msg{max-width:85%;padding:12px 16px;border-radius:14px;font-size:1.02rem;line-height:1.6;position:relative;word-wrap:break-word}
.chat-msg.user{align-self:flex-end;background:linear-gradient(135deg,#4f46e5,#6d28d9);color:#fff;border-bottom-right-radius:4px}
.chat-msg.assistant{align-self:flex-start;background:#12121a;border:1px solid #1e1e2e;color:#c9d1d9;border-bottom-left-radius:4px}
.chat-meta{font-size:.7rem;color:#4b5563;margin-top:4px}
.chat-msg.user .chat-meta{text-align:right;color:#ffffffaa}
.chat-typing{align-self:flex-start;padding:10px 14px;background:#12121a;border:1px solid #1e1e2e;border-radius:14px;border-bottom-left-radius:4px;color:#6b7280;font-size:.85rem;font-style:italic}

/* Cmd bar */
.cmd-bar{display:flex;align-items:flex-end;gap:0;margin-top:8px;background:#0a0a0f;border:1px solid #1e1e2e;border-radius:10px;overflow:hidden;flex-shrink:0}
.cmd-prompt{padding:12px 0 12px 14px;color:#818cf8;font-family:'SF Mono','Fira Code',monospace;font-size:1rem;font-weight:600;user-select:none}
.cmd-input{flex:1;background:transparent;border:none;outline:none;color:#e6edf3;font-family:'SF Mono','Fira Code',monospace;font-size:1rem;padding:12px;resize:none;min-height:44px;max-height:160px;line-height:1.4;overflow-y:auto}
.cmd-input::placeholder{color:#3d3d4d}
.cmd-send{border:none;border-left:1px solid #1e1e2e;border-radius:0;padding:12px 18px;font-size:.95rem;align-self:flex-end;background:#12121a;color:#818cf8;cursor:pointer;font-weight:600;transition:background .15s}
.cmd-send:hover{background:#1e1e2e}
.cmd-upload{border:none;border-left:1px solid #1e1e2e;border-radius:0;padding:12px 14px;font-size:1.1rem;cursor:pointer;background:#12121a;color:#6b7280;align-self:flex-end;line-height:1;transition:color .15s}
.cmd-upload:hover{color:#818cf8}

/* Raw */
.raw-wrap{padding-top:12px;display:flex;flex-direction:column;flex:1}
.raw-controls{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.raw-info{color:#4b5563;font-size:.75rem}
.raw-output{background:#0a0a0f;border:1px solid #1e1e2e;border-radius:10px;padding:12px;font-family:'SF Mono','Fira Code',monospace;font-size:.8rem;line-height:1.45;color:#c9d1d9;flex:1;min-height:300px;overflow-y:auto;white-space:pre;word-wrap:normal;overflow-x:auto}
.raw-cmd-bar{margin-top:8px}

/* Info */
.info-wrap{padding-top:16px}
.tier{margin-bottom:16px}
.tier-label{font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:#6b7280;margin-bottom:4px;display:flex;align-items:center;gap:6px}
.tier-label .dot{width:6px;height:6px;border-radius:50%;display:inline-block;background:#6b7280}
.tier-progress .tier-label{color:#c084fc}
.tier-progress .dot{background:#c084fc}
.tier-text{color:#b1bac4;line-height:1.6;font-size:1rem}
.info-footer{display:flex;justify-content:space-between;align-items:center;border-top:1px solid #1e1e2e;padding-top:12px;margin-top:12px}
.timestamps{display:flex;gap:16px;flex-wrap:wrap}
.ts{color:#3d3d4d;font-size:.75rem}
.ts span{color:#6b7280}
.btn-group{display:flex;gap:8px}
.btn{background:#12121a;color:#c9d1d9;border:1px solid #1e1e2e;padding:6px 14px;border-radius:8px;cursor:pointer;font-size:.8rem;transition:background .15s}
.btn:hover{background:#1e1e2e}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-full{background:#1a1a2e;border-color:#6366f144;color:#818cf8}
.btn-full:hover{background:#252540}
.spinner{display:inline-block;width:12px;height:12px;border:2px solid #1e1e2e;border-top-color:#818cf8;border-radius:50%;animation:spin .8s linear infinite;margin-right:4px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.empty{text-align:center;color:#6b7280;padding:60px 20px;font-size:1rem}

/* Welcome overlay */
.welcome{background:#12121a;border:1px solid #1e1e2e;border-radius:14px;padding:24px;margin-bottom:16px}
.welcome h3{color:#f0f6fc;margin-bottom:8px;font-size:1rem}
.welcome p{color:#6b7280;font-size:.9rem;line-height:1.5}
.welcome code{background:#0a0a0f;padding:2px 6px;border-radius:4px;font-size:.85rem;color:#c084fc}

/* Mobile */
@media(max-width:640px){
  .top-bar{padding:0 12px;gap:8px}
  .top-url{display:none}
  .workspace{padding:12px}
  .chat-msg{max-width:92%}
  .chat-messages{max-height:calc(100vh - 280px)}
}
</style></head>
<body>
<div class="top-bar">
  <span class="top-brand">vibe coder</span>
  <div class="top-url">
    <a href="https://__DOMAIN__/__USERNAME__/" target="_blank">__DOMAIN__/__USERNAME__</a>
    <button class="copy-btn" onclick="copyUrl()" title="Copy URL">copy</button>
  </div>
  <span class="top-spacer"></span>
  <div class="top-status" id="top-status">
    <span class="status-dot unknown" id="top-dot"></span>
    <span id="status-text">connecting...</span>
  </div>
  <span class="top-user" id="top-user">__USERNAME__</span>
  <a href="/logout" class="top-logout">Log out</a>
</div>

<div class="workspace">
  <div class="project-header">
    <div class="project-title" id="project-title">Your Workspace</div>
    <div class="project-desc" id="project-desc">Loading...</div>
  </div>

  <div class="tab-bar" id="tab-bar">
    <div class="tab active" onclick="switchTab('chat')">Chat</div>
    <div class="tab" onclick="switchTab('raw')">Terminal</div>
    <div class="tab" onclick="switchTab('info')">Info</div>
  </div>

  <div class="tab-content active" id="tab-chat">
    <div class="chat-wrap">
      <div class="chat-messages" id="chat-messages">
        <div class="welcome" id="welcome">
          <h3>Welcome to your workspace!</h3>
          <p>Claude Code is ready. Tell it what to build — for example:<br>
          <code>Build me a portfolio website with a dark theme</code><br>
          Your project will be live at <a href="https://__DOMAIN__/__USERNAME__/" target="_blank" style="color:#818cf8">__DOMAIN__/__USERNAME__</a></p>
        </div>
      </div>
      <div class="cmd-bar">
        <span class="cmd-prompt">&gt;</span>
        <textarea class="cmd-input" id="cmd-chat" rows="1"
          placeholder="Tell Claude what to build..."
          onkeydown="handleKey(event,'chat')"
          oninput="autoGrow(this)"
          autocomplete="off" spellcheck="false"></textarea>
        <button class="cmd-send" onclick="sendChat()">Send</button>
        <button class="cmd-upload" onclick="document.getElementById('upload-input').click()" title="Upload file">&#x1F4CE;</button>
        <input type="file" id="upload-input" style="display:none" onchange="uploadFile(this)" multiple>
      </div>
    </div>
  </div>

  <div class="tab-content" id="tab-raw">
    <div class="raw-wrap">
      <div class="raw-controls">
        <span class="raw-info" id="raw-info">Click to load</span>
        <button class="btn" onclick="loadRaw()">Reload</button>
      </div>
      <div class="raw-output" id="raw-output">Loading terminal output...</div>
      <div class="cmd-bar raw-cmd-bar">
        <span class="cmd-prompt">$</span>
        <textarea class="cmd-input" id="cmd-raw" rows="1"
          placeholder="Type a command..."
          onkeydown="handleKey(event,'raw')"
          oninput="autoGrow(this)"
          autocomplete="off" spellcheck="false"></textarea>
        <button class="cmd-send" onclick="sendRaw()">Send</button>
      </div>
    </div>
  </div>

  <div class="tab-content" id="tab-info">
    <div class="info-wrap">
      <div class="tier">
        <div class="tier-label"><span class="dot"></span>Project</div>
        <div class="tier-text" id="info-desc">Loading...</div>
      </div>
      <div class="tier tier-progress">
        <div class="tier-label"><span class="dot"></span>Progress</div>
        <div class="tier-text" id="info-prog">Loading...</div>
      </div>
      <div class="info-footer">
        <div class="timestamps">
          <div class="ts">project: <span id="ts-desc">-</span></div>
          <div class="ts">progress: <span id="ts-prog">-</span></div>
          <div class="ts">live: <span id="ts-rt">-</span></div>
        </div>
        <div class="btn-group">
          <button class="btn" id="btn-refresh" onclick="refreshSession()">Update</button>
          <button class="btn btn-full" id="btn-full" onclick="refreshFull()">Full</button>
        </div>
      </div>
    </div>
  </div>
</div>

<script>
const USERNAME='__USERNAME__';
const DOMAIN='__DOMAIN__';
const BASE='';
let chatMessages=[];
let currentTab='chat';
let lastStatus='';
let pollTimer=null;
let rawLoaded=false;

function copyUrl(){
  navigator.clipboard.writeText('https://'+DOMAIN+'/'+USERNAME+'/');
  const btn=document.querySelector('.copy-btn');
  btn.textContent='copied!';
  setTimeout(()=>btn.textContent='copy',1500);
}

function timeAgo(ts){
  if(!ts)return'never';
  const diff=Math.floor(Date.now()/1000-ts);
  if(diff<5)return'just now';
  if(diff<60)return diff+'s ago';
  if(diff<3600)return Math.floor(diff/60)+'m ago';
  return Math.floor(diff/3600)+'h ago';
}
function fmtTime(ts){
  if(!ts)return'';
  return new Date(ts*1000).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}
function esc(str){
  if(!str)return'';
  const d=document.createElement('div');
  d.textContent=str;
  return d.innerHTML;
}

function updateFavicon(status){
  const colors={busy:'%23f87171',idle:'%2334d399',unknown:'%23a78bfa'};
  const c=colors[status]||colors.unknown;
  document.getElementById('favicon').href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='7' fill='"+c+"'/></svg>";
}

function updateStatus(status,detail){
  const dot=document.getElementById('top-dot');
  const text=document.getElementById('status-text');
  dot.className='status-dot '+(status||'unknown');
  if(status==='busy')text.textContent=detail||'Working...';
  else if(status==='idle')text.textContent=detail||'Ready';
  else text.textContent='...';
  updateFavicon(status);
}

function switchTab(name){
  currentTab=name;
  document.querySelectorAll('.tab-content').forEach(t=>t.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  document.getElementById('tab-'+name).classList.add('active');
  const tabs=document.querySelectorAll('.tab');
  const tabNames=['chat','raw','info'];
  const idx=tabNames.indexOf(name);
  if(tabs[idx])tabs[idx].classList.add('active');
  if(name==='raw'&&!rawLoaded)loadRaw();
  if(name==='chat'){
    const el=document.getElementById('chat-messages');
    el.scrollTop=el.scrollHeight;
  }
}

function renderChatBubbles(){
  const el=document.getElementById('chat-messages');
  let html='';
  if(chatMessages.length===0){
    html=document.getElementById('welcome')?document.getElementById('welcome').outerHTML:'';
  }
  chatMessages.forEach(m=>{
    html+='<div class="chat-msg '+m.role+'">'
      +esc(m.text)
      +'<div class="chat-meta">'+fmtTime(m.ts)+'</div>'
      +'</div>';
  });
  el.innerHTML=html;
  el.scrollTop=el.scrollHeight;
}

function appendBubble(role,text,ts){
  if(role==='assistant'){
    for(let i=chatMessages.length-1;i>=0;i--){
      if(chatMessages[i].role==='assistant'){
        if(chatMessages[i].text===text)return;
        break;
      }
    }
  }
  chatMessages.push({role,text,ts});
  const w=document.getElementById('welcome');
  if(w)w.remove();
  const el=document.getElementById('chat-messages');
  const bubble=document.createElement('div');
  bubble.className='chat-msg '+role;
  bubble.innerHTML=esc(text)+'<div class="chat-meta">'+fmtTime(ts)+'</div>';
  const typing=el.querySelector('.chat-typing');
  if(typing)typing.remove();
  el.appendChild(bubble);
  el.scrollTop=el.scrollHeight;
}

function showTyping(){
  const el=document.getElementById('chat-messages');
  if(!el.querySelector('.chat-typing')){
    const d=document.createElement('div');
    d.className='chat-typing';
    d.textContent='Working...';
    el.appendChild(d);
    el.scrollTop=el.scrollHeight;
  }
}
function hideTyping(){
  const el=document.getElementById('chat-messages');
  const t=el.querySelector('.chat-typing');
  if(t)t.remove();
}

function autoGrow(el){
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,160)+'px';
}
function handleKey(e,src){
  if(e.key==='Enter'&&!e.shiftKey){
    e.preventDefault();
    if(src==='chat')sendChat();
    else sendRaw();
  }
}

async function sendChat(){
  const input=document.getElementById('cmd-chat');
  const cmd=input.value.trim();
  if(!cmd)return;
  input.disabled=true;
  appendBubble('user',cmd,Date.now()/1000);
  try{
    await fetch(BASE+'/api/session/send',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command:cmd})
    });
    input.value='';input.style.height='auto';
  }catch(e){alert('Failed to send')}
  input.disabled=false;
  input.focus();
}

async function sendRaw(){
  const input=document.getElementById('cmd-raw');
  const cmd=input.value.trim();
  if(!cmd)return;
  input.disabled=true;
  appendBubble('user',cmd,Date.now()/1000);
  try{
    await fetch(BASE+'/api/session/send',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command:cmd})
    });
    input.value='';input.style.height='auto';
    setTimeout(loadRaw,500);
  }catch(e){alert('Failed to send')}
  input.disabled=false;
  input.focus();
}

async function uploadFile(input){
  if(!input.files||!input.files.length)return;
  for(const file of input.files){
    const fd=new FormData();
    fd.append('file',file);
    const sizeKb=(file.size/1024).toFixed(1);
    appendBubble('user','Uploading '+file.name+' ('+sizeKb+' KB)...',Date.now()/1000);
    try{
      const resp=await fetch(BASE+'/api/session/upload',{method:'POST',body:fd});
      const data=await resp.json();
      if(!resp.ok)appendBubble('assistant','Upload failed: '+(data.error||'error'),Date.now()/1000);
    }catch(e){appendBubble('assistant','Upload failed: network error',Date.now()/1000)}
  }
  input.value='';
}

async function loadRaw(){
  const el=document.getElementById('raw-output');
  const info=document.getElementById('raw-info');
  el.textContent='Loading...';
  try{
    const resp=await fetch(BASE+'/api/session/raw');
    const data=await resp.json();
    rawLoaded=true;
    el.textContent=data.raw||'(empty)';
    el.scrollTop=el.scrollHeight;
    info.textContent=data.lines+' lines';
    updateStatus(data.activity_status,data.activity_detail);
  }catch(e){el.textContent='Error loading'}
}

async function loadSession(){
  try{
    const resp=await fetch(BASE+'/api/session');
    const s=await resp.json();
    if(s.title){
      document.getElementById('project-title').textContent=s.title;
      document.title='Vibe Coder \u2014 '+s.title;
    }
    if(s.description)document.getElementById('project-desc').textContent=s.description;
    updateStatus(s.activity_status,s.activity_detail);
    if(s.messages&&s.messages.length){
      chatMessages=s.messages;
      if(currentTab==='chat')renderChatBubbles();
    }
    document.getElementById('info-desc').textContent=s.description||'Loading...';
    document.getElementById('info-prog').textContent=s.progress||'Loading...';
    document.getElementById('ts-desc').textContent=timeAgo(s.description_at);
    document.getElementById('ts-prog').textContent=timeAgo(s.progress_at);
    document.getElementById('ts-rt').textContent=timeAgo(s.realtime_at);
  }catch(e){console.error('Load failed',e)}
  startPolling();
}

async function pollStatus(){
  try{
    const resp=await fetch(BASE+'/api/status');
    const st=await resp.json();
    const prev=lastStatus;
    lastStatus=st.activity_status;
    updateStatus(st.activity_status,st.activity_detail);
    if(st.activity_status==='busy')showTyping();
    else hideTyping();
    if(prev&&prev!==st.activity_status){
      refreshSession();
    }
  }catch(e){}
}

function startPolling(){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(pollStatus,10000);
}

async function refreshSession(){
  const btn=document.getElementById('btn-refresh');
  if(btn){btn.disabled=true;btn.innerHTML='<span class="spinner"></span>'}
  try{
    const resp=await fetch(BASE+'/api/session/refresh',{method:'POST'});
    const s=await resp.json();
    if(s.title)document.getElementById('project-title').textContent=s.title;
    if(s.description)document.getElementById('project-desc').textContent=s.description;
    updateStatus(s.activity_status,s.activity_detail);
    if(s.messages){
      s.messages.forEach(m=>{
        if(m.role==='assistant'){
          const exists=chatMessages.some(l=>l.role==='assistant'&&l.text===m.text);
          if(!exists)appendBubble('assistant',m.text,m.ts);
        }
      });
    }
    document.getElementById('info-desc').textContent=s.description||'';
    document.getElementById('info-prog').textContent=s.progress||'';
    document.getElementById('ts-desc').textContent=timeAgo(s.description_at);
    document.getElementById('ts-prog').textContent=timeAgo(s.progress_at);
    document.getElementById('ts-rt').textContent=timeAgo(s.realtime_at);
  }catch(e){}
  if(btn){btn.disabled=false;btn.textContent='Update'}
}

async function refreshFull(){
  const btn=document.getElementById('btn-full');
  if(btn){btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Full'}
  try{
    const resp=await fetch(BASE+'/api/session/refresh-all',{method:'POST'});
    const s=await resp.json();
    if(s.title)document.getElementById('project-title').textContent=s.title;
    if(s.description){
      document.getElementById('project-desc').textContent=s.description;
      document.getElementById('info-desc').textContent=s.description;
    }
    if(s.progress)document.getElementById('info-prog').textContent=s.progress;
    updateStatus(s.activity_status,s.activity_detail);
    if(s.messages){
      s.messages.forEach(m=>{
        if(m.role==='assistant'){
          const exists=chatMessages.some(l=>l.role==='assistant'&&l.text===m.text);
          if(!exists)appendBubble('assistant',m.text,m.ts);
        }
      });
    }
    document.getElementById('ts-desc').textContent=timeAgo(s.description_at);
    document.getElementById('ts-prog').textContent=timeAgo(s.progress_at);
    document.getElementById('ts-rt').textContent=timeAgo(s.realtime_at);
  }catch(e){}
  if(btn){btn.disabled=false;btn.textContent='Full'}
}

loadSession();
</script>
</body></html>"""

# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
