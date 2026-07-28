---
name: browser-qa
description: Drive a real browser to verify a web change before calling it done. Use whenever a change affects a page a person will look at — layout, a form, an auth flow, anything visual.
---

# Browser QA

A change to a page is not verified until a browser has rendered it. "The code
looks right" and "the endpoint returns 200" are both compatible with a blank
screen.

## Steps

1. **Find the URL.** Local app → `http://127.0.0.1:<port><root path>`. Deployed →
   the public URL. If you cannot name the URL, you cannot QA it.
2. **Load it headless first**, since it is cheap:
   `curl -s -o /dev/null -w '%{http_code}' <url>`. A non-200 stops you here.
3. **Render it.** Use the browser session the dashboard manages (Settings →
   Browser gives you a noVNC viewer and a CDP port). Navigate, screenshot, look
   at the screenshot.
4. **Exercise the actual change.** Click the button. Submit the form. Log in.
   A screenshot of a page that merely *contains* your change proves nothing about
   whether it works.
5. **Check the console.** A page can look perfect and be throwing on every
   interaction. Read the console log before you call it done.

## Rules

- A click that "succeeded" is not proof — re-read the state after every click and
  fill. Sticky headers, off-screen elements and custom selects all swallow input
  while reporting success.
- Screenshot before and after when the change is visual. Attach both.
- If the page needs a login you do not have, say so and stop. Do not fabricate a
  verification you did not perform.
