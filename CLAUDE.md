# tmux-dashboard

Global rules (how to work, git, where things are) are in `~/CLAUDE.md` — don't restate them here.

- If you think you're done, you're not: re-check the scope of the task, then do some QA.
- This app is a single 17k-line `app.py` (FastAPI + inline HTML/CSS/JS). Backend routes, styles and frontend JS all live in it; there is no `static/` or `templates/`.
- It runs from this directory under supervisor as `tmux-dashboard` (:8501) — after editing, `sudo supervisorctl restart tmux-dashboard`.
