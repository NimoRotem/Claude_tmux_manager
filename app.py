from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Dict
import glob as globmod

from fastapi import FastAPI, Request, Response, UploadFile, File
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
        pass
    return _stored_anthropic_key


def _save_anthropic_key(key: str):
    global _stored_anthropic_key
    _stored_anthropic_key = key
    try:
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        ANTHROPIC_API_KEY_FILE.write_text(key)
        ANTHROPIC_API_KEY_FILE.chmod(0o600)
    except Exception:
        pass


def _clear_anthropic_key():
    global _stored_anthropic_key
    _stored_anthropic_key = ""
    try:
        if ANTHROPIC_API_KEY_FILE.exists():
            ANTHROPIC_API_KEY_FILE.unlink()
    except Exception:
        pass


_load_anthropic_key()

app = FastAPI(root_path=ROOT_PATH)

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
async def auth_middleware(request: Request, call_next):
    # Skip auth entirely if no password is configured
    if not AUTH_PASS:
        return await call_next(request)
    path = request.url.path
    # Allow login routes without auth
    if path in ("/login", "/login/"):
        return await call_next(request)
    token = request.cookies.get("tmux_auth")
    if not _check_token(token):
        return HTMLResponse(LOGIN_PAGE)
    return await call_next(request)


@app.post("/login")
async def do_login(request: Request):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")
    if username == AUTH_USER and password == AUTH_PASS:
        token = _make_token(username)
        resp = RedirectResponse(url=request.scope.get("root_path", "") + "/", status_code=303)
        resp.set_cookie("tmux_auth", token, max_age=86400 * 30, httponly=True, samesite="lax")
        return resp
    return RedirectResponse(url=request.scope.get("root_path", "") + "/login?err=1", status_code=303)


# Three-tier cache per session
cache: Dict[str, dict] = {}

# Persistent message storage
MESSAGES_DIR = Path.home() / ".tmux-dashboard"
MESSAGES_FILE = MESSAGES_DIR / "messages.json"
NOTES_FILE = MESSAGES_DIR / "notes.json"


def _load_all_notes() -> Dict[str, str]:
    """Load all session notes from disk."""
    try:
        if NOTES_FILE.exists():
            return json.loads(NOTES_FILE.read_text())
    except Exception:
        pass
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
        pass


def _load_session_notes(session_name: str) -> str:
    """Get persisted notes for a specific session."""
    return _load_all_notes().get(session_name, "")


def _load_messages() -> Dict[str, list]:
    """Load all session messages from disk."""
    try:
        if MESSAGES_FILE.exists():
            return json.loads(MESSAGES_FILE.read_text())
    except Exception:
        pass
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
        pass


def _load_session_messages(session_name: str) -> list:
    """Get persisted messages for a specific session."""
    all_msgs = _load_messages()
    return all_msgs.get(session_name, [])


DESCRIPTION_TTL = 0    # never auto-expire
PROGRESS_TTL = 600     # 10 minutes
REALTIME_TTL = 60      # 1 minute
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
            sessions.append({
                "name": parts[0],
                "windows": parts[1] if len(parts) > 1 else "?",
                "created": parts[2] if len(parts) > 2 else "",
                "attached": parts[3] == "1" if len(parts) > 3 else False,
            })
        return sessions
    except Exception:
        return []


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
    """Get current pane line-count metadata (cheap, no content capture)."""
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-t", session_name, "-p",
             "#{history_size}:#{cursor_y}"],
            capture_output=True, text=True, timeout=3
        )
        if result.returncode == 0:
            parts = result.stdout.strip().split(":")
            history_size = int(parts[0])
            cursor_y = int(parts[1])
            return {"total_lines": history_size + cursor_y + 1}
    except Exception:
        pass
    return {"total_lines": 0}


# Track auto-approve state to avoid re-triggering
_auto_approve_sent: Dict[str, float] = {}


def _check_auto_approve(session_name: str, visible: str):
    """Detect Claude Code plan/permission prompts and auto-select option 2."""
    # Don't re-trigger within 10 seconds
    last = _auto_approve_sent.get(session_name, 0)
    if time.time() - last < 10:
        return

    lines = visible.split("\n")
    # Look for the option list pattern in the visible pane
    option2_line = -1
    selected_line = -1
    for i, line in enumerate(lines):
        stripped = line.strip()
        # Find the "2. Yes, and bypass" option
        if re.search(r'2\.\s+Yes.*bypass', stripped):
            option2_line = i
        # Find where the selector ❯ currently is
        if stripped.startswith('❯') or stripped.startswith('>'):
            selected_line = i

    if option2_line < 0 or selected_line < 0:
        return

    # Calculate how many Down presses needed
    downs = option2_line - selected_line
    if downs < 0:
        return  # Already past option 2, don't act

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
        pass


def detect_activity(session_name: str) -> dict:
    """Detect if session is busy or idle, and what command is running."""
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
        idle_prompt_patterns = [
            r'^[❯➜]\s*$',                         # bare prompt character alone on line
            r'Tip:.*claude',                       # claude code tip = just finished
            # Claude Code completion messages: "<verb> for <duration>"
            # Use a broad pattern to catch all verb variations (Churned, Sautéed, etc.)
            r'[A-Z][a-zé]+ for \d+[ms]',
        ]
        has_idle_prompt = False
        for pattern in idle_prompt_patterns:
            for line in bottom:
                if re.search(pattern, line.strip()):
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
        window_text = "\n".join(window)

        # All checks are LINE-BY-LINE.  Start-of-line anchoring is used
        # where possible to avoid false positives from these patterns
        # appearing in conversation output text.
        SPINNER_ICONS = r'[✶✽✻·\*☆◆●⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✢✦✧✹✵✴✸❋❊❉✺◇◈⟡⊛⊕⊗▸▹►▻◉◎★♦♢⬡⬢]'
        # Completion markers: "● Done" or "● Completed" = finished, NOT busy.
        # Must check these BEFORE spinner detection since ● is a spinner icon.
        COMPLETION_RE = re.compile(r'^●\s+(Done|Completed)\b')
        for line in window:
            stripped = line.strip()
            # Skip completion markers — these look like spinners but mean "finished"
            if COMPLETION_RE.match(stripped):
                continue
            # ◼ at start of line (with optional ⎿ tree prefix) = running task
            if re.match(r'^[⎿\s]*◼', stripped):
                info["status"] = "busy"
                info["detail"] = "Running task"
                return info
            # Spinner icon + verb… at START of line
            # Use … (ellipsis) or 2+ dots to avoid matching "Done." or other punctuation
            if re.match(SPINNER_ICONS + r'\s+\w+(?:…|\.{2,3})', stripped):
                info["status"] = "busy"
                if '(thinking)' in stripped or 'thought for' in stripped:
                    info["detail"] = "Thinking"
                else:
                    info["detail"] = "Working"
                return info
            # Spinner icon + verb… anywhere in line (catches inline spinners
            # after streamed text, e.g. "...Let me create it. ✢ Ebbing… (thought for 8s)")
            if re.search(SPINNER_ICONS + r'\s+\w+(?:…|\.{2,3})(?:\s*\(.*?\))?\s*$', stripped):
                info["status"] = "busy"
                if '(thinking)' in stripped or 'thought for' in stripped:
                    info["detail"] = "Thinking"
                else:
                    info["detail"] = "Working"
                return info
            # "(thought for Xs)" or "(thinking)" near end of line — strong busy signal
            if re.search(r'\(thought for \d+', stripped) or stripped.endswith('(thinking)'):
                info["status"] = "busy"
                info["detail"] = "Thinking"
                return info

        # --- Step 4: If idle prompt + no busy signals → truly idle ---
        if has_idle_prompt and not has_esc_to_interrupt:
            info["status"] = "idle"
            info["detail"] = "Waiting for input"
            return info

        # "esc to interrupt" without a spinner = background tasks running
        if has_esc_to_interrupt:
            info["status"] = "busy"
            info["detail"] = "Background tasks"
            return info

        # --- Step 4: Shell prompt check ---
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
            # Claude Code with no spinner + no "esc to interrupt" = idle
            info["status"] = "idle"
            info["detail"] = "Waiting for input"
        else:
            info["status"] = "busy"
            info["detail"] = cmd
    except Exception:
        pass
    return info


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
        chat_section = f"\n\n=== CHAT HISTORY (user commands & uploads) ===\n" + "\n".join(chat_lines)

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


async def get_realtime(session_name: str) -> str:
    recent = capture_pane_recent(session_name, 80)
    activity = detect_activity(session_name)
    status_hint = f"[Session is currently {activity['status'].upper()}"
    if activity["detail"]:
        status_hint += f" — {activity['detail']}"
    status_hint += "]"
    return await llm_call(
        system_prompt=(
            "You summarize the CURRENT STEP in a terminal session. Write 1-2 short sentences.\n\n"
            "Write as a collaborative team update using 'we' — like a coworker reporting progress.\n\n"
            "RULES:\n"
            "- Use first-person plural: 'We updated...', 'We're running...', 'We fixed...'.\n"
            "- NEVER start with 'User asked' or 'User requested'.\n"
            "- If BUSY: describe what's actively happening. Include progress % if visible.\n"
            "- If IDLE: describe what was accomplished. Use past tense.\n"
            "- Focus on the INTENT and RESULT — not shell commands or file paths.\n"
            "- Don't mention bash, curl, sleep, grep, cat, git commands — describe the goal.\n"
            "- Under 30 words.\n\n"
            "GOOD examples (busy):\n"
            "- 'Re-matching all URLs with the LLM pipeline — 64% done.'\n"
            "- 'We're adding dark mode support, currently editing the CSS theme variables.'\n"
            "GOOD examples (idle):\n"
            "- 'We fixed the auth token validation and restarted the server.'\n"
            "- 'Added a delete button with confirmation dialog and wired up the API endpoint.'\n"
            "BAD examples:\n"
            "- 'User asked to fix the login bug.' (don't say 'user asked')\n"
            "- 'Idle, waiting for input.' (too vague — what was just done?)\n"
            "- 'A bash process is executing a curl request...' (mechanics, not intent)"
        ),
        user_content=f"{status_hint}\n\ntmux session '{session_name}' latest output:\n\n{recent[-3000:]}",
        max_tokens=100,
    )


async def get_session_data(session_name: str, force_all: bool = False) -> dict:
    now = time.time()
    entry = cache.get(session_name, {})
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
    """Append an assistant message, skipping if too similar to the last one."""
    msgs = entry.setdefault("messages", [])
    # Find last assistant message
    for m in reversed(msgs):
        if m["role"] == "assistant":
            # Skip if identical or very similar (>70% word overlap)
            if m["text"] == text or _msg_similarity(m["text"], text) > 0.7:
                return
            break
    msgs.append({"role": "assistant", "text": text, "ts": ts})
    _save_messages()


def build_session_response(sess: dict, data: dict) -> dict:
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
    }


# --- Routes ---

@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML_PAGE


@app.get("/api/sessions")
async def api_sessions():
    sessions = get_tmux_sessions()
    results = await asyncio.gather(
        *[get_session_data(s["name"]) for s in sessions]
    )
    return JSONResponse([
        build_session_response(sess, data)
        for sess, data in zip(sessions, results)
    ])


@app.post("/api/sessions/{session_name}/refresh")
async def api_refresh_session(session_name: str):
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    now = time.time()
    entry = cache.get(session_name, {})
    if "messages" not in entry:
        entry["messages"] = _load_session_messages(session_name)
    entry["realtime"] = await get_realtime(session_name)
    entry["realtime_at"] = now
    _append_assistant_msg(entry, entry["realtime"], now)
    cache[session_name] = entry

    sess = next(s for s in sessions if s["name"] == session_name)
    return JSONResponse(build_session_response(sess, entry))


@app.post("/api/sessions/{session_name}/refresh-all")
async def api_refresh_all_tiers(session_name: str):
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    entry = await get_session_data(session_name, force_all=True)
    sess = next(s for s in sessions if s["name"] == session_name)
    return JSONResponse(build_session_response(sess, entry))


@app.get("/api/status")
async def api_status():
    """Lightweight: return only activity status per session, no LLM calls."""
    sessions = get_tmux_sessions()
    out = []
    for sess in sessions:
        activity = detect_activity(sess["name"])
        out.append({
            "name": sess["name"],
            "activity_status": activity["status"],
            "activity_detail": activity["detail"],
        })
    return JSONResponse(out)


@app.get("/api/sessions/{session_name}/raw")
async def api_raw_output(session_name: str):
    """Return raw scrollback content for a session."""
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    raw = capture_pane_full(session_name)
    activity = detect_activity(session_name)
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
    sessions_list = get_tmux_sessions()
    names = [s["name"] for s in sessions_list]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    pos = get_pane_position(session_name)
    current_total = pos["total_lines"]

    # First load or session reset → full capture
    if known_lines <= 0 or known_lines > current_total:
        raw = capture_pane_full(session_name)
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
    raw = capture_pane_recent(session_name, lines_from_end)
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
        return JSONResponse({"ok": True, "name": created})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.delete("/api/sessions/{session_name}")
async def api_delete_session(session_name: str):
    """Kill a tmux session and all its child processes."""
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
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
                        pass
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
                        pass
        except Exception:
            pass  # Non-fatal — tmux kill-session will still clean up

        result = subprocess.run(
            ["tmux", "kill-session", "-t", session_name],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode != 0:
            return JSONResponse({"error": result.stderr.strip() or "Failed to kill session"}, status_code=500)
        # Clean up cache
        cache.pop(session_name, None)
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
        pass
    return ""


@app.post("/api/sessions/{session_name}/upload")
async def api_upload_file(session_name: str, file: UploadFile = File(...)):
    """Upload a file to the session's current working directory."""
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)

    cwd = get_session_cwd(session_name)
    if not cwd:
        return JSONResponse({"error": "Could not determine session working directory"}, status_code=500)

    # Sanitize filename — keep only the basename
    filename = os.path.basename(file.filename or "upload")
    if not filename or filename.startswith("."):
        return JSONResponse({"error": "Invalid filename"}, status_code=400)

    dest = os.path.join(cwd, filename)
    try:
        content = await file.read()
        with open(dest, "wb") as f:
            f.write(content)
        # Record in chat history
        size_kb = len(content) / 1024
        note = f"Uploaded {filename} ({size_kb:.1f} KB) to {cwd}"
        now = time.time()
        entry = cache.setdefault(session_name, {})
        if "messages" not in entry:
            entry["messages"] = _load_session_messages(session_name)
        entry["messages"].append({"role": "user", "text": note, "ts": now})
        _save_messages()
        return JSONResponse({"ok": True, "path": dest, "size": len(content)})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


# --- CLAUDE.md viewer/editor ---

@app.get("/api/sessions/{session_name}/claude-md")
async def api_get_claude_md(session_name: str):
    """Read CLAUDE.md from the session's working directory and home dir."""
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
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
                pass
        results.append({"path": md_path, "content": content, "exists": os.path.exists(md_path), "label": "Project"})
    # Check home dir
    home_md = os.path.join(str(Path.home()), "CLAUDE.md")
    home_content = ""
    if os.path.exists(home_md):
        try:
            with open(home_md) as f:
                home_content = f.read()
        except Exception:
            pass
    results.append({"path": home_md, "content": home_content, "exists": os.path.exists(home_md), "label": "Global"})
    return JSONResponse({"files": results, "cwd": cwd or ""})


class SaveClaudeMd(BaseModel):
    path: str
    content: str


@app.post("/api/sessions/{session_name}/claude-md")
async def api_save_claude_md(session_name: str, body: SaveClaudeMd):
    """Save CLAUDE.md to the specified path."""
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    # Safety: only allow writing CLAUDE.md files
    if not body.path.endswith("CLAUDE.md"):
        return JSONResponse({"error": "Can only write CLAUDE.md files"}, status_code=400)
    try:
        os.makedirs(os.path.dirname(body.path), exist_ok=True)
        with open(body.path, "w") as f:
            f.write(body.content)
        return JSONResponse({"ok": True, "path": body.path})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
    except Exception:
        stats["cpu_load"] = {}
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


# --- Claude Code auth management ---

@app.get("/api/auth/claude-status")
async def api_claude_auth_status():
    result_data: dict = {"loggedIn": False, "hasApiKey": bool(_stored_anthropic_key)}
    try:
        result = subprocess.run(
            ["claude", "auth", "status", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode == 0 and result.stdout.strip():
            auth_info = json.loads(result.stdout.strip())
            auth_info["hasApiKey"] = bool(_stored_anthropic_key)
            return JSONResponse(auth_info)
    except Exception as e:
        result_data["error"] = str(e)
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

    from datetime import datetime, timezone
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
            pass

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


class SendCommand(BaseModel):
    command: str


@app.post("/api/sessions/{session_name}/send")
async def api_send_command(session_name: str, body: SendCommand):
    """Send keystrokes to a tmux session, as if typed at the terminal."""
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        cmd_text = body.command
        if len(cmd_text) > 200:
            # For long messages, use tmux load-buffer + paste-buffer
            # This avoids command-line length limits and ensures reliable delivery
            with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as tmp:
                tmp.write(cmd_text)
                tmp_path = tmp.name
            try:
                subprocess.run(
                    ["tmux", "load-buffer", tmp_path],
                    capture_output=True, text=True, timeout=5
                )
                subprocess.run(
                    ["tmux", "paste-buffer", "-t", session_name],
                    capture_output=True, text=True, timeout=5
                )
            finally:
                os.unlink(tmp_path)
        else:
            # Short messages: send-keys -l is fine
            subprocess.run(
                ["tmux", "send-keys", "-t", session_name, "-l", cmd_text],
                capture_output=True, text=True, timeout=5
            )
        # Then press Enter as a separate key event
        subprocess.run(
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
    sessions_list = get_tmux_sessions()
    names = [s["name"] for s in sessions_list]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "Escape"],
            capture_output=True, text=True, timeout=5
        )
        return JSONResponse({"ok": True, "action": "interrupt"})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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
.btn-danger{background:#21262d;color:#f85149;border:1px solid #f8514944}
.btn-danger:hover{background:#3d1214}

/* Tabs */
.tab-bar{display:flex;border-bottom:1px solid #21262d}
.tab{padding:10px 20px;font-size:.85rem;font-weight:500;color:#8b949e;cursor:pointer;border-bottom:2px solid transparent;transition:color .15s,border-color .15s;user-select:none}
.tab:hover{color:#c9d1d9}
.tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
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
.cmd-upload{border:none;border-left:1px solid #30363d;border-radius:0 6px 6px 0;padding:12px 14px;font-size:1.1rem;cursor:pointer;background:#21262d;color:#8b949e;align-self:flex-end;line-height:1;transition:color .15s}
.cmd-upload:hover{color:#58a6ff}
.cmd-expand{border:none;border-left:1px solid #30363d;border-radius:0;padding:12px 10px;font-size:.85rem;cursor:pointer;background:#21262d;color:#8b949e;align-self:flex-end;line-height:1;transition:color .15s}
.cmd-expand:hover{color:#58a6ff}
.cmd-slash{border:none;border-left:1px solid #30363d;border-radius:0;padding:12px 12px;font-size:.85rem;cursor:pointer;background:#21262d;color:#d2a8ff;align-self:flex-end;line-height:1;transition:all .15s;font-weight:600;font-family:'SF Mono','Fira Code',Consolas,monospace}
.cmd-slash:hover{background:#1c2128;color:#f0f6fc}
.cmd-interrupt{display:none;border:none;border-left:1px solid #30363d;border-radius:0;padding:12px 14px;font-size:.8rem;cursor:pointer;background:#da3633;color:#fff;align-self:flex-end;line-height:1;transition:all .15s;font-weight:600;letter-spacing:.03em;white-space:nowrap}
.cmd-interrupt:hover{background:#f85149}
.cmd-interrupt.visible{display:block}
/* Slash commands dropdown */
.slash-dropdown{display:none;position:absolute;bottom:100%;right:0;z-index:50;background:#161b22;border:1px solid #30363d;border-radius:8px;padding:6px 0;min-width:260px;box-shadow:0 8px 24px rgba(0,0,0,.5);margin-bottom:4px}
.slash-dropdown.active{display:block}
.slash-item{display:flex;align-items:center;gap:10px;padding:8px 14px;cursor:pointer;transition:background .1s;font-size:.85rem;color:#c9d1d9}
.slash-item:hover{background:#1c2128}
.slash-item-cmd{color:#d2a8ff;font-family:'SF Mono','Fira Code',Consolas,monospace;font-weight:600;min-width:80px}
.slash-item-desc{color:#8b949e;font-size:.75rem}

/* Raw tab */
.tab-raw{padding-top:16px}
.raw-controls{display:flex;align-items:center;gap:10px;margin-bottom:8px}
.raw-info{color:#6e7681;font-size:.75rem;flex-shrink:0}
.raw-title{flex:1;min-width:0;color:#8b949e;font-size:.8rem;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;text-align:center}
.raw-output{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;font-family:'SF Mono','Fira Code','Cascadia Code',Consolas,monospace;font-size:.8rem;line-height:1.45;color:#c9d1d9;flex:1;min-height:120px;max-height:calc(100vh - 280px);overflow-y:auto;white-space:pre;word-wrap:normal;overflow-x:auto}
.raw-output::-webkit-scrollbar{width:6px;height:6px}
.raw-output::-webkit-scrollbar-track{background:#0d1117}
.raw-output::-webkit-scrollbar-thumb{background:#30363d;border-radius:3px}

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

/* CLAUDE.md editor modal */
.claudemd-overlay{display:none;position:fixed;top:0;left:0;right:0;bottom:0;background:rgba(0,0,0,.6);z-index:200;align-items:flex-start;justify-content:center;padding-top:40px}
.claudemd-overlay.active{display:flex}
.claudemd-panel{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:24px;width:700px;max-width:calc(100vw - 32px);max-height:calc(100vh - 80px);overflow-y:auto;box-shadow:0 8px 24px rgba(0,0,0,.5);display:flex;flex-direction:column}
.claudemd-panel h3{color:#f0f6fc;margin-bottom:12px;font-size:1.1rem;display:flex;justify-content:space-between;align-items:center}
.claudemd-tabs{display:flex;gap:0;border-bottom:1px solid #21262d;margin-bottom:12px}
.claudemd-tab{padding:8px 16px;font-size:.8rem;color:#8b949e;cursor:pointer;border-bottom:2px solid transparent;transition:all .15s}
.claudemd-tab:hover{color:#c9d1d9}
.claudemd-tab.active{color:#58a6ff;border-bottom-color:#58a6ff}
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
  .modal{min-width:280px;margin:0 16px}
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
  <span class="nav-status-text" id="status-info">Watching for changes...</span>
  <button class="nav-icon-btn" onclick="openStats()" title="System Stats"><span class="icon">&#x1F4CA;</span></button>
  <button class="nav-icon-btn" onclick="openClaudeMd()" title="CLAUDE.md"><span class="icon">&#x1F4DD;</span></button>
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
    <h3>CLAUDE.md <button class="stats-close" onclick="closeClaudeMd()">&times;</button></h3>
    <div class="claudemd-tabs" id="claudemd-tabs"></div>
    <div class="claudemd-path" id="claudemd-path"></div>
    <textarea class="claudemd-editor" id="claudemd-editor" spellcheck="false"></textarea>
    <div class="claudemd-actions">
      <button class="btn" onclick="closeClaudeMd()">Cancel</button>
      <button class="btn btn-full" onclick="saveClaudeMd()">Save</button>
    </div>
  </div>
</div>

<script>
const navEl=document.getElementById('top-nav');
const mainEl=document.getElementById('main');
const statusInfoEl=document.getElementById('status-info');
const BASE='/tmux';
let sessions=[];
let selectedSession=null;
let pollTimer=null;
const activeTabs={};
const rawState={};
function getRawState(n){if(!rawState[n])rawState[n]={polling:false,timer:null,knownLines:0};return rawState[n]}
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
      <span class="nav-title" id="nav-title-${s.name}">${esc(s.title)||'Loading...'}</span>
      <span class="nav-indicators">
        <span class="nav-dot ${esc(s.activity_status)}" id="nav-dot-${s.name}"></span>
        <span class="nav-attached ${s.attached?'yes':'no'}">${s.attached?'A':'D'}</span>
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
  const tab=activeTabs[s.name]||'chat';
  // Sync server messages into local store (merge, don't replace — preserves
  // messages added locally from raw tab that server hasn't echoed back yet)
  if(s.messages && s.messages.length) mergeChatMessages(s.name, s.messages);
  // Update favicon to match selected session
  updateFavicon(s.activity_status);

  mainEl.innerHTML=`
    <div class="tab-bar">
      <div class="tab ${tab==='chat'?'active':''}" onclick="switchTab('${s.name}','chat')">Chat</div>
      <div class="tab ${tab==='raw'?'active':''}" onclick="switchTab('${s.name}','raw')">Raw Output</div>
      <div class="tab ${tab==='info'?'active':''}" onclick="switchTab('${s.name}','info')">Info</div>
      <div class="detail-badges">
        <span class="status-pill ${esc(s.activity_status)}" id="status-${s.name}">
          <span class="status-dot"></span>
          <span class="status-label">${statusLabel(s.activity_status)}</span>
          ${s.activity_detail?'<span style="font-weight:400;opacity:.7"> &middot; '+esc(s.activity_detail)+'</span>':''}
        </span>
        <span class="badge ${s.attached?'attached':'detached'}">${s.attached?'attached':'detached'}</span>
        <button class="btn btn-danger" onclick="showDeleteModal('${esc(s.name)}')" title="Kill session">Delete</button>
      </div>
    </div>

    <div class="tab-content ${tab==='chat'?'active':''}" id="tab-chat-${s.name}">
      <div class="chat-wrap">
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
            autocomplete="off" spellcheck="false"></textarea>
          <button class="btn cmd-send" onclick="sendChat('${s.name}')">Send</button>
          <button class="cmd-interrupt ${s.activity_status==='busy'?'visible':''}" id="interrupt-chat-${s.name}" onclick="interruptSession('${s.name}')" title="Interrupt Claude (Esc)">Stop</button>
          <button class="cmd-slash" onclick="toggleSlashMenu(event,'slash-chat-${s.name}')" title="Slash commands">/</button>
          <button class="cmd-expand" onclick="toggleExpand('cmd-chat-${s.name}')" title="Expand/collapse">&#x2195;</button>
          <button class="cmd-upload" onclick="document.getElementById('upload-${s.name}').click()" title="Upload file">&#x1F4CE;</button>
          <input type="file" id="upload-${s.name}" style="display:none" onchange="uploadFile('${s.name}',this)" multiple>
          <div class="slash-dropdown" id="slash-chat-${s.name}"></div>
        </div>
      </div>
    </div>

    <div class="tab-content tab-raw ${tab==='raw'?'active':''}" id="tab-raw-${s.name}">
      <div class="raw-controls">
        <span class="raw-info" id="raw-info-${s.name}">Click to load raw output</span>
        <span class="raw-title" id="raw-title-${s.name}">${esc(s.title)||''}</span>
        <button class="btn" onclick="loadRaw('${s.name}')">Reload</button>
      </div>
      <div class="raw-output" id="raw-${s.name}">Click "Raw Output" tab or "Reload" to fetch...</div>
      <div class="cmd-bar" style="position:relative">
        <span class="cmd-prompt">$</span>
        <textarea class="cmd-input" id="cmd-raw-${s.name}" rows="1"
          placeholder="Type a command and press Enter..."
          onkeydown="handleRawKey(event,'${s.name}')"
          oninput="autoGrow(this)"
          autocomplete="off" spellcheck="false"></textarea>
        <button class="btn cmd-send" onclick="sendCmd('${s.name}','raw')">Send</button>
        <button class="cmd-interrupt ${s.activity_status==='busy'?'visible':''}" id="interrupt-raw-${s.name}" onclick="interruptSession('${s.name}')" title="Interrupt Claude (Esc)">Stop</button>
        <button class="cmd-slash" onclick="toggleSlashMenu(event,'slash-raw-${s.name}')" title="Slash commands">/</button>
        <button class="cmd-expand" onclick="toggleExpand('cmd-raw-${s.name}')" title="Expand/collapse">&#x2195;</button>
        <button class="cmd-upload" onclick="document.getElementById('upload-raw-${s.name}').click()" title="Upload file">&#x1F4CE;</button>
        <input type="file" id="upload-raw-${s.name}" style="display:none" onchange="uploadFile('${s.name}',this)" multiple>
        <div class="slash-dropdown" id="slash-raw-${s.name}"></div>
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
  if(tab==='raw')startRawPolling(s.name);
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
  if(tabBar)tabBar.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));
  const tabNames=['chat','raw','info'];
  const idx=tabNames.indexOf(tab);
  if(tabBar&&idx>=0){
    const tabs=tabBar.querySelectorAll('.tab');
    if(tabs[idx])tabs[idx].classList.add('active');
  }
  const target=document.getElementById('tab-'+tab+'-'+name);
  if(target)target.classList.add('active');
  stopAllRawPolling();
  if(tab==='raw')startRawPolling(name);
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
      // Remove typing indicator if present
      const typing=chatEl.querySelector('.chat-typing');
      if(typing)typing.remove();
      const bubble=document.createElement('div');
      bubble.className='chat-msg '+role;
      bubble.innerHTML=esc(text)+'<div class="chat-meta">'+fmtTime(ts)+'</div>';
      chatEl.appendChild(bubble);
      chatEl.scrollTop=chatEl.scrollHeight;
    }
  }
}

function autoGrow(el){
  if(el.classList.contains('expanded'))return;
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,400)+'px';
}
function toggleExpand(id){
  const el=document.getElementById(id);
  if(!el)return;
  if(el.classList.contains('expanded')){
    el.classList.remove('expanded');
    el.style.height='auto';
    el.style.height=Math.min(el.scrollHeight,400)+'px';
  }else{
    el.classList.add('expanded');
    el.style.height='300px';
  }
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

async function uploadFile(name,input){
  if(!input.files||!input.files.length)return;
  for(const file of input.files){
    const fd=new FormData();
    fd.append('file',file);
    const sizeKb=(file.size/1024).toFixed(1);
    appendChatBubble(name,'user',`Uploading ${file.name} (${sizeKb} KB)...`,Date.now()/1000);
    try{
      const resp=await fetch(BASE+'/api/sessions/'+name+'/upload',{method:'POST',body:fd});
      const data=await resp.json();
      if(!resp.ok){
        appendChatBubble(name,'assistant','Upload failed: '+(data.error||'Unknown error'),Date.now()/1000);
      }
    }catch(e){
      appendChatBubble(name,'assistant','Upload failed: network error',Date.now()/1000);
    }
  }
  input.value='';
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
    if(source==='raw')setTimeout(()=>pollRawDelta(name),500);
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
  st.timer=setInterval(()=>pollRawDelta(name),2000);
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
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/raw-tail?known_lines='+st.knownLines);
    const data=await resp.json();
    if(data.mode==='full'){
      rawEl.textContent=data.raw||'(empty)';
      st.knownLines=data.pane_total;
      rawEl.scrollTop=rawEl.scrollHeight;
      if(infoEl)infoEl.textContent=data.total_lines+' lines';
    }else if(data.mode==='delta'&&data.raw){
      const wasAtBottom=(rawEl.scrollHeight-rawEl.scrollTop-rawEl.clientHeight)<30;
      const newLines=data.raw.split('\n');
      const curText=rawEl.textContent;
      const existingLines=curText.split('\n');
      // Deduplicate using overlap
      let appendFrom=0;
      if(data.overlap&&existingLines.length>=data.overlap){
        const tail=existingLines.slice(-data.overlap).join('\n');
        const head=newLines.slice(0,data.overlap).join('\n');
        if(tail===head)appendFrom=data.overlap;
      }
      const toAppend=newLines.slice(appendFrom).join('\n');
      if(toAppend){
        rawEl.textContent=curText+'\n'+toAppend;
      }
      st.knownLines=data.pane_total;
      if(infoEl)infoEl.textContent=data.total_lines+' lines';
      if(wasAtBottom)rawEl.scrollTop=rawEl.scrollHeight;
    }
    // mode==='none': nothing to do
  }catch(e){}
}

async function loadRaw(name){
  const st=getRawState(name);
  st.knownLines=0;
  const rawEl=document.getElementById('raw-'+name);
  if(rawEl)rawEl.textContent='Loading...';
  await pollRawDelta(name);
}

function updateStatusPill(name,status,detail){
  const pill=document.getElementById('status-'+name);
  if(pill){
    pill.className='status-pill '+(status||'unknown');
    pill.innerHTML='<span class="status-dot"></span><span class="status-label">'+statusLabel(status)+'</span>'
      +(detail?'<span style="font-weight:400;opacity:.7"> &middot; '+esc(detail)+'</span>':'');
  }
  toggleInterruptButtons(name,status==='busy');
  const navDot=document.getElementById('nav-dot-'+name);
  if(navDot)navDot.className='nav-dot '+(status||'unknown');
}

function updateCard(s){
  const navTitle=document.getElementById('nav-title-'+s.name);
  if(navTitle&&s.title)navTitle.textContent=s.title;

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
    const resp=await fetch(BASE+'/api/sessions');
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
}

async function refreshAllRealtime(){
  for(const s of sessions){refreshOne(s.name)}
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
    }
    if(!changed)statusInfoEl.textContent='Watching for changes...';
  }catch(e){statusInfoEl.textContent='Status poll failed'}
  _authPollCount++;
  if(_authPollCount%5===0)checkClaudeAuth();
}

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
  // Close any open slash dropdowns
  document.querySelectorAll('.slash-dropdown.active').forEach(sd=>{
    if(!sd.contains(e.target)&&!e.target.classList.contains('cmd-slash')){
      sd.classList.remove('active');
    }
  });
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

// ── Slash Commands ──
const SLASH_COMMANDS=[
  {cmd:'/clear',desc:'Wipe conversation, start fresh'},
  {cmd:'/compact',desc:'Summarize older context to save tokens'},
  {cmd:'/context',desc:'Show context window usage'},
  {cmd:'/cost',desc:'Show current session cost'},
  {cmd:'/usage',desc:'Check rate limit status'},
  {cmd:'/model sonnet',desc:'Switch to Sonnet (faster)'},
  {cmd:'/model opus',desc:'Switch to Opus (stronger)'},
  {cmd:'/plan',desc:'Toggle plan mode for complex tasks'},
];

function toggleSlashMenu(event,dropdownId){
  event.stopPropagation();
  // Close all other slash dropdowns
  document.querySelectorAll('.slash-dropdown.active').forEach(sd=>{
    if(sd.id!==dropdownId)sd.classList.remove('active');
  });
  const dd=document.getElementById(dropdownId);
  if(!dd)return;
  if(!dd.innerHTML){
    dd.innerHTML=SLASH_COMMANDS.map(c=>
      `<div class="slash-item" data-cmd="${esc(c.cmd)}">
        <span class="slash-item-cmd">${esc(c.cmd)}</span>
        <span class="slash-item-desc">${esc(c.desc)}</span>
      </div>`
    ).join('');
    dd.querySelectorAll('.slash-item').forEach(item=>{
      item.addEventListener('click',function(){
        const cmd=this.dataset.cmd;
        // Find which session this belongs to
        const name=selectedSession;
        if(!name)return;
        dd.classList.remove('active');
        // Send the slash command
        sendSlashCommand(name,cmd);
      });
    });
  }
  dd.classList.toggle('active');
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

// ── CLAUDE.md Editor ──
let _claudeMdFiles=[];
let _claudeMdActiveIdx=0;

async function openClaudeMd(){
  if(!selectedSession){alert('Select a session first.');return}
  const overlay=document.getElementById('claudemd-overlay');
  overlay.classList.add('active');
  document.getElementById('claudemd-editor').value='Loading...';
  document.getElementById('claudemd-path').textContent='';
  try{
    const resp=await fetch(BASE+'/api/sessions/'+selectedSession+'/claude-md');
    const data=await resp.json();
    _claudeMdFiles=data.files||[];
    _claudeMdActiveIdx=0;
    renderClaudeMdTabs();
    showClaudeMdFile(0);
  }catch(e){
    document.getElementById('claudemd-editor').value='Error loading CLAUDE.md';
  }
}

function renderClaudeMdTabs(){
  const tabsEl=document.getElementById('claudemd-tabs');
  tabsEl.innerHTML=_claudeMdFiles.map((f,i)=>
    `<div class="claudemd-tab ${i===_claudeMdActiveIdx?'active':''}" onclick="showClaudeMdFile(${i})">${esc(f.label)}${f.exists?'':' (new)'}</div>`
  ).join('');
}

function showClaudeMdFile(idx){
  _claudeMdActiveIdx=idx;
  const f=_claudeMdFiles[idx];
  if(!f)return;
  document.getElementById('claudemd-editor').value=f.content||'';
  document.getElementById('claudemd-path').textContent=f.path;
  renderClaudeMdTabs();
}

async function saveClaudeMd(){
  const f=_claudeMdFiles[_claudeMdActiveIdx];
  if(!f)return;
  const content=document.getElementById('claudemd-editor').value;
  try{
    const resp=await fetch(BASE+'/api/sessions/'+selectedSession+'/claude-md',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({path:f.path,content})
    });
    const data=await resp.json();
    if(!resp.ok){alert(data.error||'Failed to save');return}
    f.content=content;
    f.exists=true;
    renderClaudeMdTabs();
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
    const resp=await fetch(BASE+'/api/stats');
    const s=await resp.json();
    renderStats(s);
  }catch(e){
    document.getElementById('stats-content').innerHTML='<div style="color:#f85149">Failed to load stats.</div>';
  }
}

function renderStats(s){
  let html='';
  // Server
  html+='<div class="stats-section"><div class="stats-section-title">Server</div>';
  html+='<div class="stats-row"><span class="stats-row-label">Uptime</span><span class="stats-row-value">'+esc(s.uptime||'—')+'</span></div>';
  if(s.cpu_load&&s.cpu_load['1m']){
    html+='<div class="stats-row"><span class="stats-row-label">CPU Load</span><span class="stats-row-value">'+esc(s.cpu_load['1m'])+' / '+esc(s.cpu_load['5m'])+' / '+esc(s.cpu_load['15m'])+'</span></div>';
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

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
