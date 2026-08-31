"""Runs ON grabo-systems. Drives GRABO Sign over its loopback port.

The app listens on 127.0.0.1:8537 and takes the signed-in user from the
`X-Forwarded-Auth-User` header, which nginx sets after its own auth_request. From
the box itself we are on the trusted side of that proxy, so the header is all the
authentication there is, and no session cookie or password is needed.

Two headers matter besides that one: Host and X-Forwarded-Proto. GRABO Sign builds
the signing links from them, so without `Host: grabo.cc` every signer gets a link to
http://127.0.0.1:8537 and the email is useless.

Reads one job as JSON (a file path in argv[1]), prints one JSON result.
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.request
import uuid

BASE = os.environ.get("DOCSIGN_BASE", "http://127.0.0.1:8537/data-dashboard/docsign")
PUBLIC_HOST = os.environ.get("DOCSIGN_HOST", "grabo.cc")


def _headers(user, extra=None):
    h = {"X-Forwarded-Auth-User": user,
         "Host": PUBLIC_HOST,
         "X-Forwarded-Host": PUBLIC_HOST,
         "X-Forwarded-Proto": "https",
         "Accept": "application/json"}
    h.update(extra or {})
    return h


def _req(method, path, user, data=None, headers=None, timeout=180):
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers=_headers(user, headers))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()
            try:
                return json.loads(body.decode())
            except Exception:
                return {"_raw": base64.b64encode(body).decode(), "_status": r.status}
    except urllib.error.HTTPError as e:
        raise RuntimeError("%s %s -> %s %s: %s"
                           % (method, path, e.code, e.reason,
                              e.read().decode("utf-8", "replace")[:300]))


def _multipart(fields, files):
    """fields: {name: str}. files: [(name, filename, bytes)]."""
    boundary = "----docsign" + uuid.uuid4().hex
    out = []
    for k, v in fields.items():
        if v is None:
            continue
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                    % (boundary, k, v)).encode())
    for name, filename, blob in files:
        ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
        out.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; filename=\"%s\"\r\n"
                    "Content-Type: %s\r\n\r\n" % (boundary, name, filename, ctype)).encode())
        out.append(blob)
        out.append(b"\r\n")
    out.append(("--%s--\r\n" % boundary).encode())
    return b"".join(out), "multipart/form-data; boundary=" + boundary


# Which detected field types a signer is actually expected to complete. Anything
# else (a pre-filled text box the form already carries) is left alone.
SIGNABLE = {"signature", "initials", "date", "text", "email", "checkbox"}


def do_send(job):
    user = job.get("user") or "Nimo"
    pdf = base64.b64decode(job["pdf_b64"])
    title = job.get("title") or "Document"
    parties = job.get("parties") or []
    if not parties:
        raise RuntimeError("no parties: nobody to send to")

    body, ctype = _multipart(
        {"title": title,
         "sender_name": job.get("sender_name") or "",
         "sender_email": job.get("sender_email") or ""},
        [("file", job.get("filename") or "document.pdf", pdf)])
    created = _req("POST", "/api/documents", user, body, {"Content-Type": ctype})
    doc_id = created["id"]

    detected = {}
    if job.get("detect", True):
        try:
            detected = _req("POST", "/api/documents/%s/detect" % doc_id, user,
                            b"{}", {"Content-Type": "application/json"}, timeout=300)
        except Exception as exc:                                  # noqa: BLE001
            detected = {"error": str(exc)[:300]}

    doc = _req("GET", "/api/documents/%s" % doc_id, user)
    fields = doc.get("fields") or []
    signable = [f["id"] for f in fields
                if str(f.get("type") or "").lower() in SIGNABLE]

    # One signer gets every field. With several, split them in order so each
    # person is asked for their own block rather than all of them for everyone.
    payload_parties = []
    if len(parties) == 1:
        payload_parties.append(dict(parties[0], fieldIds=signable))
    else:
        chunk = max(1, len(signable) // len(parties)) if signable else 0
        for i, p in enumerate(parties):
            start = i * chunk
            end = (i + 1) * chunk if i < len(parties) - 1 else len(signable)
            payload_parties.append(dict(p, fieldIds=signable[start:end]))

    sent = _req("POST", "/api/documents/%s/send" % doc_id, user,
                json.dumps({"parties": payload_parties,
                            "message": job.get("message") or "",
                            "sender_name": job.get("sender_name") or "",
                            "sender_email": job.get("sender_email") or ""}).encode(),
                {"Content-Type": "application/json"}, timeout=300)
    return {"ok": True, "document_id": doc_id,
            "title": title,
            "fields_detected": len(fields),
            "fields_assigned": len(signable),
            "detect_error": detected.get("error") if isinstance(detected, dict) else None,
            "sent": sent.get("sent") or [],
            "view_url": "https://%s/data-dashboard/docsign/d/%s" % (PUBLIC_HOST, doc_id)}


def do_status(job):
    """GET /api/documents/:id answers {doc, fields, parties}.

    The owner view deliberately does NOT return each party's signing token, so a
    signing link can only come from the `send` call that created it. Keep the link
    from then; it cannot be recovered here.
    """
    user = job.get("user") or "Nimo"
    res = _req("GET", "/api/documents/%s" % job["document_id"], user)
    doc = res.get("doc") or {}
    parties = res.get("parties") or []
    return {"ok": True, "document_id": job["document_id"],
            "status": doc.get("status"), "title": doc.get("title"),
            "completed_at": doc.get("completed_at"),
            "signed": sum(1 for p in parties if p.get("signed_at")),
            "total": len(parties),
            "parties": [{"name": p.get("name"), "email": p.get("email"),
                         "signed_at": p.get("signed_at"),
                         "viewed_at": p.get("viewed_at")} for p in parties]}


def do_final(job):
    """The finished, stamped PDF, base64 so it can travel back over ssh.

    Taken from the OWNER endpoint with final=1 rather than a signer token, because
    the owner view never returns tokens and this is the same file.
    """
    user = job.get("user") or "Nimo"
    res = _req("GET", "/api/documents/%s" % job["document_id"], user)
    doc = res.get("doc") or {}
    if doc.get("status") != "completed":
        raise RuntimeError("not signed yet (status %r): %d of %d parties have signed"
                           % (doc.get("status"),
                              sum(1 for p in (res.get("parties") or []) if p.get("signed_at")),
                              len(res.get("parties") or [])))
    req = urllib.request.Request(
        BASE + "/api/documents/%s/pdf?final=1" % job["document_id"], headers=_headers(user))
    with urllib.request.urlopen(req, timeout=180) as r:
        blob = r.read()
    if not blob.startswith(b"%PDF"):
        raise RuntimeError("final PDF not ready (%d bytes, not a PDF)" % len(blob))
    return {"ok": True, "pdf_b64": base64.b64encode(blob).decode(), "bytes": len(blob)}


def do_probe(job):
    return {"ok": True, "health": _req("GET", "/healthz", job.get("user") or "Nimo"),
            "config": _req("GET", "/api/config", job.get("user") or "Nimo")}


ACTIONS = {"send": do_send, "status": do_status, "final": do_final, "probe": do_probe}

if __name__ == "__main__":
    try:
        with open(sys.argv[1], encoding="utf8") as fh:
            job = json.load(fh)
        fn = ACTIONS.get(job.get("action") or "probe")
        if not fn:
            raise RuntimeError("unknown action %r" % job.get("action"))
        print(json.dumps(fn(job)))
    except Exception as exc:                                      # noqa: BLE001
        print(json.dumps({"ok": False, "error": str(exc)[:600]}))
        sys.exit(1)
