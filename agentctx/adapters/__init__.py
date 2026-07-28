"""Per-backend translation of the single-source `core/` tree.

Every adapter answers the same questions — where does the context file go, how
is an MCP server spelled, what does policy level "workspace-write" mean here —
and nothing outside this package should know the answers.

Adding a third backend means adding a module here and registering it below. The
renderer, the memory bridge and the dashboard all go through `get_adapter()`.
"""

from .claude import ClaudeAdapter
from .codex import CodexAdapter

ADAPTERS = {a.key: a for a in (ClaudeAdapter(), CodexAdapter())}


def get_adapter(key: str):
    try:
        return ADAPTERS[str(key).strip().lower()]
    except KeyError:
        raise KeyError(f"unknown backend {key!r}; known: {', '.join(ADAPTERS)}") from None


__all__ = ["ADAPTERS", "get_adapter", "ClaudeAdapter", "CodexAdapter"]
