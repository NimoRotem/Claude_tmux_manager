#!/usr/bin/env python3
"""Capture an owner-checked browser audit event from an agent workflow."""

from __future__ import annotations

import argparse
import asyncio
import json

import app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--action", required=True)
    args = parser.parse_args()

    session = app._browser_session_by_id(args.browser)
    if (
        not session
        or app._browser_owner_id(session) != args.owner
        or not session.get("account_browser")
    ):
        print(json.dumps({"ok": False, "error": "browser not found"}))
        return 2
    if not app._browser_running(session):
        print(json.dumps({"ok": False, "error": "browser is not running"}))
        return 1

    result = asyncio.run(
        app._capture_browser_screenshot(
            session,
            args.action[:120],
            task_id="agent",
        )
    )
    event = result.get("event") or {}
    print(
        json.dumps(
            {
                "ok": bool(result.get("ok")),
                "event_id": event.get("id", ""),
                "timestamp": event.get("timestamp", ""),
                "error": result.get("error", ""),
            }
        )
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
