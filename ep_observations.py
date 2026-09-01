"""Third-party observations on a European application or patent, Art. 115 EPC.

The European twin of patent_observations.py, and it is a different instrument in
three ways that change how the panel should behave:

  NO FEE AT ALL. Art. 115 and Rule 114 provide for none. Where the US module
  computes 37 CFR 1.17(o) and hands a card to the agent, this one must actively
  stop an agent from paying anything, because a payment screen appearing at all
  would mean it is on the wrong form.

  BROADER GROUNDS THAN OPPOSITION. Observations may attack novelty, inventive
  step, sufficiency (Art. 83), added matter (Art. 76(1), 123(2)) AND CLARITY
  (Art. 84). Clarity is NOT an opposition ground under Art. 100, so this is the
  only route by which we can put a clarity objection in front of the division.
  Encoded in GROUNDS so the UI cannot silently narrow it to the Art. 100 list.

  ANONYMITY IS ALLOWED AND IS USUALLY THE WRONG CHOICE. It is permitted, and the
  EPO's own form offers it, but anonymous observations have been held
  inadmissible in inter partes proceedings (T 146/07, T 1336/09), and the EPO
  only undertakes to act within three months where the observations are
  substantiated AND not anonymous. So the default here is to name the filer, and
  choosing anonymity raises a warning rather than passing quietly.

Timing is the other trap. Observations are only considered while proceedings are
pending: filed after grant with no opposition on foot, they go to the non-public
part of the file and are read only if proceedings revive. And they are only
possible at all once the application has been PUBLISHED.

Legal basis: Art. 115 EPC, Rule 114 EPC, Guidelines E-VI, 3.
"""
from __future__ import annotations

import os
import re
from datetime import date
from pathlib import Path

import patent_forms

# Rule 114(1): in writing, in an official language of the EPO, stating the grounds.
LANGUAGES = {"en": "English", "fr": "French", "de": "German"}

# There is no fee. Stated as a constant so nothing downstream has to infer it.
FEE_EUR = 0
FEE_BASIS = "Art. 115 EPC and Rule 114 EPC provide for no fee."

# Guidelines E-VI, 3. Note Art. 84: available here, NOT an opposition ground.
GROUNDS = {
    "novelty":      ("Lack of novelty", "Art. 54 EPC"),
    "inventive":    ("Lack of inventive step", "Art. 56 EPC"),
    "sufficiency":  ("Insufficient disclosure", "Art. 83 EPC"),
    "added_matter": ("Subject-matter extends beyond the application as filed",
                     "Art. 123(2) EPC, and Art. 76(1) for a divisional"),
    "clarity":      ("Lack of clarity of the claims", "Art. 84 EPC"),
    "patentability": ("Excluded subject-matter or not susceptible of industrial application",
                      "Art. 52 to 57 EPC"),
    "double_patenting": ("Double patenting", "G 4/19"),
}
# The subset that is ALSO an opposition ground, so the UI can tell the user which
# points survive into an opposition and which are only available now.
ALSO_AN_OPPOSITION_GROUND = {"novelty", "inventive", "sufficiency", "added_matter",
                             "patentability"}

DOC_KINDS = {
    "patent": "Patent literature",
    "npl": "Non-patent literature",
}

# Rule 114(1) applies Rule 3(3) to evidence: supporting documents may be in any
# language, but the EPO may invite a translation and DISREGARDS the evidence if
# none is filed. So a non-official-language exhibit is a warning, not a failure.
OFFICIAL_LANGUAGE_CODES = set(LANGUAGES)

# A relevance statement shorter than this is a label, not a reasoned ground.
# Rule 114(1) requires the grounds to be stated; the EPO's three-month
# undertaking applies only to SUBSTANTIATED observations.
MIN_RELEVANCE_CHARS = 150

# Rule 49 EPC formal requirements for documents we generate, in PDF points.
A4 = (595.28, 841.89)
MARGIN_TOP = 56.7      # 2.0 cm
MARGIN_LEFT = 70.9     # 2.5 cm
MARGIN_RIGHT = 56.7    # 2.0 cm
MARGIN_BOTTOM = 56.7   # 2.0 cm
MIN_BODY_PT = 10.0     # Rule 49(8): capitals at least 0.21 cm high


class ObservationError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# numbers
# --------------------------------------------------------------------------
def normalise_number(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", (value or "")).upper()


def looks_like_application_number(value: str) -> bool:
    """EP application numbers are eight digits plus a check digit, e.g. 21202485.6."""
    v = normalise_number(value)
    if v.startswith("EP"):
        v = v[2:]
    return bool(re.fullmatch(r"\d{8,9}", v))


def looks_like_publication_number(value: str) -> bool:
    """EP publication numbers are seven digits with a kind code, e.g. EP4446072A1."""
    v = normalise_number(value)
    if v.startswith("EP"):
        v = v[2:]
    return bool(re.fullmatch(r"\d{6,7}(A[1239]|B[123])?", v))


def format_number(value: str) -> str:
    v = normalise_number(value)
    if not v:
        return ""
    if not v.startswith("EP"):
        v = "EP" + v
    return v


# --------------------------------------------------------------------------
# Art. 115 timing
# --------------------------------------------------------------------------
def timing_gate(published: bool = True, stage: str = "examination",
                r71_3_sent: bool = False, today=None) -> dict:
    """Can observations be filed and will they actually be read.

    stage: pre_publication | examination | opposition | granted_no_opposition |
           withdrawn_or_refused
    """
    today = today or date.today()
    if not published or stage == "pre_publication":
        return {"open": False, "considered": False,
                "reason": ("Art. 115 EPC only applies once the European patent application "
                           "has been published. Nothing can be filed yet.")}
    if stage in ("granted_no_opposition", "withdrawn_or_refused"):
        return {"open": True, "considered": False,
                "reason": ("No proceedings are pending. Observations can still be sent, but "
                           "they go to the non-public part of the file and are only read if "
                           "proceedings revive, for example when an opposition or a "
                           "limitation is started. Guidelines E-VI, 3.")}
    if stage == "opposition":
        return {"open": True, "considered": True,
                "reason": ("Opposition proceedings are pending, so observations are "
                           "considered. Note that anonymous observations have been held "
                           "inadmissible in inter partes proceedings (T 146/07, T 1336/09), "
                           "so identify the filer.")}
    # examination
    if r71_3_sent:
        return {"open": True, "considered": True, "late": True,
                "reason": ("A communication under Rule 71(3) has already issued, so the "
                           "division has finished its substantive work. Observations filed "
                           "now may not be taken into account before grant. File anyway, but "
                           "plan for an opposition rather than relying on this.")}
    return {"open": True, "considered": True,
            "reason": ("Examination is pending. Substantiated, non-anonymous observations "
                       "trigger the EPO's undertaking to issue the next action within three "
                       "months.")}


def fee() -> dict:
    """There is none. Returned as a structure so callers do not special-case it."""
    return {"amount_eur": FEE_EUR, "payable": False, "basis": FEE_BASIS,
            "warning": ("If a payment screen appears, you are on the wrong form. "
                        "An Art. 115 observation is free.")}


# --------------------------------------------------------------------------
# the review
# --------------------------------------------------------------------------
def _c(key, label, ok, detail="", blocker=False, basis=""):
    return {"key": key, "label": label, "state": "pass" if ok else ("fail" if blocker else "warn"),
            "detail": detail, "blocker": bool(blocker and not ok), "basis": basis}


def check(sub: dict, files=()) -> dict:
    """Everything Rule 114 and the EPO's form will hold us to, before we file."""
    checks = []
    items = sub.get("items") or []
    names = {os.path.basename(f.get("name") or f.get("path") or "") for f in (files or [])}

    # --- subject -----------------------------------------------------------
    num = (sub.get("number") or "").strip()
    kind = sub.get("number_kind") or "application"
    if not num:
        checks.append(_c("subject", "Application or publication number", False,
                         "The EPO form will not proceed without it.", True, "Art. 115 EPC"))
    else:
        ok = (looks_like_application_number(num) if kind == "application"
              else looks_like_publication_number(num))
        checks.append(_c("subject", "Application or publication number", ok,
                         "%s given as %s number." % (format_number(num), kind),
                         True, "Art. 115 EPC"))

    # --- timing ------------------------------------------------------------
    t = timing_gate(published=bool(sub.get("published", True)),
                    stage=sub.get("stage") or "examination",
                    r71_3_sent=bool(sub.get("r71_3_sent")))
    checks.append(_c("timing", "Proceedings are pending", t["open"] and t["considered"],
                     t["reason"], not t["open"], "Art. 115 EPC; Guidelines E-VI, 3"))

    # --- language ----------------------------------------------------------
    lang = (sub.get("language") or "en").lower()
    checks.append(_c("language", "Filed in an official language", lang in LANGUAGES,
                     "%s. Rule 114(1) requires English, French or German."
                     % LANGUAGES.get(lang, lang), True, "Rule 114(1) EPC"))

    # --- grounds -----------------------------------------------------------
    chosen = [g for g in (sub.get("grounds") or []) if g in GROUNDS]
    checks.append(_c("grounds", "Grounds stated", bool(chosen),
                     ", ".join("%s (%s)" % (GROUNDS[g][0], GROUNDS[g][1]) for g in chosen)
                     or "Rule 114(1) requires the grounds to be stated.",
                     True, "Rule 114(1) EPC"))
    only_here = [g for g in chosen if g not in ALSO_AN_OPPOSITION_GROUND]
    if only_here:
        checks.append(_c("grounds_scope", "Some grounds are available only here", True,
                         "%s cannot be raised in an Art. 100 opposition, so this is the only "
                         "route for them." % ", ".join(GROUNDS[g][0] for g in only_here),
                         False, "Art. 100 EPC"))

    # --- reasoning ---------------------------------------------------------
    body = (sub.get("reasoning") or "").strip()
    checks.append(_c("reasoning", "Reasoned statement", len(body) >= MIN_RELEVANCE_CHARS,
                     "%d characters. Observations that merely name documents are not "
                     "substantiated, and only substantiated observations get the EPO's "
                     "three-month undertaking." % len(body),
                     True, "Rule 114(1) EPC; Guidelines E-VI, 3"))

    # --- anonymity ---------------------------------------------------------
    anon = bool(sub.get("anonymous"))
    if anon:
        checks.append(_c("anonymity", "Anonymous submission chosen", False,
                         "Permitted, but anonymous observations have been held inadmissible "
                         "in inter partes proceedings (T 146/07, T 1336/09), the EPO cannot "
                         "invite us to correct a formal deficiency, and the three-month "
                         "undertaking does not apply. Name the filer unless there is a "
                         "deliberate reason not to.", False, "Guidelines E-VI, 3"))
    else:
        who = (sub.get("filer_name") or "").strip()
        checks.append(_c("anonymity", "Filer identified", bool(who),
                         who or "Give a name, or tick anonymous deliberately.",
                         True, "Guidelines E-VI, 3"))

    # --- cited documents ---------------------------------------------------
    for n, it in enumerate(items, 1):
        ident = (it.get("identifier") or "").strip()
        rel = (it.get("relevance") or "").strip()
        checks.append(_c("item_%d" % n, "D%d %s" % (n, ident or "(no identifier)"),
                         bool(ident) and len(rel) >= MIN_RELEVANCE_CHARS,
                         ("relevance is %d characters" % len(rel)) if ident
                         else "no identifier", False, "Rule 114(1) EPC"))
        if it.get("kind") == "npl" and not it.get("copy_file"):
            checks.append(_c("item_%d_copy" % n, "D%d copy attached" % n, False,
                             "Non-patent literature has to be supplied; the EPO does not "
                             "hold it.", False, "Guidelines E-VI, 3"))
        elif it.get("copy_file") and it["copy_file"] not in names:
            checks.append(_c("item_%d_copy" % n, "D%d copy uploaded" % n, False,
                             "%s is referenced but was not uploaded." % it["copy_file"],
                             True, "Guidelines E-VI, 3"))
        if it.get("language") and it["language"].lower() not in OFFICIAL_LANGUAGE_CODES:
            checks.append(_c("item_%d_lang" % n, "D%d translation" % n,
                             bool(it.get("translation_file")),
                             "Evidence may be in any language, but the EPO may invite a "
                             "translation and DISREGARDS the document if none is filed.",
                             False, "Rule 114(1) with Rule 3(3) EPC"))

    blockers = [c for c in checks if c["blocker"]]
    return {"ready": not blockers, "checks": checks, "timing": t, "fee": fee(),
            "item_count": len(items), "grounds": chosen,
            "opposition_only": sorted(set(chosen) - ALSO_AN_OPPOSITION_GROUND)}


# --------------------------------------------------------------------------
# the document we upload
# --------------------------------------------------------------------------
def build_observations(out_path, sub: dict) -> dict:
    """The reasoned statement, as an A4 PDF that meets Rule 49 EPC.

    Goes through patent_forms.finalise like everything else the panel produces,
    so the fonts are genuinely embedded with clean subset names. That check was
    written for Patent Center, but an unembedded font is just as bad here and it
    costs nothing to hold one standard.
    """
    import pymupdf

    font_file = patent_forms.FONT_FILE
    if not Path(font_file).exists():
        raise ObservationError("font missing: %s" % font_file)
    font = pymupdf.Font(fontfile=font_file)

    width, height = A4
    text_width = width - MARGIN_LEFT - MARGIN_RIGHT
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_font(fontname=patent_forms.FONT_ALIAS, fontfile=font_file)
    y = MARGIN_TOP

    def line(text, size=MIN_BODY_PT + 0.5, gap=0.0, indent=0.0):
        nonlocal page, y
        for chunk in (patent_forms._wrap(font, text, size, text_width - indent) or [""]):
            if y > height - MARGIN_BOTTOM:
                page = doc.new_page(width=width, height=height)
                page.insert_font(fontname=patent_forms.FONT_ALIAS, fontfile=font_file)
                y = MARGIN_TOP
            page.insert_text((MARGIN_LEFT + indent, y), chunk,
                             fontname=patent_forms.FONT_ALIAS, fontsize=size)
            y += size * 1.34
        y += gap

    num = format_number(sub.get("number") or "")
    line("OBSERVATIONS BY A THIRD PARTY", 13.5, gap=3)
    line("Article 115 EPC and Rule 114 EPC", 11, gap=6)
    line("Application / patent concerned: %s" % (num or "(not given)"), 11, gap=1)
    if sub.get("title"):
        line("Title: %s" % sub["title"], 11, gap=1)
    if sub.get("proprietor"):
        line("Applicant / proprietor: %s" % sub["proprietor"], 11, gap=1)
    if sub.get("anonymous"):
        line("Filed anonymously.", 11, gap=6)
    else:
        line("Filed by: %s" % (sub.get("filer_name") or ""), 11, gap=1)
        if sub.get("filer_address"):
            line(sub["filer_address"], 11, gap=6)
        else:
            y += 6

    line("The third party is not a party to the proceedings (Art. 115 EPC, second "
         "sentence). No fee is payable.", 10.5, gap=8)

    chosen = [g for g in (sub.get("grounds") or []) if g in GROUNDS]
    line("1. GROUNDS", 12, gap=3)
    for g in chosen:
        label, basis = GROUNDS[g]
        line("- %s (%s)" % (label, basis), 11, gap=1, indent=10)
    if not chosen:
        line("- (none stated)", 11, gap=1, indent=10)
    y += 6

    line("2. DOCUMENTS RELIED ON", 12, gap=3)
    items = sub.get("items") or []
    if not items:
        line("None.", 11, gap=1, indent=10)
    for n, it in enumerate(items, 1):
        meta = [DOC_KINDS.get(it.get("kind") or "npl", "Document")]
        if it.get("date"):
            meta.append("dated %s" % it["date"])
        if it.get("language") and it["language"].lower() not in OFFICIAL_LANGUAGE_CODES:
            meta.append("in %s, translation %s"
                        % (it["language"],
                           "attached" if it.get("translation_file") else "NOT ATTACHED"))
        line("D%d  %s" % (n, (it.get("identifier") or "").strip()), 11.5, gap=1, indent=10)
        line("; ".join(meta), 10, gap=2, indent=22)
    y += 6

    line("3. OBSERVATIONS", 12, gap=3)
    for para in (sub.get("reasoning") or "").split("\n"):
        if para.strip():
            line(para.strip(), 11, gap=2, indent=10)
        else:
            y += 4

    if items:
        y += 6
        line("4. RELEVANCE OF EACH DOCUMENT", 12, gap=3)
        for n, it in enumerate(items, 1):
            line("D%d  %s" % (n, (it.get("identifier") or "").strip()), 11.5, gap=1, indent=10)
            for para in (it.get("relevance") or "").split("\n"):
                if para.strip():
                    line(para.strip(), 11, gap=1, indent=22)
            y += 4

    import tempfile
    with tempfile.TemporaryDirectory(prefix="epobs_") as tmp:
        stage = Path(tmp) / "stage.pdf"
        doc.save(str(stage), garbage=4, deflate=True, clean=True)
        doc.close()
        patent_forms.finalise(stage, out_path,
                              title="Third-party observations, Art. 115 EPC, %s" % num,
                              author="" if sub.get("anonymous") else (sub.get("filer_name") or ""))
    return {"path": str(out_path), "verify": patent_forms.verify(out_path),
            "items": len(items), "grounds": chosen}
