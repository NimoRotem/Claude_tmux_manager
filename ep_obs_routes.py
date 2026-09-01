"""HTTP for Art. 115 EPC third-party observations, and the agent that files them.

Same shape as patent_obs_routes so the panel behaves identically on both sides:
a directory per submission with `in/`, `files/`, `forms/` and meta.json, a review
pass, then a handoff to a tmux session that drives the EPO's own web form while
the Live run tab streams it.

Why this route is worth having even though Online Filing 2.0 is currently shut to
us: the EPO's Third Party Observations app is a SEPARATE surface. Probed
2026-09-01, it needs no sign-in, no organisation link and no professional
representative, and an Art. 115 observation carries no fee. So this is the one
EPO filing route that is fully open today.

The wizard, as it actually renders (probed, not guessed):
  1 Personal Details   radio: provide details | anonymous
  2 Subject            radio: application number | publication number, then a
                       search field `subject.applicationNumber` and a Search button
  3 Observations       radio: upload document | complete observations template
  4 Facts and evidence "Add patent literature" / "Add non-patent literature"
  5 Summary            "Preview and send"
plus a FriendlyCaptcha that must be satisfied before sending.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, File, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse

import ep_observations as epobs
import patent_forms
import patent_packet as pkt
import patent_store as store

try:
    # Present only in the standalone patent-filing deployment, where the panel
    # owns its own tmux socket. Absent when mounted inside the dashboard.
    import sessions
except Exception:                                                 # noqa: BLE001
    sessions = None

router = APIRouter()
MAX_UPLOAD = 60 * 1024 * 1024
TPO_URL = "https://tpo.apps.epo.org/tpo/ui/prod/"

EP_OBS_DIR = store.DATA_DIR / "ep_observations"


def _err(message: str, code: int = 400):
    return JSONResponse({"error": message}, status_code=code)


def _dir(sub_id: str) -> Path:
    safe = os.path.basename(sub_id or "")
    if not safe or safe.startswith("."):
        raise ValueError("bad submission id")
    return EP_OBS_DIR / safe


def _load(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text(encoding="utf8"))
    except Exception:
        return {}


def _save(d: Path, meta: dict):
    d.mkdir(parents=True, exist_ok=True)
    (d / "meta.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf8")


def _blank() -> dict:
    return {"id": "", "created": time.time(), "number": "", "number_kind": "application",
            "title": "", "proprietor": "", "language": "en", "stage": "examination",
            "published": True, "r71_3_sent": False, "anonymous": False,
            "filer_name": "", "filer_email": "", "filer_address": "", "grounds": [], "reasoning": "",
            "items": [], "files": [], "forms": [], "status": "draft"}


# --------------------------------------------------------------------------
@router.get("/api/epobs")
async def api_epobs_list():
    EP_OBS_DIR.mkdir(parents=True, exist_ok=True)
    rows = []
    for d in sorted(EP_OBS_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        m = _load(d)
        if m:
            rows.append({"id": m.get("id") or d.name, "number": m.get("number"),
                         "title": m.get("title"), "status": m.get("status"),
                         "created": m.get("created"), "grounds": m.get("grounds") or []})
    return JSONResponse({"rows": rows, "fee": epobs.fee(),
                         "grounds": {k: {"label": v[0], "basis": v[1],
                                         "opposition_ground": k in epobs.ALSO_AN_OPPOSITION_GROUND}
                                     for k, v in epobs.GROUNDS.items()}})


@router.post("/api/epobs")
async def api_epobs_new(request: Request):
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    sub_id = "ep-%d" % int(time.time() * 1000)
    meta = _blank()
    meta.update({k: v for k, v in (body or {}).items() if k in meta})
    meta["id"] = sub_id
    d = _dir(sub_id)
    (d / "in").mkdir(parents=True, exist_ok=True)
    (d / "files").mkdir(parents=True, exist_ok=True)
    (d / "forms").mkdir(parents=True, exist_ok=True)
    _save(d, meta)
    return JSONResponse({"ok": True, "id": sub_id, "meta": meta})


@router.get("/api/epobs/{sub_id}")
async def api_epobs_get(sub_id: str):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    meta = _load(d)
    return JSONResponse({"meta": meta, "review": epobs.check(meta, meta.get("files") or [])})


@router.post("/api/epobs/{sub_id}/save")
async def api_epobs_save(sub_id: str, request: Request):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    body = await request.json()
    meta = _load(d)
    for k, v in (body or {}).items():
        if k in ("id", "created", "files", "forms"):
            continue
        meta[k] = v
    _save(d, meta)
    return JSONResponse({"ok": True, "review": epobs.check(meta, meta.get("files") or [])})


@router.post("/api/epobs/{sub_id}/upload")
async def api_epobs_upload(sub_id: str, file: UploadFile = File(...)):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    raw = await file.read()
    if len(raw) > MAX_UPLOAD:
        return _err("file too large", 413)
    name = os.path.basename(file.filename or "upload.bin")
    dest = d / "files" / name
    dest.write_bytes(raw)
    meta = _load(d)
    files = [f for f in (meta.get("files") or []) if f.get("name") != name]
    rec = {"name": name, "path": str(dest), "size": len(raw)}
    # Same document QA the US side runs. An unembedded font or a space in the
    # BaseFont name is just as wrong on a European filing; only the office that
    # complains differs.
    if name.lower().endswith(".pdf"):
        try:
            rec["qa"] = pkt.check_file(str(dest), role="other")
        except Exception as exc:                                  # noqa: BLE001
            rec["qa_error"] = str(exc)[:200]
    files.append(rec)
    meta["files"] = files
    _save(d, meta)
    return JSONResponse({"ok": True, "file": rec})


@router.post("/api/epobs/{sub_id}/check")
async def api_epobs_check(sub_id: str):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    meta = _load(d)
    return JSONResponse(epobs.check(meta, meta.get("files") or []))


async def _build(d: Path, meta: dict) -> list:
    out = d / "forms" / "observations_art115.pdf"
    info = epobs.build_observations(out, meta)
    rec = {"name": out.name, "path": str(out), "verify": info.get("verify")}
    meta["forms"] = [rec]
    _save(d, meta)
    return [rec]


@router.post("/api/epobs/{sub_id}/forms")
async def api_epobs_forms(sub_id: str):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    meta = _load(d)
    try:
        forms = await _build(d, meta)
    except Exception as exc:                                      # noqa: BLE001
        return _err("could not build the observations document: %s" % exc, 500)
    return JSONResponse({"ok": True, "forms": forms})


@router.get("/api/epobs/{sub_id}/form/{name}")
async def api_epobs_form(sub_id: str, name: str):
    d = _dir(sub_id) / "forms" / os.path.basename(name)
    if not d.exists():
        return _err("not found", 404)
    return FileResponse(str(d), media_type="application/pdf")


# --------------------------------------------------------------------------
BRIEF = """# Third-party observations, Art. 115 EPC

{stop_rule}
You are filing observations by a third party on a European patent application,
and your job is to {mission}

## The instrument, so you recognise a wrong turn
- Legal basis: **Art. 115 EPC and Rule 114 EPC**. Guidelines E-VI, 3.
- **There is NO FEE.** None. If any screen asks for money or a card, you are on
  the wrong form: stop and say so. Do not pay for anything, ever, on this run.
- We do **not** become a party to the proceedings. There is no reply, no hearing
  and no appeal for us. This is a one-shot document.
- Observations go into the **public part of the file**. Write nothing you would
  not want the proprietor and the world to read.

## Where to do it
**{tpo_url}**
Probed 2026-09-01: this is a SEPARATE surface from Online Filing 2.0. It needs no
sign-in, no organisation link and no professional representative, which is why it
works while OLF 2.0 does not. Do not go to filing.epo.org for this.

{browser_line}

## The wizard, as it actually renders
1. **Personal Details** - radio, "Provide personal details" or "Make an anonymous
   submission". {anon_step}
2. **Subject** - radio for "Application number" or "Publication number", then the
   search box and a **Search** button. We are filing against **{number}** as a
   **{number_kind} number**. Press Search and CONFIRM THE PATENT THAT COMES BACK
   IS THE RIGHT ONE before going on: check the title reads "{title}".
3. **Observations** - radio, "Upload document" or "Complete observations
   template". Choose **Upload document** and upload
   `observations_art115.pdf`, which is already written and is in the files with
   this brief.
4. **Facts and evidence** - "Add patent literature" and "Add non-patent
   literature". Add each document listed below under its right heading. Patent
   literature the EPO already holds does not need a copy; **non-patent literature
   does**, and the copies are in the uploaded files.
5. **Summary** - review, then **Preview and send**.

**E-mail is a REQUIRED field** on the Personal Details step. Ours is {filer_email}.
Without it the send button stays dead, which reads like a bug and is not one.

**Uploading a file when the browser is on another host.** If the CDP port is an
SSH tunnel to a different machine, `DOM.setFileInputFiles` with a local path
attaches a ZERO-BYTE file and the confirm fails silently, so the observation goes
up empty. Copy the PDF to the machine the browser is on FIRST, then point the
input at the path THERE. Check the size the form reports back before moving on.

There is a **FriendlyCaptcha** on the form. It ignores a synthetic
`element.click()`: drive it with a trusted `Input.dispatchMouseEvent` over CDP,
scoped to the tab. Do not use xdotool, the browser is shared and another session
may have a tab in front.

## This filing
- Application / patent: **{number}**{title_line}
- Language: {language}. Rule 114(1) requires English, French or German.
- Grounds relied on:
{grounds_block}
- Documents:
{items_block}

## Files uploaded with this brief
{files_block}

## Before you send
- The number on screen matches **{number}** and the title matches.
- The uploaded PDF is the Art. 115 observations document, not a draft.
- Every non-patent document listed has its copy attached.
- No fee screen has appeared.
{finish_step}
"""

DEMO_STOP = """## THIS IS A DEMO RUN.
Fill the wizard as far as the **Summary** step and STOP. Do not press
"Preview and send". Nothing is to reach the EPO.
There is no training mode on this form and no fee to stop at, so the Summary
screen is the only safe boundary: past it the observations are filed and they are
public and irrevocable.

"""


def _grounds_block(meta):
    out = []
    for g in (meta.get("grounds") or []):
        if g in epobs.GROUNDS:
            label, basis = epobs.GROUNDS[g]
            extra = ("" if g in epobs.ALSO_AN_OPPOSITION_GROUND
                     else "   (available ONLY here, not an Art. 100 opposition ground)")
            out.append("   - %s (%s)%s" % (label, basis, extra))
    return "\n".join(out) or "   - (none stated)"


def _items_block(meta):
    lines = []
    for n, it in enumerate(meta.get("items") or [], 1):
        kind = epobs.DOC_KINDS.get(it.get("kind") or "npl", "Document")
        bits = ["   D%d **%s** (%s)" % (n, (it.get("identifier") or "?").strip(), kind)]
        if it.get("date"):
            bits.append("dated %s" % it["date"])
        if it.get("copy_file"):
            bits.append("copy: `%s`" % it["copy_file"])
        if it.get("translation_file"):
            bits.append("translation: `%s`" % it["translation_file"])
        lines.append(", ".join(bits))
        rel = (it.get("relevance") or "").strip()
        if rel:
            lines.append("       > %s" % rel.replace("\n", " ")[:400])
    return "\n".join(lines) or "   (none)"


@router.post("/api/epobs/{sub_id}/submit")
async def api_epobs_submit(sub_id: str, request: Request):
    d = _dir(sub_id)
    if not d.exists():
        return _err("unknown submission", 404)
    body = await request.json()
    meta = _load(d)
    demo = bool(meta.get("demo") or body.get("demo"))
    review = epobs.check(meta, meta.get("files") or [])

    if not demo and not review["ready"]:
        bad = [c["label"] for c in review["checks"] if c["blocker"]]
        return _err("not ready to file: %s" % "; ".join(bad), 409)

    try:
        forms = await _build(d, meta)
    except Exception as exc:                                      # noqa: BLE001
        return _err("could not build the observations document: %s" % exc, 500)

    if demo:
        mission = ("fill the EPO's form as far as the Summary step and stop there. "
                   "You are NOT sending anything.")
        finish_step = ("- Screenshot the Summary screen, close the browser you opened "
                       "(`browser_live.shutdown(port, profile)`) and report here. Leave it "
                       "unsent.")
        stop_rule = DEMO_STOP
    else:
        mission = ("complete the observations and send them, then report back with the "
                   "receipt.")
        finish_step = ("- Capture the confirmation and any reference number the EPO returns, "
                       "close the browser you opened (`browser_live.shutdown(port, profile)`) "
                       "and report back here.")
        stop_rule = ""

    if meta.get("anonymous"):
        anon_step = ("Choose **Make an anonymous submission**. This was set deliberately; "
                     "note it costs us the EPO's three-month undertaking and has been held "
                     "inadmissible in inter partes proceedings (T 146/07, T 1336/09).")
    else:
        anon_step = ("Choose **Provide personal details** and enter: %s%s"
                     % (meta.get("filer_name") or "(name missing)",
                        (", " + meta["filer_address"]) if meta.get("filer_address") else ""))

    browser_line = ("A browser is started for the run and the panel streams it; the port is "
                    "filled in when you actually submit.")
    dry = bool(body.get("dry_run"))
    if not dry:
        try:
            import patent_panel
            binfo = await patent_panel.launch_for_run("epobs-%s" % sub_id)
            meta["browser_port"] = binfo["port"]
            meta["browser_profile"] = binfo.get("profile", "")
            browser_line = (
                "**A browser is already running for this run and the panel is streaming it: "
                "CDP on 127.0.0.1:%d. Drive THAT browser and do not start another**, or "
                "nobody can watch you. `browser_live.targets(%d)` lists its tabs."
                % (binfo["port"], binfo["port"]))
        except Exception as exc:                                  # noqa: BLE001
            meta["browser_error"] = str(exc)[:200]

    files = list(meta.get("files") or [])
    brief = BRIEF.format(
        stop_rule=stop_rule, mission=mission, finish_step=finish_step,
        browser_line=browser_line, tpo_url=TPO_URL, anon_step=anon_step,
        number=epobs.format_number(meta.get("number") or ""),
        number_kind=meta.get("number_kind") or "application",
        filer_email=meta.get("filer_email") or "(NOT SET, the form will not send)",
        title=meta.get("title") or "(title not recorded)",
        title_line=("\n- Title: %s" % meta["title"]) if meta.get("title") else "",
        language=epobs.LANGUAGES.get((meta.get("language") or "en").lower(), "English"),
        grounds_block=_grounds_block(meta), items_block=_items_block(meta),
        files_block="\n".join("- `%s`" % f["name"] for f in files + forms) or "- (none)")

    brief_path = d / "BRIEF.md"
    brief_path.write_text(brief, encoding="utf8")
    (d / "submission.json").write_text(json.dumps(meta, indent=1, default=str), encoding="utf8")

    if dry:
        return JSONResponse({"ok": True, "dry_run": True, "brief": brief, "review": review,
                             "session": ""})

    session_name = (("ep obs demo %s" if demo else "ep obs %s")
                    % store.slugify(meta.get("number") or "epobs", "epobs"))[:60]
    msg = (("DEMO RUN, fill the form but do NOT press Preview and send. " if demo else "")
           + "Please file these third-party observations under Art. 115 EPC on the "
             "EPO's own web form at " + TPO_URL + ". BRIEF.md is in this directory with the "
             "observations PDF and the cited documents. Read BRIEF.md first, verify the "
             "package yourself, then drive the five-step wizard. There is no fee: if "
             "anything asks for payment you are on the wrong form."
           + (" For this demo, stop at the Summary step." if demo else ""))
    uploads = [p for p in ([brief_path, d / "submission.json"]
                           + [Path(f["path"]) for f in files + forms]) if Path(p).exists()]

    # Two deployments, two handoffs. Where this panel is a standalone app it owns
    # its own tmux socket (PF_TMUX_SOCKET) and drives it directly, which is what
    # the live US route does. Where it is mounted inside the dashboard, the
    # dashboard's own session API is the way. Detect rather than assume: calling
    # the HTTP API from the standalone app posts the panel's cookie to the
    # dashboard, gets the login PAGE back, and dies on `Expecting value: line 1
    # column 1` with no clue why.
    created = ""
    if sessions is not None:
        workdir = d / "work"
        workdir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(sessions.upload_into, "", workdir, uploads)
        try:
            created = await asyncio.to_thread(sessions.create, session_name, str(workdir))
        except Exception as exc:                                  # noqa: BLE001
            return _err("could not open a session: %s" % exc, 502)
        await asyncio.to_thread(sessions.upload_into, created, workdir, uploads)
        await asyncio.to_thread(sessions.wait_ready, created)
        await asyncio.to_thread(sessions.send_text, created, msg)
    else:
        base = body.get("base") or os.environ.get("TMUX_DASH_LOCAL_URL",
                                                  "http://127.0.0.1:8501")
        cookies = request.headers.get("cookie", "")
        headers = {"cookie": cookies} if cookies else {}
        try:
            async with httpx.AsyncClient(base_url=base, timeout=120, headers=headers) as client:
                created, why = await store.create_session(
                    client, session_name, str(Path(__file__).parent))
                if not created:
                    return _err("could not open a session: %s" % why, 502)
                for path in uploads:
                    await client.post("/api/sessions/%s/upload" % created,
                                      files={"file": (Path(path).name, Path(path).read_bytes(),
                                                      "application/octet-stream")})
                await client.post("/api/sessions/%s/send" % created, json={"command": msg})
        except Exception as exc:                                  # noqa: BLE001
            return _err("handoff failed: %s" % exc, 500)

    meta["session"] = created
    meta["status"] = "demo, stops at Summary" if demo else "handed off"
    _save(d, meta)
    return JSONResponse({"ok": True, "session": created, "review": review,
                         "brief": str(brief_path)})
