"""FU211 — a web search that FOUND sources must not be thrown away because its answer wasn't bare JSON.

Observed in production (the Thyseed baby-bottle blog, YMYL on, no AAP/FDA citation): in 26 minutes
`search_sources` logged 22 "could not parse sources JSON" failures and `fetch_site_facts` 4 — 18 of
them "Expecting value … (char 0)", i.e. the model wrote "I'll search for…" before its JSON. Each one
returned [] and the search was lost: official sources, competitor prices, everything. And AAP's
parent site (healthychildren.org) could never count as official at all.
"""
import json
from types import SimpleNamespace as NS

from generators.base import ClaudeClient, _extract_json_object, _web_citations
from generators.blog_gen import _official_source_ok, _YMYL_OFFICIAL_DOMAINS

PINS = _YMYL_OFFICIAL_DOMAINS["medical"]
SRC = {"sources": [{"title": "Infant Formula and Bottle Feeding", "url": "https://www.fda.gov/food/infant",
                    "fact": "FDA regulates materials that contact infant food."},
                   {"title": "How to Sterilize Bottles", "url": "https://www.cdc.gov/hygiene/bottles.html",
                    "fact": "Clean bottles after every feeding."}]}


def _text(t, citations=None):
    return NS(type="text", text=t, citations=citations)


def _cite(url, title, cited):
    return NS(type="web_search_result_location", url=url, title=title, cited_text=cited)


def _client(*blocks):
    c = ClaudeClient("test-key")
    msg = NS(content=list(blocks), usage=None)
    calls = []

    def create(**kw):
        calls.append(kw)
        return msg
    c.client = NS(messages=NS(create=create))
    c._calls = calls
    return c


def test_clean_json_is_unchanged():
    c = _client(_text(json.dumps(SRC)))
    out = c.search_sources("brief")
    assert [s["url"] for s in out] == ["https://www.fda.gov/food/infant", "https://www.cdc.gov/hygiene/bottles.html"]
    assert out[0]["fact"] == "FDA regulates materials that contact infant food."


def test_prose_before_the_json_is_recovered():
    c = _client(_text("I'll search for official guidance on bottle materials.\n\nBased on my research:\n"
                      + json.dumps(SRC)))
    assert len(c.search_sources("brief")) == 2


def test_json_followed_by_prose_and_a_second_object_is_recovered():
    c = _client(_text(json.dumps(SRC) + "\n\nNote: I also checked {\"other\": 1} but it was irrelevant."))
    assert len(c.search_sources("brief")) == 2


def test_fenced_json_inside_prose_and_split_across_text_blocks():
    body = json.dumps(SRC)
    c = _client(_text("Here is what I found:\n```json\n" + body[:40]), _text(body[40:] + "\n```\nDone."))
    assert len(c.search_sources("brief")) == 2


def test_truncated_json_falls_back_to_the_pages_the_model_cited():
    cites = [_cite("https://www.healthychildren.org/English/ages-stages/baby/feeding/Pages/bottles.aspx",
                   "Choosing a Baby Bottle", "Glass bottles are durable but can break."),
             _cite("https://www.fda.gov/food/bpa", "Bisphenol A (BPA)", "BPA is not authorized in baby bottles.")]
    c = _client(_text("Based on the AAP guidance", citations=cites),
                _text('{"sources": [{"title": "Choosing a Baby Bottle", "url": "https://www.healthy'))
    out = c.search_sources("brief")
    assert [s["url"] for s in out] == [cites[0].url, cites[1].url]
    assert out[1]["fact"] == "BPA is not authorized in baby bottles."


def test_an_explicit_empty_answer_is_respected_not_replaced_by_citations():
    c = _client(_text('{"sources": []}', citations=[_cite("https://x.example/a", "A", "a")]))
    assert c.search_sources("brief") == []


def test_nothing_parseable_and_no_citations_still_returns_empty():
    assert _client(_text("I could not find anything useful.")).search_sources("brief") == []
    assert _client().search_sources("brief") == []


def test_search_sources_gets_more_room_to_finish_its_answer():
    c = _client(_text(json.dumps(SRC)))
    c.search_sources("brief")
    assert c._calls[0]["max_tokens"] == 3000


def test_fetch_site_facts_recovers_prose_wrapped_json():
    c = _client(_text('Let me look at the site.\n{"facts": ["Glass bottles from $29.99", "Borosilicate glass"]}'))
    assert c.fetch_site_facts("thyseed.com", "Thyseed", "pricing") == \
        "- Glass bottles from $29.99\n- Borosilicate glass"


def test_fetch_site_facts_citation_fallback_keeps_only_the_pinned_site():
    cites = [_cite("https://www.thyseed.com/products/glass", "Glass bottle", "4oz glass bottle $29.99"),
             _cite("https://www.amazon.com/thyseed", "Amazon", "Thyseed bottle 2-pack $24")]
    c = _client(_text("The site lists", citations=cites))
    assert c.fetch_site_facts("thyseed.com", "Thyseed", "pricing") == "- 4oz glass bottle $29.99"


def test_find_official_domain_recovers_prose_wrapped_json_but_never_guesses_from_citations():
    assert _client(_text('The official site is below.\n{"domain": "comotomo.com"}')).find_official_domain(
        "Comotomo") == "comotomo.com"
    c = _client(_text("I believe it is their store.", citations=[_cite("https://www.amazon.com/comotomo", "A", "a")]))
    assert c.find_official_domain("Comotomo") == ""


def test_extract_json_object_requires_the_expected_key():
    assert _extract_json_object('{"other": 1}', "sources") == (None, "")
    assert _extract_json_object("", "sources") == (None, "")
    data, how = _extract_json_object('x {"sources": [], } y', "sources")
    assert data == {"sources": []} and how == "salvaged"


def test_web_citations_dedupes_and_accepts_dict_blocks():
    msg = {"content": [{"type": "text", "citations": [{"url": "https://a.org/x", "title": "T", "cited_text": " a\n b "},
                                                      {"url": "https://A.org/x", "title": "T2", "cited_text": "dup"}]},
                       {"type": "web_search_tool_result"}]}
    out = _web_citations(NS(content=msg["content"]))
    assert out == [{"title": "T", "url": "https://a.org/x", "fact": "a b"}]


# ── AAP's parent-facing site, and its sister societies' patient sites, are official ─────────
def test_society_owned_patient_sites_are_official():
    for url, title in (("https://www.healthychildren.org/English/ages-stages/baby/feeding-nutrition/Pages/default.aspx",
                        "Feeding & Nutrition"),
                       ("https://familydoctor.org/condition/colic/", "Colic"),
                       ("https://www.cancer.net/cancer-types", "Cancer Types")):
        assert _official_source_ok(url, title, "Thyseed", "thyseed.com", PINS), url


def test_a_generic_org_and_a_lookalike_domain_are_still_not_official():
    assert not _official_source_ok("https://telehealth.org/news/glp1", "FDA GLP-1 Compounding Crackdown",
                                   "PeterMD", "getpetermd.com", PINS)
    assert not _official_source_ok("https://nothealthychildren.org/bottles", "Baby bottles",
                                   "Thyseed", "thyseed.com", PINS)
    assert not _official_source_ok("https://healthychildren.org.bottle-deals.com/bottles", "Baby bottles",
                                   "Thyseed", "thyseed.com", PINS)
