# tmux-dashboard

Global rules (how to work, git, where things are) are in `~/CLAUDE.md` — don't restate them here.

- If you think you're done, you're not: re-check the scope of the task, then do some QA.
- This app is a single 17k-line `app.py` (FastAPI + inline HTML/CSS/JS). Backend routes, styles and frontend JS all live in it; there is no `static/` or `templates/`.
- The exception is the USPTO filing panel, served at **https://rotem.ai/patents/filing/**
  (nginx on the `builder` VM proxies it to this box's :8501 `/patents` with
  `X-Forwarded-Prefix`; https://builder4.rotem.ai/patents still works and is the direct route): `patent_panel.py` (router + page),
  `patent_store.py` (parties, presets, the 37 CFR 1.31 gate, fees), `patent_packet.py` (packet
  intake and checks), `patent_forms.py` (fills the official USPTO PDFs), `browser_live.py`
  (CDP screencast viewer). app.py only mounts them. Data lives in `~/.tmux-dashboard/patents/`.
  Two traps worth knowing: Patent Center rejects a PDF whose BaseFont name contains a space even
  when the font IS embedded (re-distil with `gs -dSubsetFonts=true`), and Chrome 152 on this box
  never answers `Network.setCookie` over CDP, so a browser that must be logged in runs on
  instance-3 (Chrome 149) behind an ssh port-forward.
- It runs from this directory under supervisor as `tmux-dashboard` (:8501) — after editing, `sudo supervisorctl restart tmux-dashboard`.
