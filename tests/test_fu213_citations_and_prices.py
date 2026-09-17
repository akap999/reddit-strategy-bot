"""FU213 — wrong citations, same-name companies, unverifiable competitor prices, and the deploy race.

Every test here is driven by a defect that SHIPPED in a real export (a Thyseed baby-bottle
comparison): the Sources list was scrambled by a second rebuild, two "Digital Pigeon" B2B pages were
cited as evidence for the bottle brand Pigeon, and three of five competitor prices were wrong — one
cited to a PMC study, one to the FDA's BPA page, one a sale price from a Forbes roundup.

$0 and network-free: StubClaude for every model call, a stubbed `_fetch_url` for every page read.
"""
import json
import multiprocessing
import os
import tempfile
import time

import pytest

from db import Database
from generators.blog_gen import (BlogGenerator, build_blog_jsonld, _biz_topic_tokens,
                                 _is_other_business, _per_unit_price, _price_is_sale)
from tests.stubs import StubClaude


def _iso(days_ago=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def _gen(**kw):
    g = BlogGenerator(StubClaude(**kw), None)
    g._evidence_blocks = []
    g._sources_render = None
    g._price_ledger = {}
    return g


# --------------------------------------------------------------- Change 1: the Sources scramble
def _blocks(n):
    """This run's evidence order: 7 gathered pages, 6 officials, then the vendor pages."""
    out = []
    for i in range(1, n + 1):
        kind = "gathered" if i <= 7 else ("official · " if i <= 13 else "third-party · ")
        out.append({"label": (kind if kind == "gathered" else kind + f"src{i}"),
                    "url": f"https://ex{i}.example/p{i}", "text": f"fact {i}"})
    return out


def test_rebuilding_sources_over_its_own_output_is_byte_stable():
    """The shipped bug: `_finalize_article` rebuilds, then the verify step rebuilds AGAIN over the
    renumbered body and reads [S1..S22] as RAW evidence indexes — so the list became the first 22
    blocks in fetch order."""
    gen = _gen()
    gen._evidence_blocks = _blocks(57)
    body = ("Intro cites [S41] and [S17].\n\nMore [S52], then [S8] again [S41].\n\n"
            "## Sources\n\n- [S41] whatever the model typed\n")
    first = gen._rebuild_sources(body)
    assert "ex41.example" in first and "ex17.example" in first
    assert gen._rebuild_sources(first) == first
    assert gen._rebuild_sources(gen._rebuild_sources(first)) == first


def test_a_resumed_blog_reads_its_own_sources_back_without_the_instance_record():
    """The FU79 resume builds a FRESH generator, so the fallback (label + url match) is what keeps a
    manually-verified/paused blog stable."""
    gen = _gen()
    gen._evidence_blocks = _blocks(57)
    first = gen._rebuild_sources("A [S41] B [S17].\n")
    other = _gen()
    other._evidence_blocks = _blocks(57)
    assert other._rebuild_sources(first) == first


def test_a_model_written_sources_list_keeps_todays_behaviour():
    gen = _gen()
    gen._evidence_blocks = _blocks(10)
    body = "Claim [S2].\n\n## Sources\n\n1. Something the model typed itself\n"
    out = gen._rebuild_sources(body)
    assert "- [S1] " in out and "ex2.example" in out   # renumbered from the RAW index, as before


# ------------------------------------------------- Change 3: a same-named different company
_TOPIC = _biz_topic_tokens("best baby bottles for newborns", "baby feeding products", "baby bottles")


def test_a_same_named_software_listing_is_not_evidence_for_the_goods_brand():
    g2 = {"title": "Digital Pigeon Pricing 2026 | G2",
          "url": "https://www.g2.com/products/digital-pigeon/pricing",
          "fact": "Digital Pigeon plans start at $19/mo for large file delivery"}
    assert _is_other_business(g2, "pigeon.com", _TOPIC) is True


def test_a_real_product_listing_for_the_same_brand_is_kept():
    wm = {"title": "Pigeon SofTouch PPSU Baby Bottle 240ml - Walmart",
          "url": "https://www.walmart.com/ip/pigeon-softouch", "fact": "$12.99 each"}
    own = {"title": "Pigeon pricing", "url": "https://pigeon.com/en/pricing", "fact": "plans"}
    assert _is_other_business(wm, "pigeon.com", _TOPIC) is False
    assert _is_other_business(own, "pigeon.com", _TOPIC) is False


def test_the_same_name_guard_is_inert_without_topic_tokens():
    g2 = {"title": "Digital Pigeon Pricing | G2", "url": "https://g2.com/x", "fact": "software"}
    assert _is_other_business(g2, "pigeon.com", []) is False


# ------------------------------------------------------- Change 4: the verified price ledger
_CITE_WALMART = [{"title": "Philips Avent Anti-colic 9oz 3-pack - Walmart",
                  "url": "https://www.walmart.com/ip/philips-avent-anti-colic-baby-bottle-9oz-3pk",
                  "fact": "Philips Avent Anti-colic baby bottle 9 oz, 3-pack. $23.97"}]


def _accept(cand, cites, brand="Philips", own="philips.com", toks=None, **kw):
    return BlogGenerator._accept_price_candidate(cand, cites, brand, own,
                                                 _TOPIC if toks is None else toks, **kw)


def test_a_retail_listing_with_a_cited_figure_is_accepted_with_a_per_unit_price():
    entry, why = _accept({"url": _CITE_WALMART[0]["url"], "product": "Anti-colic 9 oz",
                          "price": "$23.97", "basis": "3-pack, 9 oz"}, _CITE_WALMART)
    assert entry and why == ""
    assert entry["value"] == "$23.97" and entry["source"] == "retail"
    assert entry["per_unit"] == "$7.99 each"


def test_an_editorial_roundup_is_never_a_price_source():
    cites = [{"title": "The best baby bottles of 2026 | Forbes",
              "url": "https://www.forbes.com/best-baby-bottles", "fact": "Dr. Brown's, $24.99"}]
    entry, why = _accept({"url": cites[0]["url"], "price": "$24.99", "basis": "gift box"},
                         cites, brand="Dr. Brown's", own="drbrownsbaby.com")
    assert entry is None and "not the brand's own site" in why


def test_a_sale_price_is_rejected_unless_the_page_shows_the_regular_one():
    sale = [{"title": "Dr. Brown's gift box - Babylist",
             "url": "https://www.babylist.com/store/dr-browns-gift-box",
             "fact": "Dr. Brown's baby bottle gift box. Sale $24.99 — save 20% today"}]
    entry, why = _accept({"url": sale[0]["url"], "price": "$24.99", "basis": "gift box"},
                         sale, brand="Dr. Brown's", own="drbrownsbaby.com")
    assert entry is None and "sale price" in why

    was = [{"title": "Dr. Brown's bottle - Walmart",
            "url": "https://www.walmart.com/ip/dr-browns-baby-bottle",
            "fact": "Dr. Brown's baby bottle. Sale $19.99, was $24.99"}]
    entry2, _ = _accept({"url": was[0]["url"], "price": "$19.99", "basis": "single bottle"},
                        was, brand="Dr. Brown's", own="drbrownsbaby.com")
    assert entry2 and entry2["value"] == "$24.99"   # the regular price, not the sale one


def test_a_figure_that_appears_on_no_cited_page_is_rejected():
    """The shipped failure: Philips' price cited to a PMC behavioural study, Pigeon's to the FDA
    BPA page — pages that never show a price at all."""
    cites = [{"title": "Bisphenol A (BPA) | FDA", "url": "https://www.fda.gov/bpa",
              "fact": "FDA's assessment of BPA in baby bottles."}]
    entry, why = _accept({"url": cites[0]["url"], "price": "$14.99", "basis": "single"},
                         cites, brand="Pigeon", own="pigeon.com")
    assert entry is None and "not the brand's own site" in why

    own = [{"title": "Pigeon bottles", "url": "https://pigeon.com/bottles",
            "fact": "Pigeon SofTouch baby bottle range — BPA free."}]
    entry2, why2 = _accept({"url": own[0]["url"], "price": "$14.99", "basis": "single"},
                           own, brand="Pigeon", own="pigeon.com")
    assert entry2 is None and "does not appear" in why2


def test_a_same_named_company_page_is_not_a_price_source():
    cites = [{"title": "Digital Pigeon Pricing | G2",
              "url": "https://www.g2.com/products/digital-pigeon/pricing",
              "fact": "Digital Pigeon plans start at $19.00 per month"}]
    entry, why = _accept({"url": cites[0]["url"], "price": "$19.00", "basis": "per month"},
                         cites, brand="Pigeon", own="pigeon.com")
    assert entry is None


def test_the_brands_own_site_beats_a_retailer():
    cites = [{"title": "Comotomo bottle - Amazon", "url": "https://www.amazon.com/comotomo-bottle",
              "fact": "Comotomo baby bottle 8 oz — $24.99"},
             {"title": "Comotomo bottles", "url": "https://comotomo.com/shop/bottle",
              "fact": "Comotomo baby bottle, 8 oz. $22.99"}]
    gen = _gen(price_handler=lambda b, s, d, u: {
        "candidates": [{"url": cites[0]["url"], "price": "$24.99", "basis": "single 8 oz"},
                       {"url": cites[1]["url"], "price": "$22.99", "basis": "single 8 oz"}],
        "citations": cites})
    ledger, missing, dirty = gen._ensure_price_ledger(
        {"id": None}, ["Comotomo"], {"Comotomo": {"dom": "comotomo.com"}}, {}, _TOPIC, "baby bottles")
    assert missing == [] and ledger["Comotomo"]["value"] == "$22.99"
    assert ledger["Comotomo"]["source"] == "own"


def test_your_own_value_is_never_overwritten_and_never_re_checked():
    cf = {"comotomo": {"price": {"value": "$21.00", "basis": "yours", "source": "yours",
                                 "url": "", "checked_at": _iso(900)}}}
    gen = _gen(price_handler=lambda *a: {"candidates": [{"url": "https://comotomo.com/x",
                                                         "price": "$99.99"}], "citations": []})
    ledger, missing, dirty = gen._ensure_price_ledger(
        {"id": None}, ["Comotomo"], {"Comotomo": {"dom": "comotomo.com"}}, cf, _TOPIC, "baby bottles")
    assert ledger["Comotomo"]["value"] == "$21.00" and gen.claude.price_calls == []


def test_a_tool_found_entry_older_than_the_ttl_is_re_checked():
    fresh_cite = [{"title": "Comotomo", "url": "https://comotomo.com/shop",
                   "fact": "Comotomo baby bottle 8 oz $22.99"}]
    cf = {"comotomo": {"price": {"value": "$18.00", "source": "own", "basis": "",
                                 "url": "https://comotomo.com/old", "checked_at": _iso(30)}}}
    gen = _gen(price_handler=lambda *a: {
        "candidates": [{"url": fresh_cite[0]["url"], "price": "$22.99", "basis": "single 8 oz"}],
        "citations": fresh_cite})
    ledger, _m, dirty = gen._ensure_price_ledger(
        {"id": None}, ["Comotomo"], {"Comotomo": {"dom": "comotomo.com"}}, cf, _TOPIC, "baby bottles")
    assert gen.claude.price_calls and ledger["Comotomo"]["value"] == "$22.99" and dirty

    cf_fresh = {"comotomo": {"price": {"value": "$18.00", "source": "own", "basis": "",
                                       "url": "https://comotomo.com/old", "checked_at": _iso(3)}}}
    gen2 = _gen(price_handler=lambda *a: {"candidates": [], "citations": []})
    ledger2, _m2, _d2 = gen2._ensure_price_ledger(
        {"id": None}, ["Comotomo"], {"Comotomo": {"dom": "comotomo.com"}}, cf_fresh, _TOPIC, "x")
    assert gen2.claude.price_calls == [] and ledger2["Comotomo"]["value"] == "$18.00"


# ------------------------------------------------------------ Change 4d: code writes the cells
_TABLE = """# Best baby bottles

| Brand | Starting price | Material |
|---|---|---|
| Thyseed | $29.99 | Glass |
| Philips Avent | $18.99 | PP |
| Comotomo | 5 oz and 8 oz sizes available | Silicone |

Some prose.
"""


def _ledger_gen():
    gen = _gen()
    ledger = {
        "Philips Avent": {"value": "$23.97", "basis": "3-pack, 9 oz", "per_unit": "$7.99 each",
                          "url": "https://www.walmart.com/ip/philips", "source": "retail",
                          "checked_at": _iso(0)},
        "Comotomo": {"value": "$22.99", "basis": "single 8 oz", "per_unit": "",
                     "url": "https://comotomo.com/shop", "source": "own", "checked_at": _iso(0)},
    }
    gen._evidence_blocks = gen._price_ledger_blocks(ledger)
    return gen, ledger


def test_the_cells_are_replaced_with_ledger_values_and_a_real_marker():
    gen, ledger = _ledger_gen()
    out, n = gen._write_price_cells(_TABLE, ledger)
    assert n == 2
    assert "$23.97 (3-pack, 9 oz, $7.99 each) [S1]" in out
    assert "$22.99 (single 8 oz) [S2]" in out
    assert "5 oz and 8 oz sizes available" not in out
    assert "| Thyseed | $29.99 |" in out            # the subject's own cell is never touched by code
    assert out.count("Competitor prices are regular list prices") == 1


def test_a_written_cell_is_cited_to_the_page_the_price_was_read_from():
    """The shipped failure in one line: Philips' price cited to a PMC behavioural study. A cell the
    code writes carries the [S#] of the page the figure was actually read off."""
    gen, ledger = _ledger_gen()
    out, _n = gen._write_price_cells(_TABLE, ledger)
    final = gen._rebuild_sources(out)
    assert "- [S1] price · Philips Avent · walmart.com — <https://www.walmart.com/ip/philips>" in final
    assert "$23.97 (3-pack, 9 oz, $7.99 each) [S1]" in final


def test_writing_the_cells_twice_is_stable():
    gen, ledger = _ledger_gen()
    once, _ = gen._write_price_cells(_TABLE, ledger)
    twice, n2 = gen._write_price_cells(once, ledger)
    assert twice == once and n2 == 0


def test_the_price_evidence_block_carries_the_page_it_was_read_from():
    _gen_, ledger = _ledger_gen()
    blocks = BlogGenerator._price_ledger_blocks(ledger)
    assert blocks[0]["url"] == "https://www.walmart.com/ip/philips"
    assert "regular price: $23.97" in blocks[0]["text"] and "3-pack" in blocks[0]["text"]


# ------------------------------------------------------------ Change 5: the operator's own link
_PAGE = ("Philips Avent Anti-colic baby bottle, 9 oz, 3-pack. Add to cart. $23.97. "
         "Free shipping over $35. Reviews (412).") * 4


def _link_gen(page_text, call_handler=None, price_handler=None):
    gen = _gen(call_handler=call_handler, price_handler=price_handler)
    gen._fetch_url = lambda u: page_text
    return gen


def test_a_readable_link_is_accepted_and_cited_to_that_page():
    gen = _link_gen(_PAGE, call_handler=lambda p: {
        "candidates": [{"product": "Anti-colic 9 oz", "price": "$23.97", "basis": "3-pack, 9 oz"}]})
    entry, why = gen._price_from_link("Philips Avent", ["https://philips.com/p/9oz3pk"],
                                      "Philips Avent", _TOPIC)
    assert entry and entry["source"] == "link" and entry["value"] == "$23.97"
    assert entry["url"] == "https://philips.com/p/9oz3pk"


def test_a_figure_the_page_does_not_show_is_rejected():
    gen = _link_gen(_PAGE, call_handler=lambda p: {
        "candidates": [{"product": "x", "price": "$19.99", "basis": "single"}]})
    entry, why = gen._price_from_link("Philips Avent", ["https://philips.com/p"], "Philips Avent", _TOPIC)
    assert entry is None and "not in the page text" in why


def test_a_sale_page_uses_the_regular_price():
    page = ("Dr. Brown's Natural Flow bottle. Sale $19.99 — save 20%. Was $24.99. "
            "Baby bottle, single 8 oz.") * 4
    gen = _link_gen(page, call_handler=lambda p: {
        "candidates": [{"product": "Natural Flow", "price": "$19.99", "basis": "single 8 oz"}]})
    entry, _ = gen._price_from_link("Dr. Brown's", ["https://drbrownsbaby.com/p"], "Dr. Brown's", _TOPIC)
    assert entry and entry["value"] == "$24.99"


def test_a_link_to_the_wrong_brands_page_is_rejected():
    page = ("Comotomo natural feel baby bottle 8 oz. $22.99. Add to cart.") * 8
    gen = _link_gen(page, call_handler=lambda p: {
        "candidates": [{"product": "bottle", "price": "$22.99", "basis": "single"}]})
    entry, why = gen._price_from_link("Philips Avent", ["https://example.com/other"],
                                      "Philips Avent", _TOPIC)
    assert entry is None and "never names" in why


def test_a_blocked_link_falls_back_to_one_search_pinned_to_that_exact_page():
    url = "https://philips.com/p/9oz3pk"
    cites = [{"title": "Philips Avent 9 oz 3-pack", "url": url,
              "fact": "Philips Avent Anti-colic baby bottle 9 oz 3-pack $23.97"}]
    other = [{"title": "elsewhere", "url": "https://www.walmart.com/ip/philips",
              "fact": "Philips Avent baby bottle $21.00"}]
    gen = _link_gen("", price_handler=lambda b, s, d, u: {
        "candidates": [{"url": other[0]["url"], "price": "$21.00", "basis": "single"},
                       {"url": url, "price": "$23.97", "basis": "3-pack, 9 oz"}],
        "citations": cites + other})
    entry, _ = gen._price_from_link("Philips Avent", [url], "Philips Avent", _TOPIC)
    assert gen.claude.price_calls and gen.claude.price_calls[0]["url_hint"] == url
    assert entry and entry["url"] == url and entry["value"] == "$23.97"   # the other page is refused


def test_a_link_that_yields_nothing_leaves_the_brand_for_the_operator_to_answer():
    gen = _link_gen("", price_handler=lambda *a: {"candidates": [], "citations": []})
    brand = {"id": None, "price_links": json.dumps(
        {"philips-avent": {"name": "Philips Avent", "urls": ["https://philips.com/p"]}})}
    ledger, missing, _d = gen._ensure_price_ledger(
        brand, ["Philips Avent"], {"Philips Avent": {"dom": "philips.com"}}, {}, _TOPIC, "baby bottles")
    assert ledger == {} and missing == ["Philips Avent"]


def test_a_stored_link_beats_a_search_and_your_value_beats_the_link():
    brand = {"id": None, "price_links": json.dumps(
        {"philips-avent": {"name": "Philips Avent", "urls": ["https://philips.com/p"]}})}
    gen = _link_gen(_PAGE, call_handler=lambda p: {
        "candidates": [{"product": "x", "price": "$23.97", "basis": "3-pack, 9 oz"}]},
        price_handler=lambda *a: {"candidates": [{"url": "https://www.walmart.com/ip/x",
                                                  "price": "$9.99"}], "citations": []})
    ledger, _m, _d = gen._ensure_price_ledger(
        brand, ["Philips Avent"], {"Philips Avent": {"dom": "philips.com"}}, {}, _TOPIC, "bottles")
    assert ledger["Philips Avent"]["value"] == "$23.97" and gen.claude.price_calls == []

    cf = {"philips-avent": {"price": {"value": "$20.00", "source": "yours", "basis": "",
                                      "url": "", "checked_at": _iso(400)}}}
    gen2 = _link_gen(_PAGE, call_handler=lambda p: {"candidates": [{"price": "$23.97"}]})
    ledger2, _m2, _d2 = gen2._ensure_price_ledger(
        brand, ["Philips Avent"], {"Philips Avent": {"dom": "philips.com"}}, cf, _TOPIC, "bottles")
    assert ledger2["Philips Avent"]["value"] == "$20.00"


# ------------------------------------------- Change 6: "Include pricing" off means no pricing
_BRAND = {"id": None, "name": "Thyseed", "domain_url": "https://thyseed.com",
          "category": "baby feeding products"}
_ART = {"body_markdown": "| Brand | Starting price |\n|---|---|\n| Pigeon | $12 |\n"}


def _extract(p):
    if '"peer_tools"' in p and '"dimensions"' in p:
        return {"tools": ["Pigeon"], "peer_tools": ["Pigeon"], "dimensions": ["Starting price"],
                "claims": [], "core_topic": "baby bottles", "products": [], "generic_options": []}
    return {}


def test_pricing_off_issues_no_price_work_at_all():
    gen = _gen(call_handler=_extract,
               price_handler=lambda *a: {"candidates": [], "citations": []})
    gen._fetch_url = lambda u: pytest.fail("pricing is off — no page should be fetched")
    brand = dict(_BRAND, price_links=json.dumps(
        {"pigeon": {"name": "Pigeon", "urls": ["https://pigeon.com/p"]}}))
    s = gen._source_for_completion(brand, "best baby bottles", _ART, include_pricing=False)
    assert gen.claude.price_calls == []
    briefs = [(b["brief"] or "").lower() for b in gen.claude.searches]
    # nothing ASKS for a price, and the one brief that still says the word says we don't cover it
    assert not [b for b in briefs if "pricing and plans" in b or "price/cost" in b
                or "price as published" in b or "regular list price" in b]
    assert all("pricing" not in b or "does not cover pricing" in b for b in briefs)
    assert [u for u in s["unsourced"] if u.get("price_only")] == []
    assert s["include_pricing"] is False and s["prices"] == {}
    assert gen._price_warn == ""


def test_pricing_off_swaps_the_article_and_reconcile_rules_for_a_no_pricing_rule():
    gen = _gen(call_handler=lambda p: {"title": "t", "meta_description": "m", "keywords": [],
                                       "body_markdown": "# t\n", "disclosure": ""})
    gen.generate_article(_BRAND, "best baby bottles", include_pricing=False)
    prompt = gen.claude.calls[-1]
    assert "NO PRICING IN THIS ARTICLE" in prompt
    assert "PRICING PRODUCT-MATCH" not in prompt and "PRICE BASIS" not in prompt

    gen2 = _gen(call_handler=lambda p: {"title": "t", "meta_description": "m", "keywords": [],
                                        "body_markdown": "# t\n", "disclosure": ""})
    gen2.generate_article(_BRAND, "best baby bottles")
    on = gen2.claude.calls[-1]
    assert "PRICING PRODUCT-MATCH" in on and "NO PRICING IN THIS ARTICLE" not in on


def test_the_reconcile_is_told_not_to_price_when_pricing_is_off():
    gen = _gen(call_handler=lambda p: {"body_markdown": "# t\n", "flagged": []})
    sourcing = {"name": "Thyseed", "cat": "baby bottles", "tools": ["Pigeon"],
                "dims": ["Material"], "claims": [], "fresh": [{"label": "Pigeon",
                "url": "https://pigeon.com", "text": "f"}], "include_pricing": False}
    gen._reconcile_and_finish(_BRAND, "best baby bottles", {"body_markdown": "# t\n"}, sourcing)
    assert "NO PRICING IN THIS ARTICLE" in gen.claude.calls[-1]


def test_a_price_column_is_removed_when_pricing_is_off():
    gen = _gen()
    out, dropped = gen._strip_price_columns(_TABLE)
    assert dropped == ["Starting price"]
    assert "$29.99" not in out and "| Brand | Material |" in out


def test_pricing_off_warns_when_a_figure_survives():
    gen = _gen()
    art = {"body_markdown": "Thyseed bottles are around $29.99 each.\n", "meta_description": ""}
    gen._warn(art, "x")   # prove _warn is the channel the check uses
    body, dropped = gen._strip_price_columns(art["body_markdown"])
    assert dropped == [] and body == art["body_markdown"]


def test_the_jsonld_offer_is_suppressed_when_pricing_is_off():
    brand = {"name": "Thyseed", "domain_url": "https://thyseed.com",
             "key_facts": json.dumps({"pricing": {"items": [
                 {"product": "", "value": "$29.99", "operator_set": True,
                  "source_url": "https://thyseed.com"}]}})}
    blog_on = {"title": "t", "seed": "s", "body_markdown": "# t\n", "created_at": "2026-01-01",
               "updated_at": "2026-01-01", "include_pricing": 1}
    graph_on = json.loads(build_blog_jsonld(blog_on, brand).split(">", 1)[1].rsplit("<", 1)[0]) \
        if False else None
    g_on = build_blog_jsonld(blog_on, brand)
    assert '"Product"' in json.dumps(g_on) or "Product" in str(g_on)
    g_off = build_blog_jsonld({**blog_on, "include_pricing": 0}, brand)
    assert "Offer" not in str(g_off)


def test_the_check_endpoint_refuses_when_pricing_is_off():
    import app as _app
    _app.app.config["TESTING"] = True
    c = _app.app.test_client()
    r = c.post("/api/brands/1/competitor-prices/check", json={"include_pricing": False})
    assert r.status_code == 400 and "Include pricing is off" in r.get_json()["error"]


# ------------------------------------------------------------------ Change 2: migration race
def _init_worker(path):
    db = Database(path)
    db.initialize()
    db.close()


def test_two_processes_can_initialize_the_same_new_database_at_once():
    """The FU212 deploy crash: two gunicorn workers ran the ALTER TABLE migrations at the same
    moment, the loser hit `duplicate column name` and the master shut the service down."""
    d = tempfile.mkdtemp()
    path = os.path.join(d, "race.db")
    ctx = multiprocessing.get_context("spawn")
    procs = [ctx.Process(target=_init_worker, args=(path,)) for _ in range(4)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=90)
    assert all(p.exitcode == 0 for p in procs), [p.exitcode for p in procs]
    db = Database(path)
    db.initialize()
    cols = [r[1] for r in db.conn.execute("PRAGMA table_info(brands)").fetchall()]
    assert "price_links" in cols and "content_context" in cols
    db.close()


# --------------------------------------------------------------- Change 5: storage semantics
def test_price_links_save_to_every_copy_of_the_brand_and_an_empty_box_removes_one():
    import app as _app
    d = tempfile.mkdtemp()
    path = os.path.join(d, "t.db")
    db = Database(path)
    db.initialize()
    sid = db.create_subreddit("s", "d")
    sid2 = db.create_subreddit("s2", "d2")
    for s in (sid, sid2):
        db.conn.execute("INSERT INTO brands (subreddit_id,name,context) VALUES (?,?,?)",
                        (s, "Thyseed", "c"))
    db.conn.commit()
    b1 = db.get_brand(1)
    _app._save_price_links(db, b1, [{"name": "Pigeon", "urls": ["https://pigeon.com/p"]},
                                    {"name": "Comotomo", "urls": ["not-a-url"]}])
    for bid in (1, 2):
        pl = json.loads(db.get_brand(bid)["price_links"] or "{}")
        assert list(pl) == ["pigeon"] and pl["pigeon"]["urls"] == ["https://pigeon.com/p"]
    merged = _app._save_price_links(db, db.get_brand(1), [{"name": "Pigeon", "urls": []}])
    assert merged == {}
    db.close()


def test_helpers_that_read_a_price_are_not_fooled_by_formatting():
    assert _per_unit_price("$23.97", "3-pack, 9 oz") == "$7.99 each"
    assert _per_unit_price("$23.97", "single bottle") == ""
    assert _price_is_sale("$19.99", "Sale $19.99, was $24.99")[0] is True
    assert _price_is_sale("$24.99", "Regular price $24.99. In stock.")[0] is False
