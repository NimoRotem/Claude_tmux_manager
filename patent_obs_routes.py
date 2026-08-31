"""HTTP for third-party observations. The rules live in patent_observations.

Deliberately the same shape as the filing pipeline: a directory per submission with
`in/` (whatever was uploaded, zips included), `files/` (expanded), `forms/`
(generated) and meta.json, then a QA pass, then a handoff to a tmux session that
drives Patent Center while you watch it on the Live run tab.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

import patent_forms
import patent_observations as obs
import patent_packet as pkt
import patent_store as store

router = APIRouter()
MAX_UPLOAD = 60 * 1024 * 1024


def _err(message: str, code: int = 400):
    return JSONResponse({"error": message}, status_code=code)


def _dir(sub_id: str) -> Path:
    safe = os.path.basename(sub_id or "")
    if not safe or safe.startswith("."):
        raise ValueError("bad submission id")
    return store.OBSERVATIONS_DIR / safe


def _load(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf8"))
    except Exception:
        return {}


def _save(d: Path, meta: dict):
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf8")


def _payment_key() -> str:
    """`payment` in the store is a LIST of cards, not one card. Prefer the card
    earmarked for IP filing fees, then whatever is first, then the known default."""
    rows = store.load().get("payment") or []
    if isinstance(rows, dict):                       # tolerate an older shape
        rows = [rows]
    for row in rows:
        key = (row or {}).get("advisor_key") or ""
        if "uspto" in key or "ip" in key.split("-"):
            return key
    for row in rows:
        if (row or {}).get("advisor_key"):
            return row["advisor_key"]
    return "ramp-uspto-filing-fees"


def _blank() -> dict:
    return {"application_number": "", "title": "", "publication_date": "",
            "first_rejection_date": "", "notice_of_allowance_date": "",
            "entity": "undiscounted", "first_and_only": True, "resubmission": False,
            "stmt_not_1_56": True, "stmt_complies": True,
            "signer_name": "", "registration_number": "", "items": []}


# ==========================================================================
# the submission
# ==========================================================================
@router.get("/api/obs")
async def api_obs_list():
    rows = []
    for d in sorted(store.OBSERVATIONS_DIR.glob("*"), reverse=True):
        if not d.is_dir():
            continue
        m = _load(d)
        if m:
            rows.append({"id": m.get("id"), "title": m.get("title") or "",
                         "application_number": m.get("application_number") or "",
                         "created": m.get("created"), "session": m.get("session") or "",
                         "demo": bool(m.get("demo")), "items": len(m.get("items") or [])})
    return JSONResponse({"submissions": rows[:60]})


@router.post("/api/obs")
async def api_obs_new(request: Request):
    body = await request.json()
    sub_id = "%s-%s" % (int(time.time()),
                        store.slugify(body.get("application_number") or body.get("title"), "obs"))
    d = _dir(sub_id)
    for sub in ("in", "files", "forms"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    data = store.load()
    meta = dict(_blank(), **{k: v for k, v in body.items() if k in _blank()})
    meta.update(id=sub_id, created=time.time(), files=[], forms=[])
    if not meta["signer_name"]:
        corr = (data.get("correspondence") or [{}])[0]
        meta["signer_name"] = corr.get("name") or ""
    _save(d, meta)
    return JSONResponse({"ok": True, "submission": meta})


@router.get("/api/obs/{sub_id}")
async def api_obs_get(sub_id: str):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    return JSONResponse({"ok": True, "submission": _load(d)})


@router.post("/api/obs/{sub_id}/save")
async def api_obs_save(sub_id: str, request: Request):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    body = await request.json()
    meta = _load(d)
    for key in _blank():
        if key in body:
            meta[key] = body[key]
    _save(d, meta)
    return JSONResponse({"ok": True, "submission": meta, "review": obs.check(meta, meta.get("files") or [])})


@router.delete("/api/obs/{sub_id}")
async def api_obs_delete(sub_id: str):
    d = _dir(sub_id)
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)
    return JSONResponse({"ok": True})


@router.post("/api/obs/{sub_id}/upload")
async def api_obs_upload(sub_id: str, file: UploadFile = File(...)):
    try:
        d = _dir(sub_id)
    except ValueError as exc:
        return _err(str(exc))
    if not d.exists():
        return _err("unknown submission", 404)
    name = os.path.basename(file.filename or "upload")
    if not name or name.startswith("."):
        return _err("invalid filename")
    blob = await file.read()
    if len(blob) > MAX_UPLOAD:
        return _err("file too large (%.1f MB, max %d MB)"
                    % (len(blob) / 1e6, MAX_UPLOAD // 1024 // 1024), 413)
    (d / "in").mkdir(parents=True, exist_ok=True)
    (d / "in" / name).write_bytes(blob)
    return JSONResponse({"ok": True, "name": name, "bytes": len(blob)})


@router.post("/api/obs/{sub_id}/scan")
async def api_obs_scan(sub_id: str):
    """Expand whatever arrived and check each file the way Patent Center will.

    Anything in a zip is unpacked. Every file is then a candidate to be an item's
    copy, translation or evidence of publication; the list itself stays the
    person's to write, because only they know why a document is relevant.
    """
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    meta = _load(d)
    files_dir = d / "files"
    shutil.rmtree(files_dir, ignore_errors=True)
    files_dir.mkdir(parents=True, exist_ok=True)
    expanded = await asyncio.to_thread(pkt.expand, d / "in", files_dir)

    entries = []
    for p in expanded:
        info = await asyncio.to_thread(pkt.check_file, p, "other")
        entries.append({"name": p.name, "path": str(p),
                        "bytes": p.stat().st_size,
                        "checks": info.get("checks", []),
                        "ok": all(c.get("ok") for c in info.get("checks", []) if c.get("blocking", True))})
    meta["files"] = entries
    meta["scanned"] = time.time()
    _save(d, meta)
    return JSONResponse({"ok": True, "files": entries,
                         "review": obs.check(meta, entries)})


@router.post("/api/obs/{sub_id}/check")
async def api_obs_check(sub_id: str):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    meta = _load(d)
    return JSONResponse({"ok": True, "review": obs.check(meta, meta.get("files") or [])})


# ==========================================================================
# forms
# ==========================================================================
@router.post("/api/obs/{sub_id}/forms")
async def api_obs_forms(sub_id: str):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    meta = _load(d)
    forms_dir = d / "forms"
    forms_dir.mkdir(parents=True, exist_ok=True)
    data = store.load()

    sig = ""
    signer = (meta.get("signer_name") or "").strip().lower()
    for person in (data.get("inventors") or []) + (data.get("practitioners") or []):
        full = ("%s %s" % (person.get("given", ""), person.get("family", ""))).strip().lower()
        if signer and signer in (full, (person.get("name") or "").strip().lower()):
            f = person.get("signature_file") or ""
            if f and (store.SIGNATURES_DIR / f).exists():
                sig = str(store.SIGNATURES_DIR / f)
    out = []
    try:
        a = await asyncio.to_thread(obs.build_sb429, forms_dir / "SB429_WORKSHEET.pdf", meta, sig)
        out.append({"name": "SB429_WORKSHEET.pdf", "path": a["path"], "verify": a["verify"],
                    "upload": False, "overflow": a.get("overflow") or {},
                    "label": "PTO/SB/429 worksheet, 37 CFR 1.290",
                    "note": ("Do NOT upload this. The form itself says not to file it through the "
                             "electronic system; Patent Center builds the list from its own "
                             "screens. Use this to check what you typed there.")})
        b = await asyncio.to_thread(obs.build_concise_descriptions,
                                    forms_dir / "CONCISE_DESCRIPTIONS.pdf", meta)
        out.append({"name": "CONCISE_DESCRIPTIONS.pdf", "path": b["path"], "verify": b["verify"],
                    "upload": True, "overflow": {},
                    "label": "Concise description of relevance, 37 CFR 1.290(d)(2)",
                    "note": "This one IS filed. It is the part the examiner reads."})
    except Exception as exc:                                      # noqa: BLE001
        return _err("could not build the forms: %s" % exc, 500)
    meta["forms"] = out
    _save(d, meta)
    return JSONResponse({"ok": True, "forms": out,
                         "review": obs.check(meta, meta.get("files") or [])})


@router.get("/api/obs/{sub_id}/form/{name}")
async def api_obs_form(sub_id: str, name: str):
    d = _dir(sub_id)
    p = d / "forms" / os.path.basename(name)
    if not p.exists():
        return _err("no such form", 404)
    return FileResponse(str(p), media_type="application/pdf", filename=p.name)


@router.get("/api/obs/{sub_id}/form/{name}/thumb")
async def api_obs_form_thumb(sub_id: str, name: str, page: int = 0):
    import pymupdf
    d = _dir(sub_id)
    p = d / "forms" / os.path.basename(name)
    if not p.exists():
        return _err("no such form", 404)

    def render():
        doc = pymupdf.open(str(p))
        try:
            pg = doc[max(0, min(page, doc.page_count - 1))]
            return pg.get_pixmap(dpi=110).tobytes("png")
        finally:
            doc.close()
    return Response(await asyncio.to_thread(render), media_type="image/png")


@router.get("/api/obs/{sub_id}/file/{name}")
async def api_obs_file(sub_id: str, name: str):
    d = _dir(sub_id)
    p = d / "files" / os.path.basename(name)
    if not p.exists():
        return _err("no such file", 404)
    return FileResponse(str(p), filename=p.name)


# ==========================================================================
# the handoff
# ==========================================================================
BRIEF = """# Third-party observation, 37 CFR 1.290

{stop_rule}You are filing a third-party preissuance submission under 35 U.S.C. 122(e)
and 37 CFR 1.290 against SOMEONE ELSE'S pending application. This is not our
application and we are not a party to it. The panel has already checked the
package; verify it yourself, then {mission}

## Target
- Application: **{appno}**{title_line}
- 1.290(b) deadline: **{deadline}** ({timing})
- Items listed: **{item_count}**
- Fee: **{fee_text}**

## The documents
{items_block}

## Files uploaded with this brief
{files_block}

## What Patent Center needs
1. Log in at https://patentcenter.uspto.gov/ as the registered account
   (`get_secret uspto-account`). If the device-trust cookie is needed, the browser
   for this runs on instance-3; see `browser_live.launch_remote`.
2. **Confirm the target is the application this brief describes, before you type
   anything into Patent Center.** Pull its file wrapper from the USPTO ODP API
   (`GET https://api.uspto.gov/api/v1/patent/applications/<8 digits>` with
   `X-API-KEY`, key in `~/.patent-api/CREDS.md`) and check three things: it is
   still PENDING, its title and applicant are the ones named above, and its
   publication and rejection dates are the ones the deadline was computed from.
   The dates in this brief came from whoever typed them; the file wrapper is the
   record. If any of the three disagrees, STOP and report rather than filing.
3. Choose the **third-party preissuance submission** workflow, not a normal
   document upload, and enter the target application number above.
4. Enter the document list on Patent Center's own screens. **Do not upload
   SB429_WORKSHEET.pdf.** That form carries the instruction "Do not submit this
   form electronically via USPTO patent electronic filing system"; it exists so you
   can check the list you typed. Uploading it is a defect, not a belt and braces.
5. Upload CONCISE_DESCRIPTIONS.pdf, plus every copy, translation and evidence of
   publication listed above. A US patent or US publication needs no copy.
6. Tick the two statements under 1.290(d)(5): the party has no 1.56 duty to
   disclose, and the submission complies with 122(e) and 1.290. Both are already
   true of us, which is why this route is open at all: we are a third party here.
7. {fee_step}
8. {finish_step}

## Non-negotiable
{stop_bullet}- If the 1.290(b) window has closed, STOP and report. A late submission is
  refused as non-compliant and the fee is not returned.
- A DEMO run is still a real login to a real Patent Center against a real
  application. If the target is somebody else's live file, do not put sample or
  placeholder documents into it, even in a workflow you intend to abandon before
  submitting. Stop and report instead.
- Do not add argument, opinion or claim charts beyond the concise descriptions
  already written. 1.290 allows a description of relevance and nothing more.
- If anything in the package is wrong, fix it and say what you changed. If it
  cannot be fixed, stop and report the exact blocker rather than filing something
  that will be thrown out.
"""

DEMO_STOP = """## THIS IS A DEMO RUN. DO NOT FILE AND DO NOT PAY.
Go through the whole workflow so it can be watched, and stop at the payment screen.
Do not press the final submit and do not enter a card.

"""


def _items_block(meta):
    lines = []
    for n, it in enumerate(meta.get("items") or [], 1):
        kind = obs.DOC_KINDS.get(it.get("kind") or "npl", "Document")
        bits = ["%d. **%s** (%s)" % (n, (it.get("identifier") or "?").strip(), kind)]
        if it.get("date"):
            bits.append("dated %s" % it["date"])
        if it.get("copy_file"):
            bits.append("copy: `%s`" % it["copy_file"])
        if it.get("non_english"):
            bits.append("not in English, translation `%s`" % (it.get("translation_file") or "MISSING"))
        if it.get("evidence_file"):
            bits.append("evidence of publication: `%s`" % it["evidence_file"])
        lines.append("   ".join(bits))
        rel = (it.get("relevance") or "").strip()
        if rel:
            lines.append("    > %s" % rel.replace("\n", " ")[:400])
    return "\n".join(lines) or "(none listed)"


@router.post("/api/obs/{sub_id}/submit")
async def api_obs_submit(sub_id: str, request: Request):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    body = await request.json()
    meta = _load(d)
    demo = bool(meta.get("demo") or body.get("demo"))
    review = obs.check(meta, meta.get("files") or [])

    # A demo is allowed to be incomplete; a real filing is not. Handing a session a
    # package that 1.290 will refuse wastes the fee and can burn the deadline.
    if not demo and not review["ready"]:
        bad = [c["label"] for c in review["checks"] if c["blocker"]]
        return _err("not ready to file: %s" % "; ".join(bad), 409)

    fee = review["fee"]
    fee_text = ("no fee, 37 CFR 1.290(g)" if fee["exempt"]
                else "$%d (%s, fee code %s)" % (fee["total"], fee["entity"], fee["code"]))
    if demo:
        mission = ("drive Patent Center as far as the payment screen and stop there. "
                   "You are NOT filing and you are NOT paying.")
        fee_step = "DEMO: do NOT pay. Stop when the fee or payment screen appears."
        finish_step = ("Screenshot the payment screen, read back the fee Patent Center "
                       "calculated, close the browser you opened "
                       "(`browser_live.shutdown(port, profile)`) and report here. Leave it "
                       "unsubmitted.")
        stop_rule = DEMO_STOP
        stop_bullet = ("- THIS IS A DEMO. Do not submit and do not pay, whatever else this "
                       "brief says.\n")
    else:
        mission = "complete the submission, pay if a fee is due, and report back."
        fee_step = (("No fee is due: the 1.290(g) statement applies, three or fewer items on "
                     "our first and only submission in this application. Tick the exemption, "
                     "do not pay.") if fee["exempt"] else
                    ("Pay %s with the Ramp card: `get_payment_method` for `%s`. Never write the "
                     "number to disk or into a screenshot you keep."
                     % (fee_text, _payment_key())))
        finish_step = ("Capture the acknowledgement receipt, the submission date and any "
                       "transaction id, close the browser you opened "
                       "(`browser_live.shutdown(port, profile)`) and report back here.")
        stop_rule = ""
        stop_bullet = ""

    files = [f for f in (meta.get("files") or [])]
    forms = [f for f in (meta.get("forms") or [])]
    brief = BRIEF.format(
        stop_rule=stop_rule, mission=mission, fee_step=fee_step, finish_step=finish_step,
        stop_bullet=stop_bullet,
        appno=obs.format_application_number(meta.get("application_number") or ""),
        title_line=("\n- Title: %s" % meta["title"]) if meta.get("title") else "",
        deadline=review["timing"].get("deadline") or "unknown",
        timing=review["timing"].get("reason", "").replace("\n", " "),
        item_count=review["item_count"], fee_text=fee_text,
        items_block=_items_block(meta),
        files_block="\n".join("- `%s`" % f["name"] for f in files + forms) or "(none)")

    brief_path = d / "BRIEF.md"
    brief_path.write_text(brief, encoding="utf8")
    (d / "submission.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf8")

    if body.get("dry_run"):
        return JSONResponse({"ok": True, "dry_run": True, "brief": brief,
                             "review": review, "session": ""})

    base = body.get("base") or os.environ.get("TMUX_DASH_LOCAL_URL", "http://127.0.0.1:8501")
    slug = store.slugify(meta.get("application_number") or "obs", "obs")
    session_name = (("obs demo %s" if demo else "obs %s") % slug)[:60]
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

            uploads = [brief_path, d / "submission.json"]
            uploads += [Path(f["path"]) for f in files]
            uploads += [Path(f["path"]) for f in forms]
            for path in uploads:
                if not path.exists():
                    continue
                await client.post("/api/sessions/%s/upload" % created,
                                  files={"file": (path.name, path.read_bytes(),
                                                  "application/octet-stream")})
            msg = (("DEMO RUN, do not file and do not pay. " if demo else "")
                   + "Please file this third-party observation under 37 CFR 1.290. The brief "
                   "is in BRIEF.md, uploaded with the documents, the concise descriptions and "
                   "submission.json. Read BRIEF.md first, verify the package yourself, then "
                   "complete the Patent Center third-party preissuance submission."
                   + (" For this demo, stop at the payment screen: do not submit and do not "
                      "pay." if demo else ""))
            await client.post("/api/sessions/%s/send" % created, json={"command": msg})
    except Exception as exc:                                      # noqa: BLE001
        return _err("handoff failed: %s" % exc, 500)

    meta["session"] = created
    meta["status"] = "demo, stops before payment" if demo else "handed off"
    _save(d, meta)
    return JSONResponse({"ok": True, "session": created, "review": review,
                         "brief": str(brief_path)})


# ==========================================================================
# the demo
# ==========================================================================
DEMO_ITEMS = [
    {"kind": "us_patent", "identifier": "9,872,565", "date": "01/23/2018",
     "party": "Chen", "relevance":
     "Discloses an eccentric-mass vibration motor carried on a rigid perimeter frame that is "
     "held against a compliant surface by a sustained pressure differential. Figures 3 and 4 "
     "show the sliding perimeter seal and column 6 lines 12 to 40 describe maintaining the "
     "differential during motion, which is the arrangement recited in claim 1 of the "
     "application."},
    {"kind": "us_pub", "identifier": "2015/0272346 A1", "date": "10/01/2015",
     "party": "Iwasaki", "relevance":
     "Paragraphs 0031 to 0038 teach driving such a device in the 40 to 90 Hz band and "
     "adjusting amplitude against measured surface compliance, which is the limitation added "
     "to claim 4 to distinguish the art of record."},
    {"kind": "foreign", "identifier": "EP-2345678-A1", "date": "07/14/2011", "party": "Bosch",
     "non_english": True, "copy_file": "EP2345678.pdf",
     "translation_file": "EP2345678_translation.pdf", "relevance":
     "Teaches the sustained pressure differential across a sliding perimeter as the retention "
     "mechanism, the feature the applicant argues is absent from the prior art. The relevant "
     "passage is page 4 lines 8 to 27 of the attached English translation."},
    {"kind": "npl", "identifier":
     "Okada, T., 'Vibration coupling in compliant bedding', Journal of Sound and Vibration "
     "331(4), pp. 812-825, 2012, Elsevier, Amsterdam",
     "date": "02/2012", "party": "Okada", "copy_file": "okada2012.pdf",
     "evidence_file": "okada2012_masthead.pdf", "relevance":
     "Measures vibration transfer through compliant bedding at the amplitudes recited in "
     "claims 7 to 9 and reports the same coupling coefficient range, which goes to the "
     "obviousness of the claimed operating band over the combination of Chen and Iwasaki."},
]


@router.post("/api/obs/demo")
async def api_obs_demo():
    """A complete, deliberately fee-bearing sample so the payment stop is real.

    Four items, so 1.290(g) does not apply and Patent Center reaches a payment
    screen the demo can stop in front of. The target is a sample application number
    that is not ours: a third-party submission against your own application is a
    contradiction, because the 1.290(d)(5)(i) statement would be false.

    The target must also not be ANYONE's. This demo shipped with 18/402,517, picked
    because it looked invented; it is Halliburton's real application "Downhole
    drilling sub with controllable stiffness", published US 2025/0215755 A1 and
    abandoned 2025-08-23. A demo run against it would have logged into the real
    Patent Center and started a real third-party submission on a stranger's file,
    carrying four sample PDFs that say on their face that they are not real
    documents. Every 8-digit number in an assigned series is somebody's application,
    so the placeholder uses series 88, which the Office does not assign. The
    consequence is deliberate: the demo cannot reach a payment screen, because a
    real fee screen needs a real pending application, and that is a file we have no
    business putting sample documents into.
    """
    from datetime import date, timedelta
    sub_id = "%s-demo" % int(time.time())
    d = _dir(sub_id)
    for part in ("in", "files", "forms"):
        (d / part).mkdir(parents=True, exist_ok=True)

    samples = store.SAMPLES_DIR / "observations"
    copied = []
    if samples.exists():
        for p in sorted(samples.glob("*")):
            if p.is_file():
                shutil.copy2(p, d / "in" / p.name)
                copied.append(p.name)

    meta = dict(_blank())
    meta.update({
        "id": sub_id, "created": time.time(), "demo": True,
        "application_number": "88/888,888",
        "title": "DEMO - vibration device for bedding covering elements (not a real application)",
        "publication_date": (date.today() - timedelta(days=40)).isoformat(),
        "entity": "undiscounted", "first_and_only": True,
        "stmt_not_1_56": True, "stmt_complies": True,
        "signer_name": "Nimrod Rotem",
        "items": [dict(it) for it in DEMO_ITEMS],
        "files": [], "forms": [],
    })
    _save(d, meta)
    return JSONResponse({"ok": True, "submission": meta, "staged": copied})
