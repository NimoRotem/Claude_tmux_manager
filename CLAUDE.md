# tmux-dashboard

Global rules (how to work, git, where things are) are in `~/CLAUDE.md`. Do not restate them here.

- If you think you're done, you're not: re-check the scope of the task, then do some QA.
- This app is a single ~30k-line `app.py` (FastAPI + inline HTML/CSS/JS). Backend routes, styles and frontend JS all live in it; there is no `static/` or `templates/`.
- **The live filing and trademark apps are NOT in this repo any more.** `https://rotem.ai/patents/filing/`
  is `~/patent-filing/` (its own supervisor unit `patent-filing`, its own login, data in
  `~/.patent-filing`) and `https://rotem.ai/patents/trademarks/` is `~/trademark-filing/`. Both
  started as the modules below and forked; their copies are AHEAD. Editing the in-repo copy changes
  only the dashboard-mounted fork at `:8501/patents`, and nothing the user is looking at.
- `browser_fleet.py` answers who owns every Chrome and every CDP forward on a box, flags a dead or
  duplicated forward, a CDP port two browsers both claimed, and a throwaway profile nobody owns, and
  can ask a browser whether it is still signed in (from its cookie jar, without opening a tab).
  `python browser_fleet.py [--probe] [--reap [--yes]] [--host instance-3]`.
- The fork that is still here, mounted by app.py at `:8501/patents`
  (https://builder4.rotem.ai/patents is the direct route, and nginx passes
  `X-Forwarded-Prefix`): `patent_panel.py` (router + page),
  `patent_store.py` (parties, presets, the 37 CFR 1.31 gate, fees), `patent_packet.py` (packet
  intake and checks), `patent_forms.py` (fills the official USPTO PDFs), `browser_live.py`
  (CDP screencast viewer). app.py only mounts them. Data lives in `~/.tmux-dashboard/patents/`.
  Two traps worth knowing: Patent Center rejects a PDF whose BaseFont name contains a space even
  when the font IS embedded (re-distil with `gs -dSubsetFonts=true`), and Chrome 152 on this box
  never answers `Network.setCookie` over CDP, so a browser that must be logged in runs on
  instance-3 (Chrome 149) behind an ssh port-forward.
- It runs from this directory under supervisor as `tmux-dashboard` (:8501). After editing, `sudo supervisorctl restart tmux-dashboard`.
