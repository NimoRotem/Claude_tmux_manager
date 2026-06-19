# Claude Code Instructions

Work as independently as possible. Follow these guidelines:

- Don't be lazy. Do all the tasks. Do not stop until done.
- Do NOT ask clarifying questions — make reasonable assumptions and proceed.
- Do NOT ask for confirmation before taking actions — just do it.
- Don't stop and wait for clarifications. Done is better than perfect. Just do it.
- If you think you're done, you're not. Re-check the scope of the task; if everything seems done, do some more QA.
- **No menu-at-the-end.** Never end a response with "want me to also X?" / "should I do Y or Z?" / "add A now or later?". If the adjacent improvement is obviously useful, INCLUDE it in the same turn — don't surface it as a choice.
- **No upfront permission-asking either.** Same rule applies before acting: don't ask "should I X?" Decide and do.
- **Be concise. Lead with the answer.** Write short, plain, to-the-point. Cut preamble, analogies, hedging, and don't restate the question. Use the fewest lines that fully answer — tight lines over headers/sections. For explanations follow: what happened → cause → fix → recommendation, ~1 line each.

## Git

- Never commit secrets (.env, API keys, private keys).
- Always check `git status` before committing.
- Never commit directly to `main`; use a feature branch → commit → push → PR → merge → pull main.
- Clear commit messages: a short summary line, then an optional explanation.
- Don't force-push unless explicitly instructed.
