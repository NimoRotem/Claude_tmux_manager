# Claude Code specifics

These notes apply only when you are Claude Code. Everything above this section is
shared with the other backend and must not be duplicated here.

## Tools you have that the other backend does not

- **Task / subagents** — delegate a self-contained search or analysis to a
  subagent when it would otherwise mean reading across many files and you only
  need the conclusion. Do not delegate a single-fact lookup you can do directly.
- **TodoWrite** — track multi-step work. Use it for anything with three or more
  distinct steps; skip it for a single edit.
- Both are yours alone. Instructions elsewhere that mention "subagents" or "the
  todo list" mean these; ignore them if a rule was written for the other backend.

## Escalating out of the sandbox

You run with permissions pre-granted for this workspace. If a command is refused,
the fix is not to retry it verbatim — say what was refused and why you need it.

## Memory

Your project memory directory is loaded for you. Treat `MEMORY.md` in it as an
index: one line per memory, pointing at a sibling file. Write the fact to its own
file and add the pointer. Do not paste memory content into `MEMORY.md` itself.
