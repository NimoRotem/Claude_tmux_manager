from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import subprocess
import time
from pathlib import Path
from typing import Dict

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel
import openai
import uvicorn

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
PORT = int(os.environ.get("TMUX_DASH_PORT", "8501"))
ROOT_PATH = os.environ.get("TMUX_DASH_ROOT_PATH", "/tmux")
NEW_SESSION_CMD = os.environ.get("TMUX_DASH_NEW_SESSION_CMD", "")  # e.g. "claude"

client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

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
            r'Saut[ée]ed for',                     # "Sautéed for Xs" = just finished
            r'Whisked for',
            r'Warped for',
            r'Worked for',
        ]
        has_idle_prompt = False
        for pattern in idle_prompt_patterns:
            for line in bottom:
                if re.search(pattern, line.strip()):
                    has_idle_prompt = True
                    break
            if has_idle_prompt:
                break

        # If idle prompt present but NO "esc to interrupt" → truly idle
        if has_idle_prompt and not has_esc_to_interrupt:
            info["status"] = "idle"
            info["detail"] = "Waiting for input"
            return info

        # --- Step 3: Check for active spinners/progress on bottom lines ---
        busy_patterns = [
            (r'Generating[….]',                     "Generating"),
            (r'Reticulating[….]',                   "Thinking"),
            (r'Whisking[….]',                       "Thinking"),
            (r'Warping[….]',                        "Thinking"),
            (r'Saut[ée]ing[….]',                    "Thinking"),
            (r'Running[….]',                        "Running"),
            (r'thought for \d+s\)',                  "Thinking"),
            (r'thinking\)',                          "Thinking"),
            (r'\d+m \d+s · ↓',                      "Processing"),
            (r'Installing',                         "Installing"),
            (r'Building',                           "Building"),
            (r'Compiling',                          "Compiling"),
            (r'Downloading',                        "Downloading"),
        ]
        for pattern, label in busy_patterns:
            if re.search(pattern, bottom_text):
                info["status"] = "busy"
                info["detail"] = label
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

    need_description = force_all or "description" not in entry
    need_progress = force_all or "progress" not in entry or (now - entry.get("progress_at", 0)) >= PROGRESS_TTL

    full_output = None
    if need_description or need_progress:
        full_output = capture_pane_full(session_name)

    tasks = {}
    if need_description:
        tasks["title_desc"] = get_title_and_description(session_name, full_output)
    if need_progress:
        tasks["progress"] = get_progress(session_name, full_output)
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
        if "realtime" in result_map:
            entry["realtime"] = result_map["realtime"]
            entry["realtime_at"] = now
            _append_assistant_msg(entry, result_map["realtime"], now)

    cache[session_name] = entry
    if entry.get("messages"):
        _save_messages()
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
    """Kill a tmux session."""
    sessions = get_tmux_sessions()
    names = [s["name"] for s in sessions]
    if session_name not in names:
        return JSONResponse({"error": "Session not found"}, status_code=404)
    try:
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
        # -l sends text literally (no key-name interpretation)
        subprocess.run(
            ["tmux", "send-keys", "-t", session_name, "-l", body.command],
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



HTML_PAGE = r"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0">
<title>tmux Dashboard</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#0f1117;color:#e1e4e8;min-height:100vh;display:flex;flex-direction:column}

/* Nav bar */
.top-nav{background:#161b22;border-bottom:1px solid #30363d;padding:0 24px;display:flex;align-items:center;gap:0;overflow-x:auto;flex-shrink:0}
.top-nav::-webkit-scrollbar{height:0}
.nav-brand{font-size:.85rem;font-weight:700;color:#58a6ff;padding:12px 16px 12px 0;border-right:1px solid #30363d;margin-right:4px;white-space:nowrap;user-select:none}
.nav-item{display:flex;align-items:center;gap:8px;padding:10px 16px;cursor:pointer;border-bottom:2px solid transparent;transition:background .15s,border-color .15s;white-space:nowrap;user-select:none}
.nav-item:hover{background:#1c2128}
.nav-item.active{border-bottom-color:#58a6ff;background:#1c2128}
.nav-session-id{font-size:.75rem;font-weight:700;color:#8b949e;background:#21262d;padding:1px 6px;border-radius:4px;min-width:20px;text-align:center}
.nav-item.active .nav-session-id{color:#58a6ff;background:#1c2333}
.nav-title{font-size:.8rem;color:#c9d1d9;max-width:180px;overflow:hidden;text-overflow:ellipsis}
.nav-indicators{display:flex;align-items:center;gap:5px}
.nav-dot{width:7px;height:7px;border-radius:50%;flex-shrink:0}
.nav-dot.busy{background:#f85149;animation:pulse-glow 1.5s ease-in-out infinite}
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

/* Detail header */
.detail-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:12px;gap:12px}
.detail-header-left{display:flex;align-items:center;gap:10px;min-width:0;flex:1}
.detail-title-text{font-size:1.3rem;font-weight:600;color:#f0f6fc;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;min-width:0}
.detail-badges{display:flex;gap:8px;align-items:center;flex-shrink:0}
.status-pill{font-size:.75rem;padding:3px 12px;border-radius:12px;font-weight:600;display:flex;align-items:center;gap:5px}
.status-pill.busy{background:#f8514922;color:#f85149;border:1px solid #f8514944}
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
.chat-typing{align-self:flex-start;padding:10px 14px;background:#161b22;border:1px solid #30363d;border-radius:12px;border-bottom-left-radius:4px;color:#8b949e;font-size:.85rem;font-style:italic}
.chat-typing .dots{display:inline-block;animation:typing 1.4s infinite}
@keyframes typing{0%,80%,100%{opacity:.3}40%{opacity:1}}

/* Command bar */
.cmd-bar{display:flex;align-items:flex-end;gap:0;margin-top:8px;background:#0d1117;border:1px solid #30363d;border-radius:6px;overflow:hidden;flex-shrink:0}
.cmd-prompt{padding:12px 0 12px 14px;color:#3fb950;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:1rem;font-weight:600;user-select:none}
.cmd-input{flex:1;background:transparent;border:none;outline:none;color:#e6edf3;font-family:'SF Mono','Fira Code',Consolas,monospace;font-size:1rem;padding:12px;resize:none;min-height:44px;max-height:160px;line-height:1.4;overflow-y:auto}
.cmd-input::placeholder{color:#484f58}
.cmd-send{border:none;border-left:1px solid #30363d;border-radius:0;padding:12px 18px;font-size:.95rem;align-self:flex-end}

/* Raw tab */
.tab-raw{padding-top:16px}
.raw-controls{display:flex;justify-content:space-between;align-items:center;margin-bottom:8px}
.raw-info{color:#6e7681;font-size:.75rem}
.raw-output{background:#0d1117;border:1px solid #21262d;border-radius:8px;padding:12px;font-family:'SF Mono','Fira Code','Cascadia Code',Consolas,monospace;font-size:.8rem;line-height:1.45;color:#c9d1d9;flex:1;min-height:300px;overflow-y:auto;white-space:pre;word-wrap:normal;overflow-x:auto}
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

/* Mobile */
@media(max-width:768px){
  .top-nav{padding:0 8px}
  .nav-brand{padding:10px 8px 10px 0;margin-right:2px;font-size:.75rem}
  .nav-item{padding:8px 10px;gap:5px}
  .nav-title{display:none}
  .nav-attached{display:none}
  .nav-status-text{display:none}
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
<nav class="top-nav" id="top-nav">
  <span class="nav-brand">tmux</span>
  <button class="nav-new-btn" onclick="showCreateModal()" title="New session">+</button>
  <span class="nav-spacer"></span>
  <span class="nav-status-text" id="status-info">Watching for changes...</span>
  <button class="nav-refresh-btn" onclick="refreshAllRealtime()">Refresh All</button>
</nav>
<div class="main" id="main"></div>
<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal-content"></div>
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
const rawCache={};
const lastStatus={};
// Local chat messages mirror (kept in sync with server)
const chatMessages={};

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
  if(s==='busy')return'Busy';
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
  const s=sessions.find(x=>x.name===selectedSession);
  if(!s){mainEl.innerHTML='<div class="empty">No session selected</div>';return}
  const tab=activeTabs[s.name]||'chat';
  // Sync server messages into local store
  if(s.messages && s.messages.length) chatMessages[s.name]=s.messages;

  mainEl.innerHTML=`
    <div class="detail-header">
      <div class="detail-header-left">
        <span class="detail-title-text" id="title-${s.name}">${esc(s.title)||'Session '+esc(s.name)}</span>
      </div>
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

    <div class="tab-bar">
      <div class="tab ${tab==='chat'?'active':''}" onclick="switchTab('${s.name}','chat')">Chat</div>
      <div class="tab ${tab==='raw'?'active':''}" onclick="switchTab('${s.name}','raw')">Raw Output</div>
      <div class="tab ${tab==='info'?'active':''}" onclick="switchTab('${s.name}','info')">Info</div>
    </div>

    <div class="tab-content ${tab==='chat'?'active':''}" id="tab-chat-${s.name}">
      <div class="chat-wrap">
        <div class="chat-messages" id="chat-${s.name}">
          ${renderChatBubbles(s.name)}
          ${s.activity_status==='busy'?'<div class="chat-typing"><span class="dots">...</span> Working</div>':''}
        </div>
        <div class="cmd-bar">
          <span class="cmd-prompt">&gt;</span>
          <textarea class="cmd-input" id="cmd-chat-${s.name}" rows="1"
            placeholder="Send a message..."
            onkeydown="handleChatKey(event,'${s.name}')"
            oninput="autoGrow(this)"
            autocomplete="off" spellcheck="false"></textarea>
          <button class="btn cmd-send" onclick="sendChat('${s.name}')">Send</button>
        </div>
      </div>
    </div>

    <div class="tab-content tab-raw ${tab==='raw'?'active':''}" id="tab-raw-${s.name}">
      <div class="raw-controls">
        <span class="raw-info" id="raw-info-${s.name}">Click to load raw output</span>
        <button class="btn" onclick="loadRaw('${s.name}')">Reload</button>
      </div>
      <div class="raw-output" id="raw-${s.name}">Click "Raw Output" tab or "Reload" to fetch...</div>
      <div class="cmd-bar">
        <span class="cmd-prompt">$</span>
        <textarea class="cmd-input" id="cmd-raw-${s.name}" rows="1"
          placeholder="Type a command and press Enter..."
          onkeydown="handleRawKey(event,'${s.name}')"
          oninput="autoGrow(this)"
          autocomplete="off" spellcheck="false"></textarea>
        <button class="btn cmd-send" onclick="sendCmd('${s.name}','raw')">Send</button>
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
      <div class="detail-footer" style="margin-top:24px">
        <div class="timestamps">
          <div class="ts">project: <span id="ts-desc-${s.name}">${timeAgo(s.description_at)}</span></div>
          <div class="ts">progress: <span id="ts-prog-${s.name}">${timeAgo(s.progress_at)}</span></div>
          <div class="ts">live: <span id="ts-rt-${s.name}">${timeAgo(s.realtime_at)}</span></div>
        </div>
        <div class="btn-group">
          <button class="btn" id="btn-${s.name}" onclick="refreshOne('${esc(s.name)}')">Update</button>
          <button class="btn btn-full" id="btn-full-${s.name}" onclick="refreshFull('${esc(s.name)}')">Full</button>
        </div>
      </div>
    </div>`;

  // Scroll chat to bottom
  const chatEl=document.getElementById('chat-'+s.name);
  if(chatEl)chatEl.scrollTop=chatEl.scrollHeight;
  // Auto-load raw
  if(tab==='raw'&&!rawCache[s.name])loadRaw(s.name);
}

function selectSession(name){
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
  if(tab==='raw'&&!rawCache[name])loadRaw(name);
  if(tab==='chat'){
    const chatEl=document.getElementById('chat-'+name);
    if(chatEl)chatEl.scrollTop=chatEl.scrollHeight;
  }
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
  el.style.height='auto';
  el.style.height=Math.min(el.scrollHeight,160)+'px';
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
  try{
    await fetch(BASE+'/api/sessions/'+name+'/send',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({command:cmd})
    });
    input.value='';input.style.height='auto';
    if(source==='raw')setTimeout(()=>loadRaw(name),500);
  }catch(e){alert('Failed to send.')}
  input.disabled=false;
  input.focus();
}

async function loadRaw(name){
  const rawEl=document.getElementById('raw-'+name);
  const infoEl=document.getElementById('raw-info-'+name);
  if(rawEl)rawEl.textContent='Loading...';
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/raw');
    const data=await resp.json();
    rawCache[name]=true;
    if(rawEl){rawEl.textContent=data.raw||'(empty)';rawEl.scrollTop=rawEl.scrollHeight}
    if(infoEl)infoEl.textContent=data.lines+' lines';
    updateStatusPill(name,data.activity_status,data.activity_detail);
  }catch(e){if(rawEl)rawEl.textContent='Error loading.'}
}

function updateStatusPill(name,status,detail){
  const pill=document.getElementById('status-'+name);
  if(pill){
    pill.className='status-pill '+(status||'unknown');
    pill.innerHTML='<span class="status-dot"></span><span class="status-label">'+statusLabel(status)+'</span>'
      +(detail?'<span style="font-weight:400;opacity:.7"> &middot; '+esc(detail)+'</span>':'');
  }
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
  const titleEl=document.getElementById('title-'+s.name);
  if(titleEl&&s.title)titleEl.textContent=s.title;
  const desc=document.getElementById('desc-'+s.name);
  const prog=document.getElementById('prog-'+s.name);
  if(desc)desc.textContent=s.description||'';
  if(prog)prog.textContent=s.progress||'';
  const tsDesc=document.getElementById('ts-desc-'+s.name);
  const tsProg=document.getElementById('ts-prog-'+s.name);
  const tsRt=document.getElementById('ts-rt-'+s.name);
  if(tsDesc)tsDesc.textContent=timeAgo(s.description_at);
  if(tsProg)tsProg.textContent=timeAgo(s.progress_at);
  if(tsRt)tsRt.textContent=timeAgo(s.realtime_at);
  updateStatusPill(s.name,s.activity_status,s.activity_detail);

  // Update typing indicator
  const chatEl=document.getElementById('chat-'+s.name);
  if(chatEl){
    const existing=chatEl.querySelector('.chat-typing');
    if(s.activity_status==='busy'&&!existing){
      const typing=document.createElement('div');
      typing.className='chat-typing';
      typing.innerHTML='<span class="dots">...</span> Working';
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
      if(s.messages&&s.messages.length)chatMessages[s.name]=s.messages;
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
  if(btn){btn.disabled=true;btn.innerHTML='<span class="spinner"></span>Full'}
  [desc,prog].forEach(el=>{if(el)el.classList.add('loading')});
  try{
    const resp=await fetch(BASE+'/api/sessions/'+name+'/refresh-all',{method:'POST'});
    const data=await resp.json();
    const idx=sessions.findIndex(s=>s.name===name);
    if(idx>=0){sessions[idx]={...sessions[idx],...data};updateCard(sessions[idx])}
  }catch(e){}
  [desc,prog].forEach(el=>{if(el)el.classList.remove('loading')});
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
    }
    if(!changed)statusInfoEl.textContent='Watching for changes...';
  }catch(e){statusInfoEl.textContent='Status poll failed'}
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

loadAll();
</script>
</body></html>
"""

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT)
