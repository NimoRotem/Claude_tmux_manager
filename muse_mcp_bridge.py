#!/usr/bin/env python3
"""Owner-bound stdio bridge from Muse Code to a Streamable HTTP MCP server.

Muse 0.1 accepts static HTTP headers in settings but has no equivalent of
Codex's ``bearer_token_env_var``. This bridge loads the token from the process
environment or an owner-private file and keeps its value out of Muse settings.
It exposes the remote tools over Muse's working stdio transport.
"""

from __future__ import annotations

import argparse
import logging
import os
import stat
from contextlib import asynccontextmanager
from pathlib import Path

import anyio
import httpx
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.lowlevel import NotificationOptions, Server
from mcp.server.stdio import stdio_server

logger = logging.getLogger("muse-mcp-bridge")
logging.basicConfig(level=logging.WARNING)


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True)
    parser.add_argument("--bearer-env", default="ADVISOR_TOKEN")
    parser.add_argument("--bearer-file", default="")
    return parser.parse_args()


ARGS = _arguments()


@asynccontextmanager
async def remote_lifespan(_server: Server):
    token = os.environ.get(ARGS.bearer_env, "").strip()
    if not token and ARGS.bearer_file:
        token_file = Path(ARGS.bearer_file).expanduser()
        token_stat = token_file.stat()
        if not stat.S_ISREG(token_stat.st_mode):
            raise RuntimeError("bearer token path is not a regular file")
        if token_stat.st_uid != os.geteuid() or token_stat.st_mode & 0o077:
            raise RuntimeError("bearer token file is not private to the current owner")
        token = token_file.read_text().strip()
    if not token:
        raise RuntimeError("required bearer token is unavailable")
    headers = {"Authorization": f"Bearer {token}"}
    timeout = httpx.Timeout(30.0, read=300.0)
    async with httpx.AsyncClient(
        headers=headers,
        timeout=timeout,
        follow_redirects=True,
    ) as http_client:
        async with streamable_http_client(
            ARGS.url,
            http_client=http_client,
        ) as (read_stream, write_stream, _session_id):
            async with ClientSession(read_stream, write_stream) as remote:
                await remote.initialize()
                yield {"remote": remote}


bridge = Server(
    "muse-owner-bound-mcp-bridge",
    version="1.0.0",
    lifespan=remote_lifespan,
)


@bridge.list_tools()
async def list_tools():
    remote: ClientSession = bridge.request_context.lifespan_context["remote"]
    return (await remote.list_tools()).tools


@bridge.call_tool()
async def call_tool(name: str, arguments: dict | None):
    remote: ClientSession = bridge.request_context.lifespan_context["remote"]
    return await remote.call_tool(name, arguments or {})


async def main() -> None:
    async with stdio_server() as (read_stream, write_stream):
        await bridge.run(
            read_stream,
            write_stream,
            bridge.create_initialization_options(
                notification_options=NotificationOptions(),
                experimental_capabilities={},
            ),
        )


if __name__ == "__main__":
    anyio.run(main)
