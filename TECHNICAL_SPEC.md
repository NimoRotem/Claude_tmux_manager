# tmux Dashboard Technical Specification

## Purpose

The dashboard manages owner-scoped Codex conversations running in tmux. It preserves exact Codex rollout roots, restores durable tabs after process or host restarts, and exposes terminal, chat, project, authentication, and maintenance controls through a FastAPI application.

## Session lifecycle invariants

- Every managed tab is bound to an explicit owner, lifecycle generation, project cwd, and—once available—an exact user-root Codex rollout UUID.
- Create, restore, park, resume, and delete operations revalidate owner and generation under per-session and tmux-server mutation fences.
- Interactive close is a staged knowledge-preserving operation. The lifecycle remains recoverable while the conversation is captured and summarized.
- A tab may enter `deleting` and tmux may be terminated only after its private close archive and project technical-spec entry have been atomically written and verified.
- Operational account cleanup remains a separate controller path from interactive close.

## Knowledge-preserving close

The browser starts an asynchronous, owner-scoped close job and polls its status. The controller:

1. Blocks new session resumes/prompts, pauses autonomous prompt producers, checkpoints the live cwd/root, and waits for active Codex work to become idle.
2. Securely opens the exact owner-bound root rollout without following links, archives all of its user/assistant conversation events, and hashes every raw rollout byte so tool-only activity is also fenced. A missing or invalid rollout fails closed.
   The immutable `session_meta.cwd` is the conversation's historical origin and may differ after an exact root is resumed with `codex resume -C`; it must resolve safely, while the live tmux cwd must still match the current lifecycle project cwd.
3. Produces a close-specific technical handoff with bounded, chunked map/reduce coverage of the whole conversation. Every chunk and a mandatory final technical/privacy edit must succeed; an unavailable summarizer, incomplete merge, or conversation beyond the safe budget leaves the tab open.
4. Removes secret-shaped values, owner identifiers, customer/vendor case narrative, and private absolute paths from repository-facing text. The handoff retains durable engineering facts rather than personal or operational history.
5. Writes an immutable generation-and-content-keyed private archive beneath the owner’s dashboard data directory and fsyncs both file and containing directory.
6. Merges an idempotent, marker-delimited entry into `repo/lisa-app/docs/TECHNICAL_SPEC.md` when the session workspace contains a Lisa checkout. Non-Lisa workspaces fall back to their existing `docs/TECHNICAL_SPEC.md` or `TECHNICAL_SPEC.md`. Human-authored content is preserved.
7. Re-checkpoints and revalidates owner, generation, cwd, root UUID, exact tmux identity, and rollout fingerprint before invoking the established delete transaction.

If capture, archive, spec persistence, verification, or identity checks fail, the job reports a retryable error and leaves the tab open.

## Session knowledge log

Entries below are managed by the dashboard when sessions close. Existing entries are never removed; a retry for the same lifecycle generation replaces only its matching marker block.
