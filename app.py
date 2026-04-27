from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict
import glob as globmod

logger = logging.getLogger("tmux-dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import openai
import uvicorn

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PORT = int(os.environ.get("TMUX_DASH_PORT", "8501"))
ROOT_PATH = os.environ.get("TMUX_DASH_ROOT_PATH", "/tmux")
NEW_SESSION_CMD = os.environ.get("TMUX_DASH_NEW_SESSION_CMD", "")  # e.g. "claude"

client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

# --- Claude Code API key storage ---
MESSAGES_DIR = Path.home() / ".tmux-dashboard"
ANTHROPIC_API_KEY_FILE = MESSAGES_DIR / "anthropic_api_key"
_stored_anthropic_key: str = ""


def _load_anthropic_key() -> str:
    global _stored_anthropic_key
    try:
        if ANTHROPIC_API_KEY_FILE.exists():
            _stored_anthropic_key = ANTHROPIC_API_KEY_FILE.read_text().strip()
    except Exception:
        logger.debug("Failed to load Anthropic API key from %s", ANTHROPIC_API_KEY_FILE, exc_info=True)
    return _stored_anthropic_key


def _save_anthropic_key(key: str):
    global _stored_anthropic_key
    _stored_anthropic_key = key
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        ANTHROPIC_API_KEY_FILE.write_text(key)
        ANTHROPIC_API_KEY_FILE.chmod(0o600)
    except Exception:
        logger.debug("Failed to save Anthropic API key", exc_info=True)


def _clear_anthropic_key():
    global _stored_anthropic_key
    _stored_anthropic_key = ""
    try:
        if ANTHROPIC_API_KEY_FILE.exists():
            ANTHROPIC_API_KEY_FILE.unlink()
    except Exception:
        logger.debug("Failed to clear Anthropic API key", exc_info=True)


_load_anthropic_key()

# Track auth mode per session: "subscription" or "api"
_session_auth_mode: Dict[str, str] = {}

# Away mode state per session
_away_mode_state: Dict[str, dict] = {}
# Per-session structure when active:
# {
#   "enabled": bool,
#   "phase": int (1-5),
#   "phase_name": str,
#   "step": int,
#   "step_name": str,
#   "started_at": float,
#   "log": [{"ts": float, "phase": int, "step": int, "action": str}],
#   "report": str,
#   "task": asyncio.Task | None,
# }

# Go Nuts mode state per session (same structure as away mode)
_go_nuts_state: Dict[str, dict] = {}

# Flag to prevent CancelledError handlers from wiping persisted state during shutdown.
# When True, worker cancel handlers skip setting enabled=False and re-saving to disk.
_shutting_down = False

# --- Persistent autonomous mode state ---
# Survives restarts: stores which sessions had away/go-nuts mode enabled.
AUTONOMOUS_STATE_FILE = MESSAGES_DIR / "autonomous-modes.json"

def _save_autonomous_state():
    """Persist which sessions have away/go-nuts mode enabled to disk."""
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        state = {}
        for name, s in _away_mode_state.items():
            if s.get("enabled"):
                state.setdefault(name, {})["away_mode"] = True
        for name, s in _go_nuts_state.items():
            if s.get("enabled"):
                state.setdefault(name, {})["go_nuts_mode"] = True
        AUTONOMOUS_STATE_FILE.write_text(json.dumps(state))
    except Exception:
        logger.debug("Failed to save autonomous mode state", exc_info=True)

def _load_autonomous_state() -> Dict[str, dict]:
    """Load persisted autonomous mode state from disk."""
    try:
        if AUTONOMOUS_STATE_FILE.exists():
            return json.loads(AUTONOMOUS_STATE_FILE.read_text())
    except Exception:
        logger.debug("Failed to load autonomous mode state", exc_info=True)
    return {}


def _is_claude_running(session_name: str) -> bool:
    """Check if Claude Code process is running in the session (not just a bare shell).

    When Claude Code OOMs or crashes, the tmux pane falls back to the parent
    shell (bash/zsh). This function distinguishes that from Claude Code running.
    Returns True if Claude (node) is the foreground process, False if bare shell.
    """
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p",
             "#{pane_current_command}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return False
        cmd = result.stdout.strip().lower()
        # Claude Code runs as 'node' or sometimes 'claude'. A bare shell is bash/zsh/sh/fish.
        shell_commands = {"bash", "zsh", "sh", "fish", "dash", "-bash", "-zsh", "-sh"}
        if cmd in shell_commands:
            return False
        # If it's node, claude, or anything else non-shell, Claude is likely running
        return True
    except Exception:
        return False


async def _async_is_claude_running(session_name: str) -> bool:
    """Non-blocking version of _is_claude_running."""
    return await asyncio.to_thread(_is_claude_running, session_name)


async def _ensure_claude_running(session_name: str, log_fn=None, state: dict = None) -> bool:
    """Check if Claude Code is running; if not, restart it. Returns True if Claude is running after check.

    This handles OOM crashes where Claude dies and the pane falls back to bash.
    """
    alog = logging.getLogger("autonomous")
    if await _async_is_claude_running(session_name):
        return True

    msg = f"Claude Code not running in '{session_name}' — restarting it"
    alog.warning(msg)
    if log_fn and state:
        log_fn(state, msg)

    try:
        # Send claude command to the bare shell
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "claude --dangerously-skip-permissions", "Enter"],
            capture_output=True, text=True, timeout=5)

        # Wait for Claude Code to start (up to 30s)
        for _ in range(15):
            await asyncio.sleep(2)
            if await _async_is_claude_running(session_name):
                alog.info(f"Claude Code restarted successfully in '{session_name}'")
                if log_fn and state:
                    log_fn(state, "Claude Code restarted successfully")
                # Give it a moment to fully initialize
                await asyncio.sleep(5)
                return True

        alog.error(f"Failed to restart Claude Code in '{session_name}' after 30s")
        if log_fn and state:
            log_fn(state, "Failed to restart Claude Code after 30s")
        return False
    except Exception as e:
        alog.error(f"Error restarting Claude Code in '{session_name}': {e}")
        return False


_background_tasks: list = []


@asynccontextmanager
async def lifespan(_app: FastAPI):
    """Manage startup and shutdown for the tmux Dashboard FastAPI application."""
    # --- Startup ---
    loop = asyncio.get_running_loop()
    loop.set_default_executor(ThreadPoolExecutor(max_workers=20))
    logger.info("tmux Dashboard starting up — port=%s, root_path=%s, auth=%s, openai=%s",
                PORT, ROOT_PATH,
                "enabled" if AUTH_PASS else "disabled",
                "configured" if OPENAI_API_KEY else "missing")
    if not AUTH_PASS:
        logger.warning("TMUX_DASH_PASS is not set — authentication is DISABLED. "
                       "Set TMUX_DASH_PASS to enable auth.")
    if not OPENAI_API_KEY:
        logger.warning("OPENAI_API_KEY is not set — LLM summaries will not work.")
    if not os.environ.get("TMUX_DASH_SECRET"):
        logger.warning("TMUX_DASH_SECRET is not set — auth tokens will be invalidated on restart. "
                       "Set a persistent secret for stable sessions.")
    sessions = get_tmux_sessions()
    logger.info("Found %d existing tmux sessions", len(sessions))
    task = asyncio.create_task(_auto_responder_loop())
    _background_tasks.append(task)
    logger.info("Auto-responder background task started")
    watchdog_task = asyncio.create_task(_watchdog_loop())
    _background_tasks.append(watchdog_task)
    logger.info("Autonomous mode watchdog started")

    # Restore persistent autonomous mode state from disk
    saved = _load_autonomous_state()
    if saved:
        session_names = {s["name"] for s in sessions}
        for name, modes in saved.items():
            if name not in session_names:
                logger.info("Skipping autonomous restore for '%s' — session no longer exists", name)
                continue
            if modes.get("away_mode"):
                logger.info("Restoring Away Mode for '%s' (was active before restart)", name)
                state = {
                    "enabled": True, "phase": 4, "phase_name": "Continuous (restored)",
                    "step": 0, "step_name": "Restored after restart",
                    "started_at": time.time(), "log": [], "report": "", "task": None,
                }
                _away_mode_state[name] = state
                _away_log(state, "Away mode restored after server restart")
                t = asyncio.create_task(_restore_autonomous_mode(name, state, "away"))
                state["task"] = t
            elif modes.get("go_nuts_mode"):
                logger.info("Restoring Go Nuts Mode for '%s' (was active before restart)", name)
                state = {
                    "enabled": True, "phase": 4, "phase_name": "Continuous Build (restored)",
                    "step": 0, "step_name": "Restored after restart",
                    "started_at": time.time(), "log": [], "report": "", "task": None,
                }
                _go_nuts_state[name] = state
                _go_nuts_log(state, "Go Nuts mode restored after server restart")
                t = asyncio.create_task(_restore_autonomous_mode(name, state, "gonuts"))
                state["task"] = t

    # Clean up orphaned entries (sessions that no longer exist were skipped above
    # but file still has their old state). Re-save now based on in-memory dicts only.
    _save_autonomous_state()

    yield  # Application is running

    # --- Shutdown ---
    global _shutting_down
    _shutting_down = True  # Prevent CancelledError handlers from wiping persisted state
    logger.info("tmux Dashboard shutting down — cancelling %d background tasks", len(_background_tasks))
    # Save autonomous mode state BEFORE cancelling tasks (so enabled=True is preserved)
    _save_autonomous_state()
    logger.info("Autonomous mode state saved to disk for restore on next startup")
    for t in _background_tasks:
        if not t.done():
            t.cancel()
    # Cancel any running away-mode workers
    for name, state in _away_mode_state.items():
        if state.get("task") and not state["task"].done():
            state["task"].cancel()
            logger.info("Cancelled away-mode worker for '%s'", name)
    # Cancel any running go-nuts-mode workers
    for name, state in _go_nuts_state.items():
        if state.get("task") and not state["task"].done():
            state["task"].cancel()
            logger.info("Cancelled go-nuts-mode worker for '%s'", name)
    logger.info("Shutdown complete")


app = FastAPI(root_path=ROOT_PATH, lifespan=lifespan)

# --- Auth ---
AUTH_USER = os.environ.get("TMUX_DASH_USER", "admin")
AUTH_PASS = os.environ.get("TMUX_DASH_PASS", "")
AUTH_SECRET = os.environ.get("TMUX_DASH_SECRET", secrets.token_hex(32))


def _make_token(username: str) -> str:
    sig = hmac.new(AUTH_SECRET.encode(), username.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{username}:{sig}"


def _check_token(token: str) -> bool:
    if not token or ":" not in token:
        return False
    username, sig = token.split(":", 1)
    expected = hmac.new(AUTH_SECRET.encode(), username.encode(), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(sig, expected)


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>tmux Dashboard — Login</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh;display:flex;align-items:center;justify-content:center}
.login-box{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:32px;width:340px}
.login-box h2{color:#f0f6fc;margin-bottom:8px;font-size:1.2rem}
.login-box p{color:#8b949e;font-size:.85rem;margin-bottom:20px}
.field{margin-bottom:14px}
.field label{display:block;font-size:.8rem;color:#8b949e;margin-bottom:4px}
.field input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:10px 12px;font-size:.95rem;outline:none}
.field input:focus{border-color:#58a6ff}
.err{color:#f85149;font-size:.8rem;margin-bottom:10px;display:none}
.login-btn{width:100%;background:#1f6feb;color:#fff;border:none;padding:10px;border-radius:6px;cursor:pointer;font-size:.95rem;font-weight:500}
.login-btn:hover{background:#388bfd}
</style></head><body>
<form class="login-box" method="POST" action="login">
  <h2>tmux Dashboard</h2>
  <p>Enter credentials to continue.</p>
  <div class="err" id="err">Invalid username or password.</div>
  <div class="field"><label>Username</label><input name="username" autocomplete="username" autofocus></div>
  <div class="field"><label>Password</label><input name="password" type="password" autocomplete="current-password"></div>
  <button class="login-btn" type="submit">Log in</button>
</form>
<script>if(location.search.includes('err=1'))document.getElementById('err').style.display='block'</script>
</body></html>"""


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Add security headers to all responses and log slow requests."""
    start = time.time()
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["X-XSS-Protection"] = "1; mode=block"
    response.headers["Permissions-Policy"] = "camera=(), microphone=(), geolocation=()"
    # CSP: allow inline scripts/styles (needed for the embedded single-page HTML) and blob: for xterm.js
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; "
        "script-src 'self' 'unsafe-inline' blob:; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "connect-src 'self'; "
        "font-src 'self' data:; "
        "frame-ancestors 'self'"
    )
    duration = time.time() - start
    if duration > 2.0:
        logger.warning("Slow request: %s %s took %.1fs", request.method, request.url.path, duration)
    return response


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    # Skip auth entirely if no password is configured
    if not AUTH_PASS:
        return await call_next(request)
    path = request.url.path
    # Allow login routes without auth
    rp = request.scope.get("root_path", "")
    if path in ("/login", "/login/", rp + "/login", rp + "/login/"):
        return await call_next(request)
    token = request.cookies.get("tmux_auth")
    if not _check_token(token):
        resp = HTMLResponse(LOGIN_PAGE)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    return await call_next(request)


# Simple in-memory login rate limiter: (ip, window_start_minute) -> attempt_count
_login_attempts: Dict[str, int] = {}
_LOGIN_MAX_ATTEMPTS = 10  # per IP per minute
_LOGIN_WINDOW = 60        # seconds


def _check_login_rate_limit(ip: str) -> bool:
    """Return True if the IP is allowed to attempt login, False if rate-limited."""
    now = time.time()
    window_key = f"{ip}:{int(now // _LOGIN_WINDOW)}"
    count = _login_attempts.get(window_key, 0)
    if count >= _LOGIN_MAX_ATTEMPTS:
        return False
    _login_attempts[window_key] = count + 1
    # Prune old keys to avoid unbounded growth
    stale = [k for k in list(_login_attempts) if k != window_key and k.split(":")[0] == ip]
    for k in stale:
        del _login_attempts[k]
    return True


@app.post("/login")
async def do_login(request: Request):
    ip = request.client.host if request.client else "unknown"
    if not _check_login_rate_limit(ip):
        logger.warning("Login rate limit exceeded for IP %s", ip)
        return HTMLResponse("Too many login attempts. Please wait a moment.", status_code=429)
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if hmac.compare_digest(username, AUTH_USER) and hmac.compare_digest(password, AUTH_PASS):
        token = _make_token(username)
        resp = RedirectResponse(url=request.scope.get("root_path", "") + "/", status_code=303)
        is_https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
        resp.set_cookie("tmux_auth", token, max_age=86400 * 30, httponly=True, samesite="lax", secure=is_https)
        return resp
    return RedirectResponse(url=request.scope.get("root_path", "") + "/login?err=1", status_code=303)


# Three-tier cache per session
cache: Dict[str, dict] = {}

# Persistent message storage
MESSAGES_FILE = MESSAGES_DIR / "messages.json"
NOTES_FILE = MESSAGES_DIR / "notes.json"


def _load_all_notes() -> Dict[str, str]:
    """Load all session notes from disk."""
    try:
        if NOTES_FILE.exists():
            return json.loads(NOTES_FILE.read_text())
    except Exception:
        logger.debug("Failed to load notes from %s", NOTES_FILE, exc_info=True)
    return {}


def _save_notes():
    """Persist all session notes to disk."""
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        existing = _load_all_notes()
        for name, entry in cache.items():
            notes = entry.get("notes")
            if notes:
                existing[name] = notes
        NOTES_FILE.write_text(json.dumps(existing))
    except Exception:
        logger.debug("Failed to save notes to %s", NOTES_FILE, exc_info=True)


def _load_session_notes(session_name: str) -> str:
    """Get persisted notes for a specific session."""
    return _load_all_notes().get(session_name, "")


def _load_messages() -> Dict[str, list]:
    """Load all session messages from disk."""
    try:
        if MESSAGES_FILE.exists():
            return json.loads(MESSAGES_FILE.read_text())
    except Exception:
        logger.debug("Failed to load messages from %s", MESSAGES_FILE, exc_info=True)
    return {}


def _save_messages():
    """Persist all session messages to disk (merge with existing)."""
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        # Load existing to avoid dropping sessions not yet in cache
        existing = _load_messages()
        # Update with current cache data
        for name, entry in cache.items():
            msgs = entry.get("messages")
            if msgs:
                existing[name] = msgs
        MESSAGES_FILE.write_text(json.dumps(existing))
    except Exception:
        logger.debug("Failed to save messages to %s", MESSAGES_FILE, exc_info=True)


def _load_session_messages(session_name: str) -> list:
    """Get persisted messages for a specific session."""
    all_msgs = _load_messages()
    return all_msgs.get(session_name, [])


DESCRIPTION_TTL = 0    # never auto-expire
PROGRESS_TTL = 600     # 10 minutes
REALTIME_TTL = 15      # 15 seconds — text extraction is cheap (no LLM call usually)
NOTES_TTL = 600        # 10 minutes


def get_tmux_sessions() -> list[dict]:
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F",
             "#{session_name}:#{session_windows}:#{session_created}:#{session_attached}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return []
        sessions = []
        for line in result.stdout.strip().split("\n"):
            if not line:
                continue
            parts = line.split(":")
            name = parts[0]
            if name.startswith("__") and name.endswith("__"):
                continue  # Skip internal sessions (e.g. __auth_login_tmp__)
            sessions.append({
                "name": name,
                "windows": parts[1] if len(parts) > 1 else "?",
                "created": parts[2] if len(parts) > 2 else "",
                "attached": parts[3] == "1" if len(parts) > 3 else False,
            })
        return sessions
    except Exception:
        return []


def _find_session(session_name: str) -> tuple:
    """Look up a tmux session by name.

    Returns (sessions_list, session_dict) if found, or (sessions_list, None) if not.
    """
    sessions = get_tmux_sessions()
    for s in sessions:
        if s["name"] == session_name:
            return sessions, s
    return sessions, None


def capture_pane_full(session_name: str) -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", "-"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def capture_pane_recent(session_name: str, lines: int = 80) -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def get_pane_position(session_name: str) -> dict:
    """Get current pane line-count metadata (cheap, no content capture).

    Uses history_size + pane_height (not cursor_y) so the count only changes
    when new content actually scrolls up, not when the cursor moves within
    the visible area (status bar updates, etc.).  This prevents false deltas
    that cause duplicate lines in the terminal view.
    """
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p",
             "#{history_size}:#{pane_height}"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(":")
            history_size = int(parts[0])
            pane_height = int(parts[1])
            return {"total_lines": history_size + pane_height}
    except Exception:
        logger.debug("Failed to get pane position for '%s'", session_name, exc_info=True)
    return {"total_lines": 0}


# Track auto-approve state to avoid re-triggering
_auto_approve_sent: Dict[str, float] = {}

# Content stability tracking for idle detection
# Stores (hash, first_seen_time, consecutive_count) per session
_pane_stability: Dict[str, tuple] = {}

# Hysteresis for activity detection — prevents rapid busy/idle flickering.
# Stores per session: {"status": str, "since": float, "consecutive_idle": int, "raw": str}
_activity_state: Dict[str, dict] = {}
# Require N consecutive idle readings before switching from busy → idle.
# At 10s polling interval, 3 readings = ~30 seconds of consistent idle signal.
IDLE_CONFIRM_COUNT = 3

# Pre-compiled regexes for activity detection (hot path — called every ~10s per session)
_SPINNER_ICONS = r'[✶✽✻☆◆●⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✢✦✧✹✵✴✸❋❊❉✺◇◈⟡⊛⊕⊗▸▹►▻◉◎★♦♢⬡⬢]'
_RE_COMPLETION = re.compile(
    r'^[✶✽✻●⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✢✦✧✹✵✴✸❋❊❉✺◇◈⟡⊛⊕⊗▸▹►▻◉◎★♦♢⬡⬢☆◆]\s+'
    r'(?:Done|Completed|[A-Z][a-zé]+(?:ed|d)\s+for\s+\d+[hms])'
)
_RE_RUNNING_TASK = re.compile(r'^[⎿\s]*◼')
_RE_SPINNER_START = re.compile(_SPINNER_ICONS + r'\s+\w+(?:…|\.{2,3})')
_RE_SPINNER_INLINE = re.compile(_SPINNER_ICONS + r'\s+\w+(?:…|\.{2,3})(?:\s*\(.*?\))?\s*$')
_RE_THOUGHT = re.compile(r'\(thought for \d+')
_RE_SHELL_PROMPT = re.compile(r'[\$#%>]\s*$')
_RE_IDLE_PROMPT = re.compile(r'^[❯➜]\s*$')
_RE_TIP_CLAUDE = re.compile(r'Tip:.*claude')
_RE_COMPLETION_MSG = re.compile(r'[A-Z][a-zé]+ for \d+[ms]')


_AUTONOMOUS_KEYWORDS = [
    "don't ask", "without asking", "bypass", "skip permission",
    "autonomous", "all permissions", "proceed without",
    "do everything", "yes to all", "approve all",
    "don't confirm", "without confirm", "skip confirm",
    "no further", "without further",
]


def _check_auto_approve(session_name: str, visible: str):
    """Detect Claude Code permission prompts and numbered question prompts,
    then auto-select the most autonomous / 'just do it' option."""
    # Don't re-trigger within 10 seconds
    last = _auto_approve_sent.get(session_name, 0)
    if time.time() - last < 10:
        return

    lines = visible.split("\n")

    # --- Strategy 1: Permission prompt with "Yes, and bypass" ---
    option2_line = -1
    selected_line = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        if re.search(r'2\.\s+Yes.*bypass', stripped):
            option2_line = i
        if stripped.startswith('❯') or stripped.startswith('>'):
            selected_line = i

    if option2_line >= 0 and selected_line >= 0:
        downs = option2_line - selected_line
        if downs >= 0:
            _send_option(session_name, downs)
            return

    # --- Strategy 2: Numbered question prompt (1. / 2. / 3. style) ---
    # Claude sometimes asks the user to pick from numbered options after planning.
    # We look for a list of numbered options and pick the most autonomous one.
    numbered_options = {}  # line_index -> (number, text)
    for i, line in enumerate(lines):
        stripped = line.strip()
        m = re.match(r'^(\d+)[.\-\)]\s+(.+)', stripped)
        if m:
            numbered_options[i] = (int(m.group(1)), m.group(2))

    if len(numbered_options) >= 2:
        # Find the option that means "do it all, don't ask again"
        best_line = None
        best_score = -1
        for line_idx, (num, text) in numbered_options.items():
            lower = text.lower()
            score = sum(1 for kw in _AUTONOMOUS_KEYWORDS if kw in lower)
            # Also favor option 1 as tiebreaker (usually the most autonomous)
            if score > best_score or (score == best_score and best_line is not None
                                      and num < numbered_options.get(best_line, (999, ""))[0]):
                best_score = score
                best_line = line_idx

        # Only act if we found a clear autonomous option (keyword match)
        # or if there are exactly 2-3 options and option 1 mentions doing/proceeding
        if best_score > 0 and best_line is not None:
            target_num = numbered_options[best_line][0]
            # Type the number and press Enter
            try:
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name, "-l", str(target_num)],
                    capture_output=True, text=True, timeout=3
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", session_name, "Enter"],
                    capture_output=True, text=True, timeout=3
                )
                _auto_approve_sent[session_name] = time.time()
            except Exception:
                logger.debug("Auto-approve send failed for '%s'", session_name, exc_info=True)
            return

    # --- Strategy 3: AskUserQuestion with labeled options (cursor-based) ---
    # Claude Code sometimes presents options where ❯ is the selector and
    # options contain labels. Pick the one with autonomous keywords.
    if selected_line >= 0:
        option_lines = []
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Options in a cursor-based list start with ❯, >, or spaces (unselected)
            if re.match(r'^[❯>\s]\s+\S', stripped):
                option_lines.append((i, stripped.lstrip('❯> ')))
        if len(option_lines) >= 2:
            autonomous_target = None
            for idx, (line_i, text) in enumerate(option_lines):
                lower = text.lower()
                for kw in _AUTONOMOUS_KEYWORDS:
                    if kw in lower:
                        autonomous_target = line_i
                        break
                if autonomous_target is not None:
                    break
            if autonomous_target is not None:
                downs = autonomous_target - selected_line
                if downs >= 0:
                    _send_option(session_name, downs)
                    return


def _send_option(session_name: str, downs: int):
    """Send Down arrow keys + Enter to select an option in a tmux pane."""
    try:
        for _ in range(downs):
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Down"],
                capture_output=True, text=True, timeout=3
            )
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            capture_output=True, text=True, timeout=3
        )
        _auto_approve_sent[session_name] = time.time()
    except Exception:
        logger.debug("Failed to send option keys to '%s'", session_name, exc_info=True)


def _detect_activity_raw(session_name: str) -> dict:
    """Raw single-snapshot activity detection (no debounce)."""
    info = {"status": "unknown", "command": "", "detail": ""}
    try:
        # Get the foreground command and pane pid
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p",
             "#{pane_current_command}:#{pane_pid}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return info

        parts = result.stdout.strip().split(":")
        cmd = parts[0] if parts else ""
        info["command"] = cmd

        # Capture the very bottom of the visible pane — this is the ground truth.
        # tmux capture-pane without -S captures just the visible area.
        try:
            vis = subprocess.run(
                ["tmux", "capture-pane", "-t", session_name, "-p"],
                capture_output=True, text=True, timeout=5
            )
            visible = vis.stdout if vis.returncode == 0 else ""
        except Exception:
            visible = ""

        # Auto-approve plan/permission prompts
        _check_auto_approve(session_name, visible)

        all_lines = visible.split("\n")
        # Strip trailing empty lines to find the real bottom
        while all_lines and not all_lines[-1].strip():
            all_lines.pop()

        # Look at the bottom 6 lines to catch prompt + status bar + separators
        bottom = all_lines[-6:] if len(all_lines) >= 6 else all_lines
        bottom_text = "\n".join(bottom)

        # --- Step 1: Check "esc to interrupt" — strongest busy signal ---
        # This appears in Claude Code's status bar when a task is actively running.
        has_esc_to_interrupt = "esc to interrupt" in bottom_text

        # --- Step 2: Check for idle prompt indicators in bottom area ---
        idle_prompt_patterns = [_RE_IDLE_PROMPT, _RE_TIP_CLAUDE, _RE_COMPLETION_MSG]
        # Lines containing these phrases override idle — session is still working
        busy_overrides = [
            "still running",
            "agents running",
            "waiting for completion",
            "in progress",
        ]
        has_idle_prompt = False
        for pattern in idle_prompt_patterns:
            for line in bottom:
                stripped = line.strip()
                if pattern.search(stripped):
                    # Check if the same line has a busy override
                    lower = stripped.lower()
                    if any(phrase in lower for phrase in busy_overrides):
                        continue  # not truly idle
                    has_idle_prompt = True
                    break
            if has_idle_prompt:
                break

        # --- Step 3: Check for active spinners/progress ---
        # Scan a wider window (bottom 25 lines) because Claude Code's
        # spinners and task indicators appear in the content area
        # *above* the bottom chrome.  This MUST run before we return
        # idle — the ❯ prompt is always visible even while Claude is
        # executing tools / thinking / streaming.
        window = all_lines[-25:] if len(all_lines) >= 25 else all_lines

        # All checks are LINE-BY-LINE.  Start-of-line anchoring is used
        # where possible to avoid false positives from these patterns
        # appearing in conversation output text.
        # (Regexes are pre-compiled at module level: _RE_COMPLETION, etc.)
        for line in window:
            stripped = line.strip()
            # Skip completion markers — these look like spinners but mean "finished"
            if _RE_COMPLETION.match(stripped):
                continue
            # ◼ at start of line (with optional ⎿ tree prefix) = running task
            if _RE_RUNNING_TASK.match(stripped):
                info["status"] = "busy"
                info["detail"] = "Running task"
                return info
            # Spinner icon + verb… at START of line
            if _RE_SPINNER_START.match(stripped):
                info["status"] = "busy"
                if '(thinking)' in stripped or 'thought for' in stripped:
                    info["detail"] = "Thinking"
                else:
                    info["detail"] = "Working"
                return info
            # Spinner icon + verb… anywhere in line (catches inline spinners)
            if _RE_SPINNER_INLINE.search(stripped):
                info["status"] = "busy"
                if '(thinking)' in stripped or 'thought for' in stripped:
                    info["detail"] = "Thinking"
                else:
                    info["detail"] = "Working"
                return info
            # "(thought for Xs)" or "(thinking)" near end of line — strong busy signal
            if _RE_THOUGHT.search(stripped) or stripped.endswith('(thinking)'):
                info["status"] = "busy"
                info["detail"] = "Thinking"
                return info
            # "N local agents still running" or "Waiting for completion" = busy
            lower = stripped.lower()
            if "still running" in lower or "waiting for completion" in lower:
                info["status"] = "busy"
                info["detail"] = "Agents running"
                return info

        # --- Step 4: Content stability check ---
        # If the terminal content hasn't changed for 20+ seconds and there's no
        # "esc to interrupt", the session is idle — real work produces output,
        # real spinners animate.  This catches cases text patterns miss.
        content_hash = hashlib.md5(visible.encode()).hexdigest()
        now = time.time()
        prev = _pane_stability.get(session_name)
        if prev and prev[0] == content_hash:
            # Content unchanged since last check
            stable_since = prev[1]
            stable_seconds = now - stable_since
            _pane_stability[session_name] = (content_hash, stable_since, prev[2] + 1)
        else:
            # Content changed — reset
            stable_seconds = 0
            _pane_stability[session_name] = (content_hash, now, 1)

        content_is_static = stable_seconds >= 20

        # --- Step 5: If idle prompt + no busy signals → truly idle ---
        if has_idle_prompt and not has_esc_to_interrupt:
            info["status"] = "idle"
            info["detail"] = ""
            return info

        # "esc to interrupt" without a spinner = background tasks running
        if has_esc_to_interrupt:
            info["status"] = "busy"
            info["detail"] = "Background tasks"
            return info

        # --- Step 6: Static content override ---
        # If the terminal hasn't changed in 20+ seconds and the foreground
        # command is claude/node, it's almost certainly idle — the text-based
        # checks above may have missed it or the output just looks ambiguous.
        if content_is_static and cmd.lower() in ("claude", "node"):
            info["status"] = "idle"
            info["detail"] = ""
            return info

        # --- Step 7: Shell prompt check ---
        last_line = bottom[-1].strip() if bottom else ""
        shell_cmds = {"bash", "zsh", "sh", "fish", "tmux"}
        if cmd.lower() in shell_cmds:
            if _RE_SHELL_PROMPT.search(last_line) or not last_line:
                info["status"] = "idle"
                info["detail"] = "Shell prompt"
            else:
                info["status"] = "busy"
                info["detail"] = cmd
        elif cmd.lower() in ("claude", "node"):
            # Claude Code with no spinner + no "esc to interrupt" = idle
            info["status"] = "idle"
            info["detail"] = ""
        else:
            info["status"] = "busy"
            info["detail"] = cmd
    except Exception:
        logger.debug("Activity detection failed for session '%s'", session_name, exc_info=True)
    return info


def detect_activity(session_name: str) -> dict:
    """Debounced activity detection with asymmetric hysteresis.

    - busy → idle: requires IDLE_CONFIRM_COUNT consecutive idle readings (~30s)
    - idle → busy: immediate (1 reading)

    This prevents flickering when Claude Code briefly shows no spinner
    between tool calls or streaming chunks.
    """
    raw = _detect_activity_raw(session_name)
    now = time.time()
    prev = _activity_state.get(session_name)

    if prev is None:
        # First reading — accept as-is
        _activity_state[session_name] = {
            "status": raw["status"],
            "since": now,
            "consecutive_idle": 1 if raw["status"] == "idle" else 0,
            "raw": raw,
        }
        return raw

    if raw["status"] == "busy":
        # Busy is always accepted immediately — reset idle counter
        _activity_state[session_name] = {
            "status": "busy",
            "since": now if prev["status"] != "busy" else prev["since"],
            "consecutive_idle": 0,
            "raw": raw,
        }
        return raw

    if raw["status"] in ("idle", "unknown"):
        if prev["status"] == "busy":
            # Trying to transition busy → idle: increment counter but hold busy
            idle_count = prev["consecutive_idle"] + 1
            if idle_count >= IDLE_CONFIRM_COUNT:
                # Enough consecutive idle readings — confirm transition
                _activity_state[session_name] = {
                    "status": raw["status"],
                    "since": now,
                    "consecutive_idle": idle_count,
                    "raw": raw,
                }
                return raw
            else:
                # Not enough yet — stay busy but record the idle reading
                _activity_state[session_name] = {
                    "status": "busy",
                    "since": prev["since"],
                    "consecutive_idle": idle_count,
                    "raw": prev["raw"],  # keep last busy details
                }
                return prev["raw"]
        else:
            # Already idle/unknown — stay idle, keep counting
            _activity_state[session_name] = {
                "status": raw["status"],
                "since": prev["since"],
                "consecutive_idle": prev["consecutive_idle"] + 1,
                "raw": raw,
            }
            return raw

    # Fallback
    _activity_state[session_name] = {
        "status": raw["status"],
        "since": now,
        "consecutive_idle": 0,
        "raw": raw,
    }
    return raw


async def async_detect_activity(session_name: str) -> dict:
    """Non-blocking detect_activity — runs in thread pool to avoid blocking the event loop."""
    return await asyncio.to_thread(detect_activity, session_name)


async def llm_call(system_prompt: str, user_content: str, max_tokens: int = 200) -> str:
    start = time.time()
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
        duration = time.time() - start
        tokens_used = getattr(resp.usage, "total_tokens", 0) if resp.usage else 0
        logger.debug("LLM call completed in %.1fs, %d tokens", duration, tokens_used)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        duration = time.time() - start
        logger.error("LLM call failed after %.1fs: %s", duration, e)
        return f"(error: {e})"


async def get_title_and_description(session_name: str, full_output: str) -> tuple:
    """Return (title, description) for a session."""
    lines = full_output.split("\n")
    early = "\n".join(lines[:150])
    mid_start = len(lines) // 3
    middle = "\n".join(lines[mid_start:mid_start + 80])
    context = f"=== EARLIEST OUTPUT (first 150 lines) ===\n{early}\n\n=== MIDDLE SECTION ===\n{middle}"
    truncated = context[:4000]

    title_coro = llm_call(
        system_prompt=(
            "Given terminal output from a tmux session, produce a SHORT title (3-6 words) "
            "naming the project or task. Use the actual project name or directory if visible. "
            "Examples: 'monitor-app LLM re-match', 'tmux-dashboard project', "
            "'Next.js frontend build'. "
            "Return ONLY the title, no quotes, no punctuation at the end."
        ),
        user_content=f"tmux session '{session_name}':\n\n{truncated}",
        max_tokens=30,
    )
    desc_coro = llm_call(
        system_prompt=(
            "You summarize what a terminal session is for. Write ONE short plain sentence. "
            "Be informative and specific — mention the tool, the actual project name, and "
            "what it does or where it runs if you can tell. "
            "Write like a human would casually describe it to a colleague.\n"
            "GOOD examples:\n"
            "- 'Claude Code working on the product monitoring app at monitor.grabo.cc'\n"
            "- 'Building and testing the tmux-dashboard FastAPI service'\n"
            "- 'Running a data migration script for the user database'\n"
            "BAD examples (too verbose/robotic):\n"
            "- 'This tmux session is for the Claude Code AI assistant working on...'\n"
            "- 'The session involves running and debugging an LLM-based...'\n"
            "Keep it under 20 words. No filler phrases. No 'This session is for...'."
        ),
        user_content=f"tmux session '{session_name}':\n\n{truncated}",
        max_tokens=60,
    )
    title, description = await asyncio.gather(title_coro, desc_coro)
    return title, description


async def get_progress(session_name: str, full_output: str) -> str:
    lines = full_output.split("\n")
    total = len(lines)
    slices = [("BEGINNING", "\n".join(lines[:60]))]
    if total > 200:
        q1 = total // 4
        slices.append(("QUARTER", "\n".join(lines[q1:q1 + 50])))
    if total > 300:
        mid = total // 2
        slices.append(("MIDDLE", "\n".join(lines[mid:mid + 50])))
    if total > 400:
        q3 = (total * 3) // 4
        slices.append(("THREE-QUARTER", "\n".join(lines[q3:q3 + 50])))
    slices.append(("RECENT", "\n".join(lines[-60:])))
    context = "\n\n".join(f"=== {label} ===\n{text}" for label, text in slices)
    return await llm_call(
        system_prompt=(
            "Summarize what was accomplished in this terminal session so far. "
            "Write 1-3 short plain sentences listing concrete things that were built, "
            "fixed, or completed. Use casual first-person plural ('we') and past tense. "
            "Focus on WHAT was done, not HOW (don't mention commands, bash, git, etc.).\n"
            "GOOD example: 'Built a price parser module and multi-tier classifier. "
            "Improved scraper extraction and finished the analytics dashboard.'\n"
            "BAD example: 'Several key tasks were completed including building modules "
            "and running scripts. Files such as matching.py were referenced.'\n"
            "Be condensed. Under 40 words. No filler."
        ),
        user_content=f"tmux session '{session_name}' sampled history:\n\n{context[:5000]}",
        max_tokens=100,
    )


async def get_notes(session_name: str, full_output: str, existing_notes: str = "", messages: list = None) -> str:
    """Extract key reference info from terminal output and chat history."""
    lines = full_output.split("\n")
    total = len(lines)
    slices = [("BEGINNING", "\n".join(lines[:80]))]
    if total > 200:
        q1 = total // 4
        slices.append(("QUARTER", "\n".join(lines[q1:q1 + 60])))
    if total > 300:
        mid = total // 2
        slices.append(("MIDDLE", "\n".join(lines[mid:mid + 60])))
    slices.append(("RECENT", "\n".join(lines[-80:])))
    context = "\n\n".join(f"=== {label} ===\n{text}" for label, text in slices)

    # Include chat messages (captures uploaded files, user commands, etc.)
    chat_section = ""
    if messages:
        recent_msgs = messages[-30:]  # last 30 messages
        chat_lines = [f"[{m['role']}] {m['text']}" for m in recent_msgs]
        chat_section = "\n\n=== CHAT HISTORY (user commands & uploads) ===\n" + "\n".join(chat_lines)

    prev_section = ""
    if existing_notes and existing_notes.strip():
        prev_section = f"\n\n=== PREVIOUS NOTES (merge new findings into these) ===\n{existing_notes}"

    return await llm_call(
        system_prompt=(
            "Extract key reference info from this terminal session. "
            "Organize into these sections:\n\n"
            "CREDENTIALS — usernames, passwords, API keys, tokens, secrets\n"
            "URLS — public URLs, domains, endpoints where this project is served or accessible\n"
            "STACK — languages, frameworks, libraries, dependencies, tools, package managers\n"
            "SERVICES — databases, ports, process managers (PM2/supervisor/systemd), background services\n"
            "STRUCTURE — project root, key files, directories, config file paths\n"
            "UPLOADS — paths to any files that were uploaded to this session\n"
            "NOTES — important dev decisions, gotchas, deployment steps, things to remember\n\n"
            "Rules:\n"
            "- Only include info actually visible in the terminal output or chat history\n"
            "- Keep each item on one line, be specific (include actual values, paths, ports)\n"
            "- If a section has nothing, omit it entirely\n"
            "- If previous notes exist, merge new findings into them — keep old data, "
            "remove duplicates, update changed values\n"
            "- Redact nothing — this is the developer's own reference\n"
            "- No intro/outro text, just the section headers and their items"
        ),
        user_content=f"tmux session '{session_name}' sampled history:\n\n{context[:5000]}{chat_section[:1500]}{prev_section}",
        max_tokens=500,
    )


def _extract_claude_text(terminal_output: str) -> str:
    """Extract Claude's human-readable text from terminal output.

    Claude's terminal output uses these patterns:
    - '● Text here...' = Claude's spoken text (INCLUDE)
    - '● ToolName(args...)' = Tool call (EXCLUDE)
    - '  ⎿ ...' = Tool output / indented continuation (EXCLUDE)
    - '✻ ...' = Status line (EXCLUDE)
    - Lines starting with '❯' = User prompt (EXCLUDE)

    Returns extracted text paragraphs joined by newlines.
    """
    lines = terminal_output.split("\n")
    # Known tool prefixes that indicate a tool call, not text
    tool_names = (
        "Bash(", "Read(", "Edit(", "Write(", "Grep(", "Glob(", "Task(",
        "WebFetch(", "WebSearch(", "NotebookEdit(", "AskUser", "Skill(",
        "EnterPlanMode", "ExitPlanMode", "TaskCreate(", "TaskUpdate(",
        "TaskGet(", "TaskList(", "TodoWrite(", "mcp__",
    )
    text_blocks = []
    current_block = []
    in_text_block = False
    in_tool_block = False

    for line in lines:
        stripped = line.strip()
        # Detect start of a Claude text block (● followed by text, not a tool)
        if stripped.startswith("●"):
            content_after = stripped[1:].strip()
            # Check if this is a tool call
            is_tool = any(content_after.startswith(t) for t in tool_names)
            if is_tool:
                # End any current text block
                if current_block:
                    text_blocks.append("\n".join(current_block))
                    current_block = []
                in_text_block = False
                in_tool_block = True
            else:
                # This is Claude's spoken text
                if current_block:
                    text_blocks.append("\n".join(current_block))
                    current_block = []
                in_text_block = True
                in_tool_block = False
                if content_after:
                    current_block.append(content_after)
        elif stripped.startswith("⎿") or stripped.startswith("⎿"):
            # Tool output — skip
            in_text_block = False
            in_tool_block = True
        elif stripped.startswith("✻") or stripped.startswith("❯"):
            # Status line or user prompt — end block
            if current_block:
                text_blocks.append("\n".join(current_block))
                current_block = []
            in_text_block = False
            in_tool_block = False
        elif stripped.startswith("───") or stripped == "":
            # Separator or blank line
            if in_text_block and current_block:
                # Blank line within text block — preserve as paragraph break
                if stripped == "":
                    current_block.append("")
                else:
                    text_blocks.append("\n".join(current_block))
                    current_block = []
                    in_text_block = False
        elif in_text_block:
            # Continuation of Claude's text (indented lines under ●)
            # Claude indents continuation lines with 2 spaces
            current_block.append(stripped)
        elif in_tool_block:
            # Skip tool output continuation
            pass

    if current_block:
        text_blocks.append("\n".join(current_block))

    return "\n\n".join(b for b in text_blocks if b.strip())


def _extract_claude_response_since_last_user(terminal_output: str) -> str:
    """Extract Claude's text response since the last user message (❯ prompt).

    Scans backward from the end of terminal output to find the last ❯ prompt,
    then extracts all Claude text blocks after it.
    """
    lines = terminal_output.split("\n")
    # Find the last user prompt line (❯)
    last_prompt_idx = -1
    for i in range(len(lines) - 1, -1, -1):
        stripped = lines[i].strip()
        if stripped.startswith("❯") and len(stripped) > 1:
            last_prompt_idx = i
            break

    # If no user prompt found, use all output
    if last_prompt_idx < 0:
        section = terminal_output
    else:
        section = "\n".join(lines[last_prompt_idx + 1:])

    return _extract_claude_text(section)


async def get_realtime(session_name: str) -> str:
    """Extract Claude's human-readable text from recent terminal output.

    Instead of LLM summarization, directly parses Claude's text output.
    Only falls back to LLM summarization if extracted text is very long (>500 words).
    """
    recent = await asyncio.to_thread(capture_pane_recent, session_name, 150)

    extracted = _extract_claude_response_since_last_user(recent)

    if not extracted.strip():
        return ""

    # If the extracted text is short enough, return it directly
    word_count = len(extracted.split())
    if word_count <= 500:
        return extracted.strip()

    # Text is very long — summarize it
    return await llm_call(
        system_prompt=(
            "Summarize Claude's response text into a concise message (2-4 sentences). "
            "Keep concrete details: file paths, URLs, numbers, outcomes. "
            "Write in first person as Claude would. Under 80 words."
        ),
        user_content=f"Claude's response text:\n\n{extracted[:4000]}",
        max_tokens=200,
    )


async def get_session_data(session_name: str, force_all: bool = False) -> dict:
    now = time.time()
    # IMPORTANT: use setdefault so `entry` is the SAME object as cache[session_name].
    # This prevents a race with api_send_command where a concurrent /send could
    # add a user message to a different cache entry, then get clobbered when this
    # function reassigns cache[session_name] below after awaiting LLM calls.
    entry = cache.setdefault(session_name, {})
    if "messages" not in entry:
        entry["messages"] = _load_session_messages(session_name)
    if "notes" not in entry:
        entry["notes"] = _load_session_notes(session_name)

    need_description = force_all or "description" not in entry
    need_progress = force_all or "progress" not in entry or (now - entry.get("progress_at", 0)) >= PROGRESS_TTL
    need_notes = force_all or "notes" not in entry or (now - entry.get("notes_at", 0)) >= NOTES_TTL

    full_output = None
    if need_description or need_progress or need_notes:
        full_output = capture_pane_full(session_name)

    tasks = {}
    if need_description:
        tasks["title_desc"] = get_title_and_description(session_name, full_output)
    if need_progress:
        tasks["progress"] = get_progress(session_name, full_output)
    if need_notes:
        tasks["notes"] = get_notes(session_name, full_output, entry.get("notes", ""), entry.get("messages", []))
    if force_all or "realtime" not in entry or (now - entry.get("realtime_at", 0)) >= REALTIME_TTL:
        tasks["realtime"] = get_realtime(session_name)

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
        if "notes" in result_map:
            entry["notes"] = result_map["notes"]
            entry["notes_at"] = now
        if "realtime" in result_map:
            entry["realtime"] = result_map["realtime"]
            entry["realtime_at"] = now
            _append_assistant_msg(entry, result_map["realtime"], now)

    cache[session_name] = entry
    if entry.get("messages"):
        _save_messages()
    if entry.get("notes"):
        _save_notes()
    return entry


def _msg_similarity(a: str, b: str) -> float:
    """Quick word-overlap similarity between two strings."""
    wa = set(a.lower().split())
    wb = set(b.lower().split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / max(len(wa), len(wb))


def _append_assistant_msg(entry: dict, text: str, ts: float):
    """Update or append an assistant message for the current Claude response.

    Instead of appending multiple assistant messages per response, we maintain
    a single assistant message after the last user message that gets updated
    as Claude produces more text. This keeps the chat clean:
    user → single assistant response → user → single assistant response.
    """
    if not text or not text.strip():
        return
    msgs = entry.setdefault("messages", [])

    # Find the last user message index
    last_user_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i]["role"] == "user":
            last_user_idx = i
            break

    # Find the last assistant message after the last user message
    last_assistant_idx = -1
    for i in range(len(msgs) - 1, -1, -1):
        if msgs[i]["role"] == "assistant" and i > last_user_idx:
            last_assistant_idx = i
            break

    if last_assistant_idx >= 0:
        # Update existing assistant message if content changed
        if msgs[last_assistant_idx]["text"] == text:
            return  # No change
        if _msg_similarity(msgs[last_assistant_idx]["text"], text) > 0.9:
            return  # Too similar, skip
        # Update the message with new content
        msgs[last_assistant_idx]["text"] = text
        msgs[last_assistant_idx]["ts"] = ts
    else:
        # No assistant message after last user message — create one
        msgs.append({"role": "assistant", "text": text, "ts": ts})

    _save_messages()


def build_session_response(sess: dict, data: dict, activity: dict = None) -> dict:
    if activity is None:
        activity = detect_activity(sess["name"])
    return {
        "name": sess["name"],
        "windows": sess["windows"],
        "attached": sess["attached"],
        "title": data.get("title", ""),
        "description": data.get("description", ""),
        "description_at": data.get("description_at", 0),
        "progress": data.get("progress", ""),
        "progress_at": data.get("progress_at", 0),
        "notes": data.get("notes", ""),
        "notes_at": data.get("notes_at", 0),
        "realtime": data.get("realtime", ""),
        "realtime_at": data.get("realtime_at", 0),
        "messages": data.get("messages", []),
        "activity_status": activity["status"],
        "activity_command": activity["command"],
        "activity_detail": activity["detail"],
        "auth_mode": _session_auth_mode.get(sess["name"], "subscription"),
        "away_mode": _away_mode_state.get(sess["name"], {}).get("enabled", False),
        "go_nuts_mode": _go_nuts_state.get(sess["name"], {}).get("enabled", False),
        "model": _get_session_model(sess["name"]),
    }


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/sessions")
async def api_sessions():
    sessions = get_tmux_sessions()
    results, activities = await asyncio.gather(
        asyncio.gather(*[get_session_data(s["name"]) for s in sessions]),
        asyncio.gather(*(async_detect_activity(s["name"]) for s in sessions)),
    )
    return JSONResponse([
        build_session_response(sess, data, activity=act)
        for sess, data, act in zip(sessions, results, activities)
    ])


@app.get("/api/sessions-fast")
async def api_sessions_fast():
    """Return session list with cached data only — no LLM calls. Fast startup."""
    sessions = get_tmux_sessions()
    # Run activity detection for all sessions in parallel threads
    activities = await asyncio.gather(
        *(async_detect_activity(sess["name"]) for sess in sessions)
    )
    out = []
    for sess, activity in zip(sessions, activities):
        entry = cache.get(sess["name"], {})
        if "messages" not in entry:
            entry["messages"] = _load_session_messages(sess["name"])
        if "notes" not in entry:
            entry["notes"] = _load_session_notes(sess["name"])
        cache[sess["name"]] = entry
        out.append({
            "name": sess["name"],
            "windows": sess["windows"],
            "attached": sess["attached"],
            "title": entry.get("title", ""),
            "description": entry.get("description", ""),
            "description_at": entry.get("description_at", 0),
            "progress": entry.get("progress", ""),
            "progress_at": entry.get("progress_at", 0),
            "notes": entry.get("notes", ""),
            "notes_at": entry.get("notes_at", 0),
            "realtime": entry.get("realtime", ""),
            "realtime_at": entry.get("realtime_at", 0),
            "messages": entry.get("messages", []),
            "activity_status": activity["status"],
            "activity_command": activity.get("command", ""),
            "activity_detail": activity.get("detail", ""),
            "auth_mode": _session_auth_mode.get(sess["name"], "subscription"),
            "away_mode": _away_mode_state.get(sess["name"], {}).get("enabled", False),
            "go_nuts_mode": _go_nuts_state.get(sess["name"], {}).get("enabled", False),
            "model": _get_session_model(sess["name"]),
        })
    return JSONResponse(out)


@app.post("/api/sessions/{session_name}/refresh")
async def api_refresh_session(session_name: str):
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    entry = await get_session_data(session_name)
    activity = await async_detect_activity(session_name)
    return JSONResponse(build_session_response(sess, entry, activity=activity))


@app.post("/api/sessions/{session_name}/refresh-all")
async def api_refresh_all_tiers(session_name: str):
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    entry = await get_session_data(session_name, force_all=True)
    activity = await async_detect_activity(session_name)
    return JSONResponse(build_session_response(sess, entry, activity=activity))


@app.get("/api/status")
async def api_status():
    """Lightweight: return only activity status per session, no LLM calls."""
    sessions = get_tmux_sessions()
    activities = await asyncio.gather(
        *(async_detect_activity(sess["name"]) for sess in sessions)
    )
    out = []
    for sess, activity in zip(sessions, activities):
        out.append({
            "name": sess["name"],
            "activity_status": activity["status"],
            "activity_detail": activity["detail"],
            "away_mode": _away_mode_state.get(sess["name"], {}).get("enabled", False),
            "go_nuts_mode": _go_nuts_state.get(sess["name"], {}).get("enabled", False),
            "model": _get_session_model(sess["name"]),
        })
    return JSONResponse(out)


@app.get("/api/sessions/{session_name}/raw")
async def api_raw_output(session_name: str):
    """Return raw scrollback content for a session."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    raw = await asyncio.to_thread(capture_pane_full, session_name)
    activity = await async_detect_activity(session_name)
    return JSONResponse({
        "name": session_name,
        "raw": raw,
        "lines": len(raw.split("\n")),
        "activity_status": activity["status"],
        "activity_command": activity["command"],
        "activity_detail": activity["detail"],
    })


@app.get("/api/sessions/{session_name}/raw-tail")
async def api_raw_tail(session_name: str, known_lines: int = 0):
    """Return delta output since the client's last known line count."""
    _, found = _find_session(session_name)
    if not found:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    pos = await asyncio.to_thread(get_pane_position, session_name)
    current_total = pos["total_lines"]

    # First load or session reset → full capture
    if known_lines <= 0 or known_lines > current_total:
        raw = await asyncio.to_thread(capture_pane_full, session_name)
        return JSONResponse({
            "mode": "full",
            "raw": raw,
            "total_lines": len(raw.split("\n")),
            "pane_total": current_total,
        })

    # No new content
    if current_total <= known_lines:
        return JSONResponse({
            "mode": "none",
            "total_lines": known_lines,
            "pane_total": current_total,
        })

    # Delta: capture only the new lines + small overlap for dedup
    overlap = 5
    lines_from_end = (current_total - known_lines) + overlap
    raw = await asyncio.to_thread(capture_pane_recent, session_name, lines_from_end)
    return JSONResponse({
        "mode": "delta",
        "raw": raw,
        "total_lines": current_total,
        "pane_total": current_total,
        "overlap": overlap,
    })


class CreateSession(BaseModel):
    name: str = ""


@app.post("/api/sessions/create")
async def api_create_session(body: CreateSession):
    """Create a new tmux session."""
    name = body.name.strip()
    if name:
        # Validate name: alphanumeric, dash, underscore only
        if not re.match(r'^[a-zA-Z0-9_-]+$', name):
            return JSONResponse({"error": "Invalid name. Use letters, numbers, dash, underscore."}, status_code=400)
        existing = [s["name"] for s in get_tmux_sessions()]
        if name in existing:
            return JSONResponse({"error": f"Session '{name}' already exists."}, status_code=409)
    try:
        cmd = ["tmux", "new-session", "-d"]
        if name:
            cmd += ["-s", name]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr.strip() or "Failed to create session"}, status_code=500)
        # Find the new session name (if auto-named)
        sessions = get_tmux_sessions()
        if name:
            created = name
        else:
            created = sessions[-1]["name"] if sessions else "unknown"
        # Inject stored API key so Claude Code can authenticate
        if _stored_anthropic_key:
            subprocess.run(
                ["tmux", "send-keys", "-t", created, "-l",
                 f"export ANTHROPIC_API_KEY={_stored_anthropic_key}"],
                capture_output=True, text=True, timeout=5
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", created, "Enter"],
                capture_output=True, text=True, timeout=5
            )
            _session_auth_mode[created] = "api"
        else:
            _session_auth_mode[created] = "subscription"
        # Optionally launch a command in the new session
        if NEW_SESSION_CMD:
            subprocess.run(
                ["tmux", "send-keys", "-t", created, "-l", NEW_SESSION_CMD],
                capture_output=True, text=True, timeout=5
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", created, "Enter"],
                capture_output=True, text=True, timeout=5
            )
        logger.info("Session created: '%s' (auth_mode=%s)", created, _session_auth_mode.get(created, "unknown"))
        return JSONResponse({"ok": True, "name": created})
    except Exception as e:
        logger.error("Failed to create session '%s': %s", name, e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/sessions/{session_name}")
async def api_delete_session(session_name: str):
    """Kill a tmux session and all its child processes."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        # First, find and kill all processes in the session's panes.
        # This ensures Claude Code (node) processes are terminated cleanly
        # before the tmux session is destroyed.
        try:
            # Get all pane PIDs in this session
            pane_result = subprocess.run(
                ["tmux", "list-panes", "-t", session_name, "-F", "#{pane_pid}"],
                capture_output=True, text=True, timeout=5
            )
            if pane_result.returncode == 0:
                for pid_str in pane_result.stdout.strip().split("\n"):
                    pid_str = pid_str.strip()
                    if not pid_str:
                        continue
                    # Kill the entire process group rooted at this pane's shell
                    # This catches Claude Code (node), any background tasks, etc.
                    try:
                        subprocess.run(
                            ["pkill", "-TERM", "-P", pid_str],
                            capture_output=True, text=True, timeout=3
                        )
                    except Exception:
                        logger.debug("pkill -TERM failed for pid %s", pid_str, exc_info=True)
                # Brief pause to let processes handle SIGTERM
                await asyncio.sleep(0.5)
                # Force-kill any remaining children
                for pid_str in pane_result.stdout.strip().split("\n"):
                    pid_str = pid_str.strip()
                    if not pid_str:
                        continue
                    try:
                        subprocess.run(
                            ["pkill", "-KILL", "-P", pid_str],
                            capture_output=True, text=True, timeout=3
                        )
                    except Exception:
                        logger.debug("pkill -KILL failed for pid %s", pid_str, exc_info=True)
        except Exception:
            logger.debug("Process cleanup failed for session '%s' — kill-session will still clean up", session_name, exc_info=True)

        result = subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr.strip() or "Failed to kill session"}, status_code=500)
        # Clean up all per-session state from global dicts
        cache.pop(session_name, None)
        _auto_approve_sent.pop(session_name, None)
        _pane_stability.pop(session_name, None)
        _activity_state.pop(session_name, None)
        _session_stats_cache.pop(session_name, None)
        _auto_respond_cooldown.pop(session_name, None)
        _session_auth_mode.pop(session_name, None)
        _away_mode_state.pop(session_name, None)
        # Cancel go-nuts worker if running
        gn_state = _go_nuts_state.get(session_name, {})
        if gn_state.get("task") and not gn_state["task"].done():
            gn_state["task"].cancel()
        _go_nuts_state.pop(session_name, None)
        logger.info("Session deleted: '%s'", session_name)
        return JSONResponse({"ok": True, "killed": session_name})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def get_session_cwd(session_name: str) -> str:
    """Get the current working directory of a tmux session's active pane."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p", "#{pane_current_path}"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        logger.debug("Failed to get CWD for session '%s'", session_name, exc_info=True)
    return ""


UPLOADS_DIR = MESSAGES_DIR / "uploads"


@app.post("/api/sessions/{session_name}/upload")
async def api_upload_file(session_name: str, file: UploadFile = File(...)):
    """Upload a file to a session-specific uploads dir and register in CLAUDE.md."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    cwd = get_session_cwd(session_name)

    # Sanitize filename — keep only the basename
    filename = os.path.basename(file.filename or "upload")
    if not filename or filename.startswith("."):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    # Save to fixed session-specific uploads dir under ~/.tmux-dashboard/uploads/<session>/
    uploads_dir = UPLOADS_DIR / session_name
    uploads_dir.mkdir(parents=True, exist_ok=True)
    dest = str(uploads_dir / filename)
    try:
        content = await file.read()
        max_size = 50 * 1024 * 1024  # 50 MB
        if len(content) > max_size:
            return JSONResponse({"error": f"File too large ({len(content) / 1024 / 1024:.1f} MB). Max is 50 MB."}, status_code=413)
        with open(dest, "wb") as f:
            f.write(content)
        # Append file path to session's CLAUDE.md (if we can determine cwd)
        if cwd:
            _append_upload_to_claude_md(cwd, dest, filename)
        # Record in chat history
        size_kb = len(content) / 1024
        note = f"Uploaded {filename} ({size_kb:.1f} KB) to {dest}"
        now = time.time()
        entry = cache.setdefault(session_name, {})
        if "messages" not in entry:
            entry["messages"] = _load_session_messages(session_name)
        entry["messages"].append({"role": "user", "text": note, "ts": now})
        _save_messages()
        return JSONResponse({"ok": True, "path": dest, "size": len(content)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


def _append_upload_to_claude_md(cwd: str, file_path: str, filename: str):
    """Append an uploaded file's path to the project CLAUDE.md so Claude Code can find it."""
    md_path = os.path.join(cwd, "CLAUDE.md")
    marker = "# Uploaded Files"
    entry_line = f"- `{file_path}`"
    try:
        existing = ""
        if os.path.exists(md_path):
            with open(md_path) as f:
                existing = f.read()
        # Don't duplicate if already listed
        if file_path in existing:
            return
        if marker in existing:
            # Append under existing section
            parts = existing.split(marker, 1)
            updated = parts[0] + marker + parts[1].rstrip("\n") + "\n" + entry_line + "\n"
        else:
            # Add new section at the end
            updated = existing.rstrip("\n") + "\n\n" + marker + "\n" + entry_line + "\n"
        with open(md_path, "w") as f:
            f.write(updated)
    except Exception:
        logger.debug("Failed to update CLAUDE.md with upload path %s", file_path, exc_info=True)


# --- CLAUDE.md viewer/editor ---

@app.get("/api/sessions/{session_name}/claude-md")
async def api_get_claude_md(session_name: str):
    """Read CLAUDE.md from the session's working directory and home dir."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    cwd = get_session_cwd(session_name)
    results = []
    # Check session CWD
    if cwd:
        md_path = os.path.join(cwd, "CLAUDE.md")
        content = ""
        if os.path.exists(md_path):
            try:
                with open(md_path) as f:
                    content = f.read()
            except Exception:
                logger.debug("Failed to read CLAUDE.md at %s", md_path, exc_info=True)
        results.append({"path": md_path, "content": content, "exists": os.path.exists(md_path), "label": "Project"})
    # Check home dir
    home_md = os.path.join(str(Path.home()), "CLAUDE.md")
    home_content = ""
    if os.path.exists(home_md):
        try:
            with open(home_md) as f:
                home_content = f.read()
        except Exception:
            logger.debug("Failed to read global CLAUDE.md at %s", home_md, exc_info=True)
    results.append({"path": home_md, "content": home_content, "exists": os.path.exists(home_md), "label": "Global"})
    return JSONResponse({"files": results, "cwd": cwd or ""})


class SaveClaudeMd(BaseModel):
    path: str
    content: str


@app.post("/api/sessions/{session_name}/claude-md")
async def api_save_claude_md(session_name: str, body: SaveClaudeMd):
    """Save CLAUDE.md to the specified path."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    # Safety: only allow writing CLAUDE.md files within home directory
    if not body.path.endswith("CLAUDE.md"):
        return JSONResponse({"error": "Can only write CLAUDE.md files"}, status_code=400)
    real_path = os.path.realpath(body.path)
    home_dir = str(Path.home())
    if not real_path.startswith(home_dir + "/") and real_path != home_dir:
        return JSONResponse({"error": "Path must be within home directory"}, status_code=403)
    if not real_path.endswith("/CLAUDE.md"):
        return JSONResponse({"error": "Invalid path after resolution"}, status_code=400)
    try:
        os.makedirs(os.path.dirname(real_path), exist_ok=True)
        with open(real_path, "w") as f:
            f.write(body.content)
        return JSONResponse({"ok": True, "path": real_path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Global CLAUDE.md (header button — no session needed) ---

@app.get("/api/claude-md-global")
async def api_get_claude_md_global():
    """Read global CLAUDE.md from home dir."""
    home_md = os.path.join(str(Path.home()), "CLAUDE.md")
    content = ""
    exists = os.path.exists(home_md)
    if exists:
        try:
            with open(home_md) as f:
                content = f.read()
        except Exception:
            logger.debug("Failed to read global CLAUDE.md", exc_info=True)
    return JSONResponse({"path": home_md, "content": content, "exists": exists})


@app.post("/api/claude-md-global")
async def api_save_claude_md_global(body: SaveClaudeMd):
    """Save global CLAUDE.md."""
    home_md = os.path.join(str(Path.home()), "CLAUDE.md")
    real_path = os.path.realpath(body.path)
    if real_path != os.path.realpath(home_md):
        return JSONResponse({"error": "Can only save to global CLAUDE.md"}, status_code=400)
    try:
        with open(real_path, "w") as f:
            f.write(body.content)
        return JSONResponse({"ok": True, "path": real_path})
    except Exception:
        logger.exception("Failed to save global CLAUDE.md")
        return JSONResponse({"error": "Failed to save"}, status_code=500)


# --- Skills file management ---

SKILLS_DIR = MESSAGES_DIR / "skills"
SKILL_LIBRARY_DIR = MESSAGES_DIR / "skill-library"

_SKILL_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_-]+\.md$")


def _sanitize_skill_filename(name: str) -> str:
    """Sanitize and validate a skill filename."""
    name = os.path.basename(name)
    if not name.endswith(".md"):
        name += ".md"
    if not _SKILL_FILENAME_RE.match(name):
        return ""
    return name


def _skill_dir_for_session(session_name: str) -> Path:
    d = SKILLS_DIR / session_name
    d.mkdir(parents=True, exist_ok=True)
    return d


class SkillFileBody(BaseModel):
    name: str
    content: str


class SkillLibraryBody(BaseModel):
    name: str
    session_name: str


@app.get("/api/sessions/{session_name}/skills")
async def api_list_skills(session_name: str):
    """List .md skill files for a session."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    d = _skill_dir_for_session(session_name)
    files = []
    for p in sorted(d.iterdir()):
        if p.suffix == ".md" and p.is_file():
            try:
                content = p.read_text()
                stat = p.stat()
                files.append({
                    "name": p.name,
                    "content": content,
                    "size": stat.st_size,
                    "modified": stat.st_mtime,
                })
            except Exception:
                logger.debug("Failed to read skill file %s", p, exc_info=True)
    return JSONResponse({"files": files, "path": str(d)})


@app.post("/api/sessions/{session_name}/skills")
async def api_save_skill(session_name: str, body: SkillFileBody):
    """Create or update a skill .md file."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    fname = _sanitize_skill_filename(body.name)
    if not fname:
        return JSONResponse({"error": "Invalid filename. Use alphanumeric, hyphens, underscores with .md extension."}, status_code=400)
    d = _skill_dir_for_session(session_name)
    fpath = d / fname
    # Resolve to prevent traversal
    if not str(fpath.resolve()).startswith(str(d.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    try:
        fpath.write_text(body.content)
        return JSONResponse({"ok": True, "name": fname, "path": str(fpath)})
    except Exception:
        logger.exception("Failed to save skill file")
        return JSONResponse({"error": "Failed to save skill file"}, status_code=500)


@app.delete("/api/sessions/{session_name}/skills/{filename}")
async def api_delete_skill(session_name: str, filename: str):
    """Delete a skill .md file."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    fname = _sanitize_skill_filename(filename)
    if not fname:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    d = _skill_dir_for_session(session_name)
    fpath = d / fname
    if not str(fpath.resolve()).startswith(str(d.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not fpath.exists():
        return JSONResponse({"error": "File not found"}, status_code=404)
    try:
        fpath.unlink()
        return JSONResponse({"ok": True})
    except Exception:
        logger.exception("Failed to delete skill file")
        return JSONResponse({"error": "Failed to delete skill file"}, status_code=500)


@app.get("/api/skill-library")
async def api_list_skill_library():
    """List all saved skill sets in the library."""
    SKILL_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    sets = []
    for d in sorted(SKILL_LIBRARY_DIR.iterdir()):
        if d.is_dir():
            files = [f.name for f in sorted(d.iterdir()) if f.suffix == ".md" and f.is_file()]
            try:
                created = d.stat().st_ctime
            except Exception:
                created = 0
            sets.append({"name": d.name, "files": files, "created": created})
    return JSONResponse({"sets": sets})


@app.post("/api/skill-library/save")
async def api_save_to_library(body: SkillLibraryBody):
    """Copy a session's skill files to the library under a given set name."""
    _, sess = _find_session(body.session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    set_name = re.sub(r"[^a-zA-Z0-9_-]", "", body.name)
    if not set_name:
        return JSONResponse({"error": "Invalid set name"}, status_code=400)
    src = _skill_dir_for_session(body.session_name)
    dest = SKILL_LIBRARY_DIR / set_name
    dest.mkdir(parents=True, exist_ok=True)
    copied = []
    for p in src.iterdir():
        if p.suffix == ".md" and p.is_file():
            shutil.copy2(str(p), str(dest / p.name))
            copied.append(p.name)
    return JSONResponse({"ok": True, "name": set_name, "files": copied})


@app.post("/api/skill-library/load")
async def api_load_from_library(body: SkillLibraryBody):
    """Load a skill set from the library into a session's skills dir (merge/overwrite)."""
    _, sess = _find_session(body.session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    set_name = re.sub(r"[^a-zA-Z0-9_-]", "", body.name)
    if not set_name:
        return JSONResponse({"error": "Invalid set name"}, status_code=400)
    src = SKILL_LIBRARY_DIR / set_name
    if not src.is_dir():
        return JSONResponse({"error": "Library set not found"}, status_code=404)
    dest = _skill_dir_for_session(body.session_name)
    loaded = []
    for p in src.iterdir():
        if p.suffix == ".md" and p.is_file():
            shutil.copy2(str(p), str(dest / p.name))
            loaded.append(p.name)
    return JSONResponse({"ok": True, "name": set_name, "files": loaded})


# --- System stats ---

@app.get("/api/stats")
async def api_stats():
    """System stats: CPU, disk, memory, tmux sessions, Claude processes."""
    stats = {}
    # CPU load
    try:
        with open('/proc/loadavg') as f:
            parts = f.read().split()
            stats["cpu_load"] = {"1m": parts[0], "5m": parts[1], "15m": parts[2]}
            # Running threads from field 4 (e.g. "2/150")
            if len(parts) > 3 and '/' in parts[3]:
                running, total = parts[3].split('/')
                stats["threads_running"] = int(running)
                stats["threads_total"] = int(total)
    except Exception:
        stats["cpu_load"] = {}
    # CPU count and approximate usage percent
    try:
        cpu_count = os.cpu_count() or 1
        stats["cpu_count"] = cpu_count
        load_1m = float(stats.get("cpu_load", {}).get("1m", 0))
        stats["cpu_percent"] = min(round(load_1m / cpu_count * 100, 1), 100.0)
    except Exception:
        stats["cpu_count"] = 1
        stats["cpu_percent"] = 0
    # Memory
    try:
        result = subprocess.run(["free", "-m"], capture_output=True, text=True, timeout=5)
        lines = result.stdout.strip().split("\n")
        if len(lines) >= 2:
            parts = lines[1].split()
            stats["memory"] = {
                "total_mb": int(parts[1]),
                "used_mb": int(parts[2]),
                "available_mb": int(parts[6]) if len(parts) > 6 else int(parts[3]),
            }
    except Exception:
        stats["memory"] = {}
    # Disk
    try:
        usage = shutil.disk_usage("/")
        stats["disk"] = {
            "total_gb": round(usage.total / (1024**3), 1),
            "used_gb": round(usage.used / (1024**3), 1),
            "free_gb": round(usage.free / (1024**3), 1),
            "pct": round(usage.used / usage.total * 100, 1),
        }
    except Exception:
        stats["disk"] = {}
    # tmux sessions
    stats["tmux_sessions"] = get_tmux_sessions()
    # Claude processes
    try:
        result = subprocess.run(
            ["pgrep", "-a", "claude"],
            capture_output=True, text=True, timeout=5
        )
        stats["claude_processes"] = [
            l.strip() for l in result.stdout.strip().split("\n") if l.strip()
        ]
    except Exception:
        stats["claude_processes"] = []
    # Node processes (Claude Code runs as node)
    try:
        result = subprocess.run(
            ["pgrep", "-a", "-f", "claude"],
            capture_output=True, text=True, timeout=5
        )
        stats["claude_related"] = len([
            l for l in result.stdout.strip().split("\n") if l.strip()
        ])
    except Exception:
        stats["claude_related"] = 0
    # Uptime
    try:
        with open('/proc/uptime') as f:
            uptime_secs = float(f.read().split()[0])
            days = int(uptime_secs // 86400)
            hours = int((uptime_secs % 86400) // 3600)
            stats["uptime"] = f"{days}d {hours}h"
    except Exception:
        stats["uptime"] = "unknown"
    return JSONResponse(stats)


@app.get("/api/health")
async def api_health():
    """Lightweight health check — verifies tmux is accessible."""
    checks = {"status": "ok", "tmux": False, "openai": bool(OPENAI_API_KEY)}
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True, text=True, timeout=3
        )
        checks["tmux"] = result.returncode == 0 or "no server running" in result.stderr
    except Exception:
        checks["tmux"] = False
    if not checks["tmux"]:
        checks["status"] = "degraded"
    return JSONResponse(checks)


# --- Claude Code auth management ---

_claude_auth_cache: dict = {"ts": 0, "data": {}}

@app.get("/api/auth/claude-status")
async def api_claude_auth_status():
    now = time.time()
    if now - _claude_auth_cache["ts"] < 60 and _claude_auth_cache["data"]:
        cached = dict(_claude_auth_cache["data"])
        cached["hasApiKey"] = bool(_stored_anthropic_key)
        return JSONResponse(cached)

    result_data: dict = {"loggedIn": False, "hasApiKey": bool(_stored_anthropic_key)}

    # Try reading credentials file directly (instant vs ~25s subprocess)
    creds_file = Path.home() / ".claude" / ".credentials.json"
    try:
        creds = json.loads(creds_file.read_text())
        oauth = creds.get("claudeAiOauth", {})
        if oauth and oauth.get("expiresAt", 0) > now * 1000:  # expiresAt is millis
            result_data["loggedIn"] = True
            result_data["subscriptionType"] = oauth.get("subscriptionType", "")
            result_data["email"] = "Claude Code"
            _claude_auth_cache["ts"] = now
            _claude_auth_cache["data"] = result_data
            return JSONResponse(result_data)
    except Exception:
        logger.debug("Could not read credentials file, falling back to subprocess")

    # Fallback: subprocess with generous timeout
    try:
        result = await asyncio.to_thread(subprocess.run,
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            auth_info = json.loads(result.stdout.strip())
            auth_info["hasApiKey"] = bool(_stored_anthropic_key)
            _claude_auth_cache["ts"] = now
            _claude_auth_cache["data"] = auth_info
            return JSONResponse(auth_info)
    except Exception:
        logger.debug("Claude auth subprocess fallback also failed", exc_info=True)

    return JSONResponse(result_data)


class SetApiKey(BaseModel):
    apiKey: str


@app.post("/api/auth/api-key")
async def api_set_claude_key(body: SetApiKey):
    key = body.apiKey.strip()
    if key:
        if not key.startswith(("sk-ant-", "sk-")):
            return JSONResponse(
                {"error": "Invalid API key format. Expected key starting with sk-ant- or sk-."},
                status_code=400,
            )
        _save_anthropic_key(key)
        return JSONResponse({"ok": True, "message": "API key stored."})
    else:
        _clear_anthropic_key()
        return JSONResponse({"ok": True, "message": "API key cleared."})


@app.post("/api/auth/logout")
async def api_claude_auth_logout():
    errors = []
    try:
        result = subprocess.run(
            ["claude", "auth", "logout"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            errors.append(result.stderr.strip() or "OAuth logout failed")
    except Exception as e:
        errors.append(str(e))
    _clear_anthropic_key()
    if errors:
        return JSONResponse({"ok": True, "warnings": errors})
    return JSONResponse({"ok": True})


_usage_cache: dict = {"ts": 0, "data": {}}

@app.get("/api/auth/usage")
async def api_claude_usage():
    """Token usage for today, parsed from Claude Code session JSONL files."""
    now = time.time()
    if now - _usage_cache["ts"] < 60:
        return JSONResponse(_usage_cache["data"])

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    home = str(Path.home())
    patterns = [
        f"{home}/.claude/projects/*/*.jsonl",
        f"{home}/.claude/projects/*/subagents/*.jsonl",
        f"{home}/.claude/projects/*/*/subagents/*.jsonl",
    ]
    files: set = set()
    for p in patterns:
        files.update(globmod.glob(p))

    input_tok = 0
    output_tok = 0
    cache_read = 0
    cache_create = 0
    msg_count = 0

    for fpath in files:
        try:
            mtime = os.path.getmtime(fpath)
            if datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d") < today:
                continue
            with open(fpath) as f:
                for line in f:
                    d = json.loads(line)
                    if d.get("type") != "assistant":
                        continue
                    ts = d.get("timestamp", "")
                    if not ts.startswith(today):
                        continue
                    msg = d if "usage" in d else d.get("message", {})
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    input_tok += usage.get("input_tokens", 0)
                    output_tok += usage.get("output_tokens", 0)
                    cache_read += usage.get("cache_read_input_tokens", 0)
                    cache_create += usage.get("cache_creation_input_tokens", 0)
                    msg_count += 1
        except Exception:
            logger.debug("Failed to parse usage JSONL for '%s'", fpath, exc_info=True)

    data = {
        "date": today,
        "messages": msg_count,
        "inputTokens": input_tok,
        "outputTokens": output_tok,
        "cacheReadTokens": cache_read,
        "cacheCreateTokens": cache_create,
        "totalTokens": input_tok + output_tok + cache_read + cache_create,
    }
    _usage_cache["ts"] = now
    _usage_cache["data"] = data
    return JSONResponse(data)


_stats_usage_cache: dict = {"ts": 0, "data": {}}

def _estimate_cost(inp: int, out: int, cr: int, cc: int, model: str) -> float:
    """Estimate cost in USD from token counts and model name."""
    if "opus" in model:
        ci, co, ccr, ccc = 15.0, 75.0, 1.5, 18.75
    elif "haiku" in model:
        ci, co, ccr, ccc = 1.0, 5.0, 0.1, 1.25
    else:  # sonnet or unknown
        ci, co, ccr, ccc = 3.0, 15.0, 0.3, 3.75
    return inp * ci / 1e6 + out * co / 1e6 + cr * ccr / 1e6 + cc * ccc / 1e6


@app.get("/api/stats/usage")
async def api_stats_usage():
    """Aggregated token usage across all sessions: 5h window + this week."""
    now = time.time()
    if now - _stats_usage_cache["ts"] < 120 and _stats_usage_cache["data"]:
        return JSONResponse(_stats_usage_cache["data"])

    now_dt = datetime.now(timezone.utc)
    cutoff_5h = (now_dt - timedelta(hours=5)).isoformat()
    # Monday 00:00 UTC of current week
    week_start = (now_dt - timedelta(days=now_dt.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    ).isoformat()

    home = str(Path.home())
    patterns = [
        f"{home}/.claude/projects/*/*.jsonl",
        f"{home}/.claude/projects/*/subagents/*.jsonl",
        f"{home}/.claude/projects/*/*/subagents/*.jsonl",
    ]
    all_files: set = set()
    for p in patterns:
        all_files.update(globmod.glob(p))

    # Build mapping: project_dir -> list of (timestamp, inp, out, cr, cc, model)
    dir_entries: dict = {}  # project_dir -> entries list
    week_start_date = week_start[:10]

    for fpath in all_files:
        try:
            mtime = os.path.getmtime(fpath)
            if datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d") < week_start_date:
                continue
            # Derive project dir (strip subagents/ if present)
            pdir = os.path.dirname(fpath)
            if os.path.basename(pdir) == "subagents":
                pdir = os.path.dirname(pdir)
            if pdir not in dir_entries:
                dir_entries[pdir] = []
            with open(fpath) as f:
                for line in f:
                    d = json.loads(line)
                    if d.get("type") != "assistant":
                        continue
                    ts = d.get("timestamp", "")
                    if ts < week_start:
                        continue
                    msg = d if "usage" in d else d.get("message", {})
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)
                    cc = usage.get("cache_creation_input_tokens", 0)
                    model = msg.get("model", d.get("message", {}).get("model", "unknown"))
                    dir_entries[pdir].append((ts, inp, out, cr, cc, model))
        except Exception:
            logger.debug("Failed to parse stats usage JSONL '%s'", fpath, exc_info=True)

    # Map tmux sessions to project dirs
    tmux_sessions = get_tmux_sessions()
    session_dirs: dict = {}  # project_dir -> session_name
    for s in tmux_sessions:
        cwd = get_session_cwd(s["name"])
        if not cwd:
            continue
        sanitized = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
        projects_base = str(Path.home() / ".claude" / "projects")
        for candidate in [
            os.path.join(projects_base, sanitized),
            os.path.join(projects_base, "-" + sanitized.lstrip("-")),
        ]:
            if candidate in dir_entries:
                session_dirs[candidate] = s["name"]
                break
        else:
            last_part = cwd.rstrip("/").rsplit("/", 1)[-1]
            for pdir in dir_entries:
                if last_part in os.path.basename(pdir):
                    session_dirs[pdir] = s["name"]
                    break

    # Aggregate per project dir
    g5h = {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheCreateTokens": 0, "totalTokens": 0, "messages": 0, "estimatedCost": 0.0}
    gweek = {"inputTokens": 0, "outputTokens": 0, "cacheReadTokens": 0, "cacheCreateTokens": 0, "totalTokens": 0, "messages": 0, "estimatedCost": 0.0}
    session_list = []

    for pdir, entries in dir_entries.items():
        if not entries:
            continue
        sname = session_dirs.get(pdir, os.path.basename(pdir).split("-")[-1] or os.path.basename(pdir))
        s5h = {"totalTokens": 0, "messages": 0, "estimatedCost": 0.0}
        sweek = {"totalTokens": 0, "messages": 0, "estimatedCost": 0.0}
        latest_ts = ""
        latest_model = "unknown"

        for ts, inp, out, cr, cc, model in entries:
            total = inp + out + cr + cc
            # This week
            sweek["totalTokens"] += total
            sweek["messages"] += 1
            sweek["estimatedCost"] += _estimate_cost(inp, out, cr, cc, model)
            gweek["inputTokens"] += inp
            gweek["outputTokens"] += out
            gweek["cacheReadTokens"] += cr
            gweek["cacheCreateTokens"] += cc
            gweek["totalTokens"] += total
            gweek["messages"] += 1
            gweek["estimatedCost"] += _estimate_cost(inp, out, cr, cc, model)
            if ts > latest_ts:
                latest_ts = ts
                latest_model = model
            # 5h window
            if ts >= cutoff_5h:
                s5h["totalTokens"] += total
                s5h["messages"] += 1
                s5h["estimatedCost"] += _estimate_cost(inp, out, cr, cc, model)
                g5h["inputTokens"] += inp
                g5h["outputTokens"] += out
                g5h["cacheReadTokens"] += cr
                g5h["cacheCreateTokens"] += cc
                g5h["totalTokens"] += total
                g5h["messages"] += 1
                g5h["estimatedCost"] += _estimate_cost(inp, out, cr, cc, model)

        # Round costs
        s5h["estimatedCost"] = round(s5h["estimatedCost"], 2)
        sweek["estimatedCost"] = round(sweek["estimatedCost"], 2)

        session_list.append({
            "name": sname,
            "model": latest_model,
            "window5h": s5h,
            "thisWeek": sweek,
            "lastActive": latest_ts,
        })

    # Sort by most recently active
    session_list.sort(key=lambda x: x["lastActive"], reverse=True)
    g5h["estimatedCost"] = round(g5h["estimatedCost"], 2)
    gweek["estimatedCost"] = round(gweek["estimatedCost"], 2)

    data = {
        "window5h": g5h,
        "thisWeek": gweek,
        "sessions": session_list,
    }
    _stats_usage_cache["ts"] = now
    _stats_usage_cache["data"] = data
    return JSONResponse(data)


# --- Per-session token stats & rate tracking ---

_session_stats_cache: Dict[str, dict] = {}
_session_model_cache: Dict[str, dict] = {}  # {session_name: {"model": str, "ts": float}}


def _get_session_model(session_name: str) -> str:
    """Detect the current model for a session by reading the latest JSONL entries."""
    now = time.time()
    cached = _session_model_cache.get(session_name)
    if cached and now - cached.get("ts", 0) < 30:
        return cached.get("model", "")
    files = _find_session_jsonl_files(session_name)
    if not files:
        _session_model_cache[session_name] = {"model": "", "ts": now}
        return ""
    # Find newest file by mtime
    newest = max(files, key=lambda f: os.path.getmtime(f), default=None)
    if not newest:
        _session_model_cache[session_name] = {"model": "", "ts": now}
        return ""
    model = ""
    try:
        with open(newest, "rb") as f:
            # Read last ~32KB to find recent model entries
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 32768))
            tail = f.read().decode("utf-8", errors="replace")
        for line in reversed(tail.strip().split("\n")):
            try:
                d = json.loads(line)
                if d.get("type") == "assistant":
                    msg = d if "model" in d else d.get("message", {})
                    m = msg.get("model", "")
                    if m:
                        model = m
                        break
            except (json.JSONDecodeError, KeyError):
                continue
    except Exception:
        logger.debug("Failed to detect model for '%s'", session_name, exc_info=True)
    _session_model_cache[session_name] = {"model": model, "ts": now}
    return model


def _find_session_jsonl_files(session_name: str) -> list:
    """Find Claude Code JSONL files for a tmux session based on its working directory."""
    cwd = get_session_cwd(session_name)
    if not cwd:
        return []
    # Claude Code sanitizes paths: replaces all non-alphanumeric chars with hyphens
    sanitized = re.sub(r"[^a-zA-Z0-9]", "-", cwd)
    projects_base = Path.home() / ".claude" / "projects"
    # Try exact match first, then fallback to glob match
    project_dir = str(projects_base / sanitized)
    if not os.path.isdir(project_dir):
        # Try with leading dash (common pattern)
        alt = str(projects_base / ("-" + sanitized.lstrip("-")))
        if os.path.isdir(alt):
            project_dir = alt
        else:
            # Glob fallback: match any dir containing the last path component
            last_part = cwd.rstrip("/").rsplit("/", 1)[-1]
            candidates = globmod.glob(str(projects_base / f"*{last_part}*"))
            if candidates:
                project_dir = candidates[0]
            else:
                return []
    files = globmod.glob(os.path.join(project_dir, "*.jsonl"))
    files += globmod.glob(os.path.join(project_dir, "subagents", "*.jsonl"))
    return files


def _parse_session_stats(session_name: str) -> dict:
    """Parse JSONL files and compute per-session token stats with rate tracking."""
    now = time.time()
    cached = _session_stats_cache.get(session_name)
    if cached and now - cached.get("_ts", 0) < 15:
        return cached

    files = _find_session_jsonl_files(session_name)
    if not files:
        result = {"available": False, "_ts": now}
        _session_stats_cache[session_name] = result
        return result

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    now_epoch = now

    # Collect all assistant messages with usage from today
    entries = []  # (epoch_seconds, input_tok, output_tok, cache_read, cache_create, model)
    total_input = 0
    total_output = 0
    total_cache_read = 0
    total_cache_create = 0
    msg_count = 0
    models_seen = {}
    latest_model = "unknown"  # Track the most recently used model
    latest_model_ts = ""

    for fpath in files:
        try:
            mtime = os.path.getmtime(fpath)
            if datetime.fromtimestamp(mtime, timezone.utc).strftime("%Y-%m-%d") < today:
                continue
            with open(fpath) as f:
                for line in f:
                    d = json.loads(line)
                    if d.get("type") != "assistant":
                        continue
                    ts_str = d.get("timestamp", "")
                    if not ts_str.startswith(today):
                        continue
                    msg = d if "usage" in d else d.get("message", {})
                    usage = msg.get("usage")
                    if not usage:
                        continue
                    inp = usage.get("input_tokens", 0)
                    out = usage.get("output_tokens", 0)
                    cr = usage.get("cache_read_input_tokens", 0)
                    cc = usage.get("cache_creation_input_tokens", 0)
                    model = msg.get("model", d.get("message", {}).get("model", "unknown"))

                    total_input += inp
                    total_output += out
                    total_cache_read += cr
                    total_cache_create += cc
                    msg_count += 1
                    models_seen[model] = models_seen.get(model, 0) + 1
                    if ts_str >= latest_model_ts:
                        latest_model_ts = ts_str
                        latest_model = model

                    # Parse timestamp to epoch for rate calc
                    try:
                        dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        epoch = dt.timestamp()
                        entries.append((epoch, inp, out, cr, cc))
                    except Exception:
                        logger.debug("Failed to parse timestamp in stats JSONL entry", exc_info=True)
        except Exception:
            logger.debug("Failed to read stats JSONL for '%s'", session_name, exc_info=True)

    if not entries:
        result = {"available": False, "_ts": now}
        _session_stats_cache[session_name] = result
        return result

    # Sort by timestamp
    entries.sort(key=lambda e: e[0])

    # Cost estimation (per 1M tokens)
    # Use the most recently used model (not the most frequent — user may have switched mid-session)
    primary_model = latest_model if latest_model != "unknown" else (max(models_seen, key=models_seen.get) if models_seen else "unknown")
    if "opus" in primary_model:
        cost_input, cost_output = 15.0, 75.0
        cost_cache_read, cost_cache_create = 1.5, 18.75
    elif "haiku" in primary_model:
        cost_input, cost_output = 1.0, 5.0
        cost_cache_read, cost_cache_create = 0.1, 1.25
    else:  # sonnet or unknown
        cost_input, cost_output = 3.0, 15.0
        cost_cache_read, cost_cache_create = 0.3, 3.75

    estimated_cost = (
        total_input * cost_input / 1_000_000
        + total_output * cost_output / 1_000_000
        + total_cache_read * cost_cache_read / 1_000_000
        + total_cache_create * cost_cache_create / 1_000_000
    )

    # Rate calculation: bucket into 1-minute windows
    # Only consider windows with meaningful output (> 10 output tokens = actually streaming)
    buckets = {}  # minute_epoch -> {input, output, total}
    for epoch, inp, out, cr, cc in entries:
        minute = int(epoch // 60) * 60
        b = buckets.setdefault(minute, {"input": 0, "output": 0, "total": 0})
        b["input"] += inp
        b["output"] += out
        b["total"] += inp + out + cr + cc

    # Active minutes: only windows with meaningful output (streaming, not just tool calls)
    active_minutes = [m for m, b in buckets.items() if b["output"] > 10]
    active_minutes.sort()

    # Peak rate: median of top 5 windows (avoid outlier spikes)
    output_rates = sorted([b["output"] for b in buckets.values() if b["output"] > 10], reverse=True)
    peak_output_rate = 0
    if output_rates:
        top = output_rates[:5]
        peak_output_rate = top[len(top) // 2]  # median of top 5

    # Recent rate: last 3 active minutes within the past 10 minutes
    recent_output_rate = 0
    cutoff = now_epoch - 600  # 10 minutes ago
    recent_active = [m for m in active_minutes if m >= cutoff]
    if recent_active:
        recent_mins = recent_active[-3:]
        recent_output_rate = int(sum(buckets[m]["output"] for m in recent_mins) / len(recent_mins))

    # Rate limit detection: only meaningful when session is currently busy
    # and has recent activity (within last 5 minutes)
    rate_status = "normal"
    rate_pct = 100
    activity = detect_activity(session_name)
    is_busy = activity["status"] == "busy"
    has_recent = recent_active and (now_epoch - recent_active[-1]) < 300

    if peak_output_rate > 100 and recent_output_rate > 0 and has_recent:
        rate_pct = min(100, int(recent_output_rate / peak_output_rate * 100))
        if is_busy and rate_pct < 30:
            rate_status = "severely_limited"
        elif is_busy and rate_pct < 60:
            rate_status = "limited"
    elif not has_recent:
        rate_pct = 0  # no recent data

    # Time since last activity
    last_active = entries[-1][0] if entries else 0
    secs_since_last = int(now_epoch - last_active) if last_active else -1

    # Session duration (first to last entry)
    session_start = entries[0][0]
    session_duration_min = int((entries[-1][0] - session_start) / 60) if len(entries) > 1 else 0

    result = {
        "available": True,
        "model": primary_model,
        "messageCount": msg_count,
        "totalInput": total_input,
        "totalOutput": total_output,
        "cacheRead": total_cache_read,
        "cacheCreate": total_cache_create,
        "totalTokens": total_input + total_output + total_cache_read + total_cache_create,
        "estimatedCost": round(estimated_cost, 4),
        "peakOutputRate": peak_output_rate,  # tokens/min
        "peakTotalRate": peak_output_rate,
        "recentOutputRate": recent_output_rate,
        "recentTotalRate": recent_output_rate,
        "rateStatus": rate_status,  # normal | limited | severely_limited
        "ratePct": rate_pct,
        "activeMinutes": len(active_minutes),
        "sessionDurationMin": session_duration_min,
        "secsSinceLastActivity": secs_since_last,
        "modelsUsed": models_seen,
        "_ts": now,
    }
    _session_stats_cache[session_name] = result
    return result


@app.get("/api/sessions/{session_name}/stats")
async def api_session_stats(session_name: str):
    """Per-session token usage, cost, and rate limit detection."""
    stats = await asyncio.to_thread(_parse_session_stats, session_name)
    return JSONResponse(stats)


class SendCommand(BaseModel):
    command: str

class SendKeys(BaseModel):
    keys: list  # List of tmux key names, e.g. ["Escape"], ["C-c"], ["q", "Enter"]

class AuthModeBody(BaseModel):
    mode: str  # "api" or "subscription"

class AwayModeBody(BaseModel):
    enabled: bool

class GoNutsModeBody(BaseModel):
    enabled: bool


@app.post("/api/sessions/{session_name}/send")
async def api_send_command(session_name: str, body: SendCommand):
    """Send keystrokes to a tmux session, as if typed at the terminal."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        cmd_text = body.command
        if len(cmd_text) > 200:
            # For long messages, use tmux load-buffer + paste-buffer.
            # Claude Code's bracketed paste mode shows "[Pasted text +N lines]"
            # as a preview and often swallows the Enter that follows, leaving
            # the paste stuck until the user sends a second message. We defeat
            # this by sending the \e[?2004l escape sequence first (disables
            # bracketed paste), then pasting, then waiting long enough for
            # the terminal to render before pressing Enter.
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", session_name, "-H",
                 "1b", "5b", "3f", "32", "30", "30", "34", "6c"],  # \e[?2004l
                capture_output=True, text=True, timeout=5
            )
            await asyncio.sleep(0.15)
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
                tmp.write(cmd_text)
                tmp_path = tmp.name
            try:
                await asyncio.to_thread(subprocess.run,
                    ["tmux", "load-buffer", tmp_path],
                    capture_output=True, text=True, timeout=5
                )
                await asyncio.to_thread(subprocess.run,
                    ["tmux", "paste-buffer", "-t", session_name],
                    capture_output=True, text=True, timeout=5
                )
            finally:
                os.unlink(tmp_path)
            # Wait long enough for Claude Code to render the pasted content
            # before pressing Enter. Scale with length; cap at 5s.
            wait_secs = max(0.8, min(5.0, len(cmd_text) / 1500))
            await asyncio.sleep(wait_secs)
            # Press Enter to submit
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, text=True, timeout=5
            )
            # Belt-and-braces: if a bracketed paste preview is still showing
            # (because the escape sequence arrived too late, or bracketed paste
            # was re-enabled mid-flight), a second Enter usually dismisses the
            # preview and submits. Check the pane, only re-press Enter if we
            # still see paste preview markers.
            await asyncio.sleep(0.4)
            try:
                tail = await asyncio.to_thread(capture_pane_recent, session_name, 6)
                if "Pasted text" in tail or "[Pasted" in tail:
                    await asyncio.to_thread(subprocess.run,
                        ["tmux", "send-keys", "-t", session_name, "Enter"],
                        capture_output=True, text=True, timeout=5
                    )
            except Exception:
                logger.debug("Post-paste verification failed", exc_info=True)
        else:
            # Short messages: send-keys -l is fine
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", session_name, "-l", cmd_text],
                capture_output=True, text=True, timeout=5
            )
            # Press Enter as a separate key event
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, text=True, timeout=5
            )
        # Record user message in chat history
        now = time.time()
        entry = cache.setdefault(session_name, {})
        if "messages" not in entry:
            entry["messages"] = _load_session_messages(session_name)
        entry["messages"].append({
            "role": "user", "text": body.command, "ts": now
        })
        _save_messages()
        return JSONResponse({"ok": True, "sent": body.command})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)



@app.post("/api/sessions/{session_name}/interrupt")
async def api_interrupt_session(session_name: str):
    """Send Escape key to interrupt a running Claude Code session."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "Escape"],
            capture_output=True, text=True, timeout=5
        )
        return JSONResponse({"ok": True, "action": "interrupt"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# Allowed tmux key names to prevent injection
ALLOWED_TMUX_KEYS = {
    "Escape", "Enter", "Space", "Tab", "BSpace",
    "Up", "Down", "Left", "Right",
    "C-c", "C-d", "C-z", "C-l", "C-a", "C-e", "C-u", "C-k", "C-w",
    "PageUp", "PageDown", "Home", "End",
}

@app.post("/api/sessions/{session_name}/send-keys")
async def api_send_keys(session_name: str, body: SendKeys):
    """Send raw key sequences to a tmux session (Escape, C-c, Enter, q, etc.).

    Unlike /send, this does NOT wrap text in -l (literal) mode and does NOT
    auto-append Enter. Use this for terminal control keys.
    """
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        for key in body.keys:
            # Allow single printable characters (q, y, n, etc.) and known tmux key names
            if key in ALLOWED_TMUX_KEYS or (len(key) == 1 and key.isprintable()):
                await asyncio.to_thread(subprocess.run,
                    ["tmux", "send-keys", "-t", session_name, key],
                    capture_output=True, text=True, timeout=5
                )
            else:
                return JSONResponse({"error": f"Key not allowed: {key}"}, status_code=400)
        return JSONResponse({"ok": True, "keys_sent": body.keys})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


class BracketedPasteBody(BaseModel):
    enabled: bool

@app.post("/api/sessions/{session_name}/bracketed-paste")
async def api_bracketed_paste_toggle(session_name: str, body: BracketedPasteBody):
    """Toggle bracketed paste mode for a tmux session.
    Sends the ANSI escape sequence to enable/disable bracketed paste in the terminal.
    """
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        if body.enabled:
            # \e[?2004h — enable bracketed paste
            hex_seq = ["1b", "5b", "3f", "32", "30", "30", "34", "68"]
        else:
            # \e[?2004l — disable bracketed paste
            hex_seq = ["1b", "5b", "3f", "32", "30", "30", "34", "6c"]
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "-H"] + hex_seq,
            capture_output=True, text=True, timeout=5,
        )
        return JSONResponse({"ok": True, "bracketed_paste": body.enabled})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/sessions/{session_name}/set-auth-mode")
async def api_set_auth_mode(session_name: str, body: AuthModeBody):
    """Toggle between API key and subscription auth for a specific session."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        if body.mode == "api":
            key = _stored_anthropic_key
            if not key:
                # Fallback: try to extract from ~/CLAUDE.md
                try:
                    claude_md = (Path.home() / "CLAUDE.md").read_text()
                    for line in claude_md.splitlines():
                        line = line.strip()
                        if line.startswith("sk-ant-"):
                            key = line.split()[0].rstrip(",;")
                            break
                        elif "sk-ant-" in line:
                            m = re.search(r'(sk-ant-\S+)', line)
                            if m:
                                key = m.group(1).rstrip(",;")
                                break
                except Exception:
                    logger.debug("Failed to scan credentials file for API key", exc_info=True)
            if not key:
                return JSONResponse({"error": "No API key found"}, status_code=400)
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l",
                 f"export ANTHROPIC_API_KEY={key}"],
                capture_output=True, text=True, timeout=5
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, text=True, timeout=5
            )
        elif body.mode == "subscription":
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l",
                 "unset ANTHROPIC_API_KEY"],
                capture_output=True, text=True, timeout=5
            )
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, text=True, timeout=5
            )
        else:
            return JSONResponse({"error": "Invalid mode"}, status_code=400)
        _session_auth_mode[session_name] = body.mode
        return JSONResponse({"ok": True, "mode": body.mode, "session": session_name})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- Auto-responder for Claude Code interactive prompts ---
# Automatically detects when Claude Code is waiting for user input
# (plan approval, questions, permission prompts) and sends Enter
# to select the default/first option — keeps sessions unblocked.

_auto_respond_cooldown: Dict[str, float] = {}
_AUTO_RESPOND_INTERVAL = 3      # seconds between checks
_AUTO_RESPOND_COOLDOWN = 10     # min seconds between auto-responds per session
_auto_respond_log: list = []    # recent auto-respond events (for debugging)


def _detect_interactive_prompt(visible_text: str) -> str | None:
    """Check if visible terminal shows a Claude Code interactive prompt.

    Returns a description of the detected prompt, or None.
    """
    lines = visible_text.strip().split("\n")
    last_25 = lines[-25:]
    text = "\n".join(last_25)

    # Must have the ❯ selection cursor
    if "\u276f" not in text and "❯" not in text:
        return None

    # Count lines that look like numbered options: "  1. text" or "❯ 1. text"
    numbered = 0
    has_selector_on_option = False
    for line in last_25:
        stripped = line.strip()
        if re.match(r"^[❯\u276f\s]*\d+\.\s", stripped):
            numbered += 1
        if re.match(r"^❯\s*\d+\.", stripped) or re.match(r"^\u276f\s*\d+\.", stripped):
            has_selector_on_option = True

    # Strong signal: specific Claude Code prompt keywords
    strong_keywords = [
        "bypass permissions",
        "manually approve edits",
        "shift+tab to approve",
        "Would you like to proceed",
        "Tell Claude what to change",
        "approve with this feedback",
    ]
    has_strong = any(kw in text for kw in strong_keywords)

    # Plan approval or known prompt pattern
    if has_strong and numbered >= 2:
        return "plan_approval"

    # Generic Claude Code selection prompt: ❯ on a numbered option + 2+ options
    if has_selector_on_option and numbered >= 2:
        return "selection_prompt"

    return None


async def _auto_responder_loop():
    """Background loop that auto-responds to Claude Code interactive prompts."""
    log = logging.getLogger("auto-responder")
    await asyncio.sleep(5)  # initial delay after startup
    while True:
        try:
            await asyncio.sleep(_AUTO_RESPOND_INTERVAL)
            sessions_list = get_tmux_sessions()
            now = time.time()
            for sess in sessions_list:
                name = sess["name"]
                # Check cooldown
                last = _auto_respond_cooldown.get(name, 0)
                if now - last < _AUTO_RESPOND_COOLDOWN:
                    continue
                # Capture visible pane (not history — just what's on screen)
                try:
                    result = await asyncio.to_thread(
                        subprocess.run,
                        ["tmux", "capture-pane", "-t", name, "-p"],
                        capture_output=True, text=True, timeout=3,
                    )
                    if result.returncode != 0 or not result.stdout.strip():
                        continue
                except Exception:
                    continue

                prompt_type = _detect_interactive_prompt(result.stdout)
                if prompt_type:
                    # Send Enter to accept the highlighted (first) option
                    await asyncio.to_thread(
                        subprocess.run,
                        ["tmux", "send-keys", "-t", name, "Enter"],
                        capture_output=True, text=True, timeout=3,
                    )
                    _auto_respond_cooldown[name] = now
                    event = {"session": name, "type": prompt_type, "ts": now}
                    _auto_respond_log.append(event)
                    # Keep log bounded
                    if len(_auto_respond_log) > 50:
                        _auto_respond_log.pop(0)
                    log.info(f"Auto-responded to {prompt_type} in session '{name}'")
        except Exception:
            logger.debug("Auto-responder loop iteration failed", exc_info=True)


@app.get("/api/auto-respond-log")
async def api_auto_respond_log():
    """Recent auto-respond events for debugging."""
    return JSONResponse(_auto_respond_log[-20:])


# --- Autonomous Mode Watchdog ---
# Monitors all active away-mode and go-nuts-mode sessions.
# Detects stalls (no terminal change for too long) and unsticks them.

_watchdog_snapshots: Dict[str, dict] = {}
# Per-session: {"content_hash": str, "first_seen": float, "nudge_count": int, "last_nudge": float}

_WATCHDOG_INTERVAL = 30         # Check every 30 seconds
_STALL_THRESHOLD = 600          # 10 minutes of identical terminal = stalled
_NUDGE_COOLDOWN = 180           # Wait 3 minutes between nudge attempts
_MAX_NUDGES_BEFORE_RESTART = 3  # After 3 failed nudges, hard-restart the mode

_NUDGE_PROMPT = """You appear to be idle or stuck. The user is not present — you are in autonomous mode.

If you just finished a task: pick the next one and start working. Check your skill files and backlog.
If you're waiting for something: cancel the wait (Ctrl+C if needed) and move to a different task.
If you encountered an error: log it, revert if needed, and continue with the next item.

Do NOT say "standing by" or ask for instructions. Take action NOW."""

_UNSTICK_PROMPT_AWAY = """You are in Away Mode. The system detected you were stuck and restarted your task loop.

Your previous work is preserved on the current branch. Pick up where you left off:
1. Run `pwd` to confirm you are in the correct project directory
2. Check git log to see what you've already done
3. Check /tmp/ for your session-specific notes (files prefixed with your session name)
4. Pick the next most valuable skill to execute

IMPORTANT: Only work on files within your current project directory. Do not touch other projects.

Available skills are at: {skills_dir}/
Read a SKILL.md file and execute its tasks. Take action immediately."""

_UNSTICK_PROMPT_GONUTS = """You are in Go Nuts Mode. The system detected you were stuck and restarted your task loop.

Your previous work is preserved on the current branch. Pick up where you left off:
1. Run `pwd` to confirm you are in the correct project directory
2. Check git log to see what you've already built
3. Check /tmp/ for your session-specific notes (files prefixed with your session name)
4. Pick the next feature to build or generate new ideas

IMPORTANT: Only work on files within your current project directory. Do not touch other projects.

Available skills are at: {skills_dir}/
Read a SKILL.md file and execute its tasks. Build something NOW."""


async def _restore_autonomous_mode(session_name: str, state: dict, mode: str):
    """Restore an autonomous mode after server restart: wait for session, send prompt, launch loop."""
    rlog = logging.getLogger("restore")
    rlog.info(f"Restoring {mode} mode for '{session_name}' — waiting 15s for tmux to stabilize")
    log_fn = _away_log if mode == "away" else _go_nuts_log

    try:
        # Give tmux and Claude Code a moment to settle after server restart
        await asyncio.sleep(15)

        if not state.get("enabled"):
            return

        # Check the session still exists
        try:
            activity = await async_detect_activity(session_name)
        except Exception:
            log_fn(state, "Session not found during restore — stopping")
            state["enabled"] = False
            _save_autonomous_state()
            return

        # Wait for session to be idle before sending prompt (max 10 min)
        if activity.get("status") == "busy":
            log_fn(state, "Session is busy — waiting for it to finish current task")
            await _away_wait_for_idle(session_name, timeout=600)

        if not state.get("enabled"):
            return

        # Ensure Claude Code is actually running (handles OOM/crash during server downtime)
        claude_ok = await _ensure_claude_running(session_name, log_fn, state)
        if not claude_ok:
            log_fn(state, "Could not restart Claude Code during restore — stopping")
            state["enabled"] = False
            _save_autonomous_state()
            return

        # Send the appropriate unstick/resume prompt (with project isolation)
        skills_dir = _SKILLS_DIR if mode == "away" else _GO_NUTS_SKILLS_DIR
        unstick_prompt = _build_project_isolation_preamble(session_name) + (_UNSTICK_PROMPT_AWAY if mode == "away" else _UNSTICK_PROMPT_GONUTS).format(skills_dir=skills_dir)
        log_fn(state, "Sending resume prompt to session")
        await _away_send_prompt(session_name, unstick_prompt)
        await asyncio.sleep(2)

        # Now enter the continuous monitoring loop
        if mode == "away":
            await _away_mode_continuous_loop(session_name)
        else:
            await _go_nuts_continuous_loop(session_name)

    except asyncio.CancelledError:
        if _shutting_down:
            log_fn(state, f"{mode} restore cancelled (server shutdown — will restore)")
        else:
            log_fn(state, f"{mode} restore cancelled")
            state["enabled"] = False
            _save_autonomous_state()
        raise
    except Exception as e:
        log_fn(state, f"{mode} restore error: {e}")
        rlog.error(f"Restore {mode} for '{session_name}' failed: {e}")
        # Don't set enabled=False — watchdog zombie detection will restart us
    finally:
        state["task"] = None


async def _watchdog_loop():
    """Background watchdog: detects stalled autonomous sessions and unsticks them."""
    wlog = logging.getLogger("watchdog")
    wlog.info("Autonomous mode watchdog started")
    while True:
        try:
            await asyncio.sleep(_WATCHDOG_INTERVAL)
            # Collect all active autonomous sessions
            active_sessions: list[tuple[str, dict, str]] = []  # (name, state, mode)
            for name, state in _away_mode_state.items():
                if state.get("enabled") and state.get("task") and not state["task"].done():
                    active_sessions.append((name, state, "away"))
            for name, state in _go_nuts_state.items():
                if state.get("enabled") and state.get("task") and not state["task"].done():
                    active_sessions.append((name, state, "gonuts"))

            if not active_sessions:
                if _watchdog_snapshots:
                    _watchdog_snapshots.clear()
                continue

            for session_name, state, mode in active_sessions:
                try:
                    await _watchdog_check_session(session_name, state, mode, wlog)
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    wlog.error(f"Watchdog error checking '{session_name}': {e}")

            # Also check for zombie states: enabled=True but task is dead
            for name, state in list(_away_mode_state.items()):
                if state.get("enabled") and (not state.get("task") or state["task"].done()):
                    wlog.warning(f"Away mode zombie detected for '{name}' — restarting worker")
                    await _watchdog_restart_mode(name, state, "away", wlog)
            for name, state in list(_go_nuts_state.items()):
                if state.get("enabled") and (not state.get("task") or state["task"].done()):
                    wlog.warning(f"Go Nuts mode zombie detected for '{name}' — restarting worker")
                    await _watchdog_restart_mode(name, state, "gonuts", wlog)

        except asyncio.CancelledError:
            wlog.info("Watchdog cancelled")
            raise
        except Exception as e:
            wlog.error(f"Watchdog loop error: {e}")
            await asyncio.sleep(60)


async def _watchdog_check_session(session_name: str, state: dict, mode: str, wlog):
    """Check a single session for stalls."""
    import hashlib
    now = time.time()

    # Capture recent terminal content (non-blocking)
    recent = await asyncio.to_thread(capture_pane_recent, session_name, 50)
    if not recent.strip():
        return  # Empty pane, can't assess

    content_hash = hashlib.md5(recent.encode()).hexdigest()
    snap = _watchdog_snapshots.get(session_name)

    if snap is None or snap["content_hash"] != content_hash:
        # Terminal content changed — session is making progress
        _watchdog_snapshots[session_name] = {
            "content_hash": content_hash,
            "first_seen": now,
            "nudge_count": 0,
            "last_nudge": 0,
        }
        return

    # Terminal content is UNCHANGED since last check
    stall_duration = now - snap["first_seen"]

    if stall_duration < _STALL_THRESHOLD:
        return  # Not stalled yet — could be processing

    # Terminal has been identical for >10 minutes. Check if there's a good reason.
    log_fn = _away_log if mode == "away" else _go_nuts_log

    # Check if Claude Code has crashed (OOM, etc) — if so, restart immediately
    if not await _async_is_claude_running(session_name):
        wlog.warning(f"Claude Code not running in '{session_name}' — OOM/crash detected, restarting")
        log_fn(state, "Watchdog: Claude Code crashed (OOM?) — restarting")
        _watchdog_snapshots.pop(session_name, None)
        await _watchdog_restart_mode(session_name, state, mode, wlog)
        return

    # First stall detection — use LLM to check if it's a legitimate long operation
    if snap["nudge_count"] == 0 and snap["last_nudge"] == 0:
        wlog.info(f"Potential stall detected for '{session_name}' ({mode}) — {stall_duration:.0f}s unchanged")
        try:
            assessment = await llm_call(
                system_prompt=(
                    "You are monitoring an autonomous AI coding session. The terminal output has not changed "
                    "for over 10 minutes. Assess whether this is:\n"
                    "1. LEGITIMATE: downloading large files, compiling a big project, running extensive tests, "
                    "waiting for a deployment, or any operation that genuinely takes >10 minutes\n"
                    "2. STUCK: the agent said 'standing by', asked a question, hit an error and stopped, "
                    "is waiting for user input, or simply finished and didn't continue\n\n"
                    "Reply with ONLY one word: LEGITIMATE or STUCK"
                ),
                user_content=f"Terminal output (last 50 lines):\n{recent[-3000:]}",
                max_tokens=10,
            )
            assessment = assessment.strip().upper()
        except Exception:
            assessment = "STUCK"  # If we can't assess, assume stuck

        if "LEGITIMATE" in assessment:
            wlog.info(f"Session '{session_name}' stall assessed as LEGITIMATE — skipping for now")
            log_fn(state, f"Watchdog: stall detected ({stall_duration:.0f}s) but appears legitimate — waiting")
            # Push out the first_seen so we re-check in another 10 minutes
            snap["first_seen"] = now - _STALL_THRESHOLD + 300  # Re-check in 5 min
            return

        wlog.info(f"Session '{session_name}' assessed as STUCK — will nudge")

    # Session is stuck. Try nudging.
    if now - snap["last_nudge"] < _NUDGE_COOLDOWN:
        return  # Wait for cooldown between nudges

    if snap["nudge_count"] < _MAX_NUDGES_BEFORE_RESTART:
        # Gentle nudge: send continuation prompt
        snap["nudge_count"] += 1
        snap["last_nudge"] = now
        log_fn(state, f"Watchdog: nudge #{snap['nudge_count']} — sending continuation prompt")
        wlog.info(f"Nudging '{session_name}' (attempt {snap['nudge_count']}/{_MAX_NUDGES_BEFORE_RESTART})")

        # Ensure Claude Code is running before nudging
        if not await _async_is_claude_running(session_name):
            wlog.warning(f"Claude Code not running in '{session_name}' during nudge — restarting mode")
            log_fn(state, "Watchdog: Claude Code not running during nudge — restarting")
            _watchdog_snapshots.pop(session_name, None)
            await _watchdog_restart_mode(session_name, state, mode, wlog)
            return

        # If session appears to be waiting for input or truly idle, just send the nudge
        try:
            activity = await async_detect_activity(session_name)
        except Exception:
            activity = {"status": "unknown"}

        if activity["status"] == "busy":
            # Session claims busy but terminal hasn't changed — might be truly stuck
            # Send Ctrl+C first to break out of whatever it's doing
            log_fn(state, "Watchdog: session reports busy but no terminal change — sending Ctrl+C")
            await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", session_name, "C-c"], timeout=3, capture_output=True)
            await asyncio.sleep(5)

        await _away_send_prompt(session_name, _build_project_isolation_preamble(session_name) + _NUDGE_PROMPT)
        return

    # Nudges exhausted — hard restart
    log_fn(state, f"Watchdog: {_MAX_NUDGES_BEFORE_RESTART} nudges failed — restarting {mode} mode")
    wlog.warning(f"Restarting {mode} mode for '{session_name}' after {snap['nudge_count']} failed nudges")
    await _watchdog_restart_mode(session_name, state, mode, wlog)
    # Reset snapshot
    _watchdog_snapshots.pop(session_name, None)


async def _watchdog_restart_mode(session_name: str, state: dict, mode: str, wlog):
    """Gracefully restart an autonomous mode session, preserving history."""
    log_fn = _away_log if mode == "away" else _go_nuts_log

    # 1. Cancel existing task
    old_task = state.get("task")
    if old_task and not old_task.done():
        old_task.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(old_task), timeout=5)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass

    # 2. Send Ctrl+C to break any stuck process in the terminal
    try:
        await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", session_name, "C-c"], timeout=3, capture_output=True)
        await asyncio.sleep(3)
        await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", session_name, "C-c"], timeout=3, capture_output=True)
        await asyncio.sleep(2)
    except Exception:
        pass

    # 2b. Ensure Claude Code is actually running (handles OOM/crash recovery)
    claude_ok = await _ensure_claude_running(session_name, log_fn, state)
    if not claude_ok:
        log_fn(state, "Watchdog: could not restart Claude Code — aborting restart")
        state["enabled"] = False
        _save_autonomous_state()
        return

    # 3. Preserve the log history, reset state for fresh loop
    old_log = state.get("log", [])
    old_started = state.get("started_at", time.time())
    old_step = state.get("step", 0)

    log_fn(state, "Watchdog: restarting mode — skipping initial phases, jumping to continuous loop")

    # 4. Re-initialize state
    state.update({
        "enabled": True,
        "phase": 4,
        "phase_name": "Continuous (restarted)" if mode == "away" else "Continuous Build (restarted)",
        "step": old_step,
        "step_name": "Watchdog restart",
        "started_at": old_started,  # Keep original start time
        "log": old_log,  # Keep full log history
        "task": None,
    })
    _save_autonomous_state()

    # 5. Send an unstick prompt directly instead of re-running initial phases (with project isolation)
    skills_dir = _SKILLS_DIR if mode == "away" else _GO_NUTS_SKILLS_DIR
    unstick_prompt = _build_project_isolation_preamble(session_name) + (_UNSTICK_PROMPT_AWAY if mode == "away" else _UNSTICK_PROMPT_GONUTS).format(skills_dir=skills_dir)

    await _away_send_prompt(session_name, unstick_prompt)
    await asyncio.sleep(2)

    # 6. Launch fresh worker that skips to continuous loop
    if mode == "away":
        task = asyncio.create_task(_away_mode_continuous_loop(session_name))
        state["task"] = task
    else:
        task = asyncio.create_task(_go_nuts_continuous_loop(session_name))
        state["task"] = task

    wlog.info(f"Restarted {mode} mode for '{session_name}' — continuous loop relaunched")


async def _away_mode_continuous_loop(session_name: str):
    """Standalone continuous loop for away mode (used by watchdog restart)."""
    log = logging.getLogger("away-mode")
    state = _away_mode_state[session_name]
    try:
        # Wait for the unstick prompt to be processed
        await _away_wait_for_idle(session_name, timeout=600)

        cycle = state.get("step", 0) + 1
        consecutive_errors = 0
        while state.get("enabled"):
            try:
                _away_log(state, f"Monitoring for idle (cycle {cycle})...")
                idle_since = None
                while state.get("enabled"):
                    await asyncio.sleep(10)
                    try:
                        activity = await async_detect_activity(session_name)
                    except Exception:
                        activity = {"status": "unknown"}
                    if activity["status"] == "idle":
                        if idle_since is None:
                            idle_since = time.time()
                        elif time.time() - idle_since >= 90:
                            break
                    else:
                        idle_since = None

                if not state.get("enabled"):
                    return

                # Ensure Claude Code is running before sending prompt (OOM recovery)
                claude_ok = await _ensure_claude_running(session_name, _away_log, state)
                if not claude_ok:
                    _away_log(state, "Claude Code dead and couldn't restart — stopping away mode")
                    state["enabled"] = False
                    _save_autonomous_state()
                    return

                state["step"] = cycle
                state["step_name"] = f"Ping cycle {cycle}"
                _away_log(state, f"Session idle 90s — task ping (cycle {cycle})")
                await _away_send_and_wait(session_name, _AWAY_PING_PROMPT, state,
                                           f"Task ping cycle {cycle}", timeout=900)
                cycle += 1
                consecutive_errors = 0
                _save_autonomous_state()  # Periodic save after each successful cycle
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                _away_log(state, f"Cycle {cycle} error ({consecutive_errors}): {e}")
                log.error(f"Away mode cycle error for '{session_name}': {e}")
                if consecutive_errors >= 5:
                    await asyncio.sleep(300)
                    consecutive_errors = 0
                else:
                    await asyncio.sleep(30)

    except asyncio.CancelledError:
        if _shutting_down:
            _away_log(state, "Away mode (restarted) cancelled (server shutdown — will restore)")
        else:
            _away_log(state, "Away mode (restarted) cancelled")
            state["enabled"] = False
            _save_autonomous_state()
        raise
    except Exception as e:
        _away_log(state, f"Away mode (restarted) error: {e}")
        log.error(f"Away mode restarted loop error for '{session_name}': {e}")
        _save_autonomous_state()  # Save state so watchdog can recover
        # Don't set enabled=False — let watchdog zombie detection restart us
    finally:
        state["task"] = None


async def _go_nuts_continuous_loop(session_name: str):
    """Standalone continuous loop for go-nuts mode (used by watchdog restart)."""
    log = logging.getLogger("go-nuts-mode")
    state = _go_nuts_state[session_name]
    try:
        await _away_wait_for_idle(session_name, timeout=600)

        cycle = state.get("step", 0) + 1
        consecutive_errors = 0
        while state.get("enabled"):
            try:
                _go_nuts_log(state, f"Monitoring for idle (cycle {cycle})...")
                idle_since = None
                while state.get("enabled"):
                    await asyncio.sleep(10)
                    try:
                        activity = await async_detect_activity(session_name)
                    except Exception:
                        activity = {"status": "unknown"}
                    if activity["status"] == "idle":
                        if idle_since is None:
                            idle_since = time.time()
                        elif time.time() - idle_since >= 90:
                            break
                    else:
                        idle_since = None

                if not state.get("enabled"):
                    return

                # Ensure Claude Code is running before sending prompt (OOM recovery)
                claude_ok = await _ensure_claude_running(session_name, _go_nuts_log, state)
                if not claude_ok:
                    _go_nuts_log(state, "Claude Code dead and couldn't restart — stopping go nuts mode")
                    state["enabled"] = False
                    _save_autonomous_state()
                    return

                state["step"] = cycle
                state["step_name"] = f"Build cycle {cycle}"
                _go_nuts_log(state, f"Session idle 90s — build ping (cycle {cycle})")
                await _go_nuts_send_and_wait(session_name, _GN_PING_PROMPT, state,
                                              f"Build cycle {cycle}", timeout=900)
                cycle += 1
                consecutive_errors = 0
                _save_autonomous_state()  # Periodic save after each successful cycle
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                _go_nuts_log(state, f"Cycle {cycle} error ({consecutive_errors}): {e}")
                log.error(f"Go Nuts cycle error for '{session_name}': {e}")
                if consecutive_errors >= 5:
                    await asyncio.sleep(300)
                    consecutive_errors = 0
                else:
                    await asyncio.sleep(30)

    except asyncio.CancelledError:
        if _shutting_down:
            _go_nuts_log(state, "Go Nuts mode (restarted) cancelled (server shutdown — will restore)")
        else:
            _go_nuts_log(state, "Go Nuts mode (restarted) cancelled")
            state["enabled"] = False
            _save_autonomous_state()
        raise
    except Exception as e:
        _go_nuts_log(state, f"Go Nuts mode (restarted) error: {e}")
        log.error(f"Go Nuts restarted loop error for '{session_name}': {e}")
        _save_autonomous_state()  # Save state so watchdog can recover
        # Don't set enabled=False — let watchdog zombie detection restart us
    finally:
        state["task"] = None


# --- Away Mode ---
# Autonomous mode: sends structured prompts to a Claude Code session,
# waits for idle, captures output, summarizes, advances to next phase.

def _away_log(state: dict, action: str):
    """Append a log entry to the away-mode state."""
    entry = {"ts": time.time(), "phase": state.get("phase", 0), "step": state.get("step", 0), "action": action}
    state.setdefault("log", []).append(entry)
    if len(state["log"]) > 200:
        state["log"] = state["log"][-200:]


def _away_state_summary(state: dict) -> dict:
    """Return a JSON-safe summary of away-mode state (no asyncio.Task)."""
    return {
        "enabled": state.get("enabled", False),
        "phase": state.get("phase", 0),
        "phase_name": state.get("phase_name", ""),
        "step": state.get("step", 0),
        "step_name": state.get("step_name", ""),
        "started_at": state.get("started_at", 0),
        "log": state.get("log", [])[-30:],
        "report": state.get("report", ""),
    }


async def _away_send_prompt(session_name: str, prompt: str):
    """Send a long prompt to a Claude Code session via tmux paste-buffer.

    Two-phase approach to defeat the bracketed paste "[Pasted text +N lines]" hang:
    Phase 1: Write prompt to a temp file, then send a short shell-pipe command that
             reads the file and feeds it to the Claude Code prompt via xdotool-style
             keyboard simulation. This avoids bracketed paste entirely.
    Phase 2 (fallback): If Phase 1 fails or Claude Code is truly at its ❯ prompt
             (not a shell), use paste-buffer with aggressive Enter retries.
    """
    log = logging.getLogger("away-mode")
    prompt_text = prompt.rstrip("\n\r ")  # Strip trailing whitespace/newlines
    prompt_file = None
    try:
        fd, prompt_file = tempfile.mkstemp(prefix=f"away-prompt-{session_name}-", suffix=".md")
        os.close(fd)
        Path(prompt_file).write_text(prompt_text)

        # Capture terminal state before paste to detect changes later
        pre_snapshot = await asyncio.to_thread(capture_pane_recent, session_name, 5)

        # --- Strategy: Disable bracketed paste, then paste raw ---
        # Send \e[?2004l escape sequence directly to the terminal to disable
        # bracketed paste mode. tmux send-keys -H sends raw hex bytes.
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "-H",
             "1b", "5b", "3f", "32", "30", "30", "34", "6c"],  # \e[?2004l
            capture_output=True, text=True, timeout=5,
        )
        await asyncio.sleep(0.2)

        # Load file into tmux buffer and paste
        await asyncio.to_thread(subprocess.run,
            ["tmux", "load-buffer", prompt_file],
            capture_output=True, text=True, timeout=5,
        )
        await asyncio.to_thread(subprocess.run,
            ["tmux", "paste-buffer", "-t", session_name],
            capture_output=True, text=True, timeout=10,
        )

        # Scale wait time with prompt size
        wait_secs = max(2.0, min(8.0, len(prompt_text) / 1500))
        await asyncio.sleep(wait_secs)

        # Send Enter to submit
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            capture_output=True, text=True, timeout=5,
        )
        log.info(f"Sent prompt to '{session_name}' ({len(prompt_text)} chars, waited {wait_secs:.1f}s)")

        # Note: we do NOT re-enable bracketed paste (\e[?2004h) here.
        # Bracketed paste causes "[Pasted text +N lines]" previews that hang.
        # Users can toggle it back on from the Keys bar if they want it.

        # --- Verify submission with retries ---
        for attempt in range(3):
            await asyncio.sleep(3)
            try:
                activity = await async_detect_activity(session_name)
            except Exception:
                activity = {"status": "unknown"}

            if activity["status"] == "busy":
                log.info(f"Session '{session_name}' is busy — prompt accepted")
                return  # Success — session started processing

            # Check if terminal output changed (even if still "idle" per detection)
            post_snapshot = await asyncio.to_thread(capture_pane_recent, session_name, 5)
            if post_snapshot != pre_snapshot:
                log.info(f"Session '{session_name}' terminal changed — prompt likely accepted")
                return  # Terminal content changed, prompt was received

            # Still showing the same content — try Enter again
            log.warning(f"Session '{session_name}' still idle after paste (attempt {attempt+1}/3) — retrying Enter")
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, text=True, timeout=5,
            )

        # All Enter retries failed. Check if there's a bracketed paste preview stuck.
        recent = await asyncio.to_thread(capture_pane_recent, session_name, 10)
        if "Pasted text" in recent or "pasted" in recent.lower():
            # Bracketed paste preview is stuck — Escape to cancel it, then re-send
            log.warning(f"Session '{session_name}' has stuck paste preview — clearing and retrying")
            await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", session_name, "Escape"], timeout=3, capture_output=True)
            await asyncio.sleep(0.5)
            await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", session_name, "C-c"], timeout=3, capture_output=True)
            await asyncio.sleep(1)
            # Re-send the prompt, this time relying on bracketed paste disabled earlier
            await asyncio.to_thread(subprocess.run, ["tmux", "load-buffer", prompt_file], capture_output=True, text=True, timeout=5)
            await asyncio.to_thread(subprocess.run, ["tmux", "paste-buffer", "-t", session_name], capture_output=True, text=True, timeout=10)
            await asyncio.sleep(wait_secs)
            await asyncio.to_thread(subprocess.run, ["tmux", "send-keys", "-t", session_name, "Enter"], capture_output=True, text=True, timeout=5)
            log.info(f"Re-sent prompt to '{session_name}' after clearing stuck paste")

    except Exception as e:
        log.error(f"Failed to send prompt to '{session_name}': {e}")
    finally:
        if prompt_file:
            try:
                os.unlink(prompt_file)
            except (OSError, UnboundLocalError):
                pass


async def _away_wait_for_idle(session_name: str, timeout: int = 900) -> bool:
    """Wait for a session to become busy then return to idle. Returns True on success."""
    log = logging.getLogger("away-mode")

    # Phase A: wait for session to become busy (max 30s)
    start = time.time()
    became_busy = False
    while time.time() - start < 30:
        await asyncio.sleep(2)
        activity = await async_detect_activity(session_name)
        if activity["status"] == "busy":
            became_busy = True
            break

    if not became_busy:
        log.warning(f"Session '{session_name}' never became busy, proceeding anyway")

    # Phase B: wait for session to return to idle (up to timeout)
    idle_count = 0
    while time.time() - start < timeout:
        await asyncio.sleep(5)
        activity = await async_detect_activity(session_name)
        if activity["status"] == "idle":
            idle_count += 1
            if idle_count >= 2:  # 2 consecutive idle readings = confirmed idle
                return True
        else:
            idle_count = 0

    log.warning(f"Session '{session_name}' timed out after {timeout}s")
    return False


def _build_project_isolation_preamble(session_name: str) -> str:
    """Build a preamble that anchors the autonomous mode to the correct project."""
    cwd = get_session_cwd(session_name)
    return f"""PROJECT ISOLATION — READ THIS FIRST:
You are working in tmux session "{session_name}".
Your project directory is: {cwd or '(unknown — run pwd to confirm)'}

STRICT RULES:
- ONLY modify files inside your project directory ({cwd or 'current working directory'}).
- NEVER cd into, read from, or write to other project directories on this server.
- NEVER modify files under /var/www/, /opt/, or ~/  that belong to other projects.
- Write all reports and temp files to /tmp/ using your session name as prefix: /tmp/{session_name}-*.md
- Do NOT write to generic paths like /tmp/away-mode-*.md or /tmp/go-nuts-*.md — always include the session name.
- If you need to check infrastructure context, read ~/.claude/vm_projects_dir.md (read-only, never modify during autonomous mode).
- If a skill or task does not apply to THIS project, skip it entirely.
- Do NOT restart, modify configs for, or interact with services belonging to other projects.

"""


async def _away_send_and_wait(session_name: str, prompt: str, state: dict,
                               step_name: str, timeout: int = 900) -> str:
    """Send prompt, wait for completion, capture and summarize output."""
    state["step_name"] = step_name
    _away_log(state, f"Sending: {step_name}")

    prompt = _build_project_isolation_preamble(session_name) + prompt
    await _away_send_prompt(session_name, prompt)
    completed = await _away_wait_for_idle(session_name, timeout=timeout)

    if not completed:
        _away_log(state, f"Timeout on: {step_name}")
        # Send Ctrl+C to unstick if needed
        subprocess.run(["tmux", "send-keys", "-t", session_name, "C-c"], timeout=3)
        await asyncio.sleep(2)

    # Capture output and summarize
    output = capture_pane_full(session_name)
    try:
        summary = await llm_call(
            system_prompt=(
                "Summarize what the agent accomplished in this terminal output. "
                "Focus on concrete actions: files created/modified, tests run, errors, "
                "branches created, findings. Be specific and concise. Under 80 words."
            ),
            user_content=f"Away mode step: {step_name}\n\nTerminal output:\n{output[-4000:]}",
            max_tokens=200,
        )
    except Exception:
        summary = "(summary unavailable)"

    _away_log(state, f"Done: {summary[:200]}")
    state["step"] += 1
    return summary


# --- Phase implementations ---
# Skills are installed at ~/.claude/away-mode-skills/XX-name/SKILL.md
_SKILLS_DIR = str(Path.home() / ".claude" / "away-mode-skills")

_PHASE1_PROMPT = f"""I'm putting you in Away Mode. You are autonomous — the user is not present. Every action must be safe, verifiable, and revertible. You cannot ask questions — make decisions and document reasoning.

IMPORTANT: Detailed skill instructions are available as files on disk at:
  {_SKILLS_DIR}/
Each subdirectory contains a SKILL.md with specific tasks and guidance. You MUST read the relevant SKILL.md file before executing any skill.

PHASE 1: Study the Project

1. Read the project root directory structure
2. Examine root config files (package.json, pyproject.toml, Cargo.toml, go.mod, Makefile, docker-compose.yml, .env.example, etc.)
3. Examine source directories (src/, app/, lib/, components/, routes/, api/)
4. Check test directories (test/, tests/, __tests__/, spec/, e2e/)
5. Check git history: recent commits, active areas of development
6. Create a project profile at /tmp/<SESSION_NAME>-away-mode-profile.md (replace <SESSION_NAME> with the session name from the PROJECT ISOLATION preamble above) covering:
   - Project name, type, primary languages, frameworks
   - Architecture: frontend, backend, database, external services
   - Current state: can it build? tests? linting?
   - Development patterns, known issues from TODOs
7. Establish baseline:
   - Record git status and recent commits
   - Create safety branch: git checkout -b away-mode/session-$(date +%Y%m%d-%H%M%S)
   - Run existing tests if available, record results
   - Run linter if configured, record results

CRITICAL: Never commit to main/master. Work on the away-mode branch.
CRITICAL: If tests fail at baseline, note which tests fail — do NOT introduce NEW failures.

When done with this phase, immediately continue to the next task without waiting."""

_PHASE2_PROMPT = f"""PHASE 2: Select Applicable Skills

Review the project profile you created. The following skills are available on disk. For each, read its SKILL.md to understand its scope, then decide if it applies to THIS project.

Available skills (read the SKILL.md inside each directory):
 1. {_SKILLS_DIR}/05-security/SKILL.md — Security Auditing (ALWAYS applicable)
 2. {_SKILLS_DIR}/08-testing/SKILL.md — Testing & Coverage (ALWAYS applicable)
 3. {_SKILLS_DIR}/09-code-quality/SKILL.md — Code Quality & Refactoring (ALWAYS applicable)
 4. {_SKILLS_DIR}/07-dependencies/SKILL.md — Dependency Management
 5. {_SKILLS_DIR}/10-error-handling/SKILL.md — Error Handling & Resilience
 6. {_SKILLS_DIR}/13-documentation/SKILL.md — Documentation
 7. {_SKILLS_DIR}/21-codebase-audit/SKILL.md — Codebase Audit & Reporting
 8. {_SKILLS_DIR}/22-config-hardening/SKILL.md — Build & Config Hardening
 9. {_SKILLS_DIR}/01-live-qa/SKILL.md — Live QA & Runtime Testing (if web app)
10. {_SKILLS_DIR}/02-performance/SKILL.md — Performance & Speed (if web app/API)
11. {_SKILLS_DIR}/06-content-integrity/SKILL.md — Content & Data Integrity
12. {_SKILLS_DIR}/03-seo/SKILL.md — SEO & Web Standards (if public web pages)
13. {_SKILLS_DIR}/04-accessibility/SKILL.md — Accessibility (if UI exists)
14. {_SKILLS_DIR}/14-styling/SKILL.md — Styling & Visual Polish (if UI)
15. {_SKILLS_DIR}/15-data-api/SKILL.md — Data, Database & API Quality
16. {_SKILLS_DIR}/16-observability/SKILL.md — Logging & Observability
17. {_SKILLS_DIR}/19-ux-improvements/SKILL.md — UX Micro-Improvements (if UI)
18. {_SKILLS_DIR}/23-git-hygiene/SKILL.md — Git Hygiene
19. {_SKILLS_DIR}/12-devops/SKILL.md — DevOps & CI/CD
20. {_SKILLS_DIR}/20-feature-generation/SKILL.md — Smart Feature Generation

For each skill, read the SKILL.md, then decide: applicable? priority? risk level?

Write selection to /tmp/<SESSION_NAME>-away-mode-skills-selected.md (use your session name) with selected skills in priority order.

When done, immediately continue to executing skills — do not wait."""

_PHASE3_ROUND1_PROMPT = f"""PHASE 3, ROUND 1: Observe and Audit (NO code changes)

Read these skill files and execute their AUDIT tasks. Do NOT modify code — observe and report only.

1. Read {_SKILLS_DIR}/05-security/SKILL.md — execute all security scan tasks
2. Read {_SKILLS_DIR}/06-content-integrity/SKILL.md — execute content integrity checks
3. Read {_SKILLS_DIR}/21-codebase-audit/SKILL.md — execute codebase audit tasks

For EVERY finding, log it in /tmp/<SESSION_NAME>-away-mode-audit.md (use your session name) with:
- Category | Severity (critical/high/medium/low) | Description | File:Line

When done, immediately continue to the next round."""

_PHASE3_ROUND2_PROMPT = f"""PHASE 3, ROUND 2: Safe Mechanical Fixes

Read these skill files and execute their FIX tasks — only safe, deterministic changes:

1. Read {_SKILLS_DIR}/07-dependencies/SKILL.md — apply patch updates (semver-safe only)
2. Read {_SKILLS_DIR}/22-config-hardening/SKILL.md — tighten configs, fix .gitignore
3. Read {_SKILLS_DIR}/23-git-hygiene/SKILL.md — clean up git state

EXECUTION WRAPPER for every change:
1. Record current git SHA
2. Make ONE logical change
3. Run full test suite + build
4. ALL GREEN → commit: [away-mode][category] description
5. ANY RED → git checkout . to fully revert

When done, immediately continue to the next round."""

_PHASE3_ROUND3_PROMPT = f"""PHASE 3, ROUND 3: Test-Gated Improvements

Read these skill files and execute their tasks:

1. Read {_SKILLS_DIR}/08-testing/SKILL.md — generate tests for untested code (HIGHEST PRIORITY)
2. Read {_SKILLS_DIR}/09-code-quality/SKILL.md — refactor, remove dead code, simplify
3. Read {_SKILLS_DIR}/10-error-handling/SKILL.md — fix empty catches, add error handlers

EXECUTION WRAPPER: one commit per change, test after each, revert on failure.

When done, immediately continue to the next task."""

async def _away_phase_study(session_name: str, state: dict):
    """Phase 1: Study the project."""
    state["phase"] = 1
    state["phase_name"] = "Study Project"
    state["step"] = 1
    _away_log(state, "Starting Phase 1: Study the Project")
    await _away_send_and_wait(session_name, _PHASE1_PROMPT, state,
                               "Study project structure & establish baseline", timeout=600)


async def _away_phase_select(session_name: str, state: dict):
    """Phase 2: Select applicable skills."""
    state["phase"] = 2
    state["phase_name"] = "Select Skills"
    state["step"] = 1
    _away_log(state, "Starting Phase 2: Select Applicable Skills")
    await _away_send_and_wait(session_name, _PHASE2_PROMPT, state,
                               "Evaluate and select skills", timeout=300)


async def _away_phase_execute(session_name: str, state: dict):
    """Phase 3: Execute skills in rounds."""
    state["phase"] = 3
    state["phase_name"] = "Execute Skills"
    state["step"] = 1

    rounds = [
        ("Round 1: Audit (read-only)", _PHASE3_ROUND1_PROMPT, 600),
        ("Round 2: Safe fixes", _PHASE3_ROUND2_PROMPT, 900),
        ("Round 3: Test-gated improvements", _PHASE3_ROUND3_PROMPT, 900),
    ]

    for round_name, prompt, timeout in rounds:
        if not state.get("enabled"):
            return
        _away_log(state, f"Starting {round_name}")
        await _away_send_and_wait(session_name, prompt, state, round_name, timeout=timeout)
        # Brief pause between rounds
        await asyncio.sleep(5)


_AWAY_PING_PROMPT = f"""You are in Away Mode. The user is not present. You are autonomous.

You just finished your previous task and are now idle. Pick the NEXT most valuable thing to do. Choose a skill you haven't fully completed yet, or revisit one that could be improved.

STEP 1: Pick a skill from the list below.
STEP 2: Read the SKILL.md file for that skill — it contains detailed tasks and instructions.
STEP 3: Execute the tasks described in the SKILL.md.

Available skills (each has a SKILL.md with full instructions):
 1. {_SKILLS_DIR}/05-security/SKILL.md — Security Auditing & Hardening
 2. {_SKILLS_DIR}/08-testing/SKILL.md — Testing & Coverage
 3. {_SKILLS_DIR}/09-code-quality/SKILL.md — Code Quality & Refactoring
 4. {_SKILLS_DIR}/07-dependencies/SKILL.md — Dependency Management
 5. {_SKILLS_DIR}/10-error-handling/SKILL.md — Error Handling & Resilience
 6. {_SKILLS_DIR}/13-documentation/SKILL.md — Documentation
 7. {_SKILLS_DIR}/02-performance/SKILL.md — Performance & Speed
 8. {_SKILLS_DIR}/06-content-integrity/SKILL.md — Content & Data Integrity
 9. {_SKILLS_DIR}/22-config-hardening/SKILL.md — Build & Config Hardening
10. {_SKILLS_DIR}/21-codebase-audit/SKILL.md — Codebase Audit & Reporting
11. {_SKILLS_DIR}/14-styling/SKILL.md — Styling & Visual Polish
12. {_SKILLS_DIR}/15-data-api/SKILL.md — Data, Database & API Quality
13. {_SKILLS_DIR}/16-observability/SKILL.md — Logging & Observability
14. {_SKILLS_DIR}/19-ux-improvements/SKILL.md — UX Micro-Improvements
15. {_SKILLS_DIR}/23-git-hygiene/SKILL.md — Git Hygiene
16. {_SKILLS_DIR}/01-live-qa/SKILL.md — Live QA & Runtime Testing
17. {_SKILLS_DIR}/03-seo/SKILL.md — SEO & Web Standards
18. {_SKILLS_DIR}/04-accessibility/SKILL.md — Accessibility
19. {_SKILLS_DIR}/12-devops/SKILL.md — DevOps & CI/CD
20. {_SKILLS_DIR}/20-feature-generation/SKILL.md — Smart Feature Generation
21. {_SKILLS_DIR}/24-cost-optimization/SKILL.md — Cost Optimization
22. {_SKILLS_DIR}/25-migration-readiness/SKILL.md — Migration Readiness
23. {_SKILLS_DIR}/26-email-notifications/SKILL.md — Email & Notifications
24. {_SKILLS_DIR}/27-mobile-pwa/SKILL.md — Mobile & PWA
25. {_SKILLS_DIR}/28-asset-pipeline/SKILL.md — Asset Pipeline
26. {_SKILLS_DIR}/29-developer-tooling/SKILL.md — Developer Tooling
27. {_SKILLS_DIR}/30-disaster-recovery/SKILL.md — Disaster Recovery

Rules:
- Work on the away-mode branch (create one if not already on it)
- Never commit to main/master
- One logical change per commit: [away-mode][category] description
- Run tests after every change — revert immediately on new failures
- READ the SKILL.md first, then execute its specific tasks
- Take concrete action — don't just plan or summarize

Pick a skill, read its SKILL.md, and execute it now."""


async def _away_mode_worker(session_name: str):
    """Main away-mode coroutine. Runs initial phases then loops forever, pinging when idle."""
    log = logging.getLogger("away-mode")
    state = _away_mode_state[session_name]
    log.info(f"Away mode started for session '{session_name}'")
    try:
        # --- Initial setup phases (run once, errors skip to continuous loop) ---
        try:
            await _away_phase_study(session_name, state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _away_log(state, f"Phase 1 error (skipping): {e}")
            log.error(f"Away mode phase 1 error for '{session_name}': {e}")

        if not state.get("enabled"):
            return

        try:
            await _away_phase_select(session_name, state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _away_log(state, f"Phase 2 error (skipping): {e}")
            log.error(f"Away mode phase 2 error for '{session_name}': {e}")

        if not state.get("enabled"):
            return

        try:
            await _away_phase_execute(session_name, state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _away_log(state, f"Phase 3 error (skipping): {e}")
            log.error(f"Away mode phase 3 error for '{session_name}': {e}")

        if not state.get("enabled"):
            return

        # --- Continuous loop: monitor idle, ping with next task ---
        # This loop NEVER exits unless cancelled or disabled by user.
        state["phase"] = 4
        state["phase_name"] = "Continuous"
        cycle = 1
        consecutive_errors = 0
        while state.get("enabled"):
            try:
                _away_log(state, f"Monitoring for idle (cycle {cycle})...")
                # Wait for confirmed idle: 90 seconds of consecutive idle readings
                idle_since = None
                while state.get("enabled"):
                    await asyncio.sleep(10)
                    try:
                        activity = await async_detect_activity(session_name)
                    except Exception:
                        activity = {"status": "unknown"}
                    if activity["status"] == "idle":
                        if idle_since is None:
                            idle_since = time.time()
                        elif time.time() - idle_since >= 90:
                            break  # Confirmed idle for 90s
                    else:
                        idle_since = None  # Reset — session is busy

                if not state.get("enabled"):
                    return

                # Ensure Claude Code is running before sending prompt (OOM recovery)
                claude_ok = await _ensure_claude_running(session_name, _away_log, state)
                if not claude_ok:
                    _away_log(state, "Claude Code dead and couldn't restart — stopping away mode")
                    state["enabled"] = False
                    _save_autonomous_state()
                    return

                # Session has been idle for 90s — send ping prompt
                state["step"] = cycle
                state["step_name"] = f"Ping cycle {cycle}"
                _away_log(state, f"Session idle for 90s — sending task ping (cycle {cycle})")
                await _away_send_and_wait(session_name, _AWAY_PING_PROMPT, state,
                                           f"Task ping cycle {cycle}", timeout=900)
                cycle += 1
                consecutive_errors = 0
                _save_autonomous_state()  # Periodic save after each successful cycle
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                _away_log(state, f"Cycle {cycle} error ({consecutive_errors}): {e}")
                log.error(f"Away mode cycle {cycle} error for '{session_name}': {e}")
                if consecutive_errors >= 5:
                    _away_log(state, "Too many consecutive errors, pausing 5 minutes...")
                    await asyncio.sleep(300)
                    consecutive_errors = 0
                else:
                    await asyncio.sleep(30)

    except asyncio.CancelledError:
        if _shutting_down:
            _away_log(state, "Away mode cancelled (server shutdown — will restore)")
        else:
            _away_log(state, "Away mode cancelled by user")
            state["enabled"] = False
            _save_autonomous_state()
        log.info(f"Away mode cancelled for '{session_name}'")
        raise
    except Exception as e:
        _away_log(state, f"Away mode fatal error: {e}")
        log.error(f"Away mode fatal error for '{session_name}': {e}")
        _save_autonomous_state()  # Save state so watchdog can recover
        # Don't set enabled=False — let watchdog zombie detection restart us
    finally:
        state["task"] = None
        log.info(f"Away mode finished for '{session_name}'")


@app.post("/api/sessions/{session_name}/away-mode")
async def api_away_mode_toggle(session_name: str, body: AwayModeBody):
    """Toggle away mode on or off for a session."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    if body.enabled:
        # Don't allow both away mode and go-nuts mode at the same time on same session
        if _go_nuts_state.get(session_name, {}).get("enabled"):
            return JSONResponse({"error": "Go Nuts Mode is active on this session. Disable it first."}, status_code=409)

        # Check if already running for this session
        if _away_mode_state.get(session_name, {}).get("enabled"):
            return JSONResponse(_away_state_summary(_away_mode_state[session_name]))

        # Initialize and launch
        state = {
            "enabled": True,
            "phase": 0,
            "phase_name": "Initializing",
            "step": 0,
            "step_name": "",
            "started_at": time.time(),
            "log": [],
            "report": "",
            "task": None,
        }
        _away_mode_state[session_name] = state
        _away_log(state, "Away mode enabled")
        task = asyncio.create_task(_away_mode_worker(session_name))
        state["task"] = task
        _save_autonomous_state()
        return JSONResponse(_away_state_summary(state))
    else:
        # Disable
        state = _away_mode_state.get(session_name, {})
        if state.get("task") and not state["task"].done():
            state["task"].cancel()
        state["enabled"] = False
        state["task"] = None
        _away_log(state, "Away mode disabled by user")
        _save_autonomous_state()
        return JSONResponse(_away_state_summary(state))


@app.get("/api/sessions/{session_name}/away-mode")
async def api_away_mode_status(session_name: str):
    """Get current away-mode state for a session."""
    state = _away_mode_state.get(session_name, {})
    return JSONResponse(_away_state_summary(state))


# --- Go Nuts Mode ---
# Autonomous feature-building mode: discovers the project, generates a feature backlog,
# then continuously builds features, tests, and improves the project in a loop.

_GO_NUTS_SKILLS_DIR = str(Path.home() / ".claude" / "go-nuts-mode-skills")

def _go_nuts_log(state: dict, action: str):
    """Append a log entry to the go-nuts-mode state."""
    entry = {"ts": time.time(), "phase": state.get("phase", 0), "step": state.get("step", 0), "action": action}
    state.setdefault("log", []).append(entry)
    if len(state["log"]) > 200:
        state["log"] = state["log"][-200:]


def _go_nuts_state_summary(state: dict) -> dict:
    """Return a JSON-safe summary of go-nuts-mode state (no asyncio.Task)."""
    return {
        "enabled": state.get("enabled", False),
        "phase": state.get("phase", 0),
        "phase_name": state.get("phase_name", ""),
        "step": state.get("step", 0),
        "step_name": state.get("step_name", ""),
        "started_at": state.get("started_at", 0),
        "log": state.get("log", [])[-30:],
        "report": state.get("report", ""),
    }


async def _go_nuts_send_and_wait(session_name: str, prompt: str, state: dict,
                                  step_name: str, timeout: int = 900) -> str:
    """Send prompt, wait for completion, capture and summarize output."""
    state["step_name"] = step_name
    _go_nuts_log(state, f"Sending: {step_name}")

    prompt = _build_project_isolation_preamble(session_name) + prompt
    # Reuse the same send/wait infrastructure as away mode
    await _away_send_prompt(session_name, prompt)
    completed = await _away_wait_for_idle(session_name, timeout=timeout)

    if not completed:
        _go_nuts_log(state, f"Timeout on: {step_name}")
        subprocess.run(["tmux", "send-keys", "-t", session_name, "C-c"], timeout=3)
        await asyncio.sleep(2)

    output = capture_pane_full(session_name)
    try:
        summary = await llm_call(
            system_prompt=(
                "Summarize what the agent accomplished in this terminal output. "
                "Focus on concrete actions: features built, files created/modified, tests run, errors. "
                "Be specific and concise. Under 80 words."
            ),
            user_content=f"Go Nuts step: {step_name}\n\nTerminal output:\n{output[-4000:]}",
            max_tokens=200,
        )
    except Exception:
        summary = "(summary unavailable)"

    _go_nuts_log(state, f"Done: {summary[:200]}")
    state["step"] += 1
    return summary


# --- Go Nuts Phase Prompts ---

_GN_PHASE1_PROMPT = f"""I'm putting you in Go Nuts Mode. You are autonomous — the user is not present. Your job is to BUILD FEATURES and IMPROVE this project as aggressively as possible.

IMPORTANT: Detailed skill instructions are available as files on disk at:
  {_GO_NUTS_SKILLS_DIR}/
Each subdirectory contains a SKILL.md with specific tasks and guidance. You MUST read the relevant SKILL.md file before executing any skill.

PHASE 1: Discover the Project

Read {_GO_NUTS_SKILLS_DIR}/01-project-discovery/SKILL.md and execute ALL tasks described in it.

Key objectives:
1. Map every route, endpoint, model, dependency, and feature
2. Understand the product vision — who uses this, what does "done" look like?
3. Assess maturity level (skeleton/prototype/MVP/early product/established)
4. Map constraints — what can and can't we build?
5. Write the product profile to /tmp/<SESSION_NAME>-go-nuts-product-profile.md (replace <SESSION_NAME> with the session name from the PROJECT ISOLATION preamble above)

Then immediately read {_GO_NUTS_SKILLS_DIR}/02-product-gap-analysis/SKILL.md and execute it:
1. Compare what exists vs what users of this product type expect
2. Identify every missing feature and gap
3. Write the gap analysis to /tmp/<SESSION_NAME>-go-nuts-gap-analysis.md (use your session name)

CRITICAL: Create a working branch: git checkout -b go-nuts/session-$(date +%Y%m%d-%H%M%S)
CRITICAL: Never commit to main/master. All work on the go-nuts branch.
CRITICAL: After EVERY feature you build, run tests + build to verify nothing broke.

When done, immediately continue to the next task without waiting."""

_GN_PHASE2_PROMPT = f"""PHASE 2: Generate Feature Backlog

Read {_GO_NUTS_SKILLS_DIR}/03-feature-ideation/SKILL.md and execute ALL tasks:
1. Use the brainstorming frameworks (What If, Adjacent Feature, Delight, Productization)
2. Research competitors if possible (use web search)
3. Score each feature on Impact/Feasibility/Independence/Novelty
4. Write the prioritized backlog to /tmp/<SESSION_NAME>-go-nuts-feature-backlog.md (use your session name)

Then pick the TOP 3 highest-priority features from the backlog and start building them.

For EACH feature:
1. Create a git checkpoint (read {_GO_NUTS_SKILLS_DIR}/20-backup-checkpoint/SKILL.md)
2. Build the complete feature — not a stub, not a placeholder, the REAL thing
3. Match existing code style, design language, and patterns
4. Handle all states: loading, empty, error, populated
5. Run tests + build after completion
6. If tests pass → commit: [go-nuts][feature] description
7. If tests fail → revert and move to next feature

When done, immediately continue to the next task without waiting."""

_GN_PHASE3_PROMPT = f"""PHASE 3: Build Features (Batch)

Continue building features from the backlog at /tmp/<SESSION_NAME>-go-nuts-feature-backlog.md (use your session name from the PROJECT ISOLATION preamble).

For each feature, use the relevant skill file:
- UI/pages → Read {_GO_NUTS_SKILLS_DIR}/07-ui-pages-components/SKILL.md
- API/backend → Read {_GO_NUTS_SKILLS_DIR}/08-api-backend/SKILL.md
- Auth/users → Read {_GO_NUTS_SKILLS_DIR}/04-auth-user-system/SKILL.md
- Navigation → Read {_GO_NUTS_SKILLS_DIR}/05-navigation-routing/SKILL.md
- Data/state → Read {_GO_NUTS_SKILLS_DIR}/06-data-state/SKILL.md
- Search → Read {_GO_NUTS_SKILLS_DIR}/09-search-filtering/SKILL.md
- Notifications → Read {_GO_NUTS_SKILLS_DIR}/10-notifications-realtime/SKILL.md
- Settings → Read {_GO_NUTS_SKILLS_DIR}/11-settings-preferences/SKILL.md
- Content pages → Read {_GO_NUTS_SKILLS_DIR}/12-content-pages/SKILL.md
- Dashboard → Read {_GO_NUTS_SKILLS_DIR}/13-dashboard-analytics/SKILL.md
- Import/Export → Read {_GO_NUTS_SKILLS_DIR}/14-import-export/SKILL.md

EXECUTION WRAPPER for every feature:
1. Checkpoint (git stash or note SHA)
2. Build the COMPLETE feature
3. Run tests + build
4. ALL GREEN → commit: [go-nuts][feature] description
5. ANY RED → full revert, log why it failed, move on

Build as many features as you can. Prioritize high-impact, high-feasibility items.

When done, immediately continue to the next task without waiting."""

_GN_PING_PROMPT = f"""You are in Go Nuts Mode. The user is not present. You are autonomous. Your mission: BUILD FEATURES and IMPROVE the project.

You just finished your previous task and are now idle. Pick the NEXT most valuable thing to do.

STEP 1: Check your backlog at /tmp/<SESSION_NAME>-go-nuts-feature-backlog.md (use your session name) — any features left to build?
STEP 2: If backlog is empty or low, generate new ideas using {_GO_NUTS_SKILLS_DIR}/03-feature-ideation/SKILL.md
STEP 3: Pick a skill and execute it.

Available skills (each has a SKILL.md with full instructions):
 1. {_GO_NUTS_SKILLS_DIR}/07-ui-pages-components/SKILL.md — Build UI Pages & Components
 2. {_GO_NUTS_SKILLS_DIR}/08-api-backend/SKILL.md — Build API & Backend Features
 3. {_GO_NUTS_SKILLS_DIR}/04-auth-user-system/SKILL.md — Auth & User System
 4. {_GO_NUTS_SKILLS_DIR}/05-navigation-routing/SKILL.md — Navigation & Routing
 5. {_GO_NUTS_SKILLS_DIR}/06-data-state/SKILL.md — Data & State Management
 6. {_GO_NUTS_SKILLS_DIR}/09-search-filtering/SKILL.md — Search & Filtering
 7. {_GO_NUTS_SKILLS_DIR}/10-notifications-realtime/SKILL.md — Notifications & Realtime
 8. {_GO_NUTS_SKILLS_DIR}/11-settings-preferences/SKILL.md — Settings & Preferences
 9. {_GO_NUTS_SKILLS_DIR}/12-content-pages/SKILL.md — Content Pages
10. {_GO_NUTS_SKILLS_DIR}/13-dashboard-analytics/SKILL.md — Dashboard & Analytics
11. {_GO_NUTS_SKILLS_DIR}/14-import-export/SKILL.md — Import & Export
12. {_GO_NUTS_SKILLS_DIR}/15-social-collaboration/SKILL.md — Social & Collaboration
13. {_GO_NUTS_SKILLS_DIR}/16-onboarding-empty-states/SKILL.md — Onboarding & Empty States
14. {_GO_NUTS_SKILLS_DIR}/17-qa-stability-audit/SKILL.md — QA & Stability Audit
15. {_GO_NUTS_SKILLS_DIR}/18-security-sweep/SKILL.md — Security Sweep
16. {_GO_NUTS_SKILLS_DIR}/19-web-research/SKILL.md — Web Research & Inspiration
17. {_GO_NUTS_SKILLS_DIR}/03-feature-ideation/SKILL.md — Feature Ideation & Brainstorming
18. {_GO_NUTS_SKILLS_DIR}/20-backup-checkpoint/SKILL.md — Backup & Checkpoint Manager

Rules:
- Work on the go-nuts branch (create one if not already on it)
- Never commit to main/master
- One feature per commit: [go-nuts][feature] description
- Run tests + build after every change — revert immediately on new failures
- READ the SKILL.md first, then execute its specific tasks
- Build COMPLETE features, not stubs or placeholders
- Every 5th cycle, run QA audit ({_GO_NUTS_SKILLS_DIR}/17-qa-stability-audit/SKILL.md) and security sweep ({_GO_NUTS_SKILLS_DIR}/18-security-sweep/SKILL.md)

Pick a skill, read its SKILL.md, and execute it now."""


async def _go_nuts_phase_discover(session_name: str, state: dict):
    """Phase 1: Discover the project and analyze gaps."""
    state["phase"] = 1
    state["phase_name"] = "Discover Project"
    state["step"] = 1
    _go_nuts_log(state, "Starting Phase 1: Project Discovery & Gap Analysis")
    await _go_nuts_send_and_wait(session_name, _GN_PHASE1_PROMPT, state,
                                  "Discover project & analyze gaps", timeout=600)


async def _go_nuts_phase_backlog(session_name: str, state: dict):
    """Phase 2: Generate feature backlog and start building."""
    state["phase"] = 2
    state["phase_name"] = "Feature Backlog"
    state["step"] = 1
    _go_nuts_log(state, "Starting Phase 2: Generate Feature Backlog & Build Top Features")
    await _go_nuts_send_and_wait(session_name, _GN_PHASE2_PROMPT, state,
                                  "Generate backlog & build top features", timeout=900)


async def _go_nuts_phase_build(session_name: str, state: dict):
    """Phase 3: Build features from backlog."""
    state["phase"] = 3
    state["phase_name"] = "Build Features"
    state["step"] = 1
    _go_nuts_log(state, "Starting Phase 3: Build Features Batch")
    await _go_nuts_send_and_wait(session_name, _GN_PHASE3_PROMPT, state,
                                  "Build features from backlog", timeout=900)


async def _go_nuts_mode_worker(session_name: str):
    """Main go-nuts-mode coroutine. Runs discovery phases then loops forever, building features."""
    log = logging.getLogger("go-nuts-mode")
    state = _go_nuts_state[session_name]
    log.info(f"Go Nuts mode started for session '{session_name}'")
    try:
        # --- Initial setup phases (run once, errors skip to continuous loop) ---
        try:
            await _go_nuts_phase_discover(session_name, state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _go_nuts_log(state, f"Phase 1 error (skipping): {e}")
            log.error(f"Go Nuts phase 1 error for '{session_name}': {e}")

        if not state.get("enabled"):
            return

        try:
            await _go_nuts_phase_backlog(session_name, state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _go_nuts_log(state, f"Phase 2 error (skipping): {e}")
            log.error(f"Go Nuts phase 2 error for '{session_name}': {e}")

        if not state.get("enabled"):
            return

        try:
            await _go_nuts_phase_build(session_name, state)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            _go_nuts_log(state, f"Phase 3 error (skipping): {e}")
            log.error(f"Go Nuts phase 3 error for '{session_name}': {e}")

        if not state.get("enabled"):
            return

        # --- Continuous loop: monitor idle, ping with next feature task ---
        # This loop NEVER exits unless cancelled or disabled by user.
        state["phase"] = 4
        state["phase_name"] = "Continuous Build"
        cycle = 1
        consecutive_errors = 0
        while state.get("enabled"):
            try:
                _go_nuts_log(state, f"Monitoring for idle (cycle {cycle})...")
                idle_since = None
                while state.get("enabled"):
                    await asyncio.sleep(10)
                    try:
                        activity = await async_detect_activity(session_name)
                    except Exception:
                        activity = {"status": "unknown"}
                    if activity["status"] == "idle":
                        if idle_since is None:
                            idle_since = time.time()
                        elif time.time() - idle_since >= 90:
                            break
                    else:
                        idle_since = None

                if not state.get("enabled"):
                    return

                # Ensure Claude Code is running before sending prompt (OOM recovery)
                claude_ok = await _ensure_claude_running(session_name, _go_nuts_log, state)
                if not claude_ok:
                    _go_nuts_log(state, "Claude Code dead and couldn't restart — stopping go nuts mode")
                    state["enabled"] = False
                    _save_autonomous_state()
                    return

                state["step"] = cycle
                state["step_name"] = f"Build cycle {cycle}"
                _go_nuts_log(state, f"Session idle for 90s — sending build ping (cycle {cycle})")
                await _go_nuts_send_and_wait(session_name, _GN_PING_PROMPT, state,
                                              f"Build cycle {cycle}", timeout=900)
                cycle += 1
                consecutive_errors = 0
                _save_autonomous_state()  # Periodic save after each successful cycle
                await asyncio.sleep(5)

            except asyncio.CancelledError:
                raise
            except Exception as e:
                consecutive_errors += 1
                _go_nuts_log(state, f"Cycle {cycle} error ({consecutive_errors}): {e}")
                log.error(f"Go Nuts cycle {cycle} error for '{session_name}': {e}")
                if consecutive_errors >= 5:
                    _go_nuts_log(state, "Too many consecutive errors, pausing 5 minutes...")
                    await asyncio.sleep(300)
                    consecutive_errors = 0
                else:
                    await asyncio.sleep(30)

    except asyncio.CancelledError:
        if _shutting_down:
            _go_nuts_log(state, "Go Nuts mode cancelled (server shutdown — will restore)")
        else:
            _go_nuts_log(state, "Go Nuts mode cancelled by user")
            state["enabled"] = False
            _save_autonomous_state()
        log.info(f"Go Nuts mode cancelled for '{session_name}'")
        raise
    except Exception as e:
        _go_nuts_log(state, f"Go Nuts mode fatal error: {e}")
        log.error(f"Go Nuts mode fatal error for '{session_name}': {e}")
        _save_autonomous_state()  # Save state so watchdog can recover
        # Don't set enabled=False — let watchdog zombie detection restart us
    finally:
        state["task"] = None
        log.info(f"Go Nuts mode finished for '{session_name}'")


@app.post("/api/sessions/{session_name}/go-nuts-mode")
async def api_go_nuts_mode_toggle(session_name: str, body: GoNutsModeBody):
    """Toggle go-nuts mode on or off for a session."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    if body.enabled:
        # Don't allow both away mode and go-nuts mode at the same time on same session
        if _away_mode_state.get(session_name, {}).get("enabled"):
            return JSONResponse({"error": "Away Mode is active on this session. Disable it first."}, status_code=409)

        if _go_nuts_state.get(session_name, {}).get("enabled"):
            return JSONResponse(_go_nuts_state_summary(_go_nuts_state[session_name]))

        state = {
            "enabled": True,
            "phase": 0,
            "phase_name": "Initializing",
            "step": 0,
            "step_name": "",
            "started_at": time.time(),
            "log": [],
            "report": "",
            "task": None,
        }
        _go_nuts_state[session_name] = state
        _go_nuts_log(state, "Go Nuts mode enabled")
        task = asyncio.create_task(_go_nuts_mode_worker(session_name))
        state["task"] = task
        _save_autonomous_state()
        return JSONResponse(_go_nuts_state_summary(state))
    else:
        state = _go_nuts_state.get(session_name, {})
        if state.get("task") and not state["task"].done():
            state["task"].cancel()
        state["enabled"] = False
        state["task"] = None
        _go_nuts_log(state, "Go Nuts mode disabled by user")
        _save_autonomous_state()
        return JSONResponse(_go_nuts_state_summary(state))


@app.get("/api/sessions/{session_name}/go-nuts-mode")
async def api_go_nuts_mode_status(session_name: str):
    """Get current go-nuts-mode state for a session."""
    state = _go_nuts_state.get(session_name, {})
    return JSONResponse(_go_nuts_state_summary(state))


HTML_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>tmux Dashboard</title>
<link rel="icon" id="favicon" type="image/svg+xml" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='7' fill='%236e7681'/></svg>">
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh;display:flex;flex-direction:column}

/* Nav wrapper — keeps right-side items pinned while tabs scroll */
.nav-wrapper{background:#161b22;border-bottom:1px solid #30363d;display:flex;align-items:center;flex-shrink:0}
/* Nav bar — scrollable session tabs area */
.top-nav{padding:0 0 0 24px;display:flex;align-items:center;gap:0;overflow-x:auto;flex:1;min-width:0}
.top-nav::-webkit-scrollbar{height:0}
/* Pinned right section */
.nav-right{display:flex;align-items:center;flex-shrink:0;padding-right:24px}
.nav-brand{font-size:.85rem;font-weight:700;color:#58a6ff;padding:12px 16px 12px 0;border-right:1px solid #30363d;margin-right:4px;white-space:nowrap;user-select:none}
.nav-item{display:flex;align-items:center;gap:8px;padding:10px 16px;cursor:pointer;border-bottom:2px solid transparent;transition:background .15s,border-color .15s;white-space:nowrap;user-select:none}
.nav-item:hover{background:#1c2128}
.nav-item.active{border-bottom-color:#58a6ff;background:#1c2128}
.nav-session-id{font-size:.75rem;font-weight:700;color:#8b949e;background:#21262d;padding:1px 6px;border-radius:4px;min-width:20px;text-align:center}
.nav-item.active .nav-session-id{color:#58a6ff;background:#1c2333}
.nav-title{font-size:.8rem;color:#c9d1d9;max-width:180px;overflow:hidden;text-overflow:ellipsis}
.nav-indicators{display:flex;align-items:center;gap:5px}
.nav-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0;transition:all .3s ease}
.nav-dot.busy{width:10px;height:10px;background:#f85149;animation:pulse-glow 1.5s ease-in-out infinite;box-shadow:0 0 6px #f8514988}
.nav-dot.idle{background:#3fb950}
.nav-dot.unknown{background:#d2a8ff}
.nav-attached{font-size:.6rem;padding:0 5px;border-radius:8px;font-weight:600;line-height:1.5}
.nav-attached.yes{background:#238636;color:#fff}
.nav-attached.no{background:#6e768155;color:#8b949e}
.nav-spacer{flex:1}
.nav-server-stats{font-size:.7rem;color:#8b949e;white-space:nowrap;padding-right:10px;display:flex;align-items:center;gap:8px;border-right:1px solid #30363d;margin-right:10px;padding:4px 10px 4px 0;transition:color .15s}
.nav-server-stats:hover{color:#c9d1d9}
.nav-server-stats .stat-val{color:#c9d1d9;font-weight:600}
.nav-server-stats .stat-val.warn{color:#d29922}
.nav-server-stats .stat-val.crit{color:#f85149}
.nav-status-text{font-size:.75rem;color:#6e7681;white-space:nowrap;padding-right:12px}
.nav-refresh-btn{background:#1f6feb;color:#fff;border:none;padding:6px 16px;border-radius:6px;cursor:pointer;font-size:.8rem;font-weight:500;white-space:nowrap;flex-shrink:0}
.nav-refresh-btn:hover{background:#388bfd}
.nav-new-btn{background:#238636;color:#fff;border:none;width:32px;height:32px;border-radius:6px;cursor:pointer;font-size:1.2rem;font-weight:700;line-height:1;flex-shrink:0;display:flex;align-items:center;justify-content:center;margin-right:8px}
.nav-new-btn:hover{background:#2ea043}

/* Main */
.main{flex:1;display:flex;flex-direction:column;padding:16px 24px;max-width:1200px;width:100%;margin:0 auto}

/* Detail badges (inline in tab bar) */
.detail-badges{display:flex;gap:8px;align-items:center;flex-shrink:0;margin-left:auto;padding-right:4px}
.status-pill{font-size:.75rem;padding:3px 12px;border-radius:12px;font-weight:600;display:flex;align-items:center;gap:5px;transition:all .3s ease}
.status-pill.busy{background:#f8514930;color:#f85149;border:2px solid #f8514966;font-size:.85rem;padding:5px 16px;animation:pulse-glow 1.5s ease-in-out infinite}
.status-pill.idle{background:#3fb95022;color:#3fb950;border:1px solid #3fb95044}
.status-pill.unknown{background:#d2a8ff22;color:#d2a8ff;border:1px solid #d2a8ff44}
.status-dot{width:7px;height:7px;border-radius:50%;display:inline-block}
.status-pill.busy .status-dot{background:#f85149;animation:pulse-glow 1.5s ease-in-out infinite}
.status-pill.idle .status-dot{background:#3fb950}
.status-pill.unknown .status-dot{background:#d2a8ff}
.badge{font-size:.7rem;padding:2px 8px;border-radius:12px;font-weight:500}
.badge.attached{background:#238636;color:#fff}
.badge.detached{background:#6e7681;color:#fff}
.badge.model-badge{background:#30363d;color:#c9d1d9;font-size:.65rem;font-weight:500}
.btn-danger{background:#21262d;color:#f85149;border:1px solid #f8514944}
.btn-danger:hover{background:#3d1214}

/* Tabs */
.tab-bar{display:flex;border-bottom:1px solid #21262d}
.tab{padding:10px 20px;font-size:.85rem;font-weight:500;color:#8b949e;cursor:pointer;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;user-select:none}
.tab:hover{color:#c9d1d9}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
/* Tab-more dropdown (Chat/Skills/Info) */
.tab-more-wrap{position:relative}
.tab-more-trigger{display:flex;align-items:center;gap:4px}
.tab-more-menu{display:none;position:absolute;top:100%;left:0;background:#161b22;border:1px solid #30363d;border-radius:8px;min-width:120px;padding:4px 0;z-index:100;box-shadow:0 8px 24px rgba(0,0,0,.4)}
.tab-more-menu.open{display:block}
.tab-more-item{padding:8px 16px;font-size:.85rem;color:#8b949e;cursor:pointer;transition:background .15s,color .15s}
.tab-more-item:hover{background:#1c2128;color:#c9d1d9}
.tab-more-item.active{color:#58a6ff}
.tab-content{display:none}
.tab-content.active{display:flex;flex-direction:column;flex:1;min-height:0}

/* Chat tab */
.chat-wrap{display:flex;flex-direction:column;flex:1;min-height:0}
.chat-messages{flex:1;overflow-y:auto;padding:16px 0;display:flex;flex-direction:column;gap:12px;min-height:120px;max-height:calc(100vh - 300px)}
.chat-messages::-webkit-scrollbar{width:6px}
.chat-messages::-webkit-scrollbar-track{background:transparent}
.chat-messages::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
.chat-msg{max-width:85%;padding:12px 16px;border-radius:12px;font-size:1.05rem;line-height:1.6;position:relative}
.chat-msg.user{align-self:flex-end;background:#1f6feb;color:#fff;border-bottom-right-radius:4px}
.chat-msg.assistant{align-self:flex-start;background:#161b22;border:1px solid #30363d;color:#c9d1d9;border-bottom-left-radius:4px}
.chat-meta{font-size:.7rem;color:#6e7681;margin-top:4px}
.chat-msg.user .chat-meta{text-align:right;color:#ffffffaa}
.chat-typing{align-self:flex-start;padding:16px 24px;background:#f8514918;border:2px solid #f8514955;border-radius:12px;border-bottom-left-radius:4px;color:#f85149;font-size:1.15rem;font-weight:600;display:flex;align-items:center;gap:10px;animation:pulse-busy 2s ease-in-out infinite}
.chat-typing .typing-dot-group{display:flex;gap:4px;align-items:center}
.chat-typing .typing-dot{width:8px;height:8px;border-radius:50%;background:#f85149;animation:typing-bounce 1.4s ease-in-out infinite}
.chat-typing .typing-dot:nth-child(2){animation-delay:.2s}
.chat-typing .typing-dot:nth-child(3){animation-delay:.4s}
@keyframes typing-bounce{0%,80%,100%{opacity:.3;transform:scale(.8)}40%{opacity:1;transform:scale(1)}}
@keyframes pulse-busy{0%,100%{opacity:1;border-color:#f8514955}50%{opacity:.85;border-color:#f8514988}}

/* Command bar */
.cmd-bar{display:flex;align-items:flex-end;gap:0;margin-top:8px;background:#0d1117;border:1px solid #30363d;border-radius:6px;overflow:visible;flex-shrink:0}
.cmd-prompt{padding:12px 0 12px 14px;color:#3fb950;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:1rem;font-weight:600;user-select:none}
.cmd-input{flex:1;background:transparent;border:none;outline:none;color:#e6edf3;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:1rem;padding:12px;resize:vertical;min-height:44px;max-height:400px;line-height:1.4;overflow-y:auto}
.cmd-input.expanded{max-height:none;min-height:200px}
.cmd-input::placeholder{color:#484f58}
.cmd-btn-group{display:flex;align-items:flex-end;flex-shrink:0}
.cmd-send{border:none;border-left:1px solid #30363d;border-radius:0;padding:12px 18px;font-size:.95rem;align-self:flex-end;background:#21262d;color:#c9d1d9;cursor:pointer;transition:background .15s}
.cmd-send:hover{background:#30363d}

/* Raw tab */
.tab-raw{padding-top:16px}
.btn-stop{display:none;background:#da3633;color:#fff;border:1px solid #da3633;font-weight:600;font-size:.8rem;padding:4px 12px;letter-spacing:.03em}
.btn-stop:hover{background:#f85149;border-color:#f85149;color:#fff}
.btn-stop.visible{display:inline-block}
.chat-controls{display:flex;justify-content:flex-end;margin-bottom:4px;min-height:0}
.raw-controls{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.raw-info{color:#6e7681;font-size:.75rem;flex-shrink:0}
.raw-title{flex:1;min-width:0;color:#8b949e;font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center}
.raw-output{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;font-family:'SF Mono','Fira Code','Cascadia Code',Consolas,monospace;font-size:.8rem;line-height:1.45;color:#c9d1d9;flex:1;min-height:120px;max-height:calc(100vh - 280px);overflow-y:auto;white-space:pre;word-wrap:normal;overflow-x:auto;scroll-behavior:smooth}
.raw-output::-webkit-scrollbar{width:6px;height:6px}
.raw-output::-webkit-scrollbar-track{background:#0d1117}
.raw-output::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}
.raw-resize-handle{width:100%;height:8px;cursor:ns-resize;background:transparent;display:flex;align-items:center;justify-content:center;user-select:none;flex-shrink:0}
.raw-resize-handle:hover{background:#21262d}
.raw-resize-handle::after{content:'';width:40px;height:3px;border-radius:2px;background:#30363d}
.raw-resize-handle:hover::after{background:#484f58}

/* Terminal key bar */
.key-bar{display:none;align-items:center;gap:6px;padding:6px 8px;background:#161b22;border:1px solid #21262d;border-radius:0 0 6px 6px;flex-wrap:wrap;border-top:none}
.key-bar.expanded{display:flex}
.key-bar-toggle{display:flex;align-items:center;justify-content:center;gap:4px;margin-top:6px;padding:4px 10px;font-size:.68rem;color:#8b949e;background:#161b22;border:1px solid #21262d;border-radius:6px;cursor:pointer;user-select:none;transition:all .15s;width:100%}
.key-bar-toggle:hover{background:#1c2128;color:#c9d1d9;border-color:#30363d}
.key-bar-toggle.open{border-radius:6px 6px 0 0;border-bottom:none}
.key-bar-toggle .chevron{transition:transform .2s;display:inline-block;font-size:.6rem}
.key-bar-toggle.open .chevron{transform:rotate(180deg)}
.key-bar-label{font-size:.65rem;color:#6e7681;text-transform:uppercase;letter-spacing:.04em;margin-right:4px;user-select:none;white-space:nowrap}
.key-btn{display:inline-flex;align-items:center;justify-content:center;padding:4px 10px;font-size:.72rem;font-family:'SF Mono','Fira Code',Consolas,monospace;font-weight:500;color:#c9d1d9;background:#21262d;border:1px solid #30363d;border-radius:4px;cursor:pointer;transition:all .15s;user-select:none;white-space:nowrap;line-height:1.3}
.key-btn:hover{background:#30363d;color:#f0f6fc;border-color:#484f58}
.key-btn:active{background:#484f58;transform:scale(.95)}
.key-btn.key-esc{color:#f0883e;border-color:#f0883e55}
.key-btn.key-esc:hover{background:#f0883e22;color:#ffb366}
.key-btn.key-ctrlc{color:#f85149;border-color:#f8514955}
.key-btn.key-ctrlc:hover{background:#f8514922;color:#ff7b73}
.key-btn.key-slash{color:#d2a8ff;border-color:#d2a8ff44;font-size:.68rem}
.key-btn.key-slash:hover{background:#d2a8ff22;color:#f0f6fc;border-color:#d2a8ff88}
.key-bar-sep{width:1px;height:18px;background:#30363d;margin:0 2px}
.key-btn.key-toggle{font-size:.65rem;padding:3px 8px}
.key-btn.key-toggle.off{background:#da3633;border-color:#da3633;color:#fff}
.drop-zone{width:100%;margin-top:4px;padding:12px;border:2px dashed #30363d;border-radius:6px;text-align:center;color:#6e7681;font-size:.72rem;cursor:pointer;transition:all .2s;background:transparent}
.drop-zone:hover{border-color:#58a6ff;color:#8b949e;background:#58a6ff08}
.drop-zone.drag-over{border-color:#58a6ff;background:#58a6ff15;color:#58a6ff}
.drop-zone-icon{font-size:1.2rem;margin-bottom:2px;pointer-events:none}
.drop-zone-text{pointer-events:none}
.upload-progress{width:100%;margin-top:6px;display:none}
.upload-progress.active{display:block}
.upload-progress-filename{font-size:.68rem;color:#8b949e;margin-bottom:3px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.upload-progress-bar{width:100%;height:4px;background:#21262d;border-radius:2px;overflow:hidden}
.upload-progress-fill{height:100%;width:0;background:#58a6ff;border-radius:2px;transition:width .15s}
.upload-progress-fill.done{background:#3fb950}
.upload-progress-fill.error{background:#f85149}

/* Info tab */
.tab-info{padding-top:20px}
.tier{margin-bottom:18px}
.tier:last-of-type{margin-bottom:0}
.tier-label{font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;margin-bottom:4px;display:flex;align-items:center;gap:6px}
.tier-label .dot{width:6px;height:6px;border-radius:50%;display:inline-block}
.tier-description .tier-label{color:#8b949e}
.tier-description .dot{background:#8b949e}
.tier-description .tier-text{color:#c9d1d9;font-weight:500}
.tier-progress .tier-label{color:#d2a8ff}
.tier-progress .dot{background:#d2a8ff}
.tier-notes .tier-label{color:#e3b341}
.tier-notes .dot{background:#e3b341}
.tier-notes .tier-text{white-space:pre-wrap;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.85rem;line-height:1.5;max-height:300px;overflow-y:auto}
.tier-text{color:#b1bac4;line-height:1.6;font-size:1.05rem}
.tier-text.loading{color:#6e7681;font-style:italic}

/* Stats panel */
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:8px 16px;margin-top:8px}
.stat-item{display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:.85rem}
.stat-label{color:#8b949e}
.stat-value{color:#e6edf3;font-family:'SF Mono','Fira Code',Consolas,monospace;font-weight:500}
.stat-value.cost{color:#3fb950}
.rate-bar{display:flex;align-items:center;gap:8px;margin-top:8px}
.rate-bar-track{flex:1;height:6px;background:#21262d;border-radius:3px;overflow:hidden}
.rate-bar-fill{height:100%;border-radius:3px;transition:width .5s ease}
.rate-bar-fill.normal{background:#3fb950}
.rate-bar-fill.limited{background:#d29922}
.rate-bar-fill.severely_limited{background:#f85149}
.rate-label{font-size:.75rem;color:#8b949e;min-width:60px;text-align:right}
.rate-badge{display:inline-block;padding:2px 8px;border-radius:10px;font-size:.7rem;font-weight:600;text-transform:uppercase;letter-spacing:.03em}
.rate-badge.normal{background:rgba(63,185,80,.15);color:#3fb950}
.rate-badge.limited{background:rgba(210,153,34,.15);color:#d29922}
.rate-badge.severely_limited{background:rgba(248,81,73,.15);color:#f85149}
.stats-divider{grid-column:1/-1;border-top:1px solid #21262d;margin:4px 0}
.stat-value .model-tag{font-size:.75rem;padding:1px 6px;background:#30363d;border-radius:4px;color:#c9d1d9}

/* Away mode toggle */
.away-toggle{position:relative;display:inline-block;width:44px;height:24px}
.away-toggle input{opacity:0;width:0;height:0}
.away-toggle-slider{position:absolute;cursor:pointer;inset:0;background:#21262d;border-radius:12px;transition:.3s}
.away-toggle-slider:before{content:'';position:absolute;height:18px;width:18px;left:3px;bottom:3px;background:#8b949e;border-radius:50%;transition:.3s}
.away-toggle input:checked+.away-toggle-slider{background:#238636}
.away-toggle input:checked+.away-toggle-slider:before{transform:translateX(20px);background:#fff}
.away-log{margin-top:8px;font-size:.78rem;color:#6e7681;max-height:200px;overflow-y:auto;scrollbar-width:thin}
.away-log-entry{padding:2px 0;border-bottom:1px solid #161b22}
.away-log-entry .away-ts{color:#58a6ff}
.nav-away{font-size:.6rem;padding:0 5px;border-radius:8px;font-weight:600;line-height:1.5;background:#3d1f6d;color:#d2a8ff}

/* Go Nuts mode toggle */
.gonuts-toggle{position:relative;display:inline-block;width:44px;height:24px}
.gonuts-toggle input{opacity:0;width:0;height:0}
.gonuts-toggle-slider{position:absolute;cursor:pointer;inset:0;background:#21262d;border-radius:12px;transition:.3s}
.gonuts-toggle-slider:before{content:'';position:absolute;height:18px;width:18px;left:3px;bottom:3px;background:#8b949e;border-radius:50%;transition:.3s}
.gonuts-toggle input:checked+.gonuts-toggle-slider{background:#da3633}
.gonuts-toggle input:checked+.gonuts-toggle-slider:before{transform:translateX(20px);background:#fff}
.gonuts-log{margin-top:8px;font-size:.78rem;color:#6e7681;max-height:200px;overflow-y:auto;scrollbar-width:thin}
.gonuts-log-entry{padding:2px 0;border-bottom:1px solid #161b22}
.gonuts-log-entry .gonuts-ts{color:#f0883e}
.nav-gonuts{font-size:.6rem;padding:0 5px;border-radius:8px;font-weight:600;line-height:1.5;background:#6e2a0a;color:#f0883e}

/* Footer */
.detail-footer{display:flex;justify-content:space-between;align-items:center;border-top:1px solid #21262d;padding-top:12px;margin-top:12px;flex-shrink:0}
.timestamps{display:flex;gap:16px;flex-wrap:wrap}
.ts{color:#484f58;font-size:.75rem}
.ts span{color:#6e7681}
.btn-group{display:flex;gap:8px}
.btn{background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 14px;border-radius:6px;cursor:pointer;font-size:.8rem;transition:background .15s}
.btn:hover{background:#30363d}
.btn:disabled{opacity:.5;cursor:not-allowed}
.btn-full{background:#1c2333;border-color:#388bfd44;color:#58a6ff}
.btn-full:hover{background:#253049}

/* Modal */
.modal-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:100;align-items:center;justify-content:center}
.modal-overlay.active{display:flex}
.modal{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;min-width:340px;max-width:440px}
.modal h3{color:#f0f6fc;margin-bottom:12px;font-size:1.1rem}
.modal p{color:#8b949e;font-size:.9rem;margin-bottom:16px;line-height:1.5}
.modal-input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:8px 12px;font-size:.9rem;margin-bottom:16px;outline:none}
.modal-input:focus{border-color:#58a6ff}
.modal-input::placeholder{color:#484f58}
.modal-actions{display:flex;gap:8px;justify-content:flex-end}
.modal-cancel{background:#21262d;color:#c9d1d9;border:1px solid #30363d;padding:6px 16px;border-radius:6px;cursor:pointer;font-size:.85rem}
.modal-cancel:hover{background:#30363d}
.modal-confirm-create{background:#238636;color:#fff;border:none;padding:6px 16px;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:500}
.modal-confirm-create:hover{background:#2ea043}
.modal-confirm-delete{background:#da3633;color:#fff;border:none;padding:6px 16px;border-radius:6px;cursor:pointer;font-size:.85rem;font-weight:500}
.modal-confirm-delete:hover{background:#f85149}

.spinner{display:inline-block;width:12px;height:12px;border:2px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:spin .8s linear infinite;margin-right:4px;vertical-align:middle}
@keyframes spin{to{transform:rotate(360deg)}}
.empty{text-align:center;color:#8b949e;padding:60px 20px;font-size:1.1rem}
@keyframes pulse-glow{0%,100%{opacity:1}50%{opacity:.5}}

/* Claude auth indicator */
.claude-auth{display:flex;align-items:center;gap:6px;padding:6px 12px;cursor:pointer;border-radius:6px;transition:background .15s;user-select:none;flex-shrink:0;margin-right:4px}
.claude-auth:hover{background:#1c2128}
.claude-auth-label{font-size:.75rem;color:#c9d1d9;white-space:nowrap}
.claude-auth .status-dot.idle{background:#3fb950}
.claude-auth .status-dot.busy{background:#f85149}
.claude-auth .status-dot.unknown{background:#6e7681}

/* Auth dropdown */
.auth-dropdown{display:none;position:fixed;top:46px;right:24px;z-index:100;background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px;min-width:310px;max-width:420px;box-shadow:0 8px 24px rgba(0,0,0,.5)}
.auth-dropdown.active{display:block}
.auth-title{font-size:.9rem;font-weight:600;color:#f0f6fc;margin-bottom:10px}
.auth-row{display:flex;justify-content:space-between;align-items:center;padding:5px 0;font-size:.8rem}
.auth-row-label{color:#8b949e}
.auth-row-value{color:#c9d1d9;font-weight:500}
.auth-divider{border:none;border-top:1px solid #21262d;margin:10px 0}
.auth-btn{padding:8px 14px;border-radius:6px;cursor:pointer;font-size:.8rem;font-weight:500;border:none;text-align:center;transition:all .15s;width:100%}
.auth-btn-danger{background:#f8514922;color:#f85149;border:1px solid #f8514944}
.auth-btn-danger:hover{background:#3d1214}
.auth-btn-primary{background:#1f6feb;color:#fff}
.auth-btn-primary:hover{background:#388bfd}
.auth-api-input{width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:8px 10px;font-size:.8rem;font-family:'SF Mono','Fira Code',Consolas,monospace;outline:none;margin-top:4px}
.auth-api-input:focus{border-color:#58a6ff}
.auth-api-input::placeholder{color:#484f58}
.auth-hint{font-size:.75rem;color:#6e7681;line-height:1.5;margin-top:4px}
.auth-plan-badge{display:inline-block;font-size:.65rem;font-weight:600;padding:2px 8px;border-radius:10px;text-transform:uppercase;letter-spacing:.04em}
.auth-plan-badge.max{background:#d2a8ff22;color:#d2a8ff;border:1px solid #d2a8ff44}
.auth-plan-badge.pro{background:#3fb95022;color:#3fb950;border:1px solid #3fb95044}
.auth-plan-badge.free{background:#6e768122;color:#8b949e;border:1px solid #6e768144}

/* Nav icon buttons */
.nav-icon-btn{background:none;border:none;color:#8b949e;cursor:pointer;padding:6px 8px;border-radius:6px;font-size:.85rem;transition:all .15s;flex-shrink:0;display:flex;align-items:center;gap:4px}
.nav-icon-btn:hover{background:#1c2128;color:#c9d1d9}
.nav-icon-btn .icon{font-size:1rem}
/* Nav tools dropdown */
.nav-tools-wrap{position:relative;flex-shrink:0}
.nav-tools-menu{display:none;position:absolute;top:100%;right:0;background:#161b22;border:1px solid #30363d;border-radius:8px;min-width:160px;padding:4px 0;z-index:150;box-shadow:0 8px 24px rgba(0,0,0,.4);margin-top:4px}
.nav-tools-menu.open{display:block}
.nav-tools-item{padding:8px 14px;font-size:.85rem;color:#c9d1d9;cursor:pointer;display:flex;align-items:center;gap:8px;transition:background .15s}
.nav-tools-item:hover{background:#1c2128}
.nav-tools-item .icon{font-size:.9rem}

/* Stats modal */
.stats-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:200;align-items:flex-start;justify-content:center;padding-top:60px}
.stats-overlay.active{display:flex}
.stats-panel{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:560px;max-width:calc(100vw - 32px);max-height:calc(100vh - 120px);overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5)}
.stats-panel h3{color:#f0f6fc;margin-bottom:16px;font-size:1.1rem;display:flex;justify-content:space-between;align-items:center}
.stats-section{margin-bottom:16px}
.stats-section-title{font-size:.75rem;font-weight:600;text-transform:uppercase;letter-spacing:.05em;color:#8b949e;margin-bottom:8px}
.stats-row{display:flex;justify-content:space-between;align-items:center;padding:4px 0;font-size:.85rem}
.stats-row-label{color:#8b949e}
.stats-row-value{color:#c9d1d9;font-weight:500;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.8rem}
.stats-bar{height:6px;border-radius:3px;background:#21262d;overflow:hidden;margin:4px 0}
.stats-bar-fill{height:100%;border-radius:3px;transition:width .3s}
.stats-close{background:none;border:none;color:#8b949e;cursor:pointer;font-size:1.2rem;padding:0 4px}
.stats-close:hover{color:#f0f6fc}
.stats-usage-table{width:100%;border-collapse:collapse;font-size:.75rem;margin-top:4px}
.stats-usage-table th{color:#8b949e;font-weight:600;text-align:right;padding:3px 6px;border-bottom:1px solid #21262d;white-space:nowrap}
.stats-usage-table th:first-child{text-align:left}
.stats-usage-table td{padding:3px 6px;color:#c9d1d9;text-align:right;white-space:nowrap;font-family:'SF Mono','Fira Code',Consolas,monospace}
.stats-usage-table td:first-child{text-align:left;font-family:inherit;color:#f0f6fc}
.stats-usage-table tr.stats-totals-row{border-top:1px solid #30363d;font-weight:600}
.stats-usage-table tr.stats-totals-row td{color:#f0f6fc;padding-top:6px}
.stats-usage-table .model-tag{font-size:.65rem;padding:1px 5px;background:#30363d;border-radius:3px;color:#c9d1d9;display:inline-block}

/* CLAUDE.md editor modal */
/* Skills tab */
.skills-panel{display:flex;flex-direction:column;height:100%;gap:8px;padding:12px 0}
.skills-toolbar{display:flex;gap:8px;padding:0 0 8px 0}
.skills-list{flex:0 0 auto;max-height:200px;overflow-y:auto}
.skills-file{display:flex;justify-content:space-between;align-items:center;padding:6px 8px;border-bottom:1px solid #21262d}
.skills-file:hover{background:#161b22}
.skills-file-name{color:#58a6ff;cursor:pointer;font-size:.85rem}
.skills-file-meta{font-size:.7rem;color:#6e7681;margin-left:8px}
.skills-file-actions{display:flex;gap:6px}
.skills-editor-wrap{flex:1;display:flex;flex-direction:column;min-height:0}
.skills-editor-header{display:flex;gap:8px;align-items:center;margin-bottom:8px}
.skills-editor-header input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:6px 10px;font-size:.85rem;font-family:monospace}
.skills-editor{flex:1;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:12px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.85rem;resize:none;min-height:200px;line-height:1.5;outline:none}
.skills-editor:focus{border-color:#58a6ff}
.skills-empty{color:#6e7681;font-size:.85rem;padding:16px 0;text-align:center}
/* Skills library overlay */
.skills-library-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:200;align-items:flex-start;justify-content:center;padding-top:60px}
.skills-library-overlay.active{display:flex}
.skills-library-panel{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:500px;max-width:calc(100vw - 32px);max-height:calc(100vh - 120px);overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5)}
.skills-library-panel h3{color:#f0f6fc;margin-bottom:16px;font-size:1.1rem;display:flex;justify-content:space-between;align-items:center}
.skills-library-set{padding:10px 12px;border:1px solid #21262d;border-radius:8px;margin-bottom:8px;cursor:pointer;transition:background .15s}
.skills-library-set:hover{background:#21262d}
.skills-library-set-name{color:#58a6ff;font-weight:500;font-size:.9rem}
.skills-library-set-meta{font-size:.72rem;color:#6e7681;margin-top:2px}
.skills-library-actions{margin-top:16px;display:flex;gap:8px;justify-content:flex-end}

.claudemd-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:200;align-items:flex-start;justify-content:center;padding-top:40px}
.claudemd-overlay.active{display:flex}
.claudemd-panel{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:700px;max-width:calc(100vw - 32px);max-height:calc(100vh - 80px);overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);display:flex;flex-direction:column}
.claudemd-panel h3{color:#f0f6fc;margin-bottom:12px;font-size:1.1rem;display:flex;justify-content:space-between;align-items:center}
.claudemd-editor{width:100%;min-height:350px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:12px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.85rem;line-height:1.5;resize:vertical;outline:none}
.claudemd-editor:focus{border-color:#58a6ff}
.claudemd-path{font-size:.7rem;color:#6e7681;margin-bottom:8px;font-family:'SF Mono','Fira Code',Consolas,monospace}
.claudemd-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}

/* Mobile */
@media(max-width:768px){
  .top-nav{padding:0 0 0 8px}
  .nav-right{padding-right:8px}
  .nav-brand{padding:10px 8px 10px 0;margin-right:2px;font-size:.75rem}
  .nav-item{padding:8px 10px;gap:5px}
  .nav-title{display:none}
  .nav-attached{display:none}
  .nav-server-stats{display:none}
  .nav-status-text{display:none}
  .claude-auth-label{display:none}
  .claude-auth{padding:8px 10px}
  .claude-auth .status-dot{width:10px;height:10px}
  .auth-dropdown{right:8px;min-width:270px;max-width:calc(100vw - 16px)}
  .nav-refresh-btn{padding:5px 10px;font-size:.75rem}
  .nav-new-btn{width:28px;height:28px;font-size:1rem;margin-right:4px}
  .main{padding:12px}
  .detail-header{flex-direction:column;align-items:flex-start;gap:8px}
  .detail-title-text{font-size:1.1rem}
  .detail-badges{flex-wrap:wrap}
  .chat-msg{max-width:92%}
  .chat-messages{max-height:calc(100vh - 320px);min-height:80px}
  .raw-output{max-height:50vh}
  .modal{min-width:280px;margin:0 16px}
  .tab{padding:8px 12px;font-size:.8rem}
  .tab-more-menu{min-width:100px}
}
</style></head>
<body>
<div class="nav-wrapper">
<nav class="top-nav" id="top-nav">
  <span class="nav-brand">tmux</span>
  <button class="nav-new-btn" onclick="showCreateModal()" title="New session">+</button>
  <span class="nav-spacer"></span>
</nav>
<div class="nav-right">
  <span class="nav-server-stats" id="nav-server-stats" title="Click for details" onclick="openStats()" style="cursor:pointer"></span>
  <span class="nav-status-text" id="status-info">Watching for changes...</span>
  <div class="nav-tools-wrap">
    <button class="nav-icon-btn" onclick="toggleToolsMenu(event)" title="Tools"><span class="icon">&#x2699;</span></button>
    <div class="nav-tools-menu" id="nav-tools-menu">
      <div class="nav-tools-item" onclick="openStats();closeToolsMenu()"><span class="icon">&#x1F4CA;</span> System Stats</div>
      <div class="nav-tools-item" onclick="openClaudeMd();closeToolsMenu()"><span class="icon">&#x1F4DD;</span> CLAUDE.md</div>
    </div>
  </div>
  <div class="claude-auth" id="claude-auth" onclick="toggleAuthPanel(event)">
    <span class="status-dot unknown" id="claude-auth-dot"></span>
    <span class="claude-auth-label" id="claude-auth-label">...</span>
  </div>
</div>
</div>
<div class="auth-dropdown" id="auth-dropdown">
  <div id="auth-dropdown-content"></div>
</div>
<div class="main" id="main"></div>
<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal-content"></div>
</div>
<!-- Stats overlay -->
<div class="stats-overlay" id="stats-overlay" onclick="if(event.target===this)closeStats()">
  <div class="stats-panel" id="stats-panel">
    <h3>System Stats <button class="stats-close" onclick="closeStats()">&times;</button></h3>
    <div id="stats-content">Loading...</div>
  </div>
</div>
<!-- CLAUDE.md editor overlay -->
<div class="claudemd-overlay" id="claudemd-overlay" onclick="if(event.target===this)closeClaudeMd()">
  <div class="claudemd-panel" id="claudemd-panel">
    <h3>CLAUDE.md (Global) <button class="stats-close" onclick="closeClaudeMd()">&times;</button></h3>
    <div class="claudemd-path" id="claudemd-path"></div>
    <textarea class="claudemd-editor" id="claudemd-editor" spellcheck="false"></textarea>
    <div class="claudemd-actions">
      <button class="btn" onclick="closeClaudeMd()">Cancel</button>
      <button class="btn btn-full" onclick="saveClaudeMd()">Save</button>
    </div>
  </div>
</div>
<!-- Skills library overlay -->
<div class="skills-library-overlay" id="skills-library-overlay" onclick="if(event.target===this)closeSkillLibrary()">
  <div class="skills-library-panel">
    <h3>Skill Library <button class="stats-close" onclick="closeSkillLibrary()">&times;</button></h3>
    <div id="skills-library-list"></div>
    <div class="skills-library-actions">
      <button class="btn" onclick="closeSkillLibrary()">Cancel</button>
      <button class="btn btn-full" onclick="saveCurrentToLibrary()">Save Current Set</button>
    </div>
  </div>
</div>

<script>
const navEl=document.getElementById('top-nav');
const mainEl=document.getElementById('main');
const statusInfoEl=document.getElementById('status-info');
const BASE='__ROOT_PATH__';
let sessions=[];
let selectedSession=null;
let pollTimer=null;
const activeTabs={};
const rawState={};
function getRawState(n){if(!rawState[n])rawState[n]={polling:false,timer:null,knownLines:0,userScrolledUp:false};return rawState[n]}
const lastStatus={};
// Local chat messages mirror (kept in sync with server)
const chatMessages={};
// Preserve textarea drafts across re-renders
const draftText={};

function saveDrafts(){
  ['chat','raw'].forEach(tab=>{
    sessions.forEach(s=>{
      const el=document.getElementById('cmd-'+tab+'-'+s.name);
      if(el&&el.value)draftText[tab+'-'+s.name]=el.value;
      else delete draftText[tab+'-'+s.name];
    });
  });
}
function restoreDrafts(){
  ['chat','raw'].forEach(tab=>{
    sessions.forEach(s=>{
      const key=tab+'-'+s.name;
      const el=document.getElementById('cmd-'+tab+'-'+s.name);
      if(el&&draftText[key]){el.value=draftText[key];autoGrow(el)}
    });
  });
}

function updateFavicon(status){
  const colors={busy:'%23f85149',idle:'%233fb950',unknown:'%236e7681'};
  const c=colors[status]||colors.unknown;
  const svg="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 16 16'><circle cx='8' cy='8' r='7' fill='"+c+"'/></svg>";
  const link=document.getElementById('favicon');
  if(link)link.href=svg;
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
  const d=new Date(ts*1000);
  return d.toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
}
function esc(str){
  if(!str)return'';
  const d=document.createElement('div');
  d.textContent=str;
  return d.innerHTML;
}
function formatModelName(model){
  if(!model)return'';
  // Strip claude- prefix
  let m=model.replace(/^claude-/,'');
  // Strip date suffix like -20251001
  m=m.replace(/-\d{8}$/,'');
  // Extract family and version: e.g. "sonnet-4-6" -> "sonnet 4.6"
  const parts=m.match(/^([a-z]+)-(.+)$/);
  if(parts){
    const family=parts[1];
    const ver=parts[2].replace(/-/g,'.');
    return family+' '+ver;
  }
  return m;
}
function statusLabel(s){
  if(s==='busy')return'Working...';
  if(s==='idle')return'Idle';
  return'...';
}

function renderNav(){
  navEl.querySelectorAll('.nav-item').forEach(el=>el.remove());
  const brand=navEl.querySelector('.nav-brand');
  sessions.forEach(s=>{
    const item=document.createElement('div');
    item.className='nav-item'+(s.name===selectedSession?' active':'');
    item.id='nav-'+s.name;
    item.onclick=()=>selectSession(s.name);
    item.innerHTML=`
      <span class="nav-session-id">${esc(s.name)}</span>
      <span class="nav-indicators">
        <span class="nav-dot ${esc(s.activity_status)}" id="nav-dot-${s.name}"></span>
        <span class="nav-attached ${s.attached?'yes':'no'}">${s.attached?'A':'D'}</span>
        ${s.away_mode?'<span class="nav-away">AW</span>':''}
        ${s.go_nuts_mode?'<span class="nav-gonuts">GN</span>':''}
      </span>`;
    brand.after(item);
  });
  const items=Array.from(navEl.querySelectorAll('.nav-item'));
  items.reverse().forEach(item=>brand.after(item));
}

function renderChatBubbles(name){
  const msgs=chatMessages[name]||[];
  return msgs.map(m=>`
    <div class="chat-msg ${m.role}">
      ${esc(m.text)}
      <div class="chat-meta">${fmtTime(m.ts)}</div>
    </div>`).join('');
}

function renderDetail(){
  saveDrafts();
  const s=sessions.find(x=>x.name===selectedSession);
  if(!s){mainEl.innerHTML='<div class="empty">No session selected</div>';return}
  const tab=activeTabs[s.name]||'raw';
  // Sync server messages into local store (merge, don't replace — preserves
  // messages added locally from raw tab that server hasn't echoed back yet)
  if(s.messages && s.messages.length) mergeChatMessages(s.name, s.messages);
  // Update favicon to match selected session
  updateFavicon(s.activity_status);

  mainEl.innerHTML=`
    <div class="tab-bar">
      <div class="tab ${tab==='raw'?'active':''}" onclick="switchTab('${s.name}','raw')">Terminal</div>
      <div class="tab-more-wrap">
        <div class="tab tab-more-trigger ${['chat','skills','info'].includes(tab)?'active':''}" onclick="toggleTabMore(event)">${{'chat':'Chat','skills':'Skills','info':'Info'}[tab]||'More'} &#9662;</div>
        <div class="tab-more-menu" id="tab-more-menu">
          <div class="tab-more-item ${tab==='chat'?'active':''}" onclick="switchTab('${s.name}','chat');closeTabMore()">Chat</div>
          <div class="tab-more-item ${tab==='skills'?'active':''}" onclick="switchTab('${s.name}','skills');closeTabMore()">Skills</div>
          <div class="tab-more-item ${tab==='info'?'active':''}" onclick="switchTab('${s.name}','info');closeTabMore()">Info</div>
        </div>
      </div>
      <div class="detail-badges">
        <span class="status-pill ${esc(s.activity_status)}" id="status-${s.name}">
          <span class="status-dot"></span>
          <span class="status-label">${statusLabel(s.activity_status)}</span>
          ${s.activity_detail&&s.activity_status!=='busy'?'<span style="font-weight:400;opacity:.7"> &middot; '+esc(s.activity_detail)+'</span>':''}
        </span>
        ${s.model?'<span class="badge model-badge" id="model-badge-'+s.name+'">'+formatModelName(s.model)+'</span>':''}
        ${s.attached?'<span class="badge attached">attached</span>':''}
        <button class="btn btn-danger" onclick="showDeleteModal('${esc(s.name)}')" title="Kill session">Delete</button>
      </div>
    </div>

    <div class="tab-content ${tab==='chat'?'active':''}" id="tab-chat-${s.name}">
      <div class="chat-wrap">
        <div class="chat-controls">
          <button class="btn btn-stop ${s.activity_status==='busy'?'visible':''}" id="interrupt-chat-${s.name}" onclick="interruptSession('${s.name}')" title="Interrupt Claude (Esc)">Stop</button>
        </div>
        <div class="chat-messages" id="chat-${s.name}">
          ${renderChatBubbles(s.name)}
          ${s.activity_status==='busy'?'<div class="chat-typing"><span class="typing-dot-group"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span> Working...</div>':''}
        </div>
        <div class="cmd-bar" style="position:relative">
          <span class="cmd-prompt">&gt;</span>
          <textarea class="cmd-input" id="cmd-chat-${s.name}" rows="1"
            placeholder="Send a message..."
            onkeydown="handleChatKey(event,'${s.name}')"
            oninput="autoGrow(this)"
            autocomplete="off" spellcheck="true" lang="en"></textarea>
          <button class="btn cmd-send" onclick="sendChat('${s.name}')">Send</button>
          <input type="file" id="upload-${s.name}" style="display:none" onchange="uploadFile('${s.name}',this)" multiple>
        </div>
        ${buildKeyBar(s.name,'chat')}
      </div>
    </div>

    <div class="tab-content tab-raw ${tab==='raw'?'active':''}" id="tab-raw-${s.name}">
      <div class="raw-controls">
        <span class="raw-info" id="raw-info-${s.name}">Loading terminal...</span>
        <span class="raw-title" id="raw-title-${s.name}">${esc(s.title)||''}</span>
        <button class="btn btn-stop ${s.activity_status==='busy'?'visible':''}" id="interrupt-raw-${s.name}" onclick="interruptSession('${s.name}')" title="Interrupt Claude (Esc)">Stop</button>
        <button class="btn" onclick="loadRaw('${s.name}')">Reload</button>
      </div>
      <div class="raw-output" id="raw-${s.name}" style="${getTerminalHeight()}">Loading terminal output...</div>
      <div class="raw-resize-handle" onmousedown="startResize(event,'${s.name}')"></div>
      <div class="cmd-bar" style="position:relative">
        <span class="cmd-prompt">$</span>
        <textarea class="cmd-input" id="cmd-raw-${s.name}" rows="1"
          placeholder="Type a command and press Enter..."
          onkeydown="handleRawKey(event,'${s.name}')"
          oninput="autoGrow(this)"
          autocomplete="off" spellcheck="true" lang="en"></textarea>
        <button class="btn cmd-send" onclick="sendCmd('${s.name}','raw')">Send</button>
        <input type="file" id="upload-raw-${s.name}" style="display:none" onchange="uploadFile('${s.name}',this)" multiple>
      </div>
      ${buildKeyBar(s.name,'raw')}
    </div>

    <div class="tab-content ${tab==='skills'?'active':''}" id="tab-skills-${s.name}">
      <div class="skills-panel">
        <div class="skills-toolbar">
          <button class="btn" onclick="newSkillFile('${s.name}')">+ New File</button>
          <button class="btn" onclick="openSkillLibrary('${s.name}')">Library</button>
        </div>
        <div class="skills-list" id="skills-list-${s.name}"></div>
        <div class="skills-editor-wrap" id="skills-editor-wrap-${s.name}" style="display:none">
          <div class="skills-editor-header">
            <input id="skills-filename-${s.name}" placeholder="filename.md">
            <button class="btn" onclick="saveSkillFile('${s.name}')">Save</button>
            <button class="btn" onclick="closeSkillEditor('${s.name}')">Cancel</button>
          </div>
          <textarea class="skills-editor" id="skills-editor-${s.name}"></textarea>
        </div>
      </div>
    </div>

    <div class="tab-content tab-info ${tab==='info'?'active':''}" id="tab-info-${s.name}">
      <div class="tier tier-description">
        <div class="tier-label"><span class="dot"></span>Project</div>
        <div class="tier-text" id="desc-${s.name}">${esc(s.description)||'Loading...'}</div>
      </div>
      <div class="tier tier-progress">
        <div class="tier-label"><span class="dot"></span>Progress</div>
        <div class="tier-text" id="prog-${s.name}">${esc(s.progress)||'Loading...'}</div>
      </div>
      <div class="tier tier-notes">
        <div class="tier-label"><span class="dot"></span>Key Info</div>
        <div class="tier-text" id="notes-${s.name}">${esc(s.notes)||'Click "Full" to extract...'}</div>
      </div>
      <div class="tier" style="margin-top:12px">
        <div class="tier-label"><span class="dot" style="background:#58a6ff"></span>Auth Mode</div>
        <div style="display:flex;align-items:center;gap:12px;margin-top:6px">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:.85rem;color:#c9d1d9">
            <input type="radio" name="auth-mode-${s.name}" value="subscription"
              onchange="setAuthMode('${s.name}','subscription')"
              ${(s.auth_mode||'subscription')==='subscription'?'checked':''}>
            Subscription
          </label>
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:.85rem;color:#c9d1d9">
            <input type="radio" name="auth-mode-${s.name}" value="api"
              onchange="setAuthMode('${s.name}','api')"
              ${s.auth_mode==='api'?'checked':''}>
            API Key
          </label>
          <span id="auth-mode-status-${s.name}" style="font-size:.72rem;color:#8b949e"></span>
        </div>
      </div>
      <div class="tier" style="margin-top:12px" id="stats-tier-${s.name}">
        <div class="tier-label"><span class="dot" style="background:#79c0ff"></span>Usage &amp; Rate</div>
        <div id="stats-panel-${s.name}" style="margin-top:6px;color:#6e7681;font-size:.85rem">Loading stats...</div>
      </div>
      <div class="tier" style="margin-top:12px" id="away-tier-${s.name}">
        <div class="tier-label"><span class="dot" style="background:#d2a8ff"></span>Away Mode</div>
        <div style="display:flex;align-items:center;gap:12px;margin-top:6px">
          <label class="away-toggle">
            <input type="checkbox" id="away-toggle-${s.name}"
              onchange="toggleAwayMode('${esc(s.name)}',this.checked)"
              ${s.away_mode?'checked':''}>
            <span class="away-toggle-slider"></span>
          </label>
          <span id="away-status-${s.name}" style="font-size:.82rem;color:#8b949e">${s.away_mode?'Running...':'Off'}</span>
        </div>
        <div class="away-log" id="away-log-${s.name}"></div>
      </div>
      <div class="tier" style="margin-top:12px" id="gonuts-tier-${s.name}">
        <div class="tier-label"><span class="dot" style="background:#f0883e"></span>Go Nuts Mode</div>
        <div style="display:flex;align-items:center;gap:12px;margin-top:6px">
          <label class="gonuts-toggle">
            <input type="checkbox" id="gonuts-toggle-${s.name}"
              onchange="toggleGoNutsMode('${esc(s.name)}',this.checked)"
              ${s.go_nuts_mode?'checked':''}>
            <span class="gonuts-toggle-slider"></span>
          </label>
          <span id="gonuts-status-${s.name}" style="font-size:.82rem;color:#8b949e">${s.go_nuts_mode?'Running...':'Off'}</span>
        </div>
        <div class="gonuts-log" id="gonuts-log-${s.name}"></div>
      </div>
      <div class="detail-footer" style="margin-top:24px">
        <div class="timestamps">
          <div class="ts">project: <span id="ts-desc-${s.name}">${timeAgo(s.description_at)}</span></div>
          <div class="ts">progress: <span id="ts-prog-${s.name}">${timeAgo(s.progress_at)}</span></div>
          <div class="ts">notes: <span id="ts-notes-${s.name}">${timeAgo(s.notes_at)}</span></div>
          <div class="ts">live: <span id="ts-rt-${s.name}">${timeAgo(s.realtime_at)}</span></div>
        </div>
        <div class="btn-group">
          <button class="btn" id="btn-${s.name}" onclick="refreshOne('${esc(s.name)}')">Update</button>
          <button class="btn btn-full" id="btn-full-${s.name}" onclick="refreshFull('${esc(s.name)}')">Full</button>
        </div>
      </div>
    </div>`;

  // Restore draft text in textareas
  restoreDrafts();
  // Scroll chat to bottom
  const chatEl=document.getElementById('chat-'+s.name);
  if(chatEl)chatEl.scrollTop=chatEl.scrollHeight;
  // Start/stop raw polling based on active tab
  stopAllRawPolling();
  stopStatsPolling();
  if(tab==='raw'){
    const rawEl=document.getElementById('raw-'+s.name);
    if(rawEl&&rawEl.textContent.startsWith('Loading'))loadRaw(s.name);
    startRawPolling(s.name);
  }
  if(tab==='info')startStatsPolling(s.name);
  if(tab==='skills')loadSkillFiles(s.name);
}

function selectSession(name){
  stopAllRawPolling();
  selectedSession=name;
  navEl.querySelectorAll('.nav-item').forEach(el=>el.classList.remove('active'));
  const navItem=document.getElementById('nav-'+name);
  if(navItem)navItem.classList.add('active');
  renderDetail();
}

function switchTab(name,tab){
  activeTabs[name]=tab;
  const allTabs=mainEl.querySelectorAll('.tab-content');
  allTabs.forEach(t=>t.classList.remove('active'));
  const tabBar=mainEl.querySelector('.tab-bar');
  if(tabBar){
    tabBar.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
    if(tab==='raw'){
      const rawTab=tabBar.querySelector('.tab');
      if(rawTab)rawTab.classList.add('active');
    }else{
      const trigger=tabBar.querySelector('.tab-more-trigger');
      if(trigger){trigger.classList.add('active');trigger.innerHTML=({'chat':'Chat','skills':'Skills','info':'Info'}[tab]||'More')+' &#9662;';}
    }
    tabBar.querySelectorAll('.tab-more-item').forEach(el=>el.classList.remove('active'));
    const items=tabBar.querySelectorAll('.tab-more-item');
    const moreIdx=['chat','skills','info'].indexOf(tab);
    if(moreIdx>=0&&items[moreIdx])items[moreIdx].classList.add('active');
  }
  const target=document.getElementById('tab-'+tab+'-'+name);
  if(target)target.classList.add('active');
  stopAllRawPolling();
  stopStatsPolling();
  stopAllAwayPolling();
  stopAllGoNutsPolling();
  if(tab==='raw')startRawPolling(name);
  if(tab==='info'){
    startStatsPolling(name);
    const s=sessions.find(x=>x.name===name);
    if(s&&s.away_mode)startAwayPolling(name);
    else loadAwayStatus(name);
    if(s&&s.go_nuts_mode)startGoNutsPolling(name);
    else loadGoNutsStatus(name);
  }
  if(tab==='skills')loadSkillFiles(name);
  if(tab==='chat'){
    // Re-render chat bubbles to pick up messages added while on other tabs
    const chatEl=document.getElementById('chat-'+name);
    if(chatEl){
      chatEl.innerHTML=renderChatBubbles(name);
      // Re-add typing indicator if busy
      const s=sessions.find(x=>x.name===name);
      if(s&&s.activity_status==='busy'){
        const typing=document.createElement('div');
        typing.className='chat-typing';
        typing.innerHTML='<span class="typing-dot-group"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span> Working...';
        chatEl.appendChild(typing);
      }
      chatEl.scrollTop=chatEl.scrollHeight;
    }
  }
}

// ── Tab-more dropdown ──
function toggleTabMore(e){
  e.stopPropagation();
  const menu=document.getElementById('tab-more-menu');
  if(menu)menu.classList.toggle('open');
}
function closeTabMore(){
  const menu=document.getElementById('tab-more-menu');
  if(menu)menu.classList.remove('open');
}

// ── Nav tools dropdown ──
function toggleToolsMenu(e){
  e.stopPropagation();
  const menu=document.getElementById('nav-tools-menu');
  if(menu)menu.classList.toggle('open');
}
function closeToolsMenu(){
  const menu=document.getElementById('nav-tools-menu');
  if(menu)menu.classList.remove('open');
}

// Close dropdowns on outside click
document.addEventListener('click',function(){closeTabMore();closeToolsMenu()});

function mergeChatMessages(name, serverMsgs){
  // Merge server messages with local messages, preserving any locally-added
  // messages (e.g. from raw tab) that the server hasn't echoed back yet.
  const local=chatMessages[name]||[];
  if(!local.length){chatMessages[name]=[...serverMsgs];return}
  // Build a set of server message signatures for dedup
  const serverSet=new Set(serverMsgs.map(m=>m.role+':'+m.text+':'+Math.floor(m.ts)));
  // Find local-only messages (user messages added via appendChatBubble that
  // the server already recorded but with slightly different timestamp)
  const localOnly=[];
  for(const m of local){
    const sig=m.role+':'+m.text+':'+Math.floor(m.ts);
    if(!serverSet.has(sig)){
      // Check if server has same role+text with close timestamp (within 5s)
      const dup=serverMsgs.some(s=>s.role===m.role&&s.text===m.text&&Math.abs(s.ts-m.ts)<5);
      if(!dup)localOnly.push(m);
    }
  }
  // Merge: server messages + any local-only messages, sorted by timestamp
  const merged=[...serverMsgs,...localOnly].sort((a,b)=>a.ts-b.ts);
  chatMessages[name]=merged;
}

function isChatAtBottom(chatEl){
  return (chatEl.scrollHeight-chatEl.scrollTop-chatEl.clientHeight)<50;
}

function appendChatBubble(name,role,text,ts){
  if(!chatMessages[name])chatMessages[name]=[];
  // Avoid duplicate assistant messages
  if(role==='assistant'){
    const msgs=chatMessages[name];
    for(let i=msgs.length-1;i>=0;i--){
      if(msgs[i].role==='assistant'){
        if(msgs[i].text===text)return;
        break;
      }
    }
  }
  chatMessages[name].push({role,text,ts});
  // If this session's chat is visible, append to DOM
  if(name===selectedSession && (activeTabs[name]||'chat')==='chat'){
    const chatEl=document.getElementById('chat-'+name);
    if(chatEl){
      const wasAtBottom=isChatAtBottom(chatEl);
      // Remove typing indicator if present
      const typing=chatEl.querySelector('.chat-typing');
      if(typing)typing.remove();
      const bubble=document.createElement('div');
      bubble.className='chat-msg '+role;
      bubble.innerHTML=esc(text)+'<div class="chat-meta">'+fmtTime(ts)+'</div>';
      chatEl.appendChild(bubble);
      // Only auto-scroll if user was at bottom or this is their own message
      if(wasAtBottom||role==='user')chatEl.scrollTop=chatEl.scrollHeight;
    }
  }
}

function autoGrow(el){
  if(el.classList.contains('expanded'))return;
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,400)+'px';
}
function handleChatKey(e,name){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendChat(name)}
}
function handleRawKey(e,name){
  if(e.key==='Enter'&&!e.shiftKey){e.preventDefault();sendCmd(name,'raw')}
}

async function sendChat(name){
  const input=document.getElementById('cmd-chat-'+name);
  if(!input)return;
  const cmd=input.value.trim();
  if(!cmd)return;
  input.disabled=true;
  // Show user bubble immediately
  appendChatBubble(name,'user',cmd,Date.now()/1000);
  // Immediately show busy state — user just sent a message so it must be working
  setOptimisticBusy(name);
  try{
    await fetch(BASE+'/api/sessions/'+name+'/send',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command:cmd})
    });
    input.value='';input.style.height='auto';
  }catch(e){alert('Failed to send.')}
  input.disabled=false;
  input.focus();
  // After a delay, verify the busy state from the actual terminal
  scheduleBusyVerification(name);
}

function setOptimisticBusy(name){
  // Update local session state
  const idx=sessions.findIndex(s=>s.name===name);
  if(idx>=0){sessions[idx].activity_status='busy';sessions[idx].activity_detail='Processing...'}
  lastStatus[name]='busy';
  // Update status pill and nav dot
  updateStatusPill(name,'busy','Processing...');
  if(name===selectedSession)updateFavicon('busy');
  // Show typing indicator in chat
  const chatEl=document.getElementById('chat-'+name);
  if(chatEl){
    const existing=chatEl.querySelector('.chat-typing');
    if(!existing){
      const typing=document.createElement('div');
      typing.className='chat-typing';
      typing.innerHTML='<span class="typing-dot-group"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span> Working...';
      chatEl.appendChild(typing);
      chatEl.scrollTop=chatEl.scrollHeight;
    }
  }
}

function scheduleBusyVerification(name){
  // First check after 5 seconds
  setTimeout(async()=>{
    try{
      const resp=await fetch(BASE+'/api/status');
      const statuses=await resp.json();
      const st=statuses.find(s=>s.name===name);
      if(!st)return;
      if(st.activity_status==='busy'){
        // Confirmed busy — update detail from server
        updateStatusPill(name,st.activity_status,st.activity_detail);
        lastStatus[name]=st.activity_status;
        return;
      }
      // Server says idle — but might be a brief gap.  Check once more after 3s.
      setTimeout(async()=>{
        try{
          const resp2=await fetch(BASE+'/api/status');
          const statuses2=await resp2.json();
          const st2=statuses2.find(s=>s.name===name);
          if(!st2)return;
          // Now accept whatever the server says
          lastStatus[name]=st2.activity_status;
          updateStatusPill(name,st2.activity_status,st2.activity_detail);
          if(name===selectedSession)updateFavicon(st2.activity_status);
          // Update typing indicator
          const chatEl=document.getElementById('chat-'+name);
          if(chatEl){
            const existing=chatEl.querySelector('.chat-typing');
            if(st2.activity_status==='busy'&&!existing){
              const typing=document.createElement('div');
              typing.className='chat-typing';
              typing.innerHTML='<span class="typing-dot-group"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span> Working...';
              chatEl.appendChild(typing);
              chatEl.scrollTop=chatEl.scrollHeight;
            }else if(st2.activity_status!=='busy'&&existing){
              existing.remove();
            }
          }
          // Also trigger a full data refresh if status changed
          if(st2.activity_status!=='busy')refreshOne(name);
        }catch(e){}
      },3000);
    }catch(e){}
  },5000);
}

// Track which tab triggered the upload so progress shows in the right key bar
let _uploadTab={};

function _formatSize(bytes){
  if(bytes<1024)return bytes+' B';
  if(bytes<1024*1024)return (bytes/1024).toFixed(1)+' KB';
  return (bytes/(1024*1024)).toFixed(1)+' MB';
}

function _uploadOneFile(name,tab,file){
  return new Promise(function(resolve){
    const progWrap=['chat','raw'].map(function(t){return document.getElementById('upload-progress-'+t+'-'+name)});
    const progName=['chat','raw'].map(function(t){return document.getElementById('upload-progress-name-'+t+'-'+name)});
    const progFill=['chat','raw'].map(function(t){return document.getElementById('upload-progress-fill-'+t+'-'+name)});
    // Show progress bars in both tabs
    progWrap.forEach(function(el){if(el)el.classList.add('active')});
    progName.forEach(function(el){if(el)el.textContent='Uploading '+file.name+'...'});
    progFill.forEach(function(el){if(el){el.style.width='0%';el.className='upload-progress-fill'}});
    const fd=new FormData();
    fd.append('file',file);
    const xhr=new XMLHttpRequest();
    xhr.upload.addEventListener('progress',function(e){
      if(e.lengthComputable){
        const pct=Math.round(e.loaded/e.total*100);
        progFill.forEach(function(el){if(el)el.style.width=pct+'%'});
        progName.forEach(function(el){if(el)el.textContent='Uploading '+file.name+' ('+pct+'%)'});
      }
    });
    xhr.addEventListener('load',function(){
      if(xhr.status>=200&&xhr.status<300){
        progFill.forEach(function(el){if(el){el.style.width='100%';el.classList.add('done')}});
        progName.forEach(function(el){if(el)el.textContent=file.name+' uploaded'});
        appendChatBubble(name,'user','Uploaded '+file.name+' ('+_formatSize(file.size)+')',Date.now()/1000);
      }else{
        let msg='Upload failed';
        try{const d=JSON.parse(xhr.responseText);if(d.error)msg+=': '+d.error}catch(e){}
        progFill.forEach(function(el){if(el){el.style.width='100%';el.classList.add('error')}});
        progName.forEach(function(el){if(el)el.textContent=msg});
        appendChatBubble(name,'assistant',msg,Date.now()/1000);
      }
      setTimeout(function(){progWrap.forEach(function(el){if(el)el.classList.remove('active')})},2000);
      resolve();
    });
    xhr.addEventListener('error',function(){
      progFill.forEach(function(el){if(el){el.style.width='100%';el.classList.add('error')}});
      progName.forEach(function(el){if(el)el.textContent='Upload failed: network error'});
      appendChatBubble(name,'assistant','Upload failed: network error',Date.now()/1000);
      setTimeout(function(){progWrap.forEach(function(el){if(el)el.classList.remove('active')})},2000);
      resolve();
    });
    xhr.open('POST',BASE+'/api/sessions/'+name+'/upload');
    xhr.send(fd);
  });
}

async function uploadFile(name,input){
  if(!input.files||!input.files.length)return;
  const tab=_uploadTab[name]||'chat';
  for(const file of input.files){
    await _uploadOneFile(name,tab,file);
  }
  if(input.value!==undefined)input.value='';
}

function handleDrop(event,name,tab){
  event.preventDefault();
  const zone=document.getElementById('dropzone-'+tab+'-'+name);
  if(zone)zone.classList.remove('drag-over');
  const files=event.dataTransfer&&event.dataTransfer.files;
  if(!files||!files.length)return;
  _uploadTab[name]=tab;
  uploadFile(name,{files:files});
}

async function sendCmd(name,source){
  const inputId='cmd-'+source+'-'+name;
  const input=document.getElementById(inputId);
  if(!input)return;
  const cmd=input.value.trim();
  if(!cmd)return;
  input.disabled=true;
  // Also record in chat
  appendChatBubble(name,'user',cmd,Date.now()/1000);
  // Immediately show busy state
  setOptimisticBusy(name);
  try{
    await fetch(BASE+'/api/sessions/'+name+'/send',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command:cmd})
    });
    input.value='';input.style.height='auto';
    delete draftText[source+'-'+name];
    if(source==='raw'){
      // User just sent a command — they want to see the output, reset scroll lock
      const st=getRawState(name);
      st.userScrolledUp=false;
      setTimeout(()=>pollRawDelta(name),500);
    }
  }catch(e){alert('Failed to send.')}
  input.disabled=false;
  input.focus();
  scheduleBusyVerification(name);
}

// ── Raw Output Streaming ──
function startRawPolling(name){
  const st=getRawState(name);
  if(st.polling)return;
  st.polling=true;
  pollRawDelta(name);
  st.timer=setInterval(()=>pollRawDelta(name),1000);
}
function stopRawPolling(name){
  const st=getRawState(name);
  st.polling=false;
  if(st.timer){clearInterval(st.timer);st.timer=null}
}
function stopAllRawPolling(){
  for(const n in rawState)stopRawPolling(n);
}

async function pollRawDelta(name){
  const st=getRawState(name);
  const rawEl=document.getElementById('raw-'+name);
  const infoEl=document.getElementById('raw-info-'+name);
  if(!rawEl)return;
  // Track scroll position via user interaction (wheel/touch), not programmatic scroll events
  if(!rawEl._scrollTracked){
    rawEl._scrollTracked=true;
    rawEl.addEventListener('wheel',()=>{
      setTimeout(()=>{
        const atBottom=(rawEl.scrollHeight-rawEl.scrollTop-rawEl.clientHeight)<80;
        st.userScrolledUp=!atBottom;
      },50);
    },{passive:true});
    rawEl.addEventListener('touchmove',()=>{
      setTimeout(()=>{
        const atBottom=(rawEl.scrollHeight-rawEl.scrollTop-rawEl.clientHeight)<80;
        st.userScrolledUp=!atBottom;
      },50);
    },{passive:true});
  }
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/raw-tail?known_lines='+st.knownLines);
    const data=await resp.json();
    if(data.mode==='full'){
      rawEl.textContent=data.raw||'(empty)';
      st.knownLines=data.pane_total;
      // Only auto-scroll on full load if user hasn't scrolled up
      if(!st.userScrolledUp){
        rawEl.scrollTop=rawEl.scrollHeight;
      }
      if(infoEl)infoEl.textContent=data.total_lines+' lines';
    }else if(data.mode==='delta'&&data.raw){
      const newLines=data.raw.split('\n');
      const curText=rawEl.textContent;
      const existingLines=curText.split('\n');
      // Deduplicate using overlap — compare last N existing lines with first N new lines
      let appendFrom=0;
      let overlapMatched=false;
      if(data.overlap&&existingLines.length>=data.overlap){
        const tail=existingLines.slice(-data.overlap).join('\n');
        const head=newLines.slice(0,data.overlap).join('\n');
        if(tail===head){appendFrom=data.overlap;overlapMatched=true}
      }
      if(overlapMatched){
        // Overlap matched — safe to append only new content
        const toAppend=newLines.slice(appendFrom).join('\n');
        if(toAppend){
          rawEl.textContent=curText+'\n'+toAppend;
        }
      }else{
        // Overlap did NOT match — content diverged. Do a full reload to get all history
        st.knownLines=0;
        const fullResp=await fetch(BASE+'/api/sessions/'+name+'/raw-tail?known_lines=0');
        const fullData=await fullResp.json();
        if(fullData.mode==='full'){
          rawEl.textContent=fullData.raw||'(empty)';
          st.knownLines=fullData.pane_total;
          if(infoEl)infoEl.textContent=fullData.total_lines+' lines';
        }
      }
      if(!overlapMatched)return; // already handled above
      st.knownLines=data.pane_total;
      if(infoEl)infoEl.textContent=data.total_lines+' lines';
      if(!st.userScrolledUp)rawEl.scrollTop=rawEl.scrollHeight;
    }
    // mode==='none': nothing to do
  }catch(e){}
}

async function loadRaw(name){
  const st=getRawState(name);
  st.knownLines=0;
  st.userScrolledUp=false;
  const rawEl=document.getElementById('raw-'+name);
  if(rawEl)rawEl.textContent='Loading...';
  await pollRawDelta(name);
}

function updateStatusPill(name,status,detail){
  const pill=document.getElementById('status-'+name);
  if(pill){
    pill.className='status-pill '+(status||'unknown');
    pill.innerHTML='<span class="status-dot"></span><span class="status-label">'+statusLabel(status)+'</span>'
      +(detail&&status!=='busy'?'<span style="font-weight:400;opacity:.7"> &middot; '+esc(detail)+'</span>':'');
  }
  toggleInterruptButtons(name,status==='busy');
  const navDot=document.getElementById('nav-dot-'+name);
  if(navDot)navDot.className='nav-dot '+(status||'unknown');
}

function updateCard(s){
  // Sync messages from server response
  if(s.messages&&s.messages.length){
    const local=chatMessages[s.name]||[];
    // Find new assistant messages from server not in local
    s.messages.forEach(m=>{
      if(m.role==='assistant'){
        const exists=local.some(l=>l.role==='assistant'&&l.text===m.text);
        if(!exists)appendChatBubble(s.name,'assistant',m.text,m.ts);
      }
    });
  }

  if(s.name!==selectedSession)return;
  const rawTitle=document.getElementById('raw-title-'+s.name);
  if(rawTitle&&s.title)rawTitle.textContent=s.title;
  const desc=document.getElementById('desc-'+s.name);
  const prog=document.getElementById('prog-'+s.name);
  const notesEl=document.getElementById('notes-'+s.name);
  if(desc)desc.textContent=s.description||'';
  if(prog)prog.textContent=s.progress||'';
  if(notesEl&&s.notes)notesEl.textContent=s.notes;
  const tsDesc=document.getElementById('ts-desc-'+s.name);
  const tsProg=document.getElementById('ts-prog-'+s.name);
  const tsNotes=document.getElementById('ts-notes-'+s.name);
  const tsRt=document.getElementById('ts-rt-'+s.name);
  if(tsDesc)tsDesc.textContent=timeAgo(s.description_at);
  if(tsProg)tsProg.textContent=timeAgo(s.progress_at);
  if(tsNotes)tsNotes.textContent=timeAgo(s.notes_at);
  if(tsRt)tsRt.textContent=timeAgo(s.realtime_at);
  updateStatusPill(s.name,s.activity_status,s.activity_detail);

  // Update typing indicator
  const chatEl=document.getElementById('chat-'+s.name);
  if(chatEl){
    const existing=chatEl.querySelector('.chat-typing');
    if(s.activity_status==='busy'&&!existing){
      const typing=document.createElement('div');
      typing.className='chat-typing';
      typing.innerHTML='<span class="typing-dot-group"><span class="typing-dot"></span><span class="typing-dot"></span><span class="typing-dot"></span></span> Working...';
      chatEl.appendChild(typing);
      chatEl.scrollTop=chatEl.scrollHeight;
    }else if(s.activity_status!=='busy'&&existing){
      existing.remove();
    }
  }
}

async function loadAll(){
  try{
    // Phase 1: Fast load — cached data + activity status, no LLM calls
    const resp=await fetch(BASE+'/api/sessions-fast');
    sessions=await resp.json();
    sessions.forEach(s=>{
      lastStatus[s.name]=s.activity_status;
      if(s.messages&&s.messages.length)mergeChatMessages(s.name, s.messages);
    });
    if(!selectedSession&&sessions.length>0)selectedSession=sessions[0].name;
    renderNav();
    renderDetail();
  }catch(e){mainEl.innerHTML='<div class="empty">Error loading sessions.</div>'}
  startStatusPolling();
  // Phase 2: Background LLM refresh for each session
  lazyRefreshAll();
}

async function lazyRefreshAll(){
  await Promise.all(sessions.map(async s=>{
    try{
      const resp=await fetch(BASE+'/api/sessions/'+s.name+'/refresh',{method:'POST'});
      const data=await resp.json();
      const idx=sessions.findIndex(x=>x.name===s.name);
      if(idx>=0){sessions[idx]={...sessions[idx],...data};updateCard(sessions[idx])}
    }catch(e){}
  }));
}

async function refreshOne(name){
  const btn=document.getElementById('btn-'+name);
  if(btn){btn.disabled=true;btn.innerHTML='<span class="spinner"></span>'}
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/refresh',{method:'POST'});
    const data=await resp.json();
    const idx=sessions.findIndex(s=>s.name===name);
    if(idx>=0){sessions[idx]={...sessions[idx],...data};updateCard(sessions[idx])}
  }catch(e){}
  if(btn){btn.disabled=false;btn.textContent='Update'}
}

async function refreshFull(name){
  const btn=document.getElementById('btn-full-'+name);
  const desc=document.getElementById('desc-'+name);
  const prog=document.getElementById('prog-'+name);
  const notesEl=document.getElementById('notes-'+name);
  if(btn){btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Full'}
  [desc,prog,notesEl].forEach(el=>{if(el)el.classList.add('loading')});
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/refresh-all',{method:'POST'});
    const data=await resp.json();
    const idx=sessions.findIndex(s=>s.name===name);
    if(idx>=0){sessions[idx]={...sessions[idx],...data};updateCard(sessions[idx])}
  }catch(e){}
  [desc,prog,notesEl].forEach(el=>{if(el)el.classList.remove('loading')});
  if(btn){btn.disabled=false;btn.textContent='Full'}
}

function startStatusPolling(){
  if(pollTimer)clearInterval(pollTimer);
  pollTimer=setInterval(pollStatus,10000);
}

async function pollStatus(){
  try{
    const resp=await fetch(BASE+'/api/status');
    const statuses=await resp.json();
    let changed=false;
    for(const st of statuses){
      const prev=lastStatus[st.name];
      if(prev&&prev!==st.activity_status){
        changed=true;
        lastStatus[st.name]=st.activity_status;
        statusInfoEl.textContent='Session '+st.name+' changed...';
        refreshOne(st.name);
      }else{
        lastStatus[st.name]=st.activity_status;
      }
      updateStatusPill(st.name,st.activity_status,st.activity_detail);
      if(st.name===selectedSession)updateFavicon(st.activity_status);
      // Update away_mode, go_nuts_mode, and model badges in nav
      const si=sessions.findIndex(s=>s.name===st.name);
      if(si>=0){
        let navChanged=false;
        if(sessions[si].away_mode!==st.away_mode){sessions[si].away_mode=st.away_mode;navChanged=true;}
        if(sessions[si].go_nuts_mode!==st.go_nuts_mode){sessions[si].go_nuts_mode=st.go_nuts_mode;navChanged=true;}
        if(st.model&&sessions[si].model!==st.model){
          sessions[si].model=st.model;navChanged=true;
          // Update detail model badge if visible
          const mb=document.getElementById('model-badge-'+st.name);
          if(mb)mb.textContent=formatModelName(st.model);
        }
        if(navChanged)renderNav();
      }
    }
    if(!changed)statusInfoEl.textContent='Watching for changes...';
  }catch(e){statusInfoEl.textContent='Status poll failed'}
  _authPollCount++;
  if(_authPollCount%5===0)checkClaudeAuth();
  // Refresh inline server stats every 3rd poll (~30s)
  if(_authPollCount%3===0)refreshNavStats();
}

// --- Inline server stats in nav header ---
const navStatsEl=document.getElementById('nav-server-stats');
async function refreshNavStats(){
  try{
    const resp=await fetch(BASE+'/api/stats');
    const s=await resp.json();
    const cpuPct=s.cpu_percent!=null?s.cpu_percent:0;
    const threads=s.threads_running!=null?s.threads_running:'?';
    const cpuCount=s.cpu_count||1;
    const cpuClass=cpuPct>=80?'crit':cpuPct>=50?'warn':'';
    const memPct=s.memory&&s.memory.total_mb?Math.round(s.memory.used_mb/s.memory.total_mb*100):0;
    const memClass=memPct>=80?'crit':memPct>=60?'warn':'';
    navStatsEl.innerHTML='<span>CPU <span class="stat-val '+cpuClass+'">'+cpuPct+'%</span> <span style="color:#6e7681">'+threads+'/'+cpuCount+'t</span></span>'
      +'<span>MEM <span class="stat-val '+memClass+'">'+memPct+'%</span></span>';
  }catch(e){navStatsEl.innerHTML=''}
}
refreshNavStats();

function closeModal(){document.getElementById('modal-overlay').classList.remove('active')}

function showCreateModal(){
  const modal=document.getElementById('modal-content');
  modal.innerHTML=`
    <h3>New tmux session</h3>
    <p>Leave blank for an auto-assigned name, or enter a custom name.</p>
    <input type="text" class="modal-input" id="new-session-name"
      placeholder="e.g. my-project" autocomplete="off" spellcheck="false"
      onkeydown="if(event.key==='Enter')createSession()">
    <div class="modal-actions">
      <button class="modal-cancel" onclick="closeModal()">Cancel</button>
      <button class="modal-confirm-create" onclick="createSession()">Create</button>
    </div>`;
  document.getElementById('modal-overlay').classList.add('active');
  setTimeout(()=>document.getElementById('new-session-name').focus(),50);
}

async function createSession(){
  const input=document.getElementById('new-session-name');
  const name=input?input.value.trim():'';
  closeModal();
  try{
    const resp=await fetch(BASE+'/api/sessions/create',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name})
    });
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed');return}
    selectedSession=data.name;
    await loadAll();
  }catch(e){alert('Failed to create session.')}
}

function showDeleteModal(name){
  const modal=document.getElementById('modal-content');
  modal.innerHTML=`
    <h3>Kill session ${esc(name)}?</h3>
    <p>This will terminate all processes in this tmux session. Cannot be undone.</p>
    <div class="modal-actions">
      <button class="modal-cancel" onclick="closeModal()">Cancel</button>
      <button class="modal-confirm-delete" onclick="deleteSession('${esc(name)}')">Kill Session</button>
    </div>`;
  document.getElementById('modal-overlay').classList.add('active');
}

async function deleteSession(name){
  closeModal();
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name,{method:'DELETE'});
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed');return}
    if(selectedSession===name)selectedSession=null;
    delete chatMessages[name];
    await loadAll();
  }catch(e){alert('Failed to kill session.')}
}

// ── Claude Auth ──
let _authCache=null;
let _usageCache=null;
let _authPollCount=0;

async function checkClaudeAuth(){
  // Fetch auth and usage independently so one failure doesn't break the other
  try{
    const authResp=await fetch(BASE+'/api/auth/claude-status');
    if(authResp.ok){
      const data=await authResp.json();
      _authCache=data;
    }
    // If fetch succeeded but returned bad data, keep previous cache
  }catch(e){
    // Keep last known good auth state instead of resetting to disconnected
    if(!_authCache)_authCache={loggedIn:false,error:true};
  }
  try{
    const usageResp=await fetch(BASE+'/api/auth/usage');
    if(usageResp.ok) _usageCache=await usageResp.json();
  }catch(e){
    // Keep last known usage data
  }
  renderAuthIndicator();
}

function renderAuthIndicator(){
  const dot=document.getElementById('claude-auth-dot');
  const label=document.getElementById('claude-auth-label');
  if(!_authCache){dot.className='status-dot unknown';label.textContent='...';return}
  if(_authCache.hasApiKey&&!_authCache.loggedIn){
    dot.className='status-dot';dot.style.background='#d2a8ff';
    label.textContent='API Key';return;
  }
  if(_authCache.loggedIn){
    dot.className='status-dot idle';dot.style.background='';
    const email=_authCache.email||'';
    const plan=_authCache.subscriptionType||'';
    label.textContent=email+(plan?' · '+plan.charAt(0).toUpperCase()+plan.slice(1):'');
  }else{
    dot.className='status-dot busy';dot.style.background='';
    label.textContent='Not connected';
  }
}

function toggleAuthPanel(event){
  event.stopPropagation();
  const dd=document.getElementById('auth-dropdown');
  dd.classList.toggle('active');
  if(dd.classList.contains('active'))renderAuthPanel();
}

function fmtTokens(n){
  if(n>=1e9)return (n/1e9).toFixed(1)+'B';
  if(n>=1e6)return (n/1e6).toFixed(1)+'M';
  if(n>=1e3)return (n/1e3).toFixed(1)+'K';
  return String(n);
}

function renderUsageHtml(){
  if(!_usageCache||!_usageCache.totalTokens)return '';
  const u=_usageCache;
  const total=u.totalTokens;
  const outPct=total?Math.round(u.outputTokens/total*100):0;
  const inPct=total?Math.round(u.inputTokens/total*100):0;
  const crPct=total?Math.round(u.cacheCreateTokens/total*100):0;
  const rdPct=total?Math.round(u.cacheReadTokens/total*100):0;
  // Bar segments
  const barH='<div style="display:flex;height:6px;border-radius:3px;overflow:hidden;background:#21262d;margin:8px 0 4px">'
    +'<div style="width:'+outPct+'%;background:#f85149" title="Output '+outPct+'%"></div>'
    +'<div style="width:'+crPct+'%;background:#d2a8ff" title="Cache write '+crPct+'%"></div>'
    +'<div style="width:'+inPct+'%;background:#58a6ff" title="Input '+inPct+'%"></div>'
    +'<div style="width:'+rdPct+'%;background:#21262d" title="Cache read '+rdPct+'%"></div>'
    +'</div>';
  return '<hr class="auth-divider">'
    +'<div class="auth-title" style="margin-bottom:4px">Today\'s Usage</div>'
    +'<div class="auth-row"><span class="auth-row-label">Messages</span><span class="auth-row-value">'+u.messages+'</span></div>'
    +'<div class="auth-row"><span class="auth-row-label">Total tokens</span><span class="auth-row-value">'+fmtTokens(total)+'</span></div>'
    +barH
    +'<div style="display:flex;flex-wrap:wrap;gap:2px 12px;font-size:.7rem;color:#8b949e;margin-bottom:2px">'
    +'<span><span style="color:#f85149">●</span> Output '+fmtTokens(u.outputTokens)+'</span>'
    +'<span><span style="color:#58a6ff">●</span> Input '+fmtTokens(u.inputTokens)+'</span>'
    +'<span><span style="color:#d2a8ff">●</span> Cache write '+fmtTokens(u.cacheCreateTokens)+'</span>'
    +'<span style="color:#484f58">Cache read '+fmtTokens(u.cacheReadTokens)+'</span>'
    +'</div>'
    +'<p class="auth-hint" style="margin-top:6px">Usage resets on a 5-hour rolling window.</p>';
}

function renderAuthPanel(){
  const el=document.getElementById('auth-dropdown-content');
  if(!_authCache){el.innerHTML='<div class="auth-title">Loading...</div>';return}
  const usageHtml=renderUsageHtml();
  if(_authCache.loggedIn){
    const plan=(_authCache.subscriptionType||'free').toLowerCase();
    const planClass=plan==='max'?'max':plan==='pro'?'pro':'free';
    el.innerHTML=`
      <div class="auth-title">Claude Code Connected</div>
      <div class="auth-row"><span class="auth-row-label">Email</span><span class="auth-row-value">${esc(_authCache.email||'—')}</span></div>
      <div class="auth-row"><span class="auth-row-label">Plan</span><span class="auth-row-value"><span class="auth-plan-badge ${planClass}">${esc(plan)}</span></span></div>
      <div class="auth-row"><span class="auth-row-label">Auth</span><span class="auth-row-value">${esc(_authCache.authMethod||'—')}</span></div>
      ${_authCache.hasApiKey?'<div class="auth-row"><span class="auth-row-label">API Key</span><span class="auth-row-value" style="color:#3fb950">Stored</span></div>':''}
      ${usageHtml}
      <hr class="auth-divider">
      ${_authCache.hasApiKey?'<button class="auth-btn auth-btn-danger" style="margin-bottom:8px" onclick="clearApiKey()">Clear stored API key</button>':''}
      <button class="auth-btn auth-btn-danger" onclick="claudeLogout()">Sign out of Claude</button>
    `;
  }else{
    el.innerHTML=`
      <div class="auth-title">Claude Code — Not Connected</div>
      <p class="auth-hint">Set an Anthropic API key to authenticate Claude Code for new sessions:</p>
      <input type="password" class="auth-api-input" id="auth-api-key-input"
        placeholder="sk-ant-api03-..." autocomplete="off" spellcheck="false"
        value="${_authCache.hasApiKey?'••••••••••••••••':''}">
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="auth-btn auth-btn-primary" style="flex:1" onclick="saveApiKey()">Save key</button>
        ${_authCache.hasApiKey?'<button class="auth-btn auth-btn-danger" style="flex:1" onclick="clearApiKey()">Clear</button>':''}
      </div>
      ${usageHtml}
      <hr class="auth-divider">
      <p class="auth-hint">Or authenticate via OAuth by running <code style="color:#79c0ff">claude auth login</code> in a terminal session.</p>
    `;
  }
}

async function saveApiKey(){
  const input=document.getElementById('auth-api-key-input');
  if(!input)return;
  const key=input.value.trim();
  if(!key){alert('Please enter an API key.');return}
  try{
    const resp=await fetch(BASE+'/api/auth/api-key',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({apiKey:key})
    });
    const data=await resp.json();
    if(!resp.ok){alert(data.detail||'Failed to save');return}
    await checkClaudeAuth();
    renderAuthPanel();
  }catch(e){alert('Failed to save API key.')}
}

async function clearApiKey(){
  try{
    const resp=await fetch(BASE+'/api/auth/api-key',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({apiKey:''})
    });
    await resp.json();
    await checkClaudeAuth();
    renderAuthPanel();
  }catch(e){alert('Failed to clear API key.')}
}

async function claudeLogout(){
  if(!confirm('Sign out of Claude Code?'))return;
  try{
    const resp=await fetch(BASE+'/api/auth/logout',{method:'POST'});
    await resp.json();
    await checkClaudeAuth();
    renderAuthPanel();
  }catch(e){alert('Failed to sign out.')}
}

// Close auth dropdown on outside click
document.addEventListener('click',function(e){
  const dd=document.getElementById('auth-dropdown');
  const trigger=document.getElementById('claude-auth');
  if(dd.classList.contains('active')&&!dd.contains(e.target)&&!trigger.contains(e.target)){
    dd.classList.remove('active');
  }
});

// ── Interrupt Session ──
async function interruptSession(name){
  try{
    await fetch(BASE+'/api/sessions/'+name+'/interrupt',{method:'POST'});
    appendChatBubble(name,'user','[interrupted]',Date.now()/1000);
    // Clear busy state
    const idx=sessions.findIndex(s=>s.name===name);
    if(idx>=0){sessions[idx].activity_status='idle';sessions[idx].activity_detail=''}
    lastStatus[name]='idle';
    updateStatusPill(name,'idle','');
    toggleInterruptButtons(name,false);
    if(name===selectedSession)updateFavicon('idle');
    // Remove typing indicator
    const chatEl=document.getElementById('chat-'+name);
    if(chatEl){const t=chatEl.querySelector('.chat-typing');if(t)t.remove()}
  }catch(e){alert('Failed to interrupt session.')}
  // Verify state after a moment
  setTimeout(()=>refreshOne(name),2000);
}
function toggleInterruptButtons(name,show){
  ['interrupt-chat-'+name,'interrupt-raw-'+name].forEach(id=>{
    const btn=document.getElementById(id);
    if(btn){if(show)btn.classList.add('visible');else btn.classList.remove('visible')}
  });
}

// ── Terminal Resize ──
let _resizing=null;
function getTerminalHeight(){
  const saved=localStorage.getItem('terminalHeight');
  return saved?'max-height:'+saved+'px':'';
}
function startResize(e,name){
  e.preventDefault();
  const rawEl=document.getElementById('raw-'+name);
  if(!rawEl)return;
  _resizing={el:rawEl,startY:e.clientY,startH:rawEl.offsetHeight};
  document.addEventListener('mousemove',doResize);
  document.addEventListener('mouseup',stopResize);
  document.body.style.cursor='ns-resize';
  document.body.style.userSelect='none';
}
function doResize(e){
  if(!_resizing)return;
  const delta=e.clientY-_resizing.startY;
  const newH=Math.max(120,Math.min(window.innerHeight-200,_resizing.startH+delta));
  _resizing.el.style.maxHeight=newH+'px';
}
function stopResize(){
  if(!_resizing)return;
  const finalH=parseInt(_resizing.el.style.maxHeight);
  if(finalH)localStorage.setItem('terminalHeight',String(finalH));
  _resizing=null;
  document.removeEventListener('mousemove',doResize);
  document.removeEventListener('mouseup',stopResize);
  document.body.style.cursor='';
  document.body.style.userSelect='';
}

// ── Send Raw Keys ──
async function sendRawKeys(name,keys){
  try{
    await fetch(BASE+'/api/sessions/'+name+'/send-keys',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({keys:keys})
    });
    setTimeout(()=>pollRawDelta(name),400);
  }catch(e){console.error('Failed to send keys:',e)}
}

// ── Bracketed Paste Toggle ──
// Tracks per-session state. Default is ON (true). Click toggles.
let _bracketedPaste={};
async function toggleBracketedPaste(name,btn){
  if(!(name in _bracketedPaste))_bracketedPaste[name]=true;
  _bracketedPaste[name]=!_bracketedPaste[name];
  const enabled=_bracketedPaste[name];
  try{
    await fetch(BASE+'/api/sessions/'+name+'/bracketed-paste',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:enabled})
    });
  }catch(e){console.error('Bracketed paste toggle failed:',e)}
  if(btn){
    btn.textContent='Paste Mode: '+(enabled?'ON':'OFF');
    btn.classList.toggle('off',!enabled);
  }
}

// ── Auth Mode Toggle ──
async function setAuthMode(name,mode){
  const statusEl=document.getElementById('auth-mode-status-'+name);
  if(statusEl)statusEl.textContent='Switching...';
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/set-auth-mode',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode:mode})
    });
    const data=await resp.json();
    if(resp.ok){
      const idx=sessions.findIndex(s=>s.name===name);
      if(idx>=0)sessions[idx].auth_mode=mode;
      if(statusEl)statusEl.textContent=mode==='api'?'API key exported':'API key unset';
    }else{
      if(statusEl)statusEl.textContent=data.error||'Failed';
    }
  }catch(e){
    if(statusEl)statusEl.textContent='Error';
  }
}

// ── Session Stats ──
let _statsTimers={};
function fmtRate(n){
  if(n>=1000)return (n/1000).toFixed(1)+'k/min';
  return n+'/min';
}

async function loadSessionStats(name){
  const panel=document.getElementById('stats-panel-'+name);
  if(!panel)return;
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/stats');
    const st=await resp.json();
    if(!st.available){
      panel.innerHTML='<span style="color:#6e7681">No token data yet — waiting for Claude Code activity.</span>';
      return;
    }
    const rateCls=st.rateStatus;
    const rateLabel=rateCls==='severely_limited'?'Severely Limited':rateCls==='limited'?'Limited':'Normal';
    const barPct=Math.min(100,Math.max(2,st.ratePct));
    const sinceStr=st.secsSinceLastActivity<0?'—':st.secsSinceLastActivity<60?st.secsSinceLastActivity+'s ago':st.secsSinceLastActivity<3600?Math.floor(st.secsSinceLastActivity/60)+'m ago':Math.floor(st.secsSinceLastActivity/3600)+'h ago';
    panel.innerHTML=`
      <div class="stats-grid">
        <div class="stat-item"><span class="stat-label">Model</span><span class="stat-value"><span class="model-tag">${esc(st.model)}</span></span></div>
        <div class="stat-item"><span class="stat-label">Messages</span><span class="stat-value">${st.messageCount}</span></div>
        <div class="stat-item"><span class="stat-label">Input tokens</span><span class="stat-value">${fmtTokens(st.totalInput)}</span></div>
        <div class="stat-item"><span class="stat-label">Output tokens</span><span class="stat-value">${fmtTokens(st.totalOutput)}</span></div>
        <div class="stat-item"><span class="stat-label">Cache read</span><span class="stat-value">${fmtTokens(st.cacheRead)}</span></div>
        <div class="stat-item"><span class="stat-label">Cache write</span><span class="stat-value">${fmtTokens(st.cacheCreate)}</span></div>
        <div class="stats-divider"></div>
        <div class="stat-item"><span class="stat-label">API equiv.</span><span class="stat-value cost">$${st.estimatedCost.toFixed(2)}</span></div>
        <div class="stat-item"><span class="stat-label">Total tokens</span><span class="stat-value">${fmtTokens(st.totalTokens)}</span></div>
        <div class="stat-item"><span class="stat-label">Active time</span><span class="stat-value">${st.activeMinutes}m / ${st.sessionDurationMin}m</span></div>
        <div class="stat-item"><span class="stat-label">Last activity</span><span class="stat-value">${sinceStr}</span></div>
      </div>
      <div style="margin-top:10px;display:flex;align-items:center;gap:10px">
        <span style="font-size:.75rem;color:#8b949e">Rate</span>
        <span class="rate-badge ${rateCls}">${rateLabel}</span>
        <span style="font-size:.75rem;color:#6e7681">${fmtRate(st.recentOutputRate)} output</span>
        <span style="font-size:.65rem;color:#484f58">peak ${fmtRate(st.peakOutputRate)}</span>
      </div>
      <div class="rate-bar">
        <div class="rate-bar-track"><div class="rate-bar-fill ${rateCls}" style="width:${barPct}%"></div></div>
        <span class="rate-label">${st.ratePct}%</span>
      </div>`;
  }catch(e){
    panel.innerHTML='<span style="color:#6e7681">Stats unavailable</span>';
  }
}

function startStatsPolling(name){
  stopStatsPolling();
  loadSessionStats(name);
  _statsTimers[name]=setInterval(()=>loadSessionStats(name),15000);
}
function stopStatsPolling(){
  Object.values(_statsTimers).forEach(t=>clearInterval(t));
  _statsTimers={};
}

// ── Away Mode ──
let _awayTimers={};
async function toggleAwayMode(name,enabled){
  const statusEl=document.getElementById('away-status-'+name);
  if(statusEl)statusEl.textContent=enabled?'Starting...':'Stopping...';
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/away-mode',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:enabled})
    });
    const data=await resp.json();
    if(resp.ok){
      const idx=sessions.findIndex(s=>s.name===name);
      if(idx>=0)sessions[idx].away_mode=data.enabled;
      if(statusEl)statusEl.textContent=data.enabled?'Running \u2014 Phase '+(data.phase||0)+': '+(data.phase_name||''):'Off';
      if(data.enabled)startAwayPolling(name);
      else stopAwayPolling(name);
      renderNav();
    }else{
      if(statusEl)statusEl.textContent=data.error||'Failed';
      const tog=document.getElementById('away-toggle-'+name);
      if(tog)tog.checked=!enabled;
    }
  }catch(e){
    if(statusEl)statusEl.textContent='Error';
    const tog=document.getElementById('away-toggle-'+name);
    if(tog)tog.checked=!enabled;
  }
}
function startAwayPolling(name){
  stopAwayPolling(name);
  loadAwayStatus(name);
  _awayTimers[name]=setInterval(()=>loadAwayStatus(name),10000);
}
function stopAwayPolling(name){
  if(_awayTimers[name]){clearInterval(_awayTimers[name]);delete _awayTimers[name];}
}
function stopAllAwayPolling(){
  Object.keys(_awayTimers).forEach(n=>stopAwayPolling(n));
}
async function loadAwayStatus(name){
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/away-mode');
    const data=await resp.json();
    const statusEl=document.getElementById('away-status-'+name);
    const logEl=document.getElementById('away-log-'+name);
    const tog=document.getElementById('away-toggle-'+name);
    if(statusEl){
      if(data.enabled)statusEl.textContent='Phase '+data.phase+': '+(data.phase_name||'');
      else statusEl.textContent=data.log&&data.log.length?'Finished':'Off';
    }
    if(tog)tog.checked=!!data.enabled;
    if(logEl&&data.log&&data.log.length){
      logEl.innerHTML=data.log.slice(-15).map(e=>
        '<div class="away-log-entry"><span class="away-ts">['+new Date(e.ts*1000).toLocaleTimeString()+']</span> '+esc(e.action)+'</div>'
      ).join('');
      logEl.scrollTop=logEl.scrollHeight;
    }
    // Update nav badge
    const idx=sessions.findIndex(s=>s.name===name);
    if(idx>=0)sessions[idx].away_mode=data.enabled;
    if(!data.enabled&&_awayTimers[name])stopAwayPolling(name);
  }catch(e){}
}

// ── Go Nuts Mode ──
let _goNutsTimers={};
async function toggleGoNutsMode(name,enabled){
  const statusEl=document.getElementById('gonuts-status-'+name);
  if(statusEl)statusEl.textContent=enabled?'Starting...':'Stopping...';
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/go-nuts-mode',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({enabled:enabled})
    });
    const data=await resp.json();
    if(resp.ok){
      const idx=sessions.findIndex(s=>s.name===name);
      if(idx>=0)sessions[idx].go_nuts_mode=data.enabled;
      if(statusEl)statusEl.textContent=data.enabled?'Running \u2014 Phase '+(data.phase||0)+': '+(data.phase_name||''):'Off';
      if(data.enabled)startGoNutsPolling(name);
      else stopGoNutsPolling(name);
      renderNav();
    }else{
      if(statusEl)statusEl.textContent=data.error||'Failed';
      const tog=document.getElementById('gonuts-toggle-'+name);
      if(tog)tog.checked=!enabled;
    }
  }catch(e){
    if(statusEl)statusEl.textContent='Error';
    const tog=document.getElementById('gonuts-toggle-'+name);
    if(tog)tog.checked=!enabled;
  }
}
function startGoNutsPolling(name){
  stopGoNutsPolling(name);
  loadGoNutsStatus(name);
  _goNutsTimers[name]=setInterval(()=>loadGoNutsStatus(name),10000);
}
function stopGoNutsPolling(name){
  if(_goNutsTimers[name]){clearInterval(_goNutsTimers[name]);delete _goNutsTimers[name];}
}
function stopAllGoNutsPolling(){
  Object.keys(_goNutsTimers).forEach(n=>stopGoNutsPolling(n));
}
async function loadGoNutsStatus(name){
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/go-nuts-mode');
    const data=await resp.json();
    const statusEl=document.getElementById('gonuts-status-'+name);
    const logEl=document.getElementById('gonuts-log-'+name);
    const tog=document.getElementById('gonuts-toggle-'+name);
    if(statusEl){
      if(data.enabled)statusEl.textContent='Phase '+data.phase+': '+(data.phase_name||'');
      else statusEl.textContent=data.log&&data.log.length?'Finished':'Off';
    }
    if(tog)tog.checked=!!data.enabled;
    if(logEl&&data.log&&data.log.length){
      logEl.innerHTML=data.log.slice(-15).map(e=>
        '<div class="gonuts-log-entry"><span class="gonuts-ts">['+new Date(e.ts*1000).toLocaleTimeString()+']</span> '+esc(e.action)+'</div>'
      ).join('');
      logEl.scrollTop=logEl.scrollHeight;
    }
    const idx=sessions.findIndex(s=>s.name===name);
    if(idx>=0)sessions[idx].go_nuts_mode=data.enabled;
    if(!data.enabled&&_goNutsTimers[name])stopGoNutsPolling(name);
  }catch(e){}
}

// ── Key Bar + Slash Commands ──
function buildKeyBar(name,tab){
  const id='keybar-'+tab+'-'+name;
  const isOpen=localStorage.getItem('keyBarOpen')==='true';
  return `<div class="key-bar-toggle${isOpen?' open':''}" onclick="toggleKeyBar('${id}',this)">
    <span class="chevron">&#x25BC;</span> Keys &amp; Commands
  </div>
  <div class="key-bar${isOpen?' expanded':''}" id="${id}">
    <span class="key-bar-label">Keys:</span>
    <button class="key-btn key-esc" onclick="sendRawKeys('${name}',['Escape'])" title="Escape — exit menus/dialogs">Esc</button>
    <button class="key-btn key-ctrlc" onclick="sendRawKeys('${name}',['C-c'])" title="Ctrl+C — interrupt">Ctrl+C</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['C-u'])" title="Ctrl+U — clear input line (wipes any stale/phantom text from Claude Code's input buffer without interrupting the running task)">Clear Input</button>
    <span class="key-bar-sep"></span>
    <button class="key-btn" onclick="sendRawKeys('${name}',['Enter'])" title="Enter">Enter</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['Space'])" title="Space — scroll pager">Space</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['q'])" title="q — quit pager">q</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['y'])" title="y — yes">y</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['n'])" title="n — no">n</button>
    <span class="key-bar-sep"></span>
    <button class="key-btn" onclick="sendRawKeys('${name}',['Up'])" title="Arrow up">&#x2191;</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['Down'])" title="Arrow down">&#x2193;</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['Tab'])" title="Tab">Tab</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['C-d'])" title="Ctrl+D — EOF">Ctrl+D</button>
    <button class="key-btn" onclick="sendRawKeys('${name}',['C-l'])" title="Ctrl+L — clear">Ctrl+L</button>
    <span class="key-bar-sep"></span>
    <span class="key-bar-label">Cmds:</span>
    <button class="key-btn key-slash" onclick="sendSlashCommand('${name}','/clear')" title="Wipe conversation">/clear</button>
    <button class="key-btn key-slash" onclick="sendSlashCommand('${name}','/compact')" title="Summarize context">/compact</button>
    <button class="key-btn key-slash" onclick="sendSlashCommand('${name}','/context')" title="Context usage">/context</button>
    <button class="key-btn key-slash" onclick="sendSlashCommand('${name}','/cost')" title="Session cost">/cost</button>
    <button class="key-btn key-slash" onclick="sendSlashCommand('${name}','/usage')" title="Rate limits">/usage</button>
    <button class="key-btn key-slash" onclick="sendSlashCommand('${name}','/model sonnet')" title="Switch to Sonnet">/model sonnet</button>
    <button class="key-btn key-slash" onclick="sendSlashCommand('${name}','/model opus')" title="Switch to Opus">/model opus</button>
    <button class="key-btn key-slash" onclick="sendSlashCommand('${name}','/plan')" title="Plan mode">/plan</button>
    <span class="key-bar-sep"></span>
    <span class="key-bar-label">Opts:</span>
    <button class="key-btn key-toggle" id="bp-toggle-${name}" onclick="toggleBracketedPaste('${name}',this)" title="Bracketed Paste — when ON, multi-line pastes show as preview. Turn OFF to paste raw text.">Paste Mode: ON</button>
    <span class="key-bar-sep"></span>
    <button class="key-btn" onclick="_uploadTab['${name}']='${tab}';document.getElementById('upload-${tab==='raw'?'raw-':''}${name}').click()" title="Upload file">&#x1F4CE; Upload</button>
    <div class="drop-zone" id="dropzone-${tab}-${name}"
      ondragover="event.preventDefault();this.classList.add('drag-over')"
      ondragleave="this.classList.remove('drag-over')"
      ondrop="handleDrop(event,'${name}','${tab}')"
      onclick="_uploadTab['${name}']='${tab}';document.getElementById('upload-${tab==='raw'?'raw-':''}${name}').click()">
      <div class="drop-zone-icon">&#x1F4C2;</div>
      <div class="drop-zone-text">Drop files here or click to upload</div>
      <div class="upload-progress" id="upload-progress-${tab}-${name}">
        <div class="upload-progress-filename" id="upload-progress-name-${tab}-${name}"></div>
        <div class="upload-progress-bar"><div class="upload-progress-fill" id="upload-progress-fill-${tab}-${name}"></div></div>
      </div>
    </div>
  </div>`;
}

function toggleKeyBar(barId,toggleEl){
  const bar=document.getElementById(barId);
  if(!bar)return;
  const isOpen=bar.classList.contains('expanded');
  if(isOpen){
    bar.classList.remove('expanded');
    toggleEl.classList.remove('open');
    localStorage.setItem('keyBarOpen','false');
  }else{
    bar.classList.add('expanded');
    toggleEl.classList.add('open');
    localStorage.setItem('keyBarOpen','true');
  }
}

async function sendSlashCommand(name,cmd){
  appendChatBubble(name,'user',cmd,Date.now()/1000);
  setOptimisticBusy(name);
  try{
    await fetch(BASE+'/api/sessions/'+name+'/send',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command:cmd})
    });
  }catch(e){alert('Failed to send command.')}
  scheduleBusyVerification(name);
}

// ── Skills Tab ──
let _skillsLibrarySession='';

async function loadSkillFiles(name){
  const listEl=document.getElementById('skills-list-'+name);
  if(!listEl)return;
  listEl.innerHTML='<div class="skills-empty">Loading...</div>';
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/skills');
    const data=await resp.json();
    if(!data.files||!data.files.length){
      listEl.innerHTML='<div class="skills-empty">No skill files yet. Click "+ New File" to create one.</div>';
      return;
    }
    listEl.innerHTML=data.files.map(f=>`
      <div class="skills-file">
        <span>
          <span class="skills-file-name" onclick="editSkillFile('${name}','${esc(f.name)}')">${esc(f.name)}</span>
          <span class="skills-file-meta">${(f.size/1024).toFixed(1)} KB</span>
        </span>
        <span class="skills-file-actions">
          <button class="btn" style="padding:2px 8px;font-size:.75rem" onclick="editSkillFile('${name}','${esc(f.name)}')">Edit</button>
          <button class="btn btn-danger" style="padding:2px 8px;font-size:.75rem" onclick="deleteSkillFile('${name}','${esc(f.name)}')">Del</button>
        </span>
      </div>`).join('');
  }catch(e){
    listEl.innerHTML='<div class="skills-empty" style="color:#f85149">Failed to load skills.</div>';
  }
}

function newSkillFile(name){
  const wrap=document.getElementById('skills-editor-wrap-'+name);
  const fnameEl=document.getElementById('skills-filename-'+name);
  const editorEl=document.getElementById('skills-editor-'+name);
  if(!wrap||!fnameEl||!editorEl)return;
  fnameEl.value='';
  editorEl.value='';
  wrap.style.display='flex';
  fnameEl.focus();
}

async function editSkillFile(name,filename){
  const wrap=document.getElementById('skills-editor-wrap-'+name);
  const fnameEl=document.getElementById('skills-filename-'+name);
  const editorEl=document.getElementById('skills-editor-'+name);
  if(!wrap||!fnameEl||!editorEl)return;
  // Fetch content from the list we already loaded, or re-fetch
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/skills');
    const data=await resp.json();
    const file=data.files.find(f=>f.name===filename);
    if(file){
      fnameEl.value=file.name;
      editorEl.value=file.content;
    }else{
      fnameEl.value=filename;
      editorEl.value='';
    }
  }catch(e){
    fnameEl.value=filename;
    editorEl.value='';
  }
  wrap.style.display='flex';
  editorEl.focus();
}

async function saveSkillFile(name){
  const fnameEl=document.getElementById('skills-filename-'+name);
  const editorEl=document.getElementById('skills-editor-'+name);
  if(!fnameEl||!editorEl)return;
  let fname=fnameEl.value.trim();
  if(!fname){alert('Please enter a filename.');return}
  if(!fname.endsWith('.md'))fname+='.md';
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/skills',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:fname,content:editorEl.value})
    });
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to save');return}
    closeSkillEditor(name);
    loadSkillFiles(name);
  }catch(e){alert('Failed to save skill file.')}
}

async function deleteSkillFile(name,filename){
  if(!confirm('Delete '+filename+'?'))return;
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/skills/'+encodeURIComponent(filename),{method:'DELETE'});
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to delete');return}
    loadSkillFiles(name);
  }catch(e){alert('Failed to delete skill file.')}
}

function closeSkillEditor(name){
  const wrap=document.getElementById('skills-editor-wrap-'+name);
  if(wrap)wrap.style.display='none';
}

async function openSkillLibrary(name){
  _skillsLibrarySession=name;
  const overlay=document.getElementById('skills-library-overlay');
  overlay.classList.add('active');
  const listEl=document.getElementById('skills-library-list');
  listEl.innerHTML='<div style="color:#8b949e;padding:8px">Loading...</div>';
  try{
    const resp=await fetch(BASE+'/api/skill-library');
    const data=await resp.json();
    if(!data.sets||!data.sets.length){
      listEl.innerHTML='<div style="color:#6e7681;padding:12px;text-align:center">No saved skill sets yet.</div>';
      return;
    }
    listEl.innerHTML=data.sets.map(s=>`
      <div class="skills-library-set" onclick="loadSkillSet('${_skillsLibrarySession}','${esc(s.name)}')">
        <div class="skills-library-set-name">${esc(s.name)}</div>
        <div class="skills-library-set-meta">${s.files.length} file${s.files.length!==1?'s':''}: ${s.files.join(', ')}</div>
      </div>`).join('');
  }catch(e){
    listEl.innerHTML='<div style="color:#f85149;padding:8px">Failed to load library.</div>';
  }
}

function closeSkillLibrary(){
  document.getElementById('skills-library-overlay').classList.remove('active');
}

async function loadSkillSet(name,setName){
  if(!confirm('Load skill set "'+setName+'" into session '+name+'? Existing files with the same name will be overwritten.'))return;
  try{
    const resp=await fetch(BASE+'/api/skill-library/load',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:setName,session_name:name})
    });
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to load');return}
    closeSkillLibrary();
    loadSkillFiles(name);
  }catch(e){alert('Failed to load skill set.')}
}

async function saveCurrentToLibrary(){
  const name=_skillsLibrarySession;
  const setName=prompt('Enter a name for this skill set:');
  if(!setName)return;
  try{
    const resp=await fetch(BASE+'/api/skill-library/save',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:setName,session_name:name})
    });
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to save');return}
    alert('Saved '+data.files.length+' file(s) to library as "'+data.name+'"');
    openSkillLibrary(name);
  }catch(e){alert('Failed to save to library.')}
}

// ── CLAUDE.md Editor (global only) ──
let _claudeMdGlobal={path:'',content:'',exists:false};

async function openClaudeMd(){
  const overlay=document.getElementById('claudemd-overlay');
  overlay.classList.add('active');
  document.getElementById('claudemd-editor').value='Loading...';
  document.getElementById('claudemd-path').textContent='';
  try{
    const resp=await fetch(BASE+'/api/claude-md-global');
    const data=await resp.json();
    _claudeMdGlobal=data;
    document.getElementById('claudemd-editor').value=data.content||'';
    document.getElementById('claudemd-path').textContent=data.path;
  }catch(e){
    document.getElementById('claudemd-editor').value='Error loading CLAUDE.md';
  }
}

async function saveClaudeMd(){
  const content=document.getElementById('claudemd-editor').value;
  try{
    const resp=await fetch(BASE+'/api/claude-md-global',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:_claudeMdGlobal.path,content})
    });
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to save');return}
    _claudeMdGlobal.content=content;
    _claudeMdGlobal.exists=true;
    closeClaudeMd();
  }catch(e){alert('Failed to save CLAUDE.md')}
}

function closeClaudeMd(){
  document.getElementById('claudemd-overlay').classList.remove('active');
}

// ── Stats Window ──
async function openStats(){
  const overlay=document.getElementById('stats-overlay');
  overlay.classList.add('active');
  document.getElementById('stats-content').innerHTML='<div style="text-align:center;color:#8b949e;padding:20px"><span class="spinner"></span> Loading stats...</div>';
  try{
    const [statsResp,usageResp]=await Promise.all([fetch(BASE+'/api/stats'),fetch(BASE+'/api/stats/usage')]);
    const s=await statsResp.json();
    let usage=null;
    if(usageResp.ok) usage=await usageResp.json();
    renderStats(s,usage);
  }catch(e){
    document.getElementById('stats-content').innerHTML='<div style="color:#f85149">Failed to load stats.</div>';
  }
}

function renderStats(s,usage){
  let html='';
  // Server
  html+='<div class="stats-section"><div class="stats-section-title">Server</div>';
  html+='<div class="stats-row"><span class="stats-row-label">Uptime</span><span class="stats-row-value">'+esc(s.uptime||'—')+'</span></div>';
  if(s.cpu_load&&s.cpu_load['1m']){
    html+='<div class="stats-row"><span class="stats-row-label">CPU Load</span><span class="stats-row-value">'+esc(s.cpu_load['1m'])+' / '+esc(s.cpu_load['5m'])+' / '+esc(s.cpu_load['15m'])+'</span></div>';
  }
  if(s.cpu_percent!=null){
    html+='<div class="stats-row"><span class="stats-row-label">CPU Usage</span><span class="stats-row-value">'+s.cpu_percent+'% ('+s.cpu_count+' CPUs)</span></div>';
  }
  if(s.threads_running!=null){
    html+='<div class="stats-row"><span class="stats-row-label">Threads</span><span class="stats-row-value">'+s.threads_running+' running / '+s.threads_total+' total</span></div>';
  }
  html+='</div>';
  // Memory
  if(s.memory&&s.memory.total_mb){
    const memPct=Math.round(s.memory.used_mb/s.memory.total_mb*100);
    const memColor=memPct>80?'#f85149':memPct>60?'#d29922':'#3fb950';
    html+='<div class="stats-section"><div class="stats-section-title">Memory</div>';
    html+='<div class="stats-row"><span class="stats-row-label">Used / Total</span><span class="stats-row-value">'+Math.round(s.memory.used_mb/1024*10)/10+'G / '+Math.round(s.memory.total_mb/1024*10)/10+'G ('+memPct+'%)</span></div>';
    html+='<div class="stats-bar"><div class="stats-bar-fill" style="width:'+memPct+'%;background:'+memColor+'"></div></div>';
    html+='</div>';
  }
  // Disk
  if(s.disk&&s.disk.total_gb){
    const diskColor=s.disk.pct>85?'#f85149':s.disk.pct>70?'#d29922':'#3fb950';
    html+='<div class="stats-section"><div class="stats-section-title">Disk</div>';
    html+='<div class="stats-row"><span class="stats-row-label">Used / Total</span><span class="stats-row-value">'+s.disk.used_gb+'G / '+s.disk.total_gb+'G ('+s.disk.pct+'%)</span></div>';
    html+='<div class="stats-bar"><div class="stats-bar-fill" style="width:'+s.disk.pct+'%;background:'+diskColor+'"></div></div>';
    html+='</div>';
  }
  // tmux Sessions
  if(s.tmux_sessions&&s.tmux_sessions.length){
    html+='<div class="stats-section"><div class="stats-section-title">tmux Sessions ('+s.tmux_sessions.length+')</div>';
    s.tmux_sessions.forEach(t=>{
      const att=t.attached?'<span style="color:#3fb950">attached</span>':'<span style="color:#8b949e">detached</span>';
      html+='<div class="stats-row"><span class="stats-row-label">'+esc(t.name)+'</span><span class="stats-row-value">'+t.windows+' win &middot; '+att+'</span></div>';
    });
    html+='</div>';
  }
  // Claude Processes
  html+='<div class="stats-section"><div class="stats-section-title">Claude Processes</div>';
  if(s.claude_processes&&s.claude_processes.length){
    s.claude_processes.forEach(p=>{
      const short=p.length>80?p.substring(0,80)+'...':p;
      html+='<div class="stats-row" style="word-break:break-all"><span class="stats-row-value" style="font-size:.7rem">'+esc(short)+'</span></div>';
    });
  }else{
    html+='<div class="stats-row"><span class="stats-row-label">No Claude processes found</span></div>';
  }
  if(s.claude_related) html+='<div class="stats-row"><span class="stats-row-label">Total related processes</span><span class="stats-row-value">'+s.claude_related+'</span></div>';
  html+='</div>';

  // Claude Usage
  if(usage&&(usage.sessions&&usage.sessions.length||usage.thisWeek&&usage.thisWeek.messages)){
    function fmtTok(n){if(!n)return'—';if(n>=1e6)return(n/1e6).toFixed(1)+'M';if(n>=1e3)return(n/1e3).toFixed(1)+'K';return String(n)}
    function fmtCost(n){if(!n)return'—';return'$'+n.toFixed(2)}
    function modelTag(m){if(!m||m==='unknown')return'';const short=m.replace('claude-','').replace(/-\d{8}$/,'');let bg='#30363d';if(short.includes('opus'))bg='#8b5cf6';else if(short.includes('haiku'))bg='#f59e0b';else if(short.includes('sonnet'))bg='#3b82f6';return'<span class="model-tag" style="background:'+bg+'">'+esc(short)+'</span>'}
    html+='<div class="stats-section"><div class="stats-section-title">Claude Usage</div>';
    html+='<table class="stats-usage-table"><thead><tr><th style="text-align:left">Session</th><th>Model</th><th>5h Tokens</th><th>5h Cost</th><th>Week Tokens</th><th>Week Cost</th></tr></thead><tbody>';
    usage.sessions.forEach(sess=>{
      html+='<tr><td>'+esc(sess.name)+'</td><td>'+modelTag(sess.model)+'</td>';
      html+='<td>'+fmtTok(sess.window5h.totalTokens)+'</td><td>'+fmtCost(sess.window5h.estimatedCost)+'</td>';
      html+='<td>'+fmtTok(sess.thisWeek.totalTokens)+'</td><td>'+fmtCost(sess.thisWeek.estimatedCost)+'</td></tr>';
    });
    // Totals row
    html+='<tr class="stats-totals-row"><td>Total</td><td></td>';
    html+='<td>'+fmtTok(usage.window5h.totalTokens)+'</td><td>'+fmtCost(usage.window5h.estimatedCost)+'</td>';
    html+='<td>'+fmtTok(usage.thisWeek.totalTokens)+'</td><td>'+fmtCost(usage.thisWeek.estimatedCost)+'</td></tr>';
    html+='</tbody></table></div>';
  }

  document.getElementById('stats-content').innerHTML=html;
}

function closeStats(){
  document.getElementById('stats-overlay').classList.remove('active');
}

loadAll();
checkClaudeAuth();
</script>
</body></html>
"""

# Inject the actual ROOT_PATH into the JS BASE variable
HTML_PAGE = HTML_PAGE.replace("__ROOT_PATH__", ROOT_PATH)
LOGIN_PAGE = LOGIN_PAGE.replace("__ROOT_PATH__", ROOT_PATH) if "__ROOT_PATH__" in LOGIN_PAGE else LOGIN_PAGE

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
