# agentctx

One source of agent context, rendered for every backend.

Write a rule, a skill or an MCP server **once** in `core/`. An adapter translates
it into what Claude Code reads and into what Codex reads. Nothing outside
`adapters/` may branch on a backend name — if you are about to write
`if backend == "codex"` in shared code, the abstraction is missing.

```
core/                     the single source of truth
  instructions/           shared body, split by topic
    _backend/claude.md    the delta that is genuinely Claude-only
    _backend/codex.md     the delta that is genuinely Codex-only
  skills/<name>/SKILL.md  one body, two discovery mechanisms
  prompts/<name>.md       reusable prompts, identical on both
  mcp.yaml                servers, transcoded to JSON or TOML
  policy.yaml             three levels, mapped per backend (lossy — see below)
  runtime.yaml            named tiers, because there is no numeric mapping
  ignore.txt              enforced on one side, instructed on both
adapters/                 the only place a backend name may appear
  claude.py codex.py detect.toml
runtime/events/emit.sh    one event vocabulary, three sources
auth/<backend>.sh         status | login | refresh | export-env
state/memory/             the shared memory store
render.py                 core/ -> a backend's config tree
memory*.py                the store, its MCP server, and the native-memory bridge
exec.py                   headless invocation, normalised
session.py                tmux session + git worktree per session
cli.py                    render | sync | status | exec | session
integration.py            what the dashboard calls
```

## The twelve abstractions

| # | Thing | Source | Claude gets | Codex gets |
|---|---|---|---|---|
| 1 | Instructions | `core/instructions/*.md` | `CLAUDE.md` | `AGENTS.md` |
| 2 | Skills | `core/skills/<n>/SKILL.md` | symlinked, autoloaded | symlinked **+ an index table injected into AGENTS.md** |
| 3 | Prompts | `core/prompts/<n>.md` | `commands/<n>.md` | `prompts/<n>.md` |
| 4 | MCP servers | `core/mcp.yaml` | `.mcp.json` | `[mcp_servers.*]` in `config.toml` |
| 5 | Permissions | `core/policy.yaml` | `permissions.allow/deny` | `sandbox_mode` + `approval_policy` |
| 6 | Memory | `state/memory/` | digest + MCP tool | digest + MCP tool |
| 7 | Events | `runtime/events/emit.sh` | 4 hook points | 1 `notify` program |
| 8 | Auth | `auth/<backend>.sh` | Anthropic OAuth / API key | ChatGPT login / API key |
| 9 | Runtime | `core/runtime.yaml` | `model` + thinking budget | `model` + `model_reasoning_effort` |
| 10 | Headless | `exec.py` | `-p --output-format stream-json` | `exec --json` |
| 11 | Sessions | `session.py` | `agent:<project>:<backend>` + worktree | same |
| 12 | Ignore | `core/ignore.txt` | deny rules **and** prose | prose only |

### Where the mapping is genuinely lossy

Three of these are not clean translations, and pretending otherwise is how a
dual-backend setup goes quietly wrong:

* **Permissions (5).** Claude is a per-tool rule list; Codex is one sandbox mode
  plus an approval policy. A Claude deny rule blocks a single tool call; a Codex
  sandbox mode constrains the whole process. Treat the three levels as
  approximate equivalents and read `core/policy.yaml`'s table before relying on
  either. Because the Codex side is the coarser one, `always_confirm` is also
  rendered as instruction text on both.
* **Runtime tuning (9).** A token thinking budget and an effort enum have no
  honest numeric mapping. Ask for a **tier** (`fast` / `default` / `deep`), never
  for a number.
* **Events (7).** Claude's hooks fire per tool call; Codex has one notify
  program. Consumers may depend on the event *names* being the same. They must
  not depend on the count or the ordering.

### Memory, and why neither native store is used

Claude Code keeps one memory directory per project. Codex keeps a single global
`MEMORY.md` with a background consolidation pipeline. Those are different
*shapes*, so anything written into one is invisible to the other and a session
that switches backend loses its history.

So: the store here is authoritative, both backends carry only a pinned **index**
in their context file, and all reads and writes go through the `memory` MCP
server. `memory_sync.py` bridges in both directions — it imports whatever native
memory a project already had, and pushes the digest back out to the path each
backend actually reads. That last part is the trap: Claude reads
`projects/<encoded cwd>/memory/MEMORY.md`, Codex reads one global `MEMORY.md`,
and writing a per-project memory file into a Codex home is a silent no-op.

## Use

```bash
python3 -m agentctx.cli render --all                 # core/ -> both backends
python3 -m agentctx.cli render --backend codex --home ~/.codex --tier deep
python3 -m agentctx.cli sync --all --cwd /srv/app    # bridge memory both ways
python3 -m agentctx.cli status                       # installed / rendered / authed
python3 -m agentctx.cli exec --backend codex --stream "summarise this repo"
python3 -m agentctx.cli session --project app --backend claude --repo /srv/app
```

The dashboard calls `integration.prepare_session()` when it creates a session, so
in normal use none of the above is needed by hand. `POST /api/context/render`
pushes a `core/` edit to every home, including every member's.

## Rules

- **Never fork the shared body.** A backend difference is either a few lines in
  `core/instructions/_backend/<key>.md` or a translation rule in an adapter.
- **Skill bodies are stored once** and symlinked. They are never inlined into a
  context file — that is what blows up a context window.
- **Rendered files are marked and merged, not overwritten.** Managed content sits
  between markers; a user's own `AGENTS.md` prose and the rest of a `config.toml`
  survive a re-render. Rendering is idempotent.
- **An MCP server with a missing credential is left out**, not registered broken.
  A tool that appears and then fails auth reads like an outage.
- **The two CLIs never share a working directory.** `session.py` gives each
  session its own git worktree and branch.
