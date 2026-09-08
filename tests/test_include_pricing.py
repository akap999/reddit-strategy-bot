"""FU162 — the "Include pricing" operator flag (default ON). When OFF: no pricing searches, no
operator-price injection, no subject `_price_warn`, no competitor price flag-and-ask, and the key-facts
sync strips pricing. When ON: today's behavior. $0, no network (recording StubClaude)."""
import json

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude


def _brand(kf=None):
    b = {"name": "Acme", "domain_url": "https://acme.com", "category": "telehealth"}
    if kf is not None:
        b["key_facts"] = json.dumps(kf)
    return b


def _source(brand, tools, products, dims=("pricing",), search_h=None, include_pricing=True):
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
    s = gen._source_for_completion(brand, "best glp-1 clinics", {"body_markdown": body},
                                   ymyl=None, include_pricing=include_pricing)
    return gen, s


def _all_briefs(gen):
    return ([x["brief"].lower() for x in gen.claude.searches]
            + [c["brief"].lower() for c in gen.claude.site_fact_calls])


# --- OFF: no pricing work at all --------------------------------------------------------------
def test_pricing_off_no_search_no_injection_no_warn():
    kf = {"pricing": {"items": [{"product": "semaglutide", "value": "$249/mo", "operator_set": True}]}}

    def search_h(brief, allowed, blocked):   # any own-domain search would return a price if it fired
        return ([{"url": f"https://{allowed[0]}", "fact": "semaglutide $199/mo", "title": "x"}]
                if allowed else [])
    gen, s = _source(_brand(kf), ["Ro"], ["semaglutide"], search_h=search_h, include_pricing=False)
    briefs = _all_briefs(gen)
    # NO pricing-specific brief ran (the ON path's price phrases are all gated out)
    assert not any(("pricing and plans" in b) or ("price/pricing" in b) or ("current price" in b)
                   or ("price/cost" in b) for b in briefs)
    assert not any("249" in (b.get("text") or "") for b in s["fresh"])   # operator price NOT injected
    assert not getattr(gen, "_price_warn", "")                            # no subject price warning
    assert not any(u.get("price_only") for u in s["unsourced"])           # no price flag-and-ask


def test_pricing_off_sync_strips_pricing_no_search():
    gen = BlogGenerator(StubClaude(search_handler=lambda b, a, bl: [{"url": "https://acme.com/x",
                                                                     "fact": "$9/mo"}]), db=None)
    brand = _brand({"pricing": {"items": [{"product": "x", "value": "$9", "operator_set": True}]},
                    "other": {"value": "keep-me"}})
    kf, warn, seeds, extra = gen._resolve_and_sync_key_facts(brand, evidence="Acme own site text",
                                                             seed="x", include_pricing=False)
    assert "pricing" not in kf              # pricing stripped from the writer's canonical block
    assert kf.get("other")                  # non-pricing facts preserved
    assert extra == [] and seeds == []
    assert gen.claude.searches == []        # NO pricing web-search fired


# --- ON (default): pricing behaves as today (parity guard) ------------------------------------
def test_pricing_on_injects_operator_price():
    kf = {"pricing": {"items": [{"product": "semaglutide", "value": "$249/mo", "operator_set": True}]}}
    gen, s = _source(_brand(kf), ["Ro"], ["semaglutide"], include_pricing=True)
    assert any("249" in (b.get("text") or "") for b in s["fresh"])   # operator price injected when ON
