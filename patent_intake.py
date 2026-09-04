"""Read a dropped package and decide, in one pass, whether it can be filed.

The panel used to ask for a title, an entity status, which inventors, and which of
four forms to generate, before it had looked at anything. Almost all of that is in
the files already, and the parts that are not are the panel's job to work out, not
the filer's. So: expand whatever arrived, read what it says, fill in the rest from
the store, decide which forms the filing actually needs, build them, and come back
with one list of what is still wrong and what would fix each thing.

An issue carries its own remedy, because "not ready" with no next step is what made
the old panel feel like an exam:
    fix.kind == "sign"    a form needs a signature; the on-screen signer opens on it
    fix.kind == "edit"    a field is missing or wrong; that row opens for editing
    fix.kind == "upload"  something is absent from the package
    fix.kind == "agent"   mechanical, and the filing agent can do it unattended
"""
from __future__ import annotations

import re
from pathlib import Path

import patent_packet as pkt
import patent_store as store

# 37 CFR 1.72(a): the title should be short and specific. The Office objects past
# 500 characters and a two-word title is almost always too vague to survive.
TITLE_MAX = 500


def _text_of(path: Path) -> str:
    try:
        if pkt.sniff(path) == "docx":
            return pkt.docx_text(path)
        import subprocess
        return subprocess.run(["pdftotext", "-q", "-layout", str(path), "-"],
                              capture_output=True, text=True, timeout=90).stdout
    except Exception:
        return ""


_TITLE_HEAD = re.compile(
    r"^\s*(?:TITLE(?:\s+OF\s+THE\s+INVENTION)?)\s*[:\-–]?\s*$", re.I)


def extract_title(text: str) -> str:
    """The title as the drafter wrote it, not as somebody would like it.

    Two shapes cover nearly everything: a TITLE heading with the title on the
    following non-blank line, or a document that simply opens with the title in
    capitals before any numbered section.
    """
    lines = [l.rstrip() for l in (text or "").splitlines()]
    for i, line in enumerate(lines):
        if _TITLE_HEAD.match(line):
            for nxt in lines[i + 1:i + 6]:
                if nxt.strip():
                    return " ".join(nxt.split())[:TITLE_MAX]
    # inline "TITLE: something"
    m = re.search(r"^\s*TITLE(?:\s+OF\s+THE\s+INVENTION)?\s*[:\-–]\s*(\S.+)$",
                  text or "", re.I | re.M)
    if m:
        return " ".join(m.group(1).split())[:TITLE_MAX]
    # a leading all-caps block before the first recognised section heading
    for line in lines[:40]:
        s = line.strip()
        if not s:
            continue
        if re.match(r"^(CROSS|BACKGROUND|FIELD|SUMMARY|BRIEF|DETAILED|WHAT IS CLAIMED|ABSTRACT)",
                    s, re.I):
            break
        letters = [c for c in s if c.isalpha()]
        if len(s) > 12 and letters and sum(c.isupper() for c in letters) / len(letters) > 0.85:
            return " ".join(s.split())[:TITLE_MAX]
    return ""


def extract_inventor_names(text: str) -> list:
    """Names the package itself asserts, from an ADS or a declaration if present."""
    names = []
    for pat in (r"(?:Inventor|Applicant)\s*(?:Name)?\s*[:\-]\s*([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})",
                r"Legal\s+Name\s*[:\-]\s*([A-Z][A-Za-z.'\-]+(?:\s+[A-Z][A-Za-z.'\-]+){1,3})"):
        for m in re.finditer(pat, text or ""):
            n = " ".join(m.group(1).split())
            if n not in names:
                names.append(n)
    return names[:12]


def read_package(entries: list) -> dict:
    """Everything the files themselves tell us."""
    facts = {"title": "", "claims": 0, "independent": 0, "sheets": 0,
             "inventor_names": [], "has_spec": False, "has_drawings": False,
             "has_declaration": False, "has_ads": False, "spec_is_docx": False,
             "spec_name": "", "drawings_name": ""}
    for f in entries:
        role, path = f.get("role") or "", Path(f["path"])
        if role == "exclude" or not path.exists():
            continue
        if role in ("spec", "specification"):
            facts["has_spec"] = True
            facts["spec_name"] = path.name
            facts["spec_is_docx"] = pkt.sniff(path) == "docx"
            text = _text_of(path)
            facts["title"] = facts["title"] or extract_title(text)
            for n in extract_inventor_names(text):
                if n not in facts["inventor_names"]:
                    facts["inventor_names"].append(n)
            try:
                rev = pkt.review_specification(path)
                counts = rev.get("counts") or {}
                facts["claims"] = counts.get("total", 0)
                facts["independent"] = counts.get("independent", 0)
            except Exception:
                pass
        elif role in ("drawings", "drawing"):
            facts["has_drawings"] = True
            facts["drawings_name"] = path.name
            try:
                import pymupdf
                doc = pymupdf.open(str(path))
                facts["sheets"] = doc.page_count
                doc.close()
            except Exception:
                pass
        elif role in ("oath", "declaration"):
            facts["has_declaration"] = True
            for n in extract_inventor_names(_text_of(path)):
                if n not in facts["inventor_names"]:
                    facts["inventor_names"].append(n)
        elif role == "ads":
            facts["has_ads"] = True
            for n in extract_inventor_names(_text_of(path)):
                if n not in facts["inventor_names"]:
                    facts["inventor_names"].append(n)
    return facts


def match_inventors(names: list, data: dict) -> list:
    """Map names the package asserts onto people the store knows."""
    out = []
    for person in data.get("inventors") or []:
        full = ("%s %s" % (person.get("given", ""), person.get("family", ""))).strip().lower()
        for n in names:
            if n.strip().lower() == full or (
                    person.get("family", "").lower() in n.lower()
                    and person.get("given", "").lower() in n.lower()):
                if person["id"] not in out:
                    out.append(person["id"])
    return out


def forms_needed(inv_ids: list, applicant_id: str, data: dict) -> list:
    """Which of the official forms this filing actually needs.

    Nobody filing a patent wants to be asked which of four forms to tick. It falls
    out of the parties: a declaration for each inventor always; the age petition
    only where an inventor is marked 65 or over; a power of attorney and the
    3.73(c) statement only where somebody other than the inventors is the applicant.
    """
    by_id = {p["id"]: p for p in (data.get("inventors") or [])}
    needed = []
    if inv_ids:
        needed.append("declaration")
    if any((by_id.get(i) or {}).get("age_65_plus") for i in inv_ids):
        needed.append("age_petition")
    applicant = next((a for a in (data.get("applicants") or [])
                      if a.get("id") == applicant_id), None)
    juristic = bool(applicant and (applicant.get("kind") or "") != "inventors")
    if juristic:
        needed += ["power_of_attorney", "statement_373"]
    return needed


def _issue(iid, level, label, detail="", fix=None, rule=""):
    return {"id": iid, "level": level, "label": label, "detail": detail,
            "rule": rule, "fix": fix or {}}


def audit(meta: dict, data: dict, facts: dict, report: dict, gate: dict) -> dict:
    """One list of what stands between this package and the Submit button."""
    issues = []
    inv_ids = meta.get("inventor_ids") or []

    if not facts.get("has_spec"):
        issues.append(_issue("no_spec", "block", "No specification in the package",
                             "Nothing in the upload reads as the specification.",
                             {"kind": "upload", "what": "the specification, as DOCX"},
                             "37 CFR 1.51(b)(1)"))
    if not facts.get("title"):
        issues.append(_issue("no_title", "block", "The title could not be read from the files",
                             "Give it here and it goes on the ADS.",
                             {"kind": "edit", "field": "title"}, "37 CFR 1.72(a)"))
    if not facts.get("claims"):
        issues.append(_issue("no_claims", "block", "No claims found in the specification",
                             "A nonprovisional needs at least one.",
                             {"kind": "upload", "what": "a specification containing claims"},
                             "35 USC 112(b)"))
    if not inv_ids:
        issues.append(_issue("no_inventors", "block", "No inventor chosen",
                             ("The files name %s, which did not match anyone on file."
                              % ", ".join(facts.get("inventor_names") or ["nobody"])
                              if facts.get("inventor_names") else
                              "The files do not name an inventor."),
                             {"kind": "edit", "field": "inventors"}, "37 CFR 1.41"))
    if not facts.get("has_drawings"):
        issues.append(_issue("no_drawings", "warn", "No drawings in the package",
                             "Fine if the invention cannot be illustrated; unusual otherwise.",
                             {"kind": "upload", "what": "drawings as PDF"}, "37 CFR 1.81"))
    if facts.get("has_spec") and not facts.get("spec_is_docx"):
        issues.append(_issue("spec_not_docx", "warn",
                             "The specification is not DOCX, which costs a surcharge",
                             "37 CFR 1.16(u) applies to a non-DOCX specification.",
                             {"kind": "upload", "what": "the specification as DOCX"},
                             "37 CFR 1.16(u)"))

    # anything the file checker itself flagged
    for f in (report or {}).get("findings", []):
        if f.get("level") == "fail":
            issues.append(_issue("file_%s" % f.get("code", "x"), "block",
                                 f.get("title", "A file failed its checks"),
                                 f.get("detail", ""),
                                 {"kind": "agent", "what": "fix the file"}, f.get("rule", "")))
        elif f.get("level") == "warn" and f.get("code") in ("docx_quotes", "docx_comments"):
            issues.append(_issue("file_%s" % f.get("code"), "warn", f.get("title", ""),
                                 f.get("detail", ""),
                                 {"kind": "agent", "what": "clean the DOCX"}, f.get("rule", "")))

    if gate and gate.get("practitioner_required"):
        issues.append(_issue("gate", "block", "A registered practitioner is required",
                             gate.get("reason", ""), {"kind": "edit", "field": "inventors"},
                             "37 CFR 1.31(a)"))

    # signatures: a generated form with nobody's signature on it cannot be filed
    for form in meta.get("forms") or []:
        if form.get("name", "").startswith("SIGNED_"):
            continue
        if form.get("needs_signature") and not form.get("presigned"):
            # A demo never reaches Patent Center, so an unsigned form does not stop
            # it. Fabricating a signature so the demo looks tidy would be worse:
            # the whole point of the signing step is that a real one is required.
            demo = bool(meta.get("demo"))
            issues.append(_issue(
                "sign_%s" % form["name"], "warn" if demo else "block",
                "%s is not signed" % form["name"],
                ("A real filing needs this signed; this demo stops before filing, so it "
                 "does not block. Sign it anyway to try the signer."
                 if demo else "Sign it on screen, or send it to the signer."),
                {"kind": "sign", "form": form["name"], "who": form.get("signer_name", "")},
                "37 CFR 1.4(d)"))

    blocking = [i for i in issues if i["level"] == "block"]
    return {"issues": issues, "blocking": len(blocking), "ready": not blocking}
