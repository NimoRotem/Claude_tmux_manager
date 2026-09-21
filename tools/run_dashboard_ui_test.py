#!/usr/bin/env python3
"""Run tools/dashboard_ui_test.js against a dashboard app.py.

The whole UI is one inline <script> inside a Python string, so there is nothing
node can require. This pulls HTML_PAGE out of the AST (never imports app.py —
importing it on a live box touches that box's state), writes the script to a
temp file and hands it to the node test, which drives it against a stub DOM.

    python3 tools/run_dashboard_ui_test.py [path/to/app.py]

Defaults to the app.py beside this tools/ directory. Exit 0 = every check passed.
"""
from __future__ import annotations

import ast
import os
import pathlib
import re
import subprocess
import sys
import tempfile


def inline_script(app_py: pathlib.Path) -> str:
    html = None
    for node in ast.parse(app_py.read_text()).body:
        if isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id == "HTML_PAGE" for t in node.targets):
            # The later `HTML_PAGE = HTML_PAGE.replace(...)` lines are Calls, not
            # literals; only the string assignment is the template.
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                html = node.value.value
    if html is None:
        raise SystemExit("HTML_PAGE string literal not found in %s" % app_py)
    blocks = re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)
    if not blocks:
        raise SystemExit("no inline <script> in HTML_PAGE")
    js = "\n".join(blocks)
    for placeholder, value in (("__ROOT_PATH__", ""), ("__SIMPLE__", "false"),
                               ("__CACHE_PUSH_LEAD__", "60"),
                               ("__CACHE_ALERT_LEAD__", "300"),
                               ("__BRAND__", "test")):
        js = js.replace(placeholder, value)
    # Copies differ in which placeholders they carry (builder2a adds
    # __MODEL_GATEWAY__). Anything left is off by default, so the test exercises
    # the ordinary path rather than dying on an undefined identifier.
    js = re.sub(r"__[A-Z0-9_]+__", "false", js)
    return js


def main() -> int:
    here = pathlib.Path(__file__).resolve().parent
    app_py = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else here.parent / "app.py"
    test_js = here / "dashboard_ui_test.js"
    if not test_js.exists():
        raise SystemExit("missing %s" % test_js)
    tmp = tempfile.mkdtemp()
    page = os.path.join(tmp, "page.js")
    with open(page, "w") as fh:
        fh.write(inline_script(app_py))
    print("testing %s" % app_py)
    return subprocess.run(["node", str(test_js), page]).returncode


if __name__ == "__main__":
    sys.exit(main())
