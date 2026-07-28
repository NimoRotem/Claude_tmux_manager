"""The `memory` MCP server — the only sanctioned way in or out of the store.

Both backends mount this (see core/mcp.yaml), so a fact written by a Claude
session is readable by the Codex session that picks the work up, and vice versa.
That is the whole reason neither backend's native memory is used.

Speaks MCP over stdio, JSON-RPC 2.0, framed as line-delimited JSON — which is
what both CLIs' stdio transports use. No third-party dependency on purpose: this
has to start on a bare host, and an MCP server that fails to import is
indistinguishable from a broken tool.
"""

from __future__ import annotations

import json
import os
import sys

from . import memory

PROTOCOL_VERSION = "2025-06-18"
SERVER_INFO = {"name": "agentctx-memory", "version": "1.0.0"}

TOOLS = [
    {
        "name": "memory_search",
        "description": ("Search durable memory shared by every agent and backend on "
                        "this machine. Use before concluding something was never "
                        "recorded, and before asking the user to repeat themselves."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Substring to look for."},
                "scope": {"type": "string",
                          "description": "Project scope; omit to search every scope."},
                "limit": {"type": "integer", "default": 20},
            },
            "required": ["query"],
        },
    },
    {
        "name": "memory_read",
        "description": "Read one memory in full by its key.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "scope": {"type": "string", "default": "global"},
            },
            "required": ["key"],
        },
    },
    {
        "name": "memory_write",
        "description": ("Record a durable fact. One fact per key. Use for things a "
                        "future session would otherwise have to rediscover: a "
                        "decision and its reason, a trap, where something lives. "
                        "Do not record what the repo or git history already says."),
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string", "description": "Short kebab-case handle."},
                "body": {"type": "string", "description": "The fact, in a few lines."},
                "scope": {"type": "string", "default": "global",
                          "description": "Project scope, or 'global'."},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["key", "body"],
        },
    },
    {
        "name": "memory_list",
        "description": "List every memory in a scope, newest first.",
        "inputSchema": {
            "type": "object",
            "properties": {"scope": {"type": "string", "default": "global"}},
        },
    },
    {
        "name": "memory_delete",
        "description": "Delete a memory that turned out to be wrong or stale.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "key": {"type": "string"},
                "scope": {"type": "string", "default": "global"},
            },
            "required": ["key"],
        },
    },
]


def _text(payload) -> dict:
    body = payload if isinstance(payload, str) else json.dumps(payload, indent=2, default=str)
    return {"content": [{"type": "text", "text": body}]}


def _call(name: str, args: dict) -> dict:
    scope = args.get("scope") or os.environ.get("AGENTCTX_SCOPE") or "global"
    if name == "memory_search":
        hits = memory.search(args.get("query", ""),
                             scope=args.get("scope"), limit=int(args.get("limit") or 20))
        if not hits:
            return _text("No memory matched. Nothing is recorded for that yet.")
        return _text(hits)
    if name == "memory_read":
        mem = memory.read(args["key"], scope=scope)
        return _text(mem.to_markdown() if mem else f"No memory named {args['key']!r} in {scope}.")
    if name == "memory_write":
        mem = memory.write(args["key"], args["body"], scope=scope,
                           tags=args.get("tags"), source=os.environ.get("AGENTCTX_SESSION", ""))
        return _text(f"Saved {mem.key!r} in scope {mem.scope!r}.")
    if name == "memory_list":
        return _text([{"key": m.key, "tags": m.tags, "updated_at": m.updated_at}
                      for m in memory.listing(scope)])
    if name == "memory_delete":
        ok = memory.delete(args["key"], scope=scope)
        return _text("Deleted." if ok else "No such memory.")
    raise KeyError(name)


def handle(request: dict) -> dict | None:
    method = request.get("method")
    req_id = request.get("id")

    if method == "initialize":
        result = {"protocolVersion": PROTOCOL_VERSION,
                  "capabilities": {"tools": {}},
                  "serverInfo": SERVER_INFO}
    elif method in ("notifications/initialized", "initialized"):
        return None                      # notification: no response
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        params = request.get("params") or {}
        try:
            result = _call(params.get("name", ""), params.get("arguments") or {})
        except KeyError as exc:
            return {"jsonrpc": "2.0", "id": req_id,
                    "error": {"code": -32601, "message": f"unknown tool {exc}"}}
        except Exception as exc:                       # a tool error is data, not a crash
            result = _text(f"memory tool failed: {exc}")
            result["isError"] = True
    elif method == "ping":
        result = {}
    else:
        if req_id is None:
            return None
        return {"jsonrpc": "2.0", "id": req_id,
                "error": {"code": -32601, "message": f"unknown method {method!r}"}}

    return {"jsonrpc": "2.0", "id": req_id, "result": result}


def main() -> int:
    memory.MEM_ROOT.mkdir(parents=True, exist_ok=True)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        response = handle(request)
        if response is not None:
            sys.stdout.write(json.dumps(response) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
