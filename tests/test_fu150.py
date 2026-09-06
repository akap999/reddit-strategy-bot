"""FU150 — competitor-retrieval two-pass + first-party-only brand info + never-cite-negative +
PER-PRODUCT canonical key_facts sync + vertical-neutral generality ($0, no network).

Covers:
  1. _is_negative_about / _canonical_facts_block / _kf_pricing_items / _LICENSE_DIM_RE (pure helpers).
  2. generate_article prompt: first-party-only-{name} + never-cite-negative-{name} rules, the
     PREFER-INDEPENDENT rule scoped to competitor/topic, and the per-product CANONICAL block.
  3. _source_for_completion two-pass: batched domain resolve replaces per-tool find_official_domain,
     every baselined competitor is sourced, a genuinely-unsourceable tool lands in `unsourced` with
     facts == the missing dimensions; corroboration off-subject; the fetch_brief is vertical-neutral.
  4. _gather_independent_sources excludes the SUBJECT.
  5. _resolve_and_sync_key_facts (PER PRODUCT): seed a new product / conflict per product (warn+persist)
     / bot-walled reuse / never-adopt-third-party (prompt) / migrate old single-value shape.
  6. brands.key_facts migrates + round-trips.
"""
import json

from generators.blog_gen import (BlogGenerator, _is_negative_about, _canonical_facts_block,
                                  _kf_pricing_items, _LICENSE_DIM_RE)
from tests.stubs import StubClaude


BRAND = {"id": 1, "name": "Acme", "domain_url": "https://acme.com", "category": "AI music generator",
         "competitors": '["CompA", "CompB", "CompC"]'}
SEED = "best AI music generators for creators"
ARTICLE = {"body_markdown": "## Quick answer\nAcme, CompA, CompB and CompC are the options.\n\n"
                            "| Tool | Pricing |\n|---|---|\n| Acme | $9 |\n| CompA | ? |\n"}


def _gen(call_handler=None, search_handler=None, site_facts=None, official_domain=None, db=None):
    return BlogGenerator(StubClaude(call_handler=call_handler, search_handler=search_handler,
                                    site_facts=site_facts, official_domain=official_domain), db=db)


# --- 1. helpers -------------------------------------------------------------------------------

def test_is_negative_about():
    assert _is_negative_about({"title": "Acme complaints and refund problems", "fact": ""}, "Acme")
    assert _is_negative_about({"title": "Is Acme a scam? lawsuit filed", "fact": ""}, "Acme")
    assert _is_negative_about({"title": "why you should stay away from Acme", "fact": ""}, "Acme")
    assert not _is_negative_about({"title": "CompA pricing and plans review", "fact": "Pro $20/mo"}, "Acme")
    assert not _is_negative_about({"title": "Acme pricing and features", "fact": "Pro plan $9/mo"}, "Acme")
    assert not _is_negative_about({"title": "Widgetco lawsuit and complaints", "fact": ""}, "Acme")
    assert not _is_negative_about({"title": "x", "fact": "y"}, "")


def test_kf_pricing_items_and_block():
    # per-product items
    kf = {"pricing": {"items": [
        {"product": "tirzepatide", "value": "$149 first month, then $249/mo billed quarterly (60mg)"},
        {"product": "semaglutide", "value": "$199/mo"}]}}
    items = _kf_pricing_items(kf)
    assert [i["product"] for i in items] == ["tirzepatide", "semaglutide"]
    blk = _canonical_facts_block("Acme", kf, seed_products=["tirzepatide"])
    assert "CANONICAL Acme FACTS" in blk
    # complex billing preserved VERBATIM, product named, and the seed product listed FIRST
    assert "pricing (tirzepatide): $149 first month, then $249/mo billed quarterly (60mg)" in blk
    assert blk.index("tirzepatide") < blk.index("semaglutide")   # seed product first
    # back-compat: the OLD single {value} shape migrates to one general item
    old = {"pricing": {"value": "$9/mo"}}
    assert _kf_pricing_items(old) == [{"product": "", "value": "$9/mo", "source_url": "", "verified_at": ""}]
    assert "pricing: $9/mo" in _canonical_facts_block("Acme", old)
    # empty -> inert
    assert _canonical_facts_block("Acme", {}) == ""
    assert _canonical_facts_block("Acme", None) == ""


def test_license_dim_regex():
    assert _LICENSE_DIM_RE.search("commercial license")
    assert _LICENSE_DIM_RE.search("royalty-free")
    assert _LICENSE_DIM_RE.search("usage rights")
    assert not _LICENSE_DIM_RE.search("delivery time")
    assert not _LICENSE_DIM_RE.search("pricing")


# --- 2. writer prompt: #2, #3, canonical injection --------------------------------------------

def _article_prompt(gen):
    return next(p for p in gen.claude.calls if "EVIDENCE RULE" in p)


def test_generate_article_first_party_and_negative_rules():
    gen = _gen(call_handler=lambda p: {"title": SEED, "meta_description": "m", "keywords": [],
                                       "body_markdown": "## Quick answer\nx", "disclosure": "d"})
    gen.generate_article(BRAND, SEED)
    flat = " ".join(_article_prompt(gen).split())
    assert "OWN FACTS ARE FIRST-PARTY ONLY" in flat
    assert "NEVER cite a third-party / independent / review / analyst source for a fact ABOUT Acme" in flat
    assert "NEVER CITE ANYTHING NEGATIVE ABOUT Acme" in flat
    assert "PREFER INDEPENDENT SOURCES — for COMPETITOR and TOPIC facts ONLY, never for Acme" in flat
    assert "CANONICAL Acme FACTS" not in flat


def test_generate_article_injects_per_product_canonical():
    gen = _gen(call_handler=lambda p: {"title": SEED, "meta_description": "m", "keywords": [],
                                       "body_markdown": "## Quick answer\nx", "disclosure": "d"})
    kf = {"pricing": {"items": [{"product": "Pro", "value": "$19/mo"},
                                {"product": "Team", "value": "$49/mo"}]}}
    gen.generate_article(BRAND, SEED, key_facts=kf, key_facts_products=["Pro"])
    flat = " ".join(_article_prompt(gen).split())
    assert "CANONICAL Acme FACTS" in flat
    assert "pricing (Pro): $19/mo" in flat and "pricing (Team): $49/mo" in flat


# --- 3. two-pass competitor sourcing ----------------------------------------------------------

def _claim_extract(prompt):
    if '"peer_tools"' in prompt and '"dimensions"' in prompt:
        return {"tools": ["CompA", "CompB", "CompC"], "peer_tools": ["CompA", "CompB", "CompC"],
                "dimensions": ["pricing", "commercial license", "royalty-free"],
                "claims": [], "core_topic": "", "products": []}
    if '"domains"' in prompt:
        return {"domains": {"CompA": "compa.com", "CompB": "compb.com", "CompC": "compc.com"}}
    return {}


def _vendor_for_pinned(brief, allowed, blocked):
    if allowed and len(allowed) == 1 and "/" not in allowed[0]:
        dom = allowed[0]
        return [{"url": f"https://{dom}/pricing",
                 "fact": "Pro plan $20/month, commercial use allowed, output is royalty-free",
                 "title": f"{dom} pricing"}]
    return []


def test_two_pass_batched_resolve_no_starvation():
    gen = _gen(call_handler=_claim_extract, search_handler=_vendor_for_pinned)
    sourcing = gen._source_for_completion(BRAND, SEED, ARTICLE, ymyl=None)
    assert sourcing is not None
    assert sourcing["unsourced"] == []
    labels = {b["label"] for b in sourcing["fresh"]}
    assert {"CompA", "CompB", "CompC"} <= labels
    assert gen.claude.domain_calls == []   # batched resolver replaced per-tool find_official_domain


def test_genuinely_unsourceable_tool_pauses_with_dims():
    def handler(prompt):
        if '"domains"' in prompt:
            return {"domains": {"CompA": "compa.com", "CompB": "compb.com"}}   # C omitted
        return _claim_extract(prompt)
    gen = _gen(call_handler=handler, search_handler=_vendor_for_pinned, official_domain=lambda b, c: "")
    sourcing = gen._source_for_completion(BRAND, SEED, ARTICLE, ymyl=None)
    unsourced_tools = {u["tool"] for u in sourcing["unsourced"]}
    assert unsourced_tools == {"CompC"}
    rec = next(u for u in sourcing["unsourced"] if u["tool"] == "CompC")
    assert rec["facts"] == ["pricing", "commercial license", "royalty-free"]
    labels = {b["label"] for b in sourcing["fresh"]}
    assert "CompA" in labels and "CompB" in labels


def test_corroboration_drops_subject_and_negative():
    gen = _gen(call_handler=_claim_extract, search_handler=_vendor_for_pinned)
    gen._source_for_completion(BRAND, SEED, ARTICLE, ymyl=None)
    corr = next((s for s in gen.claude.searches
                 if "reputable INDEPENDENT sources" in (s["brief"] or "")), None)
    assert corr is not None
    assert "claims about Acme" not in corr["brief"]
    assert "compared tools" in corr["brief"]
    assert "do NOT return pages that criticize Acme" in corr["brief"]


def test_fetch_brief_is_vertical_neutral():
    # Tier2 (fetch_site_facts) fires when Tier1 returns no vendor block -> capture the fetch_brief.
    gen = _gen(call_handler=_claim_extract, search_handler=lambda b, a, bl: [],
               site_facts=lambda dom, brand, brief: "some facts")
    gen._source_for_completion(BRAND, SEED, ARTICLE, ymyl=None)
    briefs = " ".join(c["brief"] for c in gen.claude.site_fact_calls)
    assert briefs, "Tier2 fetch_site_facts should have been called"
    # NO music/creative-specific hardcoding; anchored on the brand category
    for banned in ("artists' voices", "generated output", "video / all-in-one", "clones real"):
        assert banned not in briefs
    assert "AI music generator" in briefs   # category-anchored (BRAND['category'])


# --- 4. independent sweep excludes the subject ------------------------------------------------

def test_independent_sweep_excludes_subject():
    gen = _gen(search_handler=lambda brief, allowed, blocked: [])
    gen._gather_independent_sources("Acme", ["CompA", "CompB"], SEED, "AI music", ["acme.com"])
    briefs = [s["brief"] for s in gen.claude.searches]
    assert not any('about "Acme"' in b for b in briefs)
    assert any('DIRECT COMPETITORS of "Acme"' in b for b in briefs)


# --- 5. canonical key_facts sync (PER PRODUCT) ------------------------------------------------

class _FakeDB:
    def __init__(self):
        self.updates = []

    def update_brand(self, brand_id, **kw):
        self.updates.append((brand_id, kw))


def _stored_items(db):
    return json.loads(db.updates[-1][1]["key_facts"])["pricing"]["items"]


def test_key_facts_seed_new_product():
    db = _FakeDB()
    gen = _gen(call_handler=lambda p: {"items": [{"product": "tirzepatide", "value": "$149/mo"}],
                                       "seed_product": "tirzepatide"}, db=db)
    brand = dict(BRAND)
    kf, warn, seed_products, extra = gen._resolve_and_sync_key_facts(
        brand, evidence="Acme own site tirzepatide $149/mo acme.com", seed="tirzepatide clinics")
    assert warn == ""                                   # seeding a new product is not a conflict
    assert seed_products == ["tirzepatide"]
    items = _stored_items(db)
    assert any(i["product"] == "tirzepatide" and i["value"] == "$149/mo" for i in items)


def test_key_facts_per_product_conflict_only_touches_that_product():
    db = _FakeDB()
    gen = _gen(call_handler=lambda p: {"items": [{"product": "tirzepatide", "value": "$179/mo"}],
                                       "seed_product": "tirzepatide"}, db=db)
    brand = dict(BRAND, key_facts=json.dumps({"pricing": {"items": [
        {"product": "tirzepatide", "value": "$149/mo"},
        {"product": "semaglutide", "value": "$199/mo"}]}}))
    kf, warn, seed_products, extra = gen._resolve_and_sync_key_facts(
        brand, evidence="Acme own site tirzepatide $179/mo acme.com", seed="tirzepatide")
    assert "tirzepatide changed" in warn and "$149/mo" in warn and "$179/mo" in warn
    items = {i["product"]: i for i in _stored_items(db)}
    assert items["tirzepatide"]["value"] == "$179/mo"
    assert items["tirzepatide"]["previous"] == "$149/mo"
    assert items["semaglutide"]["value"] == "$199/mo"   # OTHER product untouched


def test_key_facts_botwalled_reuses_stored_no_warning():
    db = _FakeDB()
    gen = _gen(call_handler=lambda p: {"items": [], "seed_product": "tirzepatide"}, db=db)
    brand = dict(BRAND, key_facts=json.dumps({"pricing": {"items": [
        {"product": "tirzepatide", "value": "$149/mo"}]}}))
    kf, warn, seed_products, extra = gen._resolve_and_sync_key_facts(
        brand, evidence="third-party review says $999", seed="tirzepatide")
    assert warn == ""
    # nothing fresh + seed product already stored -> no web-search fallback, no persist
    assert db.updates == []


def test_key_facts_case2_web_search_fills_product_page_price():
    # evidence has NO tirzepatide price; the seed's product is not stored -> ONE first-party
    # web-search pinned to acme.com finds the product page; the found block is returned for citation.
    db = _FakeDB()

    def search_handler(brief, allowed, blocked):
        if allowed == ["acme.com"] and "tirzepatide" in brief:
            return [{"url": "https://acme.com/tirzepatide/", "fact": "Tirzepatide $149/month",
                     "title": "Tirzepatide"}]
        return []
    gen = _gen(call_handler=lambda p: {"items": [], "seed_product": "tirzepatide"},
               search_handler=search_handler, db=db)
    kf, warn, seed_products, extra = gen._resolve_and_sync_key_facts(
        dict(BRAND), evidence="Acme homepage text (no price here)", seed="online tirzepatide")
    items = {i["product"]: i for i in _stored_items(db)}
    assert "tirzepatide" in items and "149" in items["tirzepatide"]["value"]
    assert items["tirzepatide"]["source_url"] == "https://acme.com/tirzepatide/"
    # the product page is returned as a citable first-party block
    assert extra and extra[0]["url"] == "https://acme.com/tirzepatide/" and extra[0]["label"] == "Acme"


def test_key_facts_single_product_general():
    # a one-service brand: extraction returns ONE item with an empty product; if its price isn't in
    # evidence, the free search is "{name} pricing" (no product token) and stores a general item.
    db = _FakeDB()

    def search_handler(brief, allowed, blocked):
        if allowed == ["acme.com"] and "acme pricing" in brief.lower():
            return [{"url": "https://acme.com/pricing", "fact": "Flat $29/month", "title": "Pricing"}]
        return []
    gen = _gen(call_handler=lambda p: {"items": [], "seed_product": ""},
               search_handler=search_handler, db=db)
    kf, warn, seed_products, extra = gen._resolve_and_sync_key_facts(
        dict(BRAND), evidence="Acme homepage (no price)", seed="is Acme worth it")
    items = _stored_items(db)
    assert len(items) == 1 and items[0]["product"] == "" and "29" in items[0]["value"]
    assert seed_products == []                    # single-product / generic seed -> no product token
    # the canonical block renders a general item cleanly (no product label)
    assert "  - pricing: " in _canonical_facts_block("Acme", kf)


def test_key_facts_reverify_catches_change_even_when_stored():
    # the seed's product is STORED but NOT in this run's evidence (bot-walled) -> the free re-verify
    # search runs anyway and catches a price change (the "inform if the last one was incorrect" path).
    db = _FakeDB()

    def search_handler(brief, allowed, blocked):
        if allowed == ["acme.com"] and "tirzepatide" in brief.lower():
            return [{"url": "https://acme.com/tirzepatide/", "fact": "Now $189/month", "title": "T"}]
        return []
    gen = _gen(call_handler=lambda p: {"items": [], "seed_product": "tirzepatide"},
               search_handler=search_handler, db=db)
    brand = dict(BRAND, key_facts=json.dumps({"pricing": {"items": [
        {"product": "tirzepatide", "value": "$149/month"}]}}))
    kf, warn, seed_products, extra = gen._resolve_and_sync_key_facts(
        brand, evidence="Acme homepage (no tirzepatide price here)", seed="online tirzepatide")
    assert "tirzepatide changed" in warn and "$149/month" in warn and "$189/month" in warn
    assert {i["product"]: i["value"] for i in _stored_items(db)}["tirzepatide"] == "Now $189/month"


def test_key_facts_operator_locked_skips_reverify():
    # an operator-set stored value is trusted: NO re-verify web-search fires even when not in evidence.
    db = _FakeDB()
    calls = {"n": 0}

    def search_handler(brief, allowed, blocked):
        calls["n"] += 1
        return [{"url": "https://acme.com/tirzepatide/", "fact": "$999/month", "title": "T"}]
    gen = _gen(call_handler=lambda p: {"items": [], "seed_product": "tirzepatide"},
               search_handler=search_handler, db=db)
    brand = dict(BRAND, key_facts=json.dumps({"pricing": {"items": [
        {"product": "tirzepatide", "value": "$149/month", "operator_set": True}]}}))
    kf, warn, seed_products, extra = gen._resolve_and_sync_key_facts(
        brand, evidence="Acme homepage (no price)", seed="tirzepatide")
    assert calls["n"] == 0            # operator value trusted -> no web-search
    assert warn == "" and db.updates == []


def test_key_facts_extract_prompt_forbids_third_party():
    db = _FakeDB()
    captured = {}

    def handler(p):
        captured["p"] = p
        return {"items": [], "seed_product": ""}
    gen = _gen(call_handler=handler, db=db)
    gen._resolve_and_sync_key_facts(dict(BRAND), evidence="some evidence text", seed="x")
    p = " ".join(captured.get("p", "").split())   # flatten line-wrapping
    assert "IGNORE every third-party / review / analyst source" in p
    assert "never use a third-party figure" in p


# --- 6. brands.key_facts migrates + round-trips -----------------------------------------------

def test_key_facts_db_round_trip(tmp_path):
    from db import Database
    db = Database(str(tmp_path / "t.db"))
    db.connect()
    db.initialize()
    try:
        sid = db.ensure_live_subreddit("fu150sub")["id"]
        bid = db.add_brand(sid, "Acme")
        payload = json.dumps({"pricing": {"items": [{"product": "Pro", "value": "$9/mo"}]}})
        db.update_brand(bid, key_facts=payload)
        got = db.get_brand(bid)
        assert json.loads(got["key_facts"])["pricing"]["items"][0]["value"] == "$9/mo"
        db.update_brand(bid, author_name="Jane")   # None-guard: byline update doesn't clobber key_facts
        assert json.loads(db.get_brand(bid)["key_facts"])["pricing"]["items"][0]["value"] == "$9/mo"
    finally:
        db.close()
