#!/usr/bin/env python3
"""Stdio MCP bridge to the GRABO Document Vault.

The vault's own MCP server lives on grabo-systems and is published at
https://grabo.cc/docvault-mcp/mcp behind a static bearer key. Codex can talk
streamable-HTTP directly, but only reads its bearer token from the *process*
environment — which would mean exporting the vault key into every member's
shell. A stdio server instead takes its key from `mcp_servers.docvault.env` in
the user's own config.toml, so the key never lands in a shell they can echo.

Two tools, forwarded verbatim: search_docvault and get_document.
"""
import json
import os
import urllib.error
import urllib.request
from typing import Any

ENDPOINT = os.environ.get("DOCVAULT_MCP_URL", "https://grabo.cc/docvault-mcp/mcp")
KEY = os.environ.get("DOCVAULT_MCP_KEY", "")
_TIMEOUT = 60
_MAX_TEXT = 200_000


def _call(tool: str, arguments: dict[str, Any]) -> str:
    if not KEY:
        raise RuntimeError("The document vault key is not configured for this session.")
    body = json.dumps({
        "jsonrpc": "2.0", "id": 1, "method": "tools/call",
        "params": {"name": tool, "arguments": arguments},
    }).encode()
    request = urllib.request.Request(ENDPOINT, data=body, headers={
        "Authorization": "Bearer " + KEY,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "User-Agent": "Grabo-DocVault-Bridge/1.0",
    })
    try:
        with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
            payload = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(400).decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError(f"Document vault returned HTTP {exc.code}. {detail}".strip()) from exc
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Could not reach the document vault; retry shortly.") from exc
    if "error" in payload:
        raise RuntimeError(str(payload["error"].get("message") or payload["error"]))
    parts = [
        block.get("text", "")
        for block in (payload.get("result", {}).get("content") or [])
        if block.get("type") == "text"
    ]
    text = "\n".join(p for p in parts if p)
    return text[:_MAX_TEXT] if text else json.dumps(payload.get("result", {}))[:_MAX_TEXT]


def search_docvault(
    query: str,
    limit: int = 10,
    file_type: str = "",
    company: str = "",
) -> str:
    """Search the GRABO / Nemo Document Vault — ~150,000 company business documents.

    Emails, PDFs, invoices, purchase orders, quotes, specs, certificates, contracts
    and Drive files going back years. This is the company's document archive: use
    it whenever someone asks about an order, a supplier, a shipment, a price, a
    certificate or "do we have a document about X". Results include a numeric id —
    pass it to get_document to read the full text.

    Args:
        query: what to look for, in plain words.
        limit: how many matches to return (default 10).
        file_type: optional filter, e.g. "pdf", "email", "xlsx".
        company: optional company/supplier name filter.
    """
    return _call("search_docvault", {
        "query": str(query or ""),
        "limit": int(limit),
        "file_type": str(file_type or ""),
        "company": str(company or ""),
    })


def get_document(id: int, include_text: bool = True) -> str:
    """Fetch one vault document by the numeric id returned by search_docvault.

    Returns its metadata (title, summary, source, link) and, by default, the
    extracted full text.
    """
    return _call("get_document", {"id": int(id), "include_text": bool(include_text)})


def main() -> None:
    from mcp.server.fastmcp import FastMCP

    server = FastMCP("grabo-docvault")
    for tool in (search_docvault, get_document):
        server.tool()(tool)
    server.run()


if __name__ == "__main__":
    main()
