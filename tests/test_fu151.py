"""FU151 — blog upgrades ($0, no network):
  A) competitor-fact CACHE (hit skips sourcing + write-back; stale/refresh re-source),
  B) parallel usage-tracking lock-safety,
  C) richer schema (HowTo/ItemList/Product/BreadcrumbList) + _validate_jsonld,
  D) deterministic quality scorecard (structure/meta/internal-link honesty).
"""
import json
import time

from generators.blog_gen import (BlogGenerator, build_blog_jsonld, _validate_jsonld,
                                  _parse_howto_steps, _first_table_entities, _price_amount)
from tests.stubs import StubClaude


BRAND = {"id": 1, "name": "Acme", "domain_url": "https://acme.com", "category": "AI music generator",
         "competitors": '["CompA", "CompB"]'}
SEED = "best AI music generators for creators"
ARTICLE = {"body_markdown": "## Quick answer\nAcme, CompA and CompB.\n\n| Tool | Pricing |\n|---|---|\n"
                            "| Acme | $9 |\n| CompA | ? |\n"}


def _gen(call_handler=None, search_handler=None, db=None):
    return BlogGenerator(StubClaude(call_handler=call_handler, search_handler=search_handler), db=db)


def _now_iso():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class _FakeDB:
    def __init__(self):
        self.updates = []

    def update_brand(self, brand_id, **kw):
        self.updates.append((brand_id, kw))


def _vendor_for_pinned(brief, allowed, blocked):
    if allowed and len(allowed) == 1 and "/" not in allowed[0]:
        dom = allowed[0]
        return [{"url": f"https://{dom}/pricing",
                 "fact": "Pro plan $20/month, commercial use allowed, royalty-free",
                 "title": f"{dom} pricing"}]
    return []


def _handler(domains_for):
    def h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": ["CompA", "CompB"], "peer_tools": ["CompA", "CompB"],
                    "dimensions": ["pricing"], "claims": [], "core_topic": "", "products": []}
        if '"domains"' in p:
            return {"domains": {t: t.lower() + ".com" for t in domains_for}}
        return {}
    return h


# --- A: competitor-fact cache ----------------------------------------------------------------

def test_competitor_cache_hit_skips_sourcing_and_writes_back_live():
    db = _FakeDB()
    cf = {"compa": {"domain": "compa.com", "verified_at": _now_iso(),
                    "blocks": [{"label": "CompA", "url": "https://compa.com/pricing",
                                "text": "cached CompA $10/mo commercial royalty-free"}]}}
    brand = dict(BRAND, competitor_facts=json.dumps(cf))
    gen = _gen(call_handler=_handler(["CompB"]), search_handler=_vendor_for_pinned, db=db)
    sourcing = gen._source_for_completion(brand, SEED, ARTICLE, ymyl=None)
    labels = {b["label"] for b in sourcing["fresh"]}
    assert {"CompA", "CompB"} <= labels
    assert any("cached CompA" in b["text"] for b in sourcing["fresh"])   # CompA came from cache
    # the batched domain resolve asked ONLY for the LIVE competitor (CompA was skipped by the cache)
    dom_prompt = next(p for p in gen.claude.calls if '"domains"' in p)
    assert "CompB" in dom_prompt and "CompA" not in dom_prompt
    # write-back persisted the LIVE competitor; the cached one survives
    cf_written = json.loads(db.updates[-1][1]["competitor_facts"])
    assert "compb" in cf_written and "compa" in cf_written


def test_competitor_cache_miss_sources_and_persists_all():
    db = _FakeDB()
    gen = _gen(call_handler=_handler(["CompA", "CompB"]), search_handler=_vendor_for_pinned, db=db)
    gen._source_for_completion(dict(BRAND), SEED, ARTICLE, ymyl=None)
    cf_written = json.loads(db.updates[-1][1]["competitor_facts"])
    assert "compa" in cf_written and "compb" in cf_written   # both live-sourced + cached


def test_competitor_cache_stale_is_resourced():
    db = _FakeDB()
    cf = {"compa": {"domain": "compa.com", "verified_at": "2020-01-01T00:00:00Z",   # ancient → stale
                    "blocks": [{"label": "CompA", "url": "https://compa.com", "text": "old"}]}}
    brand = dict(BRAND, competitor_facts=json.dumps(cf))
    gen = _gen(call_handler=_handler(["CompA", "CompB"]), search_handler=_vendor_for_pinned, db=db)
    gen._source_for_completion(brand, SEED, ARTICLE, ymyl=None)
    # stale CompA was re-sourced (its domain resolve fired) and overwritten with a recent verified_at
    dom_prompt = next(p for p in gen.claude.calls if '"domains"' in p)
    assert "CompA" in dom_prompt
    cf_written = json.loads(db.updates[-1][1]["competitor_facts"])
    assert cf_written["compa"]["verified_at"] != "2020-01-01T00:00:00Z"


def test_competitor_cache_refresh_bypasses_hit():
    db = _FakeDB()
    cf = {"compa": {"domain": "compa.com", "verified_at": _now_iso(),
                    "blocks": [{"label": "CompA", "url": "https://compa.com", "text": "cached"}]}}
    brand = dict(BRAND, competitor_facts=json.dumps(cf))
    gen = _gen(call_handler=_handler(["CompA", "CompB"]), search_handler=_vendor_for_pinned, db=db)
    gen._source_for_completion(brand, SEED, ARTICLE, ymyl=None, refresh_competitor_facts=True)
    dom_prompt = next(p for p in gen.claude.calls if '"domains"' in p)
    assert "CompA" in dom_prompt   # cache ignored → CompA re-resolved live


# --- B: usage-tracking lock safety -----------------------------------------------------------

def test_usage_track_is_lock_safe():
    import threading
    import types
    from generators.base import ClaudeClient
    c = ClaudeClient("test-key")
    c.reset_usage()

    def _msg():
        return types.SimpleNamespace(usage=types.SimpleNamespace(
            input_tokens=1, output_tokens=2, server_tool_use=None))

    def worker():
        for _ in range(1000):
            c._track(_msg())
    ts = [threading.Thread(target=worker) for _ in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert c._usage["input_tokens"] == 8000    # no lost updates under 8 concurrent workers
    assert c._usage["output_tokens"] == 16000


# --- C: richer schema + validation -----------------------------------------------------------

def test_parse_helpers():
    assert _parse_howto_steps("1. first\n2. second\n3. third") == ["first", "second", "third"]
    assert _parse_howto_steps("just prose, one point") == []
    assert _first_table_entities("| Tool | X |\n|---|---|\n| Acme | a |\n| CompA | b |") == ["Acme", "CompA"]
    assert _price_amount("$149 first month, then $249/mo") == ("149", "USD")
    assert _price_amount("no price here") == (None, None)


def test_schema_richer_types():
    blog = {"title": "How to set up X", "seed": "how to set up X", "meta_description": "d",
            "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-02T00:00:00",
            "body_markdown": "## Steps\n1. Do the first thing.\n2. Then the second.\n3. Finally third.\n\n"
                             "| Tool | Price |\n|---|---|\n| Acme | $9 |\n| CompA | $19 |\n| CompB | $29 |\n"}
    brand = {"name": "Acme", "domain_url": "https://acme.com",
             "key_facts": json.dumps({"pricing": {"items": [{"product": "", "value": "$9/mo"}]}})}
    graph = build_blog_jsonld(blog, brand, page_url="https://acme.com/how-to")["@graph"]
    types_ = [n["@type"] for n in graph]
    assert {"Article", "HowTo", "ItemList", "Product", "BreadcrumbList"} <= set(types_)
    assert len(next(n for n in graph if n["@type"] == "HowTo")["step"]) == 3
    assert len(next(n for n in graph if n["@type"] == "ItemList")["itemListElement"]) == 3
    prod = next(n for n in graph if n["@type"] == "Product")
    off = prod["offers"] if isinstance(prod["offers"], dict) else prod["offers"][0]
    assert off["price"] == "9" and off["priceCurrency"] == "USD"
    # a NON-how-to seed → no HowTo node (trigger is deterministic)
    g2 = [n["@type"] for n in build_blog_jsonld(dict(blog, seed="best X tools", title="Best X"),
                                                brand)["@graph"]]
    assert "HowTo" not in g2


def test_validate_jsonld():
    good = [{"@type": "Article", "headline": "h", "datePublished": "2026-01-01",
             "author": {"@type": "Person", "name": "x"},
             "publisher": {"@type": "Organization", "name": "p"}}]
    assert _validate_jsonld(good) == []
    assert any("datePublished" in p for p in _validate_jsonld([{"@type": "Article", "headline": "h"}]))
    faq_bad = [{"@type": "FAQPage", "mainEntity": [
        {"@type": "Question", "name": "q?", "acceptedAnswer": {"@type": "Answer", "text": ""}}]}]
    assert _validate_jsonld(faq_bad)


# --- D: quality scorecard --------------------------------------------------------------------

def test_quality_report_structure_and_meta():
    gen = _gen()
    art = {"body_markdown": "## Overview\nSome text.\n", "meta_title": "t", "meta_description": "d"}
    ch = {c["key"]: c["ok"] for c in gen._quality_report(art, BRAND)["checks"]}
    assert ch["quick_answer"] is False and ch["faq"] is False and ch["question_headings"] is False
    good = "## Quick answer\nAcme is best.\n\n## Is Acme good?\nYes it is.\n\n## FAQ\n### Does it work?\nYes.\n"
    rep = gen._quality_report({"body_markdown": good, "meta_title": "t", "meta_description": "d"}, BRAND)
    ch2 = {c["key"]: c["ok"] for c in rep["checks"]}
    assert ch2["quick_answer"] and ch2["faq"] and ch2["question_headings"]
    assert isinstance(rep["score"], int) and 0 <= rep["score"] <= 100
    long_desc = gen._quality_report({"body_markdown": good, "meta_title": "t", "meta_description": "x" * 200}, BRAND)
    assert {c["key"]: c["ok"] for c in long_desc["checks"]}["meta_desc"] is False


def test_quality_report_internal_link_honesty():
    gen = _gen()
    body = ("## Quick answer\nSee [our pricing](https://acme.com/pricing) and "
            "[rogue](https://acme.com/rogue).\n")
    rep = gen._quality_report({"body_markdown": body, "meta_title": "t", "meta_description": "d"},
                              BRAND, link_targets=[{"url": "https://acme.com/pricing"}])
    il = next(c for c in rep["checks"] if c["key"] == "internal_links")
    assert il["ok"] is False   # /rogue is internal but NOT a verified target
