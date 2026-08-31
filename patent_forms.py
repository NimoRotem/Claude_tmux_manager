"""Fill the official USPTO AcroForm PDFs so Patent Center accepts them.

Two things bite here and both are handled once, in `render`:

1. Patent Center rejects a PDF whose BaseFont name contains spaces, EVEN IF the
   font is genuinely embedded. A form filled straight from PyMuPDF comes out with
   "Liberation#20Sans#20Regular" and the upload is refused with "references a
   non-embedded font". Re-distilling through ghostscript with -dSubsetFonts=true
   renames it to a clean subset (ABCDEF+LiberationSans) and it passes.
2. LibreOffice and Distiller leave an /OpenAction on the catalogue, which the
   validators flag as active content. It is stripped along with the AcroForm.

So nothing here writes text through the widget's own appearance stream. Widgets are
read for their rectangles, the values are drawn as page content in an embedded font,
and the widgets are then deleted, which also flattens the form.
"""
from __future__ import annotations

import datetime
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pymupdf
from pypdf import PdfReader, PdfWriter

FORMS_DIR = Path(os.environ.get("PATENT_DATA_DIR",
                                Path.home() / ".tmux-dashboard" / "patents")) / "forms"
FONT_FILE = "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
FONT_ALIAS = "LibSans"

TEMPLATES = {
    "declaration": "aia0001.pdf",      # PTO/AIA/01, 37 CFR 1.63 declaration
    "age_petition": "sb0130.pdf",      # PTO/SB/130, petition to make special on age
    "power_of_attorney": "aia0082.pdf",  # PTO/AIA/82A transmittal + 82B power
    "statement_373": "aia0096.pdf",    # PTO/AIA/96, 37 CFR 3.73(c) statement
}


class FormError(RuntimeError):
    pass


def template_path(kind: str) -> Path:
    name = TEMPLATES.get(kind)
    if not name:
        raise FormError("unknown form %r" % kind)
    p = FORMS_DIR / name
    if not p.exists():
        raise FormError("blank form missing: %s. Re-download it from uspto.gov." % p)
    return p


def _wrap(font: "pymupdf.Font", text: str, size: float, width: float) -> list:
    """Greedy word wrap against the real glyph widths."""
    words, lines, cur = text.split(), [], ""
    for w in words:
        trial = (cur + " " + w).strip()
        if cur and font.text_length(trial, size) > width:
            lines.append(cur)
            cur = w
        else:
            cur = trial
    if cur:
        lines.append(cur)
    return lines or [""]


def render(kind: str, out_path, text_values: dict, checks=(), font_size: float = 10.0,
           title: str = "", author: str = "") -> Path:
    """Fill one template and write a flattened, validator-clean PDF."""
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    src = template_path(kind)
    if not Path(FONT_FILE).exists():
        raise FormError("font missing: %s (apt install fonts-liberation)" % FONT_FILE)

    doc = pymupdf.open(src)
    font = pymupdf.Font(fontfile=FONT_FILE)
    checks = set(checks or ())
    seen, filled = set(), []
    try:
        for page in doc:
            widgets = list(page.widgets())
            if not widgets:
                continue
            page.insert_font(fontname=FONT_ALIAS, fontfile=FONT_FILE)
            for w in widgets:
                name = w.field_name or ""
                seen.add(name)
                rect = w.rect
                if name in checks:
                    # An X drawn as two lines: no ZapfDingbats, no glyph substitution.
                    pad = min(2.0, rect.width * 0.18, rect.height * 0.18)
                    page.draw_line(pymupdf.Point(rect.x0 + pad, rect.y0 + pad),
                                   pymupdf.Point(rect.x1 - pad, rect.y1 - pad),
                                   color=(0, 0, 0), width=1.1)
                    page.draw_line(pymupdf.Point(rect.x1 - pad, rect.y0 + pad),
                                   pymupdf.Point(rect.x0 + pad, rect.y1 - pad),
                                   color=(0, 0, 0), width=1.1)
                    filled.append(name)
                    continue
                value = text_values.get(name)
                if value is None or str(value) == "":
                    continue
                value = str(value)
                size = font_size
                avail_w = max(rect.width - 4.0, 10.0)
                lines = _wrap(font, value, size, avail_w)

                def _overflows(ls, s):
                    # Too tall, or a single unbreakable token wider than the cell.
                    # These boxes sit in a ruled table, so spilling into the next
                    # cell is worse than a smaller point size.
                    return (len(ls) * (s * 1.18) > rect.height - 2.0
                            or any(font.text_length(l, s) > avail_w for l in ls))

                while _overflows(lines, size) and size > 5.0:
                    size -= 0.5
                    lines = _wrap(font, value, size, avail_w)
                y = rect.y0 + size * 1.02 + 1.0
                for line in lines:
                    if y > rect.y1 + size:
                        break
                    page.insert_text((rect.x0 + 2.0, y), line, fontname=FONT_ALIAS,
                                     fontsize=size, color=(0, 0, 0))
                    y += size * 1.18
                filled.append(name)
            for w in list(page.widgets()):
                page.delete_widget(w)

        unknown = [k for k in text_values if k not in seen]
        if unknown:
            raise FormError("field(s) not on %s: %s" % (TEMPLATES[kind], ", ".join(sorted(unknown))))
        missing_checks = [c for c in checks if c not in seen]
        if missing_checks:
            raise FormError("checkbox(es) not on %s: %s"
                            % (TEMPLATES[kind], ", ".join(sorted(missing_checks))))

        with tempfile.TemporaryDirectory(prefix="ptform_") as tmp:
            stage = Path(tmp) / "stage.pdf"
            doc.save(str(stage), garbage=4, deflate=True, clean=True)
            doc.close()
            distilled = Path(tmp) / "gs.pdf"
            if shutil.which("gs"):
                r = subprocess.run(
                    ["gs", "-q", "-dNOPAUSE", "-dBATCH", "-dSAFER", "-sDEVICE=pdfwrite",
                     "-dPDFSETTINGS=/prepress", "-dEmbedAllFonts=true", "-dSubsetFonts=true",
                     "-dCompatibilityLevel=1.5", "-dAutoRotatePages=/None",
                     "-sOutputFile=" + str(distilled), str(stage)],
                    capture_output=True, text=True, timeout=180)
                if r.returncode != 0 or not distilled.exists():
                    raise FormError("ghostscript failed: %s" % (r.stderr or "")[:300])
            else:
                distilled = stage
            reader = PdfReader(str(distilled))
            writer = PdfWriter()
            for pg in reader.pages:
                writer.add_page(pg)
            root = writer._root_object
            for key in ("/AcroForm", "/OpenAction", "/AA", "/Names", "/JavaScript"):
                if key in root:
                    del root[key]
            writer.add_metadata({"/Title": title or TEMPLATES[kind],
                                 "/Author": author or "", "/Producer": "", "/Creator": ""})
            with out_path.open("wb") as fh:
                writer.write(fh)
    finally:
        try:
            doc.close()
        except Exception:
            pass
    return out_path


# --------------------------------------------------------------------------
# validator, the same checks Patent Center applies at upload
# --------------------------------------------------------------------------
def verify(path) -> dict:
    """Page size, font embedding, active content. Mirrors what bounced us once."""
    path = Path(path)
    out = {"path": str(path), "ok": True, "checks": []}

    def note(ok, label, detail=""):
        out["checks"].append({"ok": bool(ok), "label": label, "detail": detail})
        if not ok:
            out["ok"] = False

    if not path.exists():
        note(False, "file exists", str(path))
        return out
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        note(False, "PDF parses", str(exc))
        return out
    note(not getattr(reader, "is_encrypted", False), "not encrypted")
    root = reader.trailer["/Root"]
    note(not any(k in root for k in ("/OpenAction", "/AA", "/JavaScript")),
         "no active content")
    sizes = set()
    for page in reader.pages:
        mb = page.mediabox
        sizes.add((round(float(mb.width)), round(float(mb.height))))
    letter = sizes <= {(612, 792)}
    a4 = sizes <= {(595, 842), (596, 842)}
    note(letter or a4, "US Letter or A4", str(sorted(sizes)))
    bad = []
    with pymupdf.open(str(path)) as doc:
        for pno in range(doc.page_count):
            for f in doc.get_page_fonts(pno):
                # (xref, ext, type, basefont, name, encoding)
                basefont, ext = f[3], f[1]
                if ext in ("n/a", "") or not f[0]:
                    bad.append(basefont + " (not embedded)")
                elif " " in basefont or "#20" in basefont:
                    bad.append(basefont + " (space in the BaseFont name)")
    note(not bad, "every font embedded with a clean name", "; ".join(sorted(set(bad))))
    note(path.stat().st_size < 25 * 1024 * 1024, "under the 25 MB per-file limit",
         "%.1f MB" % (path.stat().st_size / 1e6))
    out["pages"] = len(reader.pages)
    out["bytes"] = path.stat().st_size
    return out


def _today() -> str:
    return datetime.date.today().isoformat()


def _sig(name: str) -> str:
    """An S-signature under 37 CFR 1.4(d)(2): letters between forward slashes."""
    return "/%s/" % name.strip()


# --------------------------------------------------------------------------
# the four documents
# --------------------------------------------------------------------------
def build_declaration(out_path, inventor_name: str, invention_title: str,
                      date: str = "", signature_name: str = "") -> Path:
    """PTO/AIA/01, one per inventor. Filed together with an ADS.

    The packet a drafting tool produces is often missing 37 CFR 1.63(a)(4), the
    "made or authorized to be made by me" statement. This official form has it, so
    generating it here removes that whole class of defect.
    """
    return render(
        "declaration", out_path,
        {"Title of Invention": invention_title,
         "Inventor": inventor_name,
         "Date Optional": date or _today(),
         "Text4": _sig(signature_name or inventor_name)},
        checks=["This declaration"],           # "The attached application"
        title="Declaration (37 CFR 1.63) - PTO/AIA/01", author=inventor_name)


def build_age_petition(out_path, inventor: dict, invention_title: str,
                       application_number: str = "", confirmation_number: str = "",
                       filing_date: str = "", docket: str = "",
                       first_named_inventor: str = "", date: str = "") -> Path:
    """PTO/SB/130, 37 CFR 1.102(c)(1). No fee, and it can go in after filing.

    Option (1) is used deliberately: the inventor's own statement that he is 65 or
    older. MPEP 708.02 warns that any EVIDENCE attached becomes public with the
    file, so a birth certificate or passport is never attached here.
    """
    name = " ".join(p for p in (inventor.get("given"), inventor.get("middle"),
                                inventor.get("family")) if p)
    return render(
        "age_petition", out_path,
        {"Application Number": application_number,
         "Confirmation Number": confirmation_number,
         "Filing Date": filing_date,
         "Attorney Docket Number optional": docket,
         "First Named Inventor": first_named_inventor or name,
         "Title of Invention": invention_title,
         "Given NameRow1": inventor.get("given", ""),
         "Middle NameRow1": inventor.get("middle", ""),
         "Family NameRow1": inventor.get("family", ""),
         "SuffixRow1": inventor.get("suffix", ""),
         "Signature": _sig(name),
         "Date YYYYMMDD": date or _today(),
         "Name": name},
        checks=["I am an inventor in this application and I am 65 years of age or more"],
        title="Petition to Make Special Based on Age - PTO/SB/130", author=name)


def build_power_of_attorney(out_path, *, applicant_name: str, signer_name: str,
                            signer_title: str = "", invention_title: str = "",
                            application_number: str = "", filing_date: str = "",
                            docket: str = "", first_named_inventor: str = "",
                            customer_number: str = "", practitioners=(),
                            signer_is_inventor: bool = True,
                            juristic_applicant: bool = False,
                            date: str = "") -> Path:
    """PTO/AIA/82: the 82A transmittal plus the 82B power itself.

    Either appoint everyone behind a customer number, or list named practitioners.
    A juristic applicant signs through an officer, whose title is required.
    """
    values = {
        "Application Number": application_number,
        "Filing Date": filing_date,
        "First Named Inventor": first_named_inventor,
        "Title": invention_title,
        "Attorney Docket Number": docket,
        "Signature": _sig(signer_name),
        "Date Optional": date or _today(),
        "Name": signer_name,
        "Application Number on 82B": application_number,
        "Filing Date on 82B": filing_date,
        "Applicant Name (if applicant is a juristic entity)": (
            applicant_name if juristic_applicant else ""),
        "Signature of the applicant for patent": _sig(signer_name),
        "Name of the Signer": signer_name,
        "Title of the Signer": signer_title,
        "Date Optional_2": date or _today(),
    }
    checks = []
    if customer_number:
        values["Customer Number"] = customer_number
        values["Customer Number2"] = customer_number
        checks += ["Appoint attorneys listed under customer number",
                   "The address with the above mentioned customer number"]
    else:
        for i, p in enumerate(list(practitioners)[:10], start=1):
            values["NameRow%d" % i] = p.get("name", "")
            values["Registration NumberRow%d" % i] = p.get("registration", "")
        checks.append("Appoint attorneys listed on the attached list")
    if juristic_applicant:
        values["Title if Applicant is a juristic entity"] = signer_title
        values["Applicant Name if Applicant is a juristic entity"] = applicant_name
        checks.append("Assignee")
    elif signer_is_inventor:
        checks.append("Inventor or Joint inventor")
    return render("power_of_attorney", out_path, values, checks=checks,
                  title="Power of Attorney - PTO/AIA/82", author=signer_name)


def build_statement_373(out_path, *, assignee_name: str, assignee_kind: str,
                        application_number: str = "", filing_date: str = "",
                        invention_title: str = "", first_named_inventor: str = "",
                        reel: str = "", frame: str = "", ownership_percent: str = "",
                        signer_name: str = "", signer_title: str = "",
                        date: str = "") -> Path:
    """PTO/AIA/96, the 37 CFR 3.73(c) statement of ownership.

    Needed whenever an assignee wants to take an action, including becoming the
    applicant under 1.46(c)(2). Box 1 is "assignee of the entire right, title and
    interest"; box 3 covers a partial interest and then the percentage matters.
    """
    values = {
        "FirstNamedInventortPatent Owner": first_named_inventor,
        "Application NoPatent No": application_number,
        "FiledIssue Date": filing_date,
        "Titled 1": invention_title,
        "Assignee": assignee_name,
        "AssigneeType": assignee_kind,
        "Text13": _sig(signer_name),
        "Date": date or _today(),
        "Printed or Typed Name": signer_name,
        "Title or Registration Number": signer_title,
    }
    checks = ["Check Box1"]                      # assignee of the entire interest
    if ownership_percent:
        values["The extent by percentage of its ownership interest is"] = ownership_percent
        checks = ["Check Box3"]
    if reel or frame:
        values["the United States Patent and Trademark Office at Reel"] = reel
        values["Frame"] = frame
        checks.append("Check Box9")              # assignment recorded at reel/frame
    else:
        checks.append("Check Box10")             # copies attached
    return render("statement_373", out_path, values, checks=checks,
                  title="Statement under 37 CFR 3.73(c) - PTO/AIA/96", author=signer_name)
