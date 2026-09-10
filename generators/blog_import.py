"""FU180 — import an EXISTING blog (HTML / .doc / .docx / Markdown) as a blog row for a brand.

Once imported it is an ordinary blog: the watermark-free Qwen rewrite, the LinkedIn post + article,
the YouTube package, every export and publish-tracking all work on it unchanged.

Three things this module gets right, each learned from a real file rather than assumed:
  1. FORMAT IS SNIFFED FROM CONTENT, NOT THE EXTENSION. This tool's own "⬇ Google Doc" export writes
     HTML and names it `.doc` (app.py `fmt == "gdoc"`), so extension-based dispatch would hand a
     perfectly parseable file to a Word parser. A genuine binary Word .doc (OLE, magic D0 CF 11 E0)
     is rejected with an actionable message instead of being imported as mojibake.
  2. DATA-URI IMAGES ARE DROPPED. A real export carried one base64 logo that was 75% of the whole
     document (61,155 chars -> 15,327 after stripping). The body is fed verbatim into the rewrite
     prompt and every Claude prompt, so an inlined blob is not cosmetic — it is context burned.
  3. THIS TOOL'S OWN SCAFFOLDING IS REMOVED on re-import (the byline placeholder, the Last-updated
     dateline) so a round-trip does not accumulate duplicates.

Pure + deterministic: no network, no LLM. Never raises for content reasons — it raises
BlogImportError with a message meant for the operator.
"""
import re
import zipfile
import io
import xml.etree.ElementTree as ET

MAX_IMPORT_BYTES = 4 * 1024 * 1024      # a blog is prose; 4 MB is already generous
_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


class BlogImportError(Exception):
    """A problem the OPERATOR can act on (wrong format, empty file, too large)."""


# ── format sniffing ──────────────────────────────────────────────────────────────────────────────
def sniff_format(data, filename=""):
    """Return 'html' | 'docx' | 'ole_doc' | 'text'. CONTENT wins over the extension."""
    if data[:4] == b"\xd0\xcf\x11\xe0":                 # OLE2 compound file = legacy binary .doc
        return "ole_doc"
    if data[:2] == b"PK":                               # zip — .docx (or something else zipped)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if "word/document.xml" in z.namelist():
                    return "docx"
        except Exception:
            pass
        raise BlogImportError("that .zip isn't a Word .docx — export the blog as .docx or .html")
    head = data[:4096].decode("utf-8", "ignore").lstrip().lower()
    if head.startswith("<!doctype html") or head.startswith("<html") or "<body" in head or "<h1" in head:
        return "html"
    if (filename or "").lower().endswith((".html", ".htm")):
        return "html"
    return "text"


# ── HTML ─────────────────────────────────────────────────────────────────────────────────────────
def parse_html(raw):
    """Extract the blog fields from an HTML document. Handles this tool's own exports and any
    ordinary published article."""
    from bs4 import BeautifulSoup
    from markdownify import markdownify

    soup = BeautifulSoup(raw, "html.parser")
    meta_title = (soup.title.string or "").strip() if soup.title and soup.title.string else ""

    def _meta(name, attr="name"):
        tag = soup.find("meta", attrs={attr: name})
        return ((tag.get("content") or "").strip() if tag else "")

    meta_description = _meta("description") or _meta("og:description", "property")
    keywords = [k.strip() for k in _meta("keywords").split(",") if k.strip()]

    for tag in soup(["script", "style", "noscript", "nav", "footer", "form"]):
        tag.decompose()
    # a base64 logo was 75% of a real export — never let one into the stored body
    for img in soup.find_all("img"):
        if (img.get("src") or "").startswith("data:"):
            img.decompose()

    h1 = soup.find("h1")
    title = h1.get_text(strip=True) if h1 else ""
    root = soup.find("article") or soup.find("main") or soup.body or soup
    body = markdownify(str(root), heading_style="ATX", bullets="-")
    return {"title": title, "meta_title": meta_title, "meta_description": meta_description,
            "keywords": keywords, "body_markdown": _clean_markdown(body)}


# ── DOCX (stdlib only — no python-docx dependency) ───────────────────────────────────────────────
def _docx_runs(node):
    out = []
    for r in node.iter(f"{_W}r"):
        txt = "".join(t.text or "" for t in r.iter(f"{_W}t"))
        if not txt:
            continue
        rpr = r.find(f"{_W}rPr")
        if rpr is not None and rpr.find(f"{_W}b") is not None and txt.strip():
            txt = f"**{txt.strip()}**"
        out.append(txt)
    return "".join(out).strip()


def _docx_paragraph(p):
    text = _docx_runs(p)
    if not text:
        return ""
    ppr = p.find(f"{_W}pPr")
    style = ""
    if ppr is not None:
        st = ppr.find(f"{_W}pStyle")
        style = (st.get(f"{_W}val") or "").lower() if st is not None else ""
        if ppr.find(f"{_W}numPr") is not None:
            return f"- {text}"
    m = re.match(r"heading(\d)", style)
    if m:
        return "#" * min(int(m.group(1)), 6) + " " + re.sub(r"^\*\*(.*)\*\*$", r"\1", text)
    if style == "title":
        return "# " + re.sub(r"^\*\*(.*)\*\*$", r"\1", text)
    return text


def _docx_table(tbl):
    rows = []
    for tr in tbl.findall(f"{_W}tr"):
        cells = [" ".join(_docx_paragraph(p) for p in tc.findall(f"{_W}p")).strip().replace("|", "\\|")
                 for tc in tr.findall(f"{_W}tc")]
        if any(cells):
            rows.append(cells)
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    out = ["| " + " | ".join(rows[0]) + " |", "|" + "|".join([" --- "] * width) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows[1:]]
    return "\n".join(out)


def parse_docx(data):
    """Walk word/document.xml in order, mapping paragraphs/headings/lists/tables to Markdown."""
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            xml = z.read("word/document.xml")
    except Exception as e:
        raise BlogImportError(f"couldn't read that .docx ({e})")
    body = ET.fromstring(xml).find(f"{_W}body")
    if body is None:
        raise BlogImportError("that .docx has no document body")
    parts, title = [], ""
    for child in list(body):
        if child.tag == f"{_W}p":
            line = _docx_paragraph(child)
            if line:
                if not title and line.startswith("# "):
                    title = line[2:].strip()
                parts.append(line)
        elif child.tag == f"{_W}tbl":
            t = _docx_table(child)
            if t:
                parts.append(t)
    if not title:   # no Heading 1 → the first real line names the piece
        title = next((re.sub(r"^#+\s*|\*\*", "", p).strip() for p in parts if p.strip()), "")
    return {"title": title[:200], "meta_title": "", "meta_description": "", "keywords": [],
            "body_markdown": _clean_markdown("\n\n".join(parts))}


# ── shared cleanup ───────────────────────────────────────────────────────────────────────────────
_SCAFFOLD_RE = re.compile(
    r"(?im)^\s*(?:\*?\[add author byline before publishing\]\*?"      # FU152 placeholder
    r"|\*?last updated:[^\n]*\*?"                                     # export dateline
    r"|!\[[^\]]*\]\(data:[^)]*\))\s*$")


def _clean_markdown(md):
    """Drop this tool's own scaffolding + any surviving data-URI image, then normalize whitespace."""
    md = _SCAFFOLD_RE.sub("", md or "")
    md = re.sub(r"!\[[^\]]*\]\(data:[^)]*\)", "", md)     # inline (non-line) data URIs too
    md = "\n".join(line.rstrip() for line in md.splitlines())
    md = re.sub(r"\n{3,}", "\n\n", md)
    return md.strip()


def import_blog_file(data, filename=""):
    """Dispatch on sniffed content. Returns the detected blog fields + the format used."""
    if not data:
        raise BlogImportError("that file is empty")
    if len(data) > MAX_IMPORT_BYTES:
        raise BlogImportError(f"that file is {len(data) // 1024}KB — the limit is "
                              f"{MAX_IMPORT_BYTES // 1024}KB; paste the article body instead")
    fmt = sniff_format(data, filename)
    if fmt == "ole_doc":
        raise BlogImportError(
            "that's Word's old binary .doc, which can't be read here — open it in Word or Google "
            "Docs and Save As .docx (or .html), then upload that")
    if fmt == "html":
        out = parse_html(data.decode("utf-8", "replace"))
    elif fmt == "docx":
        out = parse_docx(data)
    else:
        text = data.decode("utf-8", "replace")
        h1 = re.search(r"(?m)^#\s+(.+)$", text)
        out = {"title": (h1.group(1).strip() if h1 else ""), "meta_title": "",
               "meta_description": "", "keywords": [], "body_markdown": _clean_markdown(text)}
    if not (out.get("body_markdown") or "").strip():
        raise BlogImportError("no article text found in that file")
    out["format"] = fmt
    return out
