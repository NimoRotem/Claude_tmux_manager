---
name: deploy-dashboard
description: Deploy this agent dashboard after editing it. Use whenever app.py, providers.py or anything under agentctx/ changes and the running service needs to pick it up.
---

# Deploy the dashboard

The app is a long-running service. Editing the file changes nothing until the
service restarts, and a restart that fails leaves the dashboard down — so
validate first, restart second, verify third.

## Steps

1. **Syntax-check before touching the service.**
   ```
   python3 -c "import ast; ast.parse(open('app.py').read())"
   ```
   Then import it — that catches missing names and duplicate routes that a
   syntax check does not:
   ```
   python3 -c "import app; print(len(app.app.routes))"
   ```

2. **Re-render the agent context** if anything under `agentctx/core/` changed:
   ```
   python3 -m agentctx.cli render --all
   ```

3. **Restart.**
   ```
   sudo systemctl restart agent-dashboard    # or: supervisorctl restart tmux-dashboard
   ```
   `KillMode=process` is set deliberately: managed tmux sessions must survive a
   dashboard restart. If you change the unit, keep it.

4. **Verify.**
   ```
   curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:<port>/
   sudo journalctl -u agent-dashboard -n 40 --no-pager
   ```
   A 200 and a clean log. If the log shows a traceback, the service is up but
   broken — fix it before reporting success.

## Rules

- Never restart without the import check. A NameError in a route only surfaces at
  request time, and by then the dashboard is already serving 500s.
- Never edit the copy under the service's working directory by hand and leave git
  behind — that is how three divergent lineages of this app came to exist.
