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
import shlex
import shutil
import subprocess
import tempfile
import mimetypes
import time
from pathlib import Path
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional
import glob as globmod

logger = logging.getLogger("tmux-dashboard")
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")

from fastapi import FastAPI, Request, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, FileResponse
from pydantic import BaseModel
import openai
import uvicorn

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PORT = int(os.environ.get("TMUX_DASH_PORT", "8501"))
ROOT_PATH = os.environ.get("TMUX_DASH_ROOT_PATH", "/tmux")
NEW_SESSION_CMD = os.environ.get("TMUX_DASH_NEW_SESSION_CMD", "")  # e.g. "claude"

# --- NEMO-DEV team mode ---------------------------------------------------
# When TMUX_DASH_TEAM_MODE=1, non-admin ("user" role) accounts get a heavily
# simplified UI, a shared Claude auth token, per-user context, OAuth connections,
# and a soft sandbox (cross-server actions are blocked + sent to the admin for
# approval). Gated behind env so the shared codebase is unchanged for the personal
# single-admin dashboards (instance-3, builder) that never set these vars.
TEAM_MODE = os.environ.get("TMUX_DASH_TEAM_MODE", "") == "1"
BRAND_NAME = os.environ.get("TMUX_DASH_BRAND", "tmux")
ADMIN_APPROVAL_EMAIL = os.environ.get("TMUX_DASH_ADMIN_EMAIL", "nimrod.rotem@gmail.com")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
MAIL_FROM = os.environ.get("TMUX_DASH_MAIL_FROM", "NEMO-DEV <nemo-dev@grabo.cc>")
PUBLIC_BASE_URL = os.environ.get("TMUX_DASH_PUBLIC_URL", "")  # e.g. https://dianaotech.com
DASH_LOCAL_URL = os.environ.get("TMUX_DASH_LOCAL_URL", "http://127.0.0.1:8501")
# Team-mode default model + reasoning effort, pinned into every session's config.
TEAM_MODEL = os.environ.get("TMUX_DASH_TEAM_MODEL", "claude-opus-4-8[1m]")
TEAM_EFFORT = os.environ.get("TMUX_DASH_TEAM_EFFORT", "max")
# Email domain used for per-user git commit identity (commits are AUTHORED by the
# member even though everyone shares one OS user).
GIT_EMAIL_DOMAIN = os.environ.get("TMUX_DASH_GIT_EMAIL_DOMAIN", "dianaotech.com")

client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

# Auto-summarizer (LLM session title/description/progress/notes + realtime fallback).
# Removed/disabled by default: it issued a continuous stream of gpt-4o-mini calls,
# re-summarizing actively-changing sessions on every poll (high CPU + token cost).
# Re-enable with TMUX_DASH_AUTO_SUMMARY=1 if ever wanted again.
AUTO_SUMMARIZER_ENABLED = os.environ.get("TMUX_DASH_AUTO_SUMMARY", "").lower() in ("1", "true", "yes")

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


# Flag to prevent CancelledError handlers from wiping persisted state during shutdown.
# When True, worker cancel handlers skip setting enabled=False and re-saving to disk.
_shutting_down = False


# --- Simple Watchdog ---
# Default-ON, lightweight watchdog that auto-replies "continue" when Claude is
# idle waiting for the user to confirm whether to keep working on the current
# task. Does NOT take initiative on truly finished work — only resolves the
# "shall I continue?" pause case. Per-session opt-out persisted to disk.
SIMPLE_WATCHDOG_DISABLED_FILE = MESSAGES_DIR / "simple-watchdog-disabled.json"
_simple_watchdog_disabled: set = set()
# Per-session log of recent "continue" sends, capped at 20 entries.
_simple_watchdog_log: Dict[str, list] = {}
# Per-session bookkeeping: {"idle_since": float, "last_action": float, "last_hash": str}
_simple_watchdog_state: Dict[str, dict] = {}


def _save_simple_watchdog_disabled():
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        SIMPLE_WATCHDOG_DISABLED_FILE.write_text(json.dumps(sorted(_simple_watchdog_disabled)))
    except Exception:
        logger.debug("Failed to save simple-watchdog disabled list", exc_info=True)


def _load_simple_watchdog_disabled():
    global _simple_watchdog_disabled
    try:
        if SIMPLE_WATCHDOG_DISABLED_FILE.exists():
            data = json.loads(SIMPLE_WATCHDOG_DISABLED_FILE.read_text())
            if isinstance(data, list):
                _simple_watchdog_disabled = set(data)
    except Exception:
        logger.debug("Failed to load simple-watchdog disabled list", exc_info=True)


# --- Auto-push mode (per session): "off" | "basic" | "full" ---
# Governs how much the dashboard is allowed to type into a session's terminal on
# the user's behalf when Claude stops or waits:
#   off   — never write anything at all (no option-picking, no Enter on prompts,
#           no auto /login, no free-form "keep going" messages).
#   basic — auto-pick from Claude's option menus and confirm permission/plan
#           prompts (press Enter), and keep the session logged in. Does NOT type
#           any free-form instructions.
#   full  — everything in "basic" PLUS the autopilot watchdog that composes and
#           types a "keep going" message when Claude pauses waiting on the user
#           before a task is finished. (This was the previous always-on behavior.)
# New sessions default to "basic". Persisted per session to disk.
AUTOPUSH_MODES = ("off", "basic", "full")
AUTOPUSH_DEFAULT = "basic"
AUTOPUSH_MODE_FILE = MESSAGES_DIR / "autopush-mode.json"
_autopush_mode: Dict[str, str] = {}


def _get_autopush_mode(session_name: str) -> str:
    m = _autopush_mode.get(session_name, AUTOPUSH_DEFAULT)
    return m if m in AUTOPUSH_MODES else AUTOPUSH_DEFAULT


def _save_autopush_mode():
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        AUTOPUSH_MODE_FILE.write_text(json.dumps(_autopush_mode))
    except Exception:
        logger.debug("Failed to save autopush-mode map", exc_info=True)


def _load_autopush_mode():
    global _autopush_mode
    try:
        if AUTOPUSH_MODE_FILE.exists():
            data = json.loads(AUTOPUSH_MODE_FILE.read_text())
            if isinstance(data, dict):
                _autopush_mode = {
                    str(k): v for k, v in data.items() if v in AUTOPUSH_MODES
                }
    except Exception:
        logger.debug("Failed to load autopush-mode map", exc_info=True)


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


async def _ensure_claude_running(session_name: str, log_fn=None, state: dict = None,
                                 resume_uuid: str = None) -> bool:
    """Check if Claude Code is running; if not, restart it. Returns True if Claude is running after check.

    This handles OOM crashes where Claude dies and the pane falls back to bash.
    The relaunch reattaches to the crashed conversation so the task continues:
    `--resume <uuid>` when the exact conversation is known (preferred — sessions
    can share a cwd, which makes plain --continue grab the wrong one), otherwise
    `--continue`. A larger Node heap is set to reduce repeat OOMs.
    """
    alog = logging.getLogger("autonomous")
    if await _async_is_claude_running(session_name):
        return True

    msg = f"Claude Code not running in '{session_name}' — restarting it"
    alog.warning(msg)
    if log_fn and state:
        log_fn(state, msg)

    try:
        # Re-export the active profile's CLAUDE_CONFIG_DIR before launching, in
        # case the shell was respawned (env vars don't survive a fresh bash).
        try:
            pid = _get_session_profile_id(session_name)
            if pid != DEFAULT_PROFILE_ID:
                await asyncio.to_thread(_send_profile_export, session_name, pid)
                await asyncio.sleep(0.2)
        except Exception:
            logger.debug("Failed to re-export profile env on auto-restart", exc_info=True)
        # Re-apply clean member auth before relaunch so an accidental /login (which
        # writes stray creds that 401 against the shared key) self-heals on the next
        # start. Picks the right mode: subscription plan if live, else API key.
        try:
            if TEAM_MODE:
                _owner = _find_user_by_id(_load_session_owners().get(session_name, "admin"))
                if _owner and not _is_admin(_owner):
                    _apply_member_auth(_user_claude_config_dir(_owner))
        except Exception:
            logger.debug("Failed to re-apply member auth on relaunch", exc_info=True)
        # Relaunch Claude on the bare shell, resuming the prior conversation.
        resume_flag = f"--resume {resume_uuid}" if resume_uuid else "--continue"
        launch = ("NODE_OPTIONS=--max-old-space-size=8192 "
                  f"claude --dangerously-skip-permissions {resume_flag}")
        # C-u first to discard any stray text left on the crashed shell's prompt
        # line (e.g. a "continue" a watchdog typed before this loop took over).
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "C-u"],
            capture_output=True, text=True, timeout=5)
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "-l", launch],
            capture_output=True, text=True, timeout=5)
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "Enter"],
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
    if TEAM_MODE:
        try:
            _setup_shared_git_config()
            logger.info("Team mode: shared git config applied")
        except Exception:
            logger.debug("shared git config setup failed", exc_info=True)
    sessions = get_tmux_sessions()
    logger.info("Found %d existing tmux sessions", len(sessions))
    # Auto-responder: presses Enter ONLY when the visible pane shows a
    # Claude Code selection prompt with ❯ directly on a numbered option.
    # The detection refuses to fire when ❯ is followed by free text — that
    # is the user input box and Enter would submit it (the "phantom
    # message" bug). Approves plan/permission prompts hands-free.
    task = asyncio.create_task(_auto_responder_loop())
    _background_tasks.append(task)
    logger.info("Auto-responder background task started")
    _load_simple_watchdog_disabled()
    _load_autopush_mode()
    simple_watchdog_task = asyncio.create_task(_simple_watchdog_loop())
    _background_tasks.append(simple_watchdog_task)
    logger.info("Simple watchdog started (auto-push overrides for %d sessions)", len(_autopush_mode))

    tmp_watchdog_task = asyncio.create_task(_tmp_watchdog_loop())
    _background_tasks.append(tmp_watchdog_task)
    logger.info("Tmp watchdog started")

    login_watchdog_task = asyncio.create_task(_login_watchdog_loop())
    _background_tasks.append(login_watchdog_task)
    logger.info("Login watchdog started")

    crash_recovery_task = asyncio.create_task(_crash_recovery_loop())
    _background_tasks.append(crash_recovery_task)
    logger.info("Crash-recovery watchdog started")

    yield  # Application is running

    # --- Shutdown ---
    global _shutting_down
    _shutting_down = True  # Prevent CancelledError handlers from wiping persisted state
    logger.info("tmux Dashboard shutting down — cancelling %d background tasks", len(_background_tasks))
    for t in _background_tasks:
        if not t.done():
            t.cancel()
    logger.info("Shutdown complete")


app = FastAPI(root_path=ROOT_PATH, lifespan=lifespan)

# --- Auth ---
AUTH_USER = os.environ.get("TMUX_DASH_USER", "admin")
AUTH_PASS = os.environ.get("TMUX_DASH_PASS", "")
AUTH_SECRET = os.environ.get("TMUX_DASH_SECRET", secrets.token_hex(32))


def _make_token(user_id: str) -> str:
    sig = hmac.new(AUTH_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:24]
    return f"{user_id}:{sig}"


def _check_token(token: str) -> bool:
    if not token or ":" not in token:
        return False
    user_id, sig = token.split(":", 1)
    expected = hmac.new(AUTH_SECRET.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:24]
    return hmac.compare_digest(sig, expected)


# --- Multi-user store ---
# Each user record:
#   { id, username, password_hash, password_salt, role ("admin"|"user"),
#     created_at, last_login }
# Admin (id="admin") is bootstrapped from TMUX_DASH_USER / TMUX_DASH_PASS env vars
# on first run, then writable via the admin UI. Per-user data lives at
# ~/.tmux-dashboard/users/<id>/  (the admin keeps the legacy paths to preserve
# existing messages.json / notes.json / uploads). Per-user Claude config lives
# at ~/.claude-user-<id>/ for non-admin users; admin still uses ~/.claude.
USERS_FILE = MESSAGES_DIR / "users.json"


def _hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def _new_salt() -> str:
    return secrets.token_hex(16)


def _new_user_id() -> str:
    return "u_" + secrets.token_hex(8)


def _load_users() -> list:
    """Load users from disk. On first run, seed an admin from env vars."""
    if USERS_FILE.exists():
        try:
            data = json.loads(USERS_FILE.read_text())
            users = data.get("users") if isinstance(data, dict) else None
            if isinstance(users, list) and users:
                return users
        except Exception:
            logger.exception("Failed to read %s -- re-seeding", USERS_FILE)
    # Seed admin from env vars (single-user legacy mode)
    salt = _new_salt()
    admin = {
        "id": "admin",
        "username": AUTH_USER or "admin",
        "password_hash": _hash_password(AUTH_PASS or "", salt),
        "password_salt": salt,
        "role": "admin",
        "created_at": time.time(),
        "last_login": 0,
    }
    _save_users([admin])
    return [admin]


def _save_users(users: list):
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        USERS_FILE.write_text(json.dumps({"users": users}, indent=2))
        try:
            USERS_FILE.chmod(0o600)
        except Exception:
            logger.debug("chmod 600 on users.json failed", exc_info=True)
    except Exception:
        logger.exception("Failed to save users to %s", USERS_FILE)


def _find_user_by_id(user_id: str) -> Optional[dict]:
    for u in _load_users():
        if u.get("id") == user_id:
            return u
    return None


def _find_user_by_username(username: str) -> Optional[dict]:
    for u in _load_users():
        if u.get("username") == username:
            return u
    return None


def _verify_password(user: dict, password: str) -> bool:
    salt = user.get("password_salt", "")
    expected = user.get("password_hash", "")
    candidate = _hash_password(password, salt)
    return bool(expected) and hmac.compare_digest(candidate, expected)


def _user_from_token(token: Optional[str]) -> Optional[dict]:
    """Validate token signature AND look up the user. Returns user dict or None."""
    if not token or not _check_token(token):
        return None
    user_id = token.split(":", 1)[0]
    return _find_user_by_id(user_id)


def _current_user(request: Request) -> Optional[dict]:
    """Resolve the user for an HTTP request via the tmux_auth cookie.

    When AUTH_PASS is empty (auth disabled), behave as if the admin is logged
    in so every downstream check (`is_admin`, ownership filters, etc.) still
    works without per-call ``if not AUTH_PASS`` branches.
    """
    if not AUTH_PASS:
        admin = _find_user_by_id("admin")
        if admin:
            return admin
        return {
            "id": "admin", "username": AUTH_USER or "admin",
            "role": "admin", "_synthetic": True,
        }
    # Stash on request.state to avoid re-loading users.json per request.
    cached = getattr(request.state, "_current_user", None)
    if cached is not None:
        return cached or None  # explicit None vs sentinel
    user = _user_from_token(request.cookies.get("tmux_auth"))
    request.state._current_user = user or {}
    return user


def _is_admin(user: Optional[dict]) -> bool:
    return bool(user) and user.get("role") == "admin"


# Initialize the users store on import so the admin always exists.
try:
    _load_users()
except Exception:
    logger.exception("Failed to initialize users.json")


# --- Per-user data dirs ---
# Admin keeps the legacy ~/.tmux-dashboard/ root for backwards compatibility
# with existing messages.json / notes.json / uploads/ on disk. Non-admin users
# are isolated under ~/.tmux-dashboard/users/<user_id>/.
def _user_data_dir(user: Optional[dict]) -> Path:
    if not user or user.get("id") == "admin":
        return MESSAGES_DIR
    d = MESSAGES_DIR / "users" / user["id"]
    d.mkdir(parents=True, exist_ok=True)
    return d


def _user_messages_file(user: Optional[dict]) -> Path:
    return _user_data_dir(user) / "messages.json"


def _user_notes_file(user: Optional[dict]) -> Path:
    return _user_data_dir(user) / "notes.json"


def _user_uploads_dir(user: Optional[dict]) -> Path:
    return _user_data_dir(user) / "uploads"


def _user_claude_config_dir(user: Optional[dict]) -> Path:
    """Where Claude Code reads CLAUDE.md / MEMORY.md / settings.json / skills/
    / projects/ / memory/ for this user. Admin uses ~/.claude (the global root,
    profiles still apply). Non-admin users get a fully isolated dir.
    """
    if not user or user.get("id") == "admin":
        return Path.home() / ".claude"
    return Path.home() / f".claude-user-{user['id']}"


def _ensure_user_claude_config_dir(user: dict):
    """Create + seed a fresh Claude config dir for a non-admin user."""
    if not user or user.get("id") == "admin":
        return
    d = _user_claude_config_dir(user)
    d.mkdir(parents=True, exist_ok=True)
    for sub in ("skills", "projects", "memory", "agents", "commands"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    # Seed minimal files so Claude Code has something to read.
    claude_md = d / "CLAUDE.md"
    if not claude_md.exists():
        claude_md.write_text(
            f"# {user.get('username', user['id'])}'s CLAUDE.md\n"
            "Personal notes and project context for this user.\n"
        )
    memory_md = d / "MEMORY.md"
    if not memory_md.exists():
        memory_md.write_text(f"# {user.get('username', user['id'])}'s Memory Index\n")
    settings = d / "settings.json"
    if not settings.exists():
        settings.write_text(json.dumps({
            "permissions": {},
            "env": {},
        }, indent=2))
    # Stub .claude.json so first launch doesn't spam the onboarding prompts.
    claude_json = d / ".claude.json"
    if not claude_json.exists():
        claude_json.write_text(json.dumps({
            "hasCompletedOnboarding": True,
            "numStartups": 1,
        }, indent=2))
    # NEMO-DEV team mode: shared Claude auth token, managed global context block,
    # and the soft-sandbox guard hook. Re-applied every call so it self-heals and
    # stays current (e.g. after the admin edits the global context).
    if TEAM_MODE:
        try:
            # Prefer the subscription PLAN; fall back to the shared API key only if
            # there's no live plan token.
            _apply_member_auth(d)
            _sync_global_context_into(claude_md)
            _sync_group_context_into(claude_md, user.get("group", ""))
            _sync_group_skills_into(d, user.get("group", ""))
            _install_sandbox_hook(d, user)
            _ensure_google_mcp(d, user)
            _disable_claude_ai_connectors(d)
            _set_team_model_effort(d)
            _sync_git_rules_into(claude_md)
        except Exception:
            logger.exception("Failed to apply team-mode setup for user %s", user.get("id"))


# --- Session ownership ---
SESSION_OWNERS_FILE = MESSAGES_DIR / "session_owners.json"
_session_owners_cache: Optional[Dict[str, str]] = None


def _load_session_owners() -> Dict[str, str]:
    global _session_owners_cache
    if _session_owners_cache is not None:
        return _session_owners_cache
    try:
        if SESSION_OWNERS_FILE.exists():
            data = json.loads(SESSION_OWNERS_FILE.read_text())
            if isinstance(data, dict):
                _session_owners_cache = {str(k): str(v) for k, v in data.items()}
                return _session_owners_cache
    except Exception:
        logger.debug("Failed to load session owners", exc_info=True)
    _session_owners_cache = {}
    return _session_owners_cache


def _save_session_owners():
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        SESSION_OWNERS_FILE.write_text(json.dumps(_load_session_owners(), indent=2))
    except Exception:
        logger.debug("Failed to save session owners", exc_info=True)


def _session_owner_id(session_name: str) -> str:
    """Return the owner user_id for a session. Pre-existing sessions with no
    recorded owner default to the admin."""
    owners = _load_session_owners()
    return owners.get(session_name, "admin")


def _set_session_owner(session_name: str, user_id: str):
    owners = _load_session_owners()
    owners[session_name] = user_id
    _save_session_owners()


def _clear_session_owner(session_name: str):
    owners = _load_session_owners()
    if session_name in owners:
        owners.pop(session_name, None)
        _save_session_owners()


def _user_for_session(session_name: str) -> Optional[dict]:
    """Find the user record that owns this session, falling back to admin."""
    owner_id = _session_owner_id(session_name)
    user = _find_user_by_id(owner_id) or _find_user_by_id("admin")
    return user


def _user_can_access_session(user: Optional[dict], session_name: str) -> bool:
    """Admins see everything. Regular users only see sessions they own."""
    if _is_admin(user):
        return True
    if not user:
        return False
    return _session_owner_id(session_name) == user["id"]


LOGIN_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>__BRAND__ Dashboard — Login</title>
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
  <h2>__BRAND__ Dashboard</h2>
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


# Regex for /api/sessions/<name>/... and /api/sessions/<name> (DELETE/GET on bare URL).
_SESSION_PATH_RE = re.compile(r"^/api/sessions/([^/]+)(?:/.*)?$")


_ADMIN_ONLY_PREFIXES = (
    "/api/profiles",
    "/api/skill-library",
    "/api/global-claude",
    "/api/global",
    "/api/all-sessions",
)


@app.middleware("http")
async def session_ownership_middleware(request: Request, call_next):
    """Block per-session API calls when the caller doesn't own the session
    and reject admin-only routes for non-admin users.

    Admins always pass. Sessions with no recorded owner default to admin, so
    legacy sessions stay accessible by the admin without migration.
    """
    if not AUTH_PASS:
        return await call_next(request)
    path = request.url.path
    rp = request.scope.get("root_path", "")
    if rp and path.startswith(rp):
        rel = path[len(rp):] or "/"
    else:
        rel = path
    user = _current_user(request)
    # Admin-only routes (Profiles, global CLAUDE.md, etc.)
    for prefix in _ADMIN_ONLY_PREFIXES:
        if rel == prefix or rel.startswith(prefix + "/"):
            if not _is_admin(user):
                return JSONResponse({"error": "Admin only"}, status_code=403)
            break
    # Only gate paths under /api/sessions/<name>. The list endpoints
    # /api/sessions and /api/sessions-fast are handled at the route level
    # (they filter to the caller's owned sessions).
    if rel in ("/api/sessions", "/api/sessions-fast", "/api/sessions/create"):
        return await call_next(request)
    m = _SESSION_PATH_RE.match(rel)
    if not m:
        return await call_next(request)
    session_name = m.group(1)
    if not _user_can_access_session(user, session_name):
        return JSONResponse({"error": "Session not found"}, status_code=404)
    return await call_next(request)


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
    if path in ("/logout", "/logout/", rp + "/logout", rp + "/logout/"):
        return await call_next(request)
    # SSO verify endpoint for nginx auth_request from sibling knowva.ai apps:
    # it must return its own 200/401 based on the cookie, NOT the login-page
    # fallback (auth_request only treats a real 2xx as authenticated).
    if path.endswith("/api/auth/verify"):
        return await call_next(request)
    # Allow qa-output files without auth
    if path.startswith("/qa-output/") or path.startswith(rp + "/qa-output/"):
        return await call_next(request)
    # Sandbox guard hook calls this from localhost with no cookie (it checks the
    # client host itself). OAuth callback self-verifies a signed state param and
    # must work even when the cross-site redirect from Google drops the cookie.
    if path.endswith("/api/sandbox/check") or path.endswith("/api/connections/google/callback"):
        return await call_next(request)
    # Public project serving: /<username>/<project>[/...] is served publicly (the
    # /<username> project-list page itself stays gated below).
    rel_path = path[len(rp):] if (rp and path.startswith(rp)) else path
    if _is_public_project_request(rel_path):
        return await call_next(request)
    token = request.cookies.get("tmux_auth")
    if not _check_token(token):
        resp = HTMLResponse(LOGIN_PAGE)
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
        resp.headers["Pragma"] = "no-cache"
        return resp
    # Token signature is valid — also verify the user still exists. If users.json
    # was tampered with or the user got deleted while logged in, fall back to
    # the login screen.
    user = _user_from_token(token)
    if not user:
        resp = HTMLResponse(LOGIN_PAGE)
        resp.delete_cookie("tmux_auth")
        return resp
    request.state._current_user = user
    return await call_next(request)


@app.get("/api/auth/verify")
async def api_auth_verify(request: Request):
    """SSO check for nginx ``auth_request`` from sibling knowva.ai apps.

    Returns 200 when the shared ``tmux_auth`` cookie is valid, else 401 — so a
    single login to this dashboard unlocks the other knowva.ai apps (matcher,
    crypto, zoom, ...) which gate on this endpoint instead of separate logins.
    """
    if _user_from_token(request.cookies.get("tmux_auth")):
        return JSONResponse({"ok": True})
    return JSONResponse({"ok": False}, status_code=401)


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
    # Legacy env-var path: if the credentials match TMUX_DASH_USER/TMUX_DASH_PASS,
    # accept and treat as the admin user. This keeps the dashboard reachable even
    # if users.json was deleted by hand.
    legacy_ok = (
        AUTH_PASS
        and hmac.compare_digest(username, AUTH_USER)
        and hmac.compare_digest(password, AUTH_PASS)
    )
    user = _find_user_by_username(username)
    if user and _verify_password(user, password):
        target_user = user
    elif legacy_ok:
        # Rebuild the admin user record on the fly if missing/out of sync. Find
        # the admin inside *this* `users` list so the mutation we save below
        # actually lands on the right object (calling _find_user_by_id would
        # return a copy from a separate _load_users()).
        users = _load_users()
        target_user = next((u for u in users if u.get("id") == "admin"), None)
        salt = _new_salt()
        if target_user is None:
            target_user = {
                "id": "admin",
                "username": username,
                "password_hash": _hash_password(password, salt),
                "password_salt": salt,
                "role": "admin",
                "created_at": time.time(),
                "last_login": 0,
            }
            users.append(target_user)
        else:
            # Re-sync username + password hash to whatever the env says (this
            # protects against a stale users.json shipped with an old salt).
            target_user["username"] = username
            target_user["password_salt"] = salt
            target_user["password_hash"] = _hash_password(password, salt)
        _save_users(users)
    else:
        return RedirectResponse(url=request.scope.get("root_path", "") + "/login?err=1", status_code=303)

    # Update last_login + capture IP / browser for the admin audit view
    ua = (request.headers.get("user-agent", "") or "")[:300]
    fwd = request.headers.get("x-forwarded-for", "")
    real_ip = fwd.split(",")[0].strip() if fwd else ip
    try:
        users = _load_users()
        for u in users:
            if u.get("id") == target_user["id"]:
                u["last_login"] = time.time()
                u["last_login_ip"] = real_ip
                u["last_login_ua"] = ua
                break
        _save_users(users)
    except Exception:
        logger.debug("Failed to update last_login for %s", target_user.get("id"), exc_info=True)

    token = _make_token(target_user["id"])
    resp = RedirectResponse(url=request.scope.get("root_path", "") + "/", status_code=303)
    is_https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    resp.set_cookie("tmux_auth", token, max_age=86400 * 30, httponly=True, samesite="lax", secure=is_https)
    return resp


@app.post("/logout")
async def do_logout(request: Request):
    resp = RedirectResponse(url=request.scope.get("root_path", "") + "/login", status_code=303)
    resp.delete_cookie("tmux_auth")
    return resp


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"
    group: str = ""


class UpdateUserBody(BaseModel):
    password: Optional[str] = None
    role: Optional[str] = None
    username: Optional[str] = None
    group: Optional[str] = None


def _public_user(u: dict) -> dict:
    """Strip secrets before returning a user record to the client."""
    return {
        "id": u.get("id", ""),
        "username": u.get("username", ""),
        "role": u.get("role", "user"),
        "group": u.get("group", ""),
        "created_at": u.get("created_at", 0),
        "last_login": u.get("last_login", 0),
        "last_login_ip": u.get("last_login_ip", ""),
        "last_login_ua": u.get("last_login_ua", ""),
    }


def _user_session_count(user_id: str) -> int:
    owners = _load_session_owners()
    return sum(1 for v in owners.values() if v == user_id)


@app.get("/api/admin/users")
async def api_admin_list_users(request: Request):
    user = _current_user(request)
    if not _is_admin(user):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    users = _load_users()
    out = []
    for u in users:
        rec = _public_user(u)
        rec["session_count"] = _user_session_count(u["id"])
        out.append(rec)
    return JSONResponse({"users": out})


@app.post("/api/admin/users")
async def api_admin_create_user(request: Request, body: CreateUserBody):
    user = _current_user(request)
    if not _is_admin(user):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    username = (body.username or "").strip()
    password = body.password or ""
    role = body.role if body.role in ("user", "admin") else "user"
    if not username:
        return JSONResponse({"error": "Username is required"}, status_code=400)
    if not re.match(r"^[A-Za-z0-9._@-]{2,40}$", username):
        return JSONResponse({"error": "Username must be 2-40 chars (letters, numbers, . _ @ -)"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)
    users = _load_users()
    if any(u.get("username") == username for u in users):
        return JSONResponse({"error": f"Username '{username}' already exists"}, status_code=409)
    salt = _new_salt()
    new_user = {
        "id": _new_user_id(),
        "username": username,
        "password_hash": _hash_password(password, salt),
        "password_salt": salt,
        "role": role,
        "group": (body.group or "").strip(),
        "created_at": time.time(),
        "last_login": 0,
    }
    users.append(new_user)
    _save_users(users)
    # Seed the user's data + Claude config dirs so they're ready to use.
    try:
        _user_data_dir(new_user)
        _ensure_user_claude_config_dir(new_user)
    except Exception:
        logger.exception("Failed to seed dirs for new user %s", new_user["id"])
    logger.info("Admin '%s' created user '%s' (role=%s)", user["username"], username, role)
    return JSONResponse({"ok": True, "user": _public_user(new_user)})


@app.patch("/api/admin/users/{user_id}")
async def api_admin_update_user(request: Request, user_id: str, body: UpdateUserBody):
    user = _current_user(request)
    if not _is_admin(user):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    users = _load_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        return JSONResponse({"error": "User not found"}, status_code=404)
    changed = False
    if body.username is not None:
        new_un = body.username.strip()
        if not re.match(r"^[A-Za-z0-9._@-]{2,40}$", new_un):
            return JSONResponse({"error": "Invalid username"}, status_code=400)
        if any(u.get("username") == new_un and u["id"] != user_id for u in users):
            return JSONResponse({"error": "Username already taken"}, status_code=409)
        target["username"] = new_un
        changed = True
    if body.password is not None:
        if len(body.password) < 6:
            return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)
        salt = _new_salt()
        target["password_salt"] = salt
        target["password_hash"] = _hash_password(body.password, salt)
        changed = True
    if body.role is not None:
        if body.role not in ("user", "admin"):
            return JSONResponse({"error": "Role must be 'user' or 'admin'"}, status_code=400)
        # Block demoting the last remaining admin so we don't lock everyone out.
        if target["id"] == "admin" and body.role != "admin":
            return JSONResponse({"error": "The default admin cannot be demoted"}, status_code=400)
        admin_count = sum(1 for u in users if u.get("role") == "admin")
        if target.get("role") == "admin" and body.role != "admin" and admin_count <= 1:
            return JSONResponse({"error": "Cannot demote the only remaining admin"}, status_code=400)
        target["role"] = body.role
        changed = True
    if body.group is not None:
        target["group"] = body.group.strip()
        changed = True
    if changed:
        _save_users(users)
        # Re-apply per-user context (incl. the group block) for non-admin users.
        try:
            if not _is_admin(target):
                _ensure_user_claude_config_dir(target)
        except Exception:
            logger.debug("Failed to re-apply context after user update", exc_info=True)
    return JSONResponse({"ok": True, "user": _public_user(target)})


@app.delete("/api/admin/users/{user_id}")
async def api_admin_delete_user(request: Request, user_id: str):
    user = _current_user(request)
    if not _is_admin(user):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    if user_id == "admin":
        return JSONResponse({"error": "The default admin cannot be deleted"}, status_code=400)
    users = _load_users()
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        return JSONResponse({"error": "User not found"}, status_code=404)
    # Kill any tmux sessions this user owned (their content would otherwise
    # become orphaned and visible only to admins).
    owners = _load_session_owners()
    owned = [name for name, oid in owners.items() if oid == user_id]
    for name in owned:
        try:
            subprocess.run(["tmux", "kill-session", "-t", name],
                           capture_output=True, text=True, timeout=5)
        except Exception:
            logger.debug("Failed to kill session '%s' during user delete", name, exc_info=True)
        _clear_session_owner(name)
    # Remove the user record.
    users = [u for u in users if u["id"] != user_id]
    _save_users(users)
    # Tear down per-user data + Claude config dirs.
    try:
        data_dir = MESSAGES_DIR / "users" / user_id
        if data_dir.exists():
            shutil.rmtree(data_dir, ignore_errors=True)
    except Exception:
        logger.debug("Failed to remove user data dir for %s", user_id, exc_info=True)
    try:
        cfg_dir = Path.home() / f".claude-user-{user_id}"
        if cfg_dir.exists():
            shutil.rmtree(cfg_dir, ignore_errors=True)
    except Exception:
        logger.debug("Failed to remove user claude config for %s", user_id, exc_info=True)
    logger.info("Admin '%s' deleted user '%s' (and %d sessions)",
                user["username"], target.get("username", user_id), len(owned))
    return JSONResponse({"ok": True})


def _set_auth_cookie(resp, request: Request, token: str):
    is_https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    resp.set_cookie("tmux_auth", token, max_age=86400 * 30,
                    httponly=True, samesite="lax", secure=is_https)
    return resp


@app.post("/api/admin/users/{user_id}/impersonate")
async def api_admin_impersonate(request: Request, user_id: str):
    """Admin 'log in as' a user to see their work. Stashes the admin's own token
    in a side cookie so they can return; swaps tmux_auth to the target."""
    admin = _current_user(request)
    if not _is_admin(admin):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    target = next((u for u in _load_users() if u["id"] == user_id), None)
    if not target:
        return JSONResponse({"error": "User not found"}, status_code=404)
    if target["id"] == admin["id"]:
        return JSONResponse({"error": "That's already you"}, status_code=400)
    is_https = request.headers.get("x-forwarded-proto") == "https" or request.url.scheme == "https"
    resp = JSONResponse({"ok": True, "username": target.get("username", "")})
    # Keep the EARLIEST admin token if already impersonating, so a chain of
    # impersonations still returns to the real admin.
    orig = request.cookies.get("tmux_imp_orig")
    if not (orig and _is_admin(_user_from_token(orig))):
        orig = _make_token(admin["id"])
    resp.set_cookie("tmux_imp_orig", orig, max_age=86400,
                    httponly=True, samesite="lax", secure=is_https)
    _set_auth_cookie(resp, request, _make_token(target["id"]))
    logger.info("Admin '%s' is now impersonating '%s'", admin.get("username"), target.get("username"))
    return resp


@app.post("/api/unimpersonate")
async def api_unimpersonate(request: Request):
    """Return to the admin account. Authorized by possessing a valid admin token
    in the tmux_imp_orig cookie, so the impersonated (non-admin) session can call it."""
    orig = request.cookies.get("tmux_imp_orig")
    admin = _user_from_token(orig) if orig else None
    if not admin or not _is_admin(admin):
        return JSONResponse({"error": "Not impersonating"}, status_code=400)
    resp = JSONResponse({"ok": True, "username": admin.get("username", "")})
    _set_auth_cookie(resp, request, orig)
    resp.delete_cookie("tmux_imp_orig")
    logger.info("Returned to admin '%s' from impersonation", admin.get("username"))
    return resp


class SaveMyContextBody(BaseModel):
    content: str


def _my_context_path(user: dict, filename: str) -> Optional[Path]:
    """Resolve a writable per-user context file. Returns None for paths that
    would escape the user's Claude config dir."""
    base = _user_claude_config_dir(user)
    base.mkdir(parents=True, exist_ok=True)
    target = (base / filename).resolve()
    try:
        target.relative_to(base.resolve())
    except ValueError:
        return None
    return target


_MY_CONTEXT_ALLOWED = {"CLAUDE.md", "MEMORY.md", "settings.json"}


@app.get("/api/my/context")
async def api_my_context(request: Request):
    """Return current user's CLAUDE.md / MEMORY.md / settings.json contents."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    if not _is_admin(user):
        _ensure_user_claude_config_dir(user)
    out = {"dir": str(_user_claude_config_dir(user)), "files": []}
    for name in ("CLAUDE.md", "MEMORY.md", "settings.json"):
        p = _my_context_path(user, name)
        content = ""
        exists = False
        if p and p.exists():
            try:
                content = p.read_text()
                exists = True
            except Exception:
                logger.debug("Failed to read %s", p, exc_info=True)
        out["files"].append({"name": name, "content": content, "exists": exists, "path": str(p)})
    return JSONResponse(out)


@app.post("/api/my/context/{filename}")
async def api_my_context_save(request: Request, filename: str, body: SaveMyContextBody):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    if filename not in _MY_CONTEXT_ALLOWED:
        return JSONResponse({"error": "Not editable from this endpoint"}, status_code=400)
    if not _is_admin(user):
        _ensure_user_claude_config_dir(user)
    p = _my_context_path(user, filename)
    if p is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    try:
        # Validate settings.json before writing — Claude Code crashes hard
        # on invalid JSON in this file.
        if filename == "settings.json":
            try:
                json.loads(body.content or "{}")
            except json.JSONDecodeError as e:
                return JSONResponse({"error": f"Invalid JSON: {e.msg}"}, status_code=400)
        p.write_text(body.content or "")
        return JSONResponse({"ok": True, "path": str(p)})
    except Exception:
        logger.exception("Failed to save my-context %s for %s", filename, user["id"])
        return JSONResponse({"error": "Failed to save"}, status_code=500)


# --- History (per-user past sessions) ---

@app.get("/api/history")
async def api_history(request: Request):
    """List past sessions for the current user, with title/notes/last activity.

    A "session" here is any entry in the user's messages.json (current OR
    deleted). Each entry includes the Key Info (notes) so the history list
    can show it inline without a second round-trip.
    """
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    messages_by_session = _load_messages(user)
    notes_by_session = _load_all_notes(user)
    # Live cache might have newer in-memory entries for currently active sessions
    # this user owns; merge them in so the list reflects the most recent state.
    live_sessions = set()
    owners = _load_session_owners()
    for sess in get_tmux_sessions():
        if owners.get(sess["name"], "admin") == user["id"]:
            live_sessions.add(sess["name"])
    out = []
    all_names = set(messages_by_session.keys()) | set(notes_by_session.keys()) | live_sessions
    # Live sessions for the admin without explicit ownership records
    if _is_admin(user):
        for sess in get_tmux_sessions():
            if owners.get(sess["name"], "admin") == "admin":
                all_names.add(sess["name"])
                live_sessions.add(sess["name"])
    for name in all_names:
        msgs = messages_by_session.get(name) or []
        # If the session is currently in cache (memory), prefer the live list
        # so newly-sent messages show up without waiting for the next save.
        cache_entry = cache.get(name) or {}
        if cache_entry.get("messages"):
            msgs = cache_entry["messages"]
        notes = notes_by_session.get(name, "") or cache_entry.get("notes", "")
        title = cache_entry.get("title") or ""
        last_ts = 0
        user_msg_count = 0
        for m in msgs:
            if not isinstance(m, dict):
                continue
            ts = m.get("ts") or 0
            if ts > last_ts:
                last_ts = ts
            if m.get("role") == "user":
                user_msg_count += 1
        out.append({
            "session_name": name,
            "title": title,
            "key_info": notes,
            "user_message_count": user_msg_count,
            "total_messages": len(msgs),
            "last_message_at": last_ts,
            "is_live": name in live_sessions,
        })
    out.sort(key=lambda e: e["last_message_at"], reverse=True)
    return JSONResponse({"sessions": out})


@app.get("/api/history/{session_name}")
async def api_history_detail(request: Request, session_name: str):
    """Return all user messages + Key Info for a past session this user owns."""
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    # Ownership check (admins bypass)
    if not _is_admin(user):
        owner_id = _load_session_owners().get(session_name, "admin")
        if owner_id != user["id"]:
            return JSONResponse({"error": "Not found"}, status_code=404)
    # Prefer the live cache for currently-active sessions, fall back to disk.
    msgs: list = []
    cache_entry = cache.get(session_name) or {}
    if cache_entry.get("messages"):
        msgs = cache_entry["messages"]
    else:
        msgs = _load_messages(user).get(session_name, [])
    notes = ""
    if cache_entry.get("notes"):
        notes = cache_entry["notes"]
    else:
        notes = _load_all_notes(user).get(session_name, "")
    user_msgs = [
        {"text": m.get("text", ""), "ts": m.get("ts", 0)}
        for m in msgs
        if isinstance(m, dict) and m.get("role") == "user"
    ]
    return JSONResponse({
        "session_name": session_name,
        "key_info": notes,
        "user_messages": user_msgs,
        "total_user_messages": len(user_msgs),
    })


@app.get("/api/me")
async def api_me(request: Request):
    """Return the currently logged-in user (for the frontend to know who they are)."""
    user = _current_user(request)
    if not user:
        # Auth disabled (no AUTH_PASS) → expose a synthetic admin so the UI works.
        if not AUTH_PASS:
            return JSONResponse({
                "id": "admin", "username": AUTH_USER or "admin",
                "role": "admin", "auth_disabled": True,
                "team_mode": TEAM_MODE, "brand": BRAND_NAME, "simple": False,
            })
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    is_admin = _is_admin(user)
    # Impersonation: an admin "logged in as" this user has their real token in
    # the side cookie, so we can surface a "return to admin" banner.
    imp_orig = request.cookies.get("tmux_imp_orig")
    imp_admin = _user_from_token(imp_orig) if imp_orig else None
    impersonating = bool(imp_admin and _is_admin(imp_admin) and imp_admin["id"] != user["id"])
    return JSONResponse({
        "id": user["id"],
        "username": user.get("username", ""),
        "role": user.get("role", "user"),
        "auth_disabled": False,
        "team_mode": TEAM_MODE,
        "brand": BRAND_NAME,
        # "simple" = the heavily-stripped team UI shown to non-admin members.
        "simple": bool(TEAM_MODE and not is_admin),
        "impersonating": impersonating,
        "impersonator": imp_admin.get("username", "") if impersonating else "",
    })


# ===========================================================================
# NEMO-DEV team mode: shared auth, global context, soft sandbox, approvals,
# Google connections. All gated behind TEAM_MODE; no effect on personal boxes.
# ===========================================================================
import base64
import urllib.request
import urllib.parse

SHARED_CREDENTIALS = Path.home() / ".claude" / ".credentials.json"
GLOBAL_CONTEXT_FILE = MESSAGES_DIR / "nemo-global-context.md"
SANDBOX_HOOK_PATH = MESSAGES_DIR / "hooks" / "nemo_sandbox_guard.py"
APPROVALS_FILE = MESSAGES_DIR / "approvals.json"
CONNECTIONS_DIR = MESSAGES_DIR / "connections"
GOOGLE_OAUTH_CLIENT_FILE = MESSAGES_DIR / "google_oauth_client.json"

_GLOBAL_CTX_BEGIN = "<!-- NEMO-DEV GLOBAL CONTEXT (managed — edits below are overwritten) -->"
_GLOBAL_CTX_END = "<!-- END NEMO-DEV GLOBAL CONTEXT -->"

_DEFAULT_GLOBAL_CONTEXT = """# NEMO-DEV environment (shared global context)
You are running inside **NEMO-DEV**, a shared team development environment on the
server `dianaotech.com`. Several team members share this machine; each has their
own private workspace, memory, and context below this block.

## Hard rules — soft sandbox
- You may freely read, create, and change files and run services **on this server only**.
- Do **NOT** modify, deploy to, SSH into, or change configuration on any OTHER
  server or cloud resource (other GCP VMs, other hosts, production systems, buckets).
- Reaching sensitive data on other servers is blocked by a guard. If you truly need
  it, the block automatically emails the admin for approval — do not try to work
  around it (no obfuscation, no alternate tooling).
- Remote/cloud tools (`gcloud`, `gsutil`, `bq`, `ssh`, `scp`, `kubectl`, `aws`, the
  GCP metadata server, …) are restricted; expect them to be denied unless approved.

## Projects & working folder
- Unless told otherwise, publish every project you build at https://dianaotech.com/<username>/<project>.
- Default <project> = the current tmux session name (unless that name is taken or you're
  told another). Example: user "coffee" in session "XYABC" asks for a calculator app →
  build it and publish it publicly at https://dianaotech.com/coffee/XYABC.
- HOW TO PUBLISH: put the project's web files in `$NEMO_PROJECT_DIR` (= `~/nemo-projects/<username>/<project>/`,
  exported in your shell; create it). Static sites: write `index.html` (+ assets) there and it's
  served immediately at `$NEMO_PROJECT_URL`. Dynamic apps (Node/Flask/etc.): run your server on a
  free port and write `$NEMO_PROJECT_DIR/.serve.json` = `{"port": <PORT>}`; it'll be reverse-proxied there.
- Your username is `$NEMO_USER`, this session is `$NEMO_SESSION`, and the live link is `$NEMO_PROJECT_URL`
  (also shown as a clickable link in the dashboard for this session).

Stay focused on the user's project in this workspace.
"""


def _html_escape(s: str) -> str:
    return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _ensure_global_context_file():
    GLOBAL_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not GLOBAL_CONTEXT_FILE.exists():
        GLOBAL_CONTEXT_FILE.write_text(_DEFAULT_GLOBAL_CONTEXT)


def _read_global_context() -> str:
    _ensure_global_context_file()
    try:
        return GLOBAL_CONTEXT_FILE.read_text()
    except Exception:
        return ""


def _sync_global_context_into(claude_md: Path):
    """Keep a managed global-context block at the TOP of a user's CLAUDE.md,
    preserving the user's own content below the END marker."""
    block = _GLOBAL_CTX_BEGIN + "\n" + _read_global_context().rstrip() + "\n" + _GLOBAL_CTX_END + "\n"
    existing = ""
    if claude_md.exists():
        try:
            existing = claude_md.read_text()
        except Exception:
            existing = ""
    if _GLOBAL_CTX_BEGIN in existing and _GLOBAL_CTX_END in existing:
        pre = existing.split(_GLOBAL_CTX_BEGIN, 1)[0]
        post = existing.split(_GLOBAL_CTX_END, 1)[1]
        user_part = (pre + post).lstrip("\n")
    else:
        user_part = existing.lstrip("\n")
    claude_md.write_text(block + "\n" + user_part)


_PROJ_NOTE_BEGIN = "<!-- NEMO-DEV PROJECTS CONVENTION (managed) -->"
_PROJ_NOTE_END = "<!-- END NEMO-DEV PROJECTS CONVENTION -->"
_PROJ_NOTE = """## Projects & working folder
- Publish projects at https://dianaotech.com/<username>/<project> (default <project> = the current tmux session name).
- Put the project's web files in `$NEMO_PROJECT_DIR` (= `~/nemo-projects/<username>/<project>/`); static files are served immediately at `$NEMO_PROJECT_URL`. For a dynamic app, run your server on a free port and write `$NEMO_PROJECT_DIR/.serve.json` = `{"port": <PORT>}` to have it reverse-proxied there.
- This session: user `$NEMO_USER`, link `$NEMO_PROJECT_URL` (also shown as a clickable link in the dashboard)."""


def _sync_projects_note_into(claude_md: Path):
    """Add a managed projects-convention block at the top of a config's CLAUDE.md
    (used for admins, who don't receive the member global block)."""
    existing = claude_md.read_text() if claude_md.exists() else ""
    if _PROJ_NOTE_BEGIN in existing and _PROJ_NOTE_END in existing:
        pre = existing.split(_PROJ_NOTE_BEGIN, 1)[0]
        post = existing.split(_PROJ_NOTE_END, 1)[1]
        existing = (pre + post).lstrip("\n")
    else:
        existing = existing.lstrip("\n")
    block = _PROJ_NOTE_BEGIN + "\n" + _PROJ_NOTE + "\n" + _PROJ_NOTE_END + "\n"
    try:
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        claude_md.write_text(block + "\n" + existing)
    except Exception:
        logger.debug("Failed to sync projects note into %s", claude_md, exc_info=True)


_GIT_RULES_BEGIN = "<!-- NEMO-DEV GIT RULES (managed) -->"
_GIT_RULES_END = "<!-- END NEMO-DEV GIT RULES -->"
_GIT_RULES = """## Git on a shared machine (multiple people, one box)
Several teammates work on this server as the same OS user, so be disciplined:
- **Identity is preset** — your commits are authored as `$GIT_AUTHOR_NAME <$GIT_AUTHOR_EMAIL>` (= your dashboard username). Do NOT change git `user.name`/`user.email` or pass `--author`; let the env vars stand so attribution is correct.
- **Stay in your own space** — work inside this session's cwd / `$NEMO_PROJECT_DIR`. Never edit files in another member's project dir (`~/nemo-projects/<someone-else>/...`).
- **Branch, never commit to a shared branch** — always work on a feature branch named `$NEMO_USER/<short-topic>`. Never commit directly to `main`/`master` or to a branch someone else is using.
- **Sync before you start** — `git fetch` + rebase/merge latest so you're not building on stale code. Resolve conflicts cleanly.
- **Push your branch, open a PR** — let the repo owner review/merge. **NEVER force-push** `main` or any shared branch.
- **Isolate when sharing a repo** — if a teammate is already working in a repo's working tree, don't fight over it: make your own worktree — `git worktree add ../<repo>-$NEMO_USER -b $NEMO_USER/<topic>` — and work there.
- **Never commit secrets** (.env, tokens, keys). Check `git status` before committing."""


def _sync_git_rules_into(claude_md: Path):
    """Maintain a managed GIT RULES block in a CLAUDE.md (members + admins). Placed
    just under the projects note / top so it's always current regardless of edits."""
    existing = claude_md.read_text() if claude_md.exists() else ""
    if _GIT_RULES_BEGIN in existing and _GIT_RULES_END in existing:
        pre = existing.split(_GIT_RULES_BEGIN, 1)[0]
        post = existing.split(_GIT_RULES_END, 1)[1]
        existing = (pre.rstrip("\n") + "\n" + post.lstrip("\n"))
    block = _GIT_RULES_BEGIN + "\n" + _GIT_RULES + "\n" + _GIT_RULES_END + "\n"
    # Insert after the projects-note block if present, else prepend.
    try:
        claude_md.parent.mkdir(parents=True, exist_ok=True)
        if _PROJ_NOTE_END in existing:
            head, tail = existing.split(_PROJ_NOTE_END, 1)
            claude_md.write_text(head + _PROJ_NOTE_END + "\n\n" + block + tail.lstrip("\n"))
        else:
            claude_md.write_text(block + "\n" + existing.lstrip("\n"))
    except Exception:
        logger.debug("Failed to sync git rules into %s", claude_md, exc_info=True)


def _setup_shared_git_config():
    """Set safe, friction-reducing git defaults once for the shared OS user so
    multi-user work behaves predictably. Idempotent (git config is declarative)."""
    defaults = [
        ("push.default", "current"),
        ("push.autoSetupRemote", "true"),
        ("pull.rebase", "false"),
        ("init.defaultBranch", "main"),
        ("rerere.enabled", "true"),
        ("merge.conflictStyle", "zdiff3"),
    ]
    for k, v in defaults:
        try:
            subprocess.run(["git", "config", "--global", k, v],
                           capture_output=True, text=True, timeout=5)
        except Exception:
            logger.debug("git config --global %s failed", k, exc_info=True)
    # A global ignore so per-user/editor noise never gets committed by accident.
    try:
        gi = Path.home() / ".gitignore_global"
        if not gi.exists():
            gi.write_text(".DS_Store\n*.swp\n.serve.json\n.nemo_primed\nnode_modules/\n__pycache__/\n.venv/\n")
        subprocess.run(["git", "config", "--global", "core.excludesfile", str(gi)],
                       capture_output=True, text=True, timeout=5)
    except Exception:
        logger.debug("global gitignore setup failed", exc_info=True)


def _share_credentials_symlink(cfg_dir: Path):
    """Point a user's .credentials.json at the shared admin token so one login
    authenticates everyone. A single file = a single refresh token, which avoids
    the OAuth rotation war that divergent copies would cause."""
    try:
        link = cfg_dir / ".credentials.json"
        if link.is_symlink():
            try:
                if os.readlink(link) == str(SHARED_CREDENTIALS):
                    return
            except OSError:
                pass
            link.unlink()
        elif link.exists():
            link.unlink()
        link.symlink_to(SHARED_CREDENTIALS)
    except Exception:
        logger.debug("Failed to symlink shared credentials into %s", cfg_dir, exc_info=True)


def _approve_anthropic_key(cfg_dir: Path, key: str):
    """Pre-approve a shared ANTHROPIC_API_KEY in the config dir's settings.json so
    Claude Code doesn't interactively prompt 'Detected a custom API key — use it?'
    (which defaults to No). Claude matches on the key's last 20 chars."""
    if not key:
        return
    suffix = key[-20:]
    sp = cfg_dir / "settings.json"
    try:
        s = json.loads(sp.read_text()) if sp.exists() else {}
        if not isinstance(s, dict):
            s = {}
    except Exception:
        s = {}
    car = s.get("customApiKeyResponses")
    if not isinstance(car, dict):
        car = {}
    approved = car.get("approved") if isinstance(car.get("approved"), list) else []
    if suffix not in approved:
        approved.append(suffix)
    car["approved"] = approved
    if not isinstance(car.get("rejected"), list):
        car["rejected"] = []
    s["customApiKeyResponses"] = car
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(s, indent=2))
    except Exception:
        logger.debug("Failed to write customApiKeyResponses into %s", sp, exc_info=True)


def _set_api_key_helper(cfg_dir: Path):
    """Point Claude Code at the shared API key via settings.json `apiKeyHelper`.
    Interactive claude does NOT honor a bare ANTHROPIC_API_KEY env var for
    inference (it falls back to /login), but apiKeyHelper authenticates reliably —
    and the key stays in a 0600 file rather than the terminal scrollback."""
    sp = cfg_dir / "settings.json"
    try:
        s = json.loads(sp.read_text()) if sp.exists() else {}
        if not isinstance(s, dict):
            s = {}
    except Exception:
        s = {}
    s["apiKeyHelper"] = "cat " + shlex.quote(str(ANTHROPIC_API_KEY_FILE))
    try:
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text(json.dumps(s, indent=2))
    except Exception:
        logger.debug("Failed to set apiKeyHelper in %s", sp, exc_info=True)


def _remove_subscription_creds(cfg_dir: Path):
    """In API-key mode, drop any (dead) subscription .credentials.json so claude
    uses the API key instead of trying the expired OAuth token and hitting /login."""
    p = cfg_dir / ".credentials.json"
    try:
        if p.is_symlink() or p.exists():
            p.unlink()
    except Exception:
        logger.debug("Failed to remove subscription creds in %s", cfg_dir, exc_info=True)


def _apply_api_key_auth(cfg_dir: Path):
    """Configure a config dir to authenticate via the shared API key."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _remove_subscription_creds(cfg_dir)
    _set_api_key_helper(cfg_dir)
    _approve_anthropic_key(cfg_dir, _stored_anthropic_key)


def _subscription_token_valid() -> bool:
    """True when the shared admin subscription token (~/.claude/.credentials.json)
    exists and isn't expired — i.e. the Max/Pro PLAN is usable for members."""
    try:
        o = json.loads(SHARED_CREDENTIALS.read_text()).get("claudeAiOauth", {})
        return bool(o) and int(o.get("expiresAt") or 0) > int(time.time() * 1000)
    except Exception:
        return False


def _remove_api_key_helper(cfg_dir: Path):
    """Strip apiKeyHelper + customApiKeyResponses so claude uses the (symlinked)
    subscription token instead of the metered API key."""
    sp = cfg_dir / "settings.json"
    try:
        s = json.loads(sp.read_text()) if sp.exists() else {}
        if not isinstance(s, dict):
            return
    except Exception:
        return
    if "apiKeyHelper" in s or "customApiKeyResponses" in s:
        s.pop("apiKeyHelper", None)
        s.pop("customApiKeyResponses", None)
        try:
            sp.write_text(json.dumps(s, indent=2))
        except Exception:
            logger.debug("Failed to strip apiKeyHelper from %s", sp, exc_info=True)


def _apply_subscription_auth(cfg_dir: Path):
    """Configure a config dir to authenticate via the shared subscription PLAN:
    symlink .credentials.json to the admin token and remove API-key settings."""
    cfg_dir.mkdir(parents=True, exist_ok=True)
    _remove_api_key_helper(cfg_dir)
    _share_credentials_symlink(cfg_dir)


def _disable_claude_ai_connectors(cfg_dir: Path):
    """Turn off the claude.ai ACCOUNT-level connectors (Drive/Gmail/Calendar that
    ride the shared plan account, `mcp__claude_ai_*`). On the shared plan those are
    the admin account's data — a leak into member sessions, and members can't
    re-auth them when they expire. Members use our per-user `google` MCP instead.
    Requires Claude Code >= 2.1.182. Custom mcpServers are unaffected."""
    sp = cfg_dir / "settings.json"
    try:
        s = json.loads(sp.read_text()) if sp.exists() else {}
        if not isinstance(s, dict):
            s = {}
    except Exception:
        s = {}
    if s.get("disableClaudeAiConnectors") is not True:
        s["disableClaudeAiConnectors"] = True
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(s, indent=2))
        except Exception:
            logger.debug("Failed to set disableClaudeAiConnectors in %s", sp, exc_info=True)


def _set_team_model_effort(cfg_dir: Path):
    """Pin the team default model + reasoning effort (Opus 4.8, max effort) into a
    config dir's settings.json so every session launches on it. Claude Code reads
    `model` from settings and CLAUDE_CODE_EFFORT_LEVEL from settings `env`."""
    sp = cfg_dir / "settings.json"
    try:
        s = json.loads(sp.read_text()) if sp.exists() else {}
        if not isinstance(s, dict):
            s = {}
    except Exception:
        s = {}
    changed = False
    if TEAM_MODEL and s.get("model") != TEAM_MODEL:
        s["model"] = TEAM_MODEL
        changed = True
    if TEAM_EFFORT:
        env = s.get("env") if isinstance(s.get("env"), dict) else {}
        if env.get("CLAUDE_CODE_EFFORT_LEVEL") != TEAM_EFFORT:
            env["CLAUDE_CODE_EFFORT_LEVEL"] = TEAM_EFFORT
            s["env"] = env
            changed = True
    if changed:
        try:
            sp.parent.mkdir(parents=True, exist_ok=True)
            sp.write_text(json.dumps(s, indent=2))
        except Exception:
            logger.debug("Failed to set team model/effort in %s", sp, exc_info=True)


def _apply_member_auth(cfg_dir: Path) -> str:
    """Member auth = the shared subscription PLAN, always (per Nimo: use the plan,
    never the metered API). Symlinks .credentials.json to the admin's plan token and
    strips any apiKeyHelper so we can't accidentally fall back to the API key. The
    metered-API path is intentionally NOT used here."""
    _apply_subscription_auth(cfg_dir)
    return "subscription"


def _seed_trust(cfg_dir: Path, cwd: str):
    """Pre-accept Claude Code's per-folder trust dialog for `cwd` in this config
    dir's .claude.json so it doesn't prompt (which would hang a detached session)."""
    cj = cfg_dir / ".claude.json"
    try:
        d = json.loads(cj.read_text()) if cj.exists() else {}
        if not isinstance(d, dict):
            d = {}
    except Exception:
        d = {}
    projects = d.get("projects") if isinstance(d.get("projects"), dict) else {}
    proj = projects.get(cwd) if isinstance(projects.get(cwd), dict) else {}
    proj["hasTrustDialogAccepted"] = True
    projects[cwd] = proj
    d["projects"] = projects
    d.setdefault("hasCompletedOnboarding", True)
    try:
        cj.write_text(json.dumps(d, indent=2))
    except Exception:
        logger.debug("Failed to seed trust into %s", cj, exc_info=True)


# A standalone primer that drives a throwaway tmux session WITH an attached pty
# client to accept the one-time "Bypass Permissions" warning. A detached session
# (no client) can't confirm it and claude exits, so members would otherwise hang.
PRIME_SCRIPT_PATH = MESSAGES_DIR / "hooks" / "prime_claude.sh"
_PRIME_SCRIPT = r'''#!/usr/bin/env bash
# Accept the one-time --dangerously-skip-permissions warning for a config dir.
CFG="$1"
MARKER="$CFG/.nemo_primed"
[ -f "$MARKER" ] && { echo "already primed"; exit 0; }
KEY="$(cat "$NEMO_KEY_FILE" 2>/dev/null)"
S="prime_$$"
tmux kill-session -t "$S" 2>/dev/null
tmux new-session -d -s "$S" -x 200 -y 50 -c "$PWD" || exit 1
# Subscription mode (no key): rely on the config dir's symlinked plan creds.
if [ -n "$KEY" ]; then PRE="export ANTHROPIC_API_KEY=$KEY; "; else PRE="unset ANTHROPIC_API_KEY; "; fi
tmux send-keys -t "$S" "${PRE}export CLAUDE_CONFIG_DIR=$CFG; claude --dangerously-skip-permissions" Enter
# Attach a pty client in the background so claude sees an interactive terminal.
setsid bash -c "script -qfc 'tmux attach -t $S' /dev/null" >/dev/null 2>&1 &
ok=0
for i in $(seq 1 45); do
  pane="$(tmux capture-pane -t "$S" -p 2>/dev/null)"
  if echo "$pane" | grep -q "bypass permissions on"; then ok=1; break; fi
  if echo "$pane" | grep -q "Yes, I accept"; then
    tmux send-keys -t "$S" Down; sleep 1; tmux send-keys -t "$S" Enter; sleep 2
  fi
  sleep 1
done
tmux send-keys -t "$S" C-c 2>/dev/null
sleep 1
tmux kill-session -t "$S" 2>/dev/null
pkill -f "tmux attach -t $S" 2>/dev/null
if [ "$ok" = "1" ]; then date +%s > "$MARKER"; echo "primed"; exit 0; fi
echo "prime failed"; exit 1
'''


def _write_prime_script():
    PRIME_SCRIPT_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if (not PRIME_SCRIPT_PATH.exists()) or PRIME_SCRIPT_PATH.read_text() != _PRIME_SCRIPT:
            PRIME_SCRIPT_PATH.write_text(_PRIME_SCRIPT)
        PRIME_SCRIPT_PATH.chmod(0o755)
    except Exception:
        logger.debug("Failed to write prime script", exc_info=True)


def _prime_claude_config(cfg_dir: Path) -> bool:
    """One-time per config dir: accept the bypass-permissions warning so detached
    sessions launch cleanly. Idempotent via a marker file. Works in subscription
    mode (symlinked plan creds) or, as a legacy path, with a stored API key."""
    if not _subscription_token_valid() and not _stored_anthropic_key:
        return False
    marker = cfg_dir / ".nemo_primed"
    if marker.exists():
        return True
    try:
        cfg_dir.mkdir(parents=True, exist_ok=True)
        _seed_trust(cfg_dir, os.getcwd())
        if _stored_anthropic_key:
            _approve_anthropic_key(cfg_dir, _stored_anthropic_key)
        _write_prime_script()
        env = dict(os.environ,
                   NEMO_KEY_FILE=str(ANTHROPIC_API_KEY_FILE),
                   PATH=os.environ.get("PATH", "") + ":/usr/local/bin:/usr/bin")
        subprocess.run(["bash", str(PRIME_SCRIPT_PATH), str(cfg_dir)],
                       cwd=os.getcwd(), env=env, capture_output=True, text=True, timeout=90)
    except Exception:
        logger.debug("prime_claude_config failed for %s", cfg_dir, exc_info=True)
    return marker.exists()


_SANDBOX_HOOK_SCRIPT = r'''#!/usr/bin/env python3
# NEMO-DEV soft-sandbox guard (auto-generated; do not edit).
# PreToolUse hook: blocks actions that touch OTHER servers / cloud resources and
# routes them to the admin for approval. Local changes on this server are allowed.
import sys, json, os, re, urllib.request

DASH_URL = os.environ.get("NEMO_DASH_URL", "__DASH_URL__")

BLOCK_PATTERNS = [
    r"\bgcloud\b", r"\bgsutil\b", r"\bbq\b", r"\bkubectl\b", r"\bhelm\b",
    r"\bssh\b", r"\bscp\b", r"\bsftp\b", r"\bsshpass\b", r"\bmosh\b",
    r"\bdoctl\b", r"\baws\b", r"\baz\b", r"\bterraform\b",
    r"169\.254\.169\.254", r"metadata\.google\.internal",
    r"\brsync\b[^\n]*::", r"\brsync\b[^\n]*@",
]
BLOCK_RE = [re.compile(p, re.I) for p in BLOCK_PATTERNS]


def extract_text(tool_input):
    if not isinstance(tool_input, dict):
        return ""
    parts = []
    for k in ("command", "cmd", "script", "url"):
        v = tool_input.get(k)
        if isinstance(v, str):
            parts.append(v)
    return "\n".join(parts)


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    tool_name = data.get("tool_name", "")
    text = extract_text(data.get("tool_input", {}))
    if not text or not any(rx.search(text) for rx in BLOCK_RE):
        sys.exit(0)
    cfg = os.environ.get("CLAUDE_CONFIG_DIR", "")
    uid = ""
    if ".claude-user-" in cfg:
        uid = cfg.split(".claude-user-", 1)[1].strip("/").split("/")[0]
    payload = json.dumps({
        "user_id": uid, "tool": tool_name,
        "command": text[:4000], "cwd": data.get("cwd", os.getcwd()),
    }).encode()
    try:
        req = urllib.request.Request(
            DASH_URL.rstrip("/") + "/api/sandbox/check",
            data=payload, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            resp = json.load(r)
    except Exception:
        print("NEMO-DEV sandbox: cross-server action blocked (approval service "
              "unreachable). This server only — other servers are off-limits.",
              file=sys.stderr)
        sys.exit(2)
    if resp.get("decision") == "allow":
        sys.exit(0)
    print(resp.get("reason") or
          "NEMO-DEV sandbox: this targets another server and is blocked; the admin "
          "was asked to approve. Do not attempt to bypass it.", file=sys.stderr)
    sys.exit(2)


main()
'''


def _write_sandbox_hook_script():
    SANDBOX_HOOK_PATH.parent.mkdir(parents=True, exist_ok=True)
    content = _SANDBOX_HOOK_SCRIPT.replace("__DASH_URL__", DASH_LOCAL_URL)
    try:
        if (not SANDBOX_HOOK_PATH.exists()) or SANDBOX_HOOK_PATH.read_text() != content:
            SANDBOX_HOOK_PATH.write_text(content)
        SANDBOX_HOOK_PATH.chmod(0o755)
    except Exception:
        logger.debug("Failed to write sandbox hook", exc_info=True)


def _install_sandbox_hook(cfg_dir: Path, user: dict):
    """Register the guard as a PreToolUse(Bash) hook in the user's settings.json."""
    _write_sandbox_hook_script()
    settings_path = cfg_dir / "settings.json"
    try:
        settings = json.loads(settings_path.read_text()) if settings_path.exists() else {}
        if not isinstance(settings, dict):
            settings = {}
    except Exception:
        settings = {}
    hook_cmd = "python3 " + shlex.quote(str(SANDBOX_HOOK_PATH))
    entry = {"matcher": "Bash|WebFetch", "hooks": [{"type": "command", "command": hook_cmd}]}
    hooks = settings.get("hooks") if isinstance(settings.get("hooks"), dict) else {}
    pre = hooks.get("PreToolUse")
    if not isinstance(pre, list):
        pre = []
    pre = [h for h in pre if "nemo_sandbox_guard" not in json.dumps(h)]
    pre.append(entry)
    hooks["PreToolUse"] = pre
    settings["hooks"] = hooks
    try:
        settings_path.write_text(json.dumps(settings, indent=2))
    except Exception:
        logger.debug("Failed to install sandbox hook into %s", settings_path, exc_info=True)


# --- email (Resend) --------------------------------------------------------
def _send_email(subject: str, html_body: str, to: Optional[str] = None) -> bool:
    to = to or ADMIN_APPROVAL_EMAIL
    if not RESEND_API_KEY:
        logger.warning("NEMO-DEV email not sent (no RESEND_API_KEY): %s", subject)
        return False
    payload = json.dumps({
        "from": MAIL_FROM, "to": [to], "subject": subject,
        "html": html_body, "text": re.sub("<[^>]+>", "", html_body),
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload,
            headers={"Authorization": "Bearer " + RESEND_API_KEY,
                     "Content-Type": "application/json",
                     # Resend is behind Cloudflare, which 403s (error 1010) the
                     # default Python-urllib User-Agent. Send a normal one.
                     "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) NEMO-DEV/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            r.read()
        return True
    except Exception:
        logger.exception("Failed to send email via Resend")
        return False


# --- approval store --------------------------------------------------------
def _load_approvals() -> dict:
    try:
        d = json.loads(APPROVALS_FILE.read_text())
        return d if isinstance(d, dict) else {"requests": {}}
    except Exception:
        return {"requests": {}}


def _save_approvals(data: dict):
    try:
        APPROVALS_FILE.parent.mkdir(parents=True, exist_ok=True)
        APPROVALS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        logger.exception("Failed to save approvals")


def _approval_key(user_id: str, command: str) -> str:
    norm = re.sub(r"\s+", " ", (command or "").strip())
    return hashlib.sha256((str(user_id) + "|" + norm).encode()).hexdigest()[:16]


def _notify_admin_approval(rec: dict):
    base = PUBLIC_BASE_URL.rstrip("/")
    who = rec.get("username") or rec.get("user_id") or "a user"
    subj = "[NEMO-DEV] Approval needed: " + who + " requested cross-server access"
    html = (
        '<div style="font-family:-apple-system,Segoe UI,sans-serif;max-width:640px">'
        "<h2>NEMO-DEV — cross-server action blocked</h2>"
        "<p>A team member's Claude session tried something that reaches another "
        "server. It was blocked and is awaiting your approval.</p>"
        "<p><b>User:</b> " + _html_escape(who) + " (" + _html_escape(rec.get("user_id", "")) + ")<br>"
        "<b>Tool:</b> " + _html_escape(rec.get("tool", "")) + "<br>"
        "<b>Working dir:</b> " + _html_escape(rec.get("cwd", "")) + "</p>"
        "<p><b>Command:</b></p><pre style=\"background:#f4f4f4;padding:10px;border-radius:6px;"
        "white-space:pre-wrap;word-break:break-all\">" + _html_escape((rec.get("command") or "")[:2000]) + "</pre>"
        '<p>Log in to <a href="' + (base or "#") + '">NEMO-DEV</a> and open the '
        "<b>Approvals</b> panel (gear menu) to approve or deny.</p></div>"
    )
    _send_email(subj, html)


@app.post("/api/sandbox/check")
async def api_sandbox_check(request: Request):
    """Called by the per-user PreToolUse guard hook (localhost only). Records the
    blocked attempt, notifies the admin once, and reports allow/deny."""
    if request.client and request.client.host not in ("127.0.0.1", "::1", "localhost"):
        return JSONResponse({"decision": "deny", "reason": "forbidden"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    user_id = (body.get("user_id") or "").strip()
    command = body.get("command") or ""
    cwd = body.get("cwd") or ""
    tool = body.get("tool") or ""
    key = _approval_key(user_id, command)
    data = _load_approvals()
    reqs = data.setdefault("requests", {})
    rec = reqs.get(key)
    now = time.time()
    if rec and rec.get("status") == "approved" and now - rec.get("decided_at", 0) < 3600:
        return JSONResponse({"decision": "allow"})
    if rec and rec.get("status") == "denied" and now - rec.get("decided_at", 0) < 600:
        return JSONResponse({"decision": "deny",
                             "reason": "NEMO-DEV: the admin denied this cross-server action."})
    u = _find_user_by_id(user_id) if user_id else None
    is_new = rec is None
    reqs[key] = {
        "key": key, "user_id": user_id,
        "username": (u or {}).get("username", "") if u else "",
        "tool": tool, "command": command[:4000], "cwd": cwd,
        "status": "pending",
        "created_at": rec.get("created_at", now) if rec else now,
        "last_seen": now,
        "count": (rec.get("count", 0) + 1) if rec else 1,
    }
    _save_approvals(data)
    if is_new:
        try:
            _notify_admin_approval(reqs[key])
        except Exception:
            logger.exception("Failed to notify admin of approval request")
    return JSONResponse({"decision": "deny", "reason":
        "NEMO-DEV sandbox: this action targets another server and is blocked. A "
        "request was sent to the admin (" + ADMIN_APPROVAL_EMAIL + ") for approval. "
        "Keep working on things that stay on this server; do not try to bypass."})


@app.get("/api/approvals")
async def api_list_approvals(request: Request):
    user = _current_user(request)
    if not _is_admin(user):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    reqs = list(_load_approvals().get("requests", {}).values())
    reqs.sort(key=lambda r: r.get("last_seen", 0), reverse=True)
    return JSONResponse({"requests": reqs})


@app.post("/api/approvals/{key}/{action}")
async def api_decide_approval(request: Request, key: str, action: str):
    user = _current_user(request)
    if not _is_admin(user):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    if action not in ("approve", "deny"):
        return JSONResponse({"error": "Bad action"}, status_code=400)
    data = _load_approvals()
    rec = data.get("requests", {}).get(key)
    if not rec:
        return JSONResponse({"error": "Not found"}, status_code=404)
    rec["status"] = "approved" if action == "approve" else "denied"
    rec["decided_at"] = time.time()
    rec["decided_by"] = user.get("username", "admin")
    _save_approvals(data)
    return JSONResponse({"ok": True, "status": rec["status"]})


# --- Google connections (Drive / Gmail / Calendar) -------------------------
GOOGLE_SCOPES = {
    "drive": ["https://www.googleapis.com/auth/drive.readonly"],
    "gmail": ["https://www.googleapis.com/auth/gmail.readonly"],
    "calendar": ["https://www.googleapis.com/auth/calendar.readonly"],
}
GOOGLE_LABELS = {"drive": "Google Drive", "gmail": "Gmail", "calendar": "Google Calendar"}


def _google_client():
    cid = os.environ.get("GOOGLE_OAUTH_CLIENT_ID", "")
    csec = os.environ.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
    if (not cid or not csec) and GOOGLE_OAUTH_CLIENT_FILE.exists():
        try:
            j = json.loads(GOOGLE_OAUTH_CLIENT_FILE.read_text())
            j = j.get("web") or j.get("installed") or j
            cid = cid or j.get("client_id", "")
            csec = csec or j.get("client_secret", "")
        except Exception:
            logger.debug("Failed to read Google OAuth client file", exc_info=True)
    return cid, csec


def _conn_path(user_id: str, service: str) -> Path:
    return CONNECTIONS_DIR / str(user_id) / (service + ".json")


def _sign_state(payload: str) -> str:
    sig = hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
    return base64.urlsafe_b64encode((payload + "|" + sig).encode()).decode()


def _verify_state(state: str) -> Optional[str]:
    try:
        raw = base64.urlsafe_b64decode(state.encode()).decode()
        payload, sig = raw.rsplit("|", 1)
        exp = hmac.new(AUTH_SECRET.encode(), payload.encode(), hashlib.sha256).hexdigest()[:24]
        if hmac.compare_digest(sig, exp):
            return payload
    except Exception:
        pass
    return None


def _callback_uri(request: Request) -> str:
    base = PUBLIC_BASE_URL.rstrip("/") or str(request.base_url).rstrip("/")
    return base + ROOT_PATH + "/api/connections/google/callback"


def _ensure_google_mcp(cfg_dir: Path, user: dict):
    """Register the single `google` MCP server (Drive/Gmail/Calendar) in this user's
    .claude.json so their Claude has the tools. The server reads the user's per-user
    OAuth tokens at call time; tools return a friendly 'connect first' message until
    the user connects a service. No-op if GOOGLE_MCP_COMMAND isn't configured."""
    cmd = os.environ.get("GOOGLE_MCP_COMMAND", "")
    if not cmd or not user or not user.get("id"):
        return
    cj = cfg_dir / ".claude.json"
    try:
        data = json.loads(cj.read_text()) if cj.exists() else {}
        if not isinstance(data, dict):
            data = {}
    except Exception:
        data = {}
    parts = shlex.split(cmd)
    servers = data.get("mcpServers") if isinstance(data.get("mcpServers"), dict) else {}
    servers["google"] = {
        "command": parts[0],
        "args": parts[1:],
        "env": {
            "GOOGLE_MCP_CREDENTIALS_DIR": str(CONNECTIONS_DIR / user["id"]),
            "GOOGLE_OAUTH_CLIENT_FILE": str(GOOGLE_OAUTH_CLIENT_FILE),
        },
    }
    data["mcpServers"] = servers
    try:
        cj.write_text(json.dumps(data, indent=2))
    except Exception:
        logger.debug("Failed to write google MCP entry into %s", cj, exc_info=True)


def _write_google_mcp(user: dict, service: str):
    """Called after a successful connect; ensures the google MCP server is registered."""
    _ensure_google_mcp(_user_claude_config_dir(user), user)


@app.get("/api/connections")
async def api_connections(request: Request):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    cid, _ = _google_client()
    out = {
        "configured": bool(cid),
        "mcp_ready": bool(os.environ.get("GOOGLE_MCP_COMMAND", "")),
        "services": [],
    }
    for svc in ("drive", "gmail", "calendar"):
        out["services"].append({
            "id": svc, "label": GOOGLE_LABELS[svc],
            "connected": _conn_path(user["id"], svc).exists(),
        })
    return JSONResponse(out)


@app.get("/api/connections/{service}/start")
async def api_connection_start(request: Request, service: str):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    if service not in GOOGLE_SCOPES:
        return JSONResponse({"error": "Unknown service"}, status_code=400)
    cid, csec = _google_client()
    if not cid or not csec:
        return JSONResponse({"error": "Google connections are not configured yet. "
                             "Ask the admin to add the OAuth client."}, status_code=503)
    state = _sign_state(user["id"] + ":" + service + ":" + str(int(time.time())))
    params = urllib.parse.urlencode({
        "client_id": cid,
        "redirect_uri": _callback_uri(request),
        "response_type": "code",
        "scope": " ".join(GOOGLE_SCOPES[service]),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
        "state": state,
    })
    return RedirectResponse("https://accounts.google.com/o/oauth2/v2/auth?" + params)


@app.get("/api/connections/google/callback")
async def api_connection_callback(request: Request):
    if request.query_params.get("error"):
        return RedirectResponse(ROOT_PATH + "/?connect=denied")
    code = request.query_params.get("code")
    payload = _verify_state(request.query_params.get("state") or "")
    if not code or not payload:
        return HTMLResponse("Invalid OAuth state", status_code=400)
    try:
        user_id, service, ts = payload.split(":")
    except ValueError:
        return HTMLResponse("Invalid OAuth state", status_code=400)
    if service not in GOOGLE_SCOPES or time.time() - int(ts) > 600:
        return HTMLResponse("OAuth flow expired — please retry.", status_code=400)
    cid, csec = _google_client()
    data = urllib.parse.urlencode({
        "code": code, "client_id": cid, "client_secret": csec,
        "redirect_uri": _callback_uri(request), "grant_type": "authorization_code",
    }).encode()
    try:
        req = urllib.request.Request("https://oauth2.googleapis.com/token", data=data)
        with urllib.request.urlopen(req, timeout=20) as r:
            tok = json.load(r)
    except Exception:
        logger.exception("Google token exchange failed")
        return RedirectResponse(ROOT_PATH + "/?connect=error")
    p = _conn_path(user_id, service)
    p.parent.mkdir(parents=True, exist_ok=True)
    tok["_obtained_at"] = time.time()
    tok["_service"] = service
    try:
        p.write_text(json.dumps(tok, indent=2))
        p.chmod(0o600)
    except Exception:
        logger.exception("Failed to store connection token")
        return RedirectResponse(ROOT_PATH + "/?connect=error")
    u = _find_user_by_id(user_id)
    if u:
        _write_google_mcp(u, service)
    return RedirectResponse(ROOT_PATH + "/?connect=ok&svc=" + service)


@app.delete("/api/connections/{service}")
async def api_connection_delete(request: Request, service: str):
    user = _current_user(request)
    if not user:
        return JSONResponse({"error": "Not logged in"}, status_code=401)
    p = _conn_path(user["id"], service)
    if p.exists():
        try:
            p.unlink()
        except Exception:
            logger.exception("Failed to remove connection")
    return JSONResponse({"ok": True})


# --- Global system context (admin) -----------------------------------------
@app.get("/api/global-context")
async def api_get_global_context(request: Request):
    user = _current_user(request)
    if not _is_admin(user):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    return JSONResponse({"content": _read_global_context(), "path": str(GLOBAL_CONTEXT_FILE)})


@app.post("/api/global-context")
async def api_save_global_context(request: Request):
    user = _current_user(request)
    if not _is_admin(user):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    GLOBAL_CONTEXT_FILE.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_CONTEXT_FILE.write_text(body.get("content", "") or "")
    # Re-sync the managed block into every existing member's CLAUDE.md immediately.
    synced = 0
    for u in _load_users():
        if u.get("role") == "admin":
            continue
        try:
            d = _user_claude_config_dir(u)
            d.mkdir(parents=True, exist_ok=True)
            _sync_global_context_into(d / "CLAUDE.md")
            synced += 1
        except Exception:
            logger.debug("Failed to re-sync global context for %s", u.get("id"), exc_info=True)
    return JSONResponse({"ok": True, "synced": synced})


# ===========================================================================
# Work groups · admin context-file editor · admin user history · projects
# ===========================================================================
import mimetypes
from starlette.responses import Response

GROUPS_FILE = MESSAGES_DIR / "groups.json"
GROUPS_DIR = MESSAGES_DIR / "groups"
PROJECTS_ROOT = Path.home() / "nemo-projects"
_GROUP_CTX_BEGIN = "<!-- NEMO-DEV GROUP CONTEXT (managed — edits below are overwritten) -->"
_GROUP_CTX_END = "<!-- END NEMO-DEV GROUP CONTEXT -->"
# Top-level path segments reserved for the app (never treated as usernames).
_RESERVED_TOP = {"", "api", "login", "logout", "qa-output", "static", "favicon.ico",
                 "robots.txt", "sw.js", "health", "_next", "assets", "tmux", "ws"}


def _load_groups() -> dict:
    try:
        d = json.loads(GROUPS_FILE.read_text())
        return d if isinstance(d, dict) else {"groups": []}
    except Exception:
        return {"groups": []}


def _save_groups(data: dict):
    try:
        GROUPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        GROUPS_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        logger.exception("Failed to save groups")


def _group_dir(group_id: str) -> Path:
    return GROUPS_DIR / group_id


def _ensure_group_dir(group_id: str):
    d = _group_dir(group_id)
    d.mkdir(parents=True, exist_ok=True)
    (d / "skills").mkdir(parents=True, exist_ok=True)
    if not (d / "CLAUDE.md").exists():
        (d / "CLAUDE.md").write_text("# Group context\nShared rules and context for everyone in this work group.\n")
    if not (d / "MEMORY.md").exists():
        (d / "MEMORY.md").write_text("# Group memory\n")
    if not (d / "settings.json").exists():
        (d / "settings.json").write_text(json.dumps({"permissions": {}, "env": {}}, indent=2))


def _read_group_context(group_id: str) -> str:
    p = _group_dir(group_id) / "CLAUDE.md"
    try:
        return p.read_text() if p.exists() else ""
    except Exception:
        return ""


def _sync_group_context_into(claude_md: Path, group_id: str):
    """Maintain a managed GROUP CONTEXT block in a member's CLAUDE.md (below the
    global block). Removes it when the user has no group."""
    existing = claude_md.read_text() if claude_md.exists() else ""
    if _GROUP_CTX_BEGIN in existing and _GROUP_CTX_END in existing:
        pre = existing.split(_GROUP_CTX_BEGIN, 1)[0]
        post = existing.split(_GROUP_CTX_END, 1)[1]
        existing = pre.rstrip("\n") + "\n" + post.lstrip("\n")
    if not group_id:
        claude_md.write_text(existing)
        return
    block = _GROUP_CTX_BEGIN + "\n" + _read_group_context(group_id).rstrip() + "\n" + _GROUP_CTX_END + "\n"
    if _GLOBAL_CTX_END in existing:
        head, tail = existing.split(_GLOBAL_CTX_END, 1)
        existing = head + _GLOBAL_CTX_END + "\n\n" + block + tail.lstrip("\n")
    else:
        existing = block + "\n" + existing.lstrip("\n")
    claude_md.write_text(existing)


def _sync_group_skills_into(cfg_dir: Path, group_id: str):
    """Symlink a group's skills into the member's skills dir (group-prefixed)."""
    if not group_id:
        return
    src = _group_dir(group_id) / "skills"
    if not src.exists():
        return
    dst = cfg_dir / "skills"
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for entry in src.iterdir():
            link = dst / ("group-" + entry.name)
            try:
                if link.is_symlink():
                    if os.readlink(link) == str(entry):
                        continue
                    link.unlink()
                elif link.exists():
                    continue
                link.symlink_to(entry)
            except Exception:
                pass
    except Exception:
        pass


# --- admin context-file editor (per user / per group) ---------------------
_CONTEXT_TOP_FILES = ["CLAUDE.md", "MEMORY.md", "settings.json", ".claude.json", ".mcp.json"]
_CONTEXT_DIRS = ["skills", "agents", "commands"]


def _context_root(scope: str, ident: str):
    if scope == "user":
        u = _find_user_by_id(ident)
        if not u:
            return None
        d = _user_claude_config_dir(u)
        if not _is_admin(u):
            _ensure_user_claude_config_dir(u)
        return d
    if scope == "group":
        if not any(g.get("id") == ident for g in _load_groups().get("groups", [])):
            return None
        _ensure_group_dir(ident)
        return _group_dir(ident)
    return None


def _list_context_files(root: Path):
    out = []
    for name in _CONTEXT_TOP_FILES:
        p = root / name
        if p.is_file():
            out.append({"path": name, "size": p.stat().st_size})
    for d in _CONTEXT_DIRS:
        base = root / d
        if base.exists():
            for p in sorted(base.rglob("*")):
                if p.is_file() and not p.name.startswith("."):
                    out.append({"path": str(p.relative_to(root)), "size": p.stat().st_size})
    return out


def _safe_ctx_path(root: Path, rel: str):
    rel = (rel or "").lstrip("/").replace("\\", "/")
    if not rel or ".." in rel.split("/"):
        return None
    target = (root / rel).resolve()
    try:
        target.relative_to(root.resolve())
    except ValueError:
        return None
    return target


class CtxFileBody(BaseModel):
    path: str
    content: str = ""


@app.get("/api/admin/context/{scope}/{ident}")
async def api_admin_context_list(request: Request, scope: str, ident: str):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    root = _context_root(scope, ident)
    if root is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    root.mkdir(parents=True, exist_ok=True)
    return JSONResponse({"root": str(root), "files": _list_context_files(root)})


@app.get("/api/admin/context/{scope}/{ident}/file")
async def api_admin_context_read(request: Request, scope: str, ident: str, path: str = ""):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    root = _context_root(scope, ident)
    if root is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    target = _safe_ctx_path(root, path)
    if target is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if not target.exists():
        return JSONResponse({"path": path, "content": "", "exists": False})
    try:
        return JSONResponse({"path": path, "content": target.read_text(), "exists": True})
    except Exception:
        return JSONResponse({"error": "Unreadable (binary file?)"}, status_code=400)


@app.post("/api/admin/context/{scope}/{ident}/file")
async def api_admin_context_write(request: Request, scope: str, ident: str, body: CtxFileBody):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    root = _context_root(scope, ident)
    if root is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    target = _safe_ctx_path(root, body.path)
    if target is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if body.path.endswith(".json"):
        try:
            json.loads(body.content or "{}")
        except json.JSONDecodeError as e:
            return JSONResponse({"error": "Invalid JSON: " + e.msg}, status_code=400)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content or "")
    except Exception:
        return JSONResponse({"error": "Write failed"}, status_code=500)
    # Group CLAUDE.md edit → re-sync that group's members immediately.
    if scope == "group" and target.name == "CLAUDE.md":
        for u in _load_users():
            if u.get("group") == ident and u.get("role") != "admin":
                try:
                    _ensure_user_claude_config_dir(u)
                except Exception:
                    pass
    return JSONResponse({"ok": True})


@app.delete("/api/admin/context/{scope}/{ident}/file")
async def api_admin_context_delete(request: Request, scope: str, ident: str, path: str = ""):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    root = _context_root(scope, ident)
    if root is None:
        return JSONResponse({"error": "Not found"}, status_code=404)
    target = _safe_ctx_path(root, path)
    if target is None or not target.exists():
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    try:
        shutil.rmtree(target, ignore_errors=True) if target.is_dir() else target.unlink()
    except Exception:
        return JSONResponse({"error": "Delete failed"}, status_code=500)
    return JSONResponse({"ok": True})


# --- work groups CRUD (admin) ---------------------------------------------
class GroupBody(BaseModel):
    name: str


@app.get("/api/admin/groups")
async def api_admin_groups(request: Request):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    users = _load_users()
    out = []
    for g in _load_groups().get("groups", []):
        out.append({**g, "member_count": sum(1 for u in users if u.get("group") == g.get("id"))})
    return JSONResponse({"groups": out})


@app.post("/api/admin/groups")
async def api_admin_create_group(request: Request, body: GroupBody):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    name = (body.name or "").strip()
    gid = re.sub(r"-+", "-", re.sub(r"[^a-z0-9-]", "-", name.lower())).strip("-")[:40]
    if not name or not gid:
        return JSONResponse({"error": "Valid name required"}, status_code=400)
    data = _load_groups()
    if any(g.get("id") == gid for g in data.get("groups", [])):
        return JSONResponse({"error": "Group already exists"}, status_code=409)
    data.setdefault("groups", []).append({"id": gid, "name": name, "created_at": time.time()})
    _save_groups(data)
    _ensure_group_dir(gid)
    return JSONResponse({"ok": True, "id": gid})


@app.delete("/api/admin/groups/{group_id}")
async def api_admin_delete_group(request: Request, group_id: str):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    data = _load_groups()
    data["groups"] = [g for g in data.get("groups", []) if g.get("id") != group_id]
    _save_groups(data)
    users = _load_users()
    changed = False
    for u in users:
        if u.get("group") == group_id:
            u["group"] = ""
            changed = True
    if changed:
        _save_users(users)
    return JSONResponse({"ok": True})


# --- admin: view any user's full history ----------------------------------
def _history_list_for(target: dict):
    messages_by_session = _load_messages(target)
    notes_by_session = _load_all_notes(target)
    owners = _load_session_owners()
    live = {s["name"] for s in get_tmux_sessions() if owners.get(s["name"], "admin") == target["id"]}
    out = []
    for name in set(messages_by_session) | set(notes_by_session) | live:
        msgs = messages_by_session.get(name) or []
        last_ts, uc = 0, 0
        for m in msgs:
            if isinstance(m, dict):
                last_ts = max(last_ts, m.get("ts") or 0)
                uc += 1 if m.get("role") == "user" else 0
        out.append({"session_name": name, "key_info": notes_by_session.get(name, ""),
                    "user_message_count": uc, "total_messages": len(msgs),
                    "last_message_at": last_ts, "is_live": name in live})
    out.sort(key=lambda e: e["last_message_at"], reverse=True)
    return out


@app.get("/api/admin/users/{user_id}/history")
async def api_admin_user_history(request: Request, user_id: str):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    target = _find_user_by_id(user_id)
    if not target:
        return JSONResponse({"error": "User not found"}, status_code=404)
    return JSONResponse({"sessions": _history_list_for(target)})


@app.get("/api/admin/users/{user_id}/history/{session_name}")
async def api_admin_user_history_detail(request: Request, user_id: str, session_name: str):
    if not _is_admin(_current_user(request)):
        return JSONResponse({"error": "Admin only"}, status_code=403)
    target = _find_user_by_id(user_id)
    if not target:
        return JSONResponse({"error": "User not found"}, status_code=404)
    return JSONResponse({
        "session_name": session_name,
        "key_info": _load_all_notes(target).get(session_name, ""),
        "messages": _load_messages(target).get(session_name) or [],
    })


# --- public projects: serving + helpers -----------------------------------
def _safe_seg(s: str) -> bool:
    return bool(s) and re.match(r"^[A-Za-z0-9._-]{1,64}$", s or "") is not None and s not in (".", "..")


def _user_projects_dir(username: str) -> Path:
    return PROJECTS_ROOT / username


def _list_projects(username: str):
    d = _user_projects_dir(username)
    if not d.exists():
        return []
    return sorted(p.name for p in d.iterdir() if p.is_dir() and not p.name.startswith("."))


def _project_dir(username: str, project: str):
    if not _safe_seg(username) or not _safe_seg(project):
        return None
    return _user_projects_dir(username) / project


def _is_public_project_request(rel: str) -> bool:
    """A GET to /<username>/<project>[/...] for a real, non-reserved user is public."""
    segs = [s for s in rel.split("/") if s != ""]
    if len(segs) < 2 or segs[0] in _RESERVED_TOP:
        return False
    return _find_user_by_username(segs[0]) is not None


async def _proxy_to_port(request: Request, port: int, subpath: str):
    url = "http://127.0.0.1:%d/%s" % (port, subpath)
    if request.url.query:
        url += "?" + request.url.query
    body = await request.body()
    req = urllib.request.Request(url, data=body or None, method=request.method)
    for h in ("content-type", "accept", "user-agent"):
        v = request.headers.get(h)
        if v:
            req.add_header(h, v)

    def _do():
        return urllib.request.urlopen(req, timeout=30)
    try:
        resp = await asyncio.to_thread(_do)
        return Response(content=resp.read(), status_code=resp.status,
                        media_type=resp.headers.get("Content-Type", "application/octet-stream"))
    except urllib.error.HTTPError as e:
        return Response(content=e.read(), status_code=e.code,
                        media_type=e.headers.get("Content-Type", "text/plain"))
    except Exception:
        return HTMLResponse("Project server isn't reachable on port %d (is it running?)." % port, status_code=502)


QA_OUTPUT_DIR = Path(__file__).parent / "qa-output"

@app.get("/qa-output/{filepath:path}")
async def serve_qa_output(filepath: str):
    target = (QA_OUTPUT_DIR / filepath).resolve()
    if not str(target).startswith(str(QA_OUTPUT_DIR.resolve())):
        return JSONResponse({"error": "Forbidden"}, status_code=403)
    if not target.exists():
        return JSONResponse({"error": "Not found"}, status_code=404)
    return FileResponse(str(target))


# Serve absolute-path files referenced in terminal output. The frontend
# linkifier turns paths like /home/.../foo.md into <BASE>/file?path=/home/.../foo.md
# links. Auth-gated by the global middleware; we still reject obviously
# sensitive paths and require the resolved real path to match what was asked
# (so symlinks can't escape into something unexpected).
_FILE_SERVE_DENYLIST = {
    "/etc/shadow", "/etc/gshadow", "/etc/sudoers",
    "/root/.ssh/id_rsa", "/root/.ssh/id_ed25519",
}
_FILE_SERVE_DENY_PREFIXES = (
    "/proc/", "/sys/", "/dev/",
)

# Some paths the terminal linkifier turns into <BASE>/file?path=... links are
# not absolute filesystem paths but URL routes on sister apps (typically the
# grabo.cc dashboards). When the local lookup misses, we 302-redirect to the
# upstream host so the user reaches the actual resource. Comma-separated
# overrides via TMUX_DASHBOARD_URL_REDIRECT_MAP="/prefix=https://host,...".
_DEFAULT_URL_REDIRECT_MAP = {
    "/data-dashboard": "https://grabo.cc",
    "/extensiv":        "https://grabo.cc",
    "/shippo":          "https://grabo.cc",
    "/invoices":        "https://grabo.cc",
    "/sztx":            "https://grabo.cc",
    "/hztx":            "https://grabo.cc",
    "/hzbs":            "https://grabo.cc",
    "/outflows":        "https://grabo.cc",
    "/productmanagement": "https://grabo.cc",
    "/sznptinventory":  "https://grabo.cc",
    "/ups":             "https://grabo.cc",
    "/upsv3":           "https://grabo.cc",
    "/usabanks":        "https://grabo.cc",
    "/hsbchk":          "https://grabo.cc",
    "/gusto":           "https://grabo.cc",
    "/inventory":       "https://grabo.cc",
    "/po":              "https://grabo.cc",
    "/balance-sheet":   "https://grabo.cc",
    "/bom":             "https://grabo.cc",
    "/docvault":        "https://grabo.cc",
}


def _load_url_redirect_map() -> Dict[str, str]:
    raw = os.environ.get("TMUX_DASHBOARD_URL_REDIRECT_MAP", "").strip()
    if not raw:
        return dict(_DEFAULT_URL_REDIRECT_MAP)
    merged = dict(_DEFAULT_URL_REDIRECT_MAP)
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry or "=" not in entry:
            continue
        prefix, host = entry.split("=", 1)
        prefix = prefix.strip().rstrip("/")
        host = host.strip().rstrip("/")
        if prefix.startswith("/") and host.startswith("http"):
            merged[prefix] = host
    return merged


_URL_REDIRECT_MAP = _load_url_redirect_map()


def _upstream_url_for_path(path: str) -> Optional[str]:
    """Return upstream URL if `path` is a known dashboard URL slug, else None."""
    if not path.startswith("/"):
        return None
    first = "/" + path.lstrip("/").split("/", 1)[0]
    host = _URL_REDIRECT_MAP.get(first)
    if not host:
        return None
    return host + path


def _file_error(request: Request, status: int, title: str, message: str, path: str):
    """Return JSON for API clients, friendly HTML for browsers."""
    accept = (request.headers.get("accept") or "").lower()
    wants_html = "text/html" in accept and "application/json" not in accept
    if not wants_html:
        return JSONResponse({"error": title.lower(), "message": message, "path": path}, status_code=status)
    safe_path = (path or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    safe_msg = message.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    rp = request.scope.get("root_path", "") or "/"
    html = f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<title>{status} · {title} · {BRAND_NAME} Dashboard</title>
<style>
  body{{margin:0;background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;padding:24px;box-sizing:border-box}}
  .card{{background:#161b22;border:1px solid #21262d;border-radius:10px;padding:28px 32px;max-width:640px;width:100%;box-shadow:0 6px 30px rgba(0,0,0,.4)}}
  .status{{font-size:.8rem;letter-spacing:.08em;text-transform:uppercase;color:#8b949e;margin-bottom:6px}}
  h1{{font-size:1.4rem;margin:0 0 12px 0;color:#f0f6fc}}
  p{{margin:0 0 14px 0;color:#c9d1d9;line-height:1.55}}
  .path{{display:block;background:#0d1117;border:1px solid #30363d;border-radius:6px;padding:10px 12px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.85rem;color:#79c0ff;word-break:break-all;margin:4px 0 14px 0}}
  .meta{{color:#6e7681;font-size:.85rem}}
  a{{color:#58a6ff;text-decoration:none}}
  a:hover{{text-decoration:underline}}
</style></head>
<body><div class="card">
  <div class="status">Error {status} · {title}</div>
  <h1>{safe_msg}</h1>
  <p>The terminal link pointed to:</p>
  <code class="path">{safe_path or '(no path)'}</code>
  <p class="meta">If this is a file you expected to exist, double-check the spelling, or that the dashboard is running on the host where the file lives.</p>
  <p><a href="{rp}">← back to dashboard</a></p>
</div></body></html>"""
    return HTMLResponse(html, status_code=status)


def _safe_is_dir(p: Path) -> bool:
    try:
        return p.is_dir()
    except Exception:
        return False


def _human_size(n: int) -> str:
    try:
        size = float(n)
    except Exception:
        return ""
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return (f"{int(size)} {unit}" if unit == "B" else f"{size:.1f} {unit}")
        size /= 1024.0
    return f"{size:.1f} TB"


_DIR_LISTING_MAX = 2000  # cap entries so a huge dir can't produce a giant page


def _render_dir_listing(request: Request, dir_path: Path) -> HTMLResponse:
    """Render a clickable HTML listing for a directory referenced from terminal
    output. Every row links back through /file?path=... so browsing stays behind
    the dashboard's auth middleware — same login as the dashboard itself."""
    rp = request.scope.get("root_path", "") or ""
    real = str(dir_path)

    def _attr(s: str) -> str:
        return _html_escape(s).replace('"', "&quot;")

    def _link(p: Path) -> str:
        return rp + "/file?path=" + urllib.parse.quote(str(p), safe="")

    try:
        kids = list(dir_path.iterdir())
    except PermissionError:
        return _file_error(request, 403, "Forbidden", "Permission denied listing this directory.", real)
    except Exception:
        return _file_error(request, 500, "Error", "That directory could not be read.", real)
    kids.sort(key=lambda p: (not _safe_is_dir(p), p.name.lower()))
    total = len(kids)
    truncated = total > _DIR_LISTING_MAX
    kids = kids[:_DIR_LISTING_MAX]

    rows = []
    # Parent link (skip when already at the filesystem root).
    if dir_path != dir_path.parent:
        rows.append(
            '<tr><td class="ic">&#128193;</td>'
            f'<td><a class="row-link" href="{_attr(_link(dir_path.parent))}">../</a></td>'
            '<td class="sz"></td><td class="mt"></td></tr>'
        )
    for p in kids:
        is_dir = _safe_is_dir(p)
        try:
            st = p.stat()
        except Exception:
            st = None
        name = p.name + ("/" if is_dir else "")
        icon = "&#128193;" if is_dir else "&#128196;"
        size = "" if (is_dir or st is None) else _human_size(st.st_size)
        mtime = datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M") if st else ""
        rows.append(
            f'<tr><td class="ic">{icon}</td>'
            f'<td><a class="row-link" href="{_attr(_link(p))}">{_html_escape(name)}</a></td>'
            f'<td class="sz">{_html_escape(size)}</td>'
            f'<td class="mt">{_html_escape(mtime)}</td></tr>'
        )
    body = "\n".join(rows) or '<tr><td colspan="4" class="empty">(empty directory)</td></tr>'
    note = (f'<div class="note">Showing first {_DIR_LISTING_MAX} of {total} entries.</div>'
            if truncated else "")
    count_lbl = f"{total} item" + ("" if total == 1 else "s")
    doc = f"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{_html_escape(dir_path.name or '/')} · index · {BRAND_NAME} Dashboard</title>
<style>
  body{{margin:0;background:#0d1117;color:#c9d1d9;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;padding:24px;box-sizing:border-box}}
  .wrap{{max-width:960px;margin:0 auto;background:#161b22;border:1px solid #21262d;border-radius:10px;overflow:hidden;box-shadow:0 6px 30px rgba(0,0,0,.4)}}
  .hd{{padding:18px 22px;border-bottom:1px solid #21262d}}
  .crumb{{font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.9rem;color:#79c0ff;word-break:break-all}}
  .meta{{color:#8b949e;font-size:.8rem;margin-top:6px}}
  .meta a{{color:#58a6ff;text-decoration:none}} .meta a:hover{{text-decoration:underline}}
  table{{width:100%;border-collapse:collapse;font-size:.88rem}}
  th{{text-align:left;color:#8b949e;font-weight:500;font-size:.72rem;letter-spacing:.06em;text-transform:uppercase;padding:8px 22px;border-bottom:1px solid #21262d}}
  td{{padding:7px 22px;border-bottom:1px solid #1b2027;white-space:nowrap}}
  tr:last-child td{{border-bottom:none}}
  tr:hover td{{background:#1c2330}}
  td.ic{{width:20px;padding-right:0;opacity:.85}}
  .row-link{{color:#c9d1d9;text-decoration:none;word-break:break-all}}
  .row-link:hover{{color:#79c0ff;text-decoration:underline}}
  td.sz,td.mt,th.sz,th.mt{{color:#8b949e;text-align:right;font-variant-numeric:tabular-nums}}
  td.mt,th.mt{{font-size:.8rem}}
  .empty{{color:#6e7681;text-align:center;padding:24px}}
  .note{{padding:10px 22px;color:#d29922;font-size:.8rem;border-top:1px solid #21262d}}
</style></head>
<body><div class="wrap">
  <div class="hd">
    <div class="crumb">&#128193; {_html_escape(real)}</div>
    <div class="meta">{count_lbl} · directory listing · <a href="{rp or '/'}">← dashboard</a></div>
  </div>
  <table><thead><tr><th></th><th>Name</th><th class="sz">Size</th><th class="mt">Modified</th></tr></thead>
  <tbody>
{body}
  </tbody></table>
  {note}
</div></body></html>"""
    resp = HTMLResponse(doc)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@app.get("/file")
async def serve_terminal_file(request: Request, path: str = "", download: int = 0):
    """Serve a file (or directory listing) referenced from terminal output.

    The terminal linkifier (frontend) discovers file paths — absolute
    (/home/nimrod_rotem/notes.md) and home-relative (~/PROBST_LAWSUIT_2026-07-10/)
    — and turns them into <BASE>/file?path=... links. Files render inline by
    default so .md / .py / images / PDFs show in the browser tab (pass
    ?download=1 to force a download); directories render a clickable listing.
    This route sits behind the dashboard's auth middleware, so every link is
    protected by the same login as the dashboard itself.
    """
    orig_path = path
    if not path:
        return _file_error(request, 400, "Bad request", "A file path is required.", orig_path)
    # Expand ~ / ~user home-relative paths (terminal output prints these a lot,
    # e.g. "Deliverables in ~/PROBST_LAWSUIT_2026-07-10/").
    if path.startswith("~"):
        path = os.path.expanduser(path)
    if not path.startswith("/"):
        return _file_error(request, 400, "Bad request", "An absolute path (or a ~ home path) is required.", orig_path)
    if path in _FILE_SERVE_DENYLIST:
        return _file_error(request, 403, "Forbidden", "This file is on the dashboard's protected list.", orig_path)
    for pref in _FILE_SERVE_DENY_PREFIXES:
        if path.startswith(pref):
            return _file_error(request, 403, "Forbidden", "Pseudo-filesystem paths (/proc, /sys, /dev) are not served.", orig_path)
    try:
        target = Path(path).resolve()
    except Exception:
        return _file_error(request, 400, "Bad request", "That path could not be resolved.", orig_path)
    # Re-check denylist against the resolved real path (defeats symlink tricks).
    real = str(target)
    if real in _FILE_SERVE_DENYLIST:
        return _file_error(request, 403, "Forbidden", "This file is on the dashboard's protected list.", orig_path)
    for pref in _FILE_SERVE_DENY_PREFIXES:
        if real.startswith(pref):
            return _file_error(request, 403, "Forbidden", "Pseudo-filesystem paths (/proc, /sys, /dev) are not served.", orig_path)
    if not target.exists():
        upstream = _upstream_url_for_path(orig_path)
        if upstream:
            return RedirectResponse(url=upstream, status_code=302)
        return _file_error(request, 404, "Not found", "No such file or directory on this host.", orig_path)
    # Directories → a clickable listing (each row stays behind this auth route).
    if target.is_dir():
        return _render_dir_listing(request, target)
    if not target.is_file():
        return _file_error(request, 400, "Unsupported", "That path is not a regular file or directory.", orig_path)
    mime, _ = mimetypes.guess_type(real)
    headers = {}
    # Render text/markdown/code inline as plain text so the browser shows the
    # content rather than offering a download dialog.
    ext = target.suffix.lower()
    text_like_exts = {
        ".md", ".markdown", ".txt", ".log", ".py", ".js", ".ts", ".tsx", ".jsx",
        ".json", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".sh",
        ".bash", ".zsh", ".env", ".csv", ".tsv", ".sql", ".html", ".htm",
        ".css", ".scss", ".xml", ".rb", ".go", ".rs", ".c", ".h", ".cpp",
        ".hpp", ".java", ".kt", ".swift", ".php", ".lua", ".r", ".dockerfile",
        ".gitignore", ".gitattributes",
    }
    if not mime and ext in text_like_exts:
        mime = "text/plain; charset=utf-8"
    elif mime and mime.startswith("text/"):
        mime = mime + "; charset=utf-8" if "charset" not in mime else mime
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{target.name}"'
    else:
        headers["Content-Disposition"] = f'inline; filename="{target.name}"'
    return FileResponse(str(target), media_type=mime or "application/octet-stream", headers=headers)


# Three-tier cache per session
cache: Dict[str, dict] = {}

# Persistent message storage
# NOTE: messages + notes are now scoped per-user. The legacy
# ~/.tmux-dashboard/messages.json and notes.json are the admin's files. Other
# users get ~/.tmux-dashboard/users/<id>/messages.json and notes.json.
MESSAGES_FILE = MESSAGES_DIR / "messages.json"
NOTES_FILE = MESSAGES_DIR / "notes.json"


def _read_json_file(path: Path) -> dict:
    try:
        if path.exists():
            data = json.loads(path.read_text())
            if isinstance(data, dict):
                return data
    except Exception:
        logger.debug("Failed to read %s", path, exc_info=True)
    return {}


def _write_json_file(path: Path, data: dict):
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))
    except Exception:
        logger.debug("Failed to write %s", path, exc_info=True)


def _load_all_notes(user: Optional[dict] = None) -> Dict[str, str]:
    """Load all session notes for a given user from disk. Falls back to admin
    file if `user` is None (matches legacy single-user behaviour)."""
    return _read_json_file(_user_notes_file(user))


def _save_notes():
    """Persist all session notes to per-user files based on session ownership."""
    # Group cache entries by owning user
    by_user: Dict[str, Dict[str, str]] = {}
    for name, entry in cache.items():
        notes = entry.get("notes")
        if not notes:
            continue
        owner_id = _session_owner_id(name)
        by_user.setdefault(owner_id, {})[name] = notes

    # Write each user's file, merged with any sessions not currently in cache.
    user_ids = set(by_user.keys())
    # Also touch files for users whose cache is empty but who have existing notes
    # so we don't accidentally drop them: just don't write empty files.
    for uid, updates in by_user.items():
        owner = _find_user_by_id(uid) or _find_user_by_id("admin")
        path = _user_notes_file(owner)
        existing = _read_json_file(path)
        existing.update(updates)
        _write_json_file(path, existing)


def _load_session_notes(session_name: str) -> str:
    """Get persisted notes for a specific session, from its owner's file."""
    owner = _user_for_session(session_name)
    return _load_all_notes(owner).get(session_name, "")


def _load_messages(user: Optional[dict] = None) -> Dict[str, list]:
    """Load all session messages for a given user from disk."""
    return _read_json_file(_user_messages_file(user))


def _save_messages():
    """Persist all session messages to per-user files based on session ownership."""
    by_user: Dict[str, Dict[str, list]] = {}
    for name, entry in cache.items():
        msgs = entry.get("messages")
        if not msgs:
            continue
        owner_id = _session_owner_id(name)
        by_user.setdefault(owner_id, {})[name] = msgs

    for uid, updates in by_user.items():
        owner = _find_user_by_id(uid) or _find_user_by_id("admin")
        path = _user_messages_file(owner)
        existing = _read_json_file(path)
        existing.update(updates)
        _write_json_file(path, existing)


def _load_session_messages(session_name: str) -> list:
    """Get persisted messages for a specific session from its owner's file."""
    owner = _user_for_session(session_name)
    return _load_messages(owner).get(session_name, [])


DESCRIPTION_TTL = 0    # never auto-expire
PROGRESS_TTL = 600     # 10 minutes
REALTIME_TTL = 15      # 15 seconds — text extraction is cheap (no LLM call usually)
NOTES_TTL = 600        # 10 minutes


# Sessions whose pane is running OpenAI codex belong to the codax dashboard,
# not the claude tmux dashboard. Filter them out by walking pane descendants
# and checking /proc/<pid>/comm for the codex/claude executable names.
_CLAUDE_DASH_VISIBILITY_CACHE: Dict[str, tuple] = {}
_CLAUDE_DASH_VISIBILITY_TTL = 5.0


def _session_is_claude(name: str) -> bool:
    """Return True if this tmux session belongs to the claude dashboard.

    Mirrors the heuristic in codax-dashboard but flips the verdict.
    """
    now = time.time()
    cached = _CLAUDE_DASH_VISIBILITY_CACHE.get(name)
    if cached and now - cached[1] < _CLAUDE_DASH_VISIBILITY_TTL:
        return cached[0]
    try:
        pp = subprocess.run(
            ["tmux", "display-message", "-t", name, "-p", "#{pane_pid}"],
            capture_output=True, text=True, timeout=3,
        )
        if pp.returncode != 0:
            _CLAUDE_DASH_VISIBILITY_CACHE[name] = (False, now)
            return False
        pane_pid = (pp.stdout or "").strip()
        if not pane_pid.isdigit():
            _CLAUDE_DASH_VISIBILITY_CACHE[name] = (False, now)
            return False
        to_check = [pane_pid]
        seen = {pane_pid}
        descendants = []
        for _ in range(50):
            if not to_check:
                break
            current = to_check.pop(0)
            try:
                child_res = subprocess.run(
                    ["pgrep", "-P", current],
                    capture_output=True, text=True, timeout=2,
                )
            except Exception:
                continue
            for pid in (child_res.stdout or "").strip().split():
                if pid and pid not in seen:
                    seen.add(pid)
                    descendants.append(pid)
                    to_check.append(pid)
        has_codex = False
        has_claude = False
        for pid in descendants:
            try:
                with open(f"/proc/{pid}/comm", "r") as f:
                    comm = f.read().strip().lower()
            except Exception:
                continue
            if comm == "codex":
                has_codex = True
                break
            if comm == "claude":
                has_claude = True
        if has_codex:
            decision = False
        elif has_claude:
            decision = True
        else:
            decision = True  # bare shell or unknown -> allow on the claude dashboard
    except Exception:
        decision = False
    _CLAUDE_DASH_VISIBILITY_CACHE[name] = (decision, now)
    return decision


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
            if not _session_is_claude(name):
                continue  # Hide codex sessions from the claude tmux dashboard
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


def _filter_sessions_for_user(sessions: list, user: Optional[dict]) -> list:
    """Restrict a session list to the ones the given user is allowed to see."""
    if _is_admin(user):
        return sessions
    if not user:
        return []
    owners = _load_session_owners()
    uid = user["id"]
    return [s for s in sessions if owners.get(s["name"], "admin") == uid]


def _find_session_for_user(session_name: str, user: Optional[dict]) -> tuple:
    """Same as _find_session but enforces user ownership. Returns
    (sessions, session_dict) on success or (sessions, None) if missing OR not
    owned by `user` (admins bypass)."""
    sessions, sess = _find_session(session_name)
    if sess is None:
        return sessions, None
    if not _user_can_access_session(user, session_name):
        return sessions, None
    return sessions, sess


def capture_pane_full(session_name: str) -> str:
    try:
        # -J joins terminal-wrap continuation lines so long strings (e.g. OAuth
        # login URLs) come back intact instead of split at pane width.
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-J", "-S", "-"],
            capture_output=True, text=True, timeout=10
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def capture_pane_recent(session_name: str, lines: int = 80) -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p", "-J", "-S", f"-{lines}"],
            capture_output=True, text=True, timeout=5
        )
        return result.stdout if result.returncode == 0 else ""
    except Exception:
        return ""


def get_pane_width(session_name: str) -> int:
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p", "#{pane_width}"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0:
            return int(result.stdout.strip())
    except Exception:
        pass
    return 80


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

        # Auto-approve disabled: never type/select on the user's behalf.
        # _check_auto_approve(session_name, visible)

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


async def llm_call(system_prompt: str, user_content: str, max_tokens: int = 200,
                   response_format: dict = None) -> str:
    start = time.time()
    try:
        kwargs = dict(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            max_tokens=max_tokens,
            temperature=0.3,
        )
        if response_format:
            kwargs["response_format"] = response_format
        resp = await client.chat.completions.create(**kwargs)
        duration = time.time() - start
        tokens_used = getattr(resp.usage, "total_tokens", 0) if resp.usage else 0
        logger.debug("LLM call completed in %.1fs, %d tokens", duration, tokens_used)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        duration = time.time() - start
        logger.error("LLM call failed after %.1fs: %s", duration, e)
        # Return empty string (not the error text) so callers don't cache the
        # error as content. Downstream keyword checks (`"CONTINUE" not in ...`,
        # `"LEGITIMATE" in ...`) treat empty as a no-op, which is the right
        # fail-safe behavior.
        return ""


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
    if word_count <= 500 or not AUTO_SUMMARIZER_ENABLED:
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


def _output_signature(text: str) -> str:
    """Normalized hash of terminal output, used to skip LLM re-summarization when
    the output hasn't meaningfully changed. Insensitive to trailing whitespace
    and runs of blank lines (those flap a lot during spinners / redraws)."""
    if not text:
        return ""
    out = []
    blank = False
    for ln in text.split("\n"):
        ln = ln.rstrip()
        if not ln:
            if not blank:
                out.append("")
            blank = True
        else:
            out.append(ln)
            blank = False
    return hashlib.sha256("\n".join(out).encode("utf-8", "replace")).hexdigest()


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

    has_description = "description" in entry
    has_progress = "progress" in entry
    has_notes = "notes" in entry
    progress_ttl_expired = (now - entry.get("progress_at", 0)) >= PROGRESS_TTL
    notes_ttl_expired = (now - entry.get("notes_at", 0)) >= NOTES_TTL

    # Capture pane up front when any task might fire, so we can compare a
    # content signature against the last successful summary and skip the LLM
    # call when nothing has actually changed. Capture is a cheap subprocess
    # call relative to an OpenAI request.
    full_output = None
    sig = ""
    might_need = AUTO_SUMMARIZER_ENABLED and (force_all or (
        not has_description
        or not has_progress or progress_ttl_expired
        or not has_notes or notes_ttl_expired
    ))
    if might_need:
        full_output = capture_pane_full(session_name)
        sig = _output_signature(full_output)

    # Staleness gate: skip the LLM call when the captured output hasn't changed
    # since the last successful summary. Force / missing cache still bypass.
    need_description = force_all or not has_description or (
        bool(sig) and entry.get("description_sig") != sig
    )
    need_progress = force_all or not has_progress or (
        progress_ttl_expired and bool(sig) and entry.get("progress_sig") != sig
    )
    need_notes = force_all or not has_notes or (
        notes_ttl_expired and bool(sig) and entry.get("notes_sig") != sig
    )

    # Auto-summarizer removed: never issue LLM title/description/progress/notes calls.
    if not AUTO_SUMMARIZER_ENABLED:
        need_description = need_progress = need_notes = False

    tasks = {}
    if need_description:
        tasks["title_desc"] = get_title_and_description(session_name, full_output)
    if need_progress:
        tasks["progress"] = get_progress(session_name, full_output)
    if need_notes:
        tasks["notes"] = get_notes(session_name, full_output, entry.get("notes", ""), entry.get("messages", []))
    if force_all or "realtime" not in entry or (now - entry.get("realtime_at", 0)) >= REALTIME_TTL:
        tasks["realtime"] = get_realtime(session_name)

    # Simplified Chat tab: when Claude is idle, (re)generate ONE plain-language
    # recap of its whole last turn. Triggered promptly on the busy->idle edge,
    # then at most every REALTIME_TTL; the signature gate inside get_chat_summary
    # skips the LLM call when the turn output hasn't changed.
    _chat_status = _activity_state.get(session_name, {}).get("status", "")
    if _chat_status == "busy":
        entry["_chat_was_busy"] = True
    _summary_due = (now - entry.get("chat_summary_at", 0)) >= REALTIME_TTL
    if _chat_status == "idle" and (force_all or entry.get("_chat_was_busy") or _summary_due):
        # Pass the last user message so the right transcript is matched when
        # several sessions share a cwd (project dir).
        _last_user = next((m.get("text", "") for m in reversed(entry.get("messages", []))
                           if m.get("role") == "user"), "")
        tasks["chat_summary"] = get_chat_summary(session_name, entry.get("chat_summary_sig", ""), _last_user)
        entry["chat_summary_at"] = now
        entry["_chat_was_busy"] = False

    if tasks:
        results = await asyncio.gather(*tasks.values())
        result_map = dict(zip(tasks.keys(), results))
        if "title_desc" in result_map:
            title, description = result_map["title_desc"]
            # Only commit if at least one of the parallel calls succeeded.
            # Empty strings come from llm_call's error path -- preserve prior
            # cached value (and don't update sig, so we'll retry next round).
            if (title and title.strip()) or (description and description.strip()):
                entry["title"] = title or entry.get("title", "")
                entry["description"] = description or entry.get("description", "")
                entry["description_at"] = now
                entry["description_sig"] = sig
        if "progress" in result_map:
            progress = result_map["progress"]
            if progress and progress.strip():
                entry["progress"] = progress
                entry["progress_at"] = now
                entry["progress_sig"] = sig
        if "notes" in result_map:
            notes = result_map["notes"]
            if notes and notes.strip():
                entry["notes"] = notes
                entry["notes_at"] = now
                entry["notes_sig"] = sig
        if "realtime" in result_map:
            realtime = result_map["realtime"]
            if realtime and realtime.strip():
                # Kept for the Info tab's "live" field only — NOT pushed into the
                # Chat tab (that now shows the idle summary below, not raw text).
                entry["realtime"] = realtime
                entry["realtime_at"] = now
        if "chat_summary" in result_map:
            cs = result_map["chat_summary"]
            if cs and cs.get("summary"):
                _append_assistant_msg(entry, cs["summary"], now)
                entry["chat_summary_sig"] = cs["sig"]

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


# --- Simplified Chat tab: one plain-language recap per completed Claude turn ---
#
# The Chat tab is for non-developer users who shouldn't see raw terminal/tool
# output. Instead of streaming Claude's live text into the chat, we wait until
# Claude goes idle and then post ONE short summary of everything it produced
# that turn. The clean source of "everything Claude output" is the session's
# JSONL transcript (assistant `text` blocks since the last genuine user
# message) — far cleaner than scraping the pane. We fall back to the pane scrape
# when no transcript exists.

def _read_jsonl_tail(path: str, max_bytes: int = 1_500_000) -> list:
    """Return the trailing lines of a (possibly huge) JSONL file cheaply."""
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
                f.readline()  # discard the partial first line
            data = f.read()
        return data.decode("utf-8", "replace").splitlines()
    except Exception:
        return []


def _is_genuine_user_content(cont) -> bool:
    """True for a real human prompt, False for a tool_result (also role=user)."""
    if isinstance(cont, str):
        return bool(cont.strip())
    if isinstance(cont, list):
        has_text = any(
            isinstance(b, dict) and b.get("type") == "text" and (b.get("text") or "").strip()
            for b in cont
        )
        has_tool = any(isinstance(b, dict) and b.get("type") == "tool_result" for b in cont)
        return has_text and not has_tool
    return False


def _norm_text(s: str) -> str:
    return " ".join((s or "").split()).lower()


def _last_genuine_user_text(path: str) -> str:
    """The most recent genuine human prompt recorded in a transcript file."""
    last = ""
    for ln in _read_jsonl_tail(path):
        ln = ln.strip()
        if not ln:
            continue
        try:
            o = json.loads(ln)
        except Exception:
            continue
        m = o.get("message")
        if o.get("type") == "user" and isinstance(m, dict) and _is_genuine_user_content(m.get("content")):
            c = m.get("content")
            if isinstance(c, str):
                last = c
            elif isinstance(c, list):
                last = " ".join(
                    (b.get("text") or "") for b in c
                    if isinstance(b, dict) and b.get("type") == "text"
                )
    return last.strip()


def _resolve_session_transcript(session_name: str, last_user_text: str):
    """Pick the transcript that belongs to THIS tmux session.

    Several sessions can share one cwd (hence one Claude `projects/<dir>` with
    many JSONL files), so newest-mtime is NOT a reliable per-session signal — it
    can point at a sibling session that happened to write more recently. Instead
    match on content: the transcript whose latest human prompt equals the command
    the dashboard last sent to this session. Returns None when it can't be
    disambiguated (caller falls back to this session's own terminal pane)."""
    files = _find_session_jsonl_files(session_name)
    if not files:
        return None
    if len(files) == 1:
        return files[0]
    want = _norm_text(last_user_text)
    if not want:
        return None  # nothing to match on — don't guess across sessions
    key = want[:60]
    for path in sorted(files, key=os.path.getmtime, reverse=True):
        lu = _norm_text(_last_genuine_user_text(path))
        if lu and (lu.startswith(key) or key in lu):
            return path
    return None


def _extract_last_assistant_turn(session_name: str, last_user_text: str = "") -> str:
    """Clean text of Claude's most recent turn: every assistant `text` block
    since the last genuine user message in the transcript. Thinking and tool
    calls/results are excluded. Falls back to this session's terminal pane when
    the transcript can't be unambiguously matched to this session."""
    path = _resolve_session_transcript(session_name, last_user_text)
    if path:
        try:
            texts: list = []
            for ln in _read_jsonl_tail(path):
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    o = json.loads(ln)
                except Exception:
                    continue
                msg = o.get("message")
                if not isinstance(msg, dict):
                    continue
                t = o.get("type")
                if t == "user":
                    if _is_genuine_user_content(msg.get("content")):
                        texts = []  # a new human turn begins — drop prior text
                elif t == "assistant":
                    for b in (msg.get("content") or []):
                        if isinstance(b, dict) and b.get("type") == "text":
                            tx = (b.get("text") or "").strip()
                            if tx:
                                texts.append(tx)
            if texts:
                return "\n\n".join(texts).strip()
        except Exception:
            logger.debug("transcript turn extraction failed", exc_info=True)
    # Fallback: scrape the visible terminal.
    try:
        recent = capture_pane_recent(session_name, 200)
        return _extract_claude_response_since_last_user(recent).strip()
    except Exception:
        return ""


def _trim_plain(text: str, limit: int = 600) -> str:
    """Trim to a sentence/word boundary near `limit`, adding an ellipsis."""
    text = (text or "").strip()
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "! ", "? ", "\n"):
        i = cut.rfind(sep)
        if i > limit * 0.5:
            return cut[: i + 1].strip() + " …"
    i = cut.rfind(" ")
    return (cut[:i] if i > 0 else cut).strip() + " …"


async def _summarize_turn(text: str) -> str:
    """Short, plain-language recap of one assistant turn for non-dev users."""
    body = (text or "").strip()
    if not body:
        return ""
    summary = await llm_call(
        system_prompt=(
            "You summarize what an AI coding assistant just did, for a "
            "non-technical user who cannot see the terminal. Write a short, "
            "plain, friendly recap (1-3 sentences, under 70 words) of what was "
            "done, found, or decided. Keep concrete outcomes (what changed, what "
            "was created/fixed, any link, number, or result). No code blocks, no "
            "file-path jargon unless essential, no preamble or sign-off. Write in "
            "first person as the assistant ('I ...'). If the assistant asked the "
            "user something, state the question."
        ),
        user_content=f"The assistant's full output for this turn:\n\n{body[:8000]}",
        max_tokens=170,
    )
    summary = (summary or "").strip()
    # LLM unavailable/failed -> fall back to the assistant's own words (already
    # clean prose from the transcript), trimmed, rather than raw terminal output.
    return summary or _trim_plain(body, 600)


async def get_chat_summary(session_name: str, prev_sig: str, last_user_text: str = ""):
    """Return {'sig','summary'} for the latest turn, or None when unchanged/empty."""
    turn_text = await asyncio.to_thread(_extract_last_assistant_turn, session_name, last_user_text)
    turn_text = (turn_text or "").strip()
    if not turn_text:
        return None
    sig = _output_signature(turn_text)
    if sig == prev_sig:
        return None  # this exact output was already summarized
    summary = await _summarize_turn(turn_text)
    summary = (summary or "").strip()
    if not summary:
        return None
    return {"sig": sig, "summary": summary}


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
        "autopush_mode": _get_autopush_mode(sess["name"]),
        "simple_watchdog": _get_autopush_mode(sess["name"]) == "full",
        "model": _get_session_model(sess["name"]),
        "profile_id": _get_session_profile_id(sess["name"]),
    }


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    # Inject the per-user "simple" flag so the member UI is correct from the very
    # first line of JS (before any /api/me round-trip), avoiding admin-only fetches.
    simple = bool(TEAM_MODE and not _is_admin(_current_user(request)))
    return HTMLResponse(HTML_PAGE.replace("__SIMPLE__", "true" if simple else "false"))


@app.get("/api/sessions")
async def api_sessions(request: Request):
    user = _current_user(request)
    sessions = _filter_sessions_for_user(get_tmux_sessions(), user)
    results, activities = await asyncio.gather(
        asyncio.gather(*[get_session_data(s["name"]) for s in sessions]),
        asyncio.gather(*(async_detect_activity(s["name"]) for s in sessions)),
    )
    return JSONResponse([
        build_session_response(sess, data, activity=act)
        for sess, data, act in zip(sessions, results, activities)
    ])


@app.get("/api/sessions-fast")
async def api_sessions_fast(request: Request):
    """Return session list with cached data only — no LLM calls. Fast startup."""
    user = _current_user(request)
    sessions = _filter_sessions_for_user(get_tmux_sessions(), user)
    # Run activity detection for all sessions in parallel threads
    activities = await asyncio.gather(
        *(async_detect_activity(sess["name"]) for sess in sessions)
    )
    out = []
    _owners_map = _load_session_owners()
    _uid_to_name = {u["id"]: u.get("username", "") for u in _load_users()}
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
            "owner": _uid_to_name.get(_owners_map.get(sess["name"], "admin"), "") or AUTH_USER,
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
            "autopush_mode": _get_autopush_mode(sess["name"]),
            "simple_watchdog": _get_autopush_mode(sess["name"]) == "full",
            "model": _get_session_model(sess["name"]),
            "profile_id": _get_session_profile_id(sess["name"]),
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
async def api_status(request: Request):
    """Lightweight: return only activity status per session, no LLM calls."""
    user = _current_user(request)
    sessions = _filter_sessions_for_user(get_tmux_sessions(), user)
    activities = await asyncio.gather(
        *(async_detect_activity(sess["name"]) for sess in sessions)
    )
    out = []
    for sess, activity in zip(sessions, activities):
        out.append({
            "name": sess["name"],
            "activity_status": activity["status"],
            "activity_detail": activity["detail"],
            "autopush_mode": _get_autopush_mode(sess["name"]),
            "simple_watchdog": _get_autopush_mode(sess["name"]) == "full",
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
    pane_width = await asyncio.to_thread(get_pane_width, session_name)
    return JSONResponse({
        "name": session_name,
        "raw": raw,
        "lines": len(raw.split("\n")),
        "pane_width": pane_width,
        "activity_status": activity["status"],
        "activity_command": activity["command"],
        "activity_detail": activity["detail"],
    })


def _visible_pane_hash(session_name: str) -> str:
    """Cheap fingerprint of the visible tmux pane (alternate-screen aware).

    capture-pane without -S only returns the visible area, which Claude Code
    redraws into via its TUI even when history_size never grows. We hash that
    so the client can detect TUI redraws as content changes.
    """
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-t", session_name, "-p"],
            capture_output=True, text=True, timeout=3,
        )
        if result.returncode == 0:
            return hashlib.md5(result.stdout.encode("utf-8", "replace")).hexdigest()
    except Exception:
        logger.debug("Failed to hash visible pane for '%s'", session_name, exc_info=True)
    return ""


@app.get("/api/sessions/{session_name}/raw-tail")
async def api_raw_tail(session_name: str, known_lines: int = 0, last_hash: str = ""):
    """Return delta output since the client's last known line count.

    Also detects in-place TUI redraws (Claude Code's alternate screen) by
    hashing the visible pane — a hash mismatch forces a full capture even when
    scrollback length is unchanged.
    """
    _, found = _find_session(session_name)
    if not found:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    pos = await asyncio.to_thread(get_pane_position, session_name)
    current_total = pos["total_lines"]
    vis_hash = await asyncio.to_thread(_visible_pane_hash, session_name)
    pane_width = await asyncio.to_thread(get_pane_width, session_name)

    # First load or session reset → full capture
    if known_lines <= 0 or known_lines > current_total:
        raw = await asyncio.to_thread(capture_pane_full, session_name)
        return JSONResponse({
            "mode": "full",
            "raw": raw,
            "total_lines": len(raw.split("\n")),
            "pane_total": current_total,
            "pane_width": pane_width,
            "visible_hash": vis_hash,
        })

    # No scrollback growth, but visible content changed (TUI redraw) → full
    if current_total <= known_lines:
        if last_hash and vis_hash and last_hash != vis_hash:
            raw = await asyncio.to_thread(capture_pane_full, session_name)
            return JSONResponse({
                "mode": "full",
                "raw": raw,
                "total_lines": len(raw.split("\n")),
                "pane_total": current_total,
                "pane_width": pane_width,
                "visible_hash": vis_hash,
            })
        return JSONResponse({
            "mode": "none",
            "total_lines": known_lines,
            "pane_total": current_total,
            "pane_width": pane_width,
            "visible_hash": vis_hash,
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
        "pane_width": pane_width,
        "overlap": overlap,
        "visible_hash": vis_hash,
    })


class CreateSession(BaseModel):
    name: str = ""
    profile_id: str = ""


@app.post("/api/sessions/create")
async def api_create_session(request: Request, body: CreateSession):
    """Create a new tmux session."""
    user = _current_user(request)
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
        # Record session ownership. If auth is disabled, fall back to admin.
        owner_id = user["id"] if user else "admin"
        _set_session_owner(created, owner_id)
        # Export project-publishing convention env vars so Claude can publish to
        # https://dianaotech.com/<username>/<session> reliably (see global context).
        try:
            _owner_name = (user.get("username") if user else AUTH_USER) or "admin"
            _proj_dir = str(PROJECTS_ROOT / _owner_name / created)
            _pub_base = PUBLIC_BASE_URL.rstrip("/") or "https://dianaotech.com"
            # Per-user git identity: every member shares ONE OS user, so set the
            # commit author/committer per session → commits are attributed to the
            # right person. Push still uses the box's shared GitHub creds.
            _git_email = "%s@%s" % (_owner_name, GIT_EMAIL_DOMAIN)
            _exports = ("export NEMO_USER=%s NEMO_SESSION=%s NEMO_PROJECT_DIR=%s NEMO_PROJECT_URL=%s "
                        "GIT_AUTHOR_NAME=%s GIT_AUTHOR_EMAIL=%s GIT_COMMITTER_NAME=%s GIT_COMMITTER_EMAIL=%s" % (
                shlex.quote(_owner_name), shlex.quote(created),
                shlex.quote(_proj_dir), shlex.quote("%s/%s/%s" % (_pub_base, _owner_name, created)),
                shlex.quote(_owner_name), shlex.quote(_git_email),
                shlex.quote(_owner_name), shlex.quote(_git_email)))
            subprocess.run(["tmux", "send-keys", "-t", created, "-l", _exports], capture_output=True, text=True, timeout=5)
            subprocess.run(["tmux", "send-keys", "-t", created, "Enter"], capture_output=True, text=True, timeout=5)
        except Exception:
            logger.debug("Failed to export NEMO_* project env for %s", created, exc_info=True)
        # Admins don't receive the member global block, so give them the projects
        # convention directly (members already have it in their global context).
        try:
            if TEAM_MODE and _is_admin(user):
                acfg = _user_claude_config_dir(user)
                _sync_projects_note_into(acfg / "CLAUDE.md")
                # Disable the shared-account claude.ai connectors for admins too (same
                # leak/expiry issue) and give them our per-identity google MCP instead.
                _disable_claude_ai_connectors(acfg)
                _ensure_google_mcp(acfg, user)
                _set_team_model_effort(acfg)
                _sync_git_rules_into(acfg / "CLAUDE.md")
        except Exception:
            logger.debug("Failed to harden admin team config", exc_info=True)
        # For non-admin users, force their isolated CLAUDE_CONFIG_DIR so any
        # `claude` invocation in this pane reads from the user's private config.
        if user and not _is_admin(user):
            try:
                _ensure_user_claude_config_dir(user)
                user_cfg = _user_claude_config_dir(user)
                subprocess.run(
                    ["tmux", "send-keys", "-t", created, "-l",
                     f"export CLAUDE_CONFIG_DIR={shlex.quote(str(user_cfg))}"],
                    capture_output=True, text=True, timeout=5
                )
                subprocess.run(
                    ["tmux", "send-keys", "-t", created, "Enter"],
                    capture_output=True, text=True, timeout=5
                )
            except Exception:
                logger.exception("Failed to set per-user CLAUDE_CONFIG_DIR for '%s'", created)
        # Authenticate the session. Prefer the subscription PLAN (symlink to the
        # admin's live token); fall back to the shared API key (settings.json
        # `apiKeyHelper`, since interactive claude ignores a bare env var) only when
        # there's no live plan token. Admins use ~/.claude directly (their own login).
        if user and not _is_admin(user):
            _session_auth_mode[created] = _apply_member_auth(_user_claude_config_dir(user))
        else:
            _session_auth_mode[created] = "subscription"
        # Apply profile (CLAUDE_CONFIG_DIR) if requested. For non-admin users the
        # per-user dir we exported above takes precedence; honoring profile_id
        # would let one user point at another's profile, so we ignore it for
        # non-admins. Admins keep the existing behavior.
        requested_profile = (body.profile_id or "").strip() or DEFAULT_PROFILE_ID
        if _is_admin(user) and requested_profile != DEFAULT_PROFILE_ID:
            roles = _load_roles()
            if _find_profile(requested_profile, roles):
                roles["session_profiles"][created] = requested_profile
                _save_roles(roles)
                _send_profile_export(created, requested_profile)
            else:
                logger.warning("Unknown profile_id '%s' on session create", requested_profile)
        # When authenticating via a shared API key, prime the config dir's one-time
        # bypass-permissions acceptance BEFORE launching, or the detached session's
        # claude would hit the warning with no attached client and exit. Runs once
        # per config dir (marker-guarded); first session per user waits ~10-20s.
        if _stored_anthropic_key:
            await asyncio.to_thread(_prime_claude_config, _user_claude_config_dir(user))
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
async def api_delete_session(request: Request, session_name: str):
    """Kill a tmux session and all its child processes."""
    user = _current_user(request)
    _, sess = _find_session_for_user(session_name, user)
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
        _simple_watchdog_state.pop(session_name, None)
        _simple_watchdog_log.pop(session_name, None)
        _crash_recovery_state.pop(session_name, None)
        _seen_claude_running.discard(session_name)
        if session_name in _simple_watchdog_disabled:
            _simple_watchdog_disabled.discard(session_name)
            _save_simple_watchdog_disabled()
        # Drop session->profile mapping so a future session reusing this name
        # starts on the default profile.
        try:
            _roles = _load_roles()
            if session_name in _roles.get("session_profiles", {}):
                _roles["session_profiles"].pop(session_name, None)
                _save_roles(_roles)
        except Exception:
            logger.debug("Failed to clean up profile mapping for '%s'", session_name, exc_info=True)
        # Drop the ownership record. Messages/notes are kept on disk so they
        # show up in the user's History tab even after the live session dies.
        _clear_session_owner(session_name)
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
    """Upload a file to a session-specific uploads dir; record only in this session's chat history."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)

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
        # Per-session record only — never touch the project CLAUDE.md, which is
        # shared across every session running in the same cwd.
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


@app.get("/api/sessions/{session_name}/uploads")
async def api_list_uploads(session_name: str):
    """List previously uploaded files for a session (newest first)."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    uploads_dir = UPLOADS_DIR / session_name
    files = []
    if uploads_dir.exists():
        for entry in uploads_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                st = entry.stat()
                files.append({
                    "name": entry.name,
                    "path": str(entry),
                    "size": st.st_size,
                    "mtime": st.st_mtime,
                })
            except Exception:
                continue
    files.sort(key=lambda f: f["mtime"], reverse=True)
    return JSONResponse({"files": files})


@app.delete("/api/sessions/{session_name}/uploads/{filename}")
async def api_delete_upload(session_name: str, filename: str):
    """Remove a previously uploaded file from the session uploads dir."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    safe_name = os.path.basename(filename)
    if not safe_name or safe_name.startswith("."):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    target = UPLOADS_DIR / session_name / safe_name
    try:
        if target.exists() and target.is_file():
            target.unlink()
            return JSONResponse({"ok": True})
        return JSONResponse({"error": "File not found"}, status_code=404)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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


# --- Session-scoped auto-memory (project MEMORY.md + sibling topic files) ---
# Claude Code reads MEMORY.md from `<CLAUDE_CONFIG_DIR>/projects/<encoded-cwd>/memory/MEMORY.md`,
# where the encoded path replaces `/` and `_` with `-`. We mirror that here.

def _encode_project_path(cwd: str) -> str:
    """Mirror Claude Code's project-dir encoding: replace `/` and `_` with `-`."""
    return (cwd or "").replace("/", "-").replace("_", "-")


def _session_config_base(session_name: str) -> Path:
    """Resolve the CLAUDE_CONFIG_DIR a session actually uses.

    Non-admin users always use their isolated ``~/.claude-user-<id>/`` dir,
    overriding any profile_id that may have been recorded. Admin sessions fall
    back to the normal profile resolver.
    """
    owner = _user_for_session(session_name)
    if owner and not _is_admin(owner):
        return _user_claude_config_dir(owner)
    return _profile_dir(_get_session_profile_id(session_name))


def _session_memory_dir(session_name: str) -> tuple[Path, str, str]:
    """Resolve the project memory dir for a session.
    Returns (memory_dir_path, cwd, profile_id). memory_dir_path may not exist yet.
    """
    cwd = get_session_cwd(session_name) or ""
    profile_id = _get_session_profile_id(session_name)
    encoded = _encode_project_path(cwd)
    base = _session_config_base(session_name)
    mem_dir = base / "projects" / encoded / "memory"
    return mem_dir, cwd, profile_id


_MEMORY_EXTRA_RE = re.compile(r"^[A-Za-z0-9._-]+\.md$")


def _sanitize_memory_filename(name: str) -> str:
    name = os.path.basename(name or "")
    if not _MEMORY_EXTRA_RE.match(name):
        return ""
    return name


@app.get("/api/sessions/{session_name}/memory-md")
async def api_get_session_memory_md(session_name: str):
    """Read the auto-memory MEMORY.md for the session's (profile, cwd) pair."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    mem_dir, cwd, profile_id = _session_memory_dir(session_name)
    if not cwd:
        return JSONResponse({"error": "Could not determine session cwd"}, status_code=400)
    mpath = mem_dir / "MEMORY.md"
    content = ""
    if mpath.exists():
        try:
            content = mpath.read_text()
        except Exception:
            logger.debug("Failed to read session MEMORY.md at %s", mpath, exc_info=True)
    return JSONResponse({
        "path": str(mpath), "content": content, "exists": mpath.exists(),
        "dir": str(mem_dir), "cwd": cwd, "profile_id": profile_id,
    })


@app.post("/api/sessions/{session_name}/memory-md")
async def api_save_session_memory_md(session_name: str, body: SaveClaudeMd):
    """Save the auto-memory MEMORY.md (creates dir if missing)."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    mem_dir, cwd, _profile_id = _session_memory_dir(session_name)
    if not cwd:
        return JSONResponse({"error": "Could not determine session cwd"}, status_code=400)
    mpath = mem_dir / "MEMORY.md"
    # Path safety: ensure mpath is inside mem_dir which is inside the profile dir
    try:
        if not str(mpath.resolve().parent) == str(mem_dir.resolve()):
            return JSONResponse({"error": "Invalid path"}, status_code=400)
    except Exception:
        pass
    real_target = os.path.realpath(body.path)
    if real_target != os.path.realpath(str(mpath)):
        return JSONResponse({"error": "Path mismatch"}, status_code=400)
    try:
        mem_dir.mkdir(parents=True, exist_ok=True)
        mpath.write_text(body.content)
        return JSONResponse({"ok": True, "path": str(mpath)})
    except Exception:
        logger.exception("Failed to save session MEMORY.md")
        return JSONResponse({"error": "Failed to save"}, status_code=500)


@app.get("/api/sessions/{session_name}/memory-extras")
async def api_list_session_memory_extras(session_name: str):
    """List sibling .md topic files alongside MEMORY.md (excludes MEMORY.md itself)."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    mem_dir, cwd, _profile_id = _session_memory_dir(session_name)
    files = []
    if mem_dir.exists():
        for p in sorted(mem_dir.iterdir()):
            if not (p.is_file() and p.suffix == ".md"):
                continue
            if p.name.upper() == "MEMORY.MD":
                continue
            try:
                files.append({"name": p.name, "content": p.read_text(),
                              "size": p.stat().st_size})
            except Exception:
                logger.debug("Failed to read memory topic %s", p, exc_info=True)
    return JSONResponse({"files": files, "dir": str(mem_dir), "cwd": cwd})


@app.post("/api/sessions/{session_name}/memory-extras")
async def api_save_session_memory_extra(session_name: str, body: SkillFileBody):
    """Create or update a topic file in the session's memory dir."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    mem_dir, cwd, _profile_id = _session_memory_dir(session_name)
    if not cwd:
        return JSONResponse({"error": "Could not determine session cwd"}, status_code=400)
    fname = _sanitize_memory_filename(body.name)
    if not fname:
        return JSONResponse({"error": "Invalid filename. Use alphanumerics/dots/dashes/underscores ending in .md."}, status_code=400)
    if fname.upper() == "MEMORY.MD":
        return JSONResponse({"error": "MEMORY.md has its own editor."}, status_code=400)
    mem_dir.mkdir(parents=True, exist_ok=True)
    fpath = mem_dir / fname
    if not str(fpath.resolve()).startswith(str(mem_dir.resolve()) + os.sep):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    try:
        fpath.write_text(body.content)
        return JSONResponse({"ok": True, "name": fname})
    except Exception:
        logger.exception("Failed to save memory topic file")
        return JSONResponse({"error": "Failed to save file"}, status_code=500)


@app.delete("/api/sessions/{session_name}/memory-extras/{filename}")
async def api_delete_session_memory_extra(session_name: str, filename: str):
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    mem_dir, _cwd, _profile_id = _session_memory_dir(session_name)
    fname = _sanitize_memory_filename(filename)
    if not fname or fname.upper() == "MEMORY.MD":
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    fpath = mem_dir / fname
    if not str(fpath.resolve()).startswith(str(mem_dir.resolve()) + os.sep):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if fpath.exists():
        try:
            fpath.unlink()
        except Exception:
            logger.exception("Failed to delete memory topic file")
            return JSONResponse({"error": "Failed to delete"}, status_code=500)
    return JSONResponse({"ok": True})


# --- Global extras: sidecar markdown files in ~/ matching CLAUDE*.md ---
# (e.g. ~/CLAUDE_API_KEYS.md, ~/CLAUDE_GITHUB_RULES.md). The main CLAUDE.md
# has its own dedicated editor and is excluded here.
_GLOBAL_EXTRA_RE = re.compile(r"^CLAUDE[A-Za-z0-9._-]+\.md$")


def _sanitize_global_extra_filename(name: str) -> str:
    name = os.path.basename(name or "")
    if not _GLOBAL_EXTRA_RE.match(name):
        return ""
    if name.upper() == "CLAUDE.MD":
        return ""
    return name


@app.get("/api/global-extras")
async def api_list_global_extras():
    home = Path.home()
    files = []
    try:
        for p in sorted(home.iterdir()):
            if not p.is_file():
                continue
            if not _GLOBAL_EXTRA_RE.match(p.name):
                continue
            if p.name.upper() == "CLAUDE.MD":
                continue
            try:
                files.append({"name": p.name, "content": p.read_text(),
                              "size": p.stat().st_size})
            except Exception:
                logger.debug("Failed to read global extra %s", p, exc_info=True)
    except Exception:
        logger.exception("Failed to list global extras")
    return JSONResponse({"files": files, "path": str(home)})


@app.post("/api/global-extras")
async def api_save_global_extra(body: SkillFileBody):
    fname = _sanitize_global_extra_filename(body.name)
    if not fname:
        return JSONResponse({"error": "Filename must match CLAUDE_<something>.md (e.g. CLAUDE_API_KEYS.md)."}, status_code=400)
    home = Path.home()
    fpath = home / fname
    if not str(fpath.resolve()).startswith(str(home.resolve()) + os.sep):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    try:
        fpath.write_text(body.content)
        # API-key-style files often need restrictive perms; we apply 600
        # because these conventionally hold secrets and live next to CLAUDE.md.
        try:
            os.chmod(fpath, 0o600)
        except Exception:
            logger.debug("chmod 600 failed for %s", fpath, exc_info=True)
        return JSONResponse({"ok": True, "name": fname})
    except Exception:
        logger.exception("Failed to save global extra")
        return JSONResponse({"error": "Failed to save file"}, status_code=500)


@app.delete("/api/global-extras/{filename}")
async def api_delete_global_extra(filename: str):
    fname = _sanitize_global_extra_filename(filename)
    if not fname:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    home = Path.home()
    fpath = home / fname
    if not str(fpath.resolve()).startswith(str(home.resolve()) + os.sep):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if fpath.exists():
        try:
            fpath.unlink()
        except Exception:
            logger.exception("Failed to delete global extra")
            return JSONResponse({"error": "Failed to delete"}, status_code=500)
    return JSONResponse({"ok": True})


# --- Skills file management ---
#
# There are three layers:
#   1. Library:  ~/.tmux-dashboard/skill-library/<name>/SKILL.md
#                Canonical user-authored skills with YAML frontmatter
#                (name + description). This is the source of truth.
#   2. Profile:  ~/.claude-<profile_id>/skills/<name>/SKILL.md
#                Per-profile skills directory that Claude Code reads when
#                CLAUDE_CONFIG_DIR is set. Library skills are *symlinked* in
#                here on a per-profile basis, so the same library entry can
#                be enabled in some profiles and disabled in others.
#   3. Built-ins: bundled into the Claude Code binary itself. Always present,
#                not configurable per profile. Listed via /api/builtin-skills
#                so the UI can surface them as read-only.

SKILLS_DIR = MESSAGES_DIR / "skills"
SKILL_LIBRARY_DIR = MESSAGES_DIR / "skill-library"

_SKILL_FILENAME_RE = re.compile(r"^[a-zA-Z0-9_-]+\.md$")
_SKILL_DIR_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")

# Built-in skills bundled with Claude Code itself. Always available; cannot be
# disabled per profile. Surfaced to the UI as a read-only "Built-in" section
# so users understand which skills are available without any configuration.
_BUILTIN_SKILLS = [
    {"name": "update-config", "description": "Configure Claude Code via settings.json (hooks, permissions, env)."},
    {"name": "keybindings-help", "description": "Customize keyboard shortcuts in ~/.claude/keybindings.json."},
    {"name": "simplify", "description": "Review changed code for reuse, quality, and efficiency, then fix issues."},
    {"name": "fewer-permission-prompts", "description": "Allowlist common read-only Bash/MCP calls to reduce prompts."},
    {"name": "loop", "description": "Run a prompt or slash command on a recurring interval."},
    {"name": "claude-api", "description": "Build, debug, and optimize Claude API / Anthropic SDK apps."},
    {"name": "init", "description": "Initialize a new CLAUDE.md file with codebase documentation."},
    {"name": "review", "description": "Review a pull request."},
    {"name": "security-review", "description": "Complete a security review of pending changes on the current branch."},
]


def _sanitize_skill_filename(name: str) -> str:
    """Sanitize and validate a flat .md skill filename (legacy session/profile flat-file API)."""
    name = os.path.basename(name)
    if not name.endswith(".md"):
        name += ".md"
    if not _SKILL_FILENAME_RE.match(name):
        return ""
    return name


def _sanitize_skill_dir_name(name: str) -> str:
    """Sanitize a skill directory name (the canonical Skill `<name>`)."""
    name = os.path.basename((name or "").strip())
    if name.endswith(".md"):
        name = name[:-3]
    if not _SKILL_DIR_NAME_RE.match(name):
        return ""
    return name


def _parse_skill_frontmatter(skill_md_path: Path) -> dict:
    """Extract `name` and `description` from a SKILL.md YAML frontmatter block.

    Falls back to the parent directory name when frontmatter is missing or malformed.
    """
    out = {"name": skill_md_path.parent.name, "description": ""}
    try:
        text = skill_md_path.read_text()
    except Exception:
        return out
    if not text.startswith("---"):
        return out
    # Find the closing fence
    end = text.find("\n---", 3)
    if end == -1:
        return out
    block = text[3:end]
    for raw in block.splitlines():
        line = raw.strip()
        if line.startswith("name:"):
            v = line.split(":", 1)[1].strip().strip('"').strip("'")
            if v:
                out["name"] = v
        elif line.startswith("description:"):
            v = line.split(":", 1)[1].strip().strip('"').strip("'")
            if v:
                out["description"] = v
    return out


def _read_skill_dir(d: Path) -> Optional[dict]:
    """Read a skill directory; return metadata dict or None if not a valid skill."""
    if not d.is_dir():
        return None
    skill_md = d / "SKILL.md"
    if not skill_md.is_file():
        return None
    fm = _parse_skill_frontmatter(skill_md)
    try:
        content = skill_md.read_text()
    except Exception:
        content = ""
    return {
        "name": fm["name"],
        "dir_name": d.name,
        "description": fm["description"],
        "path": str(skill_md),
        "content": content,
    }


def _list_library_skills() -> list:
    """List all skills in the library (sorted by directory name)."""
    SKILL_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    out = []
    for entry in sorted(SKILL_LIBRARY_DIR.iterdir()):
        info = _read_skill_dir(entry)
        if info:
            out.append(info)
    return out


def _profile_skills_dir(profile_id: str) -> Path:
    """Return the skills/ directory for a given profile, creating it if needed."""
    d = _profile_dir(profile_id) / "skills"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _is_library_link(skills_dir: Path, skill_dir_name: str) -> bool:
    """Return True iff `skills_dir/skill_dir_name` is a symlink to the library copy."""
    target = skills_dir / skill_dir_name
    if not target.is_symlink():
        return False
    try:
        resolved = target.resolve()
    except Exception:
        return False
    expected = (SKILL_LIBRARY_DIR / skill_dir_name).resolve()
    return resolved == expected


def _skill_dir_for_session(session_name: str) -> Path:
    d = SKILLS_DIR / session_name
    d.mkdir(parents=True, exist_ok=True)
    return d


class SkillFileBody(BaseModel):
    name: str
    content: str


class SaveLibrarySkillBody(BaseModel):
    description: str = ""
    content: str


class SkillLibraryBody(BaseModel):
    name: str
    session_name: str = ""


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
    """List all skills in the library with their frontmatter metadata."""
    return JSONResponse({"skills": _list_library_skills()})


@app.get("/api/skill-library/{skill_name}")
async def api_get_library_skill(skill_name: str):
    """Return the SKILL.md content + metadata for a single library skill."""
    name = _sanitize_skill_dir_name(skill_name)
    if not name:
        return JSONResponse({"error": "Invalid skill name"}, status_code=400)
    info = _read_skill_dir(SKILL_LIBRARY_DIR / name)
    if not info:
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    return JSONResponse(info)


@app.post("/api/skill-library/{skill_name}")
async def api_save_library_skill(skill_name: str, body: SaveLibrarySkillBody):
    """Create or update a library skill.

    Body content is the raw SKILL.md text. If it already starts with a `---`
    frontmatter block, it is trusted as-is. Otherwise we synthesize a frontmatter
    block from `skill_name` and `description`.
    """
    name = _sanitize_skill_dir_name(skill_name)
    if not name:
        return JSONResponse({"error": "Invalid skill name (alphanumeric, hyphens, underscores; max 64 chars)"}, status_code=400)
    desc = (body.description or "").strip().replace("\n", " ").replace("\r", " ")
    raw = (body.content or "").lstrip()
    if raw.startswith("---"):
        full = raw if raw.endswith("\n") else raw + "\n"
    else:
        full = f"---\nname: {name}\ndescription: {desc}\n---\n\n{raw.rstrip()}\n"
    d = SKILL_LIBRARY_DIR / name
    try:
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(full)
    except Exception:
        logger.exception("Failed to save library skill")
        return JSONResponse({"error": "Failed to save skill"}, status_code=500)
    return JSONResponse({"ok": True, "name": name})


@app.delete("/api/skill-library/{skill_name}")
async def api_delete_library_skill(skill_name: str):
    """Delete a library skill (and break any per-profile symlinks pointing to it)."""
    name = _sanitize_skill_dir_name(skill_name)
    if not name:
        return JSONResponse({"error": "Invalid skill name"}, status_code=400)
    d = SKILL_LIBRARY_DIR / name
    if not d.is_dir():
        return JSONResponse({"error": "Skill not found"}, status_code=404)
    # Sweep all profile skills/ dirs and remove dangling/library-pointing symlinks
    try:
        for profile in _load_roles().get("profiles", []):
            pid = profile.get("id")
            if not pid or pid == DEFAULT_PROFILE_ID:
                continue
            sd = _profile_dir(pid) / "skills"
            link = sd / name
            if link.is_symlink():
                try:
                    link.unlink()
                except Exception:
                    logger.debug("Failed to clean up profile symlink %s", link, exc_info=True)
    except Exception:
        logger.debug("Failed to sweep profile symlinks for deleted skill", exc_info=True)
    try:
        shutil.rmtree(str(d))
    except Exception:
        logger.exception("Failed to delete library skill")
        return JSONResponse({"error": "Failed to delete skill"}, status_code=500)
    return JSONResponse({"ok": True})


@app.get("/api/builtin-skills")
async def api_list_builtin_skills():
    """Return the list of skills bundled with Claude Code itself (read-only)."""
    return JSONResponse({"skills": list(_BUILTIN_SKILLS)})


# --- Claude Code role profiles (per-role isolated configs via CLAUDE_CONFIG_DIR) ---

ROLES_FILE = MESSAGES_DIR / "claude-roles.json"
DEFAULT_PROFILE_ID = "default"
_PROFILE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_RESERVED_PROFILE_IDS = {DEFAULT_PROFILE_ID}

# Common permissions + env baseline applied to every profile (uniform
# "no permission prompts + telemetry off + generous bash timeouts" across
# all roles). Individual profiles can still override via the Profiles editor.
_COMMON_PERMISSIONS = {
    "defaultMode": "bypassPermissions",
    "allow": ["*"],
    "deny": [],
}
_COMMON_ENV = {
    "CLAUDE_CODE_PERMISSION_MODE": "bypassPermissions",
    "DISABLE_AUTOUPDATER": "0",
    "DISABLE_TELEMETRY": "1",
    "BASH_DEFAULT_TIMEOUT_MS": "120000",
    "BASH_MAX_TIMEOUT_MS": "600000",
}

# Built-in role presets seeded on first run. Each becomes ~/.claude-<id>/
# with settings.json, CLAUDE.md, and an empty skills/ dir. The user can edit
# any of these via the Profiles editor in Settings.
_PROFILE_PRESETS = [
    {
        "id": "ui-expert", "name": "UI Expert",
        "model": "claude-sonnet-4-6", "effort": "medium",
        "permissions": dict(_COMMON_PERMISSIONS),
        "env": dict(_COMMON_ENV),
        "claude_md": (
            "# UI Expert\n\n"
            "## Verify visually\n"
            "After every UI change: run dev server, screenshot the affected viewport at 375/768/1280/1920px. "
            "Compare against the previous screenshot. Don't claim \"done\" without the diff.\n\n"
            "## Design tokens are law\n"
            "Use design tokens from `src/styles/tokens.css`. No raw hex, rem, or px in component files. "
            "If a token is missing, propose adding it before hardcoding.\n\n"
            "## Component patterns\n"
            "- Composition > props explosion. Slot/children before 12-prop variants.\n"
            "- Every interactive element must have :hover, :focus-visible, :disabled, :active.\n"
            "- Motion: prefers-reduced-motion respected always.\n\n"
            "## Accessibility floor\n"
            "WCAG 2.2 AA minimum. Every PR runs axe via Playwright. Never ship contrast < 4.5:1.\n"
        ),
    },
    {
        "id": "ux-expert", "name": "UX Expert",
        "model": "claude-opus-4-8[1m]", "effort": "high",
        "permissions": dict(_COMMON_PERMISSIONS),
        "env": dict(_COMMON_ENV),
        "claude_md": (
            "# UX Expert\n\n"
            "## Process discipline\n"
            "Problem -> user -> JTBD -> flow -> wireframe -> copy -> test plan. "
            "Never jump to UI before the JTBD is written down in one sentence.\n\n"
            "## Outputs\n"
            "- Flows: mermaid stateDiagram or flowchart\n"
            "- Heuristic evals: severity 0-4 (Nielsen scale), one finding per row, with screenshot ref\n"
            "- Microcopy: provide 3 variants, label each by tone (direct / warm / playful)\n\n"
            "## Synthesis rules\n"
            "When given >3 transcripts: cluster by theme first, count mentions, "
            "quote sparingly (<=15 words), surface contradictions explicitly.\n\n"
            "## Reject without data\n"
            "If asked \"should we add X feature\": ask what user evidence exists. "
            "No evidence -> propose smallest test to gather it before designing.\n"
        ),
    },
    {
        "id": "qa-agent", "name": "QA Agent",
        "model": "claude-sonnet-4-6", "effort": "medium",
        "permissions": dict(_COMMON_PERMISSIONS),
        "env": dict(_COMMON_ENV),
        "claude_md": (
            "# QA Agent\n\n"
            "## Test pyramid targets\n"
            "70% unit, 20% integration, 10% e2e. Question any PR that inverts this.\n\n"
            "## Coverage rules\n"
            "Lines >= 80%, branches >= 75%. New code: >= 90% lines or justify in PR. "
            "Coverage is necessary, not sufficient -- also assert behavior, not just calls.\n\n"
            "## Flake protocol\n"
            "Failed test re-run passes? Don't merge. Mark `.flaky`, file an issue, "
            "investigate root cause within 48h. Never @retry on green CI.\n\n"
            "## Edge case checklist (always)\n"
            "- empty / null / undefined inputs\n"
            "- boundary values (0, 1, max, max+1, negative)\n"
            "- concurrent operations / race conditions\n"
            "- network failure / timeout\n"
            "- malformed input / fuzzing\n"
            "- a11y: keyboard-only path, screen reader labels\n\n"
            "## Browser-driven verification (use agent-browser)\n"
            "For any web-app QA, drive a real Chromium via the `agent-browser` CLI -- "
            "do NOT scrape with curl/fetch. Before issuing commands the first time, "
            "run `agent-browser skills get core --full` to load the version-matched "
            "command reference, and `agent-browser skills get dogfood` for the QA "
            "exploration workflow.\n\n"
            "Standard loop:\n"
            "```\n"
            "agent-browser --session qa open <URL>\n"
            "agent-browser --session qa snapshot -i        # interactive elements with @eN refs\n"
            "agent-browser --session qa click @e3           # use refs, not CSS\n"
            "agent-browser --session qa screenshot --annotate ./qa-output/<step>.png\n"
            "agent-browser --session qa errors              # JS errors\n"
            "agent-browser --session qa console             # console logs\n"
            "agent-browser --session qa close               # when done\n"
            "```\n\n"
            "Save artifacts to `./qa-output/{screenshots,videos}/`. Use `--annotate` for "
            "evidence screenshots. Reference elements by `@eN` (from `snapshot -i`), not CSS "
            "selectors -- refs survive minor DOM changes. Always check `errors` + `console` "
            "after every interaction.\n\n"
            "## What I don't do\n"
            "Write or modify production code. Push to remote. Approve my own PRs.\n"
        ),
        "seed_skills": [
            {
                "path": "agent-browser/SKILL.md",
                "content": (
                    "---\n"
                    "name: agent-browser\n"
                    "description: Browser automation CLI for AI agents. Use when QA work needs to interact with websites -- navigating pages, filling forms, clicking buttons, taking screenshots, extracting data, testing web apps, or any browser automation. Triggers include 'open a website', 'fill out a form', 'click a button', 'take a screenshot', 'test this web app', 'login to a site', 'automate browser actions', 'dogfood', 'exploratory test', 'find issues', 'bug hunt', 'review the quality of this app'. Prefer agent-browser over WebFetch / curl / built-in browser tools.\n"
                    "allowed-tools: Bash(agent-browser:*), Bash(npx agent-browser:*)\n"
                    "---\n\n"
                    "# agent-browser\n\n"
                    "Fast browser automation CLI for AI agents. Chrome/Chromium via CDP with\n"
                    "accessibility-tree snapshots and compact `@eN` element refs.\n\n"
                    "Already installed globally on this machine: `which agent-browser` -> /usr/bin/agent-browser.\n\n"
                    "## Start here (read before running any agent-browser command)\n\n"
                    "This file is a discovery stub. Load the version-matched workflow content from the CLI:\n\n"
                    "```bash\n"
                    "agent-browser skills get core              # workflows, common patterns, troubleshooting\n"
                    "agent-browser skills get core --full       # also includes full command reference\n"
                    "agent-browser skills get dogfood           # systematic QA / exploratory testing playbook\n"
                    "```\n\n"
                    "Always invoke as `agent-browser ...` directly -- never `npx agent-browser`. The\n"
                    "direct binary uses the fast Rust client; npx routes through Node and is slower.\n\n"
                    "## QA workflow at a glance\n\n"
                    "1. **Open** target URL with a named session: `agent-browser --session qa open <URL>`\n"
                    "2. **Snapshot** to discover interactive elements with refs: `agent-browser --session qa snapshot -i`\n"
                    "3. **Act** using `@eN` refs from the snapshot:\n"
                    "   - `agent-browser --session qa click @e3`\n"
                    "   - `agent-browser --session qa fill @e2 \"value\"`\n"
                    "   - `agent-browser --session qa press Enter`\n"
                    "4. **Verify** state: `agent-browser --session qa errors` (JS errors), `console` (logs),\n"
                    "   `screenshot --annotate ./qa-output/<step>.png` (visual evidence).\n"
                    "5. **Close** when finished: `agent-browser --session qa close`.\n\n"
                    "Always:\n"
                    "- Use `@eN` refs from `snapshot -i`, not CSS selectors -- refs survive minor DOM changes.\n"
                    "- After every interaction check `errors` and `console` for regressions.\n"
                    "- Take an annotated screenshot of every reproducible bug. Save under `./qa-output/`.\n"
                    "- Use `--session <name>` so login/cookies persist between commands.\n\n"
                    "## QA report format\n\n"
                    "Return findings as structured JSON when the user asks for a report:\n\n"
                    "```json\n"
                    "{\n"
                    "  \"verdict\": \"HEALTHY | MINOR_ISSUES | CRITICAL_BUGS\",\n"
                    "  \"summary\": \"<one-paragraph executive summary>\",\n"
                    "  \"bugs\": [\n"
                    "    {\n"
                    "      \"severity\": \"critical | major | minor\",\n"
                    "      \"title\": \"<short>\",\n"
                    "      \"description\": \"<what, where, repro steps, expected vs actual>\",\n"
                    "      \"evidence\": \"<path to screenshot or log>\"\n"
                    "    }\n"
                    "  ]\n"
                    "}\n"
                    "```\n\n"
                    "Severity rubric:\n"
                    "- **critical**: blocks the user (crash, broken login, data loss, security)\n"
                    "- **major**: core feature degraded but workaround exists\n"
                    "- **minor**: cosmetic, copy, polish\n"
                ),
            },
            {
                "path": "qa-browser-checklist.md",
                "content": (
                    "---\n"
                    "name: qa-browser-checklist\n"
                    "description: Per-page QA checklist to run after navigating to any web page during exploratory testing. Use alongside agent-browser. Trigger when doing QA, dogfood, exploratory test, smoke test, or bug hunt on a web app.\n"
                    "---\n\n"
                    "# Per-page QA checklist\n\n"
                    "After every page load (or significant state change) during browser-driven QA, run\n"
                    "through this checklist before moving on. Skip items that are clearly not applicable.\n\n"
                    "## 1. Render & layout\n"
                    "- Page loads without spinner stuck > 5s\n"
                    "- No empty regions where content should be\n"
                    "- No overlapping or cut-off text (check at 1280 and 375 viewport)\n"
                    "- Critical above-the-fold content visible without scrolling\n\n"
                    "## 2. Console & network\n"
                    "```\n"
                    "agent-browser --session qa errors\n"
                    "agent-browser --session qa console\n"
                    "agent-browser --session qa network requests --filter 4xx\n"
                    "agent-browser --session qa network requests --filter 5xx\n"
                    "```\n"
                    "Any 4xx/5xx other than expected auth challenges is a finding. JS exceptions\n"
                    "are a finding even if the page looks fine.\n\n"
                    "## 3. Interactivity\n"
                    "- Primary CTA reachable by Tab from the top of the page\n"
                    "- :focus-visible ring present on every focusable element\n"
                    "- Forms: try empty submit, invalid email, too-long input, special chars in name\n"
                    "- Modals/dialogs close with Escape and with the close button\n\n"
                    "## 4. Accessibility floor\n"
                    "- Every form field has a label (use `agent-browser snapshot -i` and look for unlabelled inputs)\n"
                    "- Images have alt text\n"
                    "- Color contrast looks adequate; flag any low-contrast text for verification\n\n"
                    "## 5. Performance smell test\n"
                    "```\n"
                    "agent-browser --session qa vitals\n"
                    "```\n"
                    "Flag LCP > 2.5s, INP > 200ms, CLS > 0.1 on the critical path.\n\n"
                    "## 6. State leakage\n"
                    "- Refresh the page -- does state persist as expected?\n"
                    "- Navigate away and back -- does it restore correctly?\n"
                    "- Open in a new tab -- does it work standalone?\n\n"
                    "## What goes in the bug report\n\n"
                    "For each finding: title, severity, exact repro steps using `@eN` refs from a fresh\n"
                    "`snapshot -i`, expected vs actual, screenshot path under `./qa-output/screenshots/`,\n"
                    "and a console/errors excerpt if relevant.\n"
                ),
            },
        ],
    },
    {
        "id": "researcher", "name": "Researcher",
        "model": "claude-opus-4-8[1m]", "effort": "high",
        "permissions": dict(_COMMON_PERMISSIONS),
        "env": dict(_COMMON_ENV),
        "claude_md": (
            "# Researcher\n\n"
            "## Output format (always)\n"
            "1. Executive summary (<=150 words)\n"
            "2. Key findings (bulleted, each with source link)\n"
            "3. Methodology\n"
            "4. Detailed sections\n"
            "5. Open questions\n"
            "6. Sources (numbered, full citations)\n\n"
            "Save to `.research/<topic>/final_report.md`. Cache raw fetches in `.research/<topic>/raw/`.\n\n"
            "## Source hierarchy\n"
            "Primary > peer-reviewed > industry reports > reputable news > blogs > forums. "
            "Every load-bearing claim needs a primary source.\n\n"
            "## Decomposition first\n"
            "For any non-trivial query: write 3-7 sub-questions before searching. "
            "Run searches in parallel where possible. Aggregate, then synthesize.\n\n"
            "## Quote discipline\n"
            "Paraphrase by default. Direct quotes only when wording is legally / technically "
            "load-bearing. Max one quote per source, <=15 words.\n\n"
            "## Uncertainty is a finding\n"
            "\"I couldn't determine X\" is a valid output. Don't fabricate to fill gaps.\n"
        ),
    },
    {
        "id": "security-expert", "name": "Security Expert",
        "model": "claude-opus-4-8[1m]", "effort": "high",
        "permissions": dict(_COMMON_PERMISSIONS),
        "env": dict(_COMMON_ENV),
        "claude_md": (
            "# Security Expert\n\n"
            "## Default posture: read-only\n"
            "I review, report, and recommend. I do not patch unless explicitly asked "
            "(\"apply the fix\"). I do not run network commands. I do not exfiltrate "
            "data -- even sample logs go in redacted form.\n\n"
            "## Review framework\n"
            "For every concern: STRIDE category -> CVSS estimate -> exploitability notes -> "
            "proof-of-concept (sanitized) -> remediation -> references (CWE/CVE/OWASP).\n\n"
            "## Always check\n"
            "- Secrets in code/config/history (gitleaks before review)\n"
            "- Dependency CVEs (npm/pip/cargo audit)\n"
            "- Auth: session fixation, CSRF, OAuth state, JWT alg=none, weak rotation\n"
            "- Injection: SQL, command, SSTI, prompt injection in LLM contexts\n"
            "- Crypto: deprecated algorithms, ECB mode, hardcoded IVs, weak RNG\n"
            "- IDOR / authz at every endpoint, not just authn\n"
            "- SSRF / file inclusion / path traversal\n"
            "- Supply chain: lockfile drift, typosquats, install scripts\n\n"
            "## Reporting\n"
            "Severity: Critical / High / Medium / Low / Info. One issue per finding.\n\n"
            "## What I don't do\n"
            "Run exploits against live systems. Modify .env / secrets / IAM. "
            "Disable checks \"to test something.\" Approve my own findings.\n"
        ),
    },
    {
        "id": "optimizer", "name": "Optimizer",
        "model": "claude-opus-4-8[1m]", "effort": "high",
        "permissions": dict(_COMMON_PERMISSIONS),
        "env": dict(_COMMON_ENV),
        "claude_md": (
            "# Optimizer\n\n"
            "## Measure first, always\n"
            "No optimization without a baseline number. Format every proposal as:\n"
            "  Before: X (units, conditions)\n"
            "  After:  Y (same conditions)\n"
            "  Method: how measured, repetitions, variance\n\n"
            "If I can't produce \"Before\", I don't propose \"After\".\n\n"
            "## Performance budgets (web)\n"
            "- LCP < 2.5s, INP < 200ms, CLS < 0.1 (75th percentile, mobile, slow 4G)\n"
            "- JS bundle: < 170KB gzipped initial route\n"
            "- Lighthouse perf >= 90 on every PR touching the critical path\n\n"
            "## Order of operations\n"
            "1. Profile (find the hot path)\n"
            "2. Algorithmic fix (Big-O)\n"
            "3. Reduce work (memo, dedupe, cache)\n"
            "4. Parallelize / defer\n"
            "5. Lower-level tuning (allocations, syscalls)\n"
            "Never invert this order.\n\n"
            "## Anti-patterns I flag\n"
            "- Premature memo / useMemo on cheap pure expressions\n"
            "- Cache without invalidation strategy\n"
            "- \"It's faster on my machine\" without prod-like data volume\n"
            "- Micro-benchmarks without warmup, GC pause, statistical test\n"
            "- Optimizing the 1% case while the 99% case is the bottleneck\n\n"
            "## Cost dimension\n"
            "Performance includes $ -- token cost, compute cost, egress cost. "
            "Always note cost delta alongside latency delta.\n"
        ),
    },
]


def _default_profile_record() -> dict:
    return {"id": DEFAULT_PROFILE_ID, "name": "Default", "model": "",
            "effort": "",
            "permissions": dict(_COMMON_PERMISSIONS),
            "env": dict(_COMMON_ENV),
            "claude_md": "", "memory_md": "", "builtin": True}


def _save_roles(data: dict):
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        ROLES_FILE.write_text(json.dumps(data, indent=2))
    except Exception:
        logger.exception("Failed to save %s", ROLES_FILE)


_PRESET_FIELDS = ("model", "effort", "permissions", "env", "claude_md", "memory_md", "seed_skills")


def _refresh_builtin_presets(data: dict) -> bool:
    """Bring built-in preset entries up-to-date with `_PROFILE_PRESETS` content.

    Only touches profiles where `builtin: True` AND `edited` is falsy. New presets
    that weren't in the file yet are appended. The default profile is also
    refreshed from `_default_profile_record()`. Returns True if any change.
    """
    changed = False
    by_id = {p["id"]: p for p in data["profiles"]}
    # Refresh the default record (treating _default_profile_record() as its preset)
    default_existing = by_id.get(DEFAULT_PROFILE_ID)
    if default_existing is not None and not default_existing.get("edited"):
        default_template = _default_profile_record()
        for field in _PRESET_FIELDS:
            new_val = default_template.get(field) if field in default_template else None
            if default_existing.get(field) != new_val:
                if new_val is None:
                    default_existing.pop(field, None)
                else:
                    default_existing[field] = new_val
                changed = True
    for preset in _PROFILE_PRESETS:
        existing = by_id.get(preset["id"])
        if existing is None:
            data["profiles"].append({**preset, "builtin": True})
            changed = True
            continue
        if not existing.get("builtin") or existing.get("edited"):
            continue
        for field in _PRESET_FIELDS:
            new_val = preset.get(field) if field in preset else None
            if existing.get(field) != new_val:
                if new_val is None:
                    existing.pop(field, None)
                else:
                    existing[field] = new_val
                changed = True
        # Don't auto-rename: name is the most user-visible field
    return changed


def _load_roles() -> dict:
    """Load profiles + per-session mappings; seed presets on first run."""
    if not ROLES_FILE.exists():
        data = {
            "profiles": [_default_profile_record()] + [{**p, "builtin": True} for p in _PROFILE_PRESETS],
            "session_profiles": {},
        }
        _save_roles(data)
        return data
    try:
        with open(ROLES_FILE) as f:
            data = json.load(f)
        data.setdefault("profiles", [])
        data.setdefault("session_profiles", {})
        if not any(p.get("id") == DEFAULT_PROFILE_ID for p in data["profiles"]):
            data["profiles"].insert(0, _default_profile_record())
        if _refresh_builtin_presets(data):
            _save_roles(data)
        return data
    except Exception:
        logger.exception("Failed to load %s -- using defaults", ROLES_FILE)
        return {"profiles": [_default_profile_record()], "session_profiles": {}}


def _profile_dir(profile_id: str) -> Path:
    """Filesystem path used for CLAUDE_CONFIG_DIR for a given profile id."""
    if profile_id == DEFAULT_PROFILE_ID:
        return Path.home() / ".claude"
    return Path.home() / f".claude-{profile_id}"


def _materialize_profile(profile: dict):
    """Write settings.json, CLAUDE.md, and ensure skills/ exists.

    For non-default profiles: settings.json is fully owned by the dashboard and
    overwritten with the profile content (we created the dir).

    For the default profile (~/.claude): MERGE settings.json so we only touch
    `model`, `env`, `permissions` -- preserving any other keys the user has
    (e.g. `preferences`, `spinnerTipsEnabled`). On first write we back up the
    existing settings.json once.
    """
    pid = profile["id"]
    d = _profile_dir(pid)
    d.mkdir(parents=True, exist_ok=True)
    (d / "skills").mkdir(parents=True, exist_ok=True)
    is_default = (pid == DEFAULT_PROFILE_ID)

    settings_path = d / "settings.json"
    claudemd_path = d / "CLAUDE.md"
    memorymd_path = d / "MEMORY.md"

    # Compose the dashboard-managed slice
    managed: dict = {}
    if profile.get("model"):
        managed["model"] = profile["model"]
    env = dict(profile.get("env") or {})
    if profile.get("effort"):
        env.setdefault("CLAUDE_CODE_EFFORT_LEVEL", profile["effort"])
    if env:
        managed["env"] = env
    if profile.get("permissions"):
        managed["permissions"] = profile["permissions"]

    try:
        if is_default:
            # Merge: preserve user's existing keys outside our managed slice
            existing: dict = {}
            if settings_path.exists():
                try:
                    existing = json.loads(settings_path.read_text())
                    if not isinstance(existing, dict):
                        existing = {}
                except Exception:
                    logger.warning("~/.claude/settings.json was not valid JSON; will rewrite")
                    existing = {}
                # One-time backup before our first write
                bak = settings_path.with_suffix(".json.bak-pre-dashboard")
                if not bak.exists():
                    try:
                        bak.write_text(settings_path.read_text())
                    except Exception:
                        logger.debug("Failed to back up ~/.claude/settings.json", exc_info=True)
            merged = dict(existing)
            for key in ("model", "env", "permissions"):
                if key in managed:
                    merged[key] = managed[key]
                # If user blanked the field via the editor, drop it from settings
                elif key in merged and not profile.get(key) and key in ("model",):
                    merged.pop(key, None)
            settings_path.write_text(json.dumps(merged, indent=2))
        else:
            settings_path.write_text(json.dumps(managed, indent=2))
        claudemd_path.write_text(profile.get("claude_md") or "")
        memorymd_path.write_text(profile.get("memory_md") or "")
        # Seed initial skill files only when they are missing -- never overwrite,
        # so the user (or a fresh `agent-browser skills get` pull) can edit them.
        seed_skills = profile.get("seed_skills") or []
        if seed_skills:
            skills_root = d / "skills"
            for entry in seed_skills:
                rel = (entry.get("path") or "").lstrip("/").replace("\\", "/")
                content = entry.get("content") or ""
                if not rel:
                    continue
                target = (skills_root / rel).resolve()
                # Path traversal guard
                try:
                    target.relative_to(skills_root.resolve())
                except ValueError:
                    logger.warning("Skipping seed skill outside skills dir: %s", rel)
                    continue
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content)
    except Exception:
        logger.exception("Failed to materialize profile %s at %s", pid, d)


def _find_profile(profile_id: str, data: dict | None = None):
    if data is None:
        data = _load_roles()
    for p in data["profiles"]:
        if p["id"] == profile_id:
            return p
    return None


def _get_session_profile_id(session_name: str) -> str:
    data = _load_roles()
    return data["session_profiles"].get(session_name) or DEFAULT_PROFILE_ID


# Seed roles file + materialize built-ins at import time
try:
    _initial_roles = _load_roles()
    for _p in _initial_roles["profiles"]:
        if _p["id"] != DEFAULT_PROFILE_ID:
            _materialize_profile(_p)
except Exception:
    logger.exception("Failed to initialize role profiles")


def _profile_summary(p: dict) -> dict:
    return {
        "id": p["id"], "name": p.get("name", p["id"]),
        "model": p.get("model", ""), "effort": p.get("effort", ""),
        "builtin": bool(p.get("builtin")),
    }


class CreateProfileBody(BaseModel):
    name: str
    from_preset: str = ""


class UpdateProfileBody(BaseModel):
    name: Optional[str] = None
    model: Optional[str] = None
    effort: Optional[str] = None
    claude_md: Optional[str] = None
    memory_md: Optional[str] = None
    permissions: Optional[dict] = None
    env: Optional[dict] = None


class SetSessionProfileBody(BaseModel):
    profile_id: str
    restart: bool = False


@app.get("/api/profiles")
async def api_list_profiles():
    data = _load_roles()
    return JSONResponse({"profiles": [_profile_summary(p) for p in data["profiles"]]})


@app.get("/api/profiles/{profile_id}")
async def api_get_profile(profile_id: str):
    data = _load_roles()
    p = _find_profile(profile_id, data)
    if not p:
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    out = dict(p)
    out["dir"] = "" if profile_id == DEFAULT_PROFILE_ID else str(_profile_dir(profile_id))
    return JSONResponse(out)


@app.post("/api/profiles")
async def api_create_profile(body: CreateProfileBody):
    name = body.name.strip()
    if not name:
        return JSONResponse({"error": "Name is required"}, status_code=400)
    pid = re.sub(r"[^a-z0-9-]", "-", name.lower())
    pid = re.sub(r"-+", "-", pid).strip("-")[:32]
    if not pid or not _PROFILE_ID_RE.match(pid) or pid in _RESERVED_PROFILE_IDS:
        return JSONResponse({"error": "Invalid name (use letters/numbers; can't be 'default')"}, status_code=400)
    data = _load_roles()
    if any(p["id"] == pid for p in data["profiles"]):
        return JSONResponse({"error": f"Profile id '{pid}' already exists"}, status_code=409)
    base = _find_profile(body.from_preset, data) if body.from_preset else None
    new_p = {
        "id": pid, "name": name,
        "model": (base or {}).get("model", ""),
        "effort": (base or {}).get("effort", ""),
        "permissions": (base or {}).get("permissions", {}),
        "env": (base or {}).get("env", {}),
        "claude_md": (base or {}).get("claude_md", ""),
        "memory_md": (base or {}).get("memory_md", ""),
        "builtin": False,
    }
    data["profiles"].append(new_p)
    _save_roles(data)
    _materialize_profile(new_p)
    return JSONResponse({"ok": True, "id": pid, "profile": _profile_summary(new_p)})


@app.put("/api/profiles/{profile_id}")
async def api_update_profile(profile_id: str, body: UpdateProfileBody):
    data = _load_roles()
    p = _find_profile(profile_id, data)
    if not p:
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    if body.name is not None and body.name.strip():
        p["name"] = body.name.strip()
    if body.model is not None:
        p["model"] = body.model
    if body.effort is not None:
        p["effort"] = body.effort
    if body.claude_md is not None:
        p["claude_md"] = body.claude_md
    if body.memory_md is not None:
        p["memory_md"] = body.memory_md
    if body.permissions is not None:
        p["permissions"] = body.permissions
    if body.env is not None:
        p["env"] = body.env
    # Lock this profile against future preset refreshes -- user has customized it.
    p["edited"] = True
    _save_roles(data)
    _materialize_profile(p)
    return JSONResponse({"ok": True, "id": profile_id})


@app.delete("/api/profiles/{profile_id}")
async def api_delete_profile(profile_id: str):
    if profile_id == DEFAULT_PROFILE_ID:
        return JSONResponse({"error": "The default profile cannot be deleted."}, status_code=400)
    data = _load_roles()
    if not any(p["id"] == profile_id for p in data["profiles"]):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    data["profiles"] = [p for p in data["profiles"] if p["id"] != profile_id]
    # Reset any sessions on this profile to default
    for sname, pid in list(data["session_profiles"].items()):
        if pid == profile_id:
            data["session_profiles"].pop(sname, None)
    _save_roles(data)
    # Note: ~/.claude-<id>/ is intentionally NOT removed automatically. It may
    # contain history/credentials the user wants. They can `rm -rf` manually.
    return JSONResponse({"ok": True})


@app.get("/api/profiles/{profile_id}/skills")
async def api_list_profile_skills(profile_id: str):
    """List the skills currently installed under ~/.claude-<profile>/skills/.

    Each entry is a directory containing SKILL.md (the format Claude Code reads).
    Library symlinks are flagged with `from_library: true`. Any leftover flat .md
    files are surfaced under `legacy_files` so the UI can warn the user.
    """
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    d = _profile_skills_dir(profile_id)
    skills = []
    legacy_files = []
    for entry in sorted(d.iterdir()):
        info = _read_skill_dir(entry)
        if info:
            info["from_library"] = _is_library_link(d, entry.name)
            skills.append(info)
        elif entry.is_file() and entry.suffix == ".md":
            legacy_files.append(entry.name)
    return JSONResponse({"skills": skills, "legacy_files": legacy_files, "path": str(d)})


@app.get("/api/profiles/{profile_id}/skills/library")
async def api_list_profile_library_state(profile_id: str):
    """Return the full library with per-profile enabled state."""
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    skills_dir = _profile_skills_dir(profile_id)
    out = []
    for sk in _list_library_skills():
        out.append({
            "name": sk["name"],
            "dir_name": sk["dir_name"],
            "description": sk["description"],
            "enabled": _is_library_link(skills_dir, sk["dir_name"]),
        })
    return JSONResponse({"skills": out, "default_profile": profile_id == DEFAULT_PROFILE_ID})


@app.post("/api/profiles/{profile_id}/skills/library/{skill_name}")
async def api_enable_library_skill(profile_id: str, skill_name: str):
    """Enable a library skill for this profile by symlinking it into the profile's skills/ dir.

    Works on the default profile too — the library is now the source of truth.
    """
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    name = _sanitize_skill_dir_name(skill_name)
    if not name:
        return JSONResponse({"error": "Invalid skill name"}, status_code=400)
    src = SKILL_LIBRARY_DIR / name
    if not (src / "SKILL.md").is_file():
        return JSONResponse({"error": "Library skill not found"}, status_code=404)
    skills_dir = _profile_skills_dir(profile_id)
    target = skills_dir / name
    if target.is_symlink() or target.exists():
        if _is_library_link(skills_dir, name):
            return JSONResponse({"ok": True, "already_enabled": True})
        return JSONResponse({"error": f"'{name}' already exists in this profile and isn't a library link"}, status_code=409)
    try:
        target.symlink_to(src.resolve(), target_is_directory=True)
    except Exception:
        logger.exception("Failed to symlink library skill %s into %s", name, skills_dir)
        return JSONResponse({"error": "Failed to enable skill"}, status_code=500)
    return JSONResponse({"ok": True, "enabled": True})


@app.delete("/api/profiles/{profile_id}/skills/library/{skill_name}")
async def api_disable_library_skill(profile_id: str, skill_name: str):
    """Disable a library skill for this profile by removing its symlink.

    Works on the default profile too — the library is now the source of truth.
    """
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    name = _sanitize_skill_dir_name(skill_name)
    if not name:
        return JSONResponse({"error": "Invalid skill name"}, status_code=400)
    skills_dir = _profile_skills_dir(profile_id)
    target = skills_dir / name
    if not target.is_symlink() and not target.exists():
        return JSONResponse({"ok": True, "already_disabled": True})
    if not _is_library_link(skills_dir, name):
        return JSONResponse({"error": f"'{name}' is not a library link in this profile (refusing to delete custom content)"}, status_code=409)
    try:
        target.unlink()
    except Exception:
        logger.exception("Failed to unlink library skill %s from %s", name, skills_dir)
        return JSONResponse({"error": "Failed to disable skill"}, status_code=500)
    return JSONResponse({"ok": True, "disabled": True})


@app.post("/api/profiles/{profile_id}/skills/{skill_name}/promote")
async def api_promote_profile_skill(profile_id: str, skill_name: str):
    """Move a custom in-profile skill into the shared library so any profile can enable it.

    Copies <profile>/skills/<name>/ -> ~/.tmux-dashboard/skill-library/<name>/, then
    replaces the original with a symlink so the source profile keeps it active.
    """
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    name = _sanitize_skill_dir_name(skill_name)
    if not name:
        return JSONResponse({"error": "Invalid skill name"}, status_code=400)
    src = _profile_dir(profile_id) / "skills" / name
    if not (src / "SKILL.md").is_file():
        return JSONResponse({"error": "Skill not found in this profile"}, status_code=404)
    if src.is_symlink():
        return JSONResponse({"error": "Skill is already a library link"}, status_code=409)
    SKILL_LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    dst = SKILL_LIBRARY_DIR / name
    if dst.exists():
        return JSONResponse({"error": f"A library skill named '{name}' already exists"},
                            status_code=409)
    try:
        shutil.copytree(str(src), str(dst))
    except Exception:
        logger.exception("Failed to copy skill %s -> library", src)
        return JSONResponse({"error": "Failed to copy skill to library"}, status_code=500)
    # Replace original with a symlink to the library copy.
    try:
        backup = src.with_name(src.name + ".pre-promote")
        if backup.exists():
            shutil.rmtree(str(backup))
        shutil.move(str(src), str(backup))
        src.symlink_to(dst.resolve(), target_is_directory=True)
        shutil.rmtree(str(backup))
    except Exception:
        logger.exception("Failed to swap %s with symlink", src)
        return JSONResponse({"error": "Promoted to library but failed to relink source"},
                            status_code=500)
    return JSONResponse({"ok": True, "promoted": name})


@app.post("/api/profiles/{profile_id}/skills")
async def api_save_profile_skill(profile_id: str, body: SkillFileBody):
    """Legacy: write a flat .md file at the profile root skills/ dir.

    Kept so existing tooling doesn't break. Claude Code does NOT load these as
    Skills (they're not in the `<name>/SKILL.md` directory format). Prefer the
    library + per-profile toggle endpoints for new content.
    """
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    fname = _sanitize_skill_filename(body.name)
    if not fname:
        return JSONResponse({"error": "Invalid filename. Use alphanumeric, hyphens, underscores with .md extension."}, status_code=400)
    d = _profile_dir(profile_id) / "skills"
    d.mkdir(parents=True, exist_ok=True)
    fpath = d / fname
    if not str(fpath.resolve()).startswith(str(d.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    try:
        fpath.write_text(body.content)
        return JSONResponse({"ok": True, "name": fname})
    except Exception:
        logger.exception("Failed to save profile skill")
        return JSONResponse({"error": "Failed to save skill"}, status_code=500)


@app.delete("/api/profiles/{profile_id}/skills/{filename}")
async def api_delete_profile_skill(profile_id: str, filename: str):
    """Legacy: delete a flat .md file (or a directory-form skill) from a profile's skills/.

    Refuses to delete library symlinks via this endpoint — use the library
    enable/disable endpoints instead.
    """
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    d = _profile_dir(profile_id) / "skills"
    raw = os.path.basename((filename or "").strip())
    target = d / raw
    if not str(target.resolve()).startswith(str(d.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if _is_library_link(d, raw):
        return JSONResponse({"error": "Use the library disable endpoint to remove a library link"}, status_code=409)
    if not target.exists() and not target.is_symlink():
        return JSONResponse({"ok": True, "already_absent": True})
    try:
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(str(target))
    except Exception:
        logger.exception("Failed to delete profile skill")
        return JSONResponse({"error": "Failed to delete skill"}, status_code=500)
    return JSONResponse({"ok": True})


# Reserved names that the extras editor must NOT touch (these have their own
# dedicated editors). Comparison is case-insensitive against the sanitized name.
_EXTRAS_RESERVED_NAMES = {"claude.md", "memory.md", "settings.json"}


@app.get("/api/profiles/{profile_id}/extras")
async def api_list_profile_extras(profile_id: str):
    """List user-added .md files at the profile root (excludes CLAUDE.md/MEMORY.md)."""
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    d = _profile_dir(profile_id)
    d.mkdir(parents=True, exist_ok=True)
    files = []
    for p in sorted(d.iterdir()):
        if not (p.suffix == ".md" and p.is_file()):
            continue
        if p.name.lower() in _EXTRAS_RESERVED_NAMES:
            continue
        try:
            files.append({"name": p.name, "content": p.read_text(),
                          "size": p.stat().st_size})
        except Exception:
            logger.debug("Failed to read profile extra %s", p, exc_info=True)
    return JSONResponse({"files": files, "path": str(d)})


@app.post("/api/profiles/{profile_id}/extras")
async def api_save_profile_extra(profile_id: str, body: SkillFileBody):
    """Create or update a sidecar .md file at the profile root."""
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    fname = _sanitize_skill_filename(body.name)
    if not fname:
        return JSONResponse({"error": "Invalid filename. Use alphanumeric, hyphens, underscores with .md extension."}, status_code=400)
    if fname.lower() in _EXTRAS_RESERVED_NAMES:
        return JSONResponse({"error": f"'{fname}' is reserved. Use the dedicated editor for it."}, status_code=400)
    d = _profile_dir(profile_id)
    d.mkdir(parents=True, exist_ok=True)
    fpath = d / fname
    if not str(fpath.resolve()).startswith(str(d.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    try:
        fpath.write_text(body.content)
        return JSONResponse({"ok": True, "name": fname})
    except Exception:
        logger.exception("Failed to save profile extra")
        return JSONResponse({"error": "Failed to save extra file"}, status_code=500)


@app.delete("/api/profiles/{profile_id}/extras/{filename}")
async def api_delete_profile_extra(profile_id: str, filename: str):
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    fname = _sanitize_skill_filename(filename)
    if not fname:
        return JSONResponse({"error": "Invalid filename"}, status_code=400)
    if fname.lower() in _EXTRAS_RESERVED_NAMES:
        return JSONResponse({"error": "Reserved filename"}, status_code=400)
    d = _profile_dir(profile_id)
    fpath = d / fname
    if not str(fpath.resolve()).startswith(str(d.resolve())):
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if fpath.exists():
        try:
            fpath.unlink()
        except Exception:
            logger.exception("Failed to delete profile extra")
            return JSONResponse({"error": "Failed to delete extra file"}, status_code=500)
    return JSONResponse({"ok": True})


# --- Profile file browser (full ~/.claude-<id>/ contents) ---
# Lets the Profiles editor expose every file Claude Code actually loads from the
# config dir: settings.json (full), .claude.json (MCP/projects), agents/, commands/,
# plus a credentials.json status read (we never expose the token contents).

# Categories the UI surfaces. The first element of each tuple is the relative path
# under the profile dir; the second is the kind ("json", "md", "binary").
_PROFILE_FILE_CATEGORIES = {
    "settings": [("settings.json", "json")],
    "mcp":      [(".claude.json", "json")],
    "agents":   "agents",       # directory: list *.md
    "commands": "commands",     # directory: list *.md or *.json
    "plugins":  "plugins",      # directory: list top-level entries (read-only)
}

# Files we refuse to surface for editing under the generic file API.
_PROFILE_FILE_BLOCKLIST = {".credentials.json"}


def _safe_profile_path(profile_id: str, rel: str) -> Optional[Path]:
    """Return an absolute Path inside the profile dir, or None if rel escapes it."""
    rel = (rel or "").lstrip("/").replace("\\", "/")
    if not rel or ".." in rel.split("/"):
        return None
    base = _profile_dir(profile_id).resolve()
    target = (base / rel).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    name = target.name
    if name in _PROFILE_FILE_BLOCKLIST:
        return None
    return target


def _read_dir_entries(d: Path, exts: tuple = (".md", ".json", ".toml")) -> list:
    out = []
    if not d.exists() or not d.is_dir():
        return out
    for entry in sorted(d.iterdir()):
        rel_name = entry.name
        if entry.is_dir():
            out.append({"name": rel_name, "kind": "dir",
                        "size": 0, "modified": entry.stat().st_mtime})
        elif entry.is_file():
            if exts and entry.suffix.lower() not in exts:
                continue
            try:
                out.append({
                    "name": rel_name,
                    "kind": "file",
                    "size": entry.stat().st_size,
                    "modified": entry.stat().st_mtime,
                })
            except Exception:
                logger.debug("Failed to stat %s", entry, exc_info=True)
    return out


@app.get("/api/profiles/{profile_id}/files")
async def api_list_profile_files(profile_id: str):
    """Return a structured inventory of every editable file inside the profile dir."""
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    base = _profile_dir(profile_id)
    base.mkdir(parents=True, exist_ok=True)

    inv: dict = {"dir": str(base), "categories": {}}

    # Top-level singletons
    for cat, items in (("settings", _PROFILE_FILE_CATEGORIES["settings"]),
                       ("mcp", _PROFILE_FILE_CATEGORIES["mcp"])):
        files = []
        for rel, kind in items:
            p = base / rel
            files.append({
                "path": rel, "kind": kind,
                "exists": p.exists(),
                "size": p.stat().st_size if p.exists() else 0,
            })
        inv["categories"][cat] = {"type": "files", "files": files}

    # Directory categories
    for cat in ("agents", "commands"):
        sub = _PROFILE_FILE_CATEGORIES[cat]
        d = base / sub
        d.mkdir(parents=True, exist_ok=True)
        inv["categories"][cat] = {
            "type": "dir", "path": sub,
            "files": _read_dir_entries(d, exts=(".md", ".json", ".toml")),
        }

    # Plugins: list top-level dirs only, read-only
    plugins_dir = base / "plugins"
    plugin_entries: list = []
    if plugins_dir.exists() and plugins_dir.is_dir():
        for entry in sorted(plugins_dir.iterdir()):
            try:
                plugin_entries.append({
                    "name": entry.name,
                    "kind": "dir" if entry.is_dir() else "file",
                })
            except Exception:
                logger.debug("Failed to inspect plugin entry %s", entry, exc_info=True)
    inv["categories"]["plugins"] = {"type": "dir-readonly", "path": "plugins",
                                    "files": plugin_entries}

    return JSONResponse(inv)


class ProfileFileBody(BaseModel):
    path: str
    content: str


@app.get("/api/profiles/{profile_id}/file")
async def api_get_profile_file(profile_id: str, path: str):
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    target = _safe_profile_path(profile_id, path)
    if target is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    content = ""
    exists = target.exists()
    if exists:
        try:
            content = target.read_text()
        except Exception:
            logger.debug("Failed to read %s", target, exc_info=True)
            return JSONResponse({"error": "Could not read file"}, status_code=500)
    return JSONResponse({"path": path, "content": content, "exists": exists,
                         "size": target.stat().st_size if exists else 0})


@app.put("/api/profiles/{profile_id}/file")
async def api_save_profile_file(profile_id: str, body: ProfileFileBody):
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    target = _safe_profile_path(profile_id, body.path)
    if target is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    # JSON files must parse before write
    if target.suffix.lower() == ".json" and body.content.strip():
        try:
            json.loads(body.content)
        except Exception as e:
            return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content)
        return JSONResponse({"ok": True, "path": body.path,
                             "size": target.stat().st_size})
    except Exception:
        logger.exception("Failed to write profile file %s", target)
        return JSONResponse({"error": "Failed to save"}, status_code=500)


@app.delete("/api/profiles/{profile_id}/file")
async def api_delete_profile_file(profile_id: str, path: str):
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    target = _safe_profile_path(profile_id, path)
    if target is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    # Refuse to delete the singletons settings.json / .claude.json — they're
    # managed by Claude Code itself. Only files inside agents/ or commands/ are
    # safely deletable here.
    rel_parts = path.split("/")
    if rel_parts[0] not in ("agents", "commands"):
        return JSONResponse({"error": "Only files under agents/ or commands/ can be deleted here"},
                            status_code=400)
    if target.exists():
        try:
            if target.is_file() or target.is_symlink():
                target.unlink()
            else:
                shutil.rmtree(str(target))
        except Exception:
            logger.exception("Failed to delete profile file %s", target)
            return JSONResponse({"error": "Failed to delete"}, status_code=500)
    return JSONResponse({"ok": True})


@app.get("/api/profiles/{profile_id}/credentials")
async def api_profile_credentials_status(profile_id: str):
    """Read login status from .credentials.json -- never returns the token."""
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    creds_path = _profile_dir(profile_id) / ".credentials.json"
    out = {"loggedIn": False, "path": str(creds_path), "exists": creds_path.exists()}
    if not creds_path.exists():
        return JSONResponse(out)
    try:
        creds = json.loads(creds_path.read_text())
        oauth = creds.get("claudeAiOauth") or {}
        expires_at = int(oauth.get("expiresAt") or 0)
        now_ms = int(time.time() * 1000)
        out["loggedIn"] = bool(oauth and expires_at > now_ms)
        out["subscriptionType"] = oauth.get("subscriptionType", "")
        out["expiresAt"] = expires_at
        out["expiresInDays"] = max(0, (expires_at - now_ms) // 86400000) if expires_at else 0
    except Exception:
        logger.debug("Failed to parse %s", creds_path, exc_info=True)
        out["error"] = "Credentials file is unreadable"
    return JSONResponse(out)


@app.delete("/api/profiles/{profile_id}/credentials")
async def api_profile_logout(profile_id: str):
    """Remove .credentials.json for this profile (forces /login on next run)."""
    data = _load_roles()
    if not _find_profile(profile_id, data):
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    creds_path = _profile_dir(profile_id) / ".credentials.json"
    if creds_path.exists():
        try:
            creds_path.unlink()
        except Exception:
            logger.exception("Failed to delete %s", creds_path)
            return JSONResponse({"error": "Failed to delete credentials"}, status_code=500)
    return JSONResponse({"ok": True})


# --- Project-scope (per-session, cwd-bound) file management ---
# Claude Code loads these on top of the active profile, regardless of which profile
# the session uses: <cwd>/CLAUDE.md, <cwd>/.claude/settings.local.json, <cwd>/.mcp.json.
# Surface them in the session's "More" dropdown so the user can edit per-project
# rules without leaving the dashboard.

_PROJECT_FILES = [
    ("CLAUDE.md", "md",
     "Project rules loaded on top of the profile's CLAUDE.md."),
    (".claude/settings.json", "json",
     "Project settings (model, env, hooks) loaded on top of profile settings."),
    (".claude/settings.local.json", "json",
     "Project-local settings (not committed). Loaded last; wins over everything."),
    (".mcp.json", "json",
     "Project-scope MCP servers (added to profile MCP servers)."),
]


def _safe_project_path(cwd: str, rel: str) -> Optional[Path]:
    """Confine writes to known per-project files under cwd."""
    if not cwd:
        return None
    rel_clean = (rel or "").lstrip("/").replace("\\", "/")
    allowed = {p for p, _, _ in _PROJECT_FILES}
    if rel_clean not in allowed:
        return None
    base = Path(cwd).resolve()
    if not base.exists() or not base.is_dir():
        return None
    target = (base / rel_clean).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return target


@app.get("/api/sessions/{session_name}/project-files")
async def api_list_session_project_files(session_name: str):
    """Inventory of project-scope files for this session's cwd."""
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    cwd = get_session_cwd(session_name) or ""
    files = []
    if cwd:
        base = Path(cwd)
        for rel, kind, desc in _PROJECT_FILES:
            p = base / rel
            files.append({
                "path": rel, "kind": kind, "description": desc,
                "exists": p.exists(),
                "size": p.stat().st_size if p.exists() else 0,
            })
    return JSONResponse({"cwd": cwd, "files": files})


@app.get("/api/sessions/{session_name}/project-file")
async def api_get_session_project_file(session_name: str, path: str):
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    cwd = get_session_cwd(session_name) or ""
    target = _safe_project_path(cwd, path)
    if target is None:
        return JSONResponse({"error": "Invalid path (not an allowed project file or session has no cwd)"},
                            status_code=400)
    content = ""
    exists = target.exists()
    if exists:
        try:
            content = target.read_text()
        except Exception:
            logger.debug("Failed to read %s", target, exc_info=True)
            return JSONResponse({"error": "Could not read file"}, status_code=500)
    return JSONResponse({"path": path, "abs_path": str(target),
                         "content": content, "exists": exists,
                         "cwd": cwd,
                         "size": target.stat().st_size if exists else 0})


class ProjectFileBody(BaseModel):
    path: str
    content: str


@app.put("/api/sessions/{session_name}/project-file")
async def api_save_session_project_file(session_name: str, body: ProjectFileBody):
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    cwd = get_session_cwd(session_name) or ""
    target = _safe_project_path(cwd, body.path)
    if target is None:
        return JSONResponse({"error": "Invalid path"}, status_code=400)
    if target.suffix.lower() == ".json" and body.content.strip():
        try:
            json.loads(body.content)
        except Exception as e:
            return JSONResponse({"error": f"Invalid JSON: {e}"}, status_code=400)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(body.content)
        return JSONResponse({"ok": True, "path": body.path,
                             "abs_path": str(target),
                             "size": target.stat().st_size})
    except Exception:
        logger.exception("Failed to write project file %s", target)
        return JSONResponse({"error": "Failed to save"}, status_code=500)


def _send_profile_export(session_name: str, profile_id: str):
    """Send `export CLAUDE_CONFIG_DIR=...` (or unset) to the tmux pane shell."""
    if profile_id == DEFAULT_PROFILE_ID:
        cmd = "unset CLAUDE_CONFIG_DIR"
    else:
        cmd = f"export CLAUDE_CONFIG_DIR={shlex.quote(str(_profile_dir(profile_id)))}"
    try:
        subprocess.run(["tmux", "send-keys", "-t", session_name, "-l", cmd],
                       capture_output=True, text=True, timeout=5)
        subprocess.run(["tmux", "send-keys", "-t", session_name, "Enter"],
                       capture_output=True, text=True, timeout=5)
        return True
    except Exception:
        logger.debug("send-keys failed for profile export", exc_info=True)
        return False


@app.post("/api/sessions/{session_name}/profile")
async def api_set_session_profile(session_name: str, body: SetSessionProfileBody):
    _, sess = _find_session(session_name)
    if not sess:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    data = _load_roles()
    profile = _find_profile(body.profile_id, data)
    if not profile:
        return JSONResponse({"error": "Profile not found"}, status_code=404)
    pid = body.profile_id
    if pid == DEFAULT_PROFILE_ID:
        data["session_profiles"].pop(session_name, None)
    else:
        data["session_profiles"][session_name] = pid
    _save_roles(data)

    claude_running = await _async_is_claude_running(session_name)
    exported = False
    restarted = False
    if not claude_running:
        # Shell is exposed -- export immediately so the next `claude` invocation picks it up
        exported = _send_profile_export(session_name, pid)
    elif body.restart:
        # Send /exit to Claude, wait for shell, export, relaunch
        try:
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", session_name, "/exit", "Enter"],
                capture_output=True, text=True, timeout=5)
            for _ in range(15):
                await asyncio.sleep(1)
                if not await _async_is_claude_running(session_name):
                    break
            exported = _send_profile_export(session_name, pid)
            await asyncio.sleep(0.3)
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", session_name, "-l",
                 "claude --dangerously-skip-permissions"],
                capture_output=True, text=True, timeout=5)
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", session_name, "Enter"],
                capture_output=True, text=True, timeout=5)
            restarted = True
        except Exception:
            logger.exception("Failed to restart Claude with new profile")

    return JSONResponse({
        "ok": True, "profile_id": pid,
        "claude_was_running": claude_running,
        "exported": exported, "restarted": restarted,
    })


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


# --- Claude account identity + per-session stale-login detection ---
#
# A running `claude` process pins whatever account was in `.credentials.json`
# at startup; the file changing later does NOT switch a live session. So a
# session started before a login switch keeps showing the OLD account's 5-hour
# usage bar inside its TUI. We detect that by comparing each session's claude
# process start time against when the active account last *changed* (not merely
# refreshed) for that session's CLAUDE_CONFIG_DIR.

def _friendly_plan(sub: str, tier: str) -> str:
    t = (tier or "").lower()
    if "max_20x" in t:
        return "Max 20x"
    if "max_5x" in t:
        return "Max 5x"
    if "pro" in t:
        return "Pro"
    if "team" in t:
        return "Team"
    s = (sub or "").lower()
    if s == "max":
        return "Max"
    if s == "pro":
        return "Pro"
    if s == "free":
        return "Free"
    return sub.capitalize() if sub else "—"


def _clk_tck() -> int:
    try:
        return os.sysconf("SC_CLK_TCK")
    except Exception:
        return 100


_btime_cache: list = [0.0]


def _system_btime() -> float:
    if _btime_cache[0]:
        return _btime_cache[0]
    try:
        with open("/proc/stat") as f:
            for line in f:
                if line.startswith("btime "):
                    _btime_cache[0] = float(line.split()[1])
                    break
    except Exception:
        pass
    return _btime_cache[0]


def _proc_start_epoch(pid) -> float:
    """Wall-clock epoch when process <pid> started, from /proc/<pid>/stat."""
    try:
        with open(f"/proc/{pid}/stat") as f:
            data = f.read()
        # comm (field 2) is wrapped in parens and may contain spaces/parens, so
        # split after the last ')'. starttime is field 22 (clock ticks since boot).
        fields = data[data.rfind(")") + 2:].split()
        if len(fields) <= 19:
            return 0.0
        btime = _system_btime()
        if btime <= 0:
            return 0.0
        return btime + float(fields[19]) / _clk_tck()
    except Exception:
        return 0.0


def _build_proc_tree() -> tuple:
    """One `ps` call -> (children_by_ppid: dict, comm_by_pid: dict)."""
    children: dict = {}
    comm: dict = {}
    try:
        res = subprocess.run(
            ["ps", "-eo", "pid=,ppid=,comm="],
            capture_output=True, text=True, timeout=5,
        )
        for line in (res.stdout or "").splitlines():
            parts = line.split(None, 2)
            if len(parts) < 2:
                continue
            pid, ppid = parts[0], parts[1]
            children.setdefault(ppid, []).append(pid)
            comm[pid] = parts[2] if len(parts) > 2 else ""
    except Exception:
        pass
    return children, comm


def _all_pane_pids_by_session() -> dict:
    """One `tmux list-panes -a` call -> {session_name: [pane_pid, ...]}."""
    m: dict = {}
    try:
        res = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{session_name} #{pane_pid}"],
            capture_output=True, text=True, timeout=5,
        )
        for line in (res.stdout or "").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                m.setdefault(parts[0], []).append(parts[1])
    except Exception:
        pass
    return m


def _claude_pids_under(roots, children: dict, comm: dict) -> list:
    """BFS the process tree from pane roots, returning descendant `claude` pids."""
    out = []
    stack = list(roots)
    seen = set(roots)
    steps = 0
    while stack and steps < 10000:
        steps += 1
        cur = stack.pop()
        for ch in children.get(cur, []):
            if ch in seen:
                continue
            seen.add(ch)
            stack.append(ch)
            if (comm.get(ch, "") or "").lower() == "claude":
                out.append(ch)
    return out


_account_ident_cache: dict = {}


def _account_identity(config_dir) -> dict:
    """Current Claude account for a CLAUDE_CONFIG_DIR (cached 30s)."""
    key = str(config_dir)
    now = time.time()
    cached = _account_ident_cache.get(key)
    if cached and now - cached[0] < 30:
        return cached[1]
    config_dir = Path(config_dir)
    sub = tier = email = org = ""
    cred_mtime = 0.0
    creds = config_dir / ".credentials.json"
    try:
        cred_mtime = creds.stat().st_mtime
        oauth = json.loads(creds.read_text()).get("claudeAiOauth", {})
        sub = oauth.get("subscriptionType", "") or ""
        tier = oauth.get("rateLimitTier", "") or ""
    except Exception:
        pass
    # The big config (with oauthAccount.emailAddress) lives at <dir>/.claude.json,
    # except the default ~/.claude whose config is the home-level ~/.claude.json.
    cj = config_dir / ".claude.json"
    profile_fetched = 0.0
    try:
        if not cj.exists() and config_dir == Path.home() / ".claude":
            cj = Path.home() / ".claude.json"
        oa = json.loads(cj.read_text()).get("oauthAccount", {})
        email = oa.get("emailAddress", "") or ""
        org = oa.get("organizationUuid", "") or ""
        # profileFetchedAt (ms) marks when this account was last logged in/switched.
        # Unlike the credentials mtime, it does NOT move on a routine token refresh,
        # so it's the correct anchor for "when did the active account change".
        pf = oa.get("profileFetchedAt") or 0
        profile_fetched = float(pf) / 1000.0 if pf else 0.0
    except Exception:
        pass
    ident = {
        "email": email, "sub": sub, "tier": tier,
        "plan": _friendly_plan(sub, tier),
        "fp": org or (sub + "/" + tier),  # account fingerprint (changes per account)
        "cred_mtime": cred_mtime,
        "profile_fetched": profile_fetched,
    }
    _account_ident_cache[key] = (now, ident)
    return ident


_LOGIN_STATE_FILE = Path.home() / ".tmux-dashboard" / "login_state.json"


def _load_login_state() -> dict:
    try:
        return json.loads(_LOGIN_STATE_FILE.read_text())
    except Exception:
        return {}


def _save_login_state(state: dict):
    try:
        _LOGIN_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        _LOGIN_STATE_FILE.write_text(json.dumps(state))
    except Exception:
        logger.debug("Could not persist login_state.json", exc_info=True)


def _login_switch_time(config_dir, ident: dict) -> float:
    """Epoch of the last *observed* account switch for this config dir, or 0.

    Returns 0 when the dashboard has only ever seen one account fingerprint for
    this dir — i.e. no switch has happened — so long-running sessions are never
    falsely flagged. The value advances ONLY when the active fingerprint is seen
    to change between two polls.

    Why not anchor on profileFetchedAt / the credentials mtime: BOTH of those
    move on a routine OAuth token refresh (claude re-fetches the profile and
    rewrites .credentials.json every few hours) even though the account is
    unchanged. Anchoring on them made the first poll after a refresh look like a
    brand-new login and flag every older — but identical-account — session as
    "on old login". The fingerprint (org UUID / sub+tier) does NOT move on a
    refresh, so a fingerprint *change* is the only reliable switch signal.

    Persisted so a genuine switch (including one that happened while the
    dashboard was down) survives restarts.
    """
    key = str(config_dir)
    state = _load_login_state()
    cur = state.get(key)
    now = time.time()
    fp = ident.get("fp", "")
    # Best estimate of when a switch happened = the new account's profile fetch
    # time (≈ when it was logged in/switched to); fall back to creds mtime/now.
    anchor = ident.get("profile_fetched") or ident.get("cred_mtime") or now
    if not cur:
        # First sight of this dir: record the account but do NOT assume a switch
        # just happened — the account may have been active for a long time.
        state[key] = {"fp": fp, "since": anchor, "switched_at": 0}
        _save_login_state(state)
        return 0.0
    if cur.get("fp") != fp:
        # A genuine account change observed between two polls -> record it.
        state[key] = {"fp": fp, "since": anchor, "switched_at": anchor}
        _save_login_state(state)
        return anchor
    # Same account as last poll: keep the recorded switch time (0 if never).
    return float(cur.get("switched_at", 0) or 0)


@app.get("/api/login-health")
async def api_login_health():
    """Per-session Claude login health: flags sessions whose live `claude`
    process started before the current account became active (i.e. its in-TUI
    5-hour usage bar reflects a previous account)."""
    def _compute():
        sessions = get_tmux_sessions()
        children, comm = _build_proc_tree()
        panes = _all_pane_pids_by_session()
        ident_by_dir: dict = {}
        out = []
        for s in sessions:
            name = s["name"]
            base = _session_config_base(name)
            key = str(base)
            ident = ident_by_dir.get(key)
            if ident is None:
                ident = _account_identity(base)
                ident_by_dir[key] = ident
            switched_at = _login_switch_time(base, ident)
            cpids = _claude_pids_under(panes.get(name, []), children, comm)
            starts = [e for e in (_proc_start_epoch(p) for p in cpids) if e > 0]
            claude_started = min(starts) if starts else 0
            # Flag stale only when an account switch was actually observed AND
            # this session's claude predates it (5s slack avoids flagging a
            # session launched in the same moment as the switch).
            stale = bool(claude_started and switched_at and claude_started < switched_at - 5)
            out.append({
                "name": name,
                "stale": stale,
                "claude_running": bool(cpids),
                "claude_started": claude_started,
                "plan": ident["plan"],
                "account": ident["email"] or ident["sub"],
            })
        active = _account_identity(Path.home() / ".claude")
        return {
            "account": {"email": active["email"], "plan": active["plan"],
                        "sub": active["sub"], "tier": active["tier"]},
            "stale_count": sum(1 for x in out if x["stale"]),
            "sessions": out,
        }
    try:
        data = await asyncio.to_thread(_compute)
    except Exception:
        logger.exception("login-health compute failed")
        return JSONResponse({"account": {}, "stale_count": 0, "sessions": []})
    return JSONResponse(data)


@app.post("/api/sessions/{session_name}/relogin")
async def api_session_relogin(session_name: str, request: Request):
    """Gracefully exit Claude and relaunch it on the CURRENT login, preserving
    the conversation via --continue. Fixes a session stuck on a previous account."""
    user = _current_user(request)
    if not _user_can_access_session(user, session_name):
        return JSONResponse({"error": "Session not found"}, status_code=404)
    running = await _async_is_claude_running(session_name)
    if running:
        await asyncio.to_thread(subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "/exit", "Enter"],
            capture_output=True, text=True, timeout=5)
        for _ in range(20):
            await asyncio.sleep(1)
            if not await _async_is_claude_running(session_name):
                break
    # Re-export the session's profile CLAUDE_CONFIG_DIR (non-default) before the
    # relaunch, in case the shell was respawned and lost the env var.
    try:
        pid = _get_session_profile_id(session_name)
        if pid != DEFAULT_PROFILE_ID:
            await asyncio.to_thread(_send_profile_export, session_name, pid)
            await asyncio.sleep(0.3)
    except Exception:
        logger.debug("relogin: profile re-export failed", exc_info=True)
    await asyncio.to_thread(subprocess.run,
        ["tmux", "send-keys", "-t", session_name, "-l",
         "NODE_OPTIONS=--max-old-space-size=8192 claude --dangerously-skip-permissions --continue"],
        capture_output=True, text=True, timeout=5)
    await asyncio.to_thread(subprocess.run,
        ["tmux", "send-keys", "-t", session_name, "Enter"],
        capture_output=True, text=True, timeout=5)
    # Invalidate caches so the next poll reflects the relaunch immediately.
    _account_ident_cache.clear()
    return JSONResponse({"ok": True, "relaunched": True, "claude_was_running": running})


@app.post("/api/transcribe")
async def api_transcribe(audio: UploadFile = File(...)):
    """Transcribe a recorded voice clip to text (for the composer mic button)."""
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key:
        return JSONResponse({"error": "Transcription is not configured."}, status_code=503)
    try:
        data = await audio.read()
    except Exception:
        return JSONResponse({"error": "Could not read audio."}, status_code=400)
    if not data:
        return JSONResponse({"error": "Empty audio."}, status_code=400)
    suffix = os.path.splitext(audio.filename or "")[1] or ".webm"

    def _do():
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=True) as tf:
            tf.write(data)
            tf.flush()
            client = openai.OpenAI(api_key=key)
            with open(tf.name, "rb") as fh:
                tr = client.audio.transcriptions.create(model="whisper-1", file=fh)
            return (getattr(tr, "text", "") or "").strip()

    try:
        text = await asyncio.to_thread(_do)
    except Exception as e:
        logger.warning("transcribe failed: %s", e)
        return JSONResponse({"error": "Transcription failed."}, status_code=502)
    return JSONResponse({"text": text})


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
            ident = _account_identity(Path.home() / ".claude")
            result_data["rateLimitTier"] = oauth.get("rateLimitTier", "") or ident.get("tier", "")
            result_data["plan"] = ident.get("plan") or _friendly_plan(
                oauth.get("subscriptionType", ""), oauth.get("rateLimitTier", ""))
            result_data["email"] = ident.get("email") or "Claude Code"
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
_anthropic_limits_cache: dict = {"ts": 0, "data": None, "fp": ""}


def _usage_reset_passed(block: object, now_dt: datetime) -> bool:
    """True if this usage window's ``resets_at`` is already in the past.

    Once a window resets, its cached ``utilization`` is stale — the window has
    rolled over to a fresh one (utilization ~0). Serving the old value shows a
    maxed (e.g. 100%) bar for a window that no longer applies.
    """
    if not isinstance(block, dict):
        return False
    ra = block.get("resets_at")
    if not ra:
        return False
    try:
        rt = datetime.fromisoformat(str(ra).replace("Z", "+00:00"))
        if rt.tzinfo is None:
            rt = rt.replace(tzinfo=timezone.utc)
        return rt <= now_dt
    except Exception:
        return False


def _usage_windows_expired(data: object, now_dt: datetime) -> bool:
    """True if any known usage window in the payload has already reset."""
    if not isinstance(data, dict):
        return False
    return any(_usage_reset_passed(data.get(k), now_dt)
               for k in ("five_hour", "seven_day"))


def _sanitize_expired_windows(data: object, now_dt: datetime) -> object:
    """Return a copy with utilization zeroed for any window that has reset.

    Used only on the stale-cache fallback (upstream fetch failed) so a rolled-
    over window never keeps showing its pre-reset peak (e.g. 100%). The real
    value is unknown but a just-reset window is ~0, never maxed.
    """
    if not isinstance(data, dict):
        return data
    out = dict(data)
    for key in ("five_hour", "seven_day"):
        blk = out.get(key)
        if isinstance(blk, dict) and _usage_reset_passed(blk, now_dt):
            nb = dict(blk)
            nb["utilization"] = 0
            out[key] = nb
    return out


@app.get("/api/usage/limits")
async def api_anthropic_usage_limits():
    """Fetch live 5h + 7-day rate-limit utilization from Anthropic OAuth usage API.

    Cached for 1 hour per the user-facing requirement (poll hourly while
    sessions are active). On upstream failure, returns the last good payload.
    The cache is keyed on the current token, so a login switch busts it at once
    instead of showing a previous account's usage for up to an hour.
    """
    now = time.time()
    now_dt = datetime.now(timezone.utc)
    creds_file = Path.home() / ".claude" / ".credentials.json"
    token = ""
    try:
        creds = json.loads(creds_file.read_text())
        token = creds.get("claudeAiOauth", {}).get("accessToken", "") or ""
    except Exception:
        pass
    if not token:
        return JSONResponse({"error": "not_authenticated"}, status_code=401)
    fp = hashlib.sha1(token.encode()).hexdigest()[:16]
    # Serve cache only if fresh (<1h), same token, AND none of its windows have
    # reset since — otherwise a rolled-over window (e.g. the 7-day limit) keeps
    # showing its pre-reset peak (100%) for up to an hour after it dropped to ~0.
    if (now - _anthropic_limits_cache["ts"] < 3600
            and _anthropic_limits_cache["data"]
            and _anthropic_limits_cache.get("fp") == fp
            and not _usage_windows_expired(_anthropic_limits_cache["data"], now_dt)):
        return JSONResponse(_anthropic_limits_cache["data"])

    def _fetch():
        import urllib.request
        req = urllib.request.Request(
            "https://api.anthropic.com/api/oauth/usage",
            headers={
                "Authorization": f"Bearer {token}",
                "User-Agent": "tmux-dashboard/1.0",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        data = await asyncio.to_thread(_fetch)
    except Exception as e:
        logger.warning("Anthropic OAuth usage fetch failed: %s", e)
        if _anthropic_limits_cache["data"]:
            # Serve last good payload, but zero out any window that has since
            # reset so we never show a stale maxed bar on upstream failure.
            return JSONResponse(_sanitize_expired_windows(_anthropic_limits_cache["data"], now_dt))
        return JSONResponse({"error": "fetch_failed"}, status_code=502)

    payload = {"fetched_at": now, **data}
    _anthropic_limits_cache["ts"] = now
    _anthropic_limits_cache["data"] = payload
    _anthropic_limits_cache["fp"] = fp
    return JSONResponse(payload)


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
    # Use the session's OWN config dir as the transcript root. Team members run
    # in an isolated ~/.claude-user-<id>/ where their Claude writes transcripts;
    # the admin's ~/.claude would surface a different user's transcripts (or none),
    # which is why member Chat tabs showed no summary. Admin sessions resolve to
    # ~/.claude, so this is a no-op on non-team hosts.
    projects_base = _session_config_base(session_name) / "projects"
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

    SAFETY: We require the ❯ cursor to sit DIRECTLY on a numbered option
    line (e.g. "❯ 1. Yes"). If ❯ is followed by free text (e.g.
    "❯ test it in the browser") that is the user input prompt, not a
    selection — Enter would submit that text instead of selecting an option,
    which is the "phantom message" bug we must avoid.
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
    selector_followed_by_text = False
    for line in last_25:
        stripped = line.strip()
        if re.match(r"^[❯\u276f\s]*\d+\.\s", stripped):
            numbered += 1
        if re.match(r"^❯\s*\d+\.", stripped) or re.match(r"^\u276f\s*\d+\.", stripped):
            has_selector_on_option = True
        # ❯ followed by non-numeric text = user input prompt with text waiting.
        # Enter on this would submit that text — never auto-fire here.
        if re.match(r"^[❯\u276f]\s+\S", stripped) and not re.match(r"^[❯\u276f]\s*\d+\.", stripped):
            selector_followed_by_text = True

    # Bail if cursor is in the user input box with text waiting.
    if selector_followed_by_text and not has_selector_on_option:
        return None

    # Strong signal: specific Claude Code prompt keywords
    strong_keywords = [
        "bypass permissions",
        "manually approve edits",
        "shift+tab to approve",
        "Would you like to proceed",
        "approve with this feedback",
    ]
    has_strong = any(kw in text for kw in strong_keywords)

    # Plan approval / permission prompt — keyword AND cursor on a numbered
    # option, so Enter selects an option and never submits free text.
    if has_strong and has_selector_on_option and numbered >= 2:
        return "plan_approval"

    # Generic Claude Code selection prompt: ❯ on a numbered option + 2+ options
    if has_selector_on_option and numbered >= 2:
        return "selection_prompt"

    return None


_MENU_PICK_SYSTEM_PROMPT = (
    "A Claude Code agent is showing a numbered selection menu and THE USER IS AWAY and will "
    "not answer. Pick the option number that best lets the agent CONTINUE and COMPLETE the work "
    "on its own.\n"
    "- Strongly prefer options that proceed / do the work / say Yes / auto-accept / "
    "'yes, and don't ask again' / accept-edits / run it.\n"
    "- AVOID options that pause, stop, cancel, exit, quit, reject, defer, or hand control back "
    "to the user (e.g. 'no', 'let me decide', 'I'll do it myself', 'ask me later').\n"
    "- If several options proceed, pick the one that makes the MOST progress with the fewest "
    "future interruptions.\n"
    "- If genuinely unsure, pick 1.\n"
    "Reply with ONLY the option number, nothing else."
)


def _parse_menu_options(visible_text: str):
    """Parse a Claude Code numbered menu. Returns (options, selected_idx) where
    options = [(number, label), ...] in visual order and selected_idx is the
    0-based position of the ❯-highlighted option (0 if none found)."""
    options = []
    selected = None
    for line in visible_text.split("\n"):
        s = line.strip()
        m = re.match(r"^(❯|❯)?\s*(\d+)\.\s+(\S.*)$", s)
        if not m:
            continue
        if m.group(1):
            selected = len(options)
        options.append((int(m.group(2)), m.group(3).strip()))
    return options, (selected if selected is not None else 0)


async def _llm_pick_menu_option(name: str, visible: str, options: list):
    """Ask the LLM which menu option best continues the work. Returns the chosen
    option NUMBER, or None to fall back to the default (Enter)."""
    valid = {n for n, _ in options}
    if not valid:
        return None
    try:
        raw = await llm_call(
            system_prompt=_MENU_PICK_SYSTEM_PROMPT,
            user_content=(f"Session '{name}' is showing this menu:\n\n{visible[-2500:]}\n\n"
                          f"Valid option numbers: {sorted(valid)}. Reply with ONE number."),
            max_tokens=4,
        )
    except Exception:
        return None
    m = re.search(r"\d+", raw or "")
    if not m:
        return None
    n = int(m.group())
    return n if n in valid else None


async def _select_menu_option(name: str, options: list, selected_idx: int, target_num: int) -> str:
    """Navigate to the target option (arrow keys) and Enter. Returns a label for logging."""
    target_idx = next((i for i, (n, _) in enumerate(options) if n == target_num), None)
    if target_idx is None:
        target_idx = selected_idx
    delta = target_idx - selected_idx
    if 0 < abs(delta) < len(options):
        key = "Down" if delta > 0 else "Up"
        for _ in range(abs(delta)):
            await asyncio.to_thread(subprocess.run,
                ["tmux", "send-keys", "-t", name, key],
                capture_output=True, text=True, timeout=3)
            await asyncio.sleep(0.06)
    await asyncio.to_thread(subprocess.run,
        ["tmux", "send-keys", "-t", name, "Enter"],
        capture_output=True, text=True, timeout=3)
    label = next((l for n, l in options if n == target_num), "")
    return f"option {target_num} ({label[:40]})" if label else f"option {target_num}"


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
                # Auto-push "off" means never type anything into this terminal.
                if _get_autopush_mode(name) == "off":
                    continue
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
                    # Safety backstop: don't auto-approve a clearly destructive /
                    # irreversible action — leave it for a human. (Back off ~60s to
                    # avoid re-logging every poll.)
                    if _looks_destructive(result.stdout):
                        _auto_respond_cooldown[name] = now + 50
                        log.info("Auto-responder HOLDING '%s' — destructive prompt needs a human", name)
                        continue
                    # Read the options and let the LLM choose the one that best
                    # continues the work autonomously, then navigate to it + Enter.
                    # Falls back to Enter (the highlighted/first option) if the LLM
                    # is unavailable or unsure — menus still get handled instantly.
                    options, selected_idx = _parse_menu_options(result.stdout)
                    target = (await _llm_pick_menu_option(name, result.stdout, options)
                              if len(options) >= 2 else None)
                    if target is not None:
                        chosen = await _select_menu_option(name, options, selected_idx, target)
                    else:
                        await asyncio.to_thread(
                            subprocess.run,
                            ["tmux", "send-keys", "-t", name, "Enter"],
                            capture_output=True, text=True, timeout=3,
                        )
                        chosen = "default option (Enter)"
                    _auto_respond_cooldown[name] = now
                    event = {"session": name, "type": prompt_type, "choice": chosen, "ts": now}
                    _auto_respond_log.append(event)
                    # Keep log bounded
                    if len(_auto_respond_log) > 50:
                        _auto_respond_log.pop(0)
                    log.info(f"Auto-responded to {prompt_type} in session '{name}' -> {chosen}")
        except Exception:
            logger.debug("Auto-responder loop iteration failed", exc_info=True)


@app.get("/api/auto-respond-log")
async def api_auto_respond_log():
    """Recent auto-respond events for debugging."""
    return JSONResponse(_auto_respond_log[-20:])


# --- Autopilot Watchdog Loop (formerly "simple watchdog") ---
# Always-on smart supervisor. When a session goes idle because Claude stopped and
# is waiting on the user in ANY way — a question, a choice, a confirmation, work
# it deferred ("left for phase 2", "out of scope", "next steps", "we could
# also…"), or just a soft pause — it reads the screen, asks an LLM what reply
# keeps the work moving on its own, and types that reply back. The user is usually
# away, so the bias is: ALWAYS find a way to continue autonomously. It only holds
# off (WAIT) when Claude is still actively working, when the only next action is
# genuinely destructive/irreversible (needs a human), or when the task is truly
# 100% complete with nothing deferred or optional left.

_SIMPLE_WATCHDOG_INTERVAL = 20          # poll every 20s
_SIMPLE_WATCHDOG_IDLE_SECS = 45         # stable-idle this long before considering action
_SIMPLE_WATCHDOG_COOLDOWN = 90          # min seconds between replies per session
_SIMPLE_WATCHDOG_MAX_LOG = 20
_SIMPLE_WATCHDOG_MAX_SAME_STALL = 3     # back off after N nudges that don't change the screen

_SIMPLE_WATCHDOG_SYSTEM_PROMPT = (
    "You are an autonomous operator keeping a Claude Code agent moving while THE USER IS AWAY. "
    "You are shown the bottom of the agent's terminal; the agent has gone idle. If it has stopped "
    "and is in ANY way waiting on the user before it can keep working, write the exact message to "
    "send so it continues on its own. The user is not here and will not answer — waiting wastes time.\n\n"
    "CRITICAL: only ever continue work that is ALREADY underway. If the agent has not started "
    "anything — a brand-new or empty session, just the welcome screen, or an idle prompt with no "
    "question and no work above it to act on — choose 'wait'. NEVER invent a task, instruction, or "
    "next step out of nothing; you only push EXISTING work forward, you do not start new work.\n\n"
    "NEVER ASSERT RESULTS AND NEVER DECLARE THE WORK DONE. You are only reading a terminal — you do "
    "NOT actually know whether any check, test, build, deploy, or fix passed or works. So you must NEVER:\n"
    "- State or imply that checks/tests passed, or that something is verified, confirmed, working, "
    "fixed, or 'functioning correctly'.\n"
    "- Tell the agent to mark, set, treat, consider, or declare a task complete, done, verified, "
    "resolved, or finished.\n"
    "Your job is to push UNFINISHED work forward, never to rubber-stamp it as finished. If the agent "
    "is showing results and is about to wrap up, do NOT confirm them for it — instead tell it to "
    "re-verify the work ITSELF and keep going, e.g. 'Re-check that yourself end to end before "
    "concluding, then finish anything still left. Don't wait for me.' If the task genuinely has "
    "nothing left to do, choose 'wait' — let the agent or the user close it out, never close it out "
    "for them.\n\n"
    "Treat ALL of these as 'waiting on the user' and answer them so work continues:\n"
    "- Questions or choices ('Which should I do, A or B?', 'Do you want X or Y?', 'which one?').\n"
    "- Confirmations ('Shall I proceed?', 'Want me to continue?', 'Should I also do X?').\n"
    "- Deferrals / scope-punts ('I left this for phase 2', 'X is out of scope', 'as a follow-up', "
    "'next steps:', 'we could also…', 'optionally', 'if you want I can…').\n"
    "- Soft stops ('Let me know how you'd like to proceed', 'standing by', 'paused here').\n\n"
    "How to answer — always push toward FULLY DONE, autonomously:\n"
    "- Choice: pick the option that best completes the overall task and say to proceed with it, e.g. "
    "'Go with option 2 and keep going — don't wait for me.'\n"
    "- Deferral/scope-punt: tell it to do that work now, e.g. 'Do phase 2 now as well. Treat the "
    "whole thing as in scope and finish it end to end. Don't stop to ask.'\n"
    "- Confirmation: 'Yes, proceed. Keep going autonomously and don't wait for me.'\n"
    "- Make reasonable default assumptions; NEVER ask the user anything back; never tell it to stop, "
    "pause, or wait. Keep the message to 1-3 concrete sentences that include an instruction to "
    "continue without the user.\n\n"
    "Choose action 'wait' ONLY if:\n"
    "- The agent is still actively working (spinner / 'esc to interrupt' / tool output streaming), OR\n"
    "- There is NO existing task to advance: a brand-new/empty session, a bare welcome screen, or an "
    "idle prompt with no question, no deferred work, and nothing above it to act on. Do not fabricate "
    "a first instruction — only continue work already on screen, OR\n"
    "- The task is 100% complete: every goal met, nothing deferred, nothing optional left, no question "
    "on screen, OR\n"
    "- *** SAFETY OVERRIDE (this beats the continue-bias) *** the next action is genuinely "
    "DESTRUCTIVE / IRREVERSIBLE / HIGH-COST and a human must decide: deleting or overwriting "
    "production or unrecoverable data, dropping/truncating DB tables, force-pushing or rewriting "
    "shared git history, spending real money above a small (~$100) threshold, or sending mass / "
    "sensitive external messages. If there is ANY doubt about whether an action is destructive, "
    "irreversible, or high-cost, choose 'wait'. Never auto-approve these.\n\n"
    "Respond with STRICT JSON only: {\"action\":\"send\",\"message\":\"<what to type>\"} "
    "or {\"action\":\"wait\"}."
)


# Deterministic safety backstop. If the recent screen (or the message we're about
# to send) names a clearly catastrophic / irreversible operation, we NEVER
# auto-drive it — we leave it for a human, regardless of what the LLM decided.
# Kept tight so it doesn't block the common "just keep going" cases.
_DESTRUCTIVE_RE = re.compile(
    r"\bDROP\s+(?:TABLE|DATABASE|SCHEMA)\b"
    r"|\bTRUNCATE\s+TABLE\b"
    r"|\brm\s+-[rfRF]{1,2}\s+(?:-{1,2}\w+\s+)*(?:/|~|\$HOME|\*|/etc|/var|/usr|/home|/opt|/root|/boot)"
    r"|\b(?:force[- ]?push|push\s+--force\b|push\s+-f\b|git\s+reset\s+--hard)\b"
    r"|\b(?:delet|drop|wip|eras|destroy|purg)\w*\s+(?:\w+\s+){0,5}?(?:production|prod\b|all\s+(?:the\s+)?(?:user|customer|account|record|row|data|table))"
    r"|\b(?:irreversibl\w*|cannot be undone|can'?t be undone|permanently\s+(?:delet|remov|eras|destroy)\w*)"
    r"|\boverwrit\w*\s+(?:\w+\s+){0,5}?(?:production|remote\s+history|shared\s+history)"
    # high-cost spend: a spend verb near a $100+ amount, or any $100+ /month|/year rate
    # ($100+ = 3+ plain digits or comma-grouped thousands; "$99"/"$5/mo" stay under)
    r"|\b(?:spend|purchas\w*|buy|buying|charg\w*|pay|paying|subscrib\w*|upgrad\w*|order\w*)\b[^\n]{0,40}\$\s?(?:[1-9]\d{2,}|[1-9]\d?(?:,\d{3})+)"
    r"|\$\s?(?:[1-9]\d{2,}|[1-9]\d?(?:,\d{3})+)(?:\.\d+)?\s*(?:/|per)\s*(?:mo|month|yr|year)\b",
    re.I,
)


def _looks_destructive(text: str) -> bool:
    """True if the text names a clearly destructive/irreversible/high-cost action
    that should never be auto-approved without a human."""
    return bool(_DESTRUCTIVE_RE.search(text or ""))


# The watchdog must only push UNFINISHED work forward — it must never assert that
# checks/tests passed or instruct the agent to mark a task complete/verified. (It
# only reads a terminal; it cannot actually know any result.) If the composed reply
# does either, we swap it for this neutral nudge so the session still gets unstuck
# without fabricating a status or forcing a premature "done".
_WATCHDOG_SAFE_CONTINUE = (
    "Keep going on your own and take the task all the way to the end. Don't rely on my say-so for "
    "whether it's finished — re-check the work yourself first, then continue with anything still left. "
    "Don't wait for me."
)

_COMPLETION_ASSERT_RE = re.compile(
    # telling the agent to mark/treat/declare the work finished
    r"\b(?:mark|set|flag|treat|consider|declare|call|close)\b[^\n.]{0,45}?\b"
    r"(?:complete|completed|done|finished|verified|resolved|closed)\b"
    r"|\bfully\s+(?:verified|complete|completed|done|tested)\b"
    # asserting checks/tests/steps passed or were confirmed
    r"|\b(?:all|every|each|the|both)\s+(?:check|test|verification|step|task)s?\b[^\n.]{0,35}?\b"
    r"(?:pass(?:ed|es)?|confirm(?:ed)?|verifi(?:ed|es)|green|success\w*|working|complete)\b"
    # asserting something works / is confirmed / functioning correctly
    r"|\b(?:functioning|working|works?|behav\w+|operat\w+)\s+(?:correctly|properly|as[ -]expected|fine)\b"
    r"|\beverything\s+(?:is\s+|looks?\s+|seems?\s+)?(?:working|confirmed|verified|complete|good|fine|in order|passing)\b"
    r"|\bgood\s+to\s+go\b",
    re.I,
)


def _asserts_completion(text: str) -> bool:
    """True if the text claims work passed/works or tells the agent to mark a task
    complete/verified. The watchdog only pushes UNFINISHED work forward — it must
    never rubber-stamp completion or fabricate a result."""
    return bool(_COMPLETION_ASSERT_RE.search(text or ""))


def _parse_autopilot_decision(raw: str):
    """Parse the autopilot LLM JSON. Returns {'action':'send','message':...},
    {'action':'wait'}, or None. Conservative: only 'send' on valid JSON + message."""
    if not raw:
        return None
    t = raw.strip().strip("`").strip()
    if re.fullmatch(r"(?i)wait\.?", t):
        return {"action": "wait"}
    m = re.search(r"\{.*\}", t, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group())
    except Exception:
        return None
    if not isinstance(d, dict):
        return None
    act = str(d.get("action", "")).lower()
    if act == "wait":
        return {"action": "wait"}
    if act == "send":
        msg = str(d.get("message", "")).strip()
        return {"action": "send", "message": msg} if msg else None
    return None


def _simple_watchdog_record(session_name: str, action: str):
    log = _simple_watchdog_log.setdefault(session_name, [])
    log.append({"ts": time.time(), "action": action})
    if len(log) > _SIMPLE_WATCHDOG_MAX_LOG:
        del log[:-_SIMPLE_WATCHDOG_MAX_LOG]


async def _simple_watchdog_send_continue(session_name: str) -> bool:
    """Send 'continue' to the session's Claude Code prompt. Returns True on send."""
    return await _simple_watchdog_send_text(session_name, "continue")


async def _simple_watchdog_send_text(session_name: str, text: str) -> bool:
    """Type a composed reply into the session's Claude Code input box and submit.
    Collapses to a single line so Enter submits the whole message at once."""
    text = " ".join((text or "").split())
    if not text:
        return False
    try:
        # -l sends the text literally (so it isn't interpreted as tmux key names);
        # a separate Enter then submits it to Claude.
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "-l", text],
            capture_output=True, text=True, timeout=5,
        )
        await asyncio.sleep(0.1)
        await asyncio.to_thread(
            subprocess.run,
            ["tmux", "send-keys", "-t", session_name, "Enter"],
            capture_output=True, text=True, timeout=5,
        )
        return True
    except Exception as e:
        logger.debug("autopilot: failed to send reply to '%s': %s", session_name, e)
        return False


async def _simple_watchdog_loop():
    """Background loop: nudge sessions that are paused waiting for 'continue'."""
    slog = logging.getLogger("simple-watchdog")
    await asyncio.sleep(8)  # let startup settle
    while True:
        try:
            await asyncio.sleep(_SIMPLE_WATCHDOG_INTERVAL)
            sessions_list = await asyncio.to_thread(get_tmux_sessions)
            now = time.time()
            for sess in sessions_list:
                name = sess["name"]
                # Free-form "keep going" nudges only run in FULL auto-push mode.
                # ("off"/"basic" leave the composing watchdog idle; basic still
                # gets option-picking + prompt confirms via the auto-responder.)
                if _get_autopush_mode(name) != "full":
                    _simple_watchdog_state.pop(name, None)
                    continue
                # Don't fire if Claude isn't even running
                if not await _async_is_claude_running(name):
                    _simple_watchdog_state.pop(name, None)
                    continue
                # Cooldown
                state = _simple_watchdog_state.setdefault(name, {})
                last_action = state.get("last_action", 0)
                if now - last_action < _SIMPLE_WATCHDOG_COOLDOWN:
                    continue
                # Activity must be idle
                try:
                    activity = await async_detect_activity(name)
                except Exception:
                    continue
                if activity.get("status") != "idle":
                    state["idle_since"] = 0
                    continue
                # Capture the visible pane to inspect the prompt area
                try:
                    vis = await asyncio.to_thread(
                        subprocess.run,
                        ["tmux", "capture-pane", "-t", name, "-p"],
                        capture_output=True, text=True, timeout=3,
                    )
                    if vis.returncode != 0:
                        continue
                    visible = vis.stdout
                except Exception:
                    continue
                # Skip if there's an interactive selection prompt — auto-responder owns it
                if _detect_interactive_prompt(visible):
                    continue
                # Never type "continue" into a bare shell. If Claude crashed to bash
                # between the is-running check above and now, the crash-recovery loop
                # owns relaunching it — typing here would just spam the shell.
                if _looks_like_bare_shell(visible):
                    continue
                # Never act on a brand-new Claude session that hasn't started work yet
                # (welcome splash + empty prompt, no conversation). There is nothing to
                # "continue", so nudging only makes the LLM fabricate a first instruction
                # and type it into an untouched session.
                if _looks_like_fresh_claude_session(visible):
                    continue
                # Skip if user has typed something into the prompt box (don't clobber)
                if _has_pending_user_input(visible):
                    continue
                # Track stable-idle duration
                content_hash = hashlib.md5(visible.encode()).hexdigest()
                if state.get("last_hash") != content_hash:
                    state["last_hash"] = content_hash
                    state["idle_since"] = now
                    continue
                idle_for = now - state.get("idle_since", now)
                if idle_for < _SIMPLE_WATCHDOG_IDLE_SECS:
                    continue
                # Back off if we keep hitting the EXACT same stalled screen — a reply
                # that doesn't move it means poking again won't help; leave it for a human.
                if content_hash == state.get("acted_hash"):
                    same = state.get("same_stall", 0) + 1
                    state["same_stall"] = same
                    if same >= _SIMPLE_WATCHDOG_MAX_SAME_STALL:
                        if not state.get("backed_off"):
                            slog.info("Autopilot backing off '%s' — unchanged after %d replies", name, same)
                            state["backed_off"] = True
                        continue
                else:
                    state["same_stall"] = 0
                    state["backed_off"] = False
                # Read the screen and let the LLM compose the reply that keeps it moving.
                recent = await asyncio.to_thread(capture_pane_recent, name, 80)
                if not recent.strip():
                    continue
                # Safety backstop: never auto-drive a clearly destructive/irreversible
                # action — the one carve-out from the continue bias. Checked before the
                # LLM call (cheap) so we don't waste a call either.
                if _looks_destructive(recent):
                    if state.get("held_hash") != content_hash:
                        state["held_hash"] = content_hash
                        slog.info("Autopilot HOLDING '%s' — possible destructive/irreversible "
                                  "action on screen; needs a human", name)
                    continue
                try:
                    raw = await llm_call(
                        system_prompt=_SIMPLE_WATCHDOG_SYSTEM_PROMPT,
                        user_content=f"Session '{name}' terminal (most recent lines):\n\n{recent[-4500:]}",
                        max_tokens=160,
                        response_format={"type": "json_object"},
                    )
                except Exception:
                    continue
                decision = _parse_autopilot_decision(raw)
                if not decision or decision.get("action") != "send":
                    continue
                msg = (decision.get("message") or "").strip()
                if not msg or _looks_destructive(msg):
                    continue
                # Never let the watchdog assert results or rubber-stamp completion. Its
                # only job is to UNSTICK the session and push unfinished work forward —
                # not to claim checks passed or tell the agent to mark a task done. If the
                # composed reply does either, swap it for a neutral "keep going, verify it
                # yourself" nudge so we still continue without fabricating a status.
                if _asserts_completion(msg):
                    slog.info("Autopilot rewrote completion-asserting reply to '%s': %s",
                              name, (msg if len(msg) <= 120 else msg[:117] + "..."))
                    _simple_watchdog_record(name, f"rewrote completion claim: {msg[:80]}")
                    msg = _WATCHDOG_SAFE_CONTINUE
                # One more guard: re-check Claude is still running before sending
                if not await _async_is_claude_running(name):
                    continue
                ok = await _simple_watchdog_send_text(name, msg)
                if ok:
                    state["last_action"] = now
                    state["idle_since"] = now
                    state["acted_hash"] = content_hash
                    short = msg if len(msg) <= 90 else msg[:87] + "..."
                    _simple_watchdog_record(name, f"replied (idle {int(idle_for)}s): {short}")
                    slog.info("Autopilot replied to '%s' after %ds idle: %s", name, int(idle_for), short)
        except asyncio.CancelledError:
            slog.info("Simple watchdog cancelled")
            raise
        except Exception:
            logger.debug("Simple watchdog iteration failed", exc_info=True)


# --- Auto /login watchdog: re-authenticate a session when Claude asks for login ---

_LOGIN_NEEDED_RE = re.compile(
    r"(?:please run\s+/login|run\s+`?/login`?|type\s+/login|/login\s+to\s+(?:authenticate|continue|log in|sign in)|"
    r"invalid api key|authentication[ _]error|oauth[^\n]*(?:token)?[^\n]*(?:expired|invalid|revoked)|"
    r"(?:your )?session (?:has )?expired|please (?:re-?)?log\s?in|login required|"
    r"you (?:are|'re) not (?:logged in|authenticated)|sign in to continue)",
    re.I,
)
_LOGIN_WATCHDOG_INTERVAL = 15      # seconds between scans
_LOGIN_WATCHDOG_COOLDOWN = 180     # min seconds between auto /login per session
_login_watchdog_state: Dict[str, dict] = {}


async def _login_watchdog_loop():
    """If a session's Claude shows a login-required message, auto-run /login once
    (per cooldown) so the user doesn't have to notice and type it themselves."""
    llog = logging.getLogger("login-watchdog")
    await asyncio.sleep(10)  # let startup settle
    while True:
        try:
            await asyncio.sleep(_LOGIN_WATCHDOG_INTERVAL)
            sessions_list = await asyncio.to_thread(get_tmux_sessions)
            now = time.time()
            for sess in sessions_list:
                name = sess["name"]
                # Auto-push "off" means fully hands-off — don't even auto /login.
                if _get_autopush_mode(name) == "off":
                    continue
                state = _login_watchdog_state.setdefault(name, {})
                if now - state.get("last_action", 0) < _LOGIN_WATCHDOG_COOLDOWN:
                    continue
                if not await _async_is_claude_running(name):
                    continue
                try:
                    recent = await asyncio.to_thread(capture_pane_recent, name, 40)
                except Exception:
                    continue
                low = (recent or "").lower()
                # Is a Claude /login flow currently on screen? (auth-method menu,
                # OAuth URL, paste-code prompt, or the "invalid code — retry" error).
                login_flow_open = bool(recent) and (
                    ("paste" in low and "code" in low)
                    or ("https://" in recent and "oauth" in low)
                    or ("oauth error" in low)
                    or ("select login method" in low)
                    or ("press enter to retry" in low and "esc to cancel" in low)
                )

                # Shared API-key (team) mode: /login is HARMFUL here. It writes OAuth
                # creds that override the working apiKeyHelper key and 401 ("Invalid
                # bearer token"), which then reads as "login required" and would
                # retrigger this watchdog forever — the loop members actually hit. So
                # in key mode we NEVER run /login; instead, if a stray /login flow is
                # stuck on screen we cancel it (Esc) so the session falls back to the
                # shared key. An un-completed /login writes no creds, so Esc fully
                # restores key auth.
                if _stored_anthropic_key:
                    if login_flow_open:
                        try:
                            for _ in range(2):  # menu -> cancel needs two Escapes
                                await asyncio.to_thread(
                                    subprocess.run,
                                    ["tmux", "send-keys", "-t", name, "Escape"],
                                    capture_output=True, text=True, timeout=5,
                                )
                                await asyncio.sleep(0.4)
                            state["last_action"] = now
                            llog.warning("Cancelled stray /login in '%s' (key mode — restored shared-key auth)", name)
                        except Exception as e:
                            llog.debug("login watchdog esc failed for '%s': %s", name, e)
                    continue

                if not recent or not _LOGIN_NEEDED_RE.search(recent):
                    continue
                # A /login flow is already on screen (URL / paste-code prompt) — leave it.
                if login_flow_open:
                    continue
                try:
                    await asyncio.to_thread(
                        subprocess.run,
                        ["tmux", "send-keys", "-t", name, "/login", "Enter"],
                        capture_output=True, text=True, timeout=5,
                    )
                    state["last_action"] = now
                    llog.warning("Auto-ran /login in '%s' (login-required detected)", name)
                except Exception as e:
                    llog.debug("login watchdog send failed for '%s': %s", name, e)
        except asyncio.CancelledError:
            llog.info("Login watchdog cancelled")
            raise
        except Exception:
            logger.debug("Login watchdog iteration failed", exc_info=True)


# --- Crash-recovery watchdog: relaunch Claude when a session OOM/crashes to a shell ---
# When Claude Code exhausts the V8 heap it prints "Aborted" (SIGABRT) — or the OS
# OOM killer prints "Killed", or V8 prints "JavaScript heap out of memory" — and
# the tmux pane drops back to the parent bash. At that point nothing on screen is
# a live Claude prompt, so the auto-responder and simple-watchdog can't help: the
# session just sits dead at a shell forever (the exact "stuck" symptom reported).
# This loop detects that state and relaunches Claude, resuming the crashed
# conversation so the task continues where it left off.

_CRASH_RECOVERY_INTERVAL = 20          # poll every 20s
_CRASH_RECOVERY_COOLDOWN = 120         # min seconds between restart attempts per session
_CRASH_RECOVERY_MAX_ATTEMPTS = 3       # give up after this many consecutive failed restarts
_CRASH_RECOVERY_MAX_TRANSCRIPT = 60_000_000   # don't scan transcripts larger than this (bytes)
_crash_recovery_state: Dict[str, dict] = {}
_seen_claude_running: set = set()       # sessions observed running Claude this process

# Crash signatures that mean Claude (node) died and the pane fell back to a shell.
# Only ever evaluated once the pane is already a bare shell, so false positives are
# very unlikely. libc/kernel messages are matched as exact-case substrings (NOT
# anchored to line-end) because on an OOM the "Aborted" is printed OVER leftover
# TUI text — e.g. it lands mid-line as "Abortedn GRPO…" when Claude's alternate
# screen wasn't cleared. Case-sensitivity still avoids matching a lowercase
# "aborted"/"killed" sitting in prose above the shell.
_CRASH_SIGNATURE_RE = re.compile(
    r"Aborted|Killed|Segmentation fault|Bus error|"
    r"Trace/breakpoint trap|Floating point exception|core dumped"
)
# V8 / out-of-memory death throes (case-insensitive).
_CRASH_OOM_RE = re.compile(
    r"JavaScript heap out of memory|Reached heap limit"
    r"|FATAL ERROR:[^\n]*(?:heap|memory|allocation)"
    r"|<--- Last few GCs --->|out of memory",
    re.I,
)


def _looks_like_crash(text: str) -> bool:
    """True if recent pane output shows a process-death signature (OOM/SIGABRT/etc.)."""
    return bool(_CRASH_SIGNATURE_RE.search(text) or _CRASH_OOM_RE.search(text))

# A user@host:path$ / # / % prompt line. Group 1 = anything typed after it.
_SHELL_PROMPT_RE = re.compile(r"[\w.\-]+@[\w.\-]+:[^\n]*[$#%>]\s*([^\n]*)$")


def _looks_like_bare_shell(visible: str) -> bool:
    """True if the LAST non-empty line looks like a bash/zsh prompt (no Claude TUI)."""
    for line in reversed(visible.split("\n")):
        if not line.strip():
            continue
        return bool(_SHELL_PROMPT_RE.search(line.rstrip()))
    return False


def _shell_has_pending_input(visible: str) -> bool:
    """True if the user seems to have typed a command at the shell prompt that a
    relaunch would clobber. An empty prompt → safe to relaunch."""
    for line in reversed(visible.split("\n")):
        if not line.strip():
            continue
        m = _SHELL_PROMPT_RE.search(line.rstrip())
        if not m:
            return False  # last line is command output, not a typed-at prompt
        return bool(m.group(1).strip())
    return False


def _project_dir_for_cwd(cwd: str) -> Optional[Path]:
    """Map a working directory to its ~/.claude/projects/<encoded> transcript dir.
    Claude encodes the path by replacing '/', '_' and '.' with '-'."""
    base = Path.home() / ".claude" / "projects"
    enc = re.sub(r"[/_.]", "-", cwd.rstrip("/"))
    cand = base / enc
    if cand.is_dir():
        return cand
    try:
        leaf = re.sub(r"[/_.]", "-", cwd.rstrip("/").split("/")[-1])
        for d in sorted(base.glob("*" + leaf)):
            if d.is_dir():
                return d
    except Exception:
        pass
    return None


def _find_session_transcript_uuid(session_name: str) -> Optional[str]:
    """Best-effort: identify the exact conversation UUID a crashed session was
    running, by matching distinctive lines still visible on the pane against the
    project's *frozen* transcripts. Returns None when not confident, so the caller
    falls back to --continue. This disambiguates the common case where several
    sessions share one cwd and plain --continue would resume the wrong one."""
    try:
        cwd = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p", "#{pane_current_path}"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except Exception:
        return None
    if not cwd:
        return None
    proj = _project_dir_for_cwd(cwd)
    if not proj:
        return None
    scroll = capture_pane_recent(session_name, 200)
    if not scroll.strip():
        return None
    # Distinctive lines: long enough, contain real words, not prompts/box-drawing.
    cand = []
    for ln in scroll.split("\n"):
        s = ln.strip().strip("│┃─ \t")
        if len(s) < 20 or _SHELL_PROMPT_RE.search(s) or not re.search(r"[A-Za-z]{4,}", s):
            continue
        cand.append(s)
    cand = sorted(dict.fromkeys(cand), key=len, reverse=True)[:8]
    if not cand:
        return None
    now = time.time()
    best_uuid, best_score = None, 0
    try:
        files = list(proj.glob("*.jsonl"))
    except Exception:
        return None
    for f in files:
        if f.name.startswith("agent-"):
            continue
        try:
            st = f.stat()
        except Exception:
            continue
        age = now - st.st_mtime
        # Skip actively-written transcripts (owned by a live session), stale ones,
        # and anything too large to scan cheaply.
        if age < 45 or age > 86400 or st.st_size > _CRASH_RECOVERY_MAX_TRANSCRIPT:
            continue
        try:
            text = f.read_text(errors="ignore")
        except Exception:
            continue
        score = sum(1 for c in cand if c in text)
        if score > best_score:
            best_score, best_uuid = score, f.stem
    return best_uuid if best_score >= 2 else None


async def _crash_recovery_loop():
    """Relaunch Claude in sessions that have crashed/OOM'd to a bare shell."""
    rlog = logging.getLogger("crash-recovery")
    await asyncio.sleep(12)  # let startup settle
    while True:
        try:
            await asyncio.sleep(_CRASH_RECOVERY_INTERVAL)
            sessions_list = await asyncio.to_thread(get_tmux_sessions)
            now = time.time()
            owners = _load_session_owners()
            for sess in sessions_list:
                name = sess["name"]
                if await _async_is_claude_running(name):
                    _seen_claude_running.add(name)
                    st = _crash_recovery_state.get(name)
                    if st:
                        st["attempts"] = 0
                        st["gave_up"] = False
                    continue
                # Pane is a bare shell. Only touch sessions we manage / have seen run Claude.
                if name not in owners and name not in _seen_claude_running:
                    continue
                state = _crash_recovery_state.setdefault(name, {"attempts": 0, "last_action": 0})
                if now - state.get("last_action", 0) < _CRASH_RECOVERY_COOLDOWN:
                    continue
                try:
                    recent = await asyncio.to_thread(capture_pane_recent, name, 80)
                except Exception:
                    continue
                if not recent.strip():
                    continue
                # Only recover genuine crashes — never hijack an intentional shell.
                if not _looks_like_crash(recent):
                    continue
                # Don't clobber a command the user is typing at the shell.
                if _shell_has_pending_input(recent):
                    continue
                if state.get("attempts", 0) >= _CRASH_RECOVERY_MAX_ATTEMPTS:
                    if not state.get("gave_up"):
                        rlog.error("Crash recovery giving up on '%s' after %d attempts — "
                                   "manual restart needed", name, state["attempts"])
                        state["gave_up"] = True
                    continue
                uuid = await asyncio.to_thread(_find_session_transcript_uuid, name)
                state["attempts"] = state.get("attempts", 0) + 1
                state["last_action"] = now
                rlog.warning("Session '%s' crashed to shell — relaunching Claude (%s), attempt %d/%d",
                             name, ("--resume " + uuid) if uuid else "--continue",
                             state["attempts"], _CRASH_RECOVERY_MAX_ATTEMPTS)
                ok = await _ensure_claude_running(name, resume_uuid=uuid)
                if ok:
                    _seen_claude_running.add(name)
                    _crash_recovery_state[name] = {"attempts": 0, "last_action": now, "gave_up": False}
                    rlog.info("Recovered '%s' — Claude is running again", name)
        except asyncio.CancelledError:
            rlog.info("Crash recovery cancelled")
            raise
        except Exception:
            logger.debug("Crash recovery iteration failed", exc_info=True)


def _has_pending_user_input(visible: str) -> bool:
    """True if the visible pane shows the ❯ user-input box with text already typed.

    Pattern: a line like '❯ some text the user is typing'. We must NOT send
    'continue' in that case — it would concatenate or submit the user's draft.
    Empty input (just '❯' or '❯ ') is fine.
    """
    for line in visible.split("\n")[-20:]:
        m = re.search(r"❯\s+(\S.*)", line)
        if not m:
            continue
        tail = m.group(1).strip()
        # Numbered selection lines like "❯ 1. Yes" are handled by the auto-responder
        if re.match(r"^\d+\.", tail):
            continue
        # Trailing box-drawing chars are not real input
        tail = tail.rstrip("│ \t")
        if tail:
            return True
    return False


# Markers that render ONLY after a conversation has begun in the Claude Code TUI:
# the ⏺ assistant bullet and the ⎿ tool-result tree branch (and the streaming
# "esc to interrupt" footer). None appear on the fresh welcome splash — whose only
# fancy glyphs are the logo block and an ✻/✶ welcome star — so they cleanly tell
# "work has started" apart from "brand-new session". (✻/✶/✳ are deliberately NOT
# here: they also head the welcome banner in some versions.)
_CLAUDE_CONVERSATION_RE = re.compile(r"⏺|⎿|esc to interrupt")
# The welcome splash: the "Claude Code v<n>" line or the logo block glyphs. Only
# rendered before the first turn — a real conversation scrolls it off the pane.
_CLAUDE_WELCOME_RE = re.compile(r"Claude Code v\d|[▐▛▜▌▝▘█]{2,}")


def _looks_like_fresh_claude_session(visible: str) -> bool:
    """True if the pane shows a brand-new Claude session that hasn't started any
    work: the welcome splash is on screen, the ❯ box is empty, and there is no
    conversation below it. Such a session has nothing to 'continue' — without this
    guard the autopilot LLM (hard-biased to keep going) fabricates a first
    instruction out of nothing and types it into an idle, untouched session."""
    if not visible:
        return False
    if not _CLAUDE_WELCOME_RE.search(visible):
        return False
    # Any sign a turn has happened (even one short exchange) → not fresh; the
    # watchdog should handle it normally (e.g. answer a trailing question).
    if _CLAUDE_CONVERSATION_RE.search(visible):
        return False
    # An empty input box confirms the user hasn't even begun a first prompt.
    return not _has_pending_user_input(visible)


@app.get("/api/sessions/{session_name}/autopush")
async def api_autopush_status(session_name: str):
    """Return the per-session auto-push mode ('off'|'basic'|'full') + recent log."""
    return JSONResponse({
        "mode": _get_autopush_mode(session_name),
        "log": list(_simple_watchdog_log.get(session_name, []))[-_SIMPLE_WATCHDOG_MAX_LOG:],
    })


class AutopushBody(BaseModel):
    mode: str


@app.post("/api/sessions/{session_name}/autopush")
async def api_autopush_set(session_name: str, body: AutopushBody):
    """Set the per-session auto-push mode.

    off   — the dashboard never types into this terminal.
    basic — auto-pick option menus + confirm permission/plan prompts + keep the
            session logged in (no free-form messages).
    full  — everything in basic, plus auto-compose a "keep going" nudge when
            Claude pauses waiting on the user before a task is finished.
    """
    mode = (body.mode or "").strip().lower()
    if mode not in AUTOPUSH_MODES:
        return JSONResponse(
            {"error": f"mode must be one of {list(AUTOPUSH_MODES)}"}, status_code=400
        )
    _autopush_mode[session_name] = mode
    _save_autopush_mode()
    # Anything below "full" stops the free-form watchdog right away.
    if mode != "full":
        _simple_watchdog_state.pop(session_name, None)
    return JSONResponse({
        "mode": mode,
        "log": list(_simple_watchdog_log.get(session_name, []))[-_SIMPLE_WATCHDOG_MAX_LOG:],
    })


# --- Legacy simple-watchdog endpoints. Kept for back-compat and now mapped onto
# the auto-push mode: "enabled" == full, "disabled" == basic. ---
@app.get("/api/sessions/{session_name}/simple-watchdog")
async def api_simple_watchdog_status(session_name: str):
    """Return per-session simple-watchdog state (legacy shape)."""
    mode = _get_autopush_mode(session_name)
    return JSONResponse({
        "enabled": mode == "full",
        "mode": mode,
        "log": list(_simple_watchdog_log.get(session_name, []))[-_SIMPLE_WATCHDOG_MAX_LOG:],
    })


class SimpleWatchdogBody(BaseModel):
    enabled: bool


@app.post("/api/sessions/{session_name}/simple-watchdog")
async def api_simple_watchdog_toggle(session_name: str, body: SimpleWatchdogBody):
    """Enable/disable the free-form watchdog (legacy). Maps to auto-push full/basic."""
    mode = "full" if body.enabled else "basic"
    _autopush_mode[session_name] = mode
    _save_autopush_mode()
    if mode != "full":
        _simple_watchdog_state.pop(session_name, None)
    return JSONResponse({
        "enabled": mode == "full",
        "mode": mode,
        "log": list(_simple_watchdog_log.get(session_name, []))[-_SIMPLE_WATCHDOG_MAX_LOG:],
    })


_TMP_WATCHDOG_INTERVAL = 120            # poll /tmp every 2 minutes
_TMP_WATCHDOG_WARN_PCT = 75             # start cleaning at 75% full
_TMP_WATCHDOG_CRITICAL_PCT = 90         # aggressive clean at 90% full
_TMP_WATCHDOG_SAFE_AGE_NORMAL = 3600    # delete files older than 1h at warn level
_TMP_WATCHDOG_SAFE_AGE_CRITICAL = 600   # delete files older than 10m at critical
_TMP_WATCHDOG_PROTECTED_PREFIXES = (
    ".",                # .X11-unix, .ICE-unix, dotfiles
    "claude-",          # active Claude CLI cache
    "tsx-",             # active tsx cache
    "tmux-",            # tmux server sockets
    "systemd-",         # systemd runtime
    "snap-",            # snap runtime
    "node-compile-cache",
    "data-gym-cache",   # tiktoken cache (recreated on demand but expensive)
    "vscode-",
)


async def _tmp_watchdog_loop():
    """Background watchdog: prevents /tmp from filling up.

    When /tmp is a tmpfs (RAM-backed), filling it breaks bash commands and
    any tool that writes temp files. This loop monitors usage and prunes
    stale files before that happens.
    """
    tlog = logging.getLogger("tmp_watchdog")
    tlog.info("Tmp watchdog started — interval=%ds warn=%d%% critical=%d%%",
              _TMP_WATCHDOG_INTERVAL, _TMP_WATCHDOG_WARN_PCT, _TMP_WATCHDOG_CRITICAL_PCT)
    while True:
        try:
            await asyncio.sleep(_TMP_WATCHDOG_INTERVAL)
            await asyncio.to_thread(_tmp_watchdog_check, tlog)
        except asyncio.CancelledError:
            tlog.info("Tmp watchdog cancelled")
            raise
        except Exception as e:
            tlog.error(f"Tmp watchdog loop error: {e}")
            await asyncio.sleep(60)


def _tmp_watchdog_check(tlog: logging.Logger) -> None:
    """One iteration: check /tmp usage and clean if needed. Runs in a thread."""
    try:
        usage = shutil.disk_usage("/tmp")
    except OSError as e:
        tlog.error(f"shutil.disk_usage('/tmp') failed: {e}")
        return
    pct = (usage.used / usage.total) * 100 if usage.total else 0
    if pct < _TMP_WATCHDOG_WARN_PCT:
        return  # plenty of room

    if pct >= _TMP_WATCHDOG_CRITICAL_PCT:
        max_age = _TMP_WATCHDOG_SAFE_AGE_CRITICAL
        level = "CRITICAL"
    else:
        max_age = _TMP_WATCHDOG_SAFE_AGE_NORMAL
        level = "WARN"

    tlog.warning("/tmp at %.1f%% (%s) — pruning entries older than %ds",
                 pct, level, max_age)
    deleted, freed = _tmp_watchdog_prune(max_age, tlog)
    try:
        new_usage = shutil.disk_usage("/tmp")
        new_pct = (new_usage.used / new_usage.total) * 100 if new_usage.total else 0
    except OSError:
        new_pct = pct
    tlog.warning("/tmp cleanup done — removed %d entries (~%d KB freed), now %.1f%% used",
                 deleted, freed // 1024, new_pct)


def _tmp_watchdog_prune(max_age_secs: int, tlog: logging.Logger) -> tuple[int, int]:
    """Delete files/dirs in /tmp older than max_age_secs. Returns (count, bytes_freed).

    Skips any entry whose name starts with a protected prefix (system sockets,
    active CLI caches). Also skips entries owned by other users.
    """
    deleted = 0
    freed = 0
    now = time.time()
    my_uid = os.getuid()
    try:
        entries = list(os.scandir("/tmp"))
    except OSError as e:
        tlog.error(f"scandir /tmp failed: {e}")
        return (0, 0)
    for entry in entries:
        name = entry.name
        if any(name.startswith(p) for p in _TMP_WATCHDOG_PROTECTED_PREFIXES):
            continue
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if st.st_uid != my_uid:
            continue  # don't touch other users' files
        age = now - st.st_mtime
        if age < max_age_secs:
            continue
        size = _tmp_watchdog_size(entry.path) if entry.is_dir(follow_symlinks=False) else st.st_size
        try:
            if entry.is_dir(follow_symlinks=False):
                shutil.rmtree(entry.path, ignore_errors=True)
            else:
                os.unlink(entry.path)
            deleted += 1
            freed += size
        except OSError as e:
            tlog.warning(f"Failed to delete {entry.path}: {e}")
    return (deleted, freed)


def _tmp_watchdog_size(path: str) -> int:
    """Recursive size of a directory in bytes. Best-effort, ignores errors."""
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _: None):
        for f in files:
            try:
                total += os.lstat(os.path.join(root, f)).st_size
            except OSError:
                pass
    return total


HTML_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>__BRAND__ Dashboard</title>
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
.nav-usage{display:none;flex-direction:column;justify-content:center;gap:2px;white-space:nowrap;padding:0 12px 0 4px;margin-right:10px;border-right:1px solid #30363d;flex-shrink:0}
.nav-usage.has-data{display:flex}
.nav-usage-item{display:flex;align-items:center;gap:5px;cursor:default;line-height:1}
.nav-usage-label{color:#6e7681;font-weight:600;font-size:.55rem;letter-spacing:.04em;width:14px;text-transform:uppercase}
.nav-usage-bar{position:relative;width:96px;height:4px;background:#21262d;border-radius:2px;overflow:hidden}
.nav-usage-fill{position:absolute;top:0;left:0;bottom:0;background:#3fb950;border-radius:2px;transition:width .3s,background .15s}
.nav-usage-fill.warn{background:#d29922}
.nav-usage-fill.crit{background:#f85149}
.nav-usage.disabled{display:none}
/* Usage bars mirrored into tools dropdown (mobile only) */
.nav-tools-usage{display:none;padding:8px 14px 4px 14px}
.nav-tools-usage-title{color:#6e7681;font-size:.6rem;text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
.nav-tools-usage-row{display:flex;align-items:center;gap:8px;margin-top:6px}
.nav-tools-usage-label{color:#8b949e;font-weight:600;font-size:.7rem;letter-spacing:.04em;width:22px;text-transform:uppercase}
.nav-tools-usage-bar{flex:1;height:5px;background:#21262d;border-radius:3px;overflow:hidden;position:relative}
.nav-tools-usage-pct{color:#8b949e;font-size:.7rem;width:38px;text-align:right;font-variant-numeric:tabular-nums}
.nav-tools-usage-divider{height:1px;background:#21262d;margin:8px 0 0 0}
.nav-status-text{display:none}
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
.tab-more-icon{display:none;font-size:1.1rem;line-height:1}
.tab-more-menu{display:none;position:absolute;top:100%;left:0;background:#161b22;border:1px solid #30363d;border-radius:8px;min-width:120px;padding:4px 0;z-index:100;box-shadow:0 8px 24px rgba(0,0,0,.4)}
.tab-more-menu.open{display:block}
.tab-more-item{padding:8px 16px;font-size:.85rem;color:#8b949e;cursor:pointer;transition:background .15s,color .15s}
.tab-more-item:hover{background:#1c2128;color:#c9d1d9}
.tab-more-item.active{color:#58a6ff}
.tab-more-model-block{display:none}
.tab-more-model-row{padding:6px 16px;font-size:.7rem;color:#8b949e;cursor:default}
.tab-more-model-row .tab-more-model-label{color:#6e7681;font-size:.6rem;text-transform:uppercase;letter-spacing:.05em}
.tab-more-model-row .tab-more-model-value{margin-left:6px;color:#c9d1d9}
.tab-more-model-sep{height:1px;background:#21262d;margin:4px 0}
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
.chat-empty{align-self:center;margin:auto 0;max-width:80%;text-align:center;color:#6e7681;font-size:.9rem;line-height:1.6}
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
.cmd-send{border:none;border-left:1px solid #30363d;border-radius:0;padding:12px 18px;font-size:.95rem;align-self:flex-end;background:#21262d;color:#c9d1d9;cursor:pointer;transition:background .15s,color .15s;display:flex;align-items:center;justify-content:center;min-width:54px}
.cmd-send:hover{background:#30363d}
.cmd-send.is-mic{color:#8b949e}
.cmd-send.is-send{color:#3fb950}
.cmd-send.is-recording{color:#f85149;animation:composer-pulse 1s ease-in-out infinite}
.cmd-send svg{display:block}
@keyframes composer-pulse{0%,100%{opacity:1}50%{opacity:.35}}
.composer-spin{display:inline-block;width:15px;height:15px;border:2px solid #484f58;border-top-color:#c9d1d9;border-radius:50%;animation:composer-spin .7s linear infinite}
@keyframes composer-spin{to{transform:rotate(360deg)}}

/* Raw tab */
.tab-raw{padding-top:16px}
.btn-stop{display:none;background:#da3633;color:#fff;border:1px solid #da3633;font-weight:600;font-size:.8rem;padding:4px 12px;letter-spacing:.03em}
.btn-stop:hover{background:#f85149;border-color:#f85149;color:#fff}
.btn-stop.visible{display:inline-block}
.chat-controls{display:flex;justify-content:flex-end;margin-bottom:4px;min-height:0}
.raw-controls{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.raw-info{color:#6e7681;font-size:.75rem;flex-shrink:0}
.raw-title{flex:1;min-width:0;color:#8b949e;font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center}
.raw-output{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;font-family:'SF Mono','Fira Code','Cascadia Code',Consolas,monospace;font-size:.8rem;line-height:1.45;color:#c9d1d9;flex:1;min-height:120px;max-height:calc(100vh - 280px);overflow-y:auto;white-space:pre;word-wrap:normal;overflow-x:auto}
.raw-output::-webkit-scrollbar{width:12px;height:12px}
.raw-output::-webkit-scrollbar-track{background:#0d1117}
.raw-output::-webkit-scrollbar-thumb{background:#484f58;border-radius:6px;border:2px solid #0d1117}
.raw-output::-webkit-scrollbar-thumb:hover{background:#5b6571}
.raw-output{scrollbar-width:auto;scrollbar-color:#484f58 #0d1117}
/* Compact member upload footer (single row) so the terminal keeps full height */
.nemo-upload-bar{display:flex;align-items:center;gap:10px;flex-wrap:wrap;padding:6px 0;flex-shrink:0}
.nemo-drop{flex:1;min-width:120px;padding:7px 12px;border:1px dashed #30363d;border-radius:6px;color:#6e7681;font-size:.72rem;text-align:center;cursor:pointer;transition:all .15s}
.nemo-drop.drag-over{border-color:#58a6ff;color:#58a6ff;background:#1f6feb22}
.nemo-drop:hover{border-color:#484f58;color:#8b949e}
.raw-link{color:#58a6ff;text-decoration:underline;text-decoration-color:#30363d;text-underline-offset:2px;word-break:break-all;cursor:pointer}
.raw-link:hover{color:#79c0ff;text-decoration-color:#58a6ff}
.raw-link:visited{color:#a371f7}
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
.uploaded-files{width:100%;margin-top:8px;display:flex;flex-direction:column;gap:4px}
.uploaded-files-label{font-size:.65rem;color:#6e7681;text-transform:uppercase;letter-spacing:.04em;text-align:left}
.uploaded-file{display:flex;align-items:center;gap:6px;padding:5px 8px;background:#0d1117;border:1px solid #21262d;border-radius:4px;font-size:.7rem;font-family:'SF Mono','Fira Code',Consolas,monospace;color:#c9d1d9;text-align:left;overflow:hidden}
.uploaded-file:hover{border-color:#30363d}
.uploaded-file-icon{flex-shrink:0;opacity:.7}
.uploaded-file-name{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.uploaded-file-size{flex-shrink:0;color:#6e7681;font-size:.65rem}
.uploaded-file-btn{flex-shrink:0;padding:2px 6px;font-size:.65rem;background:#21262d;border:1px solid #30363d;border-radius:3px;color:#8b949e;cursor:pointer}
.uploaded-file-btn:hover{background:#30363d;color:#c9d1d9}
.uploaded-file-btn.copied{background:#238636;border-color:#238636;color:#fff}
.uploaded-file-btn.delete:hover{background:#da3633;border-color:#da3633;color:#fff}

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

/* Watchdog toggle */
.watchdog-toggle{position:relative;display:inline-block;width:44px;height:24px}
.watchdog-toggle input{opacity:0;width:0;height:0}
.watchdog-toggle-slider{position:absolute;cursor:pointer;inset:0;background:#21262d;border-radius:12px;transition:.3s}
.watchdog-toggle-slider:before{content:'';position:absolute;height:18px;width:18px;left:3px;bottom:3px;background:#8b949e;border-radius:50%;transition:.3s}
.watchdog-toggle input:checked+.watchdog-toggle-slider{background:#238636}
.watchdog-toggle input:checked+.watchdog-toggle-slider:before{transform:translateX(20px);background:#fff}
.watchdog-log{margin-top:8px;font-size:.78rem;color:#6e7681;max-height:120px;overflow-y:auto;scrollbar-width:thin}
.watchdog-log-entry{padding:2px 0;border-bottom:1px solid #161b22}
.watchdog-log-entry .watchdog-ts{color:#56d364}
/* Auto-push 3-way segmented control (off / basic / full) */
.autopush-seg{display:inline-flex;border:1px solid #30363d;border-radius:7px;overflow:hidden;background:#0d1117;vertical-align:middle}
.autopush-seg button{background:transparent;color:#8b949e;border:none;border-right:1px solid #30363d;padding:5px 13px;font-size:.78rem;cursor:pointer;transition:background .15s,color .15s;font-family:inherit;line-height:1.2}
.autopush-seg button:last-child{border-right:none}
.autopush-seg button:hover{background:#161b22;color:#c9d1d9}
.autopush-seg button.active{color:#fff;font-weight:600}
.autopush-seg button.ap-off.active{background:#484f58}
.autopush-seg button.ap-basic.active{background:#1f6feb}
.autopush-seg button.ap-full.active{background:#238636}
.tab-more-menu .autopush-seg{margin:2px 16px 4px}
.tab-more-menu .autopush-seg button{padding:4px 11px;font-size:.72rem}

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
.modal.modal-wide{max-width:min(1180px,94vw);width:min(1180px,94vw);max-height:92vh;overflow:auto}
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
/* --- NEMO-DEV team (simplified member) mode --- */
.nemo-only{display:none}
body.nemo-simple .nemo-only{display:inline-flex;align-items:center;justify-content:center}
body.nemo-simple .nav-tools-wrap{display:none}
body.nemo-simple .claude-auth{display:none}
body.nemo-simple .nav-server-stats{display:none}
body.nemo-simple .nav-usage{display:none}
body.nemo-simple .profile-select,body.nemo-simple .profile-wrap{display:none!important}
body.nemo-simple .nemo-hide-simple{display:none!important}
.approvals-badge{background:#f85149;color:#fff;border-radius:10px;padding:0 6px;font-size:.65rem;font-weight:600;margin-left:4px}
.conn-row{display:flex;align-items:center;justify-content:space-between;padding:12px 14px;border:1px solid #30363d;border-radius:8px;margin-bottom:10px;background:#0d1117}
.conn-row .conn-name{display:flex;align-items:center;gap:10px;font-size:.9rem;color:#c9d1d9}
.conn-row .conn-ico{font-size:1.15rem}
.conn-status{font-size:.75rem;color:#3fb950;margin-right:8px}
.conn-btn{background:#1f6feb;color:#fff;border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:.8rem}
.conn-btn.disconnect{background:#21262d;color:#f85149;border:1px solid #30363d}
.conn-note{font-size:.75rem;color:#8b949e;margin:4px 0 14px}
.approval-row{border:1px solid #30363d;border-radius:8px;padding:12px;margin-bottom:10px;background:#0d1117}
.approval-row pre{background:#161b22;padding:8px;border-radius:6px;overflow:auto;font-size:.75rem;margin:6px 0;white-space:pre-wrap;word-break:break-all}
.approval-meta{font-size:.78rem;color:#8b949e}
.approval-actions{display:flex;gap:8px;margin-top:8px}
.approval-actions button{border:none;padding:6px 12px;border-radius:6px;cursor:pointer;font-size:.8rem}
.btn-approve{background:#238636;color:#fff}
.btn-deny{background:#da3633;color:#fff}
.pill-pending{color:#d29922}.pill-approved{color:#3fb950}.pill-denied{color:#f85149}
/* impersonation ("log in as") banner */
#imp-banner{position:fixed;bottom:0;left:0;right:0;z-index:9999;background:#9e6a03;color:#fff;display:flex;align-items:center;justify-content:center;gap:14px;padding:9px 14px;font-size:.85rem;box-shadow:0 -2px 12px rgba(0,0,0,.45)}
#imp-banner button{background:#fff;color:#9e6a03;border:none;border-radius:6px;padding:5px 14px;font-size:.8rem;font-weight:600;cursor:pointer}
#imp-banner button:hover{background:#ffe8b3}
.users-actions button.imp{background:#1f6feb22;border:1px solid #1f6feb88;color:#79c0ff}
.create-spinner{width:34px;height:34px;border:3px solid #30363d;border-top-color:#58a6ff;border-radius:50%;animation:nemospin .8s linear infinite;margin:18px auto}
@keyframes nemospin{to{transform:rotate(360deg)}}
.ctx-wrap{display:flex;gap:12px;min-height:min(62vh,560px)}
.ctx-files{width:230px;flex:none;border:1px solid #30363d;border-radius:8px;overflow:auto;max-height:min(62vh,560px);background:#0d1117}
.ctx-file{padding:7px 10px;font-size:.8rem;color:#c9d1d9;cursor:pointer;border-bottom:1px solid #161b22;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.ctx-file:hover{background:#161b22}.ctx-file.active{background:#1f6feb33;color:#fff}
.ctx-edit{flex:1;display:flex;flex-direction:column}
.ctx-edit textarea{flex:1;width:100%;min-height:min(58vh,520px);background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#e6edf3;font-family:monospace;font-size:.82rem;padding:10px;white-space:pre-wrap;word-break:break-word}
.proj-link{display:inline-flex;align-items:center;gap:6px;font-size:.74rem;color:#3fb950;border:1px solid #2ea04340;background:#2ea04314;border-radius:6px;padding:3px 9px;margin-left:8px;text-decoration:none}
.proj-link:hover{background:#2ea04326}
.users-table td .grp-sel{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;border-radius:5px;font-size:.75rem;padding:3px 6px}
.muted{color:#8b949e}
.grp-chip{display:inline-block;background:#161b22;border:1px solid #30363d;border-radius:12px;padding:2px 10px;margin:2px;font-size:.78rem}
.grp-chip a{margin-left:4px;text-decoration:none}

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
.skills-panel{display:flex;flex-direction:column;height:100%;gap:10px;padding:12px 0}
.skills-meta{font-size:.78rem;color:#8b949e;padding:6px 10px;background:#0d1117;border:1px solid #21262d;border-radius:6px}
.skills-meta code{color:#79c0ff;background:transparent;padding:0;font-size:.78rem}
.skills-toolbar{display:flex;gap:8px}
.skills-section{display:flex;flex-direction:column}
.skills-section-header{font-size:.78rem;color:#c9d1d9;font-weight:600;padding:4px 0;border-bottom:1px solid #30363d;margin-bottom:4px}
.skills-section-hint{color:#6e7681;font-weight:400;font-size:.72rem;margin-left:4px}
.skills-list{flex:0 0 auto;max-height:240px;overflow-y:auto}
.skills-row{display:flex;align-items:flex-start;gap:8px;padding:8px 6px;border-bottom:1px solid #21262d}
.skills-row:last-child{border-bottom:0}
.skills-row.disabled-row{opacity:.65}
.skills-row-toggle{flex:0 0 auto;display:flex;align-items:center;padding-top:2px}
.skills-row-toggle input[type=checkbox]{width:16px;height:16px;cursor:pointer;accent-color:#58a6ff}
.skills-row-body{flex:1;min-width:0}
.skills-row-name{color:#58a6ff;font-size:.85rem;font-weight:500;font-family:monospace}
.skills-row-name.readonly-name{color:#c9d1d9;cursor:default}
.skills-row-name.custom-name{color:#7ee787}
.skills-row-desc{font-size:.75rem;color:#8b949e;margin-top:2px;line-height:1.4}
.skills-row-tags{font-size:.68rem;color:#6e7681;margin-top:3px;font-style:italic}
.skills-row-actions{flex:0 0 auto;display:flex;gap:6px}
.skills-row-actions .btn{padding:2px 8px;font-size:.72rem}
.skills-editor-wrap{flex:1;display:flex;flex-direction:column;min-height:0;margin-top:8px;border:1px solid #30363d;border-radius:6px;padding:8px;background:#0d1117}
.skills-editor-header{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin-bottom:8px}
.skills-editor-header input{flex:1;min-width:140px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#c9d1d9;padding:6px 10px;font-size:.85rem;font-family:monospace}
.skills-editor{flex:1;background:#0d1117;color:#c9d1d9;border:1px solid #30363d;border-radius:6px;padding:12px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.85rem;resize:none;min-height:200px;line-height:1.5;outline:none}
.skills-editor:focus{border-color:#58a6ff}
.skills-empty{color:#6e7681;font-size:.8rem;padding:12px 0;text-align:center}
/* Profile dropdown next to Terminal tab */
.profile-wrap{display:flex;align-items:center;gap:6px;margin-left:8px;padding-left:8px;border-left:1px solid #21262d}
.profile-label{font-size:.65rem;color:#6e7681;text-transform:uppercase;letter-spacing:.05em}
.profile-select{background:#0d1117;border:1px solid #30363d;color:#c9d1d9;font-size:.78rem;padding:4px 22px 4px 8px;border-radius:6px;outline:none;cursor:pointer;appearance:none;-webkit-appearance:none;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='10' height='10' viewBox='0 0 10 10'%3E%3Cpath fill='%238b949e' d='M5 7L1 3h8z'/%3E%3C/svg%3E");background-repeat:no-repeat;background-position:right 6px center}
.profile-select:focus{border-color:#58a6ff}
.profile-restart-btn{background:#21262d;border:1px solid #30363d;color:#8b949e;border-radius:6px;padding:3px 8px;cursor:pointer;font-size:.85rem;line-height:1}
.profile-restart-btn:hover{color:#c9d1d9;background:#30363d}
.profile-restart-btn.pending{color:#d2a8ff;border-color:#d2a8ff44}
@media(max-width:768px){.profile-label{display:none}.profile-wrap{margin-left:4px;padding-left:6px}}

/* Profile editor modal */
.profiles-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:200;align-items:flex-start;justify-content:center;padding-top:40px}
.profiles-overlay.active{display:flex}
.profiles-panel{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:20px;width:900px;max-width:calc(100vw - 32px);max-height:calc(100vh - 80px);overflow:hidden;box-shadow:0 8px 24px rgba(0,0,0,.5);display:flex;flex-direction:column}
.profiles-panel h3{color:#f0f6fc;font-size:1.1rem;display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.profiles-hint{font-size:.72rem;color:#6e7681;margin-bottom:12px;line-height:1.4}
.profiles-body{display:flex;gap:14px;flex:1;min-height:0}
.profiles-list{width:240px;flex-shrink:0;display:flex;flex-direction:column;gap:8px;border-right:1px solid #21262d;padding-right:14px;overflow-y:auto}
.profile-row{padding:8px 10px;border:1px solid #21262d;border-radius:6px;cursor:pointer;background:#0d1117;display:flex;flex-direction:column;gap:2px}
.profile-row:hover{background:#1c2128}
.profile-row.selected{border-color:#58a6ff;background:#0d2340}
.profile-row-name{color:#c9d1d9;font-size:.85rem;font-weight:500}
.profile-row-meta{color:#6e7681;font-size:.7rem;font-family:'SF Mono','Fira Code',Consolas,monospace}
.profile-row-builtin{color:#d2a8ff;font-size:.62rem;text-transform:uppercase;letter-spacing:.05em}
.profile-new-btn{background:#1c2333;border:1px solid #388bfd44;color:#58a6ff;padding:7px;border-radius:6px;cursor:pointer;font-size:.8rem;font-weight:500}
.profile-new-btn:hover{background:#253049}
.profile-edit{flex:1;display:flex;flex-direction:column;gap:8px;overflow-y:auto;min-width:0}
.profile-edit label{font-size:.7rem;color:#8b949e;text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin-top:4px}
.profile-edit input,.profile-edit textarea,.profile-edit select{background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:7px 9px;font-size:.85rem;outline:none;font-family:'SF Mono','Fira Code',Consolas,monospace}
.profile-edit input:focus,.profile-edit textarea:focus,.profile-edit select:focus{border-color:#58a6ff}
.profile-edit textarea{resize:vertical;line-height:1.5}
.profile-edit .ed-claude{min-height:200px}
.profile-edit .ed-memory{min-height:120px}
.profile-edit .ed-permissions{min-height:80px}
.profile-edit .extras-section{border:1px solid #21262d;border-radius:6px;padding:8px;background:#0d1117}
.profile-edit .extras-row{display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid #161b22}
.profile-edit .extras-row:last-child{border-bottom:none}
.profile-edit .extras-name{flex:1;color:#c9d1d9;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.78rem;cursor:pointer}
.profile-edit .extras-name:hover{color:#58a6ff}
.profile-edit .extras-meta{color:#6e7681;font-size:.68rem}
.profile-edit .extras-empty{color:#6e7681;font-size:.78rem;font-style:italic;padding:4px 0}
.profile-edit .extras-actions{display:flex;gap:6px;margin-top:6px}
.profile-edit .extras-btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 10px;border-radius:5px;font-size:.72rem;cursor:pointer}
.profile-edit .extras-btn:hover{background:#30363d}
.profile-edit .extras-del{color:#f85149;background:transparent;border:none;cursor:pointer;font-size:.95rem;padding:0 4px}
.profile-edit .extras-del:hover{color:#ff7b72}
.profile-edit .extras-editor{display:none;flex-direction:column;gap:6px;margin-top:6px;padding-top:6px;border-top:1px dashed #30363d}
.profile-edit .extras-editor.active{display:flex}
.profile-edit .extras-editor textarea{min-height:160px}
.profile-edit .row2{display:flex;gap:8px}
.profile-edit .row2>div{flex:1;display:flex;flex-direction:column;gap:4px}
.profile-edit-actions{display:flex;gap:8px;justify-content:space-between;margin-top:10px;border-top:1px solid #21262d;padding-top:10px}
.profile-empty{color:#6e7681;font-size:.85rem;text-align:center;padding:40px 20px}
.profile-edit-readonly{color:#6e7681;font-style:italic;padding:8px 0;font-size:.8rem}
/* Profile editor section tabs */
.pf-tabs{display:flex;gap:2px;flex-wrap:wrap;border-bottom:1px solid #21262d;margin-bottom:8px;flex-shrink:0}
.pf-tab{padding:6px 10px;font-size:.75rem;color:#8b949e;cursor:pointer;border-bottom:2px solid transparent;user-select:none;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.pf-tab:hover{color:#c9d1d9}
.pf-tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
.pf-section{display:none;flex-direction:column;gap:8px;flex:1;min-height:0;overflow-y:auto}
.pf-section.active{display:flex}
.pf-section .ed-rawjson{min-height:240px;font-family:'SF Mono','Fira Code',Consolas,monospace}
.pf-section .ed-mcp{min-height:200px;font-family:'SF Mono','Fira Code',Consolas,monospace}
.pf-section .pf-row{display:flex;align-items:center;gap:8px;padding:6px 0;border-bottom:1px solid #161b22}
.pf-section .pf-row:last-child{border-bottom:none}
.pf-section .pf-row-name{flex:1;color:#c9d1d9;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.78rem;cursor:pointer}
.pf-section .pf-row-name:hover{color:#58a6ff}
.pf-section .pf-row-meta{color:#6e7681;font-size:.68rem}
.pf-section .pf-row-tag{color:#d2a8ff;font-size:.62rem;text-transform:uppercase;letter-spacing:.05em;border:1px solid #d2a8ff44;border-radius:3px;padding:1px 5px}
.pf-section .pf-empty{color:#6e7681;font-size:.78rem;font-style:italic;padding:6px 0}
.pf-section .pf-actions{display:flex;gap:6px;margin-top:8px}
.pf-section .pf-status{padding:8px;border-radius:6px;background:#0d1117;border:1px solid #21262d;font-size:.82rem;color:#c9d1d9}
.pf-section .pf-status.ok{border-color:#238636;color:#7ee787}
.pf-section .pf-status.warn{border-color:#9e6a03;color:#e3b341}
.pf-section .pf-status.err{border-color:#da3633;color:#ff7b72}
.pf-section .pf-banner{font-size:.72rem;color:#6e7681;padding:6px 8px;background:#0d1117;border:1px dashed #21262d;border-radius:6px;line-height:1.4}
.pf-section .pf-help{font-size:.7rem;color:#6e7681;font-style:italic}

/* Settings tabs */
.settings-tabs{display:flex;gap:2px;border-bottom:1px solid #21262d;flex-shrink:0;flex-wrap:wrap}
.settings-tab{padding:8px 14px;font-size:.78rem;color:#8b949e;cursor:pointer;border-bottom:2px solid transparent;user-select:none;text-transform:uppercase;letter-spacing:.04em;font-weight:600}
.settings-tab:hover{color:#c9d1d9}
.settings-tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
.settings-section{display:flex;flex-direction:column;gap:10px;height:100%}
.settings-section label{font-size:.7rem;color:#8b949e;text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin-top:4px}
.settings-section textarea{background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:8px 10px;font-size:.85rem;outline:none;font-family:'SF Mono','Fira Code',Consolas,monospace;resize:vertical;line-height:1.5}
.settings-section textarea:focus{border-color:#58a6ff}
.settings-section .my-ctx-claude{min-height:240px}
.settings-section .my-ctx-memory{min-height:160px}
.settings-section .my-ctx-settings{min-height:120px}
.settings-section .my-ctx-path{font-size:.7rem;color:#6e7681;font-family:'SF Mono','Fira Code',Consolas,monospace;margin-bottom:2px}
.settings-section .my-ctx-actions{display:flex;justify-content:flex-end;gap:8px}
.settings-section .pf-banner{font-size:.72rem;color:#6e7681;padding:6px 8px;background:#0d1117;border:1px dashed #21262d;border-radius:6px;line-height:1.4}
.history-list{display:flex;flex-direction:column;gap:8px}
.history-row{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px 12px;cursor:pointer;display:flex;flex-direction:column;gap:5px}
.history-row:hover{border-color:#58a6ff;background:#1c2128}
.history-row-top{display:flex;justify-content:space-between;align-items:center;gap:8px}
.history-row-name{color:#e6edf3;font-weight:500;font-size:.9rem;display:flex;align-items:center;gap:8px}
.history-row-pill{font-size:.62rem;color:#7ee787;border:1px solid #238636;border-radius:3px;padding:1px 6px;text-transform:uppercase;letter-spacing:.05em}
.history-row-meta{color:#6e7681;font-size:.72rem;font-family:'SF Mono','Fira Code',Consolas,monospace}
.history-row-keyinfo{color:#c9d1d9;font-size:.78rem;line-height:1.4;background:#161b22;border-left:2px solid #58a6ff;padding:5px 8px;border-radius:0 4px 4px 0;white-space:pre-wrap;word-break:break-word;max-height:60px;overflow:hidden;position:relative}
.history-row-keyinfo.empty{color:#6e7681;font-style:italic;border-left-color:#30363d}
.history-detail-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;gap:8px}
.history-detail-back{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 10px;border-radius:5px;font-size:.78rem;cursor:pointer}
.history-detail-back:hover{background:#30363d}
.history-detail-name{color:#e6edf3;font-size:1rem;font-weight:600}
.history-detail-keyinfo{background:#161b22;border:1px solid #21262d;border-left:3px solid #58a6ff;border-radius:0 6px 6px 0;padding:10px 12px;color:#c9d1d9;font-size:.85rem;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.history-detail-keyinfo.empty{color:#6e7681;font-style:italic;border-left-color:#30363d}
.history-detail-msgs{display:flex;flex-direction:column;gap:8px}
.history-msg{background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:8px 10px;color:#e6edf3;font-size:.85rem;line-height:1.5;white-space:pre-wrap;word-break:break-word}
.history-msg-ts{color:#6e7681;font-size:.68rem;font-family:'SF Mono','Fira Code',Consolas,monospace;margin-bottom:4px}
.history-empty{color:#6e7681;font-style:italic;font-size:.85rem;text-align:center;padding:40px 20px}
.users-table{width:100%;border-collapse:collapse;font-size:.85rem}
.users-table th{text-align:left;color:#8b949e;text-transform:uppercase;font-size:.68rem;letter-spacing:.06em;padding:6px 8px;border-bottom:1px solid #21262d}
.users-table td{padding:8px;border-bottom:1px solid #161b22;color:#c9d1d9;vertical-align:middle}
.users-table tr:last-child td{border-bottom:none}
.users-actions{display:flex;gap:6px;justify-content:flex-end}
.users-actions button{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:3px 8px;border-radius:5px;font-size:.72rem;cursor:pointer}
.users-actions button:hover{background:#30363d}
.users-actions button.danger{color:#f85149;border-color:#f8514944}
.users-actions button.danger:hover{background:#3f161a;color:#ff7b72}
.users-role-admin{color:#d2a8ff;font-size:.62rem;text-transform:uppercase;letter-spacing:.05em;border:1px solid #d2a8ff44;border-radius:3px;padding:1px 5px}
.users-role-user{color:#79c0ff;font-size:.62rem;text-transform:uppercase;letter-spacing:.05em;border:1px solid #79c0ff44;border-radius:3px;padding:1px 5px}
.users-new-bar{display:flex;gap:8px;align-items:center;background:#0d1117;border:1px solid #21262d;border-radius:6px;padding:10px;margin-bottom:12px}
.users-new-bar input,.users-new-bar select{background:#161b22;border:1px solid #30363d;border-radius:5px;color:#e6edf3;padding:6px 8px;font-size:.82rem;outline:none}
.users-new-bar input:focus,.users-new-bar select:focus{border-color:#58a6ff}
.users-new-bar button{background:#1f6feb;color:#fff;border:none;padding:7px 14px;border-radius:5px;font-size:.82rem;cursor:pointer;font-weight:500}
.users-new-bar button:hover{background:#388bfd}
.nav-tools-divider{height:1px;background:#21262d;margin:4px 0}

.claudemd-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:200;align-items:flex-start;justify-content:center;padding-top:40px}
.claudemd-overlay.active{display:flex}
.claudemd-panel{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:700px;max-width:calc(100vw - 32px);max-height:calc(100vh - 80px);overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);display:flex;flex-direction:column}
.claudemd-panel h3{color:#f0f6fc;margin-bottom:12px;font-size:1.1rem;display:flex;justify-content:space-between;align-items:center}
.claudemd-editor{width:100%;min-height:350px;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:12px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.85rem;line-height:1.5;resize:vertical;outline:none}
.claudemd-editor:focus{border-color:#58a6ff}
.claudemd-path{font-size:.7rem;color:#6e7681;margin-bottom:8px;font-family:'SF Mono','Fira Code',Consolas,monospace}
.claudemd-actions{display:flex;gap:8px;justify-content:flex-end;margin-top:12px}
.claudemd-label{font-size:.7rem;color:#8b949e;text-transform:uppercase;letter-spacing:.04em;font-weight:600;margin-bottom:4px;display:block}
.claudemd-extras{padding:8px;border:1px solid #21262d;border-radius:6px;background:#0d1117}
.claudemd-extras .extras-row{display:flex;align-items:center;gap:6px;padding:4px 0;border-bottom:1px solid #161b22}
.claudemd-extras .extras-row:last-child{border-bottom:none}
.claudemd-extras .extras-name{flex:1;color:#c9d1d9;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.78rem;cursor:pointer}
.claudemd-extras .extras-name:hover{color:#58a6ff}
.claudemd-extras .extras-meta{color:#6e7681;font-size:.68rem}
.claudemd-extras .extras-empty{color:#6e7681;font-size:.78rem;font-style:italic;padding:4px 0}
.claudemd-extras .extras-actions{display:flex;gap:6px;margin-top:6px}
.claudemd-extras .extras-btn{background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 10px;border-radius:5px;font-size:.72rem;cursor:pointer}
.claudemd-extras .extras-btn:hover{background:#30363d}
.claudemd-extras .extras-del{color:#f85149;background:transparent;border:none;cursor:pointer;font-size:.95rem;padding:0 4px}
.claudemd-extras .extras-del:hover{color:#ff7b72}
.claudemd-extras .extras-editor{display:none;flex-direction:column;gap:6px;margin-top:6px;padding-top:6px;border-top:1px dashed #30363d}
.claudemd-extras .extras-editor.active{display:flex}
.claudemd-extras .extras-editor textarea{min-height:160px;width:100%;background:#0d1117;border:1px solid #30363d;border-radius:6px;color:#e6edf3;padding:8px;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:.85rem;line-height:1.5;resize:vertical;outline:none}
.claudemd-extras .extras-editor textarea:focus{border-color:#58a6ff}

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
  .nav-usage.has-data{display:none}
  .nav-tools-usage.has-data{display:block}
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
  .detail-badges .status-pill{display:none}
  .detail-badges .model-badge{display:none}
  .chat-msg{max-width:92%}
  .chat-messages{max-height:calc(100vh - 320px);min-height:80px}
  .raw-output{max-height:50vh}
  .modal{min-width:280px;margin:0 16px}
  .tab{padding:8px 12px;font-size:.8rem}
  .tab-more-menu{min-width:160px}
  .tab-more-label{display:none}
  .tab-more-arrow{display:none}
  .tab-more-icon{display:inline-block}
  .tab-more-model-block{display:block}
  .profile-select{width:34px;padding:4px;color:transparent;-webkit-text-fill-color:transparent;background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='14' height='14' viewBox='0 0 16 16'%3E%3Cpath fill='%238b949e' d='M8 8a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm0 1c-2.21 0-6 1.106-6 3.3V14h12v-1.7C14 10.106 10.21 9 8 9z'/%3E%3C/svg%3E");background-position:center;background-repeat:no-repeat}
}
</style></head>
<body>
<div id="imp-banner" style="display:none"></div>
<div class="nav-wrapper">
<nav class="top-nav" id="top-nav">
  <span class="nav-brand">__BRAND__</span>
  <button class="nav-new-btn" onclick="showCreateModal()" title="New session">+</button>
  <span class="nav-spacer"></span>
</nav>
<div class="nav-right">
  <span class="nav-server-stats" id="nav-server-stats" title="Click for details" onclick="openStats()" style="cursor:pointer"></span>
  <span class="nav-usage" id="nav-usage">
    <span class="nav-usage-item" id="nav-usage-5h-wrap" title="">
      <span class="nav-usage-label">5h</span>
      <span class="nav-usage-bar"><span class="nav-usage-fill" id="nav-usage-5h-fill" style="width:0%"></span></span>
    </span>
    <span class="nav-usage-item" id="nav-usage-7d-wrap" title="">
      <span class="nav-usage-label">7d</span>
      <span class="nav-usage-bar"><span class="nav-usage-fill" id="nav-usage-7d-fill" style="width:0%"></span></span>
    </span>
  </span>
  <span class="nav-status-text" id="status-info">Watching for changes...</span>
  <div class="nav-tools-wrap">
    <button class="nav-icon-btn" onclick="toggleToolsMenu(event)" title="Tools"><span class="icon">&#x2699;</span></button>
    <div class="nav-tools-menu" id="nav-tools-menu">
      <div class="nav-tools-usage" id="nav-tools-usage">
        <div class="nav-tools-usage-title">Anthropic limits</div>
        <div class="nav-tools-usage-row" id="tools-usage-5h-wrap" title="">
          <span class="nav-tools-usage-label">5h</span>
          <span class="nav-tools-usage-bar"><span class="nav-usage-fill" id="tools-usage-5h-fill" style="width:0%"></span></span>
          <span class="nav-tools-usage-pct" id="tools-usage-5h-pct">&mdash;</span>
        </div>
        <div class="nav-tools-usage-row" id="tools-usage-7d-wrap" title="">
          <span class="nav-tools-usage-label">7d</span>
          <span class="nav-tools-usage-bar"><span class="nav-usage-fill" id="tools-usage-7d-fill" style="width:0%"></span></span>
          <span class="nav-tools-usage-pct" id="tools-usage-7d-pct">&mdash;</span>
        </div>
        <div class="nav-tools-usage-divider"></div>
      </div>
      <div class="nav-tools-item" onclick="openStats();closeToolsMenu()"><span class="icon">&#x1F4CA;</span> System Stats</div>
      <div class="nav-tools-item nav-tools-admin" onclick="openTeamWorkspace();closeToolsMenu()"><span class="icon">&#x1F465;</span> Team &amp; Workspace</div>
      <div class="nav-tools-item nav-tools-admin" onclick="openClaudeMd();closeToolsMenu()"><span class="icon">&#x1F4DD;</span> Global Files (CLAUDE.md + sidecars)</div>
      <div class="nav-tools-item nav-tools-admin" onclick="openProfiles();closeToolsMenu()"><span class="icon">&#x1F464;</span> Profiles</div>
      <div class="nav-tools-item nav-tools-admin" id="nav-tools-approvals" onclick="openApprovals();closeToolsMenu()"><span class="icon">&#x1F6E1;&#xFE0F;</span> Approvals <span class="approvals-badge" id="approvals-badge" style="display:none"></span></div>
      <div class="nav-tools-item nav-tools-admin" onclick="openGlobalContext();closeToolsMenu()"><span class="icon">&#x1F310;</span> Global Context</div>
      <div class="nav-tools-item" onclick="openSettings('mycontext');closeToolsMenu()"><span class="icon">&#x2699;</span> Settings</div>
      <div class="nav-tools-item" onclick="openSettings('history');closeToolsMenu()"><span class="icon">&#x1F4C5;</span> History</div>
      <div class="nav-tools-divider"></div>
      <div class="nav-tools-item" id="nav-tools-whoami" style="color:#6e7681;font-size:.7rem;pointer-events:none">Loading...</div>
      <div class="nav-tools-item" onclick="doLogout();closeToolsMenu()"><span class="icon">&#x21AA;</span> Log out</div>
    </div>
  </div>
  <!-- NEMO-DEV member-only nav controls (shown only in simplified team mode) -->
  <button class="nav-icon-btn nemo-only" id="nav-conn-btn" onclick="openConnections()" title="Connect Drive / Gmail / Calendar"><span class="icon">&#x1F517;</span></button>
  <button class="nav-icon-btn nemo-only" id="nav-logout-btn" onclick="doLogout()" title="Log out"><span class="icon">&#x21AA;</span></button>
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
<!-- Project-scope (cwd-bound) file editor overlay -->
<div class="claudemd-overlay" id="projfile-overlay" onclick="if(event.target===this)closeProjectFile()">
  <div class="claudemd-panel">
    <h3 id="projfile-title">Project file <button class="stats-close" onclick="closeProjectFile()">&times;</button></h3>
    <div class="profiles-hint" style="margin-bottom:10px" id="projfile-banner"></div>
    <label class="claudemd-label" id="projfile-label">File</label>
    <div class="claudemd-path" id="projfile-path"></div>
    <textarea class="claudemd-editor" id="projfile-editor" spellcheck="false"></textarea>
    <div class="claudemd-actions">
      <button class="btn" onclick="closeProjectFile()">Close</button>
      <button class="btn btn-full" onclick="saveProjectFile()">Save</button>
    </div>
  </div>
</div>

<!-- Session MEMORY.md editor overlay -->
<div class="claudemd-overlay" id="memorymd-overlay" onclick="if(event.target===this)closeSessionMemory()">
  <div class="claudemd-panel">
    <h3>MEMORY.md <span id="memorymd-session-tag" style="font-size:.75rem;color:#8b949e;font-weight:400;margin-left:8px"></span> <button class="stats-close" onclick="closeSessionMemory()">&times;</button></h3>
    <div class="profiles-hint" style="margin-bottom:10px">Project auto-memory loaded by Claude Code in this session. Lives at <code id="memorymd-dir"></code> — scoped to the session's <code>(profile, cwd)</code> pair. <span style="color:#e3b341">Note:</span> two tmux sessions on the same profile + cwd share this file. To isolate, put each session on its own profile.</div>
    <label class="claudemd-label">MEMORY.md (index)</label>
    <div class="claudemd-path" id="memorymd-path"></div>
    <textarea class="claudemd-editor" id="memorymd-editor" spellcheck="false"></textarea>
    <div class="claudemd-actions">
      <button class="btn" onclick="closeSessionMemory()">Close</button>
      <button class="btn btn-full" onclick="saveSessionMemory()">Save MEMORY.md</button>
    </div>
    <label class="claudemd-label" style="margin-top:14px">Topic files in same directory</label>
    <div class="extras-section claudemd-extras" id="memorymd-extras">
      <div class="extras-empty">Loading...</div>
    </div>
  </div>
</div>

<!-- CLAUDE.md editor overlay -->
<div class="claudemd-overlay" id="claudemd-overlay" onclick="if(event.target===this)closeClaudeMd()">
  <div class="claudemd-panel" id="claudemd-panel">
    <h3>Global Files <button class="stats-close" onclick="closeClaudeMd()">&times;</button></h3>
    <div class="profiles-hint" style="margin-bottom:10px">Files in your home directory loaded by every Claude Code session. <code>CLAUDE.md</code> is loaded automatically; sidecar <code>CLAUDE_*.md</code> files (like <code>CLAUDE_API_KEYS.md</code>) are loaded on demand when referenced from <code>CLAUDE.md</code>.</div>
    <label class="claudemd-label">CLAUDE.md</label>
    <div class="claudemd-path" id="claudemd-path"></div>
    <textarea class="claudemd-editor" id="claudemd-editor" spellcheck="false"></textarea>
    <div class="claudemd-actions">
      <button class="btn" onclick="closeClaudeMd()">Close</button>
      <button class="btn btn-full" onclick="saveClaudeMd()">Save CLAUDE.md</button>
    </div>
    <label class="claudemd-label" style="margin-top:14px">Additional <code>CLAUDE_*.md</code> files in <code>~/</code></label>
    <div class="extras-section claudemd-extras" id="global-extras">
      <div class="extras-empty">Loading...</div>
    </div>
  </div>
</div>
<!-- Profiles editor overlay -->
<div class="profiles-overlay" id="profiles-overlay" onclick="if(event.target===this)closeProfiles()">
  <div class="profiles-panel">
    <h3>Claude Profiles <button class="stats-close" onclick="closeProfiles()">&times;</button></h3>
    <div class="profiles-hint">Each profile is a fully isolated Claude Code config (settings.json, CLAUDE.md, MEMORY.md, skills/, plus any extra <code>.md</code> sidecar files you add) under <code>~/.claude-&lt;id&gt;/</code> selected per tmux session via <code>CLAUDE_CONFIG_DIR</code>. The <strong>Default</strong> profile uses the standard <code>~/.claude</code>.</div>
    <div class="profiles-body">
      <div class="profiles-list" id="profiles-list">Loading...</div>
      <div class="profile-edit" id="profile-edit"><div class="profile-empty">Select a profile on the left, or create a new one.</div></div>
    </div>
  </div>
</div>

<!-- Settings overlay (My Context / History / Users) -->
<div class="profiles-overlay" id="settings-overlay" onclick="if(event.target===this)closeSettings()">
  <div class="profiles-panel" style="width:980px">
    <h3>Settings <button class="stats-close" onclick="closeSettings()">&times;</button></h3>
    <div class="settings-tabs" id="settings-tabs"></div>
    <div class="settings-body" id="settings-body" style="flex:1;min-height:0;display:flex;flex-direction:column;overflow:hidden">
      <div id="settings-content" style="flex:1;min-height:0;overflow-y:auto;padding-top:8px"></div>
    </div>
  </div>
</div>

<script>
const navEl=document.getElementById('top-nav');
const mainEl=document.getElementById('main');
const statusInfoEl=document.getElementById('status-info');
const BASE='__ROOT_PATH__';
const NEMO_BRAND='__BRAND__';
let NEMO_SIMPLE=('__SIMPLE__'==='true');  // server-injected per-user so it's correct before the first fetch
let sessions=[];
let selectedSession=null;
let pollTimer=null;
const activeTabs={};
const rawState={};
function getRawState(n){if(!rawState[n])rawState[n]={polling:false,timer:null,knownLines:0,userScrolledUp:false,visibleHash:'',firstLoad:true,fullText:'',paneWidth:0};return rawState[n]}

// --- "Hide Bash/Fetch" filter ---
function getHideBashPref(){
  try{const v=localStorage.getItem('hideBashLines');return v===null?true:v==='true'}catch(e){return true}
}
function setHideBashPref(v){
  try{localStorage.setItem('hideBashLines',v?'true':'false')}catch(e){}
}
// Claude Code's TUI lays out tool calls like:
//   ● Bash(sleep 540 && gcloud ...
//         --command="date; ...")        <- wrap continuation, indented, no marker
//     ⎿  Mon May 11 ...                 <- output
//
// The user wants the "specific commands the agent is writing" hidden — so we
// hide the bullet header line AND its wrap-continuation lines (indented, no
// bullet/output marker). We KEEP the `⎿` output lines so the user can still
// see what the command produced.
// Tool-call headers Claude Code renders as "● ToolName(args)". We require the
// "(" so we never hide ordinary prose that happens to start with a word like
// "Read"/"Write"/"Update"/"Add"/"Task". Covers file ops (Write/Edit/Read/...),
// shell, web, search and mcp__ tools so the terminal shows the conversation, not
// the plumbing.
const _BASH_FILTER_RE=/^(?:(?:Bash|BashOutput|Fetch|WebFetch|Read|Edit|MultiEdit|Write|NotebookEdit|Update|Grep|Glob|Task|Search|WebSearch|TodoWrite|Kill|Add)\s*\(|mcp__[^(\s]+\s*\()/i;
const _LEADING_BULLET_RE=/^[\s]*[●⏺•·]/;
const _OUTPUT_MARKER_RE=/^[\s]*⎿/;
const _ANY_DECORATION_RE=/^[\s●⏺•·■□▶▸→↳⎼└├│>*\-​]+/;
function _isBashFetchHeader(line){
  const stripped=line.replace(_ANY_DECORATION_RE,'');
  return _BASH_FILTER_RE.test(stripped);
}
// Part 5: also hide noisy "update" output (claude/npm self-update logs). These are
// long, low-value, and clutter the terminal. Folded into the same Hide toggle.
const _UPDATE_NOISE_RES=[
  /^checking for updates?/i,
  /^installing\b.*\b(claude|update|npm|node|package|version|v?\d)/i,
  /^downloading\b.*\b(claude|update|npm|version|package|v?\d)/i,
  /\b(update (installed|complete|available)|successfully updated|already up to date|claude code v?\d[\d.]* installed)\b/i,
  /^npm\b/i,
  /\bnpm (warn|notice|info|err|http|verb|sill|deprecated|audit|fund)\b/i,
  /^(added|changed|removed|audited)\s+\d+\s+packages?\b/i,
  /\bpackages?\b[^\n]*\blooking for funding\b/i,
  /^found \d+ vulnerabilit/i,
  /\bnpm audit\b/i,
  /\bto (apply|finish|complete) the update\b/i,
  /^restart claude\b/i,
  /^[\[(][#=>\-.\s]{3,}[\])]/,
];
function _isUpdateNoise(line){
  if(!line)return false;
  const s=line.replace(/^[\s⎿●⏺•·>*\-]+/,'').trim();
  if(!s)return false;
  for(let i=0;i<_UPDATE_NOISE_RES.length;i++){if(_UPDATE_NOISE_RES[i].test(s))return true;}
  return false;
}
function applyRawFilter(text){
  if(!getHideBashPref())return text;
  if(!text)return text;
  const lines=text.split('\n');
  const out=[];
  let suppressing=false;
  for(const line of lines){
    if(_isUpdateNoise(line))continue;
    if(_isBashFetchHeader(line)){
      suppressing=true;
      continue;
    }
    if(suppressing){
      // End suppression when we hit an output marker, a new bullet (different
      // tool), or a clearly unrelated structural line (empty line / something
      // starting at column 0 that isn't an indented continuation).
      if(_OUTPUT_MARKER_RE.test(line)){
        suppressing=false;
        // keep this output line
      }else if(_LEADING_BULLET_RE.test(line)){
        suppressing=false;
        // keep this bullet line (different tool)
      }else if(line.trim()===''){
        suppressing=false;
        // keep blank lines so paragraph breaks stay intact
      }else if(/^\S/.test(line)){
        // Starts at column 0 with non-space — not a wrap continuation
        suppressing=false;
      }else{
        // Indented continuation of the hidden command — skip
        continue;
      }
    }
    out.push(line);
  }
  return out.join('\n');
}
function _escTermHtml(s){
  return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}
// Linkify http(s):// URLs and absolute file paths in raw terminal output.
// URL handling:
//  (1) URL on one logical line — escape, trim trailing punctuation, wrap in <a>.
//  (2) URL split across multiple rows by Claude Code's alt-screen TUI — when a
//      whitespace/newline appears at column ≥ paneWidth-4 followed by row
//      padding and a URL-valid char on the next row, treat as a soft wrap:
//      strip padding+newline from href but emit per-chunk <a> tags so the
//      visual layout matches the terminal and every chunk is clickable.
// File-path handling:
//  Absolute paths like /home/foo/bar.md become <BASE>/file?path=... links so
//  the user can open the file in a new tab. Paths inside URLs are skipped
//  because the URL match is preferred at any tied position.
//  Like URLs, a path that Claude Code's TUI soft-wraps across rows (breaking at
//  the pane width, often with a leading indent on the continuation row) is
//  rejoined into one href — see _renderPathSpan for the wrap heuristic.
const _RAW_URL_TRAIL_RE=/[.,;:!?)\]}>'"]/;
const _RAW_PATH_TRAIL_RE=/[.,;:!?)\]}>'"]/;
const _RAW_PATH_CHAR_RE=/[A-Za-z0-9._~\-\/]/;
function _findNextLinkable(text,from){
  // Return earliest URL or path occurrence at or after `from`, or null.
  let best=null;
  const urlIdx=text.slice(from).search(/https?:\/\//);
  if(urlIdx>=0)best={kind:'url',start:from+urlIdx};
  // File paths we linkify: (a) ~/home-relative paths (e.g. ~/PROBST_LAWSUIT/),
  // (b) absolute paths whose last segment carries a .ext, (c) absolute
  // directory paths ending in '/'. Guard: not preceded by a path char or ':'
  // so we stay out of scheme://host URLs and mid-token slashes ("TCP/IP",
  // "and/or"). Only the START offset is used — _renderPathSpan() then greedily
  // consumes the full path from there and makes the final keep/skip decision.
  const pathRe=/(?:^|[^A-Za-z0-9_./:~\-])(~\/[A-Za-z0-9_.\-]+(?:\/[A-Za-z0-9_.\-]+)*\/?|\/(?:[A-Za-z0-9_.\-]+\/)*[A-Za-z0-9_.\-]+\.[A-Za-z0-9]+|\/(?:[A-Za-z0-9_.\-]+\/)+)/g;
  pathRe.lastIndex=Math.max(0,from-1);
  let pm;
  while((pm=pathRe.exec(text))!==null){
    const pStart=pm.index+(pm[0].length-pm[1].length);
    if(pStart<from){pathRe.lastIndex=pm.index+1;continue;}
    if(!best||pStart<best.start){best={kind:'path',start:pStart};}
    break;
  }
  return best;
}
function _renderUrlSpan(text,start,paneWidth){
  const pw=Math.max(20,paneWidth||80);
  const wrapCol=pw-4;
  const MAX_WRAP_LINES=20;
  const MAX_URL_LEN=4096;
  let j=start;
  let crossedNewlines=0;
  while(j<text.length&&(j-start)<MAX_URL_LEN){
    const ch=text[j];
    if(ch==='<'||ch==='>'||ch==='"'||ch==="'"||ch==='`')break;
    if(/\s/.test(ch)){
      let ls=j;
      while(ls>0&&text[ls-1]!=='\n')ls--;
      const col=j-ls;
      if(col<wrapCol)break;
      let k=j;
      while(k<text.length&&(text[k]===' '||text[k]==='\t'))k++;
      if(k>=text.length||text[k]!=='\n')break;
      if(crossedNewlines>=MAX_WRAP_LINES)break;
      const next=text[k+1];
      if(!next||/\s/.test(next))break;
      if(next==='<'||next==='>'||next==='"'||next==="'"||next==='`')break;
      const nextSlice=text.slice(k+1,k+9);
      if(nextSlice.startsWith('http://')||nextSlice.startsWith('https://'))break;
      crossedNewlines++;
      j=k+1;
      continue;
    }
    j++;
  }
  const urlRaw=text.slice(start,j);
  const hasNewlines=urlRaw.indexOf('\n')>=0;
  let href=urlRaw.replace(/[ \t]*\n[ \t]*/g,'');
  let trailText='';
  if(!hasNewlines){
    while(href.length>0&&_RAW_URL_TRAIL_RE.test(href[href.length-1])){
      trailText=href[href.length-1]+trailText;
      href=href.slice(0,-1);
    }
  }
  let html;
  if(href.length===0){
    html=_escTermHtml(urlRaw);
  }else if(!hasNewlines){
    const dispText=urlRaw.slice(0,urlRaw.length-trailText.length);
    html='<a href="'+_escTermHtml(href)+'" target="_blank" rel="noopener noreferrer" class="raw-link">'+_escTermHtml(dispText)+'</a>'+_escTermHtml(trailText);
  }else{
    const parts=urlRaw.split(/(\s+)/);
    const hrefEsc=_escTermHtml(href);
    html='';
    for(const part of parts){
      if(!part)continue;
      if(/^\s+$/.test(part)){
        html+=_escTermHtml(part);
      }else{
        html+='<a href="'+hrefEsc+'" target="_blank" rel="noopener noreferrer" class="raw-link">'+_escTermHtml(part)+'</a>';
      }
    }
  }
  return {html:html,end:j};
}
function _renderPathSpan(text,start,paneWidth){
  const pw=Math.max(20,paneWidth||80);
  const wrapCol=pw-4;
  const minWrap=Math.max(24,Math.floor(pw*0.5));
  const MAX_PATH_LEN=2048;
  const MAX_WRAP_LINES=20;
  // Greedily consume path chars. When we hit whitespace, decide whether it's the
  // end of the path or a soft wrap of one long path across TUI rows — the same
  // situation _renderUrlSpan handles for URLs. Long deliverable paths Claude
  // prints — e.g. …/GRABO_Schmalz_ ⏎  GRIPSTER_…_2026-07-10.pdf — break this way,
  // and the file only carries its .ext on the last row.
  //
  // Two wrap signals, because Claude Code doesn't always draw to the full pane
  // width — inside a bordered/indented box the content wraps at a narrower width
  // than pane_width reports, which used to leave the tail row linked on its own
  // (a truncated /GRABO_Schmalz_…pdf pointing at nothing):
  //   strong — the break sits at/after pane_width-4 (flush to the pane); trust it.
  //   rescue — the path ran flush to a NARROWER row (≤4 trailing pad, row wide
  //            enough to be real) AND what we have so far is an incomplete path
  //            (no .ext, not a dir). The completeness gate means two *complete*
  //            paths on adjacent full rows are never merged.
  let j=start;
  let crossedNewlines=0;
  while(j<text.length&&(j-start)<MAX_PATH_LEN){
    const ch=text[j];
    if(_RAW_PATH_CHAR_RE.test(ch)){j++;continue;}
    if(/\s/.test(ch)){
      let ls=j;while(ls>0&&text[ls-1]!=='\n')ls--;
      const col=j-ls;
      let k=j;while(k<text.length&&(text[k]===' '||text[k]==='\t'))k++;
      if(k>=text.length||text[k]!=='\n')break;   // must be padding-then-newline to be a wrap
      const rowWidth=k-ls;                        // this row's rendered width (rows are padded to a fixed width)
      const padding=k-j;                          // trailing spaces between the path end and the newline
      const strongWrap=col>=wrapCol;
      let rescueWrap=false;
      if(!strongWrap&&padding<=4&&rowWidth>=minWrap&&rowWidth<wrapCol){
        const sofar=text.slice(start,j).replace(/[ \t]*\n[ \t]*/g,'');
        const complete=/\.[A-Za-z0-9]+$/.test(sofar)||sofar.charAt(sofar.length-1)==='/';
        rescueWrap=!complete;
      }
      if(!strongWrap&&!rescueWrap)break;
      if(crossedNewlines>=MAX_WRAP_LINES)break;
      let m=k+1;while(m<text.length&&(text[m]===' '||text[m]==='\t'))m++;  // skip the continuation-row indent
      if(m>=text.length||!_RAW_PATH_CHAR_RE.test(text[m]))break;           // next row must resume with a path char
      const nextSlice=text.slice(m,m+8);
      if(nextSlice.startsWith('http://')||nextSlice.startsWith('https://'))break;  // don't swallow a URL on the next row
      crossedNewlines++;
      j=m;
      continue;
    }
    break;
  }
  // rawSpan keeps the internal newline+padding+indent so the on-screen layout is
  // preserved; pathJoined strips them to form the real filesystem path/href.
  let rawSpan=text.slice(start,j);
  let trail='';
  while(rawSpan.length>0&&_RAW_PATH_TRAIL_RE.test(rawSpan[rawSpan.length-1])){
    trail=rawSpan[rawSpan.length-1]+trail;
    rawSpan=rawSpan.slice(0,-1);
  }
  const pathJoined=rawSpan.replace(/[ \t]*\n[ \t]*/g,'');
  // Keep home-relative (~/…), directory (…/), or extensioned file paths; a bare
  // no-extension token that's none of these stays plain text (avoids linkifying
  // things like "/etc/hostname" or stray absolute-looking fragments).
  const _isHome=pathJoined.slice(0,2)==='~/';
  const _isDir=pathJoined.length>1&&pathJoined.charAt(pathJoined.length-1)==='/';
  const _hasExt=/\.[A-Za-z0-9]+$/.test(pathJoined);
  if(pathJoined.length<2||(!_isHome&&!_isDir&&!_hasExt)){
    return {html:_escTermHtml(text.slice(start,j)),end:j};
  }
  const href=BASE+'/file?path='+encodeURIComponent(pathJoined);
  const hrefEsc=_escTermHtml(href);
  const dataEsc=_escTermHtml(pathJoined);
  // Emit each non-whitespace chunk as its own <a> pointing at the joined href,
  // leaving the wrap whitespace as plain text — the terminal layout is untouched
  // and every visible piece of the path is clickable (mirrors _renderUrlSpan).
  const parts=rawSpan.split(/(\s+)/);
  let html='';
  for(const part of parts){
    if(!part)continue;
    if(/^\s+$/.test(part)){
      html+=_escTermHtml(part);
    }else{
      html+='<a href="'+hrefEsc+'" target="_blank" rel="noopener noreferrer" class="raw-link" data-file-path="'+dataEsc+'">'+_escTermHtml(part)+'</a>';
    }
  }
  html+=_escTermHtml(trail);
  return {html:html,end:j};
}
function _linkifyTerminalText(text,paneWidth){
  if(!text)return '(empty)';
  let out='';
  let i=0;
  while(i<text.length){
    const hit=_findNextLinkable(text,i);
    if(!hit){out+=_escTermHtml(text.slice(i));break;}
    out+=_escTermHtml(text.slice(i,hit.start));
    let rendered;
    if(hit.kind==='url'){
      rendered=_renderUrlSpan(text,hit.start,paneWidth);
    }else{
      rendered=_renderPathSpan(text,hit.start,paneWidth);
    }
    out+=rendered.html;
    i=rendered.end;
    if(i<=hit.start)i=hit.start+1;
  }
  return out;
}
function _hasSelectionWithin(el){
  const sel=window.getSelection?window.getSelection():null;
  if(!sel||sel.isCollapsed||sel.rangeCount===0)return false;
  for(let i=0;i<sel.rangeCount;i++){
    const r=sel.getRangeAt(i);
    if(el.contains(r.startContainer)||el.contains(r.endContainer)||el.contains(r.commonAncestorContainer))return true;
  }
  return false;
}
// Part 4: once the user clears a terminal selection, flush any render we deferred
// while they were highlighting text to copy.
document.addEventListener('selectionchange',()=>{
  if(typeof rawState!=='object'||!rawState)return;
  for(const n in rawState){
    const st=rawState[n];
    if(st&&st._pendingRender){
      const el=document.getElementById('raw-'+n);
      if(el&&!_hasSelectionWithin(el))renderRawText(n);
    }
  }
});
function renderRawText(name){
  const st=getRawState(name);
  const rawEl=document.getElementById('raw-'+name);
  if(!rawEl)return;
  // Part 4: don't clobber an active text selection inside the terminal — defer the
  // re-render so the user can highlight & copy while output keeps streaming.
  if(_hasSelectionWithin(rawEl)){st._pendingRender=true;return;}
  st._pendingRender=false;
  const wasAtBottom=!st.userScrolledUp;
  const prevDistFromBottom=st.userScrolledUp?(rawEl.scrollHeight-rawEl.scrollTop):0;
  const filtered=applyRawFilter(st.fullText);
  rawEl._programmaticScroll=true;
  rawEl.innerHTML=_linkifyTerminalText(filtered,st.paneWidth);
  if(wasAtBottom){
    rawEl.scrollTop=rawEl.scrollHeight;
  }else{
    rawEl.scrollTop=Math.max(0,rawEl.scrollHeight-prevDistFromBottom);
  }
}
function rerenderAllRaw(){
  // Called after the filter preference changes — re-render every cached buffer
  for(const n in rawState){
    if(rawState[n]&&rawState[n].fullText)renderRawText(n);
  }
}
function toggleHideBash(name,checked){
  setHideBashPref(checked);
  const lbl=document.getElementById('hidebash-status-'+name);
  if(lbl)lbl.textContent=checked?'On — hiding tool calls + update logs':'Off — showing all output';
  rerenderAllRaw();
}
const lastStatus={};
// Local chat messages mirror (kept in sync with server)
const chatMessages={};
// Preserve textarea drafts across re-renders
const draftText={};
// Cache terminal content + scroll position across session switches
const rawCache={}; // name -> {text, scrollTop, scrollHeight}

function saveDrafts(){
  ['chat','raw'].forEach(tab=>{
    sessions.forEach(s=>{
      const el=document.getElementById('cmd-'+tab+'-'+s.name);
      if(!el)return; // element not in DOM — don't touch saved draft
      if(el.value)draftText[tab+'-'+s.name]=el.value;
      else delete draftText[tab+'-'+s.name];
    });
  });
}
function restoreDrafts(){
  ['chat','raw'].forEach(tab=>{
    sessions.forEach(s=>{
      const key=tab+'-'+s.name;
      const el=document.getElementById('cmd-'+tab+'-'+s.name);
      if(el&&draftText[key]){el.value=draftText[key];autoGrow(el);updateComposerBtn(key)}
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
      </span>`;
    brand.after(item);
  });
  const items=Array.from(navEl.querySelectorAll('.nav-item'));
  items.reverse().forEach(item=>brand.after(item));
}

function renderChatBubbles(name){
  const msgs=chatMessages[name]||[];
  if(!msgs.length){
    return '<div class="chat-empty">No messages yet. Type below to talk to Claude — each reply shows up here as a short summary.</div>';
  }
  return msgs.map(m=>`
    <div class="chat-msg ${m.role}">
      ${esc(m.text)}
      <div class="chat-meta">${fmtTime(m.ts)}</div>
    </div>`).join('');
}

function saveRawCache(){
  // Save terminal content + scroll position before DOM is destroyed.
  // Prefer the unfiltered fullText (so the filter toggle keeps working after a
  // re-render) and remember the line count so the info label can be restored.
  sessions.forEach(s=>{
    const rawEl=document.getElementById('raw-'+s.name);
    if(rawEl&&!rawEl.textContent.startsWith('Loading')){
      const st=rawState[s.name]||{};
      const fullText=st.fullText||rawEl.textContent;
      const lineCount=fullText?fullText.split('\n').length:0;
      rawCache[s.name]={text:fullText,scrollTop:rawEl.scrollTop,scrollHeight:rawEl.scrollHeight,lineCount:lineCount};
    }
  });
}
function renderDetail(){
  saveDrafts();
  saveRawCache();
  const s=sessions.find(x=>x.name===selectedSession);
  if(!s){mainEl.innerHTML='<div class="empty">No session selected</div>';return}
  // Non-developer (simple) users land on the clean Chat tab; admins on Terminal.
  const tab=activeTabs[s.name]||(NEMO_SIMPLE?'chat':'raw');
  // Sync server messages into local store (merge, don't replace — preserves
  // messages added locally from raw tab that server hasn't echoed back yet)
  if(s.messages && s.messages.length) mergeChatMessages(s.name, s.messages);
  // Update favicon to match selected session
  updateFavicon(s.activity_status);

  mainEl.innerHTML=`
    <div class="tab-bar">
      <div class="tab ${tab==='raw'?'active':''}" onclick="switchTab('${s.name}','raw')">Terminal</div>
      ${renderProfileDropdown(s)}
      <div class="tab-more-wrap">
        <div class="tab tab-more-trigger ${['chat','skills','info'].includes(tab)?'active':''}" onclick="toggleTabMore(event)"><span class="tab-more-label">${{'chat':'Chat','skills':'Skills','info':'Info'}[tab]||'More'}</span><span class="tab-more-icon" aria-label="More">&#x22EF;</span><span class="tab-more-arrow"> &#9662;</span></div>
        <div class="tab-more-menu" id="tab-more-menu">
          ${s.model?`<div class="tab-more-model-block"><div class="tab-more-model-row"><span class="tab-more-model-label">Model</span><span class="tab-more-model-value" id="more-model-${esc(s.name)}">${formatModelName(s.model)}</span></div><div class="tab-more-model-sep"></div></div>`:''}
          <div style="padding:4px 16px 2px;color:#6e7681;font-size:.65rem;text-transform:uppercase;letter-spacing:.05em">Auto-push</div>
          ${autopushSeg(s.name, s.autopush_mode, true)}
          <div style="height:1px;background:#21262d;margin:4px 0"></div>
          <div class="tab-more-item ${tab==='chat'?'active':''}" onclick="switchTab('${s.name}','chat');closeTabMore()">Chat</div>
          ${NEMO_SIMPLE?'':`<div class="tab-more-item ${tab==='skills'?'active':''}" onclick="switchTab('${s.name}','skills');closeTabMore()">Skills</div>`}
          <div class="tab-more-item ${tab==='info'?'active':''}" onclick="switchTab('${s.name}','info');closeTabMore()">Info</div>
          ${NEMO_SIMPLE?'':`
          <div style="height:1px;background:#21262d;margin:4px 0"></div>
          <div style="padding:4px 16px;color:#6e7681;font-size:.65rem;text-transform:uppercase;letter-spacing:.05em">Session files (cwd-bound)</div>
          <div class="tab-more-item" onclick="openSessionMemory('${esc(s.name)}');closeTabMore()">Auto-memory MEMORY.md</div>
          <div class="tab-more-item" onclick="openProjectFile('${esc(s.name)}','CLAUDE.md');closeTabMore()">Project CLAUDE.md</div>
          <div class="tab-more-item" onclick="openProjectFile('${esc(s.name)}','.claude/settings.json');closeTabMore()">Project settings.json</div>
          <div class="tab-more-item" onclick="openProjectFile('${esc(s.name)}','.claude/settings.local.json');closeTabMore()">Project settings.local.json</div>
          <div class="tab-more-item" onclick="openProjectFile('${esc(s.name)}','.mcp.json');closeTabMore()">Project .mcp.json</div>`}
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
        ${(_currentUser&&_currentUser.username&&_currentUser.team_mode)?`<a class="proj-link" href="${location.origin}/${encodeURIComponent(s.owner||_currentUser.username)}/${encodeURIComponent(s.name)}" target="_blank" rel="noopener" title="Open this session's published project in a new tab (Claude publishes here)">&#x1F517; /${esc(s.owner||_currentUser.username)}/${esc(s.name)} &#8599;</a>`:''}
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
            oninput="autoGrow(this);updateComposerBtn('chat-${s.name}')"
            autocomplete="off" spellcheck="true" lang="en"></textarea>
          <button class="btn cmd-send is-mic" id="cmd-send-chat-${s.name}" onclick="composerAction('chat-${s.name}')" title="Record voice message" aria-label="Send or record voice"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3z"/><path d="M19 11a7 7 0 0 1-6 6.92V21h-2v-3.08A7 7 0 0 1 5 11h2a5 5 0 0 0 10 0h2z"/></svg></button>
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
      <div class="raw-output" id="raw-${s.name}" style="${getTerminalHeight()}">Loading Claude Code...</div>
      <div class="raw-resize-handle" onmousedown="startResize(event,'${s.name}')"></div>
      <div class="cmd-bar" style="position:relative">
        <span class="cmd-prompt">$</span>
        <textarea class="cmd-input" id="cmd-raw-${s.name}" rows="1"
          placeholder="Type a message or command…"
          onkeydown="handleRawKey(event,'${s.name}')"
          oninput="autoGrow(this);updateComposerBtn('raw-${s.name}')"
          autocomplete="off" spellcheck="true" lang="en"></textarea>
        <button class="btn cmd-send is-mic" id="cmd-send-raw-${s.name}" onclick="composerAction('raw-${s.name}')" title="Record voice message" aria-label="Send or record voice"><svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3z"/><path d="M19 11a7 7 0 0 1-6 6.92V21h-2v-3.08A7 7 0 0 1 5 11h2a5 5 0 0 0 10 0h2z"/></svg></button>
        <input type="file" id="upload-raw-${s.name}" style="display:none" onchange="uploadFile('${s.name}',this)" multiple>
      </div>
      ${buildKeyBar(s.name,'raw')}
    </div>

    <div class="tab-content ${tab==='skills'?'active':''}" id="tab-skills-${s.name}">
      <div class="skills-panel">
        <div class="skills-meta" id="skills-meta-${s.name}">Loading...</div>
        <div class="skills-toolbar">
          <button class="btn" onclick="newLibrarySkill('${s.name}')">+ New Library Skill</button>
          <button class="btn" onclick="loadProfileSkills('${s.name}')">Refresh</button>
        </div>
        <div class="skills-section">
          <div class="skills-section-header">Active skills <span class="skills-section-hint">(loaded by Claude in this profile)</span></div>
          <div class="skills-list" id="skills-active-${s.name}"></div>
        </div>
        <div class="skills-section">
          <div class="skills-section-header">Library <span class="skills-section-hint">(toggle on/off for this profile)</span></div>
          <div class="skills-list" id="skills-library-${s.name}"></div>
        </div>
        <div class="skills-section">
          <div class="skills-section-header">Built-in <span class="skills-section-hint">(bundled with Claude Code; always available)</span></div>
          <div class="skills-list" id="skills-builtin-${s.name}"></div>
        </div>
        <div class="skills-editor-wrap" id="skills-editor-wrap-${s.name}" style="display:none">
          <div class="skills-editor-header">
            <input id="skills-filename-${s.name}" placeholder="skill-name (e.g. my-skill)">
            <input id="skills-description-${s.name}" placeholder="One-line description (used by Claude to discover the skill)">
            <button class="btn" onclick="saveLibrarySkill('${s.name}')">Save</button>
            <button class="btn" onclick="closeSkillEditor('${s.name}')">Cancel</button>
          </div>
          <textarea class="skills-editor" id="skills-editor-${s.name}" placeholder="# Skill body (markdown)..."></textarea>
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
      <div id="login-health-${s.name}"></div>
      <div class="tier" style="margin-top:12px" id="stats-tier-${s.name}">
        <div class="tier-label"><span class="dot" style="background:#79c0ff"></span>Usage &amp; Rate</div>
        <div id="stats-panel-${s.name}" style="margin-top:6px;color:#6e7681;font-size:.85rem">Loading stats...</div>
      </div>
      <div class="tier" style="margin-top:12px" id="hidebash-tier-${s.name}">
        <div class="tier-label"><span class="dot" style="background:#f0883e"></span>Terminal: Hide tool calls</div>
        <div style="display:flex;align-items:center;gap:12px;margin-top:6px">
          <label class="watchdog-toggle">
            <input type="checkbox" id="hidebash-toggle-${s.name}"
              onchange="toggleHideBash('${esc(s.name)}',this.checked)"
              ${getHideBashPref()?'checked':''}>
            <span class="watchdog-toggle-slider"></span>
          </label>
          <span id="hidebash-status-${s.name}" style="font-size:.82rem;color:#8b949e">${getHideBashPref()?'On — hiding tool calls + update logs':'Off — showing all output'}</span>
        </div>
        <div style="font-size:.72rem;color:#6e7681;margin-top:6px;line-height:1.4">Hides tool-call lines like <code style="color:#79c0ff">Bash(…)</code>, <code style="color:#79c0ff">Write(…)</code>, <code style="color:#79c0ff">Edit(…)</code>, <code style="color:#79c0ff">Read(…)</code>, <code style="color:#79c0ff">Fetch(…)</code>, <code style="color:#79c0ff">add(…)</code>, <code style="color:#79c0ff">mcp__…(…)</code> (and their wrapped lines + update logs) so you can focus on the conversation. Output (<code style="color:#79c0ff">⎿</code>) stays. Setting is shared across all sessions.</div>
      </div>
      <div class="tier" style="margin-top:12px" id="watchdog-tier-${s.name}">
        <div class="tier-label"><span class="dot" style="background:#56d364"></span>Auto-push</div>
        <div style="margin-top:8px">${autopushSeg(s.name, s.autopush_mode, false)}</div>
        <div id="autopush-status-${s.name}" style="font-size:.82rem;color:#8b949e;margin-top:8px">${autopushDesc(s.autopush_mode)}</div>
        <div style="font-size:.72rem;color:#6e7681;margin-top:6px;line-height:1.5">
          <b style="color:#8b949e">Off</b> — the dashboard never types into this terminal.<br>
          <b style="color:#79c0ff">Basic</b> — auto-selects from Claude's option menus and confirms permission/plan prompts (presses Enter), and keeps the session logged in. No free-form messages.<br>
          <b style="color:#56d364">Full</b> — everything in Basic, plus it writes a "keep going" instruction when Claude pauses or seems to stop before the task is 100% finished.
        </div>
        <div class="watchdog-log" id="watchdog-log-${s.name}"></div>
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
  // Populate the uploaded-files list under the upload area
  refreshUploadedFiles(s.name);
  // Scroll chat to bottom
  const chatEl=document.getElementById('chat-'+s.name);
  if(chatEl)chatEl.scrollTop=chatEl.scrollHeight;
  // Start/stop raw polling based on active tab
  stopAllRawPolling();
  stopStatsPolling();
  if(tab==='raw'){
    const rawEl=document.getElementById('raw-'+s.name);
    if(rawEl){
      const cached=rawCache[s.name];
      const infoEl=document.getElementById('raw-info-'+s.name);
      if(cached){
        // Restore unfiltered text into state, then render through the filter
        const st=getRawState(s.name);
        st.fullText=cached.text;
        st.userScrolledUp=false;
        rawEl._programmaticScroll=true;
        rawEl.innerHTML=_linkifyTerminalText(applyRawFilter(cached.text),st.paneWidth);
        rawEl.scrollTop=rawEl.scrollHeight;
        if(infoEl&&cached.lineCount)infoEl.textContent=cached.lineCount+' lines';
        startRawPolling(s.name);
      }else{
        loadRaw(s.name);
        startRawPolling(s.name);
      }
    }
  }
  if(tab==='info'){
    startStatsPolling(s.name);
    if((s.autopush_mode||'basic')==='full')startWatchdogPolling(s.name);
    else loadWatchdogStatus(s.name);
  }
  if(tab==='skills')loadProfileSkills(s.name);
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
      if(trigger){trigger.classList.add('active');trigger.innerHTML='<span class="tab-more-label">'+({'chat':'Chat','skills':'Skills','info':'Info'}[tab]||'More')+'</span><span class="tab-more-icon" aria-label="More">&#x22EF;</span><span class="tab-more-arrow"> &#9662;</span>';}
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
  stopAllWatchdogPolling();
  if(tab==='raw')startRawPolling(name);
  if(tab==='info'){
    startStatsPolling(name);
    const s=sessions.find(x=>x.name===name);
    if(s&&(s.autopush_mode||'basic')==='full')startWatchdogPolling(name);
    else loadWatchdogStatus(name);
  }
  if(tab==='skills')loadProfileSkills(name);
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
      // Clear the "no messages yet" placeholder + any typing indicator
      const empty=chatEl.querySelector('.chat-empty');
      if(empty)empty.remove();
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

// Mirror the server's "one assistant summary per turn" model into the local
// chat + DOM: update the latest assistant bubble in place when its text changes,
// append it when the turn is new. Prevents duplicate summary bubbles.
function reconcileAssistantSummary(name, serverMsgs){
  // The server's latest assistant message within the current turn.
  let srv=null;
  for(let i=serverMsgs.length-1;i>=0;i--){
    if(serverMsgs[i].role==='assistant'){srv=serverMsgs[i];break;}
    if(serverMsgs[i].role==='user')break;
  }
  if(!srv||!srv.text)return;
  const local=chatMessages[name]||(chatMessages[name]=[]);
  // Latest local assistant message after the last local user message.
  let lu=-1;for(let i=local.length-1;i>=0;i--){if(local[i].role==='user'){lu=i;break;}}
  let la=-1;for(let i=local.length-1;i>lu;i--){if(local[i].role==='assistant'){la=i;break;}}
  if(la>=0){
    if(local[la].text!==srv.text){
      local[la].text=srv.text;local[la].ts=srv.ts;
      updateLastAssistantBubble(name,srv.text,srv.ts);
    }
  }else{
    appendChatBubble(name,'assistant',srv.text,srv.ts);
  }
}
function updateLastAssistantBubble(name,text,ts){
  if(name!==selectedSession||(activeTabs[name]||'chat')!=='chat')return;
  const chatEl=document.getElementById('chat-'+name);
  if(!chatEl)return;
  const bubbles=chatEl.querySelectorAll('.chat-msg.assistant');
  const el=bubbles[bubbles.length-1];
  if(el)el.innerHTML=esc(text)+'<div class="chat-meta">'+fmtTime(ts)+'</div>';
}

function autoGrow(el){
  if(el.classList.contains('expanded'))return;
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,400)+'px';
}
// --- WhatsApp-style send behavior ---
// Touch-primary devices (phones / tablets) report no hover + a coarse pointer.
// There, like the WhatsApp mobile app, Enter inserts a newline and the only way
// to send is the button. On desktop, Enter sends and Shift+Enter is a newline.
function isTouchPrimary(){
  try{return !!(window.matchMedia&&window.matchMedia('(hover: none) and (pointer: coarse)').matches);}
  catch(e){return false;}
}
function _composerEnterSends(e){
  // True only for a bare desktop Enter that isn't part of an IME composition.
  return e.key==='Enter'&&!e.shiftKey&&!e.ctrlKey&&!e.metaKey&&!e.altKey
    &&!e.isComposing&&e.keyCode!==229&&!isTouchPrimary();
}
function handleChatKey(e,name){
  if(_composerEnterSends(e)){e.preventDefault();sendChat(name);}
  // Otherwise Enter inserts a newline (Shift+Enter on desktop, Enter on mobile).
}
function handleRawKey(e,name){
  if(_composerEnterSends(e)){e.preventDefault();sendCmd(name,'raw');}
  // Otherwise Enter inserts a newline; send via the button (mobile) or Enter (desktop).
}

// --- WhatsApp-style composer: mic icon when empty, paper-plane when typing ---
const _COMPOSER_SEND_SVG='<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M3.3 20.7l18-8a.8.8 0 0 0 0-1.4l-18-8a.8.8 0 0 0-1.1.9L4 11l9 1-9 1-1.8 6.8a.8.8 0 0 0 1.1.9z"/></svg>';
const _COMPOSER_MIC_SVG='<svg viewBox="0 0 24 24" width="18" height="18" fill="currentColor"><path d="M12 14a3 3 0 0 0 3-3V6a3 3 0 0 0-6 0v5a3 3 0 0 0 3 3z"/><path d="M19 11a7 7 0 0 1-6 6.92V21h-2v-3.08A7 7 0 0 1 5 11h2a5 5 0 0 0 10 0h2z"/></svg>';
const _COMPOSER_STOP_SVG='<svg viewBox="0 0 24 24" width="16" height="16" fill="currentColor"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>';
const _recording={},_mediaRec={},_audioChunks={};
// `key` is 'chat-<name>' or 'raw-<name>'. Input id = cmd-<key>, button id = cmd-send-<key>.
function updateComposerBtn(key){
  const inp=document.getElementById('cmd-'+key),btn=document.getElementById('cmd-send-'+key);
  if(!inp||!btn||_recording[key])return;
  const hasText=inp.value.trim().length>0;
  btn.classList.toggle('is-send',hasText);
  btn.classList.toggle('is-mic',!hasText);
  btn.innerHTML=hasText?_COMPOSER_SEND_SVG:_COMPOSER_MIC_SVG;
  btn.title=hasText?'Send message':'Record voice message';
}
function composerAction(key){
  const inp=document.getElementById('cmd-'+key);
  if(inp&&inp.value.trim().length>0){
    if(key.indexOf('raw-')===0)sendCmd(key.slice(4),'raw');
    else sendChat(key.slice(5));
  }else{toggleRecording(key);}
}
async function toggleRecording(key){
  if(_recording[key]){const m=_mediaRec[key];if(m&&m.state!=='inactive')m.stop();return;}
  if(!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){alert('Microphone is not available in this browser.');return;}
  let stream;
  try{stream=await navigator.mediaDevices.getUserMedia({audio:true});}
  catch(e){alert('Microphone permission denied or unavailable.');return;}
  let mr;
  try{mr=new MediaRecorder(stream);}catch(e){stream.getTracks().forEach(t=>t.stop());alert('Recording is not supported in this browser.');return;}
  _mediaRec[key]=mr;_audioChunks[key]=[];
  mr.ondataavailable=ev=>{if(ev.data&&ev.data.size>0)_audioChunks[key].push(ev.data);};
  mr.onstop=async()=>{
    stream.getTracks().forEach(t=>t.stop());
    _recording[key]=false;
    const blob=new Blob(_audioChunks[key],{type:(mr.mimeType||'audio/webm')});
    const btn=document.getElementById('cmd-send-'+key);
    if(btn)btn.classList.remove('is-recording');
    if(!blob.size){updateComposerBtn(key);return;}
    if(btn){btn.classList.add('is-transcribing');btn.innerHTML='<span class="composer-spin"></span>';btn.title='Transcribing…';}
    try{
      const fd=new FormData();fd.append('audio',blob,'voice.webm');
      const resp=await fetch(BASE+'/api/transcribe',{method:'POST',body:fd});
      const j=await resp.json().catch(()=>({}));
      const inp=document.getElementById('cmd-'+key);
      if(resp.ok&&j.text){
        if(inp){inp.value=(inp.value.trim()?inp.value.replace(/\s*$/,'')+' ':'')+j.text;autoGrow(inp);inp.focus();updateComposerBtn(key);}
      }else{alert((j&&j.error)||'Transcription failed.');}
    }catch(e){alert('Transcription failed.');}
    if(btn)btn.classList.remove('is-transcribing');
    updateComposerBtn(key);
  };
  try{mr.start();}catch(e){stream.getTracks().forEach(t=>t.stop());alert('Could not start recording.');return;}
  _recording[key]=true;
  const btn=document.getElementById('cmd-send-'+key);
  if(btn){btn.classList.remove('is-mic','is-send');btn.classList.add('is-recording');btn.innerHTML=_COMPOSER_STOP_SVG;btn.title='Stop & transcribe';}
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
    input.value='';input.style.height='auto';updateComposerBtn('chat-'+name);
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
        refreshUploadedFiles(name);
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
    input.value='';input.style.height='auto';updateComposerBtn(source+'-'+name);
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
  // Poll fast (300ms) for the first ~6s while Claude Code's TUI is booting,
  // then drop to 1s steady-state polling.
  let ticks=0;
  st.timer=setInterval(()=>{
    pollRawDelta(name);
    ticks++;
    if(ticks===20){ // ~6s of fast polling
      clearInterval(st.timer);
      st.timer=setInterval(()=>pollRawDelta(name),1000);
    }
  },300);
}
function stopRawPolling(name){
  const st=getRawState(name);
  st.polling=false;
  if(st.timer){clearInterval(st.timer);st.timer=null}
}
function stopAllRawPolling(){
  for(const n in rawState)stopRawPolling(n);
}

function _ensureRawScrollTracking(rawEl,st){
  if(rawEl._scrollTracked)return;
  rawEl._scrollTracked=true;
  // Single `scroll` listener catches wheel, touch, scrollbar drag, and keyboard
  // navigation. Programmatic scrolls flip `_programmaticScroll` so they don't
  // get misclassified as user scrolls.
  rawEl.addEventListener('scroll',()=>{
    if(rawEl._programmaticScroll){
      rawEl._programmaticScroll=false;
      // Programmatic scroll-to-bottom keeps the "follow-tail" mode on
      const atBottom=(rawEl.scrollHeight-rawEl.scrollTop-rawEl.clientHeight)<10;
      if(atBottom)st.userScrolledUp=false;
      return;
    }
    const atBottom=(rawEl.scrollHeight-rawEl.scrollTop-rawEl.clientHeight)<24;
    st.userScrolledUp=!atBottom;
  },{passive:true});
}

async function pollRawDelta(name){
  const st=getRawState(name);
  const rawEl=document.getElementById('raw-'+name);
  const infoEl=document.getElementById('raw-info-'+name);
  if(!rawEl)return;
  _ensureRawScrollTracking(rawEl,st);
  try{
    const q='?known_lines='+st.knownLines+'&last_hash='+encodeURIComponent(st.visibleHash||'');
    const resp=await fetch(BASE+'/api/sessions/'+name+'/raw-tail'+q);
    const data=await resp.json();
    if(typeof data.visible_hash==='string')st.visibleHash=data.visible_hash;
    if(typeof data.pane_width==='number'&&data.pane_width>0)st.paneWidth=data.pane_width;
    if(data.mode==='full'){
      st.fullText=data.raw||'';
      st.knownLines=data.pane_total;
      st.firstLoad=false;
      renderRawText(name);
      if(infoEl)infoEl.textContent=data.total_lines+' lines';
    }else if(data.mode==='delta'&&data.raw){
      const newLines=data.raw.split('\n');
      const existingLines=(st.fullText||'').split('\n');
      let appendFrom=0;
      let overlapMatched=false;
      if(data.overlap&&existingLines.length>=data.overlap){
        const tail=existingLines.slice(-data.overlap).join('\n');
        const head=newLines.slice(0,data.overlap).join('\n');
        if(tail===head){appendFrom=data.overlap;overlapMatched=true}
      }
      if(overlapMatched){
        const toAppend=newLines.slice(appendFrom).join('\n');
        if(toAppend){
          st.fullText=(st.fullText?st.fullText+'\n':'')+toAppend;
        }
        st.knownLines=data.pane_total;
        renderRawText(name);
        if(infoEl)infoEl.textContent=data.total_lines+' lines';
      }else{
        // Overlap mismatch — fetch a full snapshot to resync
        st.knownLines=0;
        const fullResp=await fetch(BASE+'/api/sessions/'+name+'/raw-tail?known_lines=0');
        const fullData=await fullResp.json();
        if(fullData.mode==='full'){
          st.fullText=fullData.raw||'';
          st.knownLines=fullData.pane_total;
          if(typeof fullData.pane_width==='number'&&fullData.pane_width>0)st.paneWidth=fullData.pane_width;
          renderRawText(name);
          if(infoEl)infoEl.textContent=fullData.total_lines+' lines';
        }
      }
    }else if(data.mode==='none'){
      // No change upstream — but if the info label still says "Loading..."
      // (e.g. first poll after restoring cached content) make sure it's
      // replaced so the user doesn't see a stuck loading indicator.
      if(infoEl&&/loading/i.test(infoEl.textContent)){
        const lineCount=(st.fullText||'').split('\n').length;
        infoEl.textContent=(data.total_lines||lineCount)+' lines';
      }
    }
  }catch(e){}
}

async function loadRaw(name){
  const st=getRawState(name);
  st.knownLines=0;
  st.userScrolledUp=false;
  st.visibleHash='';
  st.firstLoad=true;
  st.fullText='';
  const rawEl=document.getElementById('raw-'+name);
  if(rawEl)rawEl.textContent='Loading Claude Code...';
  const infoEl=document.getElementById('raw-info-'+name);
  if(infoEl)infoEl.textContent='Loading terminal...';
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
  // Sync messages from server. The server keeps ONE assistant summary per turn,
  // updated in place as Claude's output settles, so mirror that: refresh the
  // latest assistant bubble when its text changes, append it when it's new —
  // never duplicate it.
  if(s.messages&&s.messages.length){
    reconcileAssistantSummary(s.name, s.messages);
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
    // Resolve simple-mode before the first render so member toggles don't flash.
    await loadCurrentUser();
    NEMO_SIMPLE=!!(_currentUser&&_currentUser.simple);
    document.body.classList.toggle('nemo-simple',NEMO_SIMPLE);
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
      // Update model badge in nav
      const si=sessions.findIndex(s=>s.name===st.name);
      if(si>=0){
        let navChanged=false;
        if(st.model&&sessions[si].model!==st.model){
          sessions[si].model=st.model;navChanged=true;
          // Update detail model badge if visible
          const mb=document.getElementById('model-badge-'+st.name);
          if(mb)mb.textContent=formatModelName(st.model);
          // Also update the model value shown in the mobile More dropdown
          const mm=document.getElementById('more-model-'+st.name);
          if(mm)mm.textContent=formatModelName(st.model);
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
    navStatsEl.innerHTML='<span>CPU <span class="stat-val '+cpuClass+'">'+cpuPct+'%</span> <span style="color:#6e7681">'+threads+'/'+cpuCount+'t</span></span>';
  }catch(e){navStatsEl.innerHTML=''}
}
refreshNavStats();

// --- Anthropic OAuth usage limits (5h + 7d) in nav header ---
let _usageLimitsTimer=null;
function _fmtResetTime(iso){
  if(!iso)return'';
  try{
    const d=new Date(iso);
    const now=new Date();
    const diffMs=d-now;
    if(diffMs<=0)return'now';
    const mins=Math.floor(diffMs/60000);
    if(mins<60)return'in '+mins+'m';
    const hrs=Math.floor(mins/60);
    if(hrs<48)return'in '+hrs+'h';
    return'in '+Math.floor(hrs/24)+'d';
  }catch(e){return''}
}
function _applyUsageStyle(fillEl,pctNum){
  if(!fillEl)return;
  fillEl.style.width=Math.min(100,Math.max(0,pctNum))+'%';
  let cls='';
  if(pctNum>=90)cls='crit';
  else if(pctNum>=70)cls='warn';
  fillEl.className='nav-usage-fill'+(cls?' '+cls:'');
}
async function refreshUsageLimits(){
  if(NEMO_SIMPLE) return;  // members don't see usage bars
  const wrap=document.getElementById('nav-usage');
  const toolsWrap=document.getElementById('nav-tools-usage');
  if(!wrap)return;
  const setHasData=(has)=>{
    wrap.classList.toggle('has-data',has);
    if(toolsWrap)toolsWrap.classList.toggle('has-data',has);
  };
  try{
    const resp=await fetch(BASE+'/api/usage/limits');
    if(!resp.ok){setHasData(false);return}
    const data=await resp.json();
    if(!data||(!data.five_hour&&!data.seven_day)){setHasData(false);return}
    setHasData(true);
    const fh=data.five_hour||{};
    const sd=data.seven_day||{};
    const fhPct=Math.round(Number(fh.utilization)||0);
    const sdPct=Math.round(Number(sd.utilization)||0);
    _applyUsageStyle(document.getElementById('nav-usage-5h-fill'),Number(fh.utilization)||0);
    _applyUsageStyle(document.getElementById('nav-usage-7d-fill'),Number(sd.utilization)||0);
    _applyUsageStyle(document.getElementById('tools-usage-5h-fill'),Number(fh.utilization)||0);
    _applyUsageStyle(document.getElementById('tools-usage-7d-fill'),Number(sd.utilization)||0);
    const pct5h=document.getElementById('tools-usage-5h-pct');
    const pct7d=document.getElementById('tools-usage-7d-pct');
    if(pct5h)pct5h.textContent=fhPct+'%';
    if(pct7d)pct7d.textContent=sdPct+'%';
    const fhTitle='Anthropic 5-hour limit · '+fhPct+'% used · resets '+_fmtResetTime(fh.resets_at);
    const sdTitle='Anthropic 7-day limit · '+sdPct+'% used · resets '+_fmtResetTime(sd.resets_at);
    const fhWrap=document.getElementById('nav-usage-5h-wrap');
    const sdWrap=document.getElementById('nav-usage-7d-wrap');
    if(fhWrap)fhWrap.title=fhTitle;
    if(sdWrap)sdWrap.title=sdTitle;
    const tfh=document.getElementById('tools-usage-5h-wrap');
    const tsd=document.getElementById('tools-usage-7d-wrap');
    if(tfh)tfh.title=fhTitle;
    if(tsd)tsd.title=sdTitle;
  }catch(e){
    /* keep last known display */
  }
}
function startUsageLimitsPolling(){
  if(_usageLimitsTimer)clearInterval(_usageLimitsTimer);
  refreshUsageLimits();
  // Poll every hour (3600s) while page is open — backend caches upstream calls
  _usageLimitsTimer=setInterval(refreshUsageLimits,3600*1000);
}
startUsageLimitsPolling();

// ── Login health: detect sessions whose Claude is on a previous login ──
let _loginHealth={account:null,stale_count:0,sessions:[]};
let _loginHealthTimer=null;
async function refreshLoginHealth(){
  try{
    const resp=await fetch(BASE+'/api/login-health');
    if(!resp.ok)return;
    _loginHealth=await resp.json();
  }catch(e){return}
  renderLoginHealth();
  try{renderAuthIndicator();}catch(e){}
}
function renderLoginHealth(){
  const lh=_loginHealth||{};
  const acct=lh.account||{};
  const byName={};(lh.sessions||[]).forEach(s=>byName[s.name]=s);
  document.querySelectorAll('[id^="login-health-"]').forEach(el=>{
    const name=el.id.slice('login-health-'.length);
    const s=byName[name];
    if(s&&s.stale){
      const curAcct=acct.email?(esc(acct.email)+(acct.plan?' · '+esc(acct.plan):'')):'the current login';
      el.innerHTML='<div style="margin-top:12px;background:#3d1d1d;border:1px solid #f85149;border-radius:8px;padding:10px 12px;color:#ffdcd6;font-size:.82rem;display:flex;flex-direction:column;gap:8px">'
        +'<span>⚠ This session\'s Claude started on a <b>previous login</b> and is still using it. Its in-terminal 5-hour usage bar reflects that older account — not the current login ('+curAcct+'). Restart to move it onto the current account (the conversation is preserved via --continue).</span>'
        +'<button onclick="reloginSession(\''+esc(name)+'\')" style="align-self:flex-start;background:#da3633;color:#fff;border:none;border-radius:6px;padding:6px 12px;font-size:.8rem;cursor:pointer">Restart Claude on current login</button>'
        +'</div>';
    }else{
      el.innerHTML='';
    }
  });
}
async function reloginSession(name){
  if(!confirm('Restart Claude in "'+name+'" on the current login?\n\nIt will exit and relaunch with --continue to preserve the conversation.'))return;
  try{
    const resp=await fetch(BASE+'/api/sessions/'+encodeURIComponent(name)+'/relogin',{method:'POST'});
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to restart');return}
    const el=document.getElementById('login-health-'+name);
    if(el)el.innerHTML='<div style="margin-top:12px;color:#8b949e;font-size:.82rem">Restarting Claude on the current login…</div>';
    setTimeout(refreshLoginHealth,9000);
  }catch(e){alert('Failed to restart Claude.')}
}
function startLoginHealthPolling(){
  if(_loginHealthTimer)clearInterval(_loginHealthTimer);
  refreshLoginHealth();
  _loginHealthTimer=setInterval(refreshLoginHealth,60000);
}
startLoginHealthPolling();

function closeModal(){document.getElementById('modal-overlay').classList.remove('active');const c=document.getElementById('modal-content');if(c)c.classList.remove('modal-wide')}

function showCreateModal(){
  const modal=document.getElementById('modal-content');
  // Members get a name pre-filled with 5 random chars (overridable); admins blank.
  const pre = NEMO_SIMPLE ? _randName(5) : '';
  modal.innerHTML=`
    <h3>New __BRAND__ session</h3>
    <p>${NEMO_SIMPLE ? 'A name is pre-filled — keep it or type your own.' : 'Leave blank for an auto-assigned name, or enter a custom name.'}</p>
    <input type="text" class="modal-input" id="new-session-name" value="${pre}"
      placeholder="e.g. my-project" autocomplete="off" spellcheck="false"
      onkeydown="if(event.key==='Enter')createSession()">
    <div class="modal-actions">
      <button class="modal-cancel" onclick="closeModal()">Cancel</button>
      <button class="modal-confirm-create" id="create-session-btn" onclick="createSession()">Create</button>
    </div>`;
  document.getElementById('modal-overlay').classList.add('active');
  setTimeout(()=>{const i=document.getElementById('new-session-name');if(i){i.focus();i.select();}},50);
}

async function createSession(){
  const input=document.getElementById('new-session-name');
  const name=input?input.value.trim():'';
  const modal=document.getElementById('modal-content');
  // Immediately show a loading state so it never looks frozen (the first session
  // for a member can take a few seconds to provision).
  if(modal){
    modal.innerHTML=`<h3>Creating session…</h3>
      <p class="conn-note">Setting up${name?(' "'+esc(name)+'"'):' your session'}. The first one can take a few seconds — hang tight.</p>
      <div class="create-spinner"></div>`;
  }
  try{
    const resp=await fetch(BASE+'/api/sessions/create',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name})
    });
    const data=await resp.json();
    if(!resp.ok){
      if(modal){modal.innerHTML=`<h3>Couldn't create session</h3>
        <p class="conn-note">${esc(data.error||'Failed')}</p>
        <div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button>
        <button class="modal-confirm-create" onclick="showCreateModal()">Try again</button></div>`;}
      return;
    }
    selectedSession=data.name;
    closeModal();
    await loadAll();
  }catch(e){
    if(modal){modal.innerHTML=`<h3>Couldn't create session</h3>
      <p class="conn-note">Network error — please try again.</p>
      <div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;}
  }
}

function _randName(n){const c='abcdefghijklmnopqrstuvwxyz0123456789';let s='';for(let i=0;i<n;i++)s+=c[Math.floor(Math.random()*c.length)];return s;}
async function createSessionAuto(){
  for(let attempt=0;attempt<5;attempt++){
    const name=_randName(5);
    try{
      const resp=await fetch(BASE+'/api/sessions/create',{
        method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
      const data=await resp.json();
      if(resp.ok){selectedSession=data.name;await loadAll();return}
      if(resp.status!==409){alert(data.error||'Failed');return}  // collision -> retry
    }catch(e){alert('Failed to create session.');return}
  }
  alert('Could not create a session — please try again.');
}

// ── Connections (Drive / Gmail / Calendar) ──
async function openConnections(){
  const modal=document.getElementById('modal-content');
  modal.innerHTML=`<h3>Connect data sources</h3><p class="conn-note">Loading…</p>`;
  document.getElementById('modal-overlay').classList.add('active');
  try{
    const r=await fetch(BASE+'/api/connections'); const d=await r.json();
    const icons={drive:'\u{1F4C1}',gmail:'✉️',calendar:'\u{1F4C5}'};
    const rows=(d.services||[]).map(s=>`<div class="conn-row">
        <span class="conn-name"><span class="conn-ico">${icons[s.id]||'\u{1F517}'}</span>${esc(s.label)}</span>
        <span>${s.connected
          ?`<span class="conn-status">● Connected</span><button class="conn-btn disconnect" onclick="disconnectService('${s.id}')">Disconnect</button>`
          :`<button class="conn-btn" ${d.configured?'':'disabled title=\"Admin must configure Google OAuth first\"'} onclick="connectService('${s.id}')">Connect</button>`}</span>
      </div>`).join('');
    const note = !d.configured
      ? 'Google connections aren’t configured yet — ask the admin to add the OAuth client.'
      : (d.mcp_ready ? '' : 'Connected accounts are saved securely; Claude’s live access turns on once the admin enables the Google tool.');
    modal.innerHTML=`<h3>Connect data sources</h3>
      <p class="conn-note">Give Claude access to your own Google Drive, Gmail, and Calendar in your sessions.</p>
      ${rows}${note?`<p class="conn-note">${note}</p>`:''}
      <div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;
  }catch(e){
    modal.innerHTML=`<h3>Connect data sources</h3><p class="conn-note">Failed to load.</p>
      <div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;
  }
}
function connectService(svc){ window.location.href=BASE+'/api/connections/'+svc+'/start'; }
async function disconnectService(svc){
  try{ await fetch(BASE+'/api/connections/'+svc,{method:'DELETE'}); }catch(e){}
  openConnections();
}

// ── Approvals (admin: review blocked cross-server actions) ──
async function openApprovals(){
  const modal=document.getElementById('modal-content');
  modal.innerHTML=`<h3>Approvals</h3><p class="conn-note">Loading…</p>`;
  document.getElementById('modal-overlay').classList.add('active');
  try{
    const r=await fetch(BASE+'/api/approvals'); const d=await r.json();
    const reqs=d.requests||[];
    if(!reqs.length){
      modal.innerHTML=`<h3>Approvals</h3><p class="conn-note">No cross-server requests.</p>
        <div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;
      return;
    }
    const rows=reqs.map(x=>{
      const cls={pending:'pill-pending',approved:'pill-approved',denied:'pill-denied'}[x.status]||'';
      const act=x.status==='pending'
        ?`<div class="approval-actions"><button class="btn-approve" onclick="decideApproval('${x.key}','approve')">Approve (1h)</button><button class="btn-deny" onclick="decideApproval('${x.key}','deny')">Deny</button></div>`:'';
      return `<div class="approval-row"><div class="approval-meta"><b>${esc(x.username||x.user_id||'?')}</b> &middot; <span class="${cls}">${esc(x.status)}</span> &middot; ${esc(x.cwd||'')}</div><pre>${esc(x.command||'')}</pre>${act}</div>`;
    }).join('');
    modal.innerHTML=`<h3>Approvals</h3>
      <p class="conn-note">Cross-server actions a member's Claude tried and the sandbox blocked. Approving allows that exact command for ~1 hour.</p>
      ${rows}<div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;
  }catch(e){
    modal.innerHTML=`<h3>Approvals</h3><p class="conn-note">Failed to load.</p>
      <div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;
  }
}
async function decideApproval(key,action){
  try{ await fetch(BASE+'/api/approvals/'+key+'/'+action,{method:'POST'}); }catch(e){}
  refreshApprovalsBadge(); openApprovals();
}

// ── Global context (admin) ──
async function openGlobalContext(){
  const modal=document.getElementById('modal-content');
  modal.innerHTML=`<h3>Global Context</h3><p class="conn-note">Loading…</p>`;
  document.getElementById('modal-overlay').classList.add('active');
  try{
    const r=await fetch(BASE+'/api/global-context'); const d=await r.json();
    modal.innerHTML=`<h3>Global Context — every member's Claude</h3>
      <p class="conn-note">Prepended as a managed block to each member's CLAUDE.md (their own notes &amp; memory stay below it). Edit the company rules / sandbox policy here.</p>
      <textarea id="gctx-ta" class="modal-input" style="height:320px;font-family:monospace;white-space:pre;width:100%">${esc(d.content||'')}</textarea>
      <div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Cancel</button>
      <button class="modal-confirm-create" onclick="saveGlobalContext()">Save &amp; sync</button></div>`;
  }catch(e){
    modal.innerHTML=`<h3>Global Context</h3><p class="conn-note">Failed to load.</p>
      <div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;
  }
}
async function saveGlobalContext(){
  const ta=document.getElementById('gctx-ta'); if(!ta) return;
  try{
    const r=await fetch(BASE+'/api/global-context',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({content:ta.value})});
    const d=await r.json();
    if(r.ok){ closeModal(); alert('Saved & synced to '+(d.synced||0)+' member(s).'); }
    else alert(d.error||'Failed');
  }catch(e){ alert('Failed to save.'); }
}

// Surface a one-time toast after returning from a Google OAuth connect flow.
(function(){
  try{
    const q=new URLSearchParams(window.location.search);
    if(q.has('connect')){
      const st=q.get('connect');
      const msg=st==='ok'?'Connected '+(q.get('svc')||'service')+' ✓':(st==='denied'?'Connection cancelled.':'Connection failed.');
      setTimeout(()=>{ try{ alert(msg); }catch(e){} },300);
      q.delete('connect'); q.delete('svc');
      const url=window.location.pathname+(q.toString()?('?'+q.toString()):'');
      window.history.replaceState({},'',url);
    }
  }catch(e){}
})();

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
    const plan=_authCache.plan||(_authCache.subscriptionType?_authCache.subscriptionType.charAt(0).toUpperCase()+_authCache.subscriptionType.slice(1):'');
    const stale=(_loginHealth&&_loginHealth.stale_count)||0;
    label.textContent=(stale>0?'⚠ '+stale+' on old login · ':'')+email+(plan?' · '+plan:'');
    if(stale>0){dot.className='status-dot';dot.style.background='#f85149';}
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
    const planLabel=_authCache.plan||(plan.charAt(0).toUpperCase()+plan.slice(1));
    el.innerHTML=`
      <div class="auth-title">Claude Code Connected</div>
      <div class="auth-row"><span class="auth-row-label">Email</span><span class="auth-row-value">${esc(_authCache.email||'—')}</span></div>
      <div class="auth-row"><span class="auth-row-label">Plan</span><span class="auth-row-value"><span class="auth-plan-badge ${planClass}">${esc(planLabel)}</span></span></div>
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

// ── Auto-push (auto-responder + autopilot watchdog) ──
let _watchdogTimers={};
const AUTOPUSH_TITLES={
  off:'Off — the dashboard never types into this terminal',
  basic:"Basic — auto-pick option menus + confirm permission/plan prompts (Enter), keep logged in",
  full:'Full — Basic, plus write a "keep going" instruction when Claude pauses before finishing'
};
function autopushDesc(mode){
  return ({off:'Off — no auto-typing',
           basic:'Basic — picks options + confirms prompts',
           full:'Full — options, confirms + keep-going nudges'})[mode||'basic'];
}
// Build the 3-way segmented control. `compact` shrinks it for the More dropdown.
function autopushSeg(name,mode,compact){
  mode=mode||'basic';
  const b=(m,label)=>'<button type="button" class="ap-'+m+(mode===m?' active':'')+'" title="'+
    esc(AUTOPUSH_TITLES[m])+'" onclick="event.stopPropagation();setAutopush(\''+
    esc(name).replace(/'/g,"\\'")+'\',\''+m+'\')">'+label+'</button>';
  return '<div class="autopush-seg'+(compact?' compact':'')+'" data-name="'+esc(name)+'">'+
    b('off','Off')+b('basic','Basic')+b('full','Full')+'</div>';
}
// Reflect a mode across every rendered control for this session (More menu + Info tab).
function syncAutopushUI(name,mode){
  mode=mode||'basic';
  document.querySelectorAll('.autopush-seg').forEach(seg=>{
    if(seg.dataset.name!==name)return;
    seg.querySelectorAll('button').forEach(btn=>{
      btn.classList.toggle('active',btn.classList.contains('ap-'+mode));
    });
  });
  const st=document.getElementById('autopush-status-'+name);
  if(st)st.textContent=autopushDesc(mode);
}
async function setAutopush(name,mode){
  const idx=sessions.findIndex(s=>s.name===name);
  const prev=(idx>=0?sessions[idx].autopush_mode:'basic')||'basic';
  if(mode===prev){syncAutopushUI(name,mode);return;}
  if(idx>=0)sessions[idx].autopush_mode=mode;   // optimistic
  syncAutopushUI(name,mode);
  try{
    const resp=await fetch(BASE+'/api/sessions/'+encodeURIComponent(name)+'/autopush',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({mode:mode})
    });
    const data=await resp.json();
    if(resp.ok&&data.mode){
      if(idx>=0)sessions[idx].autopush_mode=data.mode;
      syncAutopushUI(name,data.mode);
      renderWatchdogLog(name,data.log);
      if(data.mode==='full')startWatchdogPolling(name);
      else stopWatchdogPolling(name);
    }else{
      if(idx>=0)sessions[idx].autopush_mode=prev;
      syncAutopushUI(name,prev);
    }
  }catch(e){
    if(idx>=0)sessions[idx].autopush_mode=prev;
    syncAutopushUI(name,prev);
  }
}
function renderWatchdogLog(name,log){
  const logEl=document.getElementById('watchdog-log-'+name);
  if(logEl&&log&&log.length){
    logEl.innerHTML=log.slice(-10).map(e=>
      '<div class="watchdog-log-entry"><span class="watchdog-ts">['+new Date(e.ts*1000).toLocaleTimeString()+']</span> '+esc(e.action)+'</div>'
    ).join('');
    logEl.scrollTop=logEl.scrollHeight;
  }
}
function startWatchdogPolling(name){
  stopWatchdogPolling(name);
  loadWatchdogStatus(name);
  _watchdogTimers[name]=setInterval(()=>loadWatchdogStatus(name),15000);
}
function stopWatchdogPolling(name){
  if(_watchdogTimers[name]){clearInterval(_watchdogTimers[name]);delete _watchdogTimers[name];}
}
function stopAllWatchdogPolling(){
  Object.keys(_watchdogTimers).forEach(n=>stopWatchdogPolling(n));
}
async function loadWatchdogStatus(name){
  try{
    const resp=await fetch(BASE+'/api/sessions/'+encodeURIComponent(name)+'/autopush');
    const data=await resp.json();
    const idx=sessions.findIndex(s=>s.name===name);
    if(idx>=0)sessions[idx].autopush_mode=data.mode;
    syncAutopushUI(name,data.mode);
    renderWatchdogLog(name,data.log);
  }catch(e){}
}

// ── Key Bar + Slash Commands ──
function buildKeyBar(name,tab){
  // Simplified team members: no keys/commands — a COMPACT single-row upload
  // control. Keeping the footer short means the terminal gets full height and the
  // page doesn't overflow (page overflow would steal the terminal's scroll).
  if(NEMO_SIMPLE){
    return `<div class="key-bar expanded nemo-upload-bar" id="keybar-${tab}-${name}" style="border-top:none">
    <button class="key-btn" onclick="_uploadTab['${name}']='${tab}';document.getElementById('upload-${tab==='raw'?'raw-':''}${name}').click()" title="Upload file">&#x1F4CE; Upload</button>
    <div class="nemo-drop" id="dropzone-${tab}-${name}"
      ondragover="event.preventDefault();this.classList.add('drag-over')"
      ondragleave="this.classList.remove('drag-over')"
      ondrop="handleDrop(event,'${name}','${tab}')"
      onclick="_uploadTab['${name}']='${tab}';document.getElementById('upload-${tab==='raw'?'raw-':''}${name}').click()">or drop files here</div>
    <div class="upload-progress" id="upload-progress-${tab}-${name}">
      <div class="upload-progress-filename" id="upload-progress-name-${tab}-${name}"></div>
      <div class="upload-progress-bar"><div class="upload-progress-fill" id="upload-progress-fill-${tab}-${name}"></div></div>
    </div>
    <div class="uploaded-files" id="uploaded-files-${tab}-${name}"></div>
  </div>`;
  }
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
    <div class="uploaded-files" id="uploaded-files-${tab}-${name}"></div>
  </div>`;
}

function _formatUploadSize(bytes){
  if(bytes<1024)return bytes+' B';
  if(bytes<1024*1024)return (bytes/1024).toFixed(1)+' KB';
  return (bytes/(1024*1024)).toFixed(1)+' MB';
}

async function refreshUploadedFiles(name){
  let files=[];
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/uploads');
    if(resp.ok){const data=await resp.json();files=data.files||[]}
  }catch(e){return}
  ['chat','raw'].forEach(function(tab){
    const container=document.getElementById('uploaded-files-'+tab+'-'+name);
    if(!container)return;
    // Wipe + rebuild via DOM APIs (not innerHTML string interpolation). Inline
    // onclick attribute strings were silently failing for some users; direct
    // addEventListener bindings are immune to any escaping or bubbling issues.
    container.replaceChildren();
    if(!files.length)return;
    const label=document.createElement('div');
    label.className='uploaded-files-label';
    label.textContent='Uploaded files';
    container.appendChild(label);
    files.forEach(function(f){
      const row=document.createElement('div');
      row.className='uploaded-file';
      row.title=f.path||'';

      const icon=document.createElement('span');
      icon.className='uploaded-file-icon';
      icon.textContent='\u{1F4C4}';
      row.appendChild(icon);

      const nameSpan=document.createElement('span');
      nameSpan.className='uploaded-file-name';
      nameSpan.textContent=f.name;
      row.appendChild(nameSpan);

      const sizeSpan=document.createElement('span');
      sizeSpan.className='uploaded-file-size';
      sizeSpan.textContent=_formatUploadSize(f.size);
      row.appendChild(sizeSpan);

      const copyBtn=document.createElement('button');
      copyBtn.type='button';
      copyBtn.className='uploaded-file-btn';
      copyBtn.title='Copy path';
      copyBtn.textContent='Copy path';
      copyBtn.addEventListener('click',function(ev){
        ev.preventDefault();
        ev.stopPropagation();
        copyUploadPath(copyBtn,encodeURIComponent(f.path));
      });
      row.appendChild(copyBtn);

      const delBtn=document.createElement('button');
      delBtn.type='button';
      delBtn.className='uploaded-file-btn delete';
      delBtn.title='Remove';
      delBtn.textContent='✕';
      delBtn.addEventListener('click',function(ev){
        ev.preventDefault();
        ev.stopPropagation();
        // Immediate visual feedback so the user knows the click registered
        delBtn.disabled=true;
        delBtn.textContent='…';
        deleteUploadedFile(name,encodeURIComponent(f.name));
      });
      row.appendChild(delBtn);

      container.appendChild(row);
    });
  });
}

function copyUploadPath(btn,encodedPath){
  const path=decodeURIComponent(encodedPath);
  const done=function(){
    const orig=btn.textContent;
    btn.textContent='Copied';
    btn.classList.add('copied');
    setTimeout(function(){btn.textContent=orig;btn.classList.remove('copied')},1200);
  };
  if(navigator.clipboard&&navigator.clipboard.writeText){
    navigator.clipboard.writeText(path).then(done).catch(function(){
      const ta=document.createElement('textarea');ta.value=path;document.body.appendChild(ta);ta.select();
      try{document.execCommand('copy')}catch(e){}
      document.body.removeChild(ta);done();
    });
  }else{
    const ta=document.createElement('textarea');ta.value=path;document.body.appendChild(ta);ta.select();
    try{document.execCommand('copy')}catch(e){}
    document.body.removeChild(ta);done();
  }
}

async function deleteUploadedFile(name,encodedFilename){
  const filename=decodeURIComponent(encodedFilename);
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/uploads/'+encodeURIComponent(filename),{method:'DELETE'});
    if(!resp.ok){
      let msg='Delete failed ('+resp.status+')';
      try{const d=await resp.json();if(d&&d.error)msg=d.error}catch(_){}
      console.warn('deleteUploadedFile:',msg);
      appendChatBubble(name,'assistant','Failed to delete '+filename+': '+msg,Date.now()/1000);
    }
  }catch(e){
    console.warn('deleteUploadedFile error:',e);
    appendChatBubble(name,'assistant','Failed to delete '+filename+': network error',Date.now()/1000);
  }
  refreshUploadedFiles(name);
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
// Per-profile skills UI:
//   • Active section: what Claude actually loads from ~/.claude-<profile>/skills/
//   • Library section: toggle-on/off canonical skills from ~/.tmux-dashboard/skill-library/
//     (enabling = symlink into the profile's skills dir; disabling = unlink)
//   • Built-in section: read-only list of skills bundled with Claude Code
//
// The session's profile_id (set per tmux session) determines the scope.
let _builtinSkillsCache=null;
let _editingSkillSession=null;
let _editingSkillName=null;

function _profileForSession(name){
  const s=sessions.find(x=>x.name===name);
  return (s&&s.profile_id)||'default';
}

async function loadProfileSkills(name){
  const meta=document.getElementById('skills-meta-'+name);
  const activeEl=document.getElementById('skills-active-'+name);
  const libEl=document.getElementById('skills-library-'+name);
  const builtinEl=document.getElementById('skills-builtin-'+name);
  if(!activeEl||!libEl||!builtinEl)return;
  const pid=_profileForSession(name);
  if(meta)meta.innerHTML='Profile: <code>'+esc(pid)+'</code>';
  activeEl.innerHTML='<div class="skills-empty">Loading...</div>';
  libEl.innerHTML='<div class="skills-empty">Loading...</div>';
  builtinEl.innerHTML='<div class="skills-empty">Loading...</div>';
  try{
    const [activeResp,libResp,builtinResp]=await Promise.all([
      fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/skills'),
      fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/skills/library'),
      _builtinSkillsCache?Promise.resolve({ok:true,json:()=>Promise.resolve({skills:_builtinSkillsCache})}):fetch(BASE+'/api/builtin-skills'),
    ]);
    const activeData=await activeResp.json();
    const libData=await libResp.json();
    const builtinData=await builtinResp.json();
    if(builtinData&&builtinData.skills)_builtinSkillsCache=builtinData.skills;
    _renderActiveSkills(name,pid,activeData);
    _renderLibrarySkills(name,pid,libData);
    _renderBuiltinSkills(name,builtinData.skills||[]);
  }catch(e){
    activeEl.innerHTML='<div class="skills-empty" style="color:#f85149">Failed to load.</div>';
    libEl.innerHTML='<div class="skills-empty" style="color:#f85149">Failed to load.</div>';
    builtinEl.innerHTML='<div class="skills-empty" style="color:#f85149">Failed to load.</div>';
  }
}

function _renderActiveSkills(name,pid,data){
  const el=document.getElementById('skills-active-'+name);
  if(!el)return;
  const skills=(data&&data.skills)||[];
  const legacy=(data&&data.legacy_files)||[];
  const rows=[];
  if(!skills.length&&!legacy.length){
    el.innerHTML='<div class="skills-empty">No skills installed for this profile. Enable some from the Library below.</div>';
    return;
  }
  for(const sk of skills){
    const tag=sk.from_library?'library link':'custom (in-profile)';
    const nameClass=sk.from_library?'':'custom-name';
    const promoteBtn=sk.from_library?'':`
      <button class="btn" onclick="promoteProfileSkill('${esc(name)}','${esc(sk.dir_name)}')" title="Move this skill to the shared library so other profiles can enable it">Promote</button>`;
    const removeLabel=sk.from_library?'Disable':'Delete';
    const removeTitle=sk.from_library
      ? 'Disable this library skill in this profile (the library copy is kept).'
      : 'Delete this custom skill from this profile.';
    rows.push(`
      <div class="skills-row">
        <div class="skills-row-body">
          <div class="skills-row-name ${nameClass}">${esc(sk.name)}</div>
          <div class="skills-row-desc">${esc(sk.description||'(no description in frontmatter)')}</div>
          <div class="skills-row-tags">${esc(tag)} · ${esc(sk.dir_name)}/SKILL.md</div>
        </div>
        <div class="skills-row-actions">
          ${promoteBtn}
          <button class="btn btn-danger" onclick="removeProfileSkill('${esc(name)}','${esc(sk.dir_name)}',${sk.from_library?'true':'false'})" title="${esc(removeTitle)}">${removeLabel}</button>
        </div>
      </div>`);
  }
  for(const fname of legacy){
    rows.push(`
      <div class="skills-row disabled-row">
        <div class="skills-row-body">
          <div class="skills-row-name custom-name">${esc(fname)}</div>
          <div class="skills-row-desc">Legacy flat file. Claude Code does not load this format — wrap it in a <code>${esc(fname.replace(/\.md$/,''))}/SKILL.md</code> directory with frontmatter to load.</div>
        </div>
      </div>`);
  }
  el.innerHTML=rows.join('');
}

function _renderLibrarySkills(name,pid,data){
  const el=document.getElementById('skills-library-'+name);
  if(!el)return;
  const skills=(data&&data.skills)||[];
  const isDefault=!!(data&&data.default_profile);
  if(!skills.length){
    el.innerHTML='<div class="skills-empty">No skills in the library yet. Click "+ New Library Skill" to create one.</div>';
    return;
  }
  el.innerHTML=skills.map(sk=>{
    return `
      <div class="skills-row${sk.enabled?'':' disabled-row'}">
        <div class="skills-row-toggle"><input type="checkbox" ${sk.enabled?'checked':''} onchange="toggleLibrarySkill('${esc(name)}','${esc(sk.dir_name)}',this.checked)"></div>
        <div class="skills-row-body">
          <div class="skills-row-name">${esc(sk.name)}</div>
          <div class="skills-row-desc">${esc(sk.description||'(no description)')}</div>
          <div class="skills-row-tags">${esc(sk.dir_name)}/SKILL.md ${sk.enabled?'· enabled':'· disabled'}</div>
        </div>
        <div class="skills-row-actions">
          <button class="btn" onclick="editLibrarySkill('${esc(name)}','${esc(sk.dir_name)}')">Edit</button>
          <button class="btn btn-danger" onclick="deleteLibrarySkill('${esc(name)}','${esc(sk.dir_name)}')">Del</button>
        </div>
      </div>`;
  }).join('');
}

function _renderBuiltinSkills(name,skills){
  const el=document.getElementById('skills-builtin-'+name);
  if(!el)return;
  if(!skills.length){
    el.innerHTML='<div class="skills-empty">No built-in skills reported.</div>';
    return;
  }
  el.innerHTML=skills.map(sk=>`
    <div class="skills-row">
      <div class="skills-row-body">
        <div class="skills-row-name readonly-name">${esc(sk.name)}</div>
        <div class="skills-row-desc">${esc(sk.description||'')}</div>
        <div class="skills-row-tags">bundled · always available</div>
      </div>
    </div>`).join('');
}

async function toggleLibrarySkill(sessionName,skillDirName,enable){
  const pid=_profileForSession(sessionName);
  const url=BASE+'/api/profiles/'+encodeURIComponent(pid)+'/skills/library/'+encodeURIComponent(skillDirName);
  try{
    const resp=await fetch(url,{method:enable?'POST':'DELETE'});
    const data=await resp.json();
    if(!resp.ok){
      alert(data.error||'Failed to toggle skill');
    }
  }catch(e){
    alert('Failed to toggle skill.');
  }
  loadProfileSkills(sessionName);
}

async function promoteProfileSkill(sessionName, skillDirName){
  const pid=_profileForSession(sessionName);
  if(!confirm('Promote "'+skillDirName+'" to the shared library?\n\n' +
              'It will move to ~/.tmux-dashboard/skill-library/ and the current profile ' +
              'will keep it active via a symlink. Other profiles can then toggle it on ' +
              'from their Library list.')) return;
  try{
    const resp=await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/skills/'+encodeURIComponent(skillDirName)+'/promote', {method:'POST'});
    const data=await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to promote skill'); return; }
  }catch(e){ alert('Failed to promote skill.'); }
  loadProfileSkills(sessionName);
}

async function removeProfileSkill(sessionName, skillDirName, fromLibrary){
  const pid=_profileForSession(sessionName);
  const verb=fromLibrary?'Disable':'Delete';
  const desc=fromLibrary
    ? 'This removes the symlink from this profile. The library copy is kept and can be re-enabled later.'
    : 'This permanently deletes '+skillDirName+'/SKILL.md from this profile. If you want to keep it for other profiles, click Promote first.';
  if(!confirm(verb+' "'+skillDirName+'" in profile "'+pid+'"?\n\n'+desc)) return;
  try{
    let url;
    if(fromLibrary){
      url=BASE+'/api/profiles/'+encodeURIComponent(pid)+'/skills/library/'+encodeURIComponent(skillDirName);
    }else{
      url=BASE+'/api/profiles/'+encodeURIComponent(pid)+'/skills/'+encodeURIComponent(skillDirName);
    }
    const resp=await fetch(url,{method:'DELETE'});
    const data=await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to '+verb.toLowerCase()+' skill'); return; }
  }catch(e){ alert('Failed to '+verb.toLowerCase()+' skill.'); }
  loadProfileSkills(sessionName);
}

function newLibrarySkill(sessionName){
  _editingSkillSession=sessionName;
  _editingSkillName=null;
  const wrap=document.getElementById('skills-editor-wrap-'+sessionName);
  const fnameEl=document.getElementById('skills-filename-'+sessionName);
  const descEl=document.getElementById('skills-description-'+sessionName);
  const editorEl=document.getElementById('skills-editor-'+sessionName);
  if(!wrap||!fnameEl||!editorEl)return;
  fnameEl.value='';
  fnameEl.disabled=false;
  if(descEl)descEl.value='';
  editorEl.value='# My new skill\n\nDescribe what this skill does and when to use it.\n';
  wrap.style.display='flex';
  fnameEl.focus();
}

async function editLibrarySkill(sessionName,skillDirName){
  _editingSkillSession=sessionName;
  _editingSkillName=skillDirName;
  const wrap=document.getElementById('skills-editor-wrap-'+sessionName);
  const fnameEl=document.getElementById('skills-filename-'+sessionName);
  const descEl=document.getElementById('skills-description-'+sessionName);
  const editorEl=document.getElementById('skills-editor-'+sessionName);
  if(!wrap||!fnameEl||!editorEl)return;
  try{
    const resp=await fetch(BASE+'/api/skill-library/'+encodeURIComponent(skillDirName));
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to load skill');return}
    fnameEl.value=data.dir_name||skillDirName;
    fnameEl.disabled=true;  // can't rename via this editor
    if(descEl)descEl.value=data.description||'';
    // Strip frontmatter from content for display so the user edits the body only
    let body=data.content||'';
    if(body.startsWith('---')){
      const end=body.indexOf('\n---',3);
      if(end!==-1){
        body=body.slice(end+4).replace(/^\s*\n/,'');
      }
    }
    editorEl.value=body;
    wrap.style.display='flex';
    editorEl.focus();
  }catch(e){alert('Failed to load skill for editing.')}
}

async function saveLibrarySkill(sessionName){
  const fnameEl=document.getElementById('skills-filename-'+sessionName);
  const descEl=document.getElementById('skills-description-'+sessionName);
  const editorEl=document.getElementById('skills-editor-'+sessionName);
  if(!fnameEl||!editorEl)return;
  let skillName=(_editingSkillName||fnameEl.value||'').trim();
  if(!skillName){alert('Please enter a skill name (e.g. my-skill).');return}
  // Strip .md if user typed it; sanitize to allowed chars (server validates too)
  if(skillName.endsWith('.md'))skillName=skillName.slice(0,-3);
  if(!/^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$/.test(skillName)){
    alert('Skill name must be alphanumeric, hyphens, or underscores (max 64 chars).');
    return;
  }
  const description=descEl?descEl.value.trim():'';
  if(!description){
    if(!confirm('No description set. Claude Code uses the description to discover skills — without one, this skill may never trigger automatically. Save anyway?'))return;
  }
  try{
    const resp=await fetch(BASE+'/api/skill-library/'+encodeURIComponent(skillName),{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({description:description,content:editorEl.value})
    });
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to save');return}
    closeSkillEditor(sessionName);
    loadProfileSkills(sessionName);
  }catch(e){alert('Failed to save skill.')}
}

async function deleteLibrarySkill(sessionName,skillDirName){
  if(!confirm('Delete library skill "'+skillDirName+'"?\nThis removes it from EVERY profile (any symlinks pointing to it will also be removed).'))return;
  try{
    const resp=await fetch(BASE+'/api/skill-library/'+encodeURIComponent(skillDirName),{method:'DELETE'});
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to delete');return}
    loadProfileSkills(sessionName);
  }catch(e){alert('Failed to delete skill.')}
}

function closeSkillEditor(name){
  const wrap=document.getElementById('skills-editor-wrap-'+name);
  if(wrap)wrap.style.display='none';
  _editingSkillSession=null;
  _editingSkillName=null;
}

// ── Claude profiles ──
let _profilesCache = null;        // [{id,name,model,effort,builtin}, ...]
let _profilesEditing = null;       // currently-edited full profile object
let _profilePending = {};          // sessionName -> "pending restart" flag

async function loadProfiles(force){
  if(NEMO_SIMPLE){ _profilesCache=[]; return _profilesCache; }  // members have no profiles
  if(_profilesCache && !force) return _profilesCache;
  try{
    const resp = await fetch(BASE+'/api/profiles');
    const data = await resp.json();
    _profilesCache = data.profiles || [];
  }catch(e){ _profilesCache = []; }
  return _profilesCache;
}

function renderProfileDropdown(s){
  if(NEMO_SIMPLE) return '';  // members have no profile selector
  const cur = s.profile_id || 'default';
  const list = _profilesCache || [];
  const opts = list.length
    ? list.map(p=>`<option value="${esc(p.id)}" ${p.id===cur?'selected':''}>${esc(p.name)}</option>`).join('')
    : `<option value="default" selected>Default</option>`;
  const pending = _profilePending[s.name] ? ' pending' : '';
  const restartTitle = _profilePending[s.name]
    ? 'Restart Claude to apply the new profile'
    : 'Restart Claude with this profile';
  return `<div class="profile-wrap" title="Claude profile (CLAUDE_CONFIG_DIR)">
    <span class="profile-label">Profile</span>
    <select class="profile-select" id="profile-select-${esc(s.name)}" onchange="onProfileChange('${esc(s.name)}',this.value)">${opts}</select>
    <button class="profile-restart-btn${pending}" onclick="restartWithProfile('${esc(s.name)}')" title="${restartTitle}">↻</button>
  </div>`;
}

async function onProfileChange(sessionName, profileId){
  // First call: probe whether Claude is currently running in this session
  // (we pass restart:false so we get back the running state). If it IS running,
  // ask the user whether to restart now; otherwise the new profile won't take
  // effect until Claude is next launched and the user's memory will appear to
  // "spill" from the previous profile.
  try{
    let resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(sessionName)+'/profile', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({profile_id: profileId, restart: false})
    });
    let data = await resp.json();
    if(!resp.ok){alert(data.error||'Failed to set profile'); return;}
    const sess = sessions.find(x=>x.name===sessionName);
    if(sess) sess.profile_id = profileId;

    if(data.claude_was_running && !data.restarted){
      const wantRestart = confirm(
        'Profile switched to "' + profileId + '".\n\n' +
        'Claude is still running with the previous profile and will keep using it ' +
        '(including its memory) until you restart it.\n\n' +
        'Restart Claude now?'
      );
      if(wantRestart){
        resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(sessionName)+'/profile', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({profile_id: profileId, restart: true})
        });
        data = await resp.json();
        if(resp.ok && data.restarted){
          delete _profilePending[sessionName];
        }else{
          _profilePending[sessionName] = true;
        }
      }else{
        _profilePending[sessionName] = true;
      }
    }else{
      delete _profilePending[sessionName];
    }
    if(selectedSession===sessionName) renderDetail();
  }catch(e){ alert('Failed to set profile.'); }
}

async function restartWithProfile(sessionName){
  const sess = sessions.find(x=>x.name===sessionName);
  const pid = (sess && sess.profile_id) || 'default';
  if(!confirm('Exit Claude in "'+sessionName+'" and relaunch with profile "'+pid+'"? Any unsaved Claude state will be lost.')) return;
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(sessionName)+'/profile', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({profile_id: pid, restart: true})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to restart'); return; }
    delete _profilePending[sessionName];
    if(selectedSession===sessionName) renderDetail();
  }catch(e){ alert('Failed to restart Claude.'); }
}

// ── Current-user awareness ──────────────────────────────────────────────────
let _currentUser = null;

async function loadCurrentUser(){
  if(_currentUser) return _currentUser;
  try{
    const resp = await fetch(BASE+'/api/me');
    if(resp.ok){
      _currentUser = await resp.json();
    }
  }catch(e){ /* noop */ }
  return _currentUser;
}

async function applyRoleVisibility(){
  await loadCurrentUser();
  const isAdmin = !!(_currentUser && _currentUser.role === 'admin');
  NEMO_SIMPLE = !!(_currentUser && _currentUser.simple);
  document.body.classList.toggle('nemo-simple', NEMO_SIMPLE);
  document.body.classList.toggle('nemo-admin', isAdmin);
  document.querySelectorAll('.nav-tools-admin').forEach(el => {
    el.style.display = isAdmin ? '' : 'none';
  });
  const whoamiEl = document.getElementById('nav-tools-whoami');
  if(whoamiEl && _currentUser){
    const role = _currentUser.role==='admin' ? ' (admin)' : '';
    whoamiEl.textContent = 'Signed in as ' + (_currentUser.username||'?') + role;
  }
  renderImpersonationBanner();
  // Re-render session cards so simple-mode toggles (key bar, tabs) take effect now.
  try{ if(sessions && sessions.length){ renderNav(); renderDetail(); } }catch(e){}
  if(isAdmin){ refreshApprovalsBadge(); if(!window._apTimer) window._apTimer=setInterval(refreshApprovalsBadge,30000); }
}

async function refreshApprovalsBadge(){
  try{
    const r=await fetch(BASE+'/api/approvals'); if(!r.ok)return;
    const d=await r.json();
    const pending=(d.requests||[]).filter(x=>x.status==='pending').length;
    const b=document.getElementById('approvals-badge');
    if(b){ b.textContent=pending?String(pending):''; b.style.display=pending?'':'none'; }
  }catch(e){}
}

async function doLogout(){
  if(!confirm('Log out?')) return;
  try{
    const form = document.createElement('form');
    form.method = 'POST';
    form.action = BASE + '/logout';
    document.body.appendChild(form);
    form.submit();
  }catch(e){ alert('Logout failed'); }
}

// ── Settings modal (My Context / History / Users) ───────────────────────────
let _settingsActiveTab = 'mycontext';
let _settingsHistoryDetail = null; // {session_name, ...} or null when showing list

async function openSettings(tab){
  await loadCurrentUser();
  _settingsActiveTab = tab || 'mycontext';
  _settingsHistoryDetail = null;
  const overlay = document.getElementById('settings-overlay');
  overlay.classList.add('active');
  renderSettingsTabs();
  renderSettingsContent();
}

function closeSettings(){
  document.getElementById('settings-overlay').classList.remove('active');
  _settingsHistoryDetail = null;
}

// Admin shortcut: jump straight to the Users/Workspace management tab.
async function openTeamWorkspace(){
  await openSettings('users');
}

function renderSettingsTabs(){
  const tabsEl = document.getElementById('settings-tabs');
  const isAdmin = !!(_currentUser && _currentUser.role === 'admin');
  const tabs = [
    {id:'mycontext', label:'My Context'},
    {id:'history',   label:'History'},
  ];
  if(isAdmin) tabs.push({id:'users', label:'Users'});
  tabsEl.innerHTML = tabs.map(t =>
    `<div class="settings-tab${_settingsActiveTab===t.id?' active':''}" onclick="switchSettingsTab('${t.id}')">${t.label}</div>`
  ).join('');
}

function switchSettingsTab(tab){
  _settingsActiveTab = tab;
  _settingsHistoryDetail = null;
  renderSettingsTabs();
  renderSettingsContent();
}

function renderSettingsContent(){
  const el = document.getElementById('settings-content');
  if(_settingsActiveTab === 'mycontext'){
    el.innerHTML = '<div class="settings-section"><div class="pf-banner">Loading...</div></div>';
    loadMyContext();
  }else if(_settingsActiveTab === 'history'){
    if(_settingsHistoryDetail){
      renderHistoryDetail();
    }else{
      el.innerHTML = '<div class="settings-section"><div class="pf-banner">Loading your past sessions...</div></div>';
      loadHistory();
    }
  }else if(_settingsActiveTab === 'users'){
    el.innerHTML = '<div class="settings-section"><div class="pf-banner">Loading users...</div></div>';
    loadUsersAdmin();
  }
}

// --- My Context tab ---
async function loadMyContext(){
  let data;
  try{
    const resp = await fetch(BASE+'/api/my/context');
    data = await resp.json();
    if(!resp.ok){ throw new Error(data.error||'Failed to load context'); }
  }catch(e){
    document.getElementById('settings-content').innerHTML =
      '<div class="settings-section"><div class="pf-banner">Failed to load context: '+esc(e.message||e)+'</div></div>';
    return;
  }
  const files = {};
  (data.files||[]).forEach(f => { files[f.name] = f; });
  const claude = files['CLAUDE.md'] || {content:'', path:''};
  const memory = files['MEMORY.md'] || {content:'', path:''};
  const settings = files['settings.json'] || {content:'', path:''};
  const html = `
    <div class="settings-section">
      <div class="pf-banner">These files are read by Claude Code in <strong>every session you launch</strong> (set via <code>CLAUDE_CONFIG_DIR=${esc(data.dir||'')}</code>). They are private to your account.</div>
      <label>CLAUDE.md</label>
      <div class="my-ctx-path">${esc(claude.path||'')}</div>
      <textarea class="my-ctx-claude" id="my-ctx-claude" spellcheck="false">${esc(claude.content||'')}</textarea>
      <div class="my-ctx-actions"><button class="btn btn-full" onclick="saveMyContext('CLAUDE.md','my-ctx-claude')">Save CLAUDE.md</button></div>
      <label>MEMORY.md</label>
      <div class="my-ctx-path">${esc(memory.path||'')}</div>
      <textarea class="my-ctx-memory" id="my-ctx-memory" spellcheck="false">${esc(memory.content||'')}</textarea>
      <div class="my-ctx-actions"><button class="btn btn-full" onclick="saveMyContext('MEMORY.md','my-ctx-memory')">Save MEMORY.md</button></div>
      <label>settings.json</label>
      <div class="my-ctx-path">${esc(settings.path||'')}</div>
      <textarea class="my-ctx-settings" id="my-ctx-settings" spellcheck="false">${esc(settings.content||'')}</textarea>
      <div class="my-ctx-actions"><button class="btn btn-full" onclick="saveMyContext('settings.json','my-ctx-settings')">Save settings.json</button></div>
    </div>`;
  document.getElementById('settings-content').innerHTML = html;
}

async function saveMyContext(filename, textareaId){
  const ta = document.getElementById(textareaId);
  if(!ta) return;
  const content = ta.value;
  try{
    const resp = await fetch(BASE+'/api/my/context/'+encodeURIComponent(filename), {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({content})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Save failed'); return; }
    // Subtle confirmation via the path line
    const banner = ta.parentElement.querySelector('.my-ctx-path');
    if(banner){
      const orig = banner.textContent;
      banner.textContent = 'Saved ✓ — '+orig;
      setTimeout(() => { banner.textContent = orig; }, 2000);
    }
  }catch(e){ alert('Save failed: '+e.message); }
}

// --- History tab ---
async function loadHistory(){
  let data;
  try{
    const resp = await fetch(BASE+'/api/history');
    data = await resp.json();
    if(!resp.ok){ throw new Error(data.error||'Failed to load history'); }
  }catch(e){
    document.getElementById('settings-content').innerHTML =
      '<div class="settings-section"><div class="pf-banner">Failed to load history: '+esc(e.message||e)+'</div></div>';
    return;
  }
  const sessions = data.sessions || [];
  if(!sessions.length){
    document.getElementById('settings-content').innerHTML =
      '<div class="settings-section"><div class="history-empty">No past sessions yet. Create a session and send some messages — they will appear here.</div></div>';
    return;
  }
  const rows = sessions.map(s => {
    const ts = s.last_message_at ? timeAgo(s.last_message_at) : 'no activity';
    const livePill = s.is_live ? '<span class="history-row-pill">Live</span>' : '';
    const title = s.title ? ' · '+esc(s.title) : '';
    const keyInfo = (s.key_info||'').trim();
    const keyInfoBlock = keyInfo
      ? `<div class="history-row-keyinfo">${esc(keyInfo)}</div>`
      : '<div class="history-row-keyinfo empty">No Key Info captured for this session.</div>';
    return `<div class="history-row" onclick="openHistoryDetail('${esc(s.session_name)}')">
      <div class="history-row-top">
        <div class="history-row-name">${esc(s.session_name)}${title} ${livePill}</div>
        <div class="history-row-meta">${ts} · ${s.user_message_count} msgs</div>
      </div>
      ${keyInfoBlock}
    </div>`;
  }).join('');
  document.getElementById('settings-content').innerHTML =
    '<div class="settings-section"><div class="history-list">'+rows+'</div></div>';
}

async function openHistoryDetail(sessionName){
  _settingsHistoryDetail = {session_name: sessionName, loading: true};
  renderHistoryDetail();
  try{
    const resp = await fetch(BASE+'/api/history/'+encodeURIComponent(sessionName));
    const data = await resp.json();
    if(!resp.ok){
      _settingsHistoryDetail = null;
      alert(data.error||'Failed to load session');
      renderSettingsContent();
      return;
    }
    _settingsHistoryDetail = {session_name: sessionName, loading: false, data};
    renderHistoryDetail();
  }catch(e){
    _settingsHistoryDetail = null;
    alert('Failed to load session: '+e.message);
    renderSettingsContent();
  }
}

function renderHistoryDetail(){
  if(!_settingsHistoryDetail) return;
  const el = document.getElementById('settings-content');
  const {session_name, loading, data} = _settingsHistoryDetail;
  if(loading){
    el.innerHTML = `<div class="settings-section">
      <div class="history-detail-header">
        <button class="history-detail-back" onclick="backToHistoryList()">← Back</button>
        <div class="history-detail-name">${esc(session_name)}</div>
      </div>
      <div class="pf-banner">Loading...</div></div>`;
    return;
  }
  const keyInfo = (data.key_info||'').trim();
  const keyInfoBlock = keyInfo
    ? `<div class="history-detail-keyinfo">${esc(keyInfo)}</div>`
    : '<div class="history-detail-keyinfo empty">No Key Info captured for this session.</div>';
  const msgs = data.user_messages || [];
  const msgsHtml = msgs.length
    ? msgs.map(m => `<div class="history-msg">
        <div class="history-msg-ts">${m.ts?timeAgo(m.ts):'no timestamp'}</div>
        ${esc(m.text||'').replace(/\\n/g,'<br>')}
      </div>`).join('')
    : '<div class="history-empty">No user messages were recorded for this session.</div>';
  el.innerHTML = `<div class="settings-section">
    <div class="history-detail-header">
      <button class="history-detail-back" onclick="backToHistoryList()">← Back</button>
      <div class="history-detail-name">${esc(session_name)} <span style="font-size:.72rem;color:#6e7681;font-weight:400">· ${msgs.length} messages</span></div>
    </div>
    <label>Key Info</label>
    ${keyInfoBlock}
    <label style="margin-top:10px">Your messages</label>
    <div class="history-detail-msgs">${msgsHtml}</div>
  </div>`;
}

function backToHistoryList(){
  _settingsHistoryDetail = null;
  renderSettingsContent();
}

// --- Users tab (admin only) ---
let _groupsCache=[];
function _browserName(ua){ua=ua||'';if(/Edg\//.test(ua))return'Edge';if(/OPR\//.test(ua))return'Opera';if(/Chrome\//.test(ua))return'Chrome';if(/Firefox\//.test(ua))return'Firefox';if(/Safari\//.test(ua))return'Safari';return (ua.slice(0,16)||'?');}
function _groupOpts(sel){return '<option value="">— no group —</option>'+_groupsCache.map(g=>`<option value="${esc(g.id)}" ${g.id===sel?'selected':''}>${esc(g.name)}</option>`).join('');}
async function loadUsersAdmin(){
  let data, gdata={groups:[]};
  try{
    const resp = await fetch(BASE+'/api/admin/users');
    data = await resp.json();
    if(!resp.ok){ throw new Error(data.error||'Failed'); }
    const gr = await fetch(BASE+'/api/admin/groups'); if(gr.ok) gdata = await gr.json();
  }catch(e){
    document.getElementById('settings-content').innerHTML =
      '<div class="settings-section"><div class="pf-banner">Failed to load users: '+esc(e.message||e)+'</div></div>';
    return;
  }
  _groupsCache = gdata.groups||[];
  const users = data.users || [];
  const rows = users.map(u => {
    const roleTag = u.role==='admin'?'<span class="users-role-admin">admin</span>':'<span class="users-role-user">user</span>';
    const ll = u.last_login ? (timeAgo(u.last_login)+(u.last_login_ip?(' · '+esc(u.last_login_ip)):'')+(u.last_login_ua?(' · '+esc(_browserName(u.last_login_ua))):'')) : 'never';
    const isMe = (_currentUser && _currentUser.id===u.id);
    const meTag = isMe ? ' <span style="font-size:.62rem;color:#79c0ff;border:1px solid #79c0ff44;border-radius:3px;padding:1px 5px;text-transform:uppercase">you</span>' : '';
    const grpCell = u.role==='admin' ? '<span class="muted">—</span>' :
      `<select class="grp-sel" onchange="setUserGroup('${esc(u.id)}',this.value)">${_groupOpts(u.group||'')}</select>`;
    let acts = `<button class="imp" onclick="openContextEditor('user','${esc(u.id)}','${esc(u.username)}')">Context</button>
      <button onclick="openUserHistory('${esc(u.id)}','${esc(u.username)}')">History</button>
      <button onclick="resetUserPassword('${esc(u.id)}','${esc(u.username)}')">Reset pw</button>`;
    if(!(u.id==='admin'||isMe)) acts += `<button class="imp" onclick="impersonateUser('${esc(u.id)}','${esc(u.username)}')">Log in as</button>
      <button onclick="toggleUserRole('${esc(u.id)}','${u.role}')">${u.role==='admin'?'Demote':'Promote'}</button>
      <button class="danger" onclick="deleteUser('${esc(u.id)}','${esc(u.username)}')">Delete</button>`;
    return `<tr>
      <td><strong>${esc(u.username||'')}</strong>${meTag}</td>
      <td>${roleTag}</td>
      <td>${grpCell}</td>
      <td>${u.session_count||0}</td>
      <td style="font-size:.72rem">${ll}</td>
      <td><div class="users-actions">${acts}</div></td>
    </tr>`;
  }).join('');
  const groupsBar = _groupsCache.map(g=>`<span class="grp-chip">${esc(g.name)} (${g.member_count||0}) <a href="#" onclick="openContextEditor('group','${esc(g.id)}','${esc(g.name)}');return false">ctx</a> <a href="#" onclick="deleteGroup('${esc(g.id)}','${esc(g.name)}');return false" style="color:#f85149">×</a></span>`).join(' ') || '<span class="muted">No groups yet.</span>';
  document.getElementById('settings-content').innerHTML = `<div class="settings-section">
    <div class="pf-banner">Create users, place them in work groups, and view/edit each user's &amp; group's context files (CLAUDE.md, skills, MEMORY.md, settings.json…). Each user has an isolated config + history.</div>
    <div class="users-new-bar">
      <input id="users-new-username" placeholder="username (2-40 chars)" autocomplete="off">
      <input id="users-new-password" placeholder="password (min 6 chars)" type="password" autocomplete="new-password">
      <select id="users-new-role"><option value="user">user</option><option value="admin">admin</option></select>
      <select id="users-new-group">${_groupOpts('')}</select>
      <button onclick="createUserFromForm()">+ Create user</button>
    </div>
    <div class="users-new-bar" style="margin-top:8px;flex-wrap:wrap">
      <input id="new-group-name" placeholder="new work group name" autocomplete="off">
      <button onclick="createGroup()">+ Group</button>
      <span style="margin-left:8px;font-size:.8rem">Work groups: ${groupsBar}</span>
    </div>
    <table class="users-table">
      <thead><tr><th>Username</th><th>Role</th><th>Group</th><th>Sess.</th><th>Last login (time · IP · browser)</th><th></th></tr></thead>
      <tbody>${rows||'<tr><td colspan="6" class="history-empty">No users yet.</td></tr>'}</tbody>
    </table>
  </div>`;
}

async function setUserGroup(userId, group){
  try{ await fetch(BASE+'/api/admin/users/'+encodeURIComponent(userId),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({group})}); }
  catch(e){ alert('Failed to set group'); }
}
async function createGroup(){
  const el=document.getElementById('new-group-name'); const name=(el&&el.value||'').trim();
  if(!name){ alert('Enter a group name'); return; }
  const r=await fetch(BASE+'/api/admin/groups',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})});
  const d=await r.json(); if(!r.ok){ alert(d.error||'Failed'); return; } loadUsersAdmin();
}
async function deleteGroup(id,name){
  if(!confirm('Delete group "'+name+'"? Members will be unassigned (their per-user context stays).')) return;
  await fetch(BASE+'/api/admin/groups/'+encodeURIComponent(id),{method:'DELETE'}); loadUsersAdmin();
}

async function createUserFromForm(){
  const username = (document.getElementById('users-new-username')||{}).value || '';
  const password = (document.getElementById('users-new-password')||{}).value || '';
  const role = (document.getElementById('users-new-role')||{}).value || 'user';
  const group = (document.getElementById('users-new-group')||{}).value || '';
  if(!username.trim() || password.length<6){
    alert('Username + at least 6-character password required.');
    return;
  }
  try{
    const resp = await fetch(BASE+'/api/admin/users', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({username:username.trim(), password, role, group})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to create user'); return; }
    loadUsersAdmin();
  }catch(e){ alert('Failed: '+e.message); }
}

// ── Admin: context-file editor (per user / per group) ──
let _ctxState={scope:'user',ident:'',label:'',cur:''};
async function openContextEditor(scope, ident, label){
  _ctxState={scope,ident,label,cur:''};
  const modal=document.getElementById('modal-content');
  modal.classList.add('modal-wide');
  modal.innerHTML=`<h3>Context files — ${esc(label)} <span class="muted">(${scope})</span></h3><p class="conn-note">Loading…</p>`;
  document.getElementById('modal-overlay').classList.add('active');
  await _ctxRefresh();
}
async function _ctxRefresh(){
  const {scope,ident}=_ctxState;
  let d; try{ const r=await fetch(`${BASE}/api/admin/context/${scope}/${encodeURIComponent(ident)}`); d=await r.json(); if(!r.ok)throw new Error(d.error); }
  catch(e){ document.getElementById('modal-content').innerHTML=`<h3>Context files</h3><p class="conn-note">Failed: ${esc(e.message||e)}</p><div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`; return; }
  const files=d.files||[];
  const list=files.map(f=>`<div class="ctx-file ${f.path===_ctxState.cur?'active':''}" onclick="_ctxOpen('${esc(f.path)}')">${esc(f.path)}</div>`).join('')||'<div class="ctx-file muted">No files.</div>';
  const modal=document.getElementById('modal-content');
  modal.innerHTML=`<h3>Context files — ${esc(_ctxState.label)} <span class="muted">(${_ctxState.scope})</span></h3>
    <p class="conn-note">All files that shape Claude's behaviour. Edits apply to this ${_ctxState.scope}. Add a new file with the box below (e.g. <code>skills/mytool/SKILL.md</code>).</p>
    <div class="ctx-wrap">
      <div class="ctx-files">${list}</div>
      <div class="ctx-edit">
        <div id="ctx-editarea"><p class="conn-note">Select a file on the left, or add one below.</p></div>
      </div>
    </div>
    <div class="users-new-bar" style="margin-top:10px">
      <input id="ctx-newpath" placeholder="new file path e.g. skills/foo/SKILL.md" autocomplete="off" style="flex:1">
      <button onclick="_ctxNew()">+ Add file</button>
      <button class="modal-cancel" onclick="closeModal()">Close</button>
    </div>`;
  if(_ctxState.cur) _ctxOpen(_ctxState.cur);
}
async function _ctxOpen(path){
  _ctxState.cur=path;
  document.querySelectorAll('.ctx-file').forEach(el=>el.classList.toggle('active', el.textContent===path));
  const {scope,ident}=_ctxState;
  const area=document.getElementById('ctx-editarea'); if(area) area.innerHTML='<p class="conn-note">Loading…</p>';
  let d; try{ const r=await fetch(`${BASE}/api/admin/context/${scope}/${encodeURIComponent(ident)}/file?path=${encodeURIComponent(path)}`); d=await r.json(); if(!r.ok)throw new Error(d.error); }
  catch(e){ if(area)area.innerHTML=`<p class="conn-note">Failed: ${esc(e.message||e)}</p>`; return; }
  if(!area)return;
  area.innerHTML=`<textarea id="ctx-content" spellcheck="false">${esc(d.content||'')}</textarea>
    <div class="modal-actions" style="margin-top:8px">
      <button class="danger modal-cancel" onclick="_ctxDelete('${esc(path)}')">Delete</button>
      <button class="modal-confirm-create" onclick="_ctxSave()">Save ${esc(path)}</button>
    </div>`;
}
async function _ctxSave(){
  const ta=document.getElementById('ctx-content'); if(!ta)return;
  const {scope,ident,cur}=_ctxState;
  const r=await fetch(`${BASE}/api/admin/context/${scope}/${encodeURIComponent(ident)}/file`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:cur,content:ta.value})});
  const d=await r.json(); if(!r.ok){ alert(d.error||'Save failed'); return; }
  const btn=document.querySelector('#ctx-editarea .modal-confirm-create'); if(btn){const o=btn.textContent;btn.textContent='Saved ✓';setTimeout(()=>btn.textContent=o,1500);}
}
async function _ctxNew(){
  const el=document.getElementById('ctx-newpath'); const p=(el&&el.value||'').trim(); if(!p){alert('Enter a file path');return;}
  _ctxState.cur=p; await _ctxRefresh();
  const area=document.getElementById('ctx-editarea');
  if(area) area.innerHTML=`<textarea id="ctx-content" spellcheck="false"></textarea>
    <div class="modal-actions" style="margin-top:8px"><button class="modal-confirm-create" onclick="_ctxSave()">Create ${esc(p)}</button></div>`;
}
async function _ctxDelete(path){
  if(!confirm('Delete '+path+'?')) return;
  const {scope,ident}=_ctxState;
  await fetch(`${BASE}/api/admin/context/${scope}/${encodeURIComponent(ident)}/file?path=${encodeURIComponent(path)}`,{method:'DELETE'});
  _ctxState.cur=''; _ctxRefresh();
}

// ── Admin: view a user's full history ──
async function openUserHistory(userId, username){
  const modal=document.getElementById('modal-content');
  modal.innerHTML=`<h3>History — ${esc(username)}</h3><p class="conn-note">Loading…</p>`;
  document.getElementById('modal-overlay').classList.add('active');
  let d; try{ const r=await fetch(`${BASE}/api/admin/users/${encodeURIComponent(userId)}/history`); d=await r.json(); if(!r.ok)throw new Error(d.error); }
  catch(e){ modal.innerHTML=`<h3>History — ${esc(username)}</h3><p class="conn-note">Failed: ${esc(e.message||e)}</p><div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`; return; }
  const ss=d.sessions||[];
  const rows=ss.map(s=>`<div class="approval-row"><div class="approval-meta"><b>${esc(s.session_name)}</b> ${s.is_live?'<span class="pill-approved">live</span>':''} · ${s.user_message_count||0} msgs · ${s.last_message_at?timeAgo(s.last_message_at):'—'}</div>${s.key_info?`<div class="muted" style="font-size:.78rem">${esc((s.key_info||'').slice(0,160))}</div>`:''}<div class="approval-actions"><button class="btn-approve" onclick="_userHistDetail('${esc(userId)}','${esc(s.session_name)}','${esc(username)}')">View transcript</button></div></div>`).join('')||'<p class="conn-note">No history.</p>';
  modal.innerHTML=`<h3>History — ${esc(username)}</h3><p class="conn-note">All of this user's sessions (live + past).</p>${rows}<div class="modal-actions"><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;
}
async function _userHistDetail(userId, session, username){
  const modal=document.getElementById('modal-content');
  modal.innerHTML=`<h3>${esc(session)} — ${esc(username)}</h3><p class="conn-note">Loading…</p>`;
  let d; try{ const r=await fetch(`${BASE}/api/admin/users/${encodeURIComponent(userId)}/history/${encodeURIComponent(session)}`); d=await r.json(); if(!r.ok)throw new Error(d.error);}catch(e){modal.innerHTML=`<p class="conn-note">Failed: ${esc(e.message||e)}</p>`;return;}
  const msgs=(d.messages||[]).map(m=>{const role=(m.role||'?');const cls=role==='user'?'pill-approved':'';return `<div class="approval-row"><div class="approval-meta ${cls}">${esc(role)} · ${m.ts?timeAgo(m.ts):''}</div><pre>${esc((m.text||m.content||'')+'')}</pre></div>`;}).join('')||'<p class="conn-note">No messages.</p>';
  modal.innerHTML=`<h3>${esc(session)} — ${esc(username)}</h3>${d.key_info?`<p class="conn-note">Key info: ${esc(d.key_info)}</p>`:''}${msgs}<div class="modal-actions"><button class="modal-cancel" onclick="openUserHistory('${esc(userId)}','${esc(username)}')">← Back</button><button class="modal-cancel" onclick="closeModal()">Close</button></div>`;
}

async function resetUserPassword(userId, username){
  const pw = prompt('New password for "'+username+'" (min 6 chars):');
  if(pw===null) return;
  if(pw.length < 6){ alert('Password must be at least 6 characters.'); return; }
  try{
    const resp = await fetch(BASE+'/api/admin/users/'+encodeURIComponent(userId), {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({password: pw})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed'); return; }
    alert('Password updated for '+username+'.');
  }catch(e){ alert('Failed: '+e.message); }
}

async function toggleUserRole(userId, currentRole){
  const newRole = currentRole==='admin' ? 'user' : 'admin';
  if(!confirm('Change role to "'+newRole+'"?')) return;
  try{
    const resp = await fetch(BASE+'/api/admin/users/'+encodeURIComponent(userId), {
      method:'PATCH', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({role: newRole})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed'); return; }
    loadUsersAdmin();
  }catch(e){ alert('Failed: '+e.message); }
}

async function deleteUser(userId, username){
  if(!confirm('Delete user "'+username+'"? This kills their tmux sessions and removes their data and CLAUDE config directory. This cannot be undone.')) return;
  try{
    const resp = await fetch(BASE+'/api/admin/users/'+encodeURIComponent(userId), {
      method:'DELETE'
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed'); return; }
    loadUsersAdmin();
  }catch(e){ alert('Failed: '+e.message); }
}

async function impersonateUser(userId, username){
  if(!confirm('Log in as "'+username+'"? You\'ll see the dashboard exactly as they do (their sessions, their view). Use the "Return to admin" bar at the bottom to come back.')) return;
  try{
    const resp = await fetch(BASE+'/api/admin/users/'+encodeURIComponent(userId)+'/impersonate', {method:'POST'});
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed'); return; }
    window.location.href = BASE + '/';  // reload as the impersonated user
  }catch(e){ alert('Failed: '+e.message); }
}

async function returnToAdmin(){
  try{ await fetch(BASE+'/api/unimpersonate', {method:'POST'}); }catch(e){}
  window.location.href = BASE + '/';
}

function renderImpersonationBanner(){
  const el = document.getElementById('imp-banner');
  if(!el) return;
  if(_currentUser && _currentUser.impersonating){
    el.style.display = 'flex';
    el.innerHTML = '\u{1F441}️ Viewing as <b style="margin:0 2px">'+esc(_currentUser.username||'')+
      '</b> — impersonated by '+esc(_currentUser.impersonator||'admin')+
      ' <button onclick="returnToAdmin()">Return to admin</button>';
  } else {
    el.style.display = 'none';
  }
}

async function openProfiles(){
  const overlay = document.getElementById('profiles-overlay');
  overlay.classList.add('active');
  await loadProfiles(true);
  renderProfilesList();
  document.getElementById('profile-edit').innerHTML =
    '<div class="profile-empty">Select a profile on the left, or create a new one.</div>';
}

function closeProfiles(){
  document.getElementById('profiles-overlay').classList.remove('active');
  _profilesEditing = null;
}

function renderProfilesList(){
  const list = _profilesCache || [];
  const el = document.getElementById('profiles-list');
  let html = '<button class="profile-new-btn" onclick="newProfilePrompt()">+ New profile</button>';
  list.forEach(p => {
    const sel = (_profilesEditing && _profilesEditing.id===p.id) ? ' selected' : '';
    const tag = p.builtin
      ? (p.id==='default' ? '<span class="profile-row-builtin">default</span>' : '<span class="profile-row-builtin">preset</span>')
      : '';
    const meta = (p.model||'') + (p.effort?(' &middot; '+esc(p.effort)):'');
    html += `<div class="profile-row${sel}" onclick="editProfile('${esc(p.id)}')">
      <div class="profile-row-name">${esc(p.name)} ${tag}</div>
      <div class="profile-row-meta">${meta || '&mdash;'}</div>
    </div>`;
  });
  el.innerHTML = html;
}

async function editProfile(profileId){
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(profileId));
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to load profile'); return; }
    _profilesEditing = data;
    renderProfilesList();
    renderProfileEdit();
  }catch(e){ alert('Failed to load profile.'); }
}

let _profileActiveTab = 'identity';
const _PROFILE_TABS = [
  {id:'identity', label:'Identity'},
  {id:'memory',   label:'Memory'},
  {id:'login',    label:'Login'},
  {id:'settings', label:'Settings'},
  {id:'mcp',      label:'MCP'},
  {id:'agents',   label:'Agents'},
  {id:'commands', label:'Commands'},
  {id:'plugins',  label:'Plugins'},
  {id:'extras',   label:'Extras'},
];

function renderProfileEdit(){
  const p = _profilesEditing;
  const el = document.getElementById('profile-edit');
  if(!p){ el.innerHTML = '<div class="profile-empty">Select a profile.</div>'; return; }
  const isDefault = (p.id === 'default');
  const permJson = JSON.stringify(p.permissions||{}, null, 2);
  const envJson = JSON.stringify(p.env||{}, null, 2);
  const dir = p.dir || (isDefault ? '~/.claude (merged)' : ('~/.claude-'+p.id));
  const builtinTag = isDefault
    ? ' <span class="profile-row-builtin">default</span>'
    : (p.builtin ? ' <span class="profile-row-builtin">preset</span>' : '');
  // Effort glyphs requested by user
  const EFFORTS = [
    {v:'',      label:'(default)'},
    {v:'medium',label:'medium (◐)'},
    {v:'high',  label:'high (●)'},
    {v:'xhigh', label:'xhigh (◉)'},
    {v:'max',   label:'max (⬤)'},
  ];
  const effortOpts = EFFORTS.map(o =>
    `<option value="${o.v}"${(p.effort||'')===o.v?' selected':''}>${o.label}</option>`
  ).join('');
  const deleteBtn = isDefault
    ? ''
    : `<button class="modal-confirm-delete" onclick="deleteProfile('${esc(p.id)}')">Delete profile</button>`;
  const headerNote = isDefault
    ? '<div class="profiles-hint" style="margin:0 0 6px">Settings → identity edits are merged into <code>~/.claude/settings.json</code> (only <code>model</code>, <code>env</code>, <code>permissions</code> touched). Backup at <code>~/.claude/settings.json.bak-pre-dashboard</code>.</div>'
    : '';

  const tabBar = _PROFILE_TABS.map(t =>
    `<div class="pf-tab${_profileActiveTab===t.id?' active':''}" onclick="switchProfileTab('${t.id}')">${t.label}</div>`
  ).join('');

  el.innerHTML = `
    <div style="font-size:.7rem;color:#6e7681;font-family:'SF Mono',Consolas,monospace">${esc(dir)}${builtinTag}</div>
    ${headerNote}
    <div class="pf-tabs">${tabBar}</div>

    <div class="pf-section${_profileActiveTab==='identity'?' active':''}" id="pf-section-identity">
      <label>Name</label>
      <input id="ed-name" value="${esc(p.name||'')}">
      <div class="row2">
        <div>
          <label>Model</label>
          <input id="ed-model" value="${esc(p.model||'')}" placeholder="claude-sonnet-4-6 / claude-opus-4-8[1m] / blank">
        </div>
        <div>
          <label>Effort level</label>
          <select id="ed-effort">${effortOpts}</select>
        </div>
      </div>
      <label>Permissions (JSON)</label>
      <textarea id="ed-permissions" class="ed-permissions" spellcheck="false">${esc(permJson)}</textarea>
      <label>Env (JSON)</label>
      <textarea id="ed-env" class="ed-permissions" spellcheck="false">${esc(envJson)}</textarea>
    </div>

    <div class="pf-section${_profileActiveTab==='memory'?' active':''}" id="pf-section-memory">
      <div class="pf-banner">CLAUDE.md and MEMORY.md live at the profile root. Claude Code loads them at every launch in this profile.</div>
      <label>CLAUDE.md</label>
      <textarea id="ed-claude" class="ed-claude" spellcheck="false">${esc(p.claude_md||'')}</textarea>
      <label>MEMORY.md</label>
      <textarea id="ed-memory" class="ed-memory" spellcheck="false">${esc(p.memory_md||'')}</textarea>
    </div>

    <div class="pf-section${_profileActiveTab==='login'?' active':''}" id="pf-section-login">
      <div class="pf-banner">Each profile keeps its own <code>.credentials.json</code>. Logging in once per profile is enough — switching profiles does not re-prompt.</div>
      <div id="pf-credentials" class="pf-status">Loading login status...</div>
      <div class="pf-actions">
        <button class="extras-btn" onclick="refreshProfileCredentials('${esc(p.id)}')">Refresh status</button>
        <button class="extras-btn" onclick="logoutProfile('${esc(p.id)}')" style="color:#f85149;border-color:#f8514944">Log out (remove credentials)</button>
      </div>
      <div class="pf-help">To log in: open a session on this profile and run <code>/login</code> inside Claude. The token is written to <code>.credentials.json</code> inside the profile dir.</div>
    </div>

    <div class="pf-section${_profileActiveTab==='settings'?' active':''}" id="pf-section-settings">
      <div class="pf-banner">Full <code>settings.json</code> as Claude Code reads it. The Identity tab edits the <code>model</code>, <code>env</code>, and <code>permissions</code> keys — keep them in sync by saving here OR there, not both.</div>
      <label>settings.json</label>
      <textarea id="ed-settings-json" class="ed-rawjson" spellcheck="false">Loading...</textarea>
      <div class="pf-actions">
        <button class="extras-btn" onclick="saveProfileRawFile('${esc(p.id)}','settings.json','ed-settings-json')">Save settings.json</button>
        <button class="extras-btn" onclick="loadProfileRawFile('${esc(p.id)}','settings.json','ed-settings-json')">Reload</button>
      </div>
    </div>

    <div class="pf-section${_profileActiveTab==='mcp'?' active':''}" id="pf-section-mcp">
      <div class="pf-banner">Profile-scope MCP servers + per-project approvals. Lives at <code>.claude.json</code>. Project-scope <code>.mcp.json</code> is edited per session via the More dropdown.</div>
      <label>.claude.json</label>
      <textarea id="ed-mcp-json" class="ed-mcp" spellcheck="false">Loading...</textarea>
      <div class="pf-actions">
        <button class="extras-btn" onclick="saveProfileRawFile('${esc(p.id)}','.claude.json','ed-mcp-json')">Save .claude.json</button>
        <button class="extras-btn" onclick="loadProfileRawFile('${esc(p.id)}','.claude.json','ed-mcp-json')">Reload</button>
      </div>
    </div>

    <div class="pf-section${_profileActiveTab==='agents'?' active':''}" id="pf-section-agents">
      <div class="pf-banner">Custom subagents in <code>agents/</code>. Each <code>.md</code> file with frontmatter (<code>name</code>, <code>description</code>) becomes a delegatable agent in this profile.</div>
      <div id="pf-agents-list">Loading...</div>
      <div class="pf-actions">
        <button class="extras-btn" onclick="addProfileSubfile('${esc(p.id)}','agents')">+ New agent</button>
      </div>
    </div>

    <div class="pf-section${_profileActiveTab==='commands'?' active':''}" id="pf-section-commands">
      <div class="pf-banner">Custom slash commands in <code>commands/</code>. Each <code>.md</code> file becomes <code>/&lt;name&gt;</code>.</div>
      <div id="pf-commands-list">Loading...</div>
      <div class="pf-actions">
        <button class="extras-btn" onclick="addProfileSubfile('${esc(p.id)}','commands')">+ New command</button>
      </div>
    </div>

    <div class="pf-section${_profileActiveTab==='plugins'?' active':''}" id="pf-section-plugins">
      <div class="pf-banner">Installed plugins under <code>plugins/</code>. Read-only here — install/remove plugins from inside Claude Code with <code>/plugin</code>.</div>
      <div id="pf-plugins-list">Loading...</div>
    </div>

    <div class="pf-section${_profileActiveTab==='extras'?' active':''}" id="pf-section-extras">
      <div class="pf-banner">Sidecar markdown files at the profile root (e.g. <code>CLAUDE_API_KEYS.md</code>) referenced from CLAUDE.md.</div>
      <div class="extras-section" id="ed-extras">
        <div class="extras-empty">Loading...</div>
      </div>
    </div>

    <div class="profile-edit-actions">
      ${deleteBtn || '<span></span>'}
      <div style="display:flex;gap:8px">
        <button class="modal-cancel" onclick="closeProfiles()">Close</button>
        <button class="modal-confirm-create" onclick="saveProfile()">Save identity + memory</button>
      </div>
    </div>
  `;
  loadProfileExtras(p.id);
  loadProfileSectionData(p.id, _profileActiveTab);
}

function switchProfileTab(tabId){
  _profileActiveTab = tabId;
  document.querySelectorAll('.pf-tab').forEach(t=>{
    t.classList.toggle('active', t.textContent.trim().toLowerCase()===tabId);
  });
  document.querySelectorAll('.pf-section').forEach(s=>{
    s.classList.toggle('active', s.id==='pf-section-'+tabId);
  });
  if(_profilesEditing) loadProfileSectionData(_profilesEditing.id, tabId);
}

// ── Section loaders ─────────────────────────────────────────────────────────
async function loadProfileSectionData(pid, tab){
  if(tab==='login'){ refreshProfileCredentials(pid); return; }
  if(tab==='settings'){ loadProfileRawFile(pid, 'settings.json', 'ed-settings-json'); return; }
  if(tab==='mcp'){ loadProfileRawFile(pid, '.claude.json', 'ed-mcp-json'); return; }
  if(tab==='agents'){ loadProfileDirSection(pid, 'agents', 'pf-agents-list'); return; }
  if(tab==='commands'){ loadProfileDirSection(pid, 'commands', 'pf-commands-list'); return; }
  if(tab==='plugins'){ loadProfilePluginsList(pid); return; }
}

async function refreshProfileCredentials(pid){
  const el = document.getElementById('pf-credentials');
  if(!el) return;
  el.className = 'pf-status';
  el.textContent = 'Loading...';
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/credentials');
    const data = await resp.json();
    if(!resp.ok){ el.className='pf-status err'; el.textContent = data.error||'Failed to load'; return; }
    if(data.loggedIn){
      el.className='pf-status ok';
      const days = data.expiresInDays;
      const plan = data.subscriptionType ? ` · ${data.subscriptionType}` : '';
      el.textContent = `Logged in${plan}${days ? ` · ~${days} days until token expires` : ''}`;
    } else if(data.exists){
      el.className='pf-status warn';
      el.textContent = 'Credentials present but token expired. Run /login in a session on this profile.';
    } else {
      el.className='pf-status warn';
      el.textContent = 'Not logged in. Open a session on this profile and run /login inside Claude.';
    }
  }catch(e){
    el.className='pf-status err';
    el.textContent = 'Failed to load credentials status';
  }
}

async function logoutProfile(pid){
  if(!confirm('Remove .credentials.json for this profile? You will need to /login again in any session that uses it.')) return;
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/credentials', {method:'DELETE'});
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to log out'); return; }
    refreshProfileCredentials(pid);
  }catch(e){ alert('Failed to log out'); }
}

async function loadProfileRawFile(pid, relPath, taId){
  const ta = document.getElementById(taId);
  if(!ta) return;
  ta.value = 'Loading...';
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/file?path='+encodeURIComponent(relPath));
    const data = await resp.json();
    if(!resp.ok){ ta.value=''; alert(data.error||'Failed to load'); return; }
    ta.value = data.content || '';
    if(!data.exists){
      ta.placeholder = relPath + ' does not exist yet — saving will create it.';
    }
  }catch(e){ ta.value=''; alert('Failed to load file'); }
}

async function saveProfileRawFile(pid, relPath, taId){
  const ta = document.getElementById(taId);
  if(!ta) return;
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/file', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: relPath, content: ta.value})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to save'); return; }
    ta.style.borderColor = '#3fb950';
    setTimeout(()=>{ ta.style.borderColor=''; }, 600);
  }catch(e){ alert('Failed to save'); }
}

let _profileDirSections = {agents:{files:[],editing:''}, commands:{files:[],editing:''}};

async function loadProfileDirSection(pid, cat, containerId){
  const container = document.getElementById(containerId);
  if(!container) return;
  container.innerHTML = '<div class="pf-empty">Loading...</div>';
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/files');
    const data = await resp.json();
    if(!resp.ok){ container.innerHTML='<div class="pf-empty">'+(data.error||'Failed to load')+'</div>'; return; }
    const entry = (data.categories||{})[cat] || {files:[]};
    const files = (entry.files||[]).filter(f => f.kind==='file');
    _profileDirSections[cat] = _profileDirSections[cat] || {files:[],editing:''};
    _profileDirSections[cat].files = files;
    renderProfileDirSection(pid, cat, containerId);
  }catch(e){ container.innerHTML='<div class="pf-empty">Failed to load</div>'; }
}

function renderProfileDirSection(pid, cat, containerId){
  const container = document.getElementById(containerId);
  if(!container) return;
  const state = _profileDirSections[cat] || {files:[],editing:''};
  const files = state.files || [];
  if(!files.length){
    container.innerHTML = '<div class="pf-empty">No '+cat+' yet.</div>';
    return;
  }
  let html = '';
  files.forEach(f => {
    const isEditing = state.editing === f.name;
    html += `<div class="pf-row">
      <span class="pf-row-name" onclick="toggleProfileDirFile('${esc(pid)}','${esc(cat)}','${esc(f.name)}')">${esc(f.name)}</span>
      <span class="pf-row-meta">${f.size} bytes</span>
      <button class="extras-btn" onclick="toggleProfileDirFile('${esc(pid)}','${esc(cat)}','${esc(f.name)}')">${isEditing?'Hide':'Edit'}</button>
      <button class="extras-del" onclick="deleteProfileDirFile('${esc(pid)}','${esc(cat)}','${esc(f.name)}')" title="Delete">&times;</button>
    </div>
    <div class="extras-editor${isEditing?' active':''}" id="dirfile-editor-${esc(cat)}-${esc(f.name)}">
      <textarea spellcheck="false" data-dir-name="${esc(f.name)}">Loading...</textarea>
      <div style="display:flex;gap:6px;justify-content:flex-end">
        <button class="extras-btn" onclick="saveProfileDirFile('${esc(pid)}','${esc(cat)}','${esc(f.name)}')">Save changes</button>
      </div>
    </div>`;
  });
  container.innerHTML = html;
  // Lazy-load content for the currently-editing one
  if(state.editing){
    loadProfileDirFileContent(pid, cat, state.editing);
  }
}

async function toggleProfileDirFile(pid, cat, name){
  const state = _profileDirSections[cat] || {files:[],editing:''};
  state.editing = (state.editing === name) ? '' : name;
  renderProfileDirSection(pid, cat, cat==='agents'?'pf-agents-list':'pf-commands-list');
}

async function loadProfileDirFileContent(pid, cat, name){
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/file?path='+encodeURIComponent(cat+'/'+name));
    const data = await resp.json();
    if(!resp.ok){ return; }
    const ta = document.querySelector('#dirfile-editor-'+CSS.escape(cat)+'-'+CSS.escape(name)+' textarea');
    if(ta) ta.value = data.content || '';
  }catch(e){}
}

async function saveProfileDirFile(pid, cat, name){
  const ta = document.querySelector('#dirfile-editor-'+CSS.escape(cat)+'-'+CSS.escape(name)+' textarea');
  if(!ta) return;
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/file', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: cat+'/'+name, content: ta.value})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to save'); return; }
    ta.style.borderColor = '#3fb950';
    setTimeout(()=>{ ta.style.borderColor=''; }, 600);
  }catch(e){ alert('Failed to save'); }
}

async function deleteProfileDirFile(pid, cat, name){
  if(!confirm('Delete '+cat+'/'+name+'?')) return;
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/file?path='+encodeURIComponent(cat+'/'+name), {method:'DELETE'});
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to delete'); return; }
    loadProfileDirSection(pid, cat, cat==='agents'?'pf-agents-list':'pf-commands-list');
  }catch(e){ alert('Failed to delete'); }
}

async function addProfileSubfile(pid, cat){
  const example = cat==='agents'
    ? 'my-agent.md'
    : 'my-command.md';
  const raw = window.prompt('New '+cat.slice(0,-1)+' filename (must end in .md):', example);
  if(raw === null) return;
  let name = (raw||'').trim();
  if(!name) return;
  if(!name.toLowerCase().endsWith('.md')) name += '.md';
  // Sanitize: alphanumeric + dash + underscore + .md
  if(!/^[A-Za-z0-9._-]+\.md$/.test(name)){
    alert('Use alphanumerics, dashes, underscores, dots only; must end in .md.');
    return;
  }
  // Boilerplate frontmatter
  const stub = cat==='agents'
    ? `---\nname: ${name.replace(/\.md$/,'')}\ndescription: Describe when Claude should delegate to this agent.\ntools: '*'\n---\n\n# ${name.replace(/\.md$/,'')}\n\nInstructions...\n`
    : `---\ndescription: One-line summary shown in the slash-command menu.\nallowed-tools: '*'\n---\n\n# /${name.replace(/\.md$/,'')}\n\nWhat this command does...\n`;
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/file', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: cat+'/'+name, content: stub})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to create'); return; }
    _profileDirSections[cat] = _profileDirSections[cat] || {files:[],editing:''};
    _profileDirSections[cat].editing = name;
    loadProfileDirSection(pid, cat, cat==='agents'?'pf-agents-list':'pf-commands-list');
  }catch(e){ alert('Failed to create'); }
}

async function loadProfilePluginsList(pid){
  const container = document.getElementById('pf-plugins-list');
  if(!container) return;
  container.innerHTML = '<div class="pf-empty">Loading...</div>';
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(pid)+'/files');
    const data = await resp.json();
    if(!resp.ok){ container.innerHTML='<div class="pf-empty">'+(data.error||'Failed')+'</div>'; return; }
    const entries = ((data.categories||{}).plugins||{}).files || [];
    if(!entries.length){
      container.innerHTML = '<div class="pf-empty">No plugins installed. Use <code>/plugin</code> inside Claude to install.</div>';
      return;
    }
    container.innerHTML = entries.map(e =>
      `<div class="pf-row">
        <span class="pf-row-name">${esc(e.name)}</span>
        <span class="pf-row-tag">${esc(e.kind)}</span>
      </div>`
    ).join('');
  }catch(e){ container.innerHTML='<div class="pf-empty">Failed to load</div>'; }
}

let _profileExtras = {profileId:'', files:[], editing:''};

async function loadProfileExtras(profileId){
  _profileExtras = {profileId, files:[], editing:''};
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(profileId)+'/extras');
    const data = await resp.json();
    if(!resp.ok){ throw new Error(data.error||'load failed'); }
    _profileExtras.files = data.files || [];
  }catch(e){
    _profileExtras.files = [];
  }
  renderProfileExtras();
}

function renderProfileExtras(){
  const el = document.getElementById('ed-extras');
  if(!el) return;
  const files = _profileExtras.files || [];
  let html = '';
  if(!files.length){
    html += '<div class="extras-empty">No extra files yet. Add one — e.g. <code>CLAUDE_API_KEYS.md</code> — and reference it from CLAUDE.md.</div>';
  } else {
    files.forEach(f => {
      const isEditing = _profileExtras.editing === f.name;
      html += `<div class="extras-row">
        <span class="extras-name" onclick="toggleExtraEditor('${esc(f.name)}')">${esc(f.name)}</span>
        <span class="extras-meta">${f.size} bytes</span>
        <button class="extras-btn" onclick="toggleExtraEditor('${esc(f.name)}')">${isEditing?'Hide':'Edit'}</button>
        <button class="extras-del" onclick="deleteProfileExtra('${esc(f.name)}')" title="Delete">&times;</button>
      </div>
      <div class="extras-editor${isEditing?' active':''}" id="extras-editor-${esc(f.name)}">
        <textarea spellcheck="false" data-extra-name="${esc(f.name)}">${esc(f.content||'')}</textarea>
        <div style="display:flex;gap:6px;justify-content:flex-end">
          <button class="extras-btn" onclick="saveProfileExtra('${esc(f.name)}')">Save changes</button>
        </div>
      </div>`;
    });
  }
  html += `<div class="extras-actions">
    <button class="extras-btn" onclick="addProfileExtra()">+ Add markdown file</button>
  </div>`;
  el.innerHTML = html;
}

function toggleExtraEditor(name){
  _profileExtras.editing = (_profileExtras.editing === name) ? '' : name;
  renderProfileExtras();
}

async function addProfileExtra(){
  const raw = window.prompt('New filename (must end in .md):', 'CLAUDE_API_KEYS.md');
  if(raw === null) return;
  let name = (raw||'').trim();
  if(!name) return;
  if(!name.toLowerCase().endsWith('.md')) name += '.md';
  if(['claude.md','memory.md'].includes(name.toLowerCase())){
    alert("'"+name+"' has a dedicated editor above — pick a different name.");
    return;
  }
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(_profileExtras.profileId)+'/extras', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, content: ''})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to create file'); return; }
    _profileExtras.editing = data.name;
    await loadProfileExtras(_profileExtras.profileId);
  }catch(e){ alert('Failed to create file.'); }
}

async function saveProfileExtra(name){
  const ta = document.querySelector('textarea[data-extra-name="'+CSS.escape(name)+'"]');
  if(!ta) return;
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(_profileExtras.profileId)+'/extras', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, content: ta.value})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to save'); return; }
    // Update in-memory copy without re-rendering (preserves cursor)
    const f = (_profileExtras.files || []).find(x => x.name === name);
    if(f){ f.content = ta.value; f.size = new Blob([ta.value]).size; }
    // Visual flash
    ta.style.borderColor = '#3fb950';
    setTimeout(()=>{ ta.style.borderColor=''; }, 600);
  }catch(e){ alert('Failed to save.'); }
}

async function deleteProfileExtra(name){
  if(!confirm('Delete '+name+'?')) return;
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(_profileExtras.profileId)+'/extras/'+encodeURIComponent(name), {method:'DELETE'});
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to delete'); return; }
    if(_profileExtras.editing === name) _profileExtras.editing = '';
    await loadProfileExtras(_profileExtras.profileId);
  }catch(e){ alert('Failed to delete.'); }
}

async function saveProfile(){
  const p = _profilesEditing;
  if(!p) return;
  const name = (document.getElementById('ed-name').value||'').trim();
  const model = (document.getElementById('ed-model').value||'').trim();
  const effort = document.getElementById('ed-effort').value;
  const claudeMd = document.getElementById('ed-claude').value;
  const memoryMd = document.getElementById('ed-memory').value;
  let permissions, env;
  try{ permissions = JSON.parse(document.getElementById('ed-permissions').value||'{}'); }
  catch(e){ alert('Permissions JSON is invalid: '+e.message); return; }
  try{ env = JSON.parse(document.getElementById('ed-env').value||'{}'); }
  catch(e){ alert('Env JSON is invalid: '+e.message); return; }
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(p.id), {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, model, effort, claude_md: claudeMd, memory_md: memoryMd, permissions, env})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to save'); return; }
    await loadProfiles(true);
    await editProfile(p.id);
  }catch(e){ alert('Failed to save profile.'); }
}

async function deleteProfile(profileId){
  if(profileId==='default'){ alert("The default profile can't be deleted."); return; }
  if(!confirm('Delete profile "'+profileId+'"? Sessions on this profile will revert to Default.\\n\\nNote: ~/.claude-'+profileId+'/ is left on disk -- remove manually if desired.')) return;
  try{
    const resp = await fetch(BASE+'/api/profiles/'+encodeURIComponent(profileId), {method:'DELETE'});
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to delete'); return; }
    _profilesEditing = null;
    await loadProfiles(true);
    renderProfilesList();
    document.getElementById('profile-edit').innerHTML =
      '<div class="profile-empty">Profile deleted.</div>';
    // Refresh visible session dropdowns
    if(selectedSession) renderDetail();
  }catch(e){ alert('Failed to delete profile.'); }
}

async function newProfilePrompt(){
  const list = _profilesCache || [];
  const presets = list.filter(p => p.builtin && p.id!=='default');
  let promptMsg = 'New profile name (e.g. "My UI Reviewer"):';
  if(presets.length){
    promptMsg += '\\n\\nLeave empty to start blank. To clone a preset, append " | preset-id" -- available preset ids:\\n  '
      + presets.map(p=>p.id).join(', ');
  }
  const raw = window.prompt(promptMsg, '');
  if(raw === null) return;
  let name = raw.trim();
  let fromPreset = '';
  if(name.includes('|')){
    const parts = name.split('|');
    name = parts[0].trim();
    fromPreset = parts[1].trim();
  }
  if(!name){ alert('Name is required.'); return; }
  try{
    const resp = await fetch(BASE+'/api/profiles', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, from_preset: fromPreset})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to create profile'); return; }
    await loadProfiles(true);
    await editProfile(data.id);
    if(selectedSession) renderDetail();
  }catch(e){ alert('Failed to create profile.'); }
}

// ── Project-scope file editor (per-session, cwd-bound) ──
let _projFile = {sessionName:'', path:'', absPath:'', cwd:'', content:'', exists:false};

const _PROJFILE_LABELS = {
  'CLAUDE.md': 'Project CLAUDE.md',
  '.claude/settings.json': 'Project settings.json',
  '.claude/settings.local.json': 'Project settings.local.json',
  '.mcp.json': 'Project .mcp.json',
};

const _PROJFILE_DESCRIPTIONS = {
  'CLAUDE.md': 'Markdown rules loaded on top of the profile-level CLAUDE.md whenever Claude runs inside this cwd. Use this for repo-specific conventions.',
  '.claude/settings.json': 'JSON. Project-level settings (model, env, hooks, permissions). Loaded on top of profile settings.',
  '.claude/settings.local.json': 'JSON. Project-local overrides (typically gitignored). Loaded last and wins over both profile and project settings.',
  '.mcp.json': 'JSON. Project-scope MCP servers — merged with the profile MCP servers when Claude runs in this cwd.',
};

async function openProjectFile(sessionName, relPath){
  _projFile = {sessionName, path:relPath, absPath:'', cwd:'', content:'', exists:false};
  const overlay = document.getElementById('projfile-overlay');
  overlay.classList.add('active');
  document.getElementById('projfile-title').firstChild.textContent =
    (_PROJFILE_LABELS[relPath] || relPath) + ' ';
  document.getElementById('projfile-banner').innerHTML =
    (_PROJFILE_DESCRIPTIONS[relPath] || '') + ' Session: <code>'+esc(sessionName)+'</code>';
  document.getElementById('projfile-label').textContent = relPath;
  document.getElementById('projfile-editor').value = 'Loading...';
  document.getElementById('projfile-path').textContent = '';
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(sessionName)
      +'/project-file?path='+encodeURIComponent(relPath));
    const data = await resp.json();
    if(!resp.ok){
      document.getElementById('projfile-editor').value = '';
      document.getElementById('projfile-path').textContent = data.error || 'Failed to load';
      return;
    }
    _projFile = {...data, sessionName};
    document.getElementById('projfile-editor').value = data.content || '';
    document.getElementById('projfile-path').textContent = data.abs_path
      + (data.exists ? '' : '  (will be created on save)');
  }catch(e){
    document.getElementById('projfile-editor').value = '';
    document.getElementById('projfile-path').textContent = 'Error loading';
  }
}

function closeProjectFile(){
  document.getElementById('projfile-overlay').classList.remove('active');
}

async function saveProjectFile(){
  if(!_projFile.sessionName || !_projFile.path) return;
  const content = document.getElementById('projfile-editor').value;
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(_projFile.sessionName)+'/project-file', {
      method:'PUT', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: _projFile.path, content})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to save'); return; }
    _projFile.exists = true;
    _projFile.absPath = data.abs_path;
    document.getElementById('projfile-path').textContent = data.abs_path;
    const ta = document.getElementById('projfile-editor');
    ta.style.borderColor = '#3fb950';
    setTimeout(()=>{ ta.style.borderColor=''; }, 600);
  }catch(e){ alert('Failed to save'); }
}

// ── Session MEMORY.md Editor (project auto-memory) ──
let _sessMem = {sessionName:'', path:'', dir:'', cwd:'', profileId:'', content:'', exists:false};
let _sessMemExtras = {files:[], editing:''};

async function openSessionMemory(sessionName){
  _sessMem = {sessionName, path:'', dir:'', cwd:'', profileId:'', content:'', exists:false};
  _sessMemExtras = {files:[], editing:''};
  document.getElementById('memorymd-overlay').classList.add('active');
  document.getElementById('memorymd-session-tag').textContent = '— '+sessionName;
  document.getElementById('memorymd-editor').value = 'Loading...';
  document.getElementById('memorymd-path').textContent = '';
  document.getElementById('memorymd-dir').textContent = '';
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(sessionName)+'/memory-md');
    const data = await resp.json();
    if(!resp.ok){
      document.getElementById('memorymd-editor').value = '';
      alert(data.error||'Failed to load MEMORY.md');
    } else {
      _sessMem = {...data, sessionName};
      document.getElementById('memorymd-editor').value = data.content || '';
      document.getElementById('memorymd-path').textContent = data.path;
      document.getElementById('memorymd-dir').textContent = data.dir;
      if(!data.exists){
        document.getElementById('memorymd-path').textContent = data.path + '  (will be created on save)';
      }
    }
  }catch(e){
    document.getElementById('memorymd-editor').value = 'Error loading MEMORY.md';
  }
  loadSessionMemoryExtras();
}

function closeSessionMemory(){
  document.getElementById('memorymd-overlay').classList.remove('active');
}

async function saveSessionMemory(){
  if(!_sessMem.sessionName) return;
  const content = document.getElementById('memorymd-editor').value;
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(_sessMem.sessionName)+'/memory-md', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({path: _sessMem.path, content})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to save'); return; }
    _sessMem.content = content;
    _sessMem.exists = true;
    document.getElementById('memorymd-path').textContent = _sessMem.path;
    const ta = document.getElementById('memorymd-editor');
    ta.style.borderColor = '#3fb950';
    setTimeout(()=>{ ta.style.borderColor=''; }, 600);
  }catch(e){ alert('Failed to save MEMORY.md'); }
}

async function loadSessionMemoryExtras(){
  _sessMemExtras = {files:[], editing:_sessMemExtras.editing||''};
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(_sessMem.sessionName)+'/memory-extras');
    const data = await resp.json();
    if(!resp.ok) throw new Error(data.error||'load failed');
    _sessMemExtras.files = data.files || [];
  }catch(e){
    _sessMemExtras.files = [];
  }
  renderSessionMemoryExtras();
}

function renderSessionMemoryExtras(){
  const el = document.getElementById('memorymd-extras');
  if(!el) return;
  const files = _sessMemExtras.files || [];
  let html = '';
  if(!files.length){
    html += '<div class="extras-empty">No topic files yet. MEMORY.md typically links to per-topic files (e.g. <code>read_click.md</code>) in the same directory.</div>';
  } else {
    files.forEach(f => {
      const isEditing = _sessMemExtras.editing === f.name;
      html += `<div class="extras-row">
        <span class="extras-name" onclick="toggleSessionMemoryExtra('${esc(f.name)}')">${esc(f.name)}</span>
        <span class="extras-meta">${f.size} bytes</span>
        <button class="extras-btn" onclick="toggleSessionMemoryExtra('${esc(f.name)}')">${isEditing?'Hide':'Edit'}</button>
        <button class="extras-del" onclick="deleteSessionMemoryExtra('${esc(f.name)}')" title="Delete">&times;</button>
      </div>
      <div class="extras-editor${isEditing?' active':''}">
        <textarea spellcheck="false" data-mem-extra="${esc(f.name)}">${esc(f.content||'')}</textarea>
        <div style="display:flex;gap:6px;justify-content:flex-end">
          <button class="extras-btn" onclick="saveSessionMemoryExtra('${esc(f.name)}')">Save changes</button>
        </div>
      </div>`;
    });
  }
  html += `<div class="extras-actions">
    <button class="extras-btn" onclick="addSessionMemoryExtra()">+ Add topic file</button>
  </div>`;
  el.innerHTML = html;
}

function toggleSessionMemoryExtra(name){
  _sessMemExtras.editing = (_sessMemExtras.editing === name) ? '' : name;
  renderSessionMemoryExtras();
}

async function addSessionMemoryExtra(){
  const raw = window.prompt('New topic filename (must end in .md):', 'topic_name.md');
  if(raw === null) return;
  let name = (raw||'').trim();
  if(!name) return;
  if(!name.toLowerCase().endsWith('.md')) name += '.md';
  if(name.toUpperCase()==='MEMORY.MD'){ alert('MEMORY.md has the dedicated editor above.'); return; }
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(_sessMem.sessionName)+'/memory-extras', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, content: ''})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to create file'); return; }
    _sessMemExtras.editing = data.name;
    await loadSessionMemoryExtras();
  }catch(e){ alert('Failed to create file.'); }
}

async function saveSessionMemoryExtra(name){
  const ta = document.querySelector('textarea[data-mem-extra="'+CSS.escape(name)+'"]');
  if(!ta) return;
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(_sessMem.sessionName)+'/memory-extras', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, content: ta.value})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to save'); return; }
    const f = (_sessMemExtras.files || []).find(x => x.name === name);
    if(f){ f.content = ta.value; f.size = new Blob([ta.value]).size; }
    ta.style.borderColor = '#3fb950';
    setTimeout(()=>{ ta.style.borderColor=''; }, 600);
  }catch(e){ alert('Failed to save.'); }
}

async function deleteSessionMemoryExtra(name){
  if(!confirm('Delete '+name+'?')) return;
  try{
    const resp = await fetch(BASE+'/api/sessions/'+encodeURIComponent(_sessMem.sessionName)+'/memory-extras/'+encodeURIComponent(name), {method:'DELETE'});
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to delete'); return; }
    if(_sessMemExtras.editing === name) _sessMemExtras.editing = '';
    await loadSessionMemoryExtras();
  }catch(e){ alert('Failed to delete.'); }
}

// ── CLAUDE.md Editor (global only) ──
let _claudeMdGlobal={path:'',content:'',exists:false};
let _globalExtras={files:[], editing:''};

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
  loadGlobalExtras();
}

async function loadGlobalExtras(){
  _globalExtras = {files:[], editing:_globalExtras.editing||''};
  try{
    const resp = await fetch(BASE+'/api/global-extras');
    const data = await resp.json();
    if(!resp.ok) throw new Error(data.error||'load failed');
    _globalExtras.files = data.files || [];
  }catch(e){
    _globalExtras.files = [];
  }
  renderGlobalExtras();
}

function renderGlobalExtras(){
  const el = document.getElementById('global-extras');
  if(!el) return;
  const files = _globalExtras.files || [];
  let html = '';
  if(!files.length){
    html += '<div class="extras-empty">No additional files. Add e.g. <code>CLAUDE_API_KEYS.md</code> and reference it from <code>CLAUDE.md</code>.</div>';
  } else {
    files.forEach(f => {
      const isEditing = _globalExtras.editing === f.name;
      html += `<div class="extras-row">
        <span class="extras-name" onclick="toggleGlobalExtra('${esc(f.name)}')">${esc(f.name)}</span>
        <span class="extras-meta">${f.size} bytes</span>
        <button class="extras-btn" onclick="toggleGlobalExtra('${esc(f.name)}')">${isEditing?'Hide':'Edit'}</button>
        <button class="extras-del" onclick="deleteGlobalExtra('${esc(f.name)}')" title="Delete">&times;</button>
      </div>
      <div class="extras-editor${isEditing?' active':''}">
        <textarea spellcheck="false" data-global-extra="${esc(f.name)}">${esc(f.content||'')}</textarea>
        <div style="display:flex;gap:6px;justify-content:flex-end">
          <button class="extras-btn" onclick="saveGlobalExtra('${esc(f.name)}')">Save changes</button>
        </div>
      </div>`;
    });
  }
  html += `<div class="extras-actions">
    <button class="extras-btn" onclick="addGlobalExtra()">+ Add CLAUDE_*.md file</button>
  </div>`;
  el.innerHTML = html;
}

function toggleGlobalExtra(name){
  _globalExtras.editing = (_globalExtras.editing === name) ? '' : name;
  renderGlobalExtras();
}

async function addGlobalExtra(){
  const raw = window.prompt('New filename (must start with CLAUDE_ and end in .md):', 'CLAUDE_GITHUB_RULES.md');
  if(raw === null) return;
  let name = (raw||'').trim();
  if(!name) return;
  if(!name.toLowerCase().endsWith('.md')) name += '.md';
  if(name.toUpperCase()==='CLAUDE.MD'){ alert('CLAUDE.md has a dedicated editor above.'); return; }
  try{
    const resp = await fetch(BASE+'/api/global-extras', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, content: ''})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to create file'); return; }
    _globalExtras.editing = data.name;
    await loadGlobalExtras();
  }catch(e){ alert('Failed to create file.'); }
}

async function saveGlobalExtra(name){
  const ta = document.querySelector('textarea[data-global-extra="'+CSS.escape(name)+'"]');
  if(!ta) return;
  try{
    const resp = await fetch(BASE+'/api/global-extras', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({name, content: ta.value})
    });
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to save'); return; }
    const f = (_globalExtras.files || []).find(x => x.name === name);
    if(f){ f.content = ta.value; f.size = new Blob([ta.value]).size; }
    ta.style.borderColor = '#3fb950';
    setTimeout(()=>{ ta.style.borderColor=''; }, 600);
  }catch(e){ alert('Failed to save.'); }
}

async function deleteGlobalExtra(name){
  if(!confirm('Delete '+name+'?')) return;
  try{
    const resp = await fetch(BASE+'/api/global-extras/'+encodeURIComponent(name), {method:'DELETE'});
    const data = await resp.json();
    if(!resp.ok){ alert(data.error||'Failed to delete'); return; }
    if(_globalExtras.editing === name) _globalExtras.editing = '';
    await loadGlobalExtras();
  }catch(e){ alert('Failed to delete.'); }
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

loadCurrentUser().then(applyRoleVisibility);
loadProfiles().then(()=>{ if(selectedSession) renderDetail(); });
loadAll();
checkClaudeAuth();
</script>
</body></html>
"""

# Inject the actual ROOT_PATH into the JS BASE variable
HTML_PAGE = HTML_PAGE.replace("__ROOT_PATH__", ROOT_PATH)
HTML_PAGE = HTML_PAGE.replace("__BRAND__", BRAND_NAME)
LOGIN_PAGE = LOGIN_PAGE.replace("__ROOT_PATH__", ROOT_PATH) if "__ROOT_PATH__" in LOGIN_PAGE else LOGIN_PAGE
LOGIN_PAGE = LOGIN_PAGE.replace("__BRAND__", BRAND_NAME)


# ===========================================================================
# Catch-all routes for public projects + per-user project lists. Registered
# LAST so every literal/api route takes precedence over these path params.
# ===========================================================================
_PROJECTS_PAGE_CSS = (
    "body{font-family:-apple-system,Segoe UI,Roboto,sans-serif;background:#0d1117;color:#e6edf3;"
    "max-width:820px;margin:40px auto;padding:0 20px}a{color:#58a6ff;text-decoration:none}"
    "a:hover{text-decoration:underline}h1{font-size:1.3rem}.muted{color:#8b949e;font-size:.85rem}"
    "li{margin:7px 0;list-style:none}ul{padding:0}.grp{margin-top:18px;color:#79c0ff;font-weight:600}"
    ".card{background:#161b22;border:1px solid #30363d;border-radius:10px;padding:18px 22px;margin-top:16px}"
    ".open{font-size:.72rem;color:#8b949e;border:1px solid #30363d;border-radius:4px;padding:1px 6px;margin-left:8px}"
)


def _projects_page_html(title: str, rows):
    items = "".join(
        '<li><a href="/%s/%s">dianaotech.com/%s/%s</a> <span class="open">open ↗</span></li>' % (u, p, u, p)
        for (u, p) in rows) or '<li class="muted">No projects yet. Ask Claude in a session to build something — it gets published here.</li>'
    return ("<!doctype html><html><head><meta charset=utf-8><title>%s · %s</title>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<style>%s</style></head><body><h1>%s</h1>"
            "<div class=card><ul>%s</ul></div>"
            "<p class=muted>%s — projects are served at dianaotech.com/&lt;user&gt;/&lt;project&gt;.</p>"
            "</body></html>") % (title, BRAND_NAME, _PROJECTS_PAGE_CSS, title, items, BRAND_NAME)


@app.get("/{username}", response_class=HTMLResponse)
async def user_projects_page(request: Request, username: str):
    if username in _RESERVED_TOP or "." in username:
        return HTMLResponse("Not found", status_code=404)
    target = _find_user_by_username(username)
    if not target:
        return HTMLResponse("Not found", status_code=404)
    viewer = _current_user(request)
    if not viewer:
        return HTMLResponse(LOGIN_PAGE, status_code=401)
    is_admin = _is_admin(viewer)
    if viewer.get("id") != target["id"] and not is_admin:
        return HTMLResponse("Forbidden — you can only view your own projects.", status_code=403)
    # An admin visiting an admin's page gets the master list of everyone's projects.
    if is_admin and _is_admin(target):
        rows = []
        for u in sorted(_load_users(), key=lambda x: x.get("username", "")):
            for proj in _list_projects(u.get("username", "")):
                rows.append((u["username"], proj))
        return HTMLResponse(_projects_page_html("All projects (admin)", rows))
    rows = [(username, proj) for proj in _list_projects(username)]
    return HTMLResponse(_projects_page_html(username + "'s projects", rows))


@app.api_route("/{username}/{project}", methods=["GET", "POST"])
@app.api_route("/{username}/{project}/{subpath:path}", methods=["GET", "POST"])
async def serve_project(request: Request, username: str, project: str, subpath: str = ""):
    if username in _RESERVED_TOP:
        return HTMLResponse("Not found", status_code=404)
    pdir = _project_dir(username, project)
    if pdir is None or not pdir.exists():
        return HTMLResponse("Project not found. (Served from ~/nemo-projects/%s/%s/)" % (username, project),
                            status_code=404)
    serve_cfg = pdir / ".serve.json"
    if serve_cfg.exists():
        try:
            port = int(json.loads(serve_cfg.read_text()).get("port", 0))
        except Exception:
            port = 0
        if port:
            return await _proxy_to_port(request, port, subpath)
    rel = subpath or "index.html"
    target = (pdir / rel).resolve()
    try:
        target.relative_to(pdir.resolve())
    except ValueError:
        return HTMLResponse("Forbidden", status_code=403)
    if target.is_dir():
        target = target / "index.html"
    if not target.exists():
        idx = pdir / "index.html"
        target = idx if idx.exists() else None
    if not target or not target.exists():
        return HTMLResponse("Not found", status_code=404)
    mime = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
    return FileResponse(str(target), media_type=mime)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
