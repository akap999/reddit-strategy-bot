"""FU239 — the four failures a hand-corrected semaglutide article exposed, none of which were new to
the system: its stored quality report already carried thirteen warnings and a score of 58, and it
published anyway.

1. A figure no gathered source contains was stated as a cited clinical outcome ("women achieving
   18.1% and men 13.4%"). The study reports odds ratios by sex, not those percentages, and its page
   is behind an anti-bot wall so it was never read. The check that says so only warned.
2. A price column code had just FILLED was then deleted by the any-empty rule, because one starved
   competitor had no price — and its "prices checked today" note stayed behind under a table with no
   prices. That competitor was empty because the $3 ceiling skipped 8 of its searches.
3. An add-on figure passes every price test: own domain, real price, product token in the path. Live
   against PeterMD, a GLP-1 price search picked "an optional HCG add-on is available for $301".
4. One box for every missing fact made the manual ask unanswerable, and the keep/remove radio stayed
   lit while the code was already ignoring it.

$0, no network (StubClaude).
"""
import json
import re

import pytest

from generators.blog_gen import (BlogGenerator, _best_product_price, _price_is_addon)
from tests.stubs import StubClaude


# ── 3. an add-on / bundle figure is not the product's price ──────────────────────────────────────
@pytest.mark.parametrize("text,fig,want", [
    ("A 2-month GLP-1 plan is offered; an optional HCG add-on is available for $301.", "$301", True),
    ("GLP-1 plan $270/month; an optional HCG add-on is $301.", "$270", False),
    ("GLP-1 plan $270/month; an optional HCG add-on is $301.", "$301", True),
    ("GLP-1 monthly subscription is $270/month, medicine included.", "$270", False),
    ("Combo weight loss bundle is $415.", "$415", True),
    ("Membership required, $39 first month, then $149/month.", "$149", False),
    ("Add-on: sermorelin $89/month.", "$89", True),
])
def test_addon_wording_is_judged_by_the_clause_not_the_page(text, fig, want):
    """The clause, never the page: a product page legitimately lists what can be added to it, and a
    character window wide enough to catch the add-on also swallows the real price beside it."""
    assert _price_is_addon(fig, text) is want


def test_the_live_hcg_addon_is_rejected_as_the_glp1_price():
    """The exact result the live search returned for PeterMD."""
    res = [{"url": "https://getpetermd.com/product/glp-1_weight/", "title": "GLP-1",
            "fact": "A 2-month GLP-1 plan is offered; an optional HCG add-on is available for $301."}]
    assert _best_product_price(res, "GLP-1", "getpetermd.com") is None


def test_a_real_product_price_on_the_same_shape_of_page_survives():
    res = [{"url": "https://getpetermd.com/product/glp1m2m/", "title": "GLP-1",
            "fact": "GLP-1 monthly subscription is $270/month, medicine included."}]
    assert (_best_product_price(res, "GLP-1", "getpetermd.com") or {})["url"].endswith("glp1m2m/")


def test_a_fact_pricing_both_the_product_and_its_addon_keeps_the_product():
    res = [{"url": "https://x.com/product/glp-1/", "title": "GLP-1",
            "fact": "GLP-1 plan $270/month; an optional HCG add-on is $301."}]
    assert _best_product_price(res, "GLP-1", "x.com") is not None


# ── 1. a figure in NOTHING we gathered is the model's, and is removed ─────────────────────────────
WALLED = {"label": "Obesity journal", "url": "https://onlinelibrary.wiley.com/doi/abs/10.1002/oby.70120",
          "text": "Real-World Evidence on Weight Loss and Safety With Semaglutide in Obesity Telehealth."}
OWN = {"label": "PeterMD", "url": "https://getpetermd.com/product/glp1m2m/",
       "text": "GLP-1 monthly subscription is $270/month with 15 mg dosing available."}


def _gen():
    return BlogGenerator(StubClaude(), None)


def test_the_fabricated_sex_split_is_removed_and_the_sourced_price_is_not():
    body = ("## What the evidence shows\n\n"
            "A cohort study found a mean loss of 16.6% at 68 weeks, with women achieving 18.1% and "
            "men 13.4% [S1]. PeterMD's GLP-1 subscription is $270/month [S2].\n")
    out, note = _gen()._unsourced_figure_check(body, [WALLED, OWN])
    assert "18.1%" not in out and "13.4%" not in out
    assert "$270/month [S2]" in out, "a figure a gathered source DOES state must survive"
    assert "unsourced-figures" in note


def test_a_table_cell_is_blanked_not_the_row():
    body = ("| Platform | Price |\n| --- | --- |\n"
            "| PeterMD | $270/month [S2] |\n| Ro | $999.99/month [S1] |\n")
    out, _ = _gen()._unsourced_figure_check(body, [WALLED, OWN])
    assert "$999.99" not in out
    assert "| Ro |" in out and "$270/month [S2]" in out


def test_an_uncited_figure_is_left_alone():
    """The claim is that a figure was presented as SOURCED. Without a citation there is no false
    attribution, and rewriting the author's own prose is not this check's job."""
    body = "Roughly 18.1% of people report this, in our experience.\n"
    out, note = _gen()._unsourced_figure_check(body, [WALLED, OWN])
    assert out == body and not note


def test_the_sources_list_is_never_touched():
    body = ("Some claim [S1].\n\n## Sources\n\n- [S1] Obesity journal — "
            "https://onlinelibrary.wiley.com/doi/abs/10.1002/oby.70120 covering 18.1% of cases\n")
    out, _ = _gen()._unsourced_figure_check(body, [WALLED, OWN])
    assert "18.1% of cases" in out


def test_a_body_that_would_need_mass_deletion_is_left_alone_and_says_so():
    """If most of the article fails, the evidence is what failed. Gutting it would hide that."""
    body = "\n\n".join(f"Finding {i}: the rate was {i}.{i}% at 12 months [S1]." for i in range(1, 10))
    out, note = _gen()._unsourced_figure_check(body, [WALLED, OWN])
    assert out == body
    assert "too many to remove safely" in note and "regenerate" in note


def test_nothing_gathered_means_nothing_judged():
    body = "A cohort study found 18.1% [S1].\n"
    out, note = _gen()._unsourced_figure_check(body, [{"label": "x", "url": "", "text": ""}])
    assert out == body and not note


# ── 4. the manual ask: one row per required fact, each with its own link ──────────────────────────
def _finish(provided, facts=("price", "license")):
    gen = BlogGenerator(StubClaude(call_handler=lambda p: {
        "revised_body_markdown": "# T\n\nAcme costs $29 [S1].\n", "flagged": []}), None)
    gen._fetch_url = lambda u: f"PAGE TEXT FROM {u}"
    ck = {"sourcing": {"tools": ["Acme", "Beta", "Gamma"], "dims": list(facts), "claims": [],
                       "fresh": [], "unsourced": [{"tool": "Acme", "facts": list(facts)}]},
          "article": {"title": "T", "body_markdown": "# T\n\nx\n", "meta_description": "m",
                      "keywords": []},
          "draft_body": "# T\n\nx\n", "evidence_blocks": []}
    gen.finish_pending_blog({"name": "Acme", "domain_url": "https://acme.com"}, "seed", ck, provided)
    return gen


def test_each_required_fact_becomes_its_own_block_with_its_own_page():
    """Three columns, three different pages — the whole point of the per-fact rows."""
    gen = _finish([{"tool": "Acme", "url": "https://acme.com/pricing", "fact": "price: $29",
                    "items": [{"label": "price", "url": "https://acme.com/pricing", "fact": ""},
                              {"label": "license", "url": "", "fact": "commercial use allowed"}]}])
    got = [b for b in gen._evidence_blocks if b.get("label") == "Acme"]
    assert len(got) == 2, "one block per answered fact, not one for the lot"
    assert any(b["url"] == "https://acme.com/pricing" for b in got)
    assert any("license: commercial use allowed" in b["text"] for b in got)


def test_a_row_left_blank_contributes_nothing():
    gen = _finish([{"tool": "Acme", "url": "", "fact": "price: $29",
                    "items": [{"label": "price", "url": "", "fact": "$29"},
                              {"label": "license", "url": "", "fact": ""}]}])
    assert len([b for b in gen._evidence_blocks if b.get("label") == "Acme"]) == 1


def test_an_older_client_sending_one_url_and_one_fact_still_works():
    """The legacy shape has to keep working — a checkpoint can outlive a deploy."""
    gen = _finish([{"tool": "Acme", "url": "https://acme.com/p", "fact": "everything, in one box"}])
    got = [b for b in gen._evidence_blocks if b.get("label") == "Acme"]
    assert len(got) == 1 and got[0]["url"] == "https://acme.com/p"


# ── 2. a competitor the cost ceiling starved is dropped, not shipped empty ────────────────────────
def _source(tools, starved, brand_extra=None):
    """Drive the real sourcing with one competitor whose searches the ceiling SKIPS, so it ends with
    no blocks. Returns (gen, sourcing)."""
    box = {}

    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": list(tools), "peer_tools": list(tools), "dimensions": ["pricing"],
                    "products": [], "claims": [], "core_topic": "glp-1"}
        if '"domains"' in p:
            return {"domains": {t: t.lower().replace(" ", "") + ".com" for t in tools}}
        return {}

    def search_h(brief, allowed, blocked):
        b = (brief or "").lower()
        for t in tools:
            # word-boundary: a bare `in` makes "Ro" match inside "from" / "product" / "provider",
            # so every brief looked like Ro's and no other tool's branch was ever reached.
            if re.search(r"\b" + re.escape(t.lower()) + r"\b", b):
                if t == starved:
                    # the ceiling stopped looking — exactly what `skipped_searches()` counts
                    box["c"]._skipped = getattr(box["c"], "_skipped", 0) + 1
                    return []
                slug = t.lower().replace(" ", "")
                return [{"title": f"{t} pricing", "url": f"https://{slug}.com/pricing",
                         "fact": f"{t} plans start at $29 per month, billed monthly."}]
        return []

    claude = StubClaude(call_handler=call_h, search_handler=search_h)
    box["c"] = claude
    gen = BlogGenerator(claude, db=None)
    brand = {"name": "Acme", "domain_url": "https://acme.com", "category": "telehealth"}
    brand.update(brand_extra or {})
    body = "| Clinic | Pricing |\n|---|---|\n" + "".join(f"| {t} | ? |\n" for t in tools)
    return gen, gen._source_for_completion(brand, "best glp-1 clinics",
                                           {"body_markdown": body}, ymyl=None)


def test_a_starved_competitor_is_dropped_when_the_field_survives_without_it():
    """The reviewed article shipped a starved competitor as a section of filler, and its empty price
    cell then took the whole column down for every other brand."""
    gen, s = _source(["Ro", "Hims", "Noom", "Calibrate"], starved="Calibrate")
    assert "Calibrate" not in s["tools"]
    assert len(s["tools"]) == 3
    assert "Calibrate" in (gen._budget_drop_note or "")


def test_a_starved_competitor_is_KEPT_when_dropping_would_breach_the_floor():
    """Below three there is nothing to drop — the answer there is to spend more, not to shrink the
    comparison into a self-crowning one."""
    gen, s = _source(["Ro", "Hims", "Calibrate"], starved="Calibrate")
    assert "Calibrate" in s["tools"]
    assert not (gen._budget_drop_note or "")


def test_one_of_your_own_competitors_is_never_dropped_for_budget():
    gen, s = _source(["Ro", "Hims", "Noom", "Calibrate"], starved="Calibrate",
                     brand_extra={"manual_competitors": json.dumps(["Calibrate"])})
    assert "Calibrate" in s["tools"]


def test_a_competitor_that_simply_found_nothing_is_not_treated_as_starved():
    """No skipped searches means the tool looked and found nothing — that is the operator's call at
    the pause, not an automatic removal."""
    gen, s = _source(["Ro", "Hims", "Noom", "Calibrate"], starved=None)
    assert not (gen._budget_drop_note or "")
