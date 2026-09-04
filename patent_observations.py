"""Third-party observations on someone else's pending US application.

The US name for this is a preissuance submission under 35 U.S.C. 122(e) and
37 CFR 1.290: anyone at all, including a competitor, may put prior art in front of
the examiner while the application is still pending. It is not an opposition, there
is no argument about patentability, and the third party gets no further part in the
prosecution. What the rule actually demands is narrow and unforgiving, and a
submission that misses any of it is thrown out as non-compliant rather than fixed:

  1.290(b)  timing: before the EARLIER of the notice of allowance, or the LATER of
            six months after first publication and the first rejection of any claim.
  1.290(d)  content: the document list, a CONCISE DESCRIPTION of the asserted
            relevance of each item, a legible copy of everything except US patents
            and US patent application publications, an English translation of
            anything not in English, the two statements, and the fee.
  1.290(f)  fee: 37 CFR 1.17(o), per every ten items listed or fraction thereof.
  1.290(g)  the fee is waived for three or fewer items on a party's first and only
            submission in that application.

The concise description is the part people get wrong. It is required per item, it
is separate from the list, and a submission that just names documents is refused.

One trap worth stating loudly, because it is printed on the form itself: PTO/SB/429
says "Do not submit this form electronically via USPTO patent electronic filing
system". Electronic filing goes through Patent Center's own third-party submission
screens, which build the equivalent list. So the PDF this module renders is a
worksheet to check and to transcribe from, and it must NOT be uploaded as a
document. The concise-description document, by contrast, IS uploaded.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from pathlib import Path

import patent_forms

# 37 CFR 1.17(o), fee codes 1818 (undiscounted) and 2818 (small entity). Verified
# against the uspto.gov fee schedule 2026-08-31. There is deliberately no micro
# rate: the schedule footnote reads "Third-party filers are not eligible for the
# micro entity fee", and the form says the same.
FEE_PER_TEN = {"undiscounted": 195, "small": 78}
FEE_CODES = {"undiscounted": "1818", "small": "2818"}
FEE_VERIFIED = "2026-08-31"
FREE_ITEM_LIMIT = 3           # 1.290(g)
ITEMS_PER_FEE_UNIT = 10       # 1.290(f)

DOC_KINDS = {
    "us_patent": "US patent",
    "us_pub": "US patent application publication",
    "foreign": "Foreign patent or published foreign application",
    "npl": "Non-patent publication",
}
# 1.290(d)(3): a copy of every item EXCEPT a US patent or a US patent application
# publication, which the Office already holds.
NEEDS_COPY = {"foreign", "npl"}

# A concise description that is shorter than this is not a description of relevance,
# it is a label. MPEP 1134.01 wants the relevance explained, not asserted.
MIN_DESCRIPTION_CHARS = 120


class ObservationError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# 1.290(b) timing
# --------------------------------------------------------------------------
def _parse_date(value):
    if not value:
        return None
    value = str(value).strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d/%m/%Y"):
        try:
            return date(*[int(x) for x in re.split(r"[-/]", value)][:3]) if fmt == "%Y-%m-%d" \
                else date.fromisoformat(_norm_us(value))
        except Exception:
            continue
    try:
        return date.fromisoformat(value[:10])
    except Exception:
        return None


def _norm_us(value: str) -> str:
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", value)
    return "%s-%02d-%02d" % (m.group(3), int(m.group(1)), int(m.group(2))) if m else value


def _plus_six_months(d: date) -> date:
    """Six calendar months, clamped to the end of a shorter month."""
    year, month = d.year + (d.month + 5) // 12, (d.month + 5) % 12 + 1
    day = d.day
    while day > 1:
        try:
            return date(year, month, day)
        except ValueError:
            day -= 1
    return date(year, month, 1)


def timing_gate(publication_date="", first_rejection_date="", notice_of_allowance_date="",
                today=None) -> dict:
    """1.290(b). Returns whether the window is open, and the exact reason.

    deadline = min(notice of allowance, max(publication + 6 months, first rejection))
    A notice of allowance closes the window outright, whatever the other dates say.
    """
    today = today or date.today()
    pub = _parse_date(publication_date)
    rej = _parse_date(first_rejection_date)
    noa = _parse_date(notice_of_allowance_date)

    if not pub and not rej:
        return {"open": None, "deadline": "", "reason":
                "Unknown. 1.290(b) is measured from the publication date and the first "
                "rejection; give at least one of them and the window can be computed.",
                "blocker": True}

    later = None
    if pub:
        later = _plus_six_months(pub)
    if rej and (later is None or rej > later):
        later = rej

    deadline = later
    closed_by_noa = False
    if noa:
        if later is None or noa <= later:
            deadline = noa
            closed_by_noa = True

    if deadline is None:
        return {"open": None, "deadline": "", "reason": "Not enough dates to compute 1.290(b).",
                "blocker": True}

    open_now = today < deadline
    if closed_by_noa and not open_now:
        reason = ("Closed. A notice of allowance was mailed %s, and 1.290(b)(1) ends the "
                  "window on that date whatever the six-month and first-rejection dates say."
                  % noa.isoformat())
    elif open_now:
        days = (deadline - today).days
        parts = []
        if pub:
            parts.append("six months after publication is %s" % _plus_six_months(pub).isoformat())
        if rej:
            parts.append("the first rejection was %s" % rej.isoformat())
        if noa:
            parts.append("a notice of allowance was mailed %s" % noa.isoformat())
        reason = ("Open until %s, %d day%s away. Under 1.290(b) the deadline is the earlier of "
                  "the notice of allowance and the later of the other two: %s."
                  % (deadline.isoformat(), days, "" if days == 1 else "s", "; ".join(parts)))
    else:
        reason = ("Closed. The 1.290(b) deadline was %s. A submission filed now is refused as "
                  "untimely and the fee is not refunded, so do not file."
                  % deadline.isoformat())
    return {"open": open_now, "deadline": deadline.isoformat(), "reason": reason,
            "blocker": not open_now}


# --------------------------------------------------------------------------
# 1.290(f) and (g) fees
# --------------------------------------------------------------------------
def fee_for(item_count: int, entity: str = "undiscounted", first_and_only: bool = False,
            resubmission: bool = False) -> dict:
    """1.290(f), with the 1.290(g) waiver and the resubmission case."""
    entity = entity if entity in FEE_PER_TEN else "undiscounted"
    units = math.ceil(item_count / ITEMS_PER_FEE_UNIT) if item_count else 0
    gross = units * FEE_PER_TEN[entity]

    if resubmission:
        return {"total": 0, "units": units, "entity": entity, "exempt": True,
                "code": FEE_CODES[entity], "per_unit": FEE_PER_TEN[entity],
                "basis": ("Resubmission responsive to a notice of non-compliance. The form asks "
                          "the Office to apply the fee already paid, or repeats the 1.290(g) "
                          "statement. Corrections must be limited to the non-compliance.")}
    if first_and_only and item_count <= FREE_ITEM_LIMIT:
        return {"total": 0, "units": 0, "entity": entity, "exempt": True,
                "code": "", "per_unit": FEE_PER_TEN[entity],
                "basis": ("No fee under 1.290(g): %d item%s, which is three or fewer, and this is "
                          "the first and only submission in this application by this party or "
                          "anyone in privity with it. That statement is made on penalty of the "
                          "submission being refused, so only claim it if it is true."
                          % (item_count, "" if item_count == 1 else "s"))}
    return {"total": gross, "units": units, "entity": entity, "exempt": False,
            "code": FEE_CODES[entity], "per_unit": FEE_PER_TEN[entity],
            "basis": ("37 CFR 1.17(o) at $%d per ten items or part thereof, %d item%s so %d unit%s. "
                      "Fee code %s. A third party cannot use the micro entity rate."
                      % (FEE_PER_TEN[entity], item_count, "" if item_count == 1 else "s",
                         units, "" if units == 1 else "s", FEE_CODES[entity])),
            "verified": FEE_VERIFIED}


# --------------------------------------------------------------------------
# the application number
# --------------------------------------------------------------------------
def normalise_application_number(value: str) -> str:
    digits = re.sub(r"[^0-9]", "", value or "")
    return digits


def application_number_ok(value: str) -> bool:
    return len(normalise_application_number(value)) == 8


def format_application_number(value: str) -> str:
    """19791470 -> 19/791,470, the way the Office writes it."""
    d = normalise_application_number(value)
    return "%s/%s,%s" % (d[:2], d[2:5], d[5:]) if len(d) == 8 else (value or "")


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def _item_label(item, n):
    ident = (item.get("identifier") or "").strip()
    return "item %d%s" % (n, (" (%s)" % ident[:48]) if ident else "")


def check(sub: dict, files=()) -> dict:
    """Everything 37 CFR 1.290 requires, as pass/fail with the rule cited.

    A `blocker` is a defect that gets the whole submission refused as non-compliant,
    which is worse than not filing: the window may close before you find out.
    """
    out = []
    items = sub.get("items") or []
    names = {Path(f.get("path", f.get("name", ""))).name for f in files}

    def add(ok, label, detail="", blocker=False, fix=""):
        out.append({"ok": bool(ok), "label": label, "detail": detail,
                    "blocker": bool(blocker and not ok), "fix": fix})

    # 1.290(e) identify the application
    appno = sub.get("application_number") or ""
    add(application_number_ok(appno),
        "The target application is identified by number (37 CFR 1.290(e))",
        "read as %s" % (format_application_number(appno) or "nothing"),
        blocker=True,
        fix="Enter the eight-digit application number, for example 19/791,470.")

    # timing
    t = timing_gate(sub.get("publication_date"), sub.get("first_rejection_date"),
                    sub.get("notice_of_allowance_date"))
    add(t["open"] is True, "The 1.290(b) window is open", t["reason"], blocker=True,
        fix="Check the dates in Patent Center's file wrapper for the target application.")

    # at least one item
    add(bool(items), "At least one document is listed (37 CFR 1.290(d)(1))",
        "%d listed" % len(items), blocker=True,
        fix="Add the prior art you want the examiner to see.")

    # per item
    missing_desc, thin_desc, missing_copy, missing_trans, missing_ident = [], [], [], [], []
    for n, it in enumerate(items, 1):
        kind = it.get("kind") or "npl"
        if not (it.get("identifier") or "").strip():
            missing_ident.append(_item_label(it, n))
        desc = (it.get("relevance") or "").strip()
        if not desc:
            missing_desc.append(_item_label(it, n))
        elif len(desc) < MIN_DESCRIPTION_CHARS:
            thin_desc.append("%s, %d characters" % (_item_label(it, n), len(desc)))
        if kind in NEEDS_COPY and not (it.get("copy_file") or "").strip():
            missing_copy.append(_item_label(it, n))
        if it.get("non_english") and not (it.get("translation_file") or "").strip():
            missing_trans.append(_item_label(it, n))

    add(not missing_ident, "Every item is identified",
        ", ".join(missing_ident), blocker=True,
        fix="Give the document number, or for a non-patent publication the full citation.")

    add(not missing_desc,
        "Every item has a concise description of its relevance (37 CFR 1.290(d)(2))",
        ", ".join(missing_desc), blocker=True,
        fix=("Write, per item, what it discloses and why that matters to this application. "
             "This is the requirement most submissions fail on, and the Office refuses the "
             "whole submission rather than asking for it."))

    add(not thin_desc, "Each concise description actually explains the relevance",
        ", ".join(thin_desc), blocker=False,
        fix=("Under %d characters reads as a label rather than a description. Say what the "
             "document teaches and against which claim or feature." % MIN_DESCRIPTION_CHARS))

    add(not missing_copy,
        "A legible copy is attached for everything except US patents and US publications "
        "(37 CFR 1.290(d)(3))",
        ", ".join(missing_copy), blocker=True,
        fix="Attach the copy and point the item at it. The Office already holds US patents "
            "and US patent application publications, so those need no copy.")

    add(not missing_trans, "An English translation is attached for every non-English item "
        "(37 CFR 1.290(d)(4))",
        ", ".join(missing_trans), blocker=True,
        fix="Attach the translation, or unmark the item as non-English.")

    # the two statements, 1.290(d)(5)
    add(bool(sub.get("stmt_not_1_56")),
        "Statement: the party is not someone with a 1.56 duty to disclose "
        "(37 CFR 1.290(d)(5)(i))", "", blocker=True,
        fix="Tick it on the Observations tab. An inventor, an assignee or their attorney "
            "cannot use 1.290 at all; they file an IDS instead.")
    add(bool(sub.get("stmt_complies")),
        "Statement: the submission complies with 122(e) and 1.290 (37 CFR 1.290(d)(5)(ii))",
        "", blocker=True, fix="Tick it on the Observations tab.")

    # fee or exemption
    fee = fee_for(len(items), sub.get("entity") or "undiscounted",
                  bool(sub.get("first_and_only")), bool(sub.get("resubmission")))
    # A waiver that cannot apply is ignored rather than honoured, so the fee is
    # charged and nothing is underpaid. Say so plainly, because the box stays
    # ticked and the total silently disagrees with it.
    if sub.get("first_and_only") and not sub.get("resubmission") and len(items) > FREE_ITEM_LIMIT:
        add(False, "The 1.290(g) fee exemption does not apply to this list",
            "%d items, and the waiver covers three or fewer" % len(items), blocker=False,
            fix=("The fee is charged in full anyway, so nothing is underpaid. Untick the "
                 "exemption to stop the form asserting it, or cut the list to three items."))
    add(True, "The fee position is settled (37 CFR 1.290(f), (g))", fee["basis"])

    # every attached file is a PDF the Office will accept
    unref = []
    referenced = {(it.get("copy_file") or "") for it in items} | \
                 {(it.get("translation_file") or "") for it in items} | \
                 {(it.get("evidence_file") or "") for it in items}
    for name in sorted(names):
        if name and name not in referenced:
            unref.append(name)
    add(not unref, "Every uploaded file is used by an item",
        ", ".join(unref[:8]), blocker=False,
        fix="Point an item at it, or drop it. Anything uploaded but unlisted is filed "
            "without a concise description, which is itself a non-compliance.")

    blockers = [c for c in out if c["blocker"]]
    return {"checks": out, "fee": fee, "timing": t,
            "blockers": len(blockers), "ready": not blockers,
            "item_count": len(items)}


# --------------------------------------------------------------------------
# the forms
# --------------------------------------------------------------------------
def _rows_by_kind(items):
    us, foreign, npl = [], [], []
    for n, it in enumerate(items, 1):
        it = dict(it, cite=n)
        kind = it.get("kind") or "npl"
        if kind in ("us_patent", "us_pub"):
            us.append(it)
        elif kind == "foreign":
            foreign.append(it)
        else:
            npl.append(it)
    return us, foreign, npl


def build_sb429(out_path, sub: dict, signature_image: str = "") -> dict:
    """PTO/SB/429, the document list plus the 1.290(d)(5) statements.

    A worksheet, not a filing: the form itself says not to submit it through the
    electronic filing system, because Patent Center builds the list from its own
    screens. Render it, check it against what you typed there, keep it with the file.
    """
    items = sub.get("items") or []
    us, foreign, npl = _rows_by_kind(items)
    appno = format_application_number(sub.get("application_number") or "")
    values = {"Applicaiton Number": appno, "Applicaiton Number_2": appno}

    # US patents and US publications, nine rows on page 1
    for i, it in enumerate(us[:9]):
        sfx = "" if i == 0 else "_%d" % (i + 1)
        values["Cite NoRow%d" % (i + 1)] = str(it["cite"])
        values["US Document Number%s" % sfx] = (it.get("identifier") or "").strip()
        values["MMDDYYYYUS%s" % ("" if i == 0 else "_%d" % (i + 1))] = it.get("date") or ""
        values["First Named InventorUS%s" % ("" if i == 0 else "_%d" % (i + 1))] = \
            it.get("party") or ""

    # Foreign documents, nine rows. The per-row "Translation Attached" boxes are
    # `undefined` .. `undefined_9`, and they are TEXT fields on this form, not
    # checkboxes, so they take an X rather than a tick state.
    for i, it in enumerate(foreign[:9]):
        values["Cite NoRow%d_2" % (i + 1)] = str(it["cite"])
        values["Country Code2Number3Kind Code4Row%d" % (i + 1)] = (it.get("identifier") or "")
        values["MMDDYYYYRow%d" % (i + 1)] = it.get("date") or ""
        values["Applicant Patentee or First Named InventorRow%d" % (i + 1)] = it.get("party") or ""
        if it.get("non_english") and (it.get("translation_file") or "").strip():
            values["undefined" if i == 0 else "undefined_%d" % (i + 1)] = "X"

    # Non-patent publications, eleven rows on page 2. Each row has a PAIR of text
    # fields: undefined_10 is Translation Attached, undefined_11 Evidence of
    # Publication, then +2 per row.
    for i, it in enumerate(npl[:11]):
        values["Cite NoRow%d_3" % (i + 1)] = str(it["cite"])
        key = ("Author if any title of the publication pages being submitted publication date "
               "publisher where available and place of publication where availableRow%d" % (i + 1))
        values[key] = (it.get("identifier") or "").strip()
        if it.get("non_english") and (it.get("translation_file") or "").strip():
            values["undefined_%d" % (10 + i * 2)] = "X"
        if (it.get("evidence_file") or "").strip():
            values["undefined_%d" % (11 + i * 2)] = "X"

    signer = sub.get("signer_name") or ""
    values["Signature"] = "" if signature_image else ("/%s/" % signer if signer else "")
    values["Name PrintedTyped"] = signer
    values["Date"] = sub.get("signed_date") or date.today().isoformat()
    if sub.get("registration_number"):
        values["Reg No if applicable"] = sub["registration_number"]

    checks = []
    fee = fee_for(len(items), sub.get("entity") or "undiscounted",
                  bool(sub.get("first_and_only")), bool(sub.get("resubmission")))
    if sub.get("resubmission"):
        checks.append("This resubmission is being made responsive to a notification of "
                      "noncompliance issued for an earlier filed thirdparty")
    if fee["exempt"] and not sub.get("resubmission"):
        checks.append("The fee set forth in 37 CFR 1290f is not required because this "
                      "submission lists three or fewer total items and to the")
    elif not fee["exempt"]:
        checks.append("The following fee set forth in 37 CFR 1290f is submitted herewith")
        checks.append("small entity" if fee["entity"] == "small" else "regular undiscounted")

    images = {"Signature": signature_image} if signature_image else None
    patent_forms.render("third_party_sub", out_path, values, checks=checks,
                        title="Third-party submission under 37 CFR 1.290, %s" % appno,
                        author=signer, images=images)
    over = {"US patents/publications": max(0, len(us) - 9),
            "foreign documents": max(0, len(foreign) - 9),
            "non-patent publications": max(0, len(npl) - 11)}
    return {"path": str(out_path), "verify": patent_forms.verify(out_path),
            "overflow": {k: v for k, v in over.items() if v}}


def build_concise_descriptions(out_path, sub: dict) -> dict:
    """The 1.290(d)(2) concise descriptions, as their own uploadable document.

    This one IS filed. It is separate from the list because Patent Center asks for
    it as its own attachment, and because the description is what the examiner
    actually reads.
    """
    import pymupdf

    items = sub.get("items") or []
    appno = format_application_number(sub.get("application_number") or "")
    doc = pymupdf.open()
    font_file = patent_forms.FONT_FILE
    if not Path(font_file).exists():
        raise ObservationError("font missing: %s" % font_file)
    font = pymupdf.Font(fontfile=font_file)

    margin, width, leading = 72, 612 - 144, 14.0
    page = doc.new_page(width=612, height=792)
    page.insert_font(fontname=patent_forms.FONT_ALIAS, fontfile=font_file)
    y = margin

    def line(text, size=10.5, gap=0.0, bold_gap=0.0):
        nonlocal page, y
        for chunk in (patent_forms._wrap(font, text, size, width) or [""]):
            if y > 792 - margin:
                page = doc.new_page(width=612, height=792)
                page.insert_font(fontname=patent_forms.FONT_ALIAS, fontfile=font_file)
                y = margin
            page.insert_text((margin, y), chunk, fontname=patent_forms.FONT_ALIAS, fontsize=size)
            y += size * 1.32
        y += gap

    line("CONCISE DESCRIPTION OF RELEVANCE", 13, gap=4)
    line("Third-party submission under 35 U.S.C. 122(e) and 37 CFR 1.290", 10.5, gap=2)
    line("Application No. %s" % (appno or "(not given)"), 10.5, gap=2)
    if sub.get("title"):
        line("Title: %s" % sub["title"], 10.5, gap=2)
    line("Concise description of the asserted relevance of each item listed, as required by "
         "37 CFR 1.290(d)(2).", 10.5, gap=10)

    for n, it in enumerate(items, 1):
        kind = DOC_KINDS.get(it.get("kind") or "npl", "Document")
        line("Item %d. %s" % (n, (it.get("identifier") or "").strip()), 11.5, gap=1)
        meta = [kind]
        if it.get("date"):
            meta.append("dated %s" % it["date"])
        if it.get("party"):
            meta.append(it["party"])
        if it.get("non_english"):
            meta.append("not in English, translation attached")
        line("   " + "; ".join(meta), 9.5, gap=3)
        for para in (it.get("relevance") or "").strip().split("\n"):
            if para.strip():
                line("   " + para.strip(), 10.5, gap=1)
        y += 8

    import tempfile
    with tempfile.TemporaryDirectory(prefix="obsdesc_") as tmp:
        stage = Path(tmp) / "stage.pdf"
        doc.save(str(stage), garbage=4, deflate=True, clean=True)
        doc.close()
        # The same finalise every other form goes through, so this document faces
        # exactly the checks Patent Center applies rather than a near-copy of them.
        patent_forms.finalise(stage, out_path,
                              title="Concise description of relevance, %s" % appno,
                              author=sub.get("signer_name") or "")
    return {"path": str(out_path), "verify": patent_forms.verify(out_path),
            "items": len(items)}
