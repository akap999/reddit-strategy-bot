"""FU158 — pricing accuracy end-to-end: an operator-set canonical price is authoritative in the
comparison-table cell AND the JSON-LD Offer (not just meta/prose); competitor prices come from the
vendor's OWN site; every price carries its (generic, only-when-present) unit/quantity/term basis.
$0, no network (StubClaude)."""
import json

from generators.blog_gen import (BlogGenerator, build_blog_jsonld,
                                  _canonical_price_item, _canonical_facts_block)
from tests.stubs import StubClaude


OP_ITEM = {"product": "tirzepatide", "value": "$647 / 3 months",
           "source_url": "getpetermd.com", "operator_set": True}   # bare-domain source_url (Edit Brand)
GEN_79 = {"product": "", "value": "flexible plans, as low as $79/month on the yearly plan",
          "source_url": "https://getpetermd.com/mens-trt/"}         # stale general/TRT item


def _brand(kf):
    return {"name": "PeterMD", "domain_url": "https://getpetermd.com", "key_facts": json.dumps(kf)}


def _blog():
    return {"title": "Which Online Clinics Prescribe Tirzepatide in the US?",
            "seed": "which online clinics prescribe tirzepatide in the us",
            "meta_description": "d", "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-02T00:00:00",
            "body_markdown": "# T\n\n## Quick answer\nText.\n"}


def _product_node(graph):
    return next((n for n in graph if n.get("@type") == "Product"), None)


def _offer(prod):
    return prod["offers"] if isinstance(prod["offers"], dict) else prod["offers"][0]


# --- the shared selector -------------------------------------------------------------------------
def test_canonical_price_item_prefers_operator_product_match():
    it = _canonical_price_item({"pricing": {"items": [GEN_79, OP_ITEM]}}, "tirzepatide")
    assert it is not None and it.get("operator_set") and "647" in it["value"]


def test_canonical_price_item_no_match_specific_product_returns_none():
    # only a general $79 item, but the article product is tirzepatide → never substitute it.
    assert _canonical_price_item({"pricing": {"items": [GEN_79]}}, "tirzepatide") is None


def test_canonical_price_item_general_falls_back_when_product_empty():
    it = _canonical_price_item({"pricing": {"items": [GEN_79]}}, "")
    assert it is not None and it.get("product") == "" and "79" in it["value"]


# --- Change 3: build_blog_jsonld Offer -----------------------------------------------------------
def test_jsonld_offer_uses_operator_price_not_stale_general():
    graph = build_blog_jsonld(_blog(), _brand({"pricing": {"items": [OP_ITEM, GEN_79]}}))["@graph"]
    prod = _product_node(graph)
    assert prod is not None
    off = _offer(prod)
    assert off.get("price") == "647"                       # operator tirzepatide, NOT the $79 TRT item
    assert "647" in off["description"]
    assert "mens-trt" not in (off.get("url") or "")        # the stale general item never wins
    assert (off.get("url") or "").startswith("https://")   # bare-domain source_url normalized to https


def test_jsonld_operator_item_not_skipped_by_guard():
    # operator item with a bare-domain source_url (no product path) must NOT be guard-skipped.
    prod = _product_node(build_blog_jsonld(_blog(), _brand({"pricing": {"items": [OP_ITEM]}}))["@graph"])
    assert prod is not None and _offer(prod).get("price") == "647"


def test_jsonld_still_skips_mislabeled_nonoperator():
    # regression (FU156): a NON-operator named item whose token isn't in its url PATH is still skipped.
    bad = {"product": "tirzepatide", "value": "$79/mo", "source_url": "https://getpetermd.com/mens-trt/"}
    graph = build_blog_jsonld(_blog(), _brand({"pricing": {"items": [bad]}}))["@graph"]
    assert _product_node(graph) is None


def test_jsonld_consistent_offer_nonoperator_pathmatch():
    good = {"product": "tirzepatide", "value": "$357 / 3 months",
            "source_url": "https://getpetermd.com/product/tirzepatide/"}
    prod = _product_node(build_blog_jsonld(_blog(), _brand({"pricing": {"items": [good]}}))["@graph"])
    assert prod is not None and _offer(prod).get("price") == "357"


# --- Change 1: subject canonical price injected + no subject pricing re-search --------------------
def test_subject_canonical_injected_and_no_pricing_research():
    kf = {"pricing": {"items": [OP_ITEM]}}

    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": ["Noom"], "peer_tools": ["Noom"], "dimensions": ["pricing"],
                    "products": ["tirzepatide"], "claims": [], "core_topic": "tirzepatide clinics"}
        return {}

    stub = StubClaude(call_handler=call_h, search_handler=lambda b, a, bl: [])
    gen = BlogGenerator(stub, db=None)
    sourcing = gen._source_for_completion(
        _brand(kf), "which online clinics prescribe tirzepatide in the us",
        {"body_markdown": "| Clinic | Pricing |\n|---|---|\n| Noom | ? |\n"}, ymyl=None)
    assert sourcing is not None
    # the operator $647 is injected as a SUBJECT first-party fresh block (the reconcile fills the cell from it)
    assert any(str(b.get("label")).strip().lower() == "petermd" and "647" in (b.get("text") or "")
               for b in sourcing["fresh"])
    # and NO subject own-domain pricing search fired (getpetermd.com-pinned)
    assert not any(s.get("allowed") == ["getpetermd.com"] for s in stub.searches)


# --- Changes 2/4/5: prompt rules -----------------------------------------------------------------
def test_canonical_facts_block_marks_operator_and_authoritative():
    blk = _canonical_facts_block("PeterMD", {"pricing": {"items": [OP_ITEM]}}, ["tirzepatide"])
    assert "operator-set" in blk
    assert "authoritative" in blk.lower()
    assert "general plan / consult / membership" in blk.lower()


def test_generate_article_pricing_rules_present_and_generic():
    cap = {}

    def call_h(p):
        cap.setdefault("first", p)
        return {"title": "T", "meta_description": "m", "keywords": [], "body_markdown": "## Quick answer\nx"}

    gen = BlogGenerator(StubClaude(call_handler=call_h), db=None)
    gen.generate_article({"name": "PeterMD", "domain_url": "https://getpetermd.com"},
                         "best tirzepatide clinics")
    p = cap["first"]
    assert "CANONICAL PRICE IS AUTHORITATIVE" in p
    assert "COMPETITOR PRICE = the vendor's OWN site" in p
    assert "PRICE BASIS" in p
    # GENERIC — not a medical-only "dose" requirement
    assert "per seat" in p and "dose/size" in p


def test_reconcile_pricing_rules_present():
    cap = {}

    def call_h(p):
        cap.setdefault("first", p)
        return {"revised_body_markdown": "x", "flagged": []}

    gen = BlogGenerator(StubClaude(call_handler=call_h), db=None)
    gen._evidence_blocks = []
    sourcing = {"name": "PeterMD", "cat": "", "tools": ["Noom"], "dims": ["pricing"], "claims": [],
                "core_topic": "", "fresh": [{"label": "PeterMD", "url": "https://getpetermd.com",
                                             "text": "x"}], "unsourced": [], "peers": ["Noom"],
                "geo": "", "qualifier": "", "ymyl": ""}
    gen._reconcile_and_finish({"name": "PeterMD", "domain_url": "https://getpetermd.com"}, "seed",
                              {"body_markdown": "## Quick answer\nAcme $9 [S1]."}, sourcing)
    p = cap["first"]
    assert "CANONICAL PRICE IS AUTHORITATIVE" in p
    assert "COMPETITOR PRICE = the vendor's OWN site" in p
    assert "PRICE BASIS" in p


def test_verify_claims_pricing_scrutiny_present():
    cap = {}

    def call_h(p):
        cap.setdefault("first", p)
        return {"revised_body_markdown": "x", "flagged": []}

    gen = BlogGenerator(StubClaude(call_handler=call_h), db=None)
    gen.verify_claims({"name": "PeterMD"}, {"body_markdown": "## Quick answer\nAcme $9 [S1]."})
    p = cap["first"]
    assert "COMPETITOR PRICE FROM A REVIEW SOURCE" in p
    assert "PRICE MISSING ITS BASIS" in p
