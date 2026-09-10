"""FU180 — importing an EXISTING blog (HTML / .doc / .docx / Markdown) as a blog row for a brand.

The three rules that came from real files rather than assumption: format is sniffed from CONTENT
(this tool's own .doc export is HTML inside), data-URI images are dropped (one base64 logo was 75%
of a real export, and the body goes verbatim into the rewrite prompt), and the tool's own
scaffolding is stripped so a round-trip doesn't accumulate. All $0, no network.
"""
import io
import json
import os
import re
import tempfile
import zipfile

import pytest

from db import Database
from generators.blog_import import (import_blog_file, sniff_format, parse_html, parse_docx,
                                    BlogImportError, MAX_IMPORT_BYTES)

HTML = """<!doctype html>
<html><head>
<meta charset="utf-8">
<title>Best Collection Agencies for Marketing Agencies (US 2026)</title>
<meta name="description" content="Top agencies for US marketing agencies in 2026.">
<meta name="keywords" content="collections, b2b, agencies">
<script>var tracking = 1;</script>
<style>body{color:red}</style>
</head><body>
<p><em>[Add author byline before publishing]</em></p>
<p><em>Last updated: 2026-09-10</em></p>
<h1>Which Commercial Collection Agencies Are Best?</h1>
<p><strong>Quick answer:</strong> Acme suits claims of $10,000 or more.</p>
<h2>What does Acme charge?</h2>
<p>Acme charges 10-25%. See <a href="https://acme.com/pricing">pricing</a>.</p>
<ul><li>No fee unless collected</li><li>In-house law firm</li></ul>
<table><tr><th>Agency</th><th>Rate</th></tr><tr><td>Acme</td><td>10-25%</td></tr></table>
</body></html>"""


def _docx_bytes(paragraphs):
    """Build a minimal but REAL .docx: a zip whose word/document.xml holds styled paragraphs.
    `paragraphs` is [(style, text)] — style '' is body text, 'Heading1'/'Heading2' are headings."""
    W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    body = []
    for style, text in paragraphs:
        ppr = f'<w:pPr><w:pStyle w:val="{style}"/></w:pPr>' if style else ""
        body.append(f'<w:p>{ppr}<w:r><w:t>{text}</w:t></w:r></w:p>')
    xml = (f'<?xml version="1.0"?><w:document xmlns:w="{W}"><w:body>'
           + "".join(body) + '</w:body></w:document>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", xml)
        z.writestr("[Content_Types].xml", "<Types/>")
    return buf.getvalue()


# ── format sniffing: CONTENT wins over the extension ─────────────────────────────────────────────
def test_the_tools_own_doc_export_is_html_and_is_sniffed_as_such():
    """app.py's `fmt == "gdoc"` writes HTML and names it .doc — extension dispatch would break it."""
    assert sniff_format(HTML.encode(), "my-blog.doc") == "html"
    assert sniff_format(HTML.encode(), "my-blog.html") == "html"


def test_real_docx_and_legacy_binary_doc_are_told_apart():
    assert sniff_format(_docx_bytes([("Heading1", "T")]), "x.docx") == "docx"
    assert sniff_format(b"\xd0\xcf\x11\xe0stuff", "old.doc") == "ole_doc"


def test_legacy_binary_doc_is_rejected_with_an_actionable_message():
    with pytest.raises(BlogImportError) as e:
        import_blog_file(b"\xd0\xcf\x11\xe0" + b"\x00" * 200, "old.doc")
    assert "Save As .docx" in str(e.value)


def test_plain_markdown_falls_through_to_text():
    out = import_blog_file(b"# My Title\n\nSome body copy.\n", "post.md")
    assert out["format"] == "text" and out["title"] == "My Title"


# ── HTML parsing + detection ─────────────────────────────────────────────────────────────────────
def test_html_detection_maps_onto_the_blog_columns():
    out = parse_html(HTML)
    assert out["title"] == "Which Commercial Collection Agencies Are Best?"      # <h1>
    assert out["meta_title"].startswith("Best Collection Agencies")               # <title>
    assert out["meta_description"] == "Top agencies for US marketing agencies in 2026."
    assert out["keywords"] == ["collections", "b2b", "agencies"]


def test_structure_survives_the_conversion():
    b = parse_html(HTML)["body_markdown"]
    assert re.search(r"(?m)^# Which Commercial", b)
    assert re.search(r"(?m)^## What does Acme charge", b)
    assert "| Agency | Rate |" in b and "| --- |" in b        # GFM table
    assert "[pricing](https://acme.com/pricing)" in b
    assert re.search(r"(?m)^- No fee unless collected", b)
    assert "$10,000" in b and "10-25%" in b                    # the facts are what matter downstream


def test_scripts_and_styles_never_reach_the_body():
    b = parse_html(HTML)["body_markdown"]
    assert "var tracking" not in b and "color:red" not in b


def test_this_tools_own_scaffolding_is_stripped_on_reimport():
    """Round-tripping an export must not accumulate the byline placeholder / dateline."""
    b = parse_html(HTML)["body_markdown"]
    assert "Add author byline before publishing" not in b
    assert "Last updated:" not in b


def test_data_uri_images_are_dropped():
    """A real export's base64 logo was 75% of the file; the body is fed verbatim into the rewrite
    prompt, so an inlined blob is burned context, not a cosmetic issue."""
    blob = "A" * 5000
    html = f'<html><body><h1>T</h1><img src="data:image/jpeg;base64,{blob}"><p>Real copy.</p></body></html>'
    b = parse_html(html)["body_markdown"]
    assert "data:image" not in b and blob not in b
    assert "Real copy." in b
    assert len(b) < 200


# ── DOCX parsing (stdlib only) ───────────────────────────────────────────────────────────────────
def test_docx_headings_and_title_detection():
    out = parse_docx(_docx_bytes([
        ("Heading1", "Which agency is best?"),
        ("", "Acme suits large claims."),
        ("Heading2", "What does Acme charge?"),
        ("", "10-25% of what is collected."),
    ]))
    assert out["title"] == "Which agency is best?"
    b = out["body_markdown"]
    assert re.search(r"(?m)^# Which agency is best\?", b)
    assert re.search(r"(?m)^## What does Acme charge\?", b)
    assert "10-25%" in b


def test_docx_without_a_heading_falls_back_to_the_first_line():
    out = parse_docx(_docx_bytes([("", "An untitled piece about collections."), ("", "More copy.")]))
    assert out["title"] == "An untitled piece about collections."


def test_a_zip_that_is_not_a_docx_is_rejected_clearly():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(BlogImportError) as e:
        import_blog_file(buf.getvalue(), "archive.zip")
    assert "isn't a Word .docx" in str(e.value)


# ── guards ───────────────────────────────────────────────────────────────────────────────────────
def test_empty_and_oversized_files_are_refused():
    with pytest.raises(BlogImportError):
        import_blog_file(b"", "x.html")
    with pytest.raises(BlogImportError) as e:
        import_blog_file(b"<html><body>x</body></html>" + b"x" * MAX_IMPORT_BYTES, "big.html")
    assert "limit is" in str(e.value)


def test_a_file_with_no_article_text_is_refused():
    with pytest.raises(BlogImportError) as e:
        import_blog_file(b"<html><head><title>t</title></head><body></body></html>", "empty.html")
    assert "no article text" in str(e.value)


# ── the endpoint: operator input WINS, blanks are detected ───────────────────────────────────────
def _client(tmp_db):
    import app as appmod
    appmod.DB_PATH = tmp_db
    appmod._db_initialized = True
    return appmod.app.test_client()


def _brand(path):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    db.close()
    return bid


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _post(c, brand_id, data=HTML.encode(), name="blog.html", **fields):
    payload = {"file": (io.BytesIO(data), name), "brand_id": str(brand_id)}
    payload.update(fields)
    return c.post("/api/blogs/import", data=payload, content_type="multipart/form-data")


def test_import_endpoint_detects_everything_when_nothing_is_typed():
    path = _tmp()
    try:
        c, bid = _client(path), _brand(path)
        r = _post(c, bid)
        assert r.status_code == 200, r.get_json()
        j = r.get_json()
        assert j["title"] == "Which Commercial Collection Agencies Are Best?"
        assert j["meta_title"].startswith("Best Collection Agencies")
        assert j["keywords"] == ["collections", "b2b", "agencies"]
        assert j["format"] == "html"
        db = Database(path); db.connect()
        blog = db.get_blog(j["blog_id"]); db.close()
        assert blog["seed"] == j["title"]          # seed defaults to the title (the target query)
        assert blog["status"] == "draft" and blog["prompt_version"] == "imported"
        assert "| Agency | Rate |" in blog["body_markdown"]
    finally:
        os.unlink(path)


def test_operator_values_override_every_detected_field():
    path = _tmp()
    try:
        c, bid = _client(path), _brand(path)
        j = _post(c, bid, title="My Own Title", seed="which agency should I use?",
                  meta_title="My Meta", meta_description="My description",
                  keywords="one, two").get_json()
        assert j["title"] == "My Own Title" and j["meta_title"] == "My Meta"
        assert j["meta_description"] == "My description" and j["keywords"] == ["one", "two"]
        db = Database(path); db.connect()
        blog = db.get_blog(j["blog_id"]); db.close()
        assert blog["seed"] == "which agency should I use?"
        assert blog["meta_title"] == "My Meta"
    finally:
        os.unlink(path)


def test_a_doc_named_file_that_is_really_html_imports_fine():
    path = _tmp()
    try:
        c, bid = _client(path), _brand(path)
        j = _post(c, bid, name="exported.doc").get_json()
        assert j["format"] == "html" and j["chars"] > 0
    finally:
        os.unlink(path)


def test_endpoint_requires_a_file_and_a_real_brand():
    path = _tmp()
    try:
        c, bid = _client(path), _brand(path)
        assert c.post("/api/blogs/import", data={"brand_id": str(bid)},
                      content_type="multipart/form-data").status_code == 400
        assert _post(c, 0).status_code == 400
        assert _post(c, 99999).status_code == 404
    finally:
        os.unlink(path)


def test_a_bad_file_returns_400_not_500():
    path = _tmp()
    try:
        c, bid = _client(path), _brand(path)
        r = _post(c, bid, data=b"\xd0\xcf\x11\xe0" + b"\x00" * 100, name="old.doc")
        assert r.status_code == 400 and "Save As .docx" in r.get_json()["error"]
    finally:
        os.unlink(path)
