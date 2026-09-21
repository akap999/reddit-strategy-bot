"""FU238 — a canonical price the operator removed, that would not go.

A PeterMD blog about semaglutide carried "$79/month" in its meta description, its JSON-LD
description and a Product offer whose URL was literally getpetermd.com/mens-trt/ — the 12-month TRT
plan. The real GLP-1 prices ($270, and $149 then $249 quarterly) were fetched correctly and cited in
the same article. Production held exactly one canonical pricing item:

    product=''  operator_set=None  url=https://getpetermd.com/mens-trt/
    value='PeterMD offers flexible plans—monthly, bi-yearly, and yearly—with pricing starting as
           low as $79/month on the yearly plan.'

Two bugs kept it alive, and the operator had already tried to delete it.

1. The self-heal that purges an auto-stored price from the wrong product's page compares the product
   LABEL to the URL path. A NAMELESS item has no label, so `if not prod: return False` waved it
   through permanently. The guard was blind to exactly the shape it was built for.
2. The Edit Brand save treated the textarea as authoritative only for OPERATOR-set items, while the
   textarea RENDERS every stored item. So the operator deleted the line, saved, and the row survived
   with nothing said either way.

Those em-dashes in `plans—monthly` are that same value, carried verbatim into the JSON-LD because an
operator canonical value is deliberately exempt from the symbol scrub.
"""
import json

import pytest

from generators.blog_gen import _GENERIC_PRICE_PATH_RE, build_blog_jsonld

STALE = {"product": "", "operator_set": None,
         "source_url": "https://getpetermd.com/mens-trt/",
         "value": "PeterMD offers flexible plans, monthly, bi-yearly and yearly, with pricing "
                  "starting as low as $79/month on the yearly plan."}


# ── 1. a nameless price on a product page is that product's price ────────────────────────────────
@pytest.mark.parametrize("path", ["", "pricing", "price", "plans", "membership", "how-it-works",
                                  "get-started", "subscriptions", "rates"])
def test_a_general_pricing_page_still_holds_a_general_price(path):
    assert _GENERIC_PRICE_PATH_RE.search(path), path


@pytest.mark.parametrize("path", ["mens-trt", "product/glp1m2m", "tirzepatide", "weight-loss",
                                  "shop/semaglutide", "services/keyword-backlinks"])
def test_a_product_page_does_not_hold_a_general_price(path):
    """Vertical-neutral: whatever x is, a price scraped from x's page is x's price."""
    assert not _GENERIC_PRICE_PATH_RE.search(path), path


def test_the_stale_nameless_item_is_now_purged(monkeypatch):
    from generators.blog_gen import BlogGenerator
    from tests.stubs import StubClaude
    g = BlogGenerator(StubClaude(), None)
    monkeypatch.setattr(g, "_persist_key_facts", lambda *a, **k: None)
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "key_facts": json.dumps({"pricing": {"items": [STALE]}})}
    kf, _w, _p, _b = g._resolve_and_sync_key_facts(brand, "", seed="semaglutide programs")
    items = ((kf.get("pricing") or {}).get("items")) or []
    assert not any((i.get("value") or "").find("$79") >= 0 for i in items), \
        "a nameless price scraped from /mens-trt/ must not survive as the general price"


def test_an_operators_own_general_price_is_never_purged(monkeypatch):
    """The purge is for AUTO-synced rows only. What the operator typed is theirs."""
    from generators.blog_gen import BlogGenerator
    from tests.stubs import StubClaude
    g = BlogGenerator(StubClaude(), None)
    monkeypatch.setattr(g, "_persist_key_facts", lambda *a, **k: None)
    mine = dict(STALE, operator_set=True)
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "key_facts": json.dumps({"pricing": {"items": [mine]}})}
    kf, _w, _p, _b = g._resolve_and_sync_key_facts(brand, "", seed="semaglutide programs")
    items = ((kf.get("pricing") or {}).get("items")) or []
    assert any("$79" in (i.get("value") or "") for i in items)


def test_a_bare_domain_price_is_still_kept(monkeypatch):
    """No path means nothing to judge — keep it rather than guess."""
    from generators.blog_gen import BlogGenerator
    from tests.stubs import StubClaude
    g = BlogGenerator(StubClaude(), None)
    monkeypatch.setattr(g, "_persist_key_facts", lambda *a, **k: None)
    it = dict(STALE, source_url="https://getpetermd.com/")
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "key_facts": json.dumps({"pricing": {"items": [it]}})}
    kf, _w, _p, _b = g._resolve_and_sync_key_facts(brand, "", seed="semaglutide programs")
    assert any("$79" in (i.get("value") or "")
               for i in ((kf.get("pricing") or {}).get("items")) or [])


def test_the_schema_offer_no_longer_advertises_it():
    """The end of the chain: the meta, the JSON-LD description and the Product offer all read the
    canonical price, which is why one row reached all three."""
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "key_facts": json.dumps({"pricing": {"items": [
                 {"product": "GLP-1", "operator_set": True, "value": "$270/month",
                  "source_url": "https://getpetermd.com/product/glp1m2m/"}, STALE]}})}
    blog = {"title": "Best Online Semaglutide Programs", "seed": "best online semaglutide programs",
            "body_markdown": "# x\n", "created_at": "2026-09-21", "updated_at": "2026-09-21"}
    out = build_blog_jsonld(blog, brand)
    graph = (out if isinstance(out, dict) else json.loads(out))["@graph"]
    offers = [n for n in graph if n.get("@type") == "Product"]
    if offers:
        blob = json.dumps(offers)
        assert "mens-trt" not in blob and '"79"' not in blob


# ── 2. removing a line removes it ────────────────────────────────────────────────────────────────
def _save(client_items, stored_items):
    """The authoritative-textarea rule, exactly as api_update_brand applies it."""
    from generators.blog_gen import _drop_nameless_when_named, _kf_slug
    by = {_kf_slug(i.get("product")): i for i in stored_items}
    incoming = set()
    for it in client_items:
        if not str(it.get("value") or "").strip():
            continue
        incoming.add(_kf_slug(it.get("product")))
        by[_kf_slug(it.get("product"))] = dict(it, operator_set=True)
    removed = [it for s, it in by.items() if s not in incoming]
    by = {s: it for s, it in by.items() if s in incoming}
    final, dropped = _drop_nameless_when_named(list(by.values()))
    return final, removed, dropped


def test_clearing_the_box_removes_an_auto_synced_row():
    """The reported failure: the operator emptied the box before the generation and the row stayed."""
    final, removed, _ = _save([], [STALE])
    assert final == []
    assert removed and removed[0] is STALE


def test_clearing_the_box_removes_an_operator_row_too():
    final, removed, _ = _save([], [dict(STALE, operator_set=True)])
    assert final == [] and len(removed) == 1


def test_typing_the_real_prices_replaces_the_stale_one():
    final, _removed, _ = _save(
        [{"product": "GLP-1", "value": "$270/month subscription"},
         {"product": "tirzepatide", "value": "$149 to start, then $249/month billed quarterly"}],
        [STALE])
    assert {i["product"] for i in final} == {"GLP-1", "tirzepatide"}
    assert not any("$79" in (i.get("value") or "") for i in final)


def test_a_line_left_in_the_box_survives():
    final, removed, _ = _save([{"product": "GLP-1", "value": "$270/month"}],
                              [{"product": "GLP-1", "operator_set": True, "value": "$270/month"}])
    assert len(final) == 1 and not removed


def test_the_operator_is_told_what_was_removed():
    src = open("app.py", encoding="utf-8").read()
    assert "Removed from canonical pricing: " in src
    assert '"pricing_note"' in src
    ui = open("templates/index.html", encoding="utf-8").read()
    assert "_pricingNote" in ui and "r.pricing_note" in ui


# ── 4. the loop that re-created it: reject the mislabeled price AT THE SOURCE ─────────────────────
def _sync_with_search(monkeypatch, results, stored=None):
    """Drive the real sync with first-party evidence present, so the Case-2 own-domain price search
    runs and its result is offered for storage."""
    from generators.blog_gen import BlogGenerator
    from tests.stubs import StubClaude
    saved = {}
    claude = StubClaude(call_handler=lambda p: {"items": [], "seed_product": ""},
                        search_handler=lambda brief, allowed, blocked: results)
    g = BlogGenerator(claude, None)
    monkeypatch.setattr(g, "_persist_key_facts", lambda b, kf: saved.update(kf))
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "key_facts": json.dumps({"pricing": {"items": list(stored or [])}})}
    kf, _w, _p, _b = g._resolve_and_sync_key_facts(
        brand, "PeterMD offers plans.", seed="best online semaglutide programs")
    return ((kf.get("pricing") or {}).get("items")) or []


def test_a_general_price_scraped_from_a_product_page_is_never_stored(monkeypatch):
    """The actual loop. Production's row carried verified_at from the SAME DAY as the article, so the
    sync had re-scraped it: the operator's deletion was undone by the next generation. The stored-item
    purge runs BEFORE the merge, so without this the row is written now and only self-heals on the
    NEXT run — one published article too late."""
    items = _sync_with_search(monkeypatch, [
        {"title": "TRT plans", "url": "https://getpetermd.com/mens-trt/",
         "fact": "Plans start as low as $79/month on the yearly plan."}])
    assert items == [], "a general price does not live on the TRT product page"


def test_a_general_price_from_a_real_pricing_page_is_still_stored(monkeypatch):
    """The other half: the feature must not starve a brand that does publish one general price."""
    items = _sync_with_search(monkeypatch, [
        {"title": "Pricing", "url": "https://getpetermd.com/pricing/",
         "fact": "Membership is $29/month."}])
    assert len(items) == 1 and "$29" in items[0]["value"]


def test_a_named_price_is_judged_by_the_product_match_not_the_path(monkeypatch):
    """A named product's price is matched on the path, title OR fact by _best_product_price, so the
    path alone must not veto it — /product/glp1m2m/ really is where the GLP-1 price lives."""
    from generators.blog_gen import BlogGenerator
    from tests.stubs import StubClaude
    claude = StubClaude(
        call_handler=lambda p: {"items": [], "seed_product": "GLP-1"},
        search_handler=lambda brief, allowed, blocked: [
            {"title": "GLP-1 monthly", "url": "https://getpetermd.com/product/glp1m2m/",
             "fact": "GLP-1 monthly plan is $270/month."}])
    g = BlogGenerator(claude, None)
    monkeypatch.setattr(g, "_persist_key_facts", lambda *a, **k: None)
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "key_facts": json.dumps({"pricing": {"items": []}})}
    kf, _w, _p, _b = g._resolve_and_sync_key_facts(
        brand, "PeterMD offers plans.", seed="best online GLP-1 programs")
    items = ((kf.get("pricing") or {}).get("items")) or []
    assert any("$270" in (i.get("value") or "") for i in items), \
        "a product-matched named price must survive even though its path is a product slug"
