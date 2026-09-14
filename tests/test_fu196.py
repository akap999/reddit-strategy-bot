"""FU196 — the Drive/Docs surface must match the operator's reference document.

The reference ("Can You Take TRT and a GLP-1 at the Same Time_.pdf") is one of our own blogs,
restyled by hand in Google Docs and exported. Measured from it: Manrope throughout, H1 24pt,
H2 18pt, H3 14pt, body 11pt, real heading structure, a bordered comparison table, and no brand
logo above the byline placeholder.

The Drive bridge posts our HTML to an Apps Script that asks Drive to convert it to a native Doc,
and Drive's importer honours INLINE character formatting far more reliably than a <style> block —
inline is also what overrides Docs' built-in Heading 1/2/3 styles. So these tests assert the
styling is inline, not merely present in the stylesheet.

$0, no network (the Apps Script POST is stubbed).
"""
import os
import re
import tempfile

import pytest

from db import Database

BODY = (
    "*[Add author byline before publishing]*\n\n"
    "# Which Agency?\n\n"
    "## Can You Take Both?\n\n"
    "**Quick answer:** Yes, it works [S1].\n\n"
    "### Does it work?\n\n"
    "Yes it does.\n\n"
    "| Platform | Price |\n|---|---|\n| **Acme** | $10 |\n| Ro | - |\n\n"
    "- one\n- two\n\n"
    "## Sources\n\n- [S1] Acme — <https://acme.com>\n"
)
LI_POST = "Line one of the post.\n\nLine two."
LI_ART = "## An article subhead\n\nBody of the article."

_FONT = "'Manrope',Arial,sans-serif"


@pytest.fixture()
def env(monkeypatch):
    """Temp DB + configured Drive bridge + a seeded blog whose brand HAS a logo, with the
    Apps Script POST captured."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    brand_id = db.add_brand(sub["id"], "Acme")
    db.update_brand(brand_id, logo_url="https://acme.com/logo.png")
    blog_id = db.save_blog(brand_id, "which agency", title="Which Agency?",
                           body_markdown=BODY, linkedin_text=LI_POST)
    db.update_blog(blog_id, linkedin_article=LI_ART, linkedin_article_title="An Article")
    db.meta_set("gdoc_script_url", "https://script.example/exec")
    db.meta_set("gdoc_secret", "s3cret")
    db.close()

    import app as appmod
    appmod.DB_PATH = path
    appmod._db_initialized = True
    # never reach for the logo bytes over the network in a test
    monkeypatch.setattr(appmod, "_inline_image_data_uri", lambda *a, **k: "")
    sent = {}

    class _Resp:
        status_code = 200
        text = '{"ok":true,"url":"https://docs.google.com/document/d/abc"}'

        def json(self):
            return {"ok": True, "url": "https://docs.google.com/document/d/abc"}

    def _fake_post(url, json=None, timeout=None, **kw):
        sent.clear()
        sent.update(json or {})
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    yield appmod.app.test_client(), blog_id, sent
    os.unlink(path)


def _upload(client, blog_id, **body):
    r = client.post(f"/api/blogs/{blog_id}/upload-gdoc", json=body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _style_of(html, tag):
    """The inline style attribute of the first <tag ...> in the document."""
    m = re.search(r"<%s\b[^>]*\bstyle=\"([^\"]*)\"" % tag, html)
    assert m, f"<{tag}> has no inline style attribute"
    return m.group(1)


# ── the type scale, applied INLINE (what Drive's importer actually reads) ─────────────────────
@pytest.mark.parametrize("tag,size", [("h1", "24pt"), ("h2", "18pt"),
                                       ("h3", "14pt"), ("p", "11pt")])
def test_blog_upload_carries_manrope_and_the_reference_size_inline(env, tag, size):
    client, blog_id, sent = env
    _upload(client, blog_id)
    style = _style_of(sent["html"], tag)
    assert _FONT in style, f"<{tag}> is not Manrope: {style}"
    assert f"font-size:{size}" in style, f"<{tag}> is not {size}: {style}"


def test_headings_stay_real_headings_so_the_doc_keeps_its_outline(env):
    """The reference PDF carries H1/H2/H3 structure tags and a 19-entry outline, so the operator
    kept real Docs heading styles. Styled <p> would look the same and lose the navigation pane."""
    client, blog_id, sent = env
    _upload(client, blog_id)
    html = sent["html"]
    for tag in ("h1", "h2", "h3"):
        assert f"<{tag} " in html, f"<{tag}> was flattened away"


def test_bold_headings_and_black_text(env):
    client, blog_id, sent = env
    _upload(client, blog_id)
    assert "font-weight:700" in _style_of(sent["html"], "h2")
    assert "color:#000000" in _style_of(sent["html"], "p")


def test_table_cells_are_bordered(env):
    client, blog_id, sent = env
    _upload(client, blog_id)
    for tag in ("th", "td"):
        style = _style_of(sent["html"], tag)
        assert "border:1px solid #cccccc" in style
        assert _FONT in style


def test_source_links_use_the_docs_link_blue(env):
    client, blog_id, sent = env
    _upload(client, blog_id)
    style = _style_of(sent["html"], "a")
    assert "color:#1155cc" in style and "text-decoration:underline" in style


def test_list_items_are_styled(env):
    client, blog_id, sent = env
    _upload(client, blog_id)
    assert _FONT in _style_of(sent["html"], "li")


# ── the logo: the reference opens on the byline placeholder, with no brand mark ───────────────
def test_no_logo_in_the_drive_doc_even_when_the_brand_has_one(env):
    client, blog_id, sent = env
    _upload(client, blog_id)
    html = sent["html"]
    assert "<img" not in html, "the Drive doc still carries the brand logo"
    assert 'class="logo"' not in html
    # ...and the byline placeholder is the first thing in the body, as in the reference
    body = html.split("<body>", 1)[1]
    assert body.index("[Add author byline before publishing]") < body.index("<h1")


def test_the_web_export_keeps_its_logo(env):
    """The logo is dropped for the Docs surface only — the browser page is untouched."""
    client, blog_id, sent = env
    r = client.get(f"/api/blogs/{blog_id}/export?format=html")
    assert r.status_code == 200
    assert 'class="logo"' in r.get_data(as_text=True)


# ── inert proof: the browser-facing export must not move ─────────────────────────────────────
def test_html_export_is_untouched(env):
    """11pt in a browser is unreadably small, so the web page keeps its px stylesheet."""
    client, blog_id, sent = env
    html = client.get(f"/api/blogs/{blog_id}/export?format=html").get_data(as_text=True)
    assert "Manrope" not in html
    assert "font:16px/1.6 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif" in html
    assert 'style="font-family' not in html, "inline styling leaked into the web export"


# ── the other two client-facing Drive uploads ────────────────────────────────────────────────
@pytest.mark.parametrize("variant", ["linkedin_article", "linkedin_post"])
def test_the_linkedin_drive_uploads_match_too(env, variant):
    client, blog_id, sent = env
    _upload(client, blog_id, variant=variant)
    html = sent["html"]
    assert _FONT in html, f"{variant} upload is not Manrope"
    assert _FONT in _style_of(html, "p"), f"{variant} <p> is not styled inline"


# ── the helper itself ────────────────────────────────────────────────────────────────────────
def test_an_existing_inline_style_wins():
    """Ours are written first so anything already on the element takes precedence."""
    import app as appmod
    out = appmod._apply_gdoc_inline_styles('<p style="text-align:center">x</p>')
    style = re.search(r'style="([^"]*)"', out).group(1)
    assert style.startswith("font-family:'Manrope'")
    assert style.endswith("text-align:center")


def test_the_inline_pass_never_raises():
    import app as appmod
    assert appmod._apply_gdoc_inline_styles("") == ""
    assert appmod._apply_gdoc_inline_styles(None) is None
    # unbalanced markup must come back usable, not blow up the upload
    assert "Manrope" in appmod._apply_gdoc_inline_styles("<p>unclosed")
