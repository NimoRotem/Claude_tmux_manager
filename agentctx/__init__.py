"""agentctx — one source of agent context, rendered for every backend.

See README.md in this directory for the twelve abstractions and where each one
lives. The short version: `core/` is written once, `adapters/` translate it, and
nothing outside `adapters/` may branch on a backend name.
"""

__version__ = "1.0.0"
