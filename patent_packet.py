"""Take a draft in whatever shape it arrives and tell the truth about it.

Accepts loose files or a zip, works out what each file is, and runs the checks that
Patent Center actually enforces at upload plus the 37 CFR formalities that draw an
Office objection later. Everything here is a real check against the bytes.

Two of these exist because they bounced a live filing on 2026-08-30:

* `docx_cleanliness` - USPTO's DOCX validator warns "Characters from a non-Latin
  script have been detected" for ordinary curly quotation marks, and "Comments were
  found and have been removed" for an EMPTY word/comments.xml part that no author
  ever saw. `clean_docx` removes both, losslessly.
* the font check in patent_forms - a BaseFont name containing spaces is rejected as
  "non-embedded" even when the font is embedded.
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

W_NS = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"

# 37 CFR 1.84(g) sight margins, inches: top 1, left 1, right 5/8, bottom 3/8.
DRAWING_MARGINS_IN = {"top": 1.0, "left": 1.0, "right": 0.625, "bottom": 0.375}

ROLE_LABELS = {
    "exclude": "Not filed (notes, instructions)",
    "specification": "Specification, claims and abstract",
    "drawings": "Drawings",
    "oath": "Inventor's oath or declaration",
    "ads": "Application Data Sheet",
    "petition": "Petition",
    "poa": "Power of attorney",
    "other": "Other",
}
# The description Patent Center wants against each uploaded file.
DOC_DESCRIPTIONS = {
    "specification": "Application body structured text document",
    "drawings": "Drawings-only black and white line drawings",
    "oath": "Oath or Declaration filed",
    "ads": "Application Data Sheet",
    "petition": "Petition to make special based on age/health",
    "poa": "Power of Attorney",
}


def _f(code, level, title, detail="", rule=""):
    return {"code": code, "level": level, "title": title, "detail": detail, "rule": rule}


# --------------------------------------------------------------------------
# intake
# --------------------------------------------------------------------------
def _safe_member(name: str) -> bool:
    """Reject absolute paths and traversal, and the junk archivers add."""
    if not name or name.endswith("/"):
        return False
    p = Path(name)
    if p.is_absolute() or ".." in p.parts:
        return False
    base = p.name
    if base.startswith(".") or "__MACOSX" in p.parts:
        return False
    return True


def expand(src_dir, dest_dir) -> list:
    """Copy files into dest_dir, unpacking any zip. Returns the flat file list."""
    src_dir, dest_dir = Path(src_dir), Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for item in sorted(src_dir.iterdir()):
        if item.is_dir():
            continue
        if item.suffix.lower() == ".zip":
            try:
                with zipfile.ZipFile(item) as zf:
                    for member in zf.namelist():
                        if not _safe_member(member):
                            continue
                        target = dest_dir / Path(member).name
                        target = _dedupe(target)
                        with zf.open(member) as fh, target.open("wb") as out_fh:
                            shutil.copyfileobj(fh, out_fh)
                        out.append(target)
            except zipfile.BadZipFile:
                target = _dedupe(dest_dir / item.name)
                shutil.copy2(item, target)
                out.append(target)
        else:
            target = _dedupe(dest_dir / item.name)
            shutil.copy2(item, target)
            out.append(target)
    return out


def _dedupe(path: Path) -> Path:
    if not path.exists():
        return path
    stem, suffix, i = path.stem, path.suffix, 2
    while True:
        cand = path.with_name("%s-%d%s" % (stem, i, suffix))
        if not cand.exists():
            return cand
        i += 1


def sniff(path) -> str:
    with open(path, "rb") as fh:
        head = fh.read(8)
    if head.startswith(b"%PDF"):
        return "pdf"
    if head.startswith(b"PK\x03\x04"):
        return "docx" if str(path).lower().endswith((".docx", ".docm")) else "zip"
    if head[:4] == b"\xd0\xcf\x11\xe0":
        return "doc"
    return "other"


def guess_role(path) -> str:
    """Name and content based. The panel lets a human override it.

    Order matters. "APPLICATION_DATA_SHEET.pdf" contains the word "sheet", so the
    specific document names have to be tested before the drawings pattern.
    """
    name = Path(path).name.lower()
    kind = sniff(path)
    if kind == "other" or name.endswith((".md", ".txt", ".rtf", ".json", ".csv", ".html")):
        # Notes and instructions travel with a packet all the time. They are not
        # filing documents, and failing the packet over them is noise.
        return "exclude"
    if re.search(r"\bads\b|application[_ -]?data|aia0?0?14|sb0?0?14", name):
        return "ads"
    if re.search(r"declarat|\boath\b|aia0?0?01\b", name):
        return "oath"
    if re.search(r"petition|sb0?130", name):
        return "petition"
    if re.search(r"power[_ -]?of[_ -]?attorney|\bpoa\b|aia0?082", name):
        return "poa"
    if re.search(r"draw|figure|\bfig\b|sheet", name):
        return "drawings"
    if re.search(r"spec|claim|abstract|application", name) or kind == "docx":
        return "specification"
    if kind == "pdf":
        # A multi-page image-only PDF with no text layer is almost always drawings.
        try:
            txt = subprocess.run(["pdftotext", "-q", str(path), "-"], capture_output=True,
                                 text=True, timeout=60).stdout
            if len(re.sub(r"\s+", "", txt)) < 40:
                return "drawings"
        except Exception:
            pass
        return "specification"
    return "other"


# --------------------------------------------------------------------------
# DOCX
# --------------------------------------------------------------------------
def docx_text(path) -> str:
    z = zipfile.ZipFile(path)
    root = ET.fromstring(z.read("word/document.xml"))
    out = []
    for p in root.iter(W_NS + "p"):
        out.append("".join(n.text or "" for n in p.iter(W_NS + "t")))
    return "\n".join(out)


def docx_cleanliness(path) -> dict:
    """What USPTO's DOCX validator will say, before it says it."""
    z = zipfile.ZipFile(path)
    names = z.namelist()
    text = docx_text(path)
    non_ascii = sorted({ch for ch in text if ord(ch) > 126})
    smart = [c for c in non_ascii if c in "‘’“”"]
    other = [c for c in non_ascii if c not in "‘’“”"]
    has_comments = "word/comments.xml" in names
    comments_empty = False
    if has_comments:
        try:
            croot = ET.fromstring(z.read("word/comments.xml"))
            comments_empty = not list(croot.iter(W_NS + "comment"))
        except Exception:
            pass
    return {
        "non_ascii": non_ascii,
        "smart_quotes": len([c for c in text if c in "‘’“”"]),
        "other_non_ascii": other,
        "has_comments_part": has_comments,
        "comments_part_empty": comments_empty,
        "has_macros": any(n.endswith(".bin") and "vbaProject" in n for n in names),
        "cleanable": bool(smart or (has_comments and comments_empty)),
    }


def clean_docx(src, dest) -> dict:
    """Strip the two things USPTO warns about, changing nothing a reader sees.

    Curly quotes become straight ASCII quotes; an empty comments part is removed
    along with its relationship and content-type override. Word still opens it.
    """
    src, dest = Path(src), Path(dest)
    zin = zipfile.ZipFile(src)
    replaced = 0
    dest.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            if item.filename == "word/comments.xml":
                continue
            data = zin.read(item.filename)
            if item.filename == "word/document.xml":
                t = data.decode("utf8")
                before = t
                for a, b in (("“", '"'), ("”", '"'),
                             ("‘", "'"), ("’", "'")):
                    t = t.replace(a, b)
                replaced = sum(before.count(c) for c in "“”‘’")
                data = t.encode("utf8")
            elif item.filename == "word/_rels/document.xml.rels":
                data = re.sub(rb'<Relationship[^>]*Type="[^"]*/comments"[^>]*/>', b"", data)
            elif item.filename == "[Content_Types].xml":
                data = re.sub(rb'<Override[^>]*wordprocessingml\.comments\+xml[^>]*/>',
                              b"", data)
            zout.writestr(item, data)
    return {"quotes_replaced": replaced, "comments_part_removed": True, "path": str(dest)}


# --------------------------------------------------------------------------
# specification content
# --------------------------------------------------------------------------
_SECTIONS = [
    ("background", r"background"),
    ("summary", r"(brief\s+)?summary"),
    ("brief description of the drawings", r"brief\s+description\s+of\s+the\s+.{0,30}draw"),
    ("detailed description", r"detailed\s+description|description\s+of\s+the\s+(preferred\s+)?embodiment"),
]


def _claims_block(text: str) -> str:
    m = re.search(r"(what\s+is\s+claimed\s+is|i\s+claim|we\s+claim|the\s+claims?\s+are)",
                  text, re.I)
    if not m:
        m = re.search(r"(?m)^\s*claims?\s*:?\s*$", text, re.I)
    if not m:
        return ""
    tail = text[m.end():]
    end = re.search(r"\n\s*abstract\b", tail, re.I)
    return tail[:end.start()] if end else tail


def split_claims(block: str) -> list:
    if not block.strip():
        return []
    idx = [(int(m.group(1)), m.start()) for m in re.finditer(r"(?m)^\s*(\d{1,3})\.\s", block)]
    out = []
    for i, (num, pos) in enumerate(idx):
        end = idx[i + 1][1] if i + 1 < len(idx) else len(block)
        body = re.sub(r"^\s*\d{1,3}\.\s*", "", block[pos:end]).strip()
        if body:
            out.append((num, body))
    return out


def review_specification(path) -> dict:
    kind = sniff(path)
    if kind == "docx":
        text = docx_text(path)
    else:
        text = subprocess.run(["pdftotext", "-q", "-layout", str(path), "-"],
                              capture_output=True, text=True, timeout=90).stdout
    findings, counts = [], {}
    if len(re.sub(r"\s+", "", text)) < 40:
        return {"findings": [_f("spec_no_text", "warn", "No readable text in the specification",
                                "Image-only file. Structure cannot be checked.", "MPEP 608.01")],
                "counts": {}}

    missing = [label for label, pat in _SECTIONS if not re.search(pat, text, re.I)]
    if missing:
        findings.append(_f("spec_sections", "warn", "Recommended sections not found",
                           "Not detected: " + ", ".join(missing), "37 CFR 1.77"))
    else:
        findings.append(_f("spec_sections", "pass", "37 CFR 1.77 sections all present"))

    claims = split_claims(_claims_block(text))
    if not claims:
        findings.append(_f("claims_missing", "fail", "No claims found",
                           "A nonprovisional needs at least one claim.",
                           "35 USC 112(b) / 37 CFR 1.75"))
    else:
        nums = [n for n, _ in claims]
        indep = [n for n, b in claims if not re.search(r"\bclaims?\s+\d", b, re.I)]
        counts = {"total": len(claims), "independent": len(indep),
                  "dependent": len(claims) - len(indep)}
        findings.append(_f("claims_count", "pass",
                           "%d claims, %d independent" % (len(claims), len(indep))))
        if nums != list(range(1, len(nums) + 1)):
            findings.append(_f("claims_numbering", "warn",
                               "Claims are not numbered 1..N consecutively",
                               "Parsed: %s" % nums, "37 CFR 1.126"))
        forward = []
        for num, body in claims:
            refs = [int(x) for x in re.findall(r"\bclaims?\s+(\d+)", body, re.I)]
            if any(r >= num for r in refs):
                forward.append(num)
        if forward:
            findings.append(_f("claims_forward_ref", "fail",
                               "Dependent claim refers to a later or its own claim",
                               "Claim(s) %s" % forward, "37 CFR 1.75(c)"))
        multi = [n for n, b in claims
                 if len(set(re.findall(r"\bclaims?\s+(\d+)", b, re.I))) > 1]
        if multi:
            findings.append(_f("claims_multi_dep", "warn",
                               "Possible multiple dependent claim",
                               "Claim(s) %s. Each one costs $370/$185 and must be in the "
                               "alternative." % multi, "37 CFR 1.75(c) / 1.16(j)"))
        if len(indep) > 3:
            findings.append(_f("claims_excess_indep", "info",
                               "%d independent claims, over the 3 included" % len(indep),
                               "%d excess independent fee(s) apply." % (len(indep) - 3),
                               "37 CFR 1.16(h)"))
        if len(claims) > 20:
            findings.append(_f("claims_excess_total", "info",
                               "%d claims, over the 20 included" % len(claims),
                               "%d excess claim fee(s) apply." % (len(claims) - 20),
                               "37 CFR 1.16(i)"))

    m = re.search(r"\n\s*abstract(\s+of\s+the\s+disclosure)?\s*[:.]?\s*\n", text, re.I)
    if not m:
        findings.append(_f("abstract_missing", "fail", "No Abstract found",
                           "A utility application needs an abstract on its own sheet.",
                           "37 CFR 1.72(b)"))
    else:
        body = re.split(r"\n\s*(claims?|what is claimed)\b", text[m.end():], flags=re.I)[0]
        words = len(body.split())
        counts["abstract_words"] = words
        if words > 150:
            findings.append(_f("abstract_words", "fail",
                               "Abstract is %d words, over the 150 limit" % words,
                               "", "37 CFR 1.72(b)"))
        else:
            findings.append(_f("abstract_words", "pass",
                               "Abstract is %d words, within 150" % words))
    return {"findings": findings, "counts": counts}


# --------------------------------------------------------------------------
# mechanical checks
# --------------------------------------------------------------------------
def check_file(path, role: str = "other") -> dict:
    path = Path(path)
    rep = {"name": path.name, "path": str(path), "role": role,
           "bytes": path.stat().st_size if path.exists() else 0,
           "filetype": sniff(path) if path.exists() else "missing",
           "findings": []}
    add = rep["findings"].append
    if role == "exclude":
        add(_f("excluded", "info", "Kept out of the submission",
               "Notes and instructions are not filing documents."))
        return rep
    if not path.exists():
        add(_f("missing", "fail", "File not found"))
        return rep
    if rep["bytes"] == 0:
        add(_f("empty", "fail", "File is empty"))
        return rep
    if rep["filetype"] == "doc":
        add(_f("legacy_doc", "fail", "Legacy .doc file",
               "Save as a modern .docx.", "Patent Center DOCX"))
        return rep

    if rep["filetype"] == "docx":
        clean = docx_cleanliness(path)
        rep["docx"] = clean
        if clean["has_macros"]:
            add(_f("docx_macro", "fail", "Macro-enabled document",
                   "Remove macros and save as .docx.", "Patent Center DOCX"))
        if clean["smart_quotes"]:
            add(_f("docx_smart_quotes", "warn",
                   "%d curly quotation marks" % clean["smart_quotes"],
                   "USPTO reports these as \"Characters from a non-Latin script have been "
                   "detected\". One click fixes it.", "Patent Center DOCX"))
        if clean["other_non_ascii"]:
            add(_f("docx_non_ascii", "warn", "Non-Latin characters present",
                   "".join(clean["other_non_ascii"][:20]),
                   "37 CFR 1.52(a) (English language)"))
        if clean["has_comments_part"]:
            add(_f("docx_comments", "warn",
                   "Word comments part present"
                   + (" (empty)" if clean["comments_part_empty"] else ""),
                   "USPTO reports \"Comments were found and have been removed\".",
                   "Patent Center DOCX"))
        if not any(x["level"] in ("fail", "warn") for x in rep["findings"]):
            add(_f("docx_clean", "pass", "DOCX is clean for Patent Center"))
        add(_f("docx_no_surcharge", "pass", "Filed as DOCX, no 1.16(u) surcharge",
               "Saves $172 at small entity.", "37 CFR 1.16(u)"))
        return rep

    if rep["filetype"] != "pdf":
        add(_f("filetype", "fail", "Unsupported file type",
               "Patent Center takes PDF, and DOCX for the specification."))
        return rep

    from pypdf import PdfReader
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        add(_f("pdf_parse", "fail", "PDF will not parse", str(exc)[:160]))
        return rep
    rep["pages"] = len(reader.pages)
    if getattr(reader, "is_encrypted", False):
        add(_f("pdf_encrypted", "fail", "PDF is encrypted",
               "USPTO rejects secured PDFs.", "Patent Center"))
    root = reader.trailer["/Root"]
    if any(k in root for k in ("/OpenAction", "/AA", "/JavaScript")):
        add(_f("pdf_active", "warn", "PDF carries an open action or script",
               "Common from LibreOffice. Re-save without it.", "Patent Center"))
    sizes = {(round(float(p.mediabox.width)), round(float(p.mediabox.height)))
             for p in reader.pages}
    if sizes <= {(612, 792)}:
        add(_f("pdf_size", "pass", "US Letter"))
    elif sizes <= {(595, 842), (596, 842)}:
        add(_f("pdf_size", "pass", "A4"))
    else:
        add(_f("pdf_size", "fail", "Non-standard page size", str(sorted(sizes)),
               "37 CFR 1.52 / 1.84(f)"))
    if rep["bytes"] > 25 * 1024 * 1024:
        add(_f("pdf_too_big", "fail", "Over the 25 MB per-file limit",
               "%.1f MB" % (rep["bytes"] / 1e6), "Patent Center"))

    import pymupdf
    bad_fonts = []
    with pymupdf.open(str(path)) as doc:
        for pno in range(doc.page_count):
            for font in doc.get_page_fonts(pno):
                xref, ext, basefont = font[0], font[1], font[3]
                if not xref or ext in ("n/a", ""):
                    bad_fonts.append(basefont + " (not embedded)")
                elif " " in basefont or "#20" in basefont:
                    bad_fonts.append(basefont + " (space in the BaseFont name)")
    if bad_fonts:
        add(_f("pdf_fonts", "fail", "Font problem Patent Center will reject",
               "; ".join(sorted(set(bad_fonts)))
               + ". A space in the name is refused even when the font IS embedded; "
                 "re-distil with ghostscript -dSubsetFonts=true.", "Patent Center"))
    else:
        add(_f("pdf_fonts", "pass", "Fonts embedded with clean names"))

    if role == "drawings":
        rep["findings"] += _drawing_checks(path)
    return rep


def _drawing_checks(path) -> list:
    """37 CFR 1.84: black line art, nothing in the margins, legible."""
    out = []
    try:
        import numpy as np
        import pymupdf
    except Exception:
        return [_f("dwg_skip", "info", "Drawing image analysis unavailable")]
    dpi = 150
    try:
        doc = pymupdf.open(str(path))
    except Exception as exc:
        return [_f("dwg_open", "warn", "Could not open the drawings", str(exc)[:120])]
    with doc:
        for pno in range(doc.page_count):
            page = doc[pno]
            pix = page.get_pixmap(dpi=dpi)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            rgb = arr[:, :, :3].astype(np.int16)
            gray = rgb.mean(axis=2)
            H, W = gray.shape
            nonwhite = gray < 245
            ink = gray < 128
            if nonwhite.mean() < 0.0015:
                out.append(_f("dwg_blank", "warn", "Sheet %d looks blank" % (pno + 1),
                              "", "37 CFR 1.84", ))
                continue
            spread = rgb.max(axis=2) - rgb.min(axis=2)
            colour = ((spread > 40) & nonwhite).sum() / max(nonwhite.sum(), 1)
            if colour > 0.02:
                out.append(_f("dwg_colour", "fail",
                              "Sheet %d has colour" % (pno + 1),
                              "Colour drawings need a petition and a fee.",
                              "37 CFR 1.84(a)(2)"))
            mid = ((gray >= 55) & (gray <= 205)).mean()
            levels = int((np.bincount(gray.astype(np.uint8).ravel(), minlength=256)
                          > gray.size * 0.0005).sum())
            if mid > 0.06 and levels > 40:
                out.append(_f("dwg_photo", "fail",
                              "Sheet %d looks like a photo or a shaded render" % (pno + 1),
                              "Utility drawings must be black line art.",
                              "37 CFR 1.84(b)"))
            elif ink.any():
                blackness = (gray < 60).sum() / max(ink.sum(), 1)
                if blackness < 0.35 and ink.mean() > 0.002:
                    out.append(_f("dwg_faint", "warn",
                                  "Sheet %d: lines look grey rather than black" % (pno + 1),
                                  "", "37 CFR 1.84(l)"))
            edge = int(0.4 * dpi)
            bands = {
                "top": ink[edge:int(DRAWING_MARGINS_IN["top"] * dpi), edge:W - edge],
                "bottom": ink[H - int(DRAWING_MARGINS_IN["bottom"] * dpi):H - edge, edge:W - edge],
                "left": ink[edge:H - edge, edge:int(DRAWING_MARGINS_IN["left"] * dpi)],
                "right": ink[edge:H - edge, W - int(DRAWING_MARGINS_IN["right"] * dpi):W - edge],
            }
            hit = [k for k, v in bands.items() if v.size and int(v.sum()) > dpi * 3]
            if hit:
                out.append(_f("dwg_margin", "warn",
                              "Sheet %d: content in the %s margin" % (pno + 1, ", ".join(hit)),
                              "Sight margins: top 1in, left 1in, right 5/8in, bottom 3/8in.",
                              "37 CFR 1.84(g)"))
    if not any(x["level"] in ("fail", "warn") for x in out):
        out.append(_f("dwg_ok", "pass", "Drawings pass the 37 CFR 1.84 image checks"))
    return out


# --------------------------------------------------------------------------
# whole packet
# --------------------------------------------------------------------------
def review_packet(files: list) -> dict:
    """files: [{path, role}]. Returns per-file reports plus a packet verdict."""
    reports, sections = [], []
    roles = set()
    for entry in files:
        rep = check_file(entry["path"], entry.get("role", "other"))
        reports.append(rep)
        roles.add(rep["role"])
    filed = [r for r in reports if r["role"] != "exclude"]

    spec = next((e for e in files if e.get("role") == "specification"), None)
    spec_review = {}
    if spec:
        try:
            spec_review = review_specification(spec["path"])
        except Exception as exc:
            spec_review = {"findings": [_f("spec_error", "warn",
                                           "Specification review failed", str(exc)[:160])],
                           "counts": {}}
        sections.append({"name": "Specification content",
                         "findings": spec_review.get("findings", []),
                         "counts": spec_review.get("counts", {})})

    required = []
    if "specification" not in roles:
        required.append(_f("req_spec", "fail", "No specification in the packet",
                           "A nonprovisional needs a description, claims and abstract.",
                           "37 CFR 1.51"))
    else:
        required.append(_f("req_spec", "pass", "Specification present"))
    for role, label, note in (
            ("drawings", "Drawings", "Required where needed to understand the invention."),
            ("oath", "Inventor's oath or declaration",
             "Can be postponed, but that costs the 1.16(f) surcharge ($68 small)."),
            ("ads", "Application Data Sheet",
             "The panel files the Patent Center web ADS instead of a PDF, which is fine.")):
        required.append(_f("req_" + role, "pass" if role in roles else "info",
                           label + (" present" if role in roles else " not in the packet"),
                           "" if role in roles else note, "37 CFR 1.51"))
    sections.insert(0, {"name": "Required documents", "findings": required})

    mech = []
    for rep in reports:
        for finding in rep["findings"]:
            if finding["level"] in ("fail", "warn"):
                mech.append(dict(finding, detail=("%s: %s" % (rep["name"], finding["detail"])).strip(": ")))
    if mech:
        sections.append({"name": "File format", "findings": mech})

    total = sum(r["bytes"] for r in filed)
    if total > 100 * 1024 * 1024:
        sections.append({"name": "Submission size", "findings": [
            _f("pkg_size", "fail", "Submission over the 100 MB Patent Center limit",
               "%.0f MB" % (total / 1e6))]})

    allf = [x for s in sections for x in s["findings"]]
    return {
        "files": reports,
        "sections": sections,
        "counts": spec_review.get("counts", {}),
        "fail": sum(1 for x in allf if x["level"] == "fail"),
        "warn": sum(1 for x in allf if x["level"] == "warn"),
        "pass": sum(1 for x in allf if x["level"] == "pass"),
        "ok": not any(x["level"] == "fail" for x in allf),
        "total_bytes": total,
    }
