---
description: Write a handoff note so another agent (or the other backend) can pick this up cold.
---

Write a handoff for the work in this session so someone with no context can
continue it. Cover, in this order and nothing else:

1. **Goal** — what we are trying to achieve, in one sentence.
2. **State** — what is done and verified, what is written but unverified, what is
   not started. Be exact about which is which.
3. **Where things live** — the files, branches, services and URLs that matter,
   with absolute paths.
4. **The next step** — the single thing to do next, concretely enough to start
   without asking a question.
5. **Traps** — anything that already bit us. What looked right and was not.

Then save it with the `memory` tool so it survives this session:
`memory_write(key="handoff/$ARGUMENTS", value=<the note>)`.

If `$ARGUMENTS` is empty, use the current project directory's name as the key.
