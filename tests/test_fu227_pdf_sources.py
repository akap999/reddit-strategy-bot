"""FU227 — a PDF source is read as TEXT, not passed on as bytes.

The primary documents this pipeline most wants to cite — regulator filings, standards, manufacturer
specifications — are published as PDFs. Measured before this change, a real drug label reached the
evidence as `%PDF-1.7 %\xd0\xd4\xc5\xd8 6460 0 obj <</Filter/FlateDecode...` from our own fetch, and
as 4.8 MB of base64 from Anthropic's web fetch. Both are unusable AND pollution: the model was being
"grounded" in the file's container format.
"""
import base64

import pytest

from generators import pdf_text as P
from generators.base import ClaudeClient
from generators.brand_enrichment import _extract_visible_text, _fetch_page, forget_walled_domains

pytest.importorskip("pypdf", reason="PDF reading needs pypdf (requirements.txt)")


def make_pdf(text="ZEPBOUND tirzepatide MEDULLARY THYROID", pages=1):
    """A minimal, valid PDF carrying one text run per page — a few hundred bytes, so the suite needs
    no committed binary fixture."""
    objs = [b"<</Type/Catalog/Pages 2 0 R>>", None]
    kids, page_objs, n = [], [], 3
    for i in range(pages):
        content = f"BT /F1 12 Tf 10 100 Td ({text} page {i + 1}) Tj ET".encode()
        kids.append(b"%d 0 R" % n)
        page_objs.append(
            b"<</Type/Page/Parent 2 0 R/MediaBox[0 0 300 200]/Contents %d 0 R"
            b"/Resources<</Font<</F1 %d 0 R>>>>>>" % (n + 1, 2 + 2 * pages + 1))
        page_objs.append(b"<</Length %d>>stream\n" % len(content) + content + b"\nendstream")
        n += 2
    objs[1] = b"<</Type/Pages/Kids[" + b" ".join(kids) + b"]/Count %d>>" % pages
    objs += page_objs + [b"<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>"]
    out, offs = bytearray(b"%PDF-1.4\n"), []
    for i, o in enumerate(objs, 1):
        offs.append(len(out))
        out += b"%d 0 obj" % i + o + b"endobj\n"
    x = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    for off in offs:
        out += b"%010d 00000 n \n" % off
    out += b"trailer<</Size %d/Root 1 0 R>>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, x)
    return bytes(out)


class _Resp:
    def __init__(self, body, status=200, ctype="application/pdf"):
        self.status_code = status
        self.content = body if isinstance(body, bytes) else body.encode()
        self.headers = {"Content-Type": ctype}

    @property
    def text(self):
        return self.content.decode("latin-1", "replace")


@pytest.fixture(autouse=True)
def _clean_walls():
    forget_walled_domains()
    yield
    forget_walled_domains()


# ------------------------------------------------------------------ the reader
def test_a_pdf_is_recognised_by_its_type_or_its_magic_bytes():
    assert P.looks_like_pdf(make_pdf())
    assert P.looks_like_pdf(b"", "application/pdf")          # declared but empty body
    assert P.looks_like_pdf(make_pdf(), "application/octet-stream")   # mislabelled by the server
    assert not P.looks_like_pdf(b"<html><body>hi</body></html>")
    assert not P.looks_like_pdf(b"")


def test_the_text_comes_out_and_the_container_does_not():
    out = P.pdf_text(make_pdf())
    assert "ZEPBOUND tirzepatide MEDULLARY THYROID" in out
    assert "%PDF" not in out and "FlateDecode" not in out


def test_page_layout_whitespace_is_collapsed():
    # a document's text carries the page LAYOUT as blank runs; left in, they would eat the leading
    # characters every downstream consumer keeps
    out = P.pdf_text(make_pdf(pages=3))
    assert "  " not in out and "\n" not in out
    assert out.count("ZEPBOUND") == 3


def test_the_caps_bound_a_long_document():
    assert len(P.pdf_text(make_pdf(pages=5), max_pages=2).split("page")) == 3      # 2 pages read
    assert len(P.pdf_text(make_pdf(pages=5), max_chars=30)) <= 30


def test_an_unreadable_payload_yields_nothing_and_never_raises():
    assert P.pdf_text(b"") == ""
    assert P.pdf_text(b"%PDF-1.4 truncated garbage") == ""
    assert P.pdf_text(b"<html>not a pdf at all</html>") == ""


def test_a_base64_pdf_is_recognised_and_decoded():
    b64 = base64.b64encode(make_pdf()).decode()
    assert P.is_base64_pdf(b64)
    assert "ZEPBOUND" in P.pdf_text_from_base64(b64)
    assert not P.is_base64_pdf("PGh0bWw+")                  # base64 of "<html>"
    assert P.pdf_text_from_base64("PGh0bWw+") == ""
    assert P.pdf_text_from_base64("plain page text") == ""


def test_document_text_survives_the_html_extractor_it_is_handed_to():
    # the ladder returns an HTML-shaped string, so `<`, `&` and `>` inside a document must round-trip
    raw = "BMI < 30 & weight > 27 kg/m2 — ZEPBOUND"
    assert _extract_visible_text(P.as_page_html(raw), max_chars=500) == raw


# ------------------------------------------------------------- the fetch ladder
def test_a_fetched_pdf_reaches_the_caller_as_readable_text(monkeypatch):
    monkeypatch.setattr("generators.brand_enrichment.requests.get",
                        lambda *a, **k: _Resp(make_pdf()))
    html, reason = _fetch_page("https://x.gov/label/2026/1lbl.pdf")
    assert reason == "ok"
    text = _extract_visible_text(html, max_chars=5000)
    assert "MEDULLARY THYROID" in text
    assert "%PDF" not in text


def test_an_html_page_is_still_read_as_html(monkeypatch):
    page = "<html><head><title>T</title></head><body><p>" + ("real company copy " * 30) + "</p></body></html>"
    monkeypatch.setattr("generators.brand_enrichment.requests.get",
                        lambda *a, **k: _Resp(page, ctype="text/html"))
    html, reason = _fetch_page("https://x.com/")
    assert reason == "ok" and html == page


def test_a_scanned_pdf_spends_no_residential_gb_and_no_web_fetch(monkeypatch):
    """A PDF with no text layer cannot be rescued by re-fetching it — and the file is large, so a
    metered residential GET (or a web fetch that returns the same bytes base64) is pure waste."""
    calls = []

    def _get(*a, **k):
        calls.append(k.get("proxies"))
        return _Resp(b"%PDF-1.4\n" + b"\x00" * 400)      # a PDF we cannot read

    monkeypatch.setattr("generators.brand_enrichment.requests.get", _get)
    monkeypatch.setenv("REDDIT_HTTP_PROXY", "http://user:pw@proxy:1234")
    html, reason = _fetch_page("https://x.gov/scan.pdf")
    assert (html, reason) == ("", "thin")
    assert len(calls) == 1 and calls[0] is None          # direct only, never the proxy


def test_read_page_returns_the_document_text(monkeypatch):
    import generators.research as R
    monkeypatch.setattr("generators.brand_enrichment.requests.get",
                        lambda *a, **k: _Resp(make_pdf()))
    text, how = R.read_page("https://x.gov/label/2026/1lbl.pdf")
    assert how == "direct"
    assert "MEDULLARY THYROID" in text and "%PDF" not in text


# --------------------------------------------------------------- the web fetch
class _Msg:
    def __init__(self, data):
        self._data = data
        self.usage = None

    def model_dump(self, **kw):
        return {"content": [{"type": "web_fetch_tool_result",
                             "content": {"type": "web_fetch_result",
                                         "content": {"source": {"data": self._data}}}}]}


def _client(data):
    c = ClaudeClient("x")
    c.client = type("C", (), {"messages": type("M", (), {
        "create": staticmethod(lambda **kw: _Msg(data))})()})()
    return c


def test_web_fetch_decodes_a_pdf_instead_of_returning_megabytes_of_base64():
    text, code = _client(base64.b64encode(make_pdf()).decode()).web_fetch_text("https://x.gov/a.pdf")
    assert code == "ok"
    assert "ZEPBOUND" in text and not text.startswith("JVBERi0")


def test_web_fetch_says_so_when_a_pdf_cannot_be_read():
    bad = base64.b64encode(b"%PDF-1.4\n" + b"\x00" * 200).decode()
    assert _client(bad).web_fetch_text("https://x.gov/a.pdf") == ("", "pdf-unreadable")


def test_web_fetch_passes_ordinary_page_text_through_untouched():
    assert _client("--- \ncanonical: https://x.com\ntitle: T\n\nreal text").web_fetch_text(
        "https://x.com") == ("--- \ncanonical: https://x.com\ntitle: T\n\nreal text", "ok")


def test_without_pypdf_the_ladder_degrades_instead_of_shipping_the_container(monkeypatch):
    """pypdf is optional: a deploy that lacks it must lose the document, not hand its bytes on as
    page text — which is exactly what happened before this change."""
    monkeypatch.setattr(P, "PdfReader", None)
    monkeypatch.setattr(P, "_warned", False)
    monkeypatch.setattr("generators.brand_enrichment.requests.get",
                        lambda *a, **k: _Resp(make_pdf()))
    assert P.pdf_text(make_pdf()) == ""
    assert _fetch_page("https://x.gov/label/2026/1lbl.pdf") == ("", "thin")
