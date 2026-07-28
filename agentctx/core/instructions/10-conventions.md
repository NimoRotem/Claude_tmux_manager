# Conventions on this machine

## Git on a shared box

Several teammates work here as the same OS user, so be disciplined:

- **Identity is preset.** Your commits are authored as
  `$GIT_AUTHOR_NAME <$GIT_AUTHOR_EMAIL>`. Do not change `user.name`/`user.email`
  and do not pass `--author`.
- **Stay in your own space.** Work inside this session's cwd. Never edit files in
  another member's project directory.
- **Branch, never commit to a shared branch.** Work on `$AGENT_USER/<topic>`.
  Never commit directly to `main`/`master`.
- **Sync before you start** — fetch and rebase so you are not building on stale
  code.
- **Push your branch and open a PR.** Never force-push a shared branch.
- **Isolate when sharing a repo.** If someone else is already in a repo's working
  tree, make your own worktree rather than fighting over it:
  `git worktree add ../<repo>-$AGENT_USER -b $AGENT_USER/<topic>`.
- **Never commit secrets** (`.env`, tokens, keys). Check `git status` first.

## Files and processes

- Long-running work belongs in the background, not in a blocking foreground
  command that holds the pane.
- Anything you start that outlives the turn (a server, a watcher) must be
  something you can also stop. Say how, in your final message.
- Prefer editing a file in place over rewriting it wholesale.

## Verification

A change is not done because it was written. Run it. For a web change, load the
page. For a script, execute it. Paste the evidence.
