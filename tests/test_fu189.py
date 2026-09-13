"""FU189 — a comparison row that is NOT A COMPANY must not be hunted for a website.

A live blog paused with "Couldn't find public info for TRT (Testosterone Replacement Therapy) …
Paste a link to its page", which is a link that cannot exist. Generic defect: a comparison table
holds PROVIDERS (a company with a site, fetched first-party) and OPTIONS (a treatment, loan type,
material, employment model, "build in-house") which have no site at all. Every vertical compares the
second kind. $0, no network (StubClaude)."""
import json

from generators.blog_gen import (BlogGenerator, _named_as_option, _matches_a_product, _opt_forms)
from tests.stubs import StubClaude

SEED = "can you take trt and a glp-1 at the same time"
BODY = ("## Quick answer\nx\n\n| Option | Body weight change |\n|---|---|\n"
        "| PeterMD | a |\n| Ro | b |\n| TRT (Testosterone Replacement Therapy) | c |\n")
REF_MARK = "NOT a vendor sales page"


def _run(tools, *, options=(), products=(), dims=("Body weight change",), domains=None,
         brand=None, search_h=None, seed=SEED, body=BODY, cat="telehealth"):
    """Drive the real _source_for_completion with a scripted extraction. Returns (stub, sourcing)."""
    doms = domains if domains is not None else {}

    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": list(tools), "peer_tools": list(tools), "dimensions": list(dims),
                    "generic_options": list(options), "products": list(products),
                    "claims": [], "core_topic": ""}
        if '"domains"' in p:
            return {"domains": dict(doms)}
        return {}

    stub = StubClaude(call_handler=call_h, search_handler=(search_h or (lambda b, a, bl: [])))
    gen = BlogGenerator(stub, db=None)
    gen._evidence_blocks = []
    b = brand or {"name": "PeterMD", "domain_url": "https://getpetermd.com",
                  "category": cat, "competitors": []}
    return stub, gen._source_for_completion(b, seed, {"body_markdown": body}, ymyl=None)


def _briefs_for(stub, needle):
    return [s["brief"] for s in stub.searches if needle.lower() in (s["brief"] or "").lower()]


# --- the reported case ---------------------------------------------------------------------------

def test_the_reported_entity_is_never_hunted_for_a_website():
    TRT = "TRT (Testosterone Replacement Therapy)"
    stub, _ = _run(["Ro", TRT], options=[TRT], domains={"Ro": "ro.co"})
    assert not [d for d in stub.domain_calls if "TRT" in d["brand"]]
    assert not [c for c in stub.site_fact_calls if "TRT" in (c["brand"] or "")]
    # exactly ONE reference search, and NONE of the vendor-hunt shapes (~13 searches before).
    assert len(_briefs_for(stub, REF_MARK)) == 1
    assert not _briefs_for(stub, "the OFFICIAL website")
    assert not [b for b in _briefs_for(stub, "TRT (Testosterone") if "pricing and plans" in b]


def test_the_provider_alongside_it_still_gets_the_vendor_treatment():
    TRT = "TRT (Testosterone Replacement Therapy)"
    stub, _ = _run(["Ro", TRT], options=[TRT], domains={"Ro": "ro.co"})
    assert any("ro.co" in (c["domain"] or "") for c in stub.site_fact_calls) or \
        any(s["allowed"] == ["ro.co"] for s in stub.searches)
    assert not [b for b in _briefs_for(stub, "Ro:") if REF_MARK in b]


def test_an_option_never_binds_a_domain_even_when_the_fallback_offers_one():
    """The url fallback accepts any domain not on the blocklists, which is how a non-vendor got
    vendor-labelled blocks from an unrelated site."""
    TRT = "TRT (Testosterone Replacement Therapy)"

    def search_h(brief, allowed, blocked):
        return [{"title": "Some clinic", "url": "https://unrelated-clinic.com/trt",
                 "fact": "we offer TRT"}]

    stub, sourcing = _run([TRT], options=[TRT], search_h=search_h)
    assert not stub.domain_calls
    assert not any((f.get("label") or "").strip().lower() == TRT.lower() for f in sourcing["fresh"])


# --- inert on an all-provider comparison ---------------------------------------------------------

def test_an_all_provider_comparison_is_untouched():
    stub, sourcing = _run(["Ro", "Noom Med"], domains={"Ro": "ro.co", "Noom Med": "noom.com"})
    assert not _briefs_for(stub, REF_MARK)
    assert sourcing.get("options") == []


# --- cross-vertical: the standing genericity gate -------------------------------------------------

def test_the_same_routing_works_in_lending_construction_and_hr():
    for cat, opt, provider in (("business lending", "term loan", "Bluevine"),
                               ("paving contractors", "asphalt", "Acme Paving"),
                               ("HR software", "PEO", "Gusto")):
        stub, sourcing = _run([provider, opt], options=[opt], cat=cat,
                              domains={provider: provider.lower().replace(" ", "") + ".com"},
                              dims=("cost",), seed=f"best {cat}",
                              body=f"| a | b |\n|---|---|\n| {provider} | x |\n| {opt} | y |\n")
        assert not [d for d in stub.domain_calls if d["brand"] == opt], (cat, stub.domain_calls)
        assert len(_briefs_for(stub, REF_MARK)) == 1, cat
        assert not _briefs_for(stub, "the OFFICIAL website"), cat
        assert sourcing["options"] == [opt.lower()], cat


def test_the_reference_brief_carries_no_medical_vocabulary():
    stub, _ = _run(["term loan"], options=["term loan"], cat="business lending", dims=("cost",))
    brief = _briefs_for(stub, REF_MARK)[0].lower()
    for word in ("drug", "dose", "clinical", "fda", "prescription", "patient", "therapy"):
        assert word not in brief, word


# --- the products backstop ------------------------------------------------------------------------

def test_a_product_with_no_resolvable_domain_is_treated_as_an_option():
    """The model missed it; the entity is one of the article's own PRODUCTS and resolved no domain."""
    stub, sourcing = _run(["tirzepatide"], options=[], products=["tirzepatide"])
    assert sourcing["options"] == ["tirzepatide"]
    assert not stub.domain_calls


def test_the_backstop_never_misroutes_a_provider_that_has_a_domain():
    """A real vendor sharing a token with a product keeps its vendor treatment."""
    stub, sourcing = _run(["Tirzepatide Direct"], options=[], products=["tirzepatide"],
                          domains={"Tirzepatide Direct": "tirzepatidedirect.com"})
    assert sourcing["options"] == []
    assert not _briefs_for(stub, REF_MARK)


def test_the_matchers_read_both_halves_of_a_parenthetical_name():
    TRT = "TRT (Testosterone Replacement Therapy)"
    assert set(_opt_forms(TRT)) == {TRT, "TRT", "Testosterone Replacement Therapy"}
    assert _named_as_option(TRT, ["TRT"]) and _named_as_option("TRT", [TRT])
    assert _matches_a_product(TRT, ["testosterone"])
    assert not _matches_a_product("Ro", ["testosterone"])


# --- the pause asks for what can actually be supplied ----------------------------------------------

def test_an_unsourceable_option_pauses_for_FACTS_not_a_link():
    TRT = "TRT (Testosterone Replacement Therapy)"
    _, sourcing = _run([TRT], options=[TRT])
    item = [u for u in sourcing["unsourced"] if u["tool"] == TRT]
    assert item and item[0].get("generic_option") is True
    assert "dom" not in item[0]                    # there is no page to point at
    assert item[0]["facts"] == ["Body weight change"]


# --- the reconcile must not delete the row ----------------------------------------------------------

def _ref_hit(brief, allowed, blocked):
    """A reference source so `fresh` is non-empty — the reconcile bails without one."""
    return [{"title": "Prescribing information", "url": "https://example.gov/ref",
             "fact": "TRT raises serum testosterone; term loan; PEO; asphalt"}]


def _recon_prompt(sourcing, gen_stub):
    gen = BlogGenerator(gen_stub, db=None)
    gen._evidence_blocks = []
    gen._reconcile_and_finish({"name": "PeterMD", "domain_url": "https://getpetermd.com"},
                              SEED, {"body_markdown": BODY}, sourcing)
    return next((c for c in gen_stub.calls if "VERIFY + COMPLETE agent" in c), "")


def test_the_reconcile_is_told_which_rows_are_not_companies():
    TRT = "TRT (Testosterone Replacement Therapy)"
    _, sourcing = _run(["Ro", TRT], options=[TRT], domains={"Ro": "ro.co"}, search_h=_ref_hit)
    p = _recon_prompt(sourcing, StubClaude(call_handler=lambda _p: {}))
    assert "GENERIC OPTIONS" in p and TRT in p
    assert "NEVER remove a generic option's row" in p


def test_an_all_provider_reconcile_prompt_has_no_generic_options_block():
    _, sourcing = _run(["Ro", "Noom Med"], domains={"Ro": "ro.co", "Noom Med": "noom.com"},
                       search_h=_ref_hit)
    p = _recon_prompt(sourcing, StubClaude(call_handler=lambda _p: {}))
    assert "GENERIC OPTIONS" not in p


# --- no pause for an entity the dim-rescue sourced afterwards ----------------------------------------

def test_an_entity_sourced_later_drops_off_the_pause_list():
    """The dim-rescue runs AFTER the pause list is built and keeps blocks on a looser filter, so a
    pause was being queued for an entity that got sourced moments later."""
    def search_h(brief, allowed, blocked):
        if "Noom Med" in (brief or ""):
            return [{"title": "Noom Med pricing", "url": "https://noom.com/med",
                     "fact": "Noom Med costs $149/month"}]
        return []

    _, sourcing = _run(["Noom Med"], dims=("pricing",), search_h=search_h,
                       domains={"Noom Med": "noom.com"})
    assert not [u for u in sourcing["unsourced"] if u["tool"] == "Noom Med"], sourcing["unsourced"]
