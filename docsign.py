"""Send a generated form to GRABO Sign, from builder4.

GRABO Sign (supervisor `quicksign`, /home/nimrod_rotem/quicksign) listens on
127.0.0.1:8537 on grabo-systems and takes the signed-in user from the
`X-Forwarded-Auth-User` header that nginx sets after its own auth_request. There is
no API key and no service account: the only way in is to be on the box, on the
trusted side of that proxy. So this ships a job and a small agent script over ssh
and runs them there, rather than pretending an HTTP call from here could work.

Nothing is installed on grabo-systems. The agent goes over stdin every time, so it
is always the version in this repo and there is no second copy to drift.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import uuid
from pathlib import Path

HOST = os.environ.get("DOCSIGN_HOST_VM", "grabo-systems")
ZONE = os.environ.get("DOCSIGN_ZONE", "us-central1-b")
REMOTE_USER = os.environ.get("DOCSIGN_REMOTE_USER", "nimrod_rotem")
SIGN_USER = os.environ.get("DOCSIGN_USER", "Nimo")
AGENT = Path(__file__).with_name("docsign_agent.py")
VIEW_BASE = "https://grabo.cc/data-dashboard/docsign"


class DocsignError(RuntimeError):
    pass


def _ssh(command: str, stdin: bytes = b"", timeout: int = 420) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["gcloud", "compute", "ssh", "%s@%s" % (REMOTE_USER, HOST), "--zone", ZONE,
         "--quiet", "--command", command],
        input=stdin, capture_output=True, timeout=timeout)


def _run(job: dict, timeout: int = 420) -> dict:
    """Put the job on the box, run the agent against it, bring the answer back."""
    if not AGENT.exists():
        raise DocsignError("agent script missing: %s" % AGENT)
    remote = "/tmp/docsign-%s.json" % uuid.uuid4().hex[:12]
    try:
        put = _ssh("sudo -u %s bash -c 'cat > %s && chmod 600 %s'"
                   % (REMOTE_USER, remote, remote),
                   json.dumps(job).encode(), timeout=timeout)
        if put.returncode != 0:
            raise DocsignError("could not stage the job on %s: %s"
                               % (HOST, (put.stderr or b"").decode()[-300:]))
        run = _ssh("sudo -u %s python3 - %s" % (REMOTE_USER, remote),
                   AGENT.read_bytes(), timeout=timeout)
        out = (run.stdout or b"").decode("utf-8", "replace").strip()
        line = next((l for l in reversed(out.splitlines()) if l.startswith("{")), "")
        if not line:
            raise DocsignError("no answer from GRABO Sign: %s"
                               % ((run.stderr or b"").decode()[-300:] or out[-300:]))
        result = json.loads(line)
        if not result.get("ok"):
            raise DocsignError(result.get("error") or "GRABO Sign refused the request")
        return result
    finally:
        # The job carries the document; do not leave it in /tmp on a shared box.
        try:
            _ssh("sudo -u %s rm -f %s" % (REMOTE_USER, remote), timeout=90)
        except Exception:
            pass


def probe() -> dict:
    return _run({"action": "probe", "user": SIGN_USER}, timeout=180)


def send(pdf_path, title: str, parties: list, message: str = "",
         sender_name: str = "", sender_email: str = "", detect: bool = True) -> dict:
    """parties: [{"name": ..., "email": ...}]. Returns document id and signing links."""
    pdf_path = Path(pdf_path)
    if not pdf_path.exists():
        raise DocsignError("no such file: %s" % pdf_path)
    clean = [{"name": (p.get("name") or "").strip(), "email": (p.get("email") or "").strip()}
             for p in parties]
    missing = [p["name"] or "(unnamed)" for p in clean if not p["email"]]
    if missing:
        raise DocsignError("no email address for: %s. Add it on the Settings tab."
                           % ", ".join(missing))
    if not clean:
        raise DocsignError("nobody to send to")
    return _run({
        "action": "send", "user": SIGN_USER,
        "pdf_b64": base64.b64encode(pdf_path.read_bytes()).decode(),
        "filename": pdf_path.name, "title": title, "parties": clean,
        "message": message, "sender_name": sender_name, "sender_email": sender_email,
        "detect": detect,
    }, timeout=600)


def status(document_id: str) -> dict:
    return _run({"action": "status", "user": SIGN_USER, "document_id": document_id},
                timeout=180)


def final_pdf(document_id: str, dest) -> dict:
    """Pull the stamped, signed PDF back and write it over `dest`."""
    res = _run({"action": "final", "user": SIGN_USER, "document_id": document_id},
               timeout=300)
    blob = base64.b64decode(res["pdf_b64"])
    dest = Path(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(blob)
    return {"ok": True, "path": str(dest), "bytes": len(blob)}


if __name__ == "__main__":
    import sys
    action = sys.argv[1] if len(sys.argv) > 1 else "probe"
    if action == "probe":
        print(json.dumps(probe(), indent=1))
    elif action == "status":
        print(json.dumps(status(sys.argv[2]), indent=1))
    else:
        print("usage: docsign.py probe | status <document_id>")
