"""FU161 — pricing RETRIEVAL: multi-product per-product own-site search (subject + competitors), keep
`_best_product_price` STRICT, fetch the matched page when the snippet lacks the price, drop affiliate
reviews, JSON-LD emits only the operator Offer on a no-match seed, and flag-and-ask when a competitor
price can't be confirmed. $0, no network (StubClaude)."""
import json

from generators.blog_gen import (BlogGenerator, build_blog_jsonld, _is_affiliate_review)
from tests.stubs import StubClaude


def _brand(kf="", **extra):
    b = {"name": "Acme", "domain_url": "https://acme.com", "category": "telehealth"}
    if kf:
        b["key_facts"] = kf
    b.update(extra)
    return b


def _source(brand, tools, products, dims=("pricing",), search_h=None):
    """Returns (gen, sourcing). The gen's StubClaude carries the handlers, so `gen.claude.searches`
    records what was asked and `gen._price_warn` is set by the run."""
    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": list(tools), "peer_tools": list(tools), "dimensions": list(dims),
                    "products": list(products), "claims": [], "core_topic": "glp-1"}
        if '"domains"' in p:
            return {"domains": {t: t.lower().replace(" ", "") + ".com" for t in tools}}
        return {}
    gen = BlogGenerator(StubClaude(call_handler=call_h, search_handler=(search_h or (lambda b, a, bl: []))),
                        db=None)
    body = "| Clinic | Pricing |\n|---|---|\n" + "".join(f"| {t} | ? |\n" for t in tools)
    return gen, gen._source_for_completion(brand, "best glp-1 clinics", {"body_markdown": body}, ymyl=None)


# --- Change 7a: JSON-LD emits ONLY the operator Offer on a no-product-match seed --------------------
def test_jsonld_no_match_seed_emits_only_operator_offer():
    kf = {"pricing": {"items": [
        {"product": "tirzepatide", "value": "Starts at $149/month then $249/month billed quarterly",
         "source_url": "getpetermd.com", "operator_set": True},
        {"product": "", "value": "as low as $79/month on the yearly plan",
         "source_url": "https://getpetermd.com/mens-trt/"}]}}   # stale auto-synced general item
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com", "key_facts": json.dumps(kf)}
    blog = {"title": "Which Telehealth Platforms Offer Doctor-Led GLP-1 Programs?",
            "seed": "which telehealth platforms offer doctor-led glp-1 programs",
            "meta_description": "d", "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-02T00:00:00",
            "body_markdown": "# T\n\n## Quick answer\nx\n"}
    graph = build_blog_jsonld(blog, brand)["@graph"]
    prod = next((n for n in graph if n.get("@type") == "Product"), None)
    assert prod is not None
    offers = prod["offers"] if isinstance(prod["offers"], list) else [prod["offers"]]
    prices = [o.get("price") for o in offers]
    assert "149" in prices and "79" not in prices                       # only the operator Offer
    assert not any("mens-trt" in (o.get("url") or "") for o in offers)  # the stale general item gone


# --- Change 6: _is_affiliate_review ---------------------------------------------------------------
def test_is_affiliate_review():
    assert _is_affiliate_review({"url": "https://manytreatments.com/x", "title": "Hims Review 2026 | $2-$499/mo"})
    assert _is_affiliate_review({"url": "https://nutritionnc.com/ro", "title": "Ro Weight Loss Review"})
    assert not _is_affiliate_review({"url": "https://health.usnews.com/x", "title": "Ro GLP-1 Review 2026"})   # reputable
    assert not _is_affiliate_review({"url": "https://www.forbes.com/health/x", "title": "Is Ro Worth It?"})     # reputable
    assert not _is_affiliate_review({"url": "https://ro.com/pricing", "title": "Ro Pricing"}, own_domain="ro.com")  # own site
    assert not _is_affiliate_review({"url": "https://ro.com/pricing", "title": "Ro Pricing — Plans"})            # not review-shaped


# --- Change 1: MULTI-PRODUCT competitor prices (both products selected, STRICT) -------------------
def test_competitor_multi_product_prices():
    def search_h(brief, allowed, blocked):
        if allowed and any("ro.com" in str(a).lower() for a in allowed):
            return [{"url": "https://ro.com/semaglutide", "fact": "semaglutide $199/mo", "title": "Ro semaglutide"},
                    {"url": "https://ro.com/tirzepatide", "fact": "tirzepatide $299/mo", "title": "Ro tirzepatide"}]
        return []
    _gen, s = _source(_brand(), ["Ro"], ["semaglutide", "tirzepatide"], search_h=search_h)
    txt = " ".join(b.get("text", "") for b in s["fresh"])
    assert "semaglutide" in txt and "199" in txt        # semaglutide price selected
    assert "tirzepatide" in txt and "299" in txt        # tirzepatide price ALSO selected (multi-product)


# --- Change 6: affiliate dropped + flag-and-ask when no confirmed price ---------------------------
def test_affiliate_dropped_and_flag_and_ask():
    def search_h(brief, allowed, blocked):
        # own-domain searches return nothing; a broad search returns only an affiliate review
        if allowed:
            return []
        return [{"url": "https://manytreatments.com/hims", "fact": "Hims $199/mo", "title": "Hims Review 2026"}]
    _gen, s = _source(_brand(), ["Hims"], ["semaglutide"], search_h=search_h)
    assert not any("manytreatments" in (b.get("url") or "") for b in s["fresh"])   # affiliate dropped
    assert any(u.get("tool") == "Hims" for u in s["unsourced"])                    # flagged to ask the operator


# --- Change 5: SUBJECT operator-first + honest fallback (no operator value → _price_warn) ---------
def test_subject_price_honest_fallback_warns():
    gen, s = _source(_brand(), ["Ro"], ["semaglutide"])         # subject own-site searches return [] (no price)
    assert getattr(gen, "_price_warn", "")                       # subject price could not be confirmed → warn
    assert "semaglutide" in gen._price_warn


def test_subject_operator_value_used_no_search():
    kf = {"pricing": {"items": [
        {"product": "semaglutide", "value": "$149/month intro, then $249/month billed quarterly",
         "operator_set": True}]}}
    gen, s = _source(_brand(json.dumps(kf)), ["Ro"], ["semaglutide"])
    # the operator value is injected; no subject own-domain price search fired
    assert any(str(b.get("label")).strip().lower() == "acme" and "249" in (b.get("text") or "")
               for b in s["fresh"])
    assert not any(x.get("allowed") == ["acme.com"] for x in gen.claude.searches)
    assert not getattr(gen, "_price_warn", "")                  # operator value present → no warning


# --- Change 7b: full canonical-price wording rule present -----------------------------------------
def test_full_canonical_price_rule_present():
    cap = {}

    def call_h(p):
        cap.setdefault("first", p)
        return {"title": "T", "meta_description": "m", "keywords": [], "body_markdown": "## Quick answer\nx"}
    gen = BlogGenerator(StubClaude(call_handler=call_h), db=None)
    gen.generate_article({"name": "PeterMD", "domain_url": "https://getpetermd.com"}, "best glp-1 clinics")
    assert "PRICE IN FULL" in cap["first"]
    assert "then $249/month billed quarterly" in cap["first"] or "intro" in cap["first"]
