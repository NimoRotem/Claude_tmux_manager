# Codex specifics

These notes apply only when you are Codex. Everything above this section is
shared with the other backend and must not be duplicated here.

## Tools the other backend has that you do not

The shared rules above never assume subagents or a todo tool — but other
documents on this machine were written for Claude Code and may. If a rule tells
you to "delegate to a subagent", "spawn agents in parallel" or "use TodoWrite",
it does not apply to you: do the work yourself, in this session, and keep your
plan in the conversation.

## Escalating out of the sandbox

You run with the sandbox and approval policy set by the dashboard (normally
`danger-full-access` / `approval_policy = "never"`), so commands should not stop
for approval. If one is refused anyway, report the refusal and what you needed —
do not loop retrying it.

## Skills

You do not discover skills automatically. The **Skills** table below is the
complete index. When a task matches one, read that skill's `SKILL.md` **in full**
before acting on it; the one-line description is a routing hint, not the content.

## Memory

You keep a single global `MEMORY.md` with project-scoped entries, not one file
per project. The dashboard also keeps a per-project memory directory for this
workspace and syncs it into your `MEMORY.md` — read the digest below, and write
new durable facts through the `memory` MCP tool rather than editing files
directly, so a session that later switches backend keeps them.
