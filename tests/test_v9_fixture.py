"""FU55 golden anchor against the REAL shipped v9 blog (tests/fixtures/v9_blog.html).

v9 fixed the v6→v8 regressions but leaked the reconcile's OWN edit-narration into the article — a broken
comparison-table row ("Beatoven rows are deduplicated above; Udio … removed per sourcing rules") and a
dedicated off-category "Note:" blockquote. This test parses that exact file and asserts `_scrub_meta`
removes the leaked row + note while the 5 real tool rows survive, and (regression lock) that v9's FAQ
schema is 100% topic. Pure/offline ($0)."""
import html
import os
import re

import pytest

from generators.blog_gen import BlogGenerator, build_blog_jsonld
from tests.stubs import StubClaude

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "v9_blog.html")
BRAND = "AI Inspo"


def _load():
    if not os.path.exists(FIXTURE):
        pytest.skip("tests/fixtures/v9_blog.html not present")
    return open(FIXTURE, encoding="utf-8").read()


def _gen():
    return BlogGenerator(StubClaude(), None)


def _table_to_markdown(htmltext):
    """Convert v9's first <table> into a Markdown table (one `|`-row per <tr>), tags stripped."""
    tbl = re.search(r"<table>(.*?)</table>", htmltext, re.DOTALL)
    assert tbl, "no comparison <table> found in v9"
    rows = []
    for tr in re.findall(r"<tr>(.*?)</tr>", tbl.group(1), re.DOTALL):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL)
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
        rows.append("| " + " | ".join(cells) + " |")
    return "\n".join(rows)


def test_scrub_meta_removes_v9_leaked_row_keeps_real_rows():
    gen = _gen()
    md = _table_to_markdown(_load())
    assert "deduplicated above" in md and "per sourcing rules" in md   # the leak is present pre-scrub
    n_rows_before = md.count("\n") + 1
    out = gen._scrub_meta(md)
    # the meta-narration row is gone…
    assert "deduplicated above" not in out
    assert "per sourcing rules" not in out
    assert "no tool-specific fresh fact" not in out
    # …but every real tool row survives
    for tool in ("AI Inspo", "Soundraw", "Mubert", "Beatoven", "Suno"):
        assert tool in out, tool
    assert (out.count("\n") + 1) == n_rows_before - 1   # exactly one row removed


def test_scrub_meta_removes_v9_offcategory_note():
    gen = _gen()
    htmltext = _load()
    m = re.search(r"<blockquote>(.*?)</blockquote>", htmltext, re.DOTALL)
    assert m, "expected the off-category blockquote note in v9"
    note = "> " + html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
    assert "addressed in the risk section" in note                     # the tell is present
    out = gen._scrub_meta(note + "\n\n## Next section\nreal content.\n")
    assert "addressed in the risk section" not in out
    assert "real content." in out                                      # legit content untouched


def test_v9_faq_schema_is_all_topic():
    """Regression lock — v9's FAQ is already topic-only; FU54's FAQ filter must keep it that way."""
    blog = {"title": "v9", "body_markdown": _md_body(_load()), "created_at": "2026-07-02"}
    graph = build_blog_jsonld(blog, {"name": BRAND, "domain_url": "ai-inspo.com"})["@graph"]
    faqpage = [n for n in graph if n.get("@type") == "FAQPage"]
    assert faqpage, "expected a FAQPage node"
    questions = [q["name"] for q in faqpage[0]["mainEntity"]]
    assert questions and not any(BRAND.lower() in q.lower() for q in questions), questions


def _md_body(htmltext):
    """Minimal Markdown FAQ reconstruction from v9's visible <h3>?/<p> pairs."""
    pairs = []
    for m in re.finditer(r"<h3>(.*?)</h3>\s*<p>(.*?)</p>", htmltext, re.DOTALL):
        q = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        a = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if q.endswith("?"):
            pairs.append((q, a))
    return "## FAQ\n\n" + "\n\n".join(f"### {q}\n{a}" for q, a in pairs) + "\n"
