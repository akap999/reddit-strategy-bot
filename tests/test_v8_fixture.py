"""FU54 golden anchor against the REAL shipped v8 blog (tests/fixtures/v8_blog.html).

v8 baked two brand-promo questions ("Can I use AI Inspo music…", "…cheapest AI Inspo plan…") into its
FAQPage schema — advertising inside structured data. This test parses that exact file, confirms the
SHIPPED schema carried the promo questions (the bug), then runs our current build_blog_jsonld over the
same FAQ and asserts they are now excluded (the fix). Pure/offline ($0).
"""
import html
import json
import os
import re

import pytest

from generators.blog_gen import build_blog_jsonld

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "v8_blog.html")
BRAND = "AI Inspo"


def _load():
    if not os.path.exists(FIXTURE):
        pytest.skip("tests/fixtures/v8_blog.html not present")
    return open(FIXTURE, encoding="utf-8").read()


def _faq_pairs_from_html(htmltext):
    """(question, answer) pairs from the visible FAQ — each `<h3>q?</h3>` + following `<p>`."""
    pairs = []
    for m in re.finditer(r"<h3>(.*?)</h3>\s*<p>(.*?)</p>", htmltext, re.DOTALL):
        q = html.unescape(re.sub(r"<[^>]+>", "", m.group(1))).strip()
        a = html.unescape(re.sub(r"<[^>]+>", "", m.group(2))).strip()
        if q.endswith("?"):
            pairs.append((q, a))
    return pairs


def _shipped_faqpage_questions(htmltext):
    """The FAQPage mainEntity questions v8 actually SHIPPED (from its embedded JSON-LD)."""
    out = []
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', htmltext, re.DOTALL):
        try:
            data = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        nodes = data.get("@graph") if isinstance(data, dict) else None
        for node in (nodes or ([data] if isinstance(data, dict) else [])):
            if isinstance(node, dict) and node.get("@type") == "FAQPage":
                out += [q.get("name", "") for q in node.get("mainEntity", [])]
    return out


def test_v8_shipped_schema_had_the_brand_promo_bug():
    shipped = _shipped_faqpage_questions(_load())
    assert shipped, "expected an embedded FAQPage in the shipped v8 HTML"
    promo = [q for q in shipped if BRAND.lower() in q.lower()]
    assert len(promo) == 2, f"expected v8 to have shipped 2 brand-promo FAQs, got {promo}"


def test_our_builder_strips_v8_brand_promo_faqs():
    pairs = _faq_pairs_from_html(_load())
    assert len(pairs) == 5, [q for q, _ in pairs]
    body = "## FAQ\n\n" + "\n\n".join(f"### {q}\n{a}" for q, a in pairs) + "\n"
    blog = {"title": "v8", "body_markdown": body, "created_at": "2026-07-02"}
    graph = build_blog_jsonld(blog, {"name": BRAND, "domain_url": "ai-inspo.com"})["@graph"]
    faqpage = [n for n in graph if n.get("@type") == "FAQPage"]
    assert faqpage, "expected a FAQPage node"
    questions = [q["name"] for q in faqpage[0]["mainEntity"]]
    # the two brand-promo questions v8 shipped are gone; the topic questions remain
    assert not any(BRAND.lower() in q.lower() for q in questions), questions
    assert len(questions) == 3
    assert any("royalty-free" in q.lower() for q in questions)
