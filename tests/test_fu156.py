"""FU156 — blog PRICING: find the PRODUCT's price, never a DIFFERENT product's price (PeterMD TRT
$79 on a tirzepatide page) or a program/membership fee — subject AND competitors, cost-neutral.
$0, no network (StubClaude)."""
import json

from generators.blog_gen import BlogGenerator, _best_product_price, build_blog_jsonld
from tests.stubs import StubClaude


TRT = {"url": "https://getpetermd.com/mens-trt/", "fact": "TRT plans from $79/mo on the yearly plan"}
TIRZ = {"url": "https://getpetermd.com/product/tirzepatide/", "fact": "Tirzepatide $357 / 3 months paid in full"}


# --- Change 0: the pure selection helper (subject AND competitor share it) -----------------------
def test_best_product_price_picks_the_product_page_not_another_product():
    # BOTH a TRT page and the tirzepatide page on the own domain → the tirzepatide (path-match) one.
    got = _best_product_price([TRT, TIRZ], "tirzepatide", "getpetermd.com")
    assert got and "tirzepatide" in got["url"] and "357" in got["fact"]


def test_best_product_price_rejects_wrong_product():
    # ONLY the TRT page → None (never substitute a different product's price).
    assert _best_product_price([TRT], "tirzepatide", "getpetermd.com") is None


def test_best_product_price_text_match_when_no_path_match():
    # product named in the fact but not the URL path → still accepted (score 1).
    generic = {"url": "https://getpetermd.com/pricing/", "fact": "Tirzepatide is $357 per 3 months"}
    got = _best_product_price([generic], "tirzepatide", "getpetermd.com")
    assert got and got["url"].endswith("/pricing/")


def test_best_product_price_general_brand_takes_any_own_domain_price():
    got = _best_product_price([TRT], "", "getpetermd.com")   # product="" → any own-domain price
    assert got and got["url"].endswith("/mens-trt/")


def test_best_product_price_ignores_offsite():
    off = {"url": "https://reviews.example.com/x", "fact": "Tirzepatide $99/mo"}
    assert _best_product_price([off], "tirzepatide", "getpetermd.com") is None


# --- Change 1: subject resolver stores the product's price, never the wrong one ------------------
def _brand(key_facts=""):
    return {"name": "PeterMD", "domain_url": "https://getpetermd.com", "key_facts": key_facts}


def _resolver_stub(items, seed_product, search_results):
    def call_h(p):
        if '"seed_product"' in p or '"items"' in p:   # the extraction call
            return {"items": items, "seed_product": seed_product}
        return {}
    def search_h(brief, allowed, blocked):
        return list(search_results)
    return StubClaude(call_handler=call_h, search_handler=search_h)


def _pricing_items(kf):
    return ((kf or {}).get("pricing") or {}).get("items") or []


def test_subject_stores_product_price_not_trt():
    stub = _resolver_stub([], "tirzepatide", [TRT, TIRZ])
    gen = BlogGenerator(stub, db=None)
    kf, warn, seeds, blocks = gen._resolve_and_sync_key_facts(_brand(), "some first-party evidence", "tirzepatide clinics")
    items = _pricing_items(kf)
    tz = [i for i in items if i.get("product") == "tirzepatide"]
    assert tz and "357" in tz[0]["value"] and "tirzepatide" in tz[0]["source_url"]
    assert not any("mens-trt" in (i.get("source_url") or "") for i in items)   # never the TRT price


def test_subject_stores_nothing_and_retries_when_only_wrong_product():
    stub = _resolver_stub([], "tirzepatide", [TRT])   # only the TRT page ever comes back
    gen = BlogGenerator(stub, db=None)
    kf, warn, seeds, blocks = gen._resolve_and_sync_key_facts(_brand(), "some first-party evidence", "tirzepatide clinics")
    assert not any(i.get("product") == "tirzepatide" for i in _pricing_items(kf))   # no substitution
    assert len(stub.searches) == 2   # the ONE retry fired (product query + reformulated), then gave up


def test_subject_merge_purges_mislabeled_and_keeps_operator_set():
    bad = {"pricing": {"items": [
        {"product": "tirzepatide", "value": "$79/mo", "source_url": "https://getpetermd.com/mens-trt/"},
        {"product": "semaglutide", "value": "$249/mo", "source_url": "https://getpetermd.com/product/semaglutide/",
         "operator_set": True}]}}
    stub = _resolver_stub([], "", [])   # general target, search finds nothing → pure self-heal
    gen = BlogGenerator(stub, db=None)
    kf, warn, seeds, blocks = gen._resolve_and_sync_key_facts(_brand(json.dumps(bad)), "evidence", "seed")
    items = _pricing_items(kf)
    assert not any("mens-trt" in (i.get("source_url") or "") for i in items)   # mislabeled purged
    assert any(i.get("product") == "semaglutide" for i in items)               # operator_set kept


# --- Change 3: competitor pricing query is product-anchored --------------------------------------
def test_competitor_pricing_query_is_product_anchored():
    searches = []
    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": ["Noom"], "peer_tools": ["Noom"], "dimensions": ["pricing"],
                    "claims": [], "core_topic": "tirzepatide clinics", "products": ["tirzepatide"]}
        if '"domains"' in p:
            return {"domains": {"Noom": "noom.com"}}
        return {}
    def search_h(brief, allowed, blocked):
        searches.append(brief)
        return []
    gen = BlogGenerator(StubClaude(call_handler=call_h, search_handler=search_h), db=None)
    gen._source_for_completion(
        {"id": 1, "name": "Acme", "domain_url": "https://acme.com", "category": "telehealth"},
        "which clinics prescribe tirzepatide", {"body_markdown": "| Tool | Pricing |\n|---|---|\n| Noom | ? |\n"},
        ymyl=None)
    assert any("tirzepatide" in b.lower() and "price" in b.lower() for b in searches)   # product-anchored


# --- Change 4: prompt rules ----------------------------------------------------------------------
def test_pricing_product_match_rule_in_prompts():
    cap = {}
    def call_h(p):
        cap.setdefault("first", p)
        return {"title": "T", "meta_description": "m", "keywords": [], "body_markdown": "## Quick answer\nx"}
    gen = BlogGenerator(StubClaude(call_handler=call_h), db=None)
    gen.generate_article({"name": "PeterMD", "domain_url": "https://getpetermd.com"}, "best tirzepatide clinics")
    assert "PRICING PRODUCT-MATCH" in cap["first"]
    # verify_claims carries the scrutiny line too
    cap2 = {}
    def call_h2(p):
        cap2.setdefault("first", p)
        return {"revised_body_markdown": "x", "flagged": []}
    gen2 = BlogGenerator(StubClaude(call_handler=call_h2), db=None)
    gen2.verify_claims({"name": "PeterMD"}, {"body_markdown": "## Quick answer\nAcme $9 [S1]."})
    assert "PRICING PRODUCT-MATCH" in cap2["first"]


# --- Change 5: JSON-LD Offer consistency guard ---------------------------------------------------
def _blog():
    return {"title": "Tirzepatide clinics", "seed": "tirzepatide clinics", "meta_description": "d",
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-02T00:00:00",
            "body_markdown": "# Tirzepatide clinics\n\n## Quick answer\nText.\n"}


def _product_node(graph):
    return next((n for n in graph if n.get("@type") == "Product"), None)


def test_jsonld_skips_mislabeled_offer():
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "key_facts": json.dumps({"pricing": {"items": [
                 {"product": "tirzepatide", "value": "$79/mo", "source_url": "https://getpetermd.com/mens-trt/"}]}})}
    graph = build_blog_jsonld(_blog(), brand)["@graph"]
    assert _product_node(graph) is None   # mislabeled (product≠url path) → no Product/Offer


def test_jsonld_emits_consistent_offer():
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "key_facts": json.dumps({"pricing": {"items": [
                 {"product": "tirzepatide", "value": "$357 / 3 months",
                  "source_url": "https://getpetermd.com/product/tirzepatide/"}]}})}
    prod = _product_node(build_blog_jsonld(_blog(), brand)["@graph"])
    assert prod is not None
    off = prod["offers"] if isinstance(prod["offers"], dict) else prod["offers"][0]
    assert off.get("price") == "357"
