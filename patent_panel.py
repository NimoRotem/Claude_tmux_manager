"""The patent filing panel: everything a submission needs, then hand it to an agent.

Mounted into the dashboard as its own router so a 28k-line app.py does not have to
grow another 1.5k. Three jobs:

  * hold and edit the party data (patent_store), with the 37 CFR 1.31 representation
    gate evaluated live, because that is the rule that silently invalidates a pro se
    filing and you want to know before you build the packet, not after;
  * take a draft in any shape, check it against what Patent Center actually enforces
    (patent_packet), fix the two defects that are auto-fixable, and generate the
    official forms (patent_forms);
  * open a tmux Claude session with the packet, the resolved data and a written
    brief, and give you a live view of the browser it drives (browser_live).

Session creation goes back through the dashboard's own HTTP API with the caller's
cookies forwarded, rather than reaching into app.py internals, so all the auth,
profile and environment setup keeps working and this module stays decoupled.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile, WebSocket
from fastapi.responses import HTMLResponse, JSONResponse, Response

import browser_live
import docsign
import patent_forms
import patent_packet as pkt
import patent_store as store

# The dashboard installs the real check at mount time. Until it does, everything
# here is refused: this panel holds personal addresses and can spend money on a
# card, and on this box the dashboard port is reachable from the internet, so the
# safe default when the wiring is missing is closed, not open.
_AUTH_GUARD = None


def set_auth_guard(fn):
    """fn(cookies: dict) -> bool. Applied to every route AND the websocket.

    The HTTP auth middleware in app.py does not run for websockets, so the guard
    has to be enforced separately there; this is the single place both go through.
    """
    global _AUTH_GUARD
    _AUTH_GUARD = fn


def _allowed(cookies) -> bool:
    if _AUTH_GUARD is None:
        return False
    try:
        return bool(_AUTH_GUARD(dict(cookies or {})))
    except Exception:
        return False


async def _guard(request: Request):
    if not _allowed(request.cookies):
        raise HTTPException(status_code=403, detail="not authorised for the patent panel")


# HTTP routes carry the guard as a router dependency. The websocket cannot: a
# dependency declaring `Request` is unsatisfiable in a websocket scope and FastAPI
# 500s before the handler runs, so the live view gets its own router and checks the
# cookie itself in the handler.
router = APIRouter(prefix="/patents", tags=["patents"], dependencies=[Depends(_guard)])
ws_router = APIRouter(prefix="/patents", tags=["patents"])

MAX_UPLOAD = 60 * 1024 * 1024
_BROWSERS: dict = {}          # port -> launch info


def _packet_dir(packet_id: str) -> Path:
    safe = "".join(c for c in packet_id if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError("bad packet id")
    return store.PACKETS_DIR / safe


def _err(msg, code=400):
    return JSONResponse({"error": msg}, status_code=code)


# ==========================================================================
# data
# ==========================================================================
@router.get("/api/store")
async def api_store():
    data = store.load()
    return JSONResponse({"store": data, "fees_verified": store.FEE_SCHEDULE_VERIFIED,
                         "roles": pkt.ROLE_LABELS})


@router.post("/api/data/{collection}")
async def api_save(collection: str, request: Request):
    if collection not in store.COLLECTIONS:
        return _err("unknown collection %r" % collection, 404)
    row = await request.json()
    try:
        return JSONResponse({"ok": True, "row": store.upsert(collection, row)})
    except Exception as exc:
        return _err(str(exc), 500)


@router.delete("/api/data/{collection}/{row_id}")
async def api_delete(collection: str, row_id: str):
    if collection not in store.COLLECTIONS:
        return _err("unknown collection %r" % collection, 404)
    return JSONResponse({"ok": store.delete(collection, row_id)})


@router.post("/api/defaults")
async def api_defaults(request: Request):
    patch = await request.json()
    data = store.load()
    data.setdefault("defaults", {}).update(
        {k: v for k, v in patch.items() if isinstance(k, str)})
    store.save(data)
    return JSONResponse({"ok": True, "defaults": data["defaults"]})


@router.post("/api/gate")
async def api_gate(request: Request):
    body = await request.json()
    data = store.load()
    gate = store.representation_gate(data, body.get("inventor_ids") or [],
                                     body.get("applicant_id") or "")
    fees = store.estimate_fees(
        entity_status=body.get("entity_status") or "small",
        total_claims=int(body.get("total_claims") or 20),
        independent_claims=int(body.get("independent_claims") or 3),
        sheets=int(body.get("sheets") or 0),
        docx_spec=bool(body.get("docx_spec", True)),
        oath_with_application=bool(body.get("oath_with_application", True)))
    return JSONResponse({"gate": gate, "fees": fees})


# ==========================================================================
# packet
# ==========================================================================
@router.post("/api/packet")
async def api_packet_new(request: Request):
    body = await request.json()
    title = (body.get("title") or "").strip()
    packet_id = "%s-%s" % (int(time.time()), store.slugify(title, "draft"))
    d = _packet_dir(packet_id)
    (d / "in").mkdir(parents=True, exist_ok=True)
    (d / "files").mkdir(parents=True, exist_ok=True)
    (d / "forms").mkdir(parents=True, exist_ok=True)
    meta = dict(body, id=packet_id, created=time.time(), files=[])
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf8")
    return JSONResponse({"ok": True, "packet": meta})


@router.post("/api/packet/{packet_id}/upload")
async def api_packet_upload(packet_id: str, file: UploadFile = File(...)):
    try:
        d = _packet_dir(packet_id)
    except ValueError as exc:
        return _err(str(exc))
    if not d.exists():
        return _err("unknown packet", 404)
    name = os.path.basename(file.filename or "upload")
    if not name or name.startswith("."):
        return _err("invalid filename")
    body = await file.read()
    if len(body) > MAX_UPLOAD:
        return _err("file too large (%.1f MB, max %d MB)"
                    % (len(body) / 1e6, MAX_UPLOAD // 1024 // 1024), 413)
    (d / "in" / name).write_bytes(body)
    return JSONResponse({"ok": True, "name": name, "bytes": len(body)})


def _load_meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf8"))
    except Exception:
        return {}


def _save_meta(d: Path, meta: dict):
    (d / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf8")


@router.post("/api/packet/{packet_id}/scan")
async def api_packet_scan(packet_id: str):
    """Unpack whatever arrived, guess each file's role, and run every check."""
    d = _packet_dir(packet_id)
    if not d.exists():
        return _err("unknown packet", 404)
    meta = _load_meta(d)
    files_dir = d / "files"
    shutil.rmtree(files_dir, ignore_errors=True)
    files_dir.mkdir(parents=True, exist_ok=True)
    expanded = await asyncio.to_thread(pkt.expand, d / "in", files_dir)
    prior = {Path(f["path"]).name: f.get("role") for f in (meta.get("files") or [])}
    entries = []
    for p in expanded:
        role = prior.get(p.name) or await asyncio.to_thread(pkt.guess_role, p)
        entries.append({"path": str(p), "name": p.name, "role": role})
    report = await asyncio.to_thread(pkt.review_packet, entries)
    meta["files"] = entries
    meta["report"] = report
    meta["scanned"] = time.time()
    _save_meta(d, meta)
    return JSONResponse({"ok": True, "files": entries, "report": report})


@router.post("/api/packet/{packet_id}/role")
async def api_packet_role(packet_id: str, request: Request):
    body = await request.json()
    d = _packet_dir(packet_id)
    meta = _load_meta(d)
    for f in meta.get("files") or []:
        if f["name"] == body.get("name"):
            f["role"] = body.get("role") or "other"
    report = await asyncio.to_thread(pkt.review_packet, meta.get("files") or [])
    meta["report"] = report
    _save_meta(d, meta)
    return JSONResponse({"ok": True, "report": report, "files": meta.get("files")})


@router.post("/api/packet/{packet_id}/clean")
async def api_packet_clean(packet_id: str):
    """Apply the two auto-fixable DOCX defects. Nothing a reader can see changes."""
    d = _packet_dir(packet_id)
    meta = _load_meta(d)
    fixed = []
    for f in meta.get("files") or []:
        p = Path(f["path"])
        if p.suffix.lower() != ".docx" or f.get("role") == "exclude":
            continue
        try:
            info = await asyncio.to_thread(pkt.docx_cleanliness, p)
            if not info["cleanable"]:
                continue
            tmp = p.with_suffix(".cleaned.docx")
            res = await asyncio.to_thread(pkt.clean_docx, p, tmp)
            tmp.replace(p)
            fixed.append({"name": p.name, **res})
        except Exception as exc:
            fixed.append({"name": p.name, "error": str(exc)[:200]})
    report = await asyncio.to_thread(pkt.review_packet, meta.get("files") or [])
    meta["report"] = report
    _save_meta(d, meta)
    return JSONResponse({"ok": True, "fixed": fixed, "report": report})


@router.post("/api/packet/{packet_id}/forms")
async def api_packet_forms(packet_id: str, request: Request):
    """Generate the official USPTO forms this filing needs, and validate each."""
    body = await request.json()
    d = _packet_dir(packet_id)
    if not d.exists():
        return _err("unknown packet", 404)
    meta = _load_meta(d)
    data = store.load()
    title = (body.get("title") or meta.get("title") or "").strip()
    docket = (body.get("docket") or meta.get("docket") or "").strip()
    inv_ids = body.get("inventor_ids") or meta.get("inventor_ids") or []
    wanted = set(body.get("wanted") or ["declaration"])
    if "all" in wanted:
        wanted = {"declaration", "age_petition", "poa", "statement_373"}
    forms_dir = d / "forms"
    forms_dir.mkdir(parents=True, exist_ok=True)
    made = []

    def _add(path, label, note=""):
        report = patent_forms.verify(path)
        made.append({"path": str(path), "name": Path(path).name, "label": label,
                     "note": note, "verify": report})

    try:
        invs = [store.find(data, "inventors", i) for i in inv_ids]
        invs = [i for i in invs if i]
        if not invs and wanted & {"declaration", "age_petition"}:
            return _err("pick at least one inventor before generating a declaration", 400)
        if "declaration" in wanted:
            for inv in invs:
                name = store.full_name(inv)
                out = forms_dir / ("DECLARATION_%s.pdf" % store.slugify(name, "inventor"))
                await asyncio.to_thread(patent_forms.build_declaration, out, name, title,
                                        "", "", _signature_path(inv))
                _add(out, "Declaration 37 CFR 1.63 (PTO/AIA/01), %s" % name,
                     "Carries the 1.63(a)(4) statement that hand-built packets usually miss.")
        if "age_petition" in wanted:
            for inv in invs:
                if not inv.get("age_65_plus"):
                    continue
                name = store.full_name(inv)
                out = forms_dir / ("PETITION_AGE_%s.pdf" % store.slugify(name, "inventor"))
                await asyncio.to_thread(
                    patent_forms.build_age_petition, out, inv, title,
                    application_number=body.get("application_number", ""),
                    confirmation_number=body.get("confirmation_number", ""),
                    filing_date=body.get("filing_date", ""), docket=docket,
                    first_named_inventor=store.full_name(invs[0]) if invs else name,
                    signature_image=_signature_path(inv))
                _add(out, "Petition to Make Special on Age (PTO/SB/130), %s" % name,
                     "No fee under 37 CFR 1.102(c)(1). File it after the application "
                     "number exists.")
        if "poa" in wanted:
            applicant = store.find(data, "applicants", body.get("applicant_id") or "")
            prac = store.find(data, "practitioners", body.get("practitioner_id") or "")
            if not prac:
                prac = (data.get("practitioners") or [{}])[0]
            juristic = (applicant.get("kind") == "juristic")
            signer = invs[0] if invs else {}
            out = forms_dir / "POWER_OF_ATTORNEY.pdf"
            await asyncio.to_thread(
                patent_forms.build_power_of_attorney, out,
                applicant_name=applicant.get("name", ""),
                signer_name=body.get("signer_name") or (store.full_name(invs[0]) if invs else ""),
                signer_title=body.get("signer_title", ""),
                invention_title=title, docket=docket,
                first_named_inventor=store.full_name(invs[0]) if invs else "",
                customer_number=prac.get("customer_number", ""),
                practitioners=[prac] if prac else [],
                juristic_applicant=juristic, signer_is_inventor=not juristic,
                signature_image=_signature_path(signer))
            _add(out, "Power of Attorney (PTO/AIA/82)",
                 "Appoints %s. Providing representative details on an ADS is NOT a power "
                 "of attorney (37 CFR 1.32), so this form is the thing that actually does it."
                 % (prac.get("firm") or prac.get("name") or "the practitioner"))
        if "statement_373" in wanted:
            applicant = store.find(data, "applicants", body.get("applicant_id") or "")
            out = forms_dir / "STATEMENT_3_73.pdf"
            await asyncio.to_thread(
                patent_forms.build_statement_373, out,
                assignee_name=applicant.get("name", ""),
                assignee_kind=body.get("assignee_kind", "corporation"),
                invention_title=title,
                first_named_inventor=store.full_name(invs[0]) if invs else "",
                signer_name=body.get("signer_name") or (store.full_name(invs[0]) if invs else ""),
                signer_title=body.get("signer_title", ""),
                signature_image=_signature_path(invs[0] if invs else {}))
            _add(out, "Statement under 37 CFR 3.73(c) (PTO/AIA/96)",
                 "Only needed when an assignee takes an action, including becoming the "
                 "applicant under 37 CFR 1.46(c)(2).")
    except patent_forms.FormError as exc:
        return _err(str(exc), 500)
    except Exception as exc:
        return _err("form generation failed: %s" % exc, 500)

    meta["forms"] = made
    _save_meta(d, meta)
    return JSONResponse({"ok": True, "forms": made})



# ==========================================================================
# signatures
# ==========================================================================
SIG_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}


@router.post("/api/signature/{person_id}")
async def api_signature_upload(person_id: str, file: UploadFile = File(...)):
    """Store a scanned handwritten signature for one person.

    37 CFR 1.4(d)(1) allows a handwritten signature and MPEP 502.02 accepts a copy
    of one on an electronically filed document, so this is a real alternative to
    the /S-signature/ text, not decoration. Trim it to the ink before uploading:
    the image is fitted to the signature box, so a large white margin makes the
    signature itself tiny.
    """
    data = await file.read()
    if len(data) > 4 * 1024 * 1024:
        return _err("signature image too large (max 4 MB)", 413)
    ext = SIG_TYPES.get((file.content_type or "").lower())
    if not ext:
        ext = Path(file.filename or "").suffix.lower()
        if ext not in (".png", ".jpg", ".jpeg", ".webp"):
            return _err("use a PNG, JPEG or WebP image")
    store.SIGNATURES_DIR.mkdir(parents=True, exist_ok=True)
    safe = "".join(c for c in person_id if c.isalnum() or c in "-_") or "person"
    dest = store.SIGNATURES_DIR / (safe + ext)
    for old in store.SIGNATURES_DIR.glob(safe + ".*"):
        if old != dest:
            old.unlink(missing_ok=True)
    dest.write_bytes(data)
    data_store = store.load()
    for coll in ("inventors", "practitioners"):
        for row in data_store.get(coll) or []:
            if row.get("id") == person_id:
                store.upsert(coll, {"id": person_id, "signature_file": dest.name})
    return JSONResponse({"ok": True, "file": dest.name, "bytes": len(data)})


@router.get("/api/signature/{person_id}")
async def api_signature_get(person_id: str):
    safe = "".join(c for c in person_id if c.isalnum() or c in "-_")
    for path in sorted(store.SIGNATURES_DIR.glob(safe + ".*")):
        media = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                 ".webp": "image/webp"}.get(path.suffix.lower(), "application/octet-stream")
        return Response(path.read_bytes(), media_type=media,
                        headers={"Cache-Control": "no-store"})
    return _err("no signature on file", 404)


@router.delete("/api/signature/{person_id}")
async def api_signature_delete(person_id: str):
    safe = "".join(c for c in person_id if c.isalnum() or c in "-_")
    gone = 0
    for path in store.SIGNATURES_DIR.glob(safe + ".*"):
        path.unlink(missing_ok=True)
        gone += 1
    data_store = store.load()
    for coll in ("inventors", "practitioners"):
        for row in data_store.get(coll) or []:
            if row.get("id") == person_id:
                store.upsert(coll, {"id": person_id, "signature_file": ""})
    return JSONResponse({"ok": True, "removed": gone})


def _signature_path(row: dict) -> str:
    name = (row or {}).get("signature_file") or ""
    if not name:
        return ""
    p = store.SIGNATURES_DIR / os.path.basename(name)
    return str(p) if p.exists() else ""


# ==========================================================================
# reading a generated form back
# ==========================================================================
def _form_file(packet_id: str, name: str) -> Path:
    d = _packet_dir(packet_id)
    safe = os.path.basename(name)
    path = (d / "forms" / safe).resolve()
    if not str(path).startswith(str((d / "forms").resolve())) or not path.exists():
        raise FileNotFoundError(name)
    return path


@router.get("/api/packet/{packet_id}/form/{name}")
async def api_form_pdf(packet_id: str, name: str):
    """The generated PDF itself, inline so the browser renders it."""
    try:
        path = _form_file(packet_id, name)
    except (FileNotFoundError, ValueError):
        return _err("no such form", 404)
    return Response(path.read_bytes(), media_type="application/pdf",
                    headers={"Content-Disposition": 'inline; filename="%s"' % path.name,
                             "Cache-Control": "no-store"})


@router.get("/api/packet/{packet_id}/form/{name}/thumb")
async def api_form_thumb(packet_id: str, name: str, page: int = 0):
    """A PNG of one page, so the panel can show the form without a PDF plugin."""
    try:
        path = _form_file(packet_id, name)
    except (FileNotFoundError, ValueError):
        return _err("no such form", 404)

    def _render():
        import pymupdf
        with pymupdf.open(str(path)) as doc:
            pno = max(0, min(int(page), doc.page_count - 1))
            return doc[pno].get_pixmap(dpi=110).tobytes("png")

    try:
        png = await asyncio.to_thread(_render)
    except Exception as exc:
        return _err("could not render: %s" % exc, 500)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.get("/api/packet/{packet_id}/forms")
async def api_forms_list(packet_id: str):
    meta = _load_meta(_packet_dir(packet_id))
    return JSONResponse({"forms": meta.get("forms") or []})



# ==========================================================================
# send for signature (GRABO Sign)
# ==========================================================================
@router.get("/api/docsign/health")
async def api_docsign_health():
    try:
        res = await asyncio.to_thread(docsign.probe)
        cfg = res.get("config") or {}
        return JSONResponse({"ok": True, "user": cfg.get("user"),
                             "email": cfg.get("userEmail"),
                             "ai_fields": bool(cfg.get("llmAvailable")),
                             "url": cfg.get("publicUrl")})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:300]})


@router.post("/api/packet/{packet_id}/sign")
async def api_packet_sign(packet_id: str, request: Request):
    """Send generated forms to their signers through GRABO Sign.

    One document per form, because each inventor signs their own declaration and
    they should not see each other's. GRABO Sign detects the fields with AI, emails
    a private link per party, and stamps a final PDF with an audit page.
    """
    body = await request.json()
    d = _packet_dir(packet_id)
    if not d.exists():
        return _err("unknown packet", 404)
    meta = _load_meta(d)
    items = body.get("items") or []
    if not items:
        return _err("nothing selected to send")
    message = (body.get("message") or "").strip()
    forms = {f["name"]: f for f in (meta.get("forms") or [])}
    sigs = meta.setdefault("signatures", {})
    results, data = [], store.load()

    for item in items:
        name = item.get("form_name")
        form = forms.get(name)
        if not form:
            results.append({"form": name, "ok": False, "error": "not a generated form"})
            continue
        signer_name = (item.get("signer_name") or "").strip()
        signer_email = (item.get("signer_email") or "").strip()
        if not signer_email:
            results.append({"form": name, "ok": False,
                            "error": "no email address for %s" % (signer_name or "the signer")})
            continue
        try:
            res = await asyncio.to_thread(
                docsign.send, form["path"],
                item.get("title") or form.get("label") or name,
                [{"name": signer_name or signer_email, "email": signer_email}],
                message, "Patent panel", (data.get("correspondence") or [{}])[0].get("email1", ""))
        except Exception as exc:
            results.append({"form": name, "ok": False, "error": str(exc)[:300]})
            continue
        sent = (res.get("sent") or [{}])[0]
        sigs[name] = {"document_id": res["document_id"], "title": res["title"],
                      "signer_name": signer_name, "signer_email": signer_email,
                      # GRABO Sign answers camelCase here; keep the link because the
                      # owner endpoint never returns a signing token again.
                      "sign_url": sent.get("signUrl") or sent.get("sign_url") or "",
                      "emailed": bool(sent.get("ok")),
                      "view_url": res.get("view_url", ""),
                      "fields": res.get("fields_assigned", 0),
                      "sent_at": time.time(), "status": "sent"}
        results.append({"form": name, "ok": True, **sigs[name]})
        # Remember the address, so the next filing does not ask again.
        inv_id = item.get("inventor_id")
        if inv_id and signer_email:
            row = store.find(data, "inventors", inv_id)
            if row and not (row.get("email") or "").strip():
                store.upsert("inventors", {"id": inv_id, "email": signer_email})
                data = store.load()

    _save_meta(d, meta)
    return JSONResponse({"ok": any(r.get("ok") for r in results), "results": results,
                         "signatures": sigs})


@router.post("/api/packet/{packet_id}/sign/refresh")
async def api_packet_sign_refresh(packet_id: str):
    """Poll every sent document and pull back the ones that are finished."""
    d = _packet_dir(packet_id)
    meta = _load_meta(d)
    sigs = meta.get("signatures") or {}
    if not sigs:
        return JSONResponse({"ok": True, "signatures": {}})
    forms = {f["name"]: f for f in (meta.get("forms") or [])}
    for name, rec in sigs.items():
        try:
            st = await asyncio.to_thread(docsign.status, rec["document_id"])
        except Exception as exc:
            rec["error"] = str(exc)[:200]
            continue
        rec.pop("error", None)
        rec["status"] = st.get("status") or rec.get("status")
        rec["signed"] = st.get("signed", 0)
        rec["total"] = st.get("total", 0)
        rec["viewed"] = bool((st.get("parties") or [{}])[0].get("viewed_at"))
        if rec["status"] == "completed" and not rec.get("signed_file"):
            signed_name = "SIGNED_" + name
            try:
                await asyncio.to_thread(docsign.final_pdf, rec["document_id"],
                                        d / "forms" / signed_name)
            except Exception as exc:
                rec["error"] = str(exc)[:200]
                continue
            rec["signed_file"] = signed_name
            # The signed copy is what gets filed, so it joins the form list and
            # travels to the agent instead of the unsigned original.
            entry = dict(forms.get(name) or {})
            entry.update({"name": signed_name, "path": str(d / "forms" / signed_name),
                          "label": (entry.get("label") or name) + " (signed)",
                          "note": "Signed through GRABO Sign, with its audit page.",
                          "verify": patent_forms.verify(d / "forms" / signed_name)})
            meta.setdefault("forms", []).append(entry)
            forms[signed_name] = entry
    _save_meta(d, meta)
    return JSONResponse({"ok": True, "signatures": sigs, "forms": meta.get("forms") or []})


# ==========================================================================
# hand off to an agent
# ==========================================================================
BRIEF = """# USPTO filing brief

You are finishing a US patent filing end to end. Everything below was prepared and
checked by the patent panel; your job is to verify it yourself, fix anything still
wrong, drive Patent Center, pay, and report back.

## What is being filed
- Title: {title}
- Attorney docket: {docket}
- Application type: Utility, nonprovisional under 35 USC 111(a)
- Entity status: {entity_status}
- Publication: {publication}

## Parties
{parties}

## Representation gate (37 CFR 1.31, in force since 2026-07-20)
{gate}

## Fee estimate
{fees}
Patent Center's Calculate fees step is authoritative. For a small entity filing
electronically the basic filing fee is code **4011 at $70**, not the $140 paper row.

## Files
Packet: `{packet_dir}/files`
Generated forms: `{packet_dir}/forms`
{files}

## Packet check already run
{report}

## How to do it
1. Re-run the checks yourself. `patent_packet.review_packet` and
   `patent_forms.verify` live in `{dash}`; import them rather than rewriting.
2. Fix anything at fail level. The two auto-fixable DOCX defects (curly quotes read
   as "non-Latin script", and an empty comments part) may already be fixed; confirm.
3. Log in to Patent Center. Device-trust cookies are at
   `{auth_dir}/uspto_device.json` (DT + proximity, valid to 2027) and the account
   login is in the advisor as secret `uspto-account`. Inject the cookies plus a
   fresh password-only Okta session id as the `sid` cookie on auth.uspto.gov and
   USPTO skips MFA.
4. Drive a DEDICATED Chrome, not the shared one:
   `python3 -c "import browser_live; print(browser_live.launch('{slug}'))"` from
   `{dash}`. It starts with --no-proxy-server, which is required: the shared browser
   sits behind a residential proxy that 407s captcha-sdk.awswaf.com, and
   fees.uspto.gov wraps its payment POST in that script. The failure is silent, a
   "Processing..." spinner for ever and no HTTP request at all.
   Nimo can watch you live at {live_url} .
5. Use the Patent Center **web ADS**, not an uploaded ADS PDF. Upload the
   specification as **DOCX** to avoid the 37 CFR 1.16(u) surcharge. Give every file
   a document description or the submit button silently refuses.
6. Pay with the Ramp card: `get_payment_method` for `{payment_key}`. Never write the
   number to disk or into a screenshot you keep.
7. When the receipt appears, capture the application number, confirmation number and
   the payment transaction id, then close the browser you opened
   (`browser_live.shutdown(port, profile)`), and report back here.

## Non-negotiable
- Do not file if the gate above says a registered practitioner is required.
- Do not check the nonpublication request box unless explicitly told to; it forfeits
  foreign filing rights.
- Read the whole packet before submitting. You are signing a declaration under
  18 USC 1001 on Nimo's behalf, at his direction.
"""


def _fmt_report(report: dict) -> str:
    if not report:
        return "not scanned yet"
    lines = ["- %d fail, %d warn, %d pass" % (report.get("fail", 0), report.get("warn", 0),
                                              report.get("pass", 0))]
    for section in report.get("sections") or []:
        for f in section["findings"]:
            if f["level"] in ("fail", "warn"):
                lines.append("- %s: %s%s" % (f["level"].upper(), f["title"],
                                             (" (" + f["detail"] + ")") if f["detail"] else ""))
    return "\n".join(lines[:40])


@router.post("/api/packet/{packet_id}/submit")
async def api_packet_submit(packet_id: str, request: Request):
    """Open a Claude session on this packet and hand it the brief."""
    body = await request.json()
    d = _packet_dir(packet_id)
    if not d.exists():
        return _err("unknown packet", 404)
    meta = _load_meta(d)
    data = store.load()
    title = (body.get("title") or meta.get("title") or "Untitled").strip()
    docket = (body.get("docket") or meta.get("docket") or "").strip()
    inv_ids = body.get("inventor_ids") or meta.get("inventor_ids") or []
    applicant_id = body.get("applicant_id") or meta.get("applicant_id") or ""
    corr_id = body.get("correspondence_id") or meta.get("correspondence_id") or ""
    entity = body.get("entity_status") or meta.get("entity_status") or "small"
    publication = body.get("publication") or meta.get("publication") or "normal"

    snap = store.snapshot_for_agent(data, inv_ids, applicant_id, corr_id, entity,
                                    publication, docket, title)
    gate = snap["gate"]
    if gate["practitioner_required"] and not body.get("force"):
        return _err("A registered practitioner is required for this combination: "
                    + "; ".join(gate["reasons"]) + ". Fix the parties, or resubmit "
                    "with force to prepare the packet for the firm anyway.", 409)

    counts = ((meta.get("report") or {}).get("counts") or {})
    fees = store.estimate_fees(entity_status=entity,
                               total_claims=int(counts.get("total") or 20),
                               independent_claims=int(counts.get("independent") or 3))
    slug = store.slugify(title, "filing")
    session_name = ("uspto %s" % slug)[:60]

    parties = []
    for inv in snap["inventors"]:
        res = inv.get("residence") or {}
        mail = inv.get("mailing") or {}
        parties.append("- Inventor %s, residence %s %s %s, mailing %s"
                       % (store.full_name(inv), res.get("city", ""), res.get("state", ""),
                          res.get("country", ""),
                          ", ".join(x for x in (mail.get("line1"), mail.get("line2"),
                                                mail.get("city"), mail.get("state"),
                                                mail.get("postal"), mail.get("country")) if x)))
    app_row = snap["applicant"] or {}
    parties.append("- Applicant: %s (%s)" % (app_row.get("name", "the named inventors"),
                                             app_row.get("kind", "inventors")))
    corr = snap["correspondence"] or {}
    parties.append("- Correspondence: %s, %s, %s %s %s; %s / %s; %s"
                   % (corr.get("name", ""), corr.get("line1", ""), corr.get("city", ""),
                      corr.get("state", ""), corr.get("postal", ""), corr.get("email1", ""),
                      corr.get("email2", ""), corr.get("phone", "")))
    if gate["missing_inventor_data"]:
        parties.append("- STILL MISSING: " + "; ".join(gate["missing_inventor_data"]))

    gate_text = ("Pro se filing is permitted for this combination."
                 if gate["pro_se_ok"] else
                 "PRACTITIONER REQUIRED: " + "; ".join(gate["reasons"]))
    if gate["age_petition_available"]:
        gate_text += ("\nA free Petition to Make Special on Age (37 CFR 1.102(c)(1)) is "
                      "available: " + ", ".join(gate["age_petition_inventors"]) + ". It "
                      "needs the application number, so file it right after submission.")

    file_lines = []
    for f in meta.get("files") or []:
        if f.get("role") == "exclude":
            continue
        file_lines.append("- %s -> %s (Patent Center description: %s)"
                          % (f["name"], f["role"],
                             pkt.DOC_DESCRIPTIONS.get(f["role"], "choose one")))
    sigs = meta.get("signatures") or {}
    superseded = {n for n, r in sigs.items() if r.get("signed_file")}
    for f in meta.get("forms") or []:
        if f["name"] in superseded:
            continue                       # the signed copy below replaces it
        note = ""
        rec = sigs.get(f["name"])
        if rec and not rec.get("signed_file"):
            note = " [OUT FOR SIGNATURE, %s, not signed yet]" % rec.get("signer_email", "")
        file_lines.append("- %s -> generated form, %s%s" % (f["name"], f["label"], note))
    if superseded:
        file_lines.append("- the signed copies above replace the unsigned originals; "
                          "file the SIGNED_ files, not the drafts")
    unsigned_out = [n for n, r in sigs.items() if not r.get("signed_file")]
    if unsigned_out:
        file_lines.append("- STILL UNSIGNED and out with a signer: " + ", ".join(unsigned_out)
                          + ". Do not file a declaration that has not come back signed; "
                            "filing without the oath costs the 37 CFR 1.16(f) surcharge "
                            "($68 small entity) and a Notice to File Missing Parts.")

    base = str(request.base_url).rstrip("/")
    brief = BRIEF.format(
        title=title, docket=docket or "(none)", entity_status=entity,
        publication=publication, parties="\n".join(parties), gate=gate_text,
        fees="\n".join("- %s (%s): $%d" % (l["label"], l["code"], l["amount"])
                       for l in fees["lines"]) + "\n- TOTAL: $%d" % fees["total"],
        packet_dir=str(d), files="\n".join(file_lines) or "- none",
        report=_fmt_report(meta.get("report")),
        dash=str(Path(__file__).parent),
        auth_dir=str(store.DATA_DIR / "auth"),
        slug=slug, live_url=base + "/patents#live",
        payment_key=(snap.get("payment") or {}).get("advisor_key", "ramp-uspto-filing-fees"))

    brief_path = d / "BRIEF.md"
    brief_path.write_text(brief, encoding="utf8")
    if body.get("dry_run"):
        # Everything up to spawning the session, so the brief can be read before a
        # Claude instance starts acting on it.
        return JSONResponse({"ok": True, "dry_run": True, "brief": brief,
                             "brief_path": str(brief_path), "gate": gate,
                             "fees": fees, "session": ""})
    (d / "filing.json").write_text(json.dumps(snap, indent=1, default=str), encoding="utf8")

    cookies = request.headers.get("cookie", "")
    headers = {"cookie": cookies} if cookies else {}
    created = ""
    try:
        async with httpx.AsyncClient(base_url=base, timeout=120, headers=headers) as client:
            resp = await client.post("/api/sessions/create",
                                     json={"name": session_name,
                                           "cwd": str(Path(__file__).parent)})
            if resp.status_code >= 400:
                return _err("could not open a session: %s" % resp.text[:300], resp.status_code)
            created = (resp.json() or {}).get("name") or ""
            if not created:
                return _err("session create returned no name", 500)

            uploads = [brief_path, d / "filing.json"]
            uploads += [Path(f["path"]) for f in (meta.get("files") or [])
                        if f.get("role") != "exclude"]
            uploads += [Path(f["path"]) for f in (meta.get("forms") or [])
                        if f["name"] not in superseded]
            for path in uploads:
                if not path.exists():
                    continue
                files = {"file": (path.name, path.read_bytes(), "application/octet-stream")}
                await client.post("/api/sessions/%s/upload" % created, files=files)

            msg = ("Please file this US patent application. The full brief is in "
                   "BRIEF.md, which I have just uploaded along with the packet, the "
                   "generated forms and filing.json (the resolved party data). Read "
                   "BRIEF.md first, verify the packet yourself, then complete the "
                   "Patent Center submission and payment and report the application "
                   "number, confirmation number and payment transaction id.")
            await client.post("/api/sessions/%s/send" % created, json={"command": msg})
    except Exception as exc:
        return _err("handoff failed: %s" % exc, 500)

    entry = store.record_filing({
        "packet_id": packet_id, "title": title, "docket": docket,
        "session": created, "entity_status": entity, "status": "handed off",
        "gate": gate, "fee_estimate": fees["total"],
    })
    meta["session"] = created
    meta["filing_id"] = entry["id"]
    _save_meta(d, meta)
    return JSONResponse({"ok": True, "session": created, "filing": entry,
                         "brief": str(brief_path)})


# ==========================================================================
# live browser
# ==========================================================================
@router.get("/api/browsers")
async def api_browsers():
    rows = []
    for port, info in list(_BROWSERS.items()):
        up = browser_live.is_up(port)
        if not up:
            _BROWSERS.pop(port, None)
            continue
        rows.append({**info, "targets": browser_live.targets(port)})
    # Anything already listening that we did not start, so an agent's own browser
    # is watchable too.
    for port in browser_live.PORT_RANGE:
        if port in _BROWSERS or not browser_live.is_up(port):
            continue
        rows.append({"port": port, "profile": "", "headless": None, "adopted": True,
                     "targets": browser_live.targets(port)})
    if browser_live.is_up(9222) and not any(r["port"] == 9222 for r in rows):
        rows.append({"port": 9222, "profile": "", "headless": False, "shared": True,
                     "targets": browser_live.targets(9222)})
    return JSONResponse({"browsers": rows, "chrome": browser_live.CHROME})


@router.post("/api/browsers")
async def api_browser_launch(request: Request):
    """Start a browser for a filing.

    Remote is the default and it is not a preference. Chrome 152 on this box never
    answers Network.setCookie or Storage.setCookies over CDP, headless or headed,
    so the USPTO device-trust cookies cannot be injected here at all. instance-3
    runs Chrome 149, where it works, and holds the device-trust file already, so
    the browser lives there behind an ssh port-forward and everything else stays
    local. Local is kept for anything that does not need a logged-in session.
    """
    body = await request.json()
    label = store.slugify(body.get("label") or "filing", "filing")
    headless = bool(body.get("headless", True))
    remote = bool(body.get("remote", True))
    try:
        if remote:
            info = await asyncio.to_thread(browser_live.launch_remote, label, headless)
        else:
            info = await asyncio.to_thread(browser_live.launch, label, headless)
    except Exception as exc:
        return _err(str(exc), 500)
    _BROWSERS[info["port"]] = info
    return JSONResponse({"ok": True, "browser": info})


@router.delete("/api/browsers/{port}")
async def api_browser_stop(port: int):
    info = _BROWSERS.pop(port, {})
    if info.get("remote"):
        res = await asyncio.to_thread(browser_live.shutdown_remote, info)
    else:
        res = await asyncio.to_thread(browser_live.shutdown, port,
                                      info.get("profile", ""), bool(info))
    return JSONResponse(res)


@ws_router.websocket("/ws/live")
async def ws_live(sock: WebSocket):
    # Router-level dependencies do not gate websockets, and this one streams a
    # logged-in USPTO session, so check the cookie by hand before accepting.
    if not _allowed(sock.cookies):
        await sock.close(code=1008)
        return
    await sock.accept()
    try:
        port = int(sock.query_params.get("port") or 0)
        target = sock.query_params.get("target") or ""
        quality = int(sock.query_params.get("q") or 55)
        interactive = sock.query_params.get("ro") != "1"
    except Exception:
        await sock.close(code=4000)
        return
    tabs = browser_live.targets(port)
    tab = next((t for t in tabs if t["id"] == target), tabs[0] if tabs else None)
    if not tab:
        await sock.send_text(json.dumps({"t": "error", "m": "no page on port %d" % port}))
        await sock.close()
        return
    await sock.send_text(json.dumps({"t": "hello", "url": tab["url"], "title": tab["title"]}))
    try:
        await browser_live.bridge(sock, tab["ws"], quality=quality, interactive=interactive)
    except Exception as exc:
        with_msg = json.dumps({"t": "error", "m": str(exc)[:200]})
        try:
            await sock.send_text(with_msg)
        except Exception:
            pass
    finally:
        try:
            await sock.close()
        except Exception:
            pass


# ==========================================================================
# page
# ==========================================================================
@router.get("", response_class=HTMLResponse)
@router.get("/", response_class=HTMLResponse)
async def page(request: Request):
    return HTMLResponse(PAGE.replace("__BASE__", str(request.base_url).rstrip("/")))


PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Patent filing</title>
<style>
:root{--bg:#0f1216;--panel:#171b21;--panel2:#1e232b;--line:#2a313b;--fg:#e6e9ee;--mut:#96a0ae;
--acc:#4b9fff;--ok:#3ecf8e;--warn:#f5a623;--bad:#ff5d5d;--rad:10px}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
header{display:flex;align-items:center;gap:16px;padding:12px 20px;background:var(--panel);border-bottom:1px solid var(--line);position:sticky;top:0;z-index:20}
header h1{font-size:16px;margin:0;font-weight:600;letter-spacing:.2px}
nav{display:flex;gap:4px;margin-left:auto;flex-wrap:wrap}
nav button{background:none;border:1px solid transparent;color:var(--mut);padding:6px 12px;border-radius:var(--rad);cursor:pointer;font-size:13px}
nav button.on{background:var(--panel2);color:var(--fg);border-color:var(--line)}
main{padding:20px;max-width:1500px;margin:0 auto}
section{display:none} section.on{display:block}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(330px,1fr));gap:14px}
.card{background:var(--panel);border:1px solid var(--line);border-radius:var(--rad);padding:14px}
.card h3{margin:0 0 10px;font-size:13px;text-transform:uppercase;letter-spacing:.6px;color:var(--mut);font-weight:600}
label{display:block;font-size:12px;color:var(--mut);margin:8px 0 3px}
input,select,textarea{width:100%;background:var(--panel2);border:1px solid var(--line);color:var(--fg);
padding:7px 9px;border-radius:8px;font:inherit;font-size:13px}
textarea{min-height:60px;resize:vertical}
button.b{background:var(--acc);border:none;color:#06121f;font-weight:600;padding:8px 14px;border-radius:8px;cursor:pointer;font-size:13px}
button.g,a.g{background:var(--panel2);border:1px solid var(--line);color:var(--fg);padding:7px 12px;border-radius:8px;cursor:pointer;font-size:13px;text-decoration:none;display:inline-block;line-height:1.2}
button.d{background:none;border:1px solid var(--line);color:var(--bad);padding:5px 9px;border-radius:8px;cursor:pointer;font-size:12px}
button:disabled{opacity:.45;cursor:not-allowed}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.row>*{flex:1} .row>.fix{flex:0 0 auto}
.pill{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;border:1px solid var(--line);color:var(--mut)}
.pill.ok{color:var(--ok);border-color:#1d5741} .pill.warn{color:var(--warn);border-color:#6b4a12}
.pill.bad{color:var(--bad);border-color:#6b2020}
.banner{border-radius:var(--rad);padding:12px 14px;margin-bottom:14px;border:1px solid}
.banner.ok{background:#10241c;border-color:#1d5741;color:#a9efd0}
.banner.bad{background:#2a1414;border-color:#6b2020;color:#ffc9c9}
.banner.warn{background:#2a2113;border-color:#6b4a12;color:#ffdfaa}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:7px 8px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--mut);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.5px}
.mono{font-family:ui-monospace,SFMono-Regular,Menlo,monospace;font-size:12px}
.drop{border:2px dashed var(--line);border-radius:var(--rad);padding:26px;text-align:center;color:var(--mut);cursor:pointer}
.drop.hot{border-color:var(--acc);color:var(--fg)}
.chk{display:flex;gap:8px;align-items:flex-start;margin:5px 0;font-size:13px}
.chk input{width:auto;flex:0 0 auto;margin-top:3px}
.muted{color:var(--mut);font-size:12px}
.split{display:grid;grid-template-columns:340px 1fr;gap:14px}
#screen{width:100%;background:#000;border-radius:var(--rad);border:1px solid var(--line);display:block}
.toast{position:fixed;right:18px;bottom:18px;background:var(--panel2);border:1px solid var(--line);
padding:10px 14px;border-radius:var(--rad);max-width:460px;z-index:50;white-space:pre-wrap}
.hint{font-size:11px;color:var(--mut);margin-top:3px}
</style></head><body>
<header>
  <h1>Patent filing</h1>
  <span class="pill" id="feestamp"></span>
  <nav>
    <button data-s="new" class="on">New filing</button>
    <button data-s="settings">Settings</button>
    <button data-s="live">Live browser</button>
    <button data-s="history">History</button>
  </nav>
</header>
<main>

<section id="s-new" class="on">
  <div id="gate"></div>
  <div class="split">
    <div>
      <div class="card">
        <h3>Preset</h3>
        <select id="preset"><option value="">Start from scratch</option></select>
        <div class="hint" id="presetnote"></div>
        <label>Title of invention</label><textarea id="title"></textarea>
        <label>Attorney docket</label><input id="docket">
        <div class="row">
          <div><label>Entity status</label><select id="entity">
            <option value="small">Small</option><option value="large">Regular undiscounted</option>
            <option value="micro">Micro</option></select></div>
          <div><label>Publication</label><select id="publication">
            <option value="normal">Normal 18 month</option>
            <option value="early">Request early</option>
            <option value="nonpublication">Nonpublication request</option></select></div>
        </div>
        <div class="hint" id="pubwarn"></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>Inventors</h3>
        <div id="invpick"></div>
        <label>Applicant</label><select id="applicant"></select>
        <label>Correspondence</label><select id="corr"></select>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>Fees</h3><div id="fees"></div>
      </div>
    </div>
    <div>
      <div class="card">
        <h3>Draft</h3>
        <div class="drop" id="drop">Drop the draft here, or click to choose.<br>
          <span class="muted">Loose files or a zip. Notes and instructions are detected and left out of the submission.</span></div>
        <input type="file" id="fileinput" multiple style="display:none">
        <div id="filelist" style="margin-top:12px"></div>
        <div class="row" style="margin-top:10px">
          <button class="g fix" id="rescan">Re-check</button>
          <button class="g fix" id="cleanbtn">Fix DOCX warnings</button>
          <span class="fix" id="scanstat"></span>
        </div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>Checks</h3><div id="report"><span class="muted">Upload a draft to see the checks.</span></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>Forms to generate</h3>
        <div class="chk"><input type="checkbox" id="f_decl" checked><label for="f_decl" style="margin:0">
          Declaration per inventor (PTO/AIA/01)<div class="hint">Includes the 37 CFR 1.63(a)(4) statement most drafting tools omit.</div></label></div>
        <div class="chk"><input type="checkbox" id="f_age"><label for="f_age" style="margin:0">
          Petition to make special on age (PTO/SB/130)<div class="hint" id="agehint">No fee. Needs an inventor marked 65 or older.</div></label></div>
        <div class="chk"><input type="checkbox" id="f_poa"><label for="f_poa" style="margin:0">
          Power of attorney (PTO/AIA/82)</label></div>
        <div class="chk"><input type="checkbox" id="f_373"><label for="f_373" style="margin:0">
          Statement under 37 CFR 3.73(c) (PTO/AIA/96)</label></div>
        <div class="row" style="margin-top:8px">
          <button class="b fix" id="genall">Generate all four</button>
          <button class="g fix" id="genforms">Generate ticked only</button>
          <span class="fix" id="formstat"></span>
        </div>
        <div id="formlist" style="margin-top:10px"></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>Send for signature</h3>
        <p class="muted">Sends each form to its signer through <b>GRABO Sign</b>
          (grabo.cc/data-dashboard/docsign). It detects the fields with AI, emails a private
          link, and stamps a final PDF with an audit page. The signed copy comes back here and
          is what gets filed. <span id="dshealth" class="pill"></span></p>
        <div id="signrows"><span class="muted">Generate the forms first.</span></div>
        <label>Message to the signers (optional)</label>
        <input id="signmsg" placeholder="Please sign the inventor declaration for the vibration device application.">
        <div class="row" style="margin-top:10px">
          <button class="b fix" id="sendsign">Send selected</button>
          <button class="g fix" id="refreshsign">Check signatures</button>
          <span class="fix" id="signstat"></span>
        </div>
        <div id="signstate" style="margin-top:10px"></div>
      </div>
      <div class="card" style="margin-top:14px">
        <h3>Submit</h3>
        <p class="muted">Opens a Claude session in tmux with the packet, the resolved party
        data and a written brief. It re-checks everything, drives Patent Center in a
        dedicated proxy-free Chrome you can watch on the Live tab, pays, and reports back.</p>
        <div class="row"><button class="b fix" id="submit">Open the filing session</button>
        <button class="g fix" id="preview">Preview the brief</button>
        <span class="fix" id="substat"></span></div>
        <pre id="briefout" class="mono" style="display:none;white-space:pre-wrap;background:var(--panel2);
          border:1px solid var(--line);border-radius:8px;padding:12px;margin-top:10px;max-height:420px;overflow:auto"></pre>
      </div>
    </div>
  </div>
</section>

<section id="s-settings">
  <div class="card" style="margin-bottom:14px">
    <h3>Signatures</h3>
    <p class="muted">A scanned handwritten signature is stamped onto every form generated for
    that person, in place of the typed <span class="mono">/Name/</span>. 37 CFR 1.4(d)(1) allows a
    handwritten signature and MPEP 502.02 accepts a copy of one on an electronically filed
    document, so either is valid. <b>Crop to the ink</b>: the image is fitted to the signature
    box, so a big white margin makes the signature tiny. PNG with a transparent background looks
    best.</p>
    <div class="grid" id="sigs"></div>
  </div>
  <div id="parties"></div>
  <div class="card" style="margin-top:14px">
    <h3>Filing defaults</h3>
    <div class="grid" id="defaults"></div>
    <button class="g" style="margin-top:10px" id="savedefaults">Save defaults</button>
  </div>
</section>

<section id="s-live">
  <div class="card">
    <div class="row">
      <div><label>Browser</label><select id="bsel"></select></div>
      <div><label>Tab</label><select id="tsel"></select></div>
      <div class="fix" style="padding-top:18px">
        <button class="g" id="battach">Attach</button>
        <button class="g" id="bnew">New browser</button>
        <button class="d" id="bkill">Close</button>
      </div>
    </div>
    <div class="row" style="margin-top:6px">
      <span class="muted fix" id="bstat">CDP screencast, not VNC: a JPEG only when the page changes.</span>
      <span class="fix" style="flex:1"></span>
      <label class="fix" style="margin:0" title="Chrome 152 on this box cannot inject cookies over CDP, so a browser that has to be logged in runs on instance-3.">
        <input type="checkbox" id="bremote" checked style="width:auto"> Run on instance-3</label>
      <label class="fix" style="margin:0"><input type="checkbox" id="binteract" style="width:auto"> Let me click and type</label>
    </div>
  </div>
  <div class="card" style="margin-top:14px">
    <img id="screen" alt="browser">
    <div class="row" style="margin-top:8px">
      <input id="navurl" placeholder="https://patentcenter.uspto.gov/">
      <button class="g fix" id="navgo">Go</button>
      <span class="muted fix" id="livestat"></span>
    </div>
  </div>
</section>

<section id="s-history"><div class="card"><h3>Filings</h3><div id="hist"></div></div></section>
</main>
<script>
const B="__BASE__", api=(p,o)=>fetch(B+"/patents"+p,o).then(async r=>{const j=await r.json().catch(()=>({}));
  if(!r.ok) throw new Error(j.error||r.statusText); return j;});
let S={}, packet=null, roles={};
const $=id=>document.getElementById(id), esc=s=>String(s==null?"":s).replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
function toast(m,ms=5200){const d=document.createElement('div');d.className='toast';d.textContent=m;
  document.body.appendChild(d);setTimeout(()=>d.remove(),ms);}
document.querySelectorAll('nav button').forEach(b=>b.onclick=()=>{
  document.querySelectorAll('nav button').forEach(x=>x.classList.toggle('on',x===b));
  document.querySelectorAll('section').forEach(s=>s.classList.toggle('on',s.id==='s-'+b.dataset.s));
  if(b.dataset.s==='live') loadBrowsers();
});

async function boot(){
  const j=await api('/api/store'); S=j.store; roles=j.roles;
  $('feestamp').textContent='fee schedule verified '+j.fees_verified;
  const d=S.defaults||{};
  $('entity').value=d.entity_status||'small';
  fill('applicant',S.applicants,'name'); fill('corr',S.correspondence,'label');
  $('applicant').value=d.applicant_id||''; $('corr').value=d.correspondence_id||'';
  $('preset').innerHTML='<option value="">Start from scratch</option>'+
    (S.presets||[]).map(p=>`<option value="${p.id}">${esc(p.label)}</option>`).join('');
  drawInventors(); drawParties(); drawHistory(); recompute();
}
function fill(id,rows,key){$(id).innerHTML=(rows||[]).map(r=>`<option value="${r.id}">${esc(r[key])}</option>`).join('');}
function drawInventors(){
  $('invpick').innerHTML=(S.inventors||[]).map(i=>{
    const res=i.residence||{}, foreign=res.country && res.country!=='US';
    return `<div class="chk"><input type="checkbox" class="inv" value="${i.id}" ${i.id==='inv_nimrod'?'checked':''}>
    <label style="margin:0">${esc(i.given)} ${esc(i.family)}
      <span class="pill ${foreign?'bad':'ok'}">${esc(res.city||'?')}, ${esc(res.country||'?')}</span>
      ${i.age_65_plus?'<span class="pill ok">65+</span>':''}
      ${foreign?'<div class="hint">Non-US domicile: naming this inventor forces a registered practitioner.</div>':''}
    </label></div>`;}).join('');
  document.querySelectorAll('.inv').forEach(c=>c.onchange=recompute);
}
const picked=()=>[...document.querySelectorAll('.inv:checked')].map(c=>c.value);

let recTimer;
function recompute(){clearTimeout(recTimer);recTimer=setTimeout(doRecompute,120);}
async function doRecompute(){
  const counts=(packet&&packet.report&&packet.report.counts)||{};
  const j=await api('/api/gate',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({inventor_ids:picked(),applicant_id:$('applicant').value,
      entity_status:$('entity').value,total_claims:counts.total||20,
      independent_claims:counts.independent||3})});
  const g=j.gate;
  $('gate').innerHTML = g.practitioner_required
    ? `<div class="banner bad"><b>A registered patent practitioner is required.</b><br>${g.reasons.map(esc).join('<br>')}
       <div class="hint" style="color:inherit;opacity:.8">37 CFR 1.31(a), in force since 20 July 2026. A document signed by anyone else is not effective.</div></div>`
    : `<div class="banner ok"><b>Pro se filing is permitted.</b> ${g.inventor_count} inventor(s), all US-domiciled, applicant is the inventor.
       ${g.age_petition_available?'<br>Free age-based petition to make special available for '+g.age_petition_inventors.map(esc).join(', ')+'.':''}</div>`;
  if(g.missing_inventor_data.length)
    $('gate').innerHTML+=`<div class="banner warn"><b>Missing data the ADS needs:</b><br>${g.missing_inventor_data.map(esc).join('<br>')}</div>`;
  $('agehint').textContent=g.age_petition_available
    ? 'Available for '+g.age_petition_inventors.join(', ')+'. No fee, 37 CFR 1.102(c)(1).'
    : 'No fee, but it needs an inventor marked 65 or older on the Settings tab.';
  $('f_age').disabled=!g.age_petition_available;
  const f=j.fees;
  $('fees').innerHTML=`<table>${f.lines.map(l=>`<tr><td>${esc(l.label)}<div class="hint mono">${esc(l.code)}</div></td>
    <td style="text-align:right">$${l.amount}</td></tr>`).join('')}
    <tr><th>Total</th><th style="text-align:right">$${f.total}</th></tr></table>
    <div class="hint">${esc(f.note)}</div>`;
  $('pubwarn').textContent = $('publication').value==='nonpublication'
    ? 'A nonpublication request forfeits the right to file abroad unless you notify the Office within 45 days. Only tick this if you will never file outside the US.' : '';
}
['applicant','entity','publication'].forEach(id=>$(id).onchange=recompute);
$('preset').onchange=()=>{
  const p=(S.presets||[]).find(x=>x.id===$('preset').value); if(!p)return;
  $('presetnote').textContent=p.notes||'';
  document.querySelectorAll('.inv').forEach(c=>c.checked=(p.inventor_ids||[]).includes(c.value));
  if(p.applicant_id)$('applicant').value=p.applicant_id;
  if(p.correspondence_id)$('corr').value=p.correspondence_id;
  if(p.entity_status)$('entity').value=p.entity_status;
  if(p.publication)$('publication').value=p.publication;
  recompute();
};

// ---- packet ----
$('drop').onclick=()=>$('fileinput').click();
$('drop').ondragover=e=>{e.preventDefault();$('drop').classList.add('hot');};
$('drop').ondragleave=()=>$('drop').classList.remove('hot');
$('drop').ondrop=e=>{e.preventDefault();$('drop').classList.remove('hot');send(e.dataTransfer.files);};
$('fileinput').onchange=e=>send(e.target.files);
async function ensurePacket(){
  if(packet)return packet;
  const j=await api('/api/packet',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({title:$('title').value,docket:$('docket').value})});
  packet=j.packet; return packet;
}
async function send(files){
  if(!files||!files.length)return;
  await ensurePacket(); $('scanstat').textContent='uploading...';
  for(const f of files){const fd=new FormData();fd.append('file',f);
    await fetch(B+'/patents/api/packet/'+packet.id+'/upload',{method:'POST',body:fd});}
  await scan();
}
async function scan(){
  if(!packet)return; $('scanstat').textContent='checking...';
  try{const j=await api('/api/packet/'+packet.id+'/scan',{method:'POST'});
    packet.files=j.files; packet.report=j.report; drawFiles(); drawReport(); recompute();
    $('scanstat').textContent='';}
  catch(e){$('scanstat').textContent='';toast('Check failed: '+e.message);}
}
$('rescan').onclick=scan;
$('cleanbtn').onclick=async()=>{
  if(!packet)return toast('Upload a draft first.');
  $('scanstat').textContent='fixing...';
  const j=await api('/api/packet/'+packet.id+'/clean',{method:'POST'});
  packet.report=j.report; drawReport(); $('scanstat').textContent='';
  toast(j.fixed.length?('Fixed: '+j.fixed.map(f=>f.name+(f.quotes_replaced?` (${f.quotes_replaced} quotes)`:'')).join(', ')):'Nothing to fix.');
};
function drawFiles(){
  $('filelist').innerHTML=`<table><tr><th>File</th><th>Role</th><th></th></tr>`+
   (packet.files||[]).map(f=>`<tr><td class="mono">${esc(f.name)}</td>
    <td><select data-n="${esc(f.name)}" class="rolesel">${Object.entries(roles).map(([k,v])=>
      `<option value="${k}" ${f.role===k?'selected':''}>${esc(v)}</option>`).join('')}</select></td>
    <td class="muted">${f.role==='exclude'?'not filed':''}</td></tr>`).join('')+`</table>`;
  document.querySelectorAll('.rolesel').forEach(s=>s.onchange=async()=>{
    const j=await api('/api/packet/'+packet.id+'/role',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({name:s.dataset.n,role:s.value})});
    packet.report=j.report; packet.files=j.files; drawReport(); recompute();});
}
function drawReport(){
  const r=packet.report; if(!r)return;
  const badge=l=>`<span class="pill ${l==='fail'?'bad':l==='warn'?'warn':l==='pass'?'ok':''}">${l}</span>`;
  $('report').innerHTML=`<div class="row" style="margin-bottom:8px">
      <span class="fix">${badge('fail')} ${r.fail}</span><span class="fix">${badge('warn')} ${r.warn}</span>
      <span class="fix">${badge('pass')} ${r.pass}</span><span class="fix muted">${(r.total_bytes/1e6).toFixed(1)} MB</span></div>`+
    (r.sections||[]).map(s=>`<div style="margin-bottom:10px"><div class="muted" style="margin-bottom:4px">${esc(s.name)}</div>`+
      s.findings.filter(f=>f.level!=='info'||s.name==='Required documents').map(f=>
      `<div style="margin:3px 0">${badge(f.level)} ${esc(f.title)}
        ${f.detail?`<div class="hint">${esc(f.detail)}</div>`:''}
        ${f.rule?`<div class="hint mono">${esc(f.rule)}</div>`:''}</div>`).join('')+`</div>`).join('');
}
async function genForms(wanted){
  await ensurePacket(); $('formstat').textContent='generating...';
  try{
    const j=await api('/api/packet/'+packet.id+'/forms',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({title:$('title').value,docket:$('docket').value,inventor_ids:picked(),
        applicant_id:$('applicant').value,wanted})});
    packet.forms=j.forms; $('formstat').textContent='';
    drawForms(); drawSignRows();
  }catch(e){$('formstat').textContent='';toast('Form generation failed: '+e.message);}
}
$('genforms').onclick=()=>{
  const wanted=[]; if($('f_decl').checked)wanted.push('declaration');
  if($('f_age').checked&&!$('f_age').disabled)wanted.push('age_petition');
  if($('f_poa').checked)wanted.push('poa'); if($('f_373').checked)wanted.push('statement_373');
  if(!wanted.length)return toast('Tick at least one form.');
  genForms(wanted);
};
$('genall').onclick=()=>genForms(['all']);
function drawForms(){
  const fs=(packet&&packet.forms)||[];
  if(!fs.length){$('formlist').innerHTML='';return;}
  const base=B+'/patents/api/packet/'+packet.id+'/form/';
  $('formlist').innerHTML=`<div class="grid" style="margin-top:6px">`+fs.map(f=>{
    const bad=(f.verify.checks||[]).filter(c=>!c.ok);
    return `<div class="card" style="background:var(--panel2)">
      <div class="row" style="align-items:flex-start">
        <div><b class="mono" style="font-size:12px">${esc(f.name)}</b>
          <div class="hint">${esc(f.label)}</div>
          ${f.note?`<div class="hint">${esc(f.note)}</div>`:''}</div>
        <span class="pill fix ${f.verify.ok?'ok':'bad'}">${f.verify.ok?'valid':'check'}</span>
      </div>
      <a href="${base}${encodeURIComponent(f.name)}" target="_blank" title="open the PDF">
        <img src="${base}${encodeURIComponent(f.name)}/thumb?t=${Date.now()}"
             style="width:100%;margin-top:10px;border-radius:8px;border:1px solid var(--line);background:#fff"></a>
      <div class="row" style="margin-top:8px">
        <a class="g fix" target="_blank" href="${base}${encodeURIComponent(f.name)}">Open PDF</a>
        <span class="fix muted">${f.verify.pages||''} page(s)</span>
      </div>
      ${bad.length?`<div class="hint" style="color:var(--bad)">${bad.map(c=>esc(c.label+' '+(c.detail||''))).join('<br>')}</div>`:''}
    </div>`;}).join('')+`</div>`;
}
$('preview').onclick=async()=>{
  if(!$('title').value.trim())return toast('Give the invention a title first.');
  await ensurePacket(); $('substat').textContent='building...';
  const body={title:$('title').value,docket:$('docket').value,inventor_ids:picked(),
    applicant_id:$('applicant').value,correspondence_id:$('corr').value,
    entity_status:$('entity').value,publication:$('publication').value,dry_run:true,force:true};
  try{const j=await api('/api/packet/'+packet.id+'/submit',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    $('briefout').style.display='block'; $('briefout').textContent=j.brief; $('substat').textContent='';}
  catch(e){$('substat').textContent='';toast(e.message);}
};
// ---- signatures ----
api('/api/docsign/health').then(h=>{
  $('dshealth').textContent = h.ok ? ('connected as '+h.user+(h.ai_fields?', AI field detection on':'')) : 'not reachable';
  $('dshealth').className = 'pill '+(h.ok?'ok':'bad');
}).catch(()=>{$('dshealth').textContent='not reachable';$('dshealth').className='pill bad';});

function signerFor(formName){
  // A declaration or an age petition belongs to the inventor named in its filename.
  for(const i of (S.inventors||[])){
    const slug=(i.given+'-'+i.family).toLowerCase().replace(/[^a-z0-9]+/g,'-');
    if(formName.toLowerCase().includes(slug)) return i;
  }
  return (S.inventors||[]).find(i=>picked().includes(i.id)) || null;
}
function drawSignRows(){
  const fs=(packet&&packet.forms)||[];
  const unsigned=fs.filter(f=>!f.name.startsWith('SIGNED_'));
  if(!unsigned.length){$('signrows').innerHTML='<span class="muted">Generate the forms first.</span>';return;}
  const sigs=(packet&&packet.signatures)||{};
  $('signrows').innerHTML=`<table><tr><th></th><th>Form</th><th>Signer</th><th>Email</th><th>State</th></tr>`+
    unsigned.map(f=>{
      const inv=signerFor(f.name); const rec=sigs[f.name]||null;
      const nm=inv?`${inv.given} ${inv.family}`:'';
      const em=rec?rec.signer_email:(inv&&inv.email)||'';
      return `<tr><td><input type="checkbox" class="signpick" data-f="${esc(f.name)}"
                 data-inv="${inv?inv.id:''}" ${rec?'':'checked'} style="width:auto"></td>
        <td class="mono" style="font-size:12px">${esc(f.name)}</td>
        <td><input class="signname" data-f="${esc(f.name)}" value="${esc(nm)}"></td>
        <td><input class="signemail" data-f="${esc(f.name)}" value="${esc(em)}" placeholder="name@example.com"></td>
        <td>${rec?signBadge(rec):'<span class="muted">not sent</span>'}</td></tr>`;
    }).join('')+`</table>`;
}
function signBadge(r){
  if(r.error)return `<span class="pill bad">${esc(r.error.slice(0,60))}</span>`;
  if(r.status==='completed')return `<span class="pill ok">signed</span>`;
  const seen=r.viewed?' opened':'';
  return `<span class="pill warn">sent${esc(seen)}</span>`;
}
function drawSignState(){
  const sigs=(packet&&packet.signatures)||{};
  const rows=Object.entries(sigs);
  if(!rows.length){$('signstate').innerHTML='';return;}
  $('signstate').innerHTML=`<table><tr><th>Document</th><th>Signer</th><th>State</th><th>Link</th></tr>`+
    rows.map(([n,r])=>`<tr><td class="mono" style="font-size:12px">${esc(n)}</td>
      <td>${esc(r.signer_name||'')}<div class="hint">${esc(r.signer_email||'')}</div></td>
      <td>${signBadge(r)}${r.emailed===false?'<div class="hint">email failed, send the link yourself</div>':''}</td>
      <td>${r.sign_url?`<a href="${esc(r.sign_url)}" target="_blank" style="color:var(--acc)">signing link</a><br>`:''}
          ${r.view_url?`<a href="${esc(r.view_url)}" target="_blank" class="hint">in GRABO Sign</a>`:''}</td></tr>`).join('')+`</table>`;
}
$('sendsign').onclick=async()=>{
  if(!packet||!(packet.forms||[]).length)return toast('Generate the forms first.');
  const items=[...document.querySelectorAll('.signpick:checked')].map(c=>{
    const f=c.dataset.f;
    return {form_name:f, inventor_id:c.dataset.inv||'',
      signer_name:(document.querySelector(`.signname[data-f="${CSS.escape(f)}"]`)||{}).value||'',
      signer_email:(document.querySelector(`.signemail[data-f="${CSS.escape(f)}"]`)||{}).value||''};
  });
  if(!items.length)return toast('Tick at least one form.');
  const noEmail=items.filter(i=>!i.signer_email.trim());
  if(noEmail.length)return toast('Add an email address for: '+noEmail.map(i=>i.form_name).join(', '));
  $('signstat').textContent='sending, this takes a moment per document...';
  try{
    const j=await api('/api/packet/'+packet.id+'/sign',{method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({items,message:$('signmsg').value})});
    packet.signatures=j.signatures; $('signstat').textContent='';
    drawSignRows(); drawSignState();
    const bad=j.results.filter(r=>!r.ok);
    toast(bad.length?('Some failed: '+bad.map(b=>b.form+': '+b.error).join('; '))
                    :'Sent. Each signer has an email with a private link.');
  }catch(e){$('signstat').textContent='';toast('Send failed: '+e.message);}
};
$('refreshsign').onclick=async()=>{
  if(!packet)return; $('signstat').textContent='checking...';
  try{
    const j=await api('/api/packet/'+packet.id+'/sign/refresh',{method:'POST'});
    packet.signatures=j.signatures; packet.forms=j.forms;
    $('signstat').textContent=''; drawSignRows(); drawSignState(); drawForms();
    const done=Object.values(j.signatures).filter(r=>r.status==='completed').length;
    toast(done?(done+' signed and pulled back into the packet.'):'Nothing signed yet.');
  }catch(e){$('signstat').textContent='';toast(e.message);}
};
$('submit').onclick=async()=>{
  if(!$('title').value.trim())return toast('Give the invention a title first.');
  await ensurePacket(); $('substat').textContent='opening session...';
  const body={title:$('title').value,docket:$('docket').value,inventor_ids:picked(),
    applicant_id:$('applicant').value,correspondence_id:$('corr').value,
    entity_status:$('entity').value,publication:$('publication').value};
  try{
    const j=await api('/api/packet/'+packet.id+'/submit',{method:'POST',
      headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    $('substat').innerHTML=`<a href="${B}/#${encodeURIComponent(j.session)}" style="color:var(--acc)">open session ${esc(j.session)}</a>`;
    toast('Session '+j.session+' has the packet and the brief.');
    drawHistory();
  }catch(e){
    $('substat').textContent='';
    if(String(e.message).includes('practitioner is required')){
      if(confirm(e.message+'\n\nPrepare the packet for the firm anyway?')){
        body.force=true;
        const j=await api('/api/packet/'+packet.id+'/submit',{method:'POST',
          headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
        $('substat').textContent='session '+j.session;
      }
    } else toast('Submit failed: '+e.message);
  }
};

// ---- parties ----
const FIELDS={
 inventors:[['given','Given name'],['middle','Middle'],['family','Family name'],['suffix','Suffix'],
   ['residence.city','Residence city'],['residence.state','Residence state'],['residence.country','Residence country'],
   ['mailing.line1','Mailing line 1'],['mailing.line2','Mailing line 2'],['mailing.city','Mailing city'],
   ['mailing.state','State'],['mailing.postal','Postcode'],['mailing.country','Country'],
   ['email','Email'],['phone','Phone'],['age_65_plus','65 or older','bool'],['notes','Notes','text']],
 applicants:[['name','Name'],['kind','Kind (inventors|juristic|person)'],['address.line1','Line 1'],
   ['address.city','City'],['address.state','State'],['address.postal','Postcode'],['address.country','Country'],['notes','Notes','text']],
 correspondence:[['label','Label'],['customer_number','Customer number'],['name','Name'],['line1','Line 1'],
   ['line2','Line 2'],['city','City'],['state','State'],['postal','Postcode'],['country','Country'],
   ['email1','Email 1'],['email2','Email 2'],['phone','Phone'],['notes','Notes','text']],
 practitioners:[['name','Name'],['registration','Registration number'],['category','Category'],
   ['firm','Firm'],['customer_number','Customer number'],['notes','Notes','text']],
 payment:[['label','Label'],['advisor_key','Advisor secret key'],['last_four','Last four'],
   ['cap','Spend cap'],['notes','Notes','text']],
 accounts:[['label','Label'],['username','Username'],['advisor_secret','Advisor secret key'],
   ['customer_numbers','Customer numbers'],['notes','Notes','text']],
};
const COLL_NOTE={
 inventors:'Residence decides the 37 CFR 1.31 gate. A non-US country here forces a registered practitioner on any filing this person is named in.',
 applicants:'Leave the applicant as the inventors to keep the pro se route. Naming a company makes it a juristic applicant, and 1.31(a)(1) then requires a practitioner for everything.',
 correspondence:'Where the Office sends the filing receipt and every notice. A customer number sends it to that firm instead of to you.',
 practitioners:'Only needed when a practitioner is actually appointed. Listing one on an ADS is not a power of attorney (37 CFR 1.32); the PTO/AIA/82 form is.',
 payment:'Card numbers are never stored here. Only the advisor key is, and the filing agent fetches the number with get_payment_method at payment time.',
 accounts:'Passwords are never stored here either, only the name of the advisor secret that holds them.',
};
const get=(o,p)=>p.split('.').reduce((a,k)=>(a||{})[k],o);
function setp(o,p,v){const ks=p.split('.');let c=o;ks.slice(0,-1).forEach(k=>{c[k]=c[k]||{};c=c[k];});c[ks.at(-1)]=v;}
function drawParties(){
  $('parties').innerHTML=Object.keys(FIELDS).map(coll=>`
   <div class="card" style="margin-bottom:14px"><h3>${coll}</h3>
    ${COLL_NOTE[coll]?`<p class="muted" style="margin:0 0 10px">${esc(COLL_NOTE[coll])}</p>`:''}
    <div class="grid" id="grid-${coll}"></div>
    <button class="g" style="margin-top:10px" onclick="addRow('${coll}')">Add</button></div>`).join('');
  Object.keys(FIELDS).forEach(renderColl);
  drawSignatures(); drawDefaults();
}
function drawSignatures(){
  const people=[...(S.inventors||[]),...(S.practitioners||[])];
  $('sigs').innerHTML=people.map(r=>{
    const nm=r.given?`${esc(r.given)} ${esc(r.family)}`:esc(r.name||r.id);
    const has=!!r.signature_file;
    return `<div class="card" style="background:var(--panel2)">
      <b>${nm}</b>
      <div style="margin:8px 0;min-height:74px;display:flex;align-items:center;justify-content:center;
        background:#fff;border-radius:8px;border:1px solid var(--line)">
        ${has?`<img src="${B}/patents/api/signature/${r.id}?t=${Date.now()}" style="max-width:100%;max-height:70px">`
             :`<span style="color:#888;font-size:12px">no signature on file</span>`}
      </div>
      <div class="row">
        <button class="g fix" onclick="document.getElementById('sigf-${r.id}').click()">${has?'Replace':'Upload'}</button>
        ${has?`<button class="d fix" onclick="delSig('${r.id}')">Remove</button>`:''}
      </div>
      <input type="file" id="sigf-${r.id}" accept="image/png,image/jpeg,image/webp" style="display:none"
        onchange="upSig('${r.id}',this)">
    </div>`;}).join('');
}
window.upSig=async(id,el)=>{
  if(!el.files||!el.files[0])return;
  const fd=new FormData(); fd.append('file',el.files[0]);
  const r=await fetch(B+'/patents/api/signature/'+id,{method:'POST',body:fd});
  const j=await r.json().catch(()=>({}));
  if(!r.ok)return toast(j.error||'upload failed');
  await refresh(); toast('Signature saved. It will be stamped on every form for this person.');
};
window.delSig=async id=>{ if(!confirm('Remove this signature?'))return;
  await api('/api/signature/'+id,{method:'DELETE'}); await refresh(); };
const DEFAULT_FIELDS=[['entity_status','Entity status'],['docket_prefix','Docket prefix'],
  ['publication','Publication'],['correspondence_id','Default correspondence id'],
  ['applicant_id','Default applicant id'],['payment_id','Default payment id'],
  ['spec_format','Specification format'],['authorize_pdx','Authorise foreign IP office access','bool']];
function drawDefaults(){
  const d=S.defaults||{};
  $('defaults').innerHTML=`<div class="card" style="background:var(--panel2)">`+
    DEFAULT_FIELDS.map(([k,lab,t])=>t==='bool'
      ? `<label class="chk"><input type="checkbox" data-def="${k}" ${d[k]?'checked':''}> ${esc(lab)}</label>`
      : `<label>${esc(lab)}</label><input data-def="${k}" value="${esc(d[k]||'')}">`).join('')+
    `<p class="hint">Leaving the PDX box ticked is the default and the right choice unless you will
     never file abroad: it lets the EPO, JPO and KIPO pull the priority document automatically.</p></div>`;
}
$('savedefaults').onclick=async()=>{
  const d={}; document.querySelectorAll('[data-def]').forEach(el=>
    d[el.dataset.def]=el.type==='checkbox'?el.checked:el.value);
  await api('/api/defaults',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(d)});
  S.defaults=d; toast('Defaults saved.');
};
async function refresh(){ const j=await api('/api/store'); S=j.store; drawInventors(); drawParties(); }
function renderColl(coll){
  const host=$('grid-'+coll); if(!host)return;
  host.innerHTML=(S[coll]||[]).map(r=>`<div class="card" style="background:var(--panel2)">
    ${FIELDS[coll].map(([k,lab,t])=>{
      const v=get(r,k);
      if(t==='bool')return `<label class="chk"><input type="checkbox" data-c="${coll}" data-i="${r.id}" data-k="${k}" ${v?'checked':''}> ${esc(lab)}</label>`;
      if(t==='text')return `<label>${esc(lab)}</label><textarea data-c="${coll}" data-i="${r.id}" data-k="${k}">${esc(v||'')}</textarea>`;
      return `<label>${esc(lab)}</label><input data-c="${coll}" data-i="${r.id}" data-k="${k}" value="${esc(v||'')}">`;}).join('')}
    <div class="row" style="margin-top:8px"><button class="g fix" onclick="saveRow('${coll}','${r.id}')">Save</button>
    <button class="d fix" onclick="delRow('${coll}','${r.id}')">Delete</button></div></div>`).join('');
}
window.addRow=async coll=>{const j=await api('/api/data/'+coll,{method:'POST',
  headers:{'Content-Type':'application/json'},body:JSON.stringify({})});
  S[coll]=S[coll]||[];S[coll].push(j.row);renderColl(coll);if(coll==='inventors')drawInventors();};
window.saveRow=async(coll,id)=>{
  const row={id};document.querySelectorAll(`[data-c="${coll}"][data-i="${id}"]`).forEach(el=>
    setp(row,el.dataset.k,el.type==='checkbox'?el.checked:el.value));
  const j=await api('/api/data/'+coll,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(row)});
  const i=S[coll].findIndex(r=>r.id===id); if(i>=0)S[coll][i]=j.row;
  if(coll==='inventors')drawInventors(); if(coll==='applicants')fill('applicant',S.applicants,'name');
  if(coll==='correspondence')fill('corr',S.correspondence,'label');
  recompute(); toast('Saved.');};
window.delRow=async(coll,id)=>{if(!confirm('Delete this row?'))return;
  await api('/api/data/'+coll+'/'+id,{method:'DELETE'});
  S[coll]=S[coll].filter(r=>r.id!==id);renderColl(coll);
  if(coll==='inventors')drawInventors();recompute();};

function drawHistory(){
  api('/api/store').then(j=>{S=j.store;
    $('hist').innerHTML=(S.filings||[]).length?`<table><tr><th>When</th><th>Title</th><th>Docket</th><th>Session</th><th>Status</th></tr>`+
     S.filings.map(f=>`<tr><td>${new Date(f.created*1000).toLocaleString()}</td><td>${esc(f.title)}</td>
      <td class="mono">${esc(f.docket||'')}</td><td><a style="color:var(--acc)" href="${B}/#${encodeURIComponent(f.session||'')}">${esc(f.session||'')}</a></td>
      <td>${esc(f.status||'')}${f.application_number?' - '+esc(f.application_number):''}</td></tr>`).join('')+`</table>`
     :'<span class="muted">Nothing filed from here yet.</span>';});
}

// ---- live browser ----
let live=null;
async function loadBrowsers(){
  const j=await api('/api/browsers');
  $('bsel').innerHTML=j.browsers.map(b=>`<option value="${b.port}">${b.port}${b.remote?' instance-3':b.shared?' (shared)':' local'}${b.headless===false?' headed':''} - ${b.targets.length} tab(s)</option>`).join('')
    ||'<option value="">none running</option>';
  window._browsers=j.browsers; drawTabs();
}
$('bsel').onchange=drawTabs;
function drawTabs(){
  const b=(window._browsers||[]).find(x=>String(x.port)===$('bsel').value);
  $('tsel').innerHTML=b?b.targets.map(t=>`<option value="${t.id}">${esc(t.title||t.url)}</option>`).join(''):'';
}
$('bnew').onclick=async()=>{$('bstat').textContent='starting...';
  try{const j=await api('/api/browsers',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({label:($('title').value||'filing').slice(0,30),headless:true,remote:$('bremote').checked})});
  $('bstat').textContent=(j.browser.remote?'instance-3 ':'local ')+'browser on port '+j.browser.port; await loadBrowsers();}
  catch(e){$('bstat').textContent='';toast(e.message);}};
$('bkill').onclick=async()=>{const p=$('bsel').value;if(!p)return;
  await api('/api/browsers/'+p,{method:'DELETE'});await loadBrowsers();};
$('battach').onclick=()=>{
  if(live){live.close();live=null;}
  const port=$('bsel').value,target=$('tsel').value; if(!port)return;
  const ro=$('binteract').checked?'0':'1';
  const url=B.replace(/^http/,'ws')+`/patents/ws/live?port=${port}&target=${encodeURIComponent(target)}&ro=${ro}`;
  live=new WebSocket(url); let n=0,t0=Date.now(),kb=0;
  live.onmessage=e=>{const m=JSON.parse(e.data);
    if(m.t==='frame'){n++;kb+=e.data.length*0.75/1024;$('screen').src='data:image/jpeg;base64,'+m.d;
      $('livestat').textContent=`${n} frames, ${kb.toFixed(0)} KB, ${(kb/Math.max(1,(Date.now()-t0)/1000)).toFixed(1)} KB/s`;}
    else if(m.t==='nav'){$('navurl').value=m.url;}
    else if(m.t==='error'){toast(m.m);}
    else if(m.t==='hello'){$('navurl').value=m.url;$('bstat').textContent='attached: '+(m.title||m.url);}};
  live.onclose=()=>{$('bstat').textContent='detached';};
};
$('screen').onclick=e=>{ if(!live||!$('binteract').checked)return;
  const r=e.target.getBoundingClientRect();
  live.send(JSON.stringify({t:'click',x:(e.clientX-r.left)/r.width,y:(e.clientY-r.top)/r.height}));};
$('screen').onwheel=e=>{ if(!live||!$('binteract').checked)return; e.preventDefault();
  live.send(JSON.stringify({t:'scroll',x:.5,y:.5,dy:e.deltaY}));};
document.addEventListener('keydown',e=>{
  if(!live||!$('binteract').checked)return;
  if(!document.getElementById('s-live').classList.contains('on'))return;
  if(document.activeElement&&['INPUT','TEXTAREA','SELECT'].includes(document.activeElement.tagName))return;
  if(e.key.length===1){live.send(JSON.stringify({t:'text',v:e.key}));e.preventDefault();}
  else if(['Enter','Backspace','Tab','Escape','ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(e.key)){
    live.send(JSON.stringify({t:'key',v:e.key}));e.preventDefault();}});
$('navgo').onclick=()=>live&&live.send(JSON.stringify({t:'nav',v:$('navurl').value}));

boot().catch(e=>toast('Could not load: '+e.message));
</script></body></html>
"""
