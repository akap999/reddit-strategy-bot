"""Read a PDF as text.

Regulators, standards bodies and manufacturers publish their primary documents as PDFs, and until
now the fetch ladder handed those bytes to the model as if they were a web page: a 3.6 MB drug
label reached the evidence as `%PDF-1.7 %\xd0\xd4\xc5\xd8 6460 0 obj <</Filter/FlateDecode...`.
Nothing downstream reads a PDF — Anthropic's web fetch returns the file base64-encoded rather than
its text — so the most authoritative source class we cite was simultaneously unusable and a source
of pollution. This module turns those bytes into the text they contain.

`pypdf` is treated as optional: without it every function here returns "" and each caller degrades
to the behaviour it had before, minus the mojibake. Nothing raises.
"""

import base64
import io
import os
import re
from html import escape as _escape

try:                                     # optional dependency — never fail the import
    from pypdf import PdfReader
except Exception:                        # pragma: no cover - exercised only on a machine without it
    PdfReader = None

_PDF_MAGIC = b"%PDF-"
# the base64 of b"%PDF-" — enough to recognise a base64-wrapped PDF without paying for a decode
_PDF_MAGIC_B64 = "JVBERi0"
# A long document is read from the FRONT: a label's boxed warning, indications and dosing all sit in
# its first pages, and every consumer caps the text it keeps far below these ceilings anyway.
PDF_MAX_PAGES = int(os.environ.get("PDF_MAX_PAGES", "40"))
PDF_MAX_CHARS = int(os.environ.get("PDF_MAX_CHARS", "200000"))
_warned = False


def pypdf_available():
    """Is a PDF readable at all in this process?"""
    return PdfReader is not None


def looks_like_pdf(data, content_type=""):
    """True when a fetched response is a PDF — by its declared type or by its magic bytes, because a
    server that mislabels the type still sends the file."""
    if "application/pdf" in (content_type or "").lower():
        return True
    if isinstance(data, str):
        data = data[:8].encode("latin-1", "ignore")
    return bool(data) and bytes(data[:5]) == _PDF_MAGIC


def pdf_text(data, max_pages=None, max_chars=None):
    """The readable text of a PDF, or "" when it has none we can reach. A scanned document has no
    text layer, and no amount of re-fetching (or residential GB) will give it one — so "" here means
    "this document cannot be read", not "try again"."""
    global _warned
    if not data:
        return ""
    if PdfReader is None:
        if not _warned:
            _warned = True
            print("[pdf_text] pypdf is not installed — PDF sources cannot be read "
                  "(add pypdf to requirements.txt)", flush=True)
        return ""
    max_pages = PDF_MAX_PAGES if max_pages is None else int(max_pages)
    max_chars = PDF_MAX_CHARS if max_chars is None else int(max_chars)
    try:
        reader = PdfReader(io.BytesIO(data))
        pages = reader.pages
    except Exception as e:
        print(f"[pdf_text] could not open the PDF: {e}", flush=True)
        return ""
    parts, n = [], 0
    for i, page in enumerate(pages):
        if i >= max_pages or n >= max_chars:
            break
        try:
            t = page.extract_text() or ""
        except Exception:
            continue                      # one unreadable page must not lose the rest
        if t:
            parts.append(t)
            n += len(t)
    # A PDF's text carries the page LAYOUT as whitespace — a label's multi-column first page is
    # mostly blank runs. Collapse it here so the leading characters every consumer keeps are
    # content, not the shape of the page.
    return re.sub(r"\s+", " ", "\n".join(parts)).strip()[:max_chars]


def is_base64_pdf(text):
    """True when a payload is a base64-encoded PDF rather than the page text a caller asked for."""
    return (text or "").lstrip().startswith(_PDF_MAGIC_B64)


def pdf_text_from_base64(text, **kw):
    """The text of a PDF that arrived base64-encoded (what Anthropic's web fetch returns for one).
    "" when the payload is not a base64 PDF, or carries no text layer."""
    s = (text or "").strip()
    if not is_base64_pdf(s):
        return ""
    try:
        data = base64.b64decode(s, validate=False)
    except Exception:
        return ""
    return pdf_text(data, **kw) if looks_like_pdf(data) else ""


def as_page_html(text):
    """Wrap plain document text so the HTML-shaped consumers downstream read it unchanged — they
    strip tags and unescape entities, so what comes back out is exactly what went in."""
    return "<html><body><pre>" + _escape(text or "") + "</pre></body></html>"
