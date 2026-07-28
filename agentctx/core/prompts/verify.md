---
description: Prove the last change actually works, end to end, before calling it done.
---

Verify the change we just made to $ARGUMENTS. Do not describe what should
happen — make it happen and paste what came back.

1. Re-read the diff (`git diff`, or `git diff HEAD~1` if it is committed) and
   state in one line what it is supposed to change.
2. Run whatever proves it at the lowest level available: the test, the script,
   the endpoint. Paste the real output, including failures.
3. Exercise it the way a person would. If it is a page, load it in the browser
   and interact with it — see the `browser-qa` skill. If it is a CLI, run it.
   If it is a service, restart it and hit it.
4. Look for what the change could have broken next door: callers of the function
   you edited, other routes on the same file, the other backend if the change
   touched shared code.
5. Report: what you ran, what it returned, what is verified, what is still
   unverified. If something failed, say so plainly and stop — do not "fix" it in
   the same breath without saying you did.
