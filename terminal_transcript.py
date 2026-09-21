"""Incremental, read-only conversation text for the terminal's clean view."""
from __future__ import annotations

import hashlib
import json
import re
import threading
from collections import OrderedDict
from pathlib import Path

from runtime_control import LockedJsonStore


_ANSI = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")


def _visible_message(record):
    if not isinstance(record, dict) or record.get("isSidechain"):
        return None
    role = record.get("type")
    if role == "system":
        if record.get("subtype") == "compact_boundary":
            return "system", "Conversation compacted. Earlier messages remain available."
        if record.get("subtype") == "local_command" and isinstance(record.get("content"), str):
            return "system", _ANSI.sub("", record["content"]).strip("\n")
        return None
    if role not in ("user", "assistant") or record.get("isMeta") or record.get("isCompactSummary"):
        return None
    message = record.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(block["text"] for block in content
                         if isinstance(block, dict) and block.get("type") == "text"
                         and isinstance(block.get("text"), str))
    else:
        return None
    if role == "user":
        command = re.match(r"^<command-name>([^<]+)</command-name>", text)
        output = re.match(r"^<local-command-(?:stdout|stderr)>([\s\S]*?)</local-command-(?:stdout|stderr)>\s*$", text)
        if command:
            args = re.search(r"<command-args>([\s\S]*?)</command-args>", text)
            text = command[1] + (" " + args[1].strip() if args and args[1].strip() else "")
        elif output:
            role, text = "system", output[1]
        elif text.startswith("<local-command-caveat>"):
            return None
    text = _ANSI.sub("", text).strip("\n")
    return (role, text) if text.strip() else None


def _snapshot(messages):
    rows, user_rows, user_lines = [], [], []
    for role, text in messages:
        if rows:
            rows.append("")
            user_lines.append(0)
        parts = text.split("\n")
        if role == "user":
            user_rows.append(len(rows))
            rows.extend(["❯ " + parts[0], *["  " + part for part in parts[1:]]])
            user_lines.extend([2, *([1] * (len(parts) - 1))])
        else:
            rows.extend([("● " if role == "assistant" else "• ") + parts[0], *parts[1:]])
            user_lines.extend([0] * len(parts))
    raw = "\n".join(rows)
    return {"raw": raw, "user_rows": user_rows, "user_lines": user_lines,
            "message_count": len(messages), "lines": len(rows),
            "version": hashlib.sha256(raw.encode()).hexdigest()[:24]}


class TranscriptCache:
    """Cache only visible text, and read only bytes appended since the last poll.

    A stable record UUID identifies a message. Repeated snapshots replace that
    message; identical words in separate messages are intentionally preserved.
    Partial final records wait for their newline. Eviction drops only a cache,
    never saved history.
    """

    def __init__(self, max_files=32):
        self._files = OrderedDict()
        self._lock = threading.Lock()
        self._max_files = max_files
        self._combined = OrderedDict()

    def read_many(self, paths):
        snapshots = [self.read(path) for path in paths]
        if len(snapshots) == 1:
            return snapshots[0]
        key = tuple(str(path) for path in paths)
        versions = tuple(s["version"] for s in snapshots)
        with self._lock:
            cached = self._combined.get(key)
            if cached and cached[0] == versions:
                self._combined.move_to_end(key)
                return cached[1]
            rows, flags, starts = [], [], []
            for snapshot in snapshots:
                if rows:
                    boundary = ["", "• New conversation. Earlier messages remain available.", ""]
                    rows.extend(boundary)
                    flags.extend([0] * len(boundary))
                offset = len(rows)
                starts.extend(offset + i for i in snapshot["user_rows"])
                rows.extend(snapshot["raw"].split("\n") if snapshot["raw"] else [])
                flags.extend(snapshot["user_lines"])
            raw = "\n".join(rows)
            combined = {"raw": raw, "user_rows": starts, "user_lines": flags, "lines": len(rows),
                        "message_count": sum(s["message_count"] for s in snapshots),
                        "version": hashlib.sha256(raw.encode()).hexdigest()[:24]}
            self._combined[key] = (versions, combined)
            self._combined.move_to_end(key)
            while len(self._combined) > self._max_files:
                self._combined.popitem(last=False)
            return combined

    def read(self, path):
        path = Path(path)
        with self._lock:
            stat = path.stat()
            stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            state = self._files.get(path)
            if state and state["stamp"] == stamp:
                self._files.move_to_end(path)
                return state["snapshot"]
            if (not state or state["stamp"][:2] != stamp[:2]
                    or stat.st_size < state["offset"] or stat.st_size == state["stamp"][2]):
                state = {"offset": 0, "messages": OrderedDict(), "snapshot": _snapshot([])}
            dirty = False
            with path.open("rb") as stream:
                stream.seek(state["offset"])
                while True:
                    offset = stream.tell()
                    line = stream.readline()
                    if not line or not line.endswith(b"\n"):
                        break
                    state["offset"] = stream.tell()
                    try:
                        record = json.loads(line)
                        visible = _visible_message(record)
                    except (ValueError, TypeError):
                        continue
                    if visible is None:
                        continue
                    uid = record.get("uuid")
                    key = uid if isinstance(uid, str) and uid else offset
                    if state["messages"].get(key) != visible:
                        state["messages"][key] = visible
                        dirty = True
            if dirty:
                state["snapshot"] = _snapshot(list(state["messages"].values()))
            state["stamp"] = stamp
            self._files[path] = state
            self._files.move_to_end(path)
            while len(self._files) > self._max_files:
                self._files.popitem(last=False)
            return state["snapshot"]


class ConversationHistory:
    """Remember /clear boundaries within one owner and one tmux incarnation."""

    def __init__(self, path):
        self._store = LockedJsonStore(Path(path), lambda: {"sessions": {}})
        self._cache = {}

    def ids(self, name, owner, created, current, initial=""):
        identity = (str(owner), str(created))
        cache_key = (*identity, current, initial)
        cached = self._cache.get(name)
        if cached and cached[0] == cache_key:
            return list(cached[1])

        def update(data):
            sessions = data.setdefault("sessions", {})
            row = sessions.get(name, {})
            if (row.get("owner"), row.get("created")) != identity:
                row = {"owner": identity[0], "created": identity[1], "ids": []}
            for uid in (initial, current):
                if re.fullmatch(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}", uid or "") and uid not in row["ids"]:
                    row["ids"].append(uid)
            sessions[name] = row
            return row["ids"]

        _, ids = self._store.update(update)
        self._cache[name] = (cache_key, list(ids))
        return list(ids)
