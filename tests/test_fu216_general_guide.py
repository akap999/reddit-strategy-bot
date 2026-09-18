"""FU216 — "General guide" blogs: a generic how-to / informational article with no competitor
comparison, and the brand's service area only in the closing call-to-action.

The case that started it: CMK Construction's "In what order should you renovate a house?" was
generated with the Geography field BLANK, yet its Quick answer said "For Tampa Bay homeowners…", the
permits step named five Florida counties and a Hillsborough County phone number, hcfl.gov was cited
twice as `official ·`, a whole section profiled four Tampa Bay contractors, a four-provider table and an
ItemList shipped, and the keywords carried "Tampa Bay home renovation" and "remodeling order Tampa".
That export is the fixture here (tests/fixtures/fu216_cmk_guide_body.md).

Everything is a StubClaude, $0. The OFF path is proven byte-identical by the FU205 goldens
(tests/test_fu205_prompt_inert.py) — these tests cover the ON path.
"""
import json
import os
import tempfile

import pytest

from db import Database
from generators.blog_gen import BlogGenerator as BG, build_blog_jsonld, _mentions_area
from tests.stubs import StubClaude

_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "fu216_cmk_guide_body.md")
REAL_BODY = open(_FIX, encoding="utf-8").read()

SEED = "In what order should you renovate a house?"
PLACES = ["Tampa Bay", "Tampa", "Hillsborough", "Pinellas", "Pasco", "Sarasota", "Manatee"]
LOCAL_CONTRACTORS = ["Elite Builder Renovation", "Onsite Construction", "Renovate Tampa Bay",
                     "Lindross Remodeling"]

BRAND = {
    "id": 11, "name": "CMK Construction", "domain_url": "https://www.cmkconstructioninc.com",
    "category": "kitchen and bathroom remodeling contractor",
    "audience": "homeowners in Tampa Bay",
    "context": ("CMK Construction remodels kitchens and bathrooms across Tampa Bay — Hillsborough, "
                "Pinellas, Pasco, Sarasota and Manatee counties — from a 4,000 sq ft Design Studio."),
    "competitors": json.dumps(["Elite Builder Renovation", "Revive Kitchen & Bath"]),
    "manual_competitors": json.dumps(["Lindross Remodeling"]),
    "competitor_domains": json.dumps({"Elite Builder Renovation": "elitebuilderrenovation.com"}),
}


def _gen(call_handler=None, search_handler=None, **kw):
    stub = StubClaude(call_handler=call_handler or (lambda p: {}), search_handler=search_handler, **kw)
    return BG(stub, db=None), stub


def _guide_gen(places=PLACES, **kw):
    g, stub = _gen(**kw)
    g._guide, g._guide_places = True, list(places)
    return g, stub


def _article_stub(p):
    return {"title": SEED, "meta_description": "m", "keywords": [], "body_markdown": "# x\n",
            "disclosure": "d"}


# ── the writer prompt ─────────────────────────────────────────────────────────────────────────
def test_a_guide_article_prompt_drops_the_comparison_rules_and_carries_the_guide_block():
    g, _ = _gen(call_handler=_article_stub)
    g._guide_places = list(PLACES)   # what `_prepare_guide` would have set for a blank geography
    g.generate_article(BRAND, SEED, guide=True)
    p = g._article_prompt
    assert "ENTITY-TYPE MATCH" not in p
    assert "MINIMUM COMPETITORS" not in p
    assert "names CMK Construction\n    as a fit" not in p
    assert "who might prefer an alternative\"\n    section" not in p
    assert "GENERAL GUIDE (FU216" in p
    assert "operates in Tampa Bay, Tampa, Hillsborough, Pinellas, Pasco, Sarasota, Manatee" in p
    assert "NEVER a specific county or city office" in p
    assert "IMMEDIATELY BEFORE the FAQ" in p
    assert "when to call a professional" in p


def test_the_off_article_prompt_keeps_every_comparison_rule_and_no_guide_block():
    g, _ = _gen(call_handler=_article_stub)
    g.generate_article(BRAND, SEED)
    p = g._article_prompt
    for must in ("ENTITY-TYPE MATCH", "MINIMUM COMPETITORS", "as a fit", "who might prefer an alternative"):
        assert must in p
    assert "GENERAL GUIDE" not in p


def test_the_guide_brand_block_names_no_competitor_but_keeps_what_the_brand_does():
    g, _ = _gen()
    g._guide = True
    g._priced_names = ["Revive Kitchen & Bath"]
    g._subject_phrase, g._subject_peers = "bathroom remodeling", {"Bath World": "bathworld.com"}
    block = g._brand_block(BRAND)[2]
    for bad in ("Competitors", "YOU MUST COMPARE", "Specialists", "MUST BE COMPARED",
                "Elite Builder", "Lindross", "Bath World"):
        assert bad not in block, bad
    assert "Category: kitchen and bathroom remodeling contractor" in block
    assert "Context:" in block
    g._guide = False
    off = g._brand_block(BRAND)[2]
    assert "YOU MUST COMPARE" in off and "Elite Builder Renovation" in off


def test_verify_claims_gets_one_guide_line_only_in_guide_mode():
    art = {"body_markdown": REAL_BODY}
    g, stub = _guide_gen(call_handler=lambda p: {"revised_body_markdown": "x", "flagged": []})
    g.verify_claims(BRAND, art)
    assert "GENERAL GUIDE: this article is a generic guide" in stub.calls[-1]
    assert "service area (Tampa Bay, Tampa, Hillsborough" in stub.calls[-1]
    g2, stub2 = _gen(call_handler=lambda p: {"revised_body_markdown": "x", "flagged": []})
    g2.verify_claims(BRAND, art)
    assert "GENERAL GUIDE" not in stub2.calls[-1]


# ── the service area ──────────────────────────────────────────────────────────────────────────
def test_the_service_area_keeps_only_places_the_brand_itself_wrote():
    g, stub = _gen(call_handler=lambda p: {"places": [
        "Tampa Bay", "Tampa", "Hillsborough, Pinellas & Pasco", "Orlando", "United States", "nationwide"]})
    got = g._brand_service_area(BRAND)
    # "Tampa" (the metro's core city) is in the brand's own words; Orlando is a guess; the US a country
    assert got == ["Tampa Bay", "Tampa", "Hillsborough", "Pinellas", "Pasco"]
    n = len(stub.calls)
    assert g._brand_service_area(BRAND) == got and len(stub.calls) == n, "cached per brand"


def test_a_failed_service_area_lookup_is_an_empty_list():
    class _Boom(StubClaude):
        def call(self, *a, **k):
            raise RuntimeError("down")
    g = BG(_Boom(), db=None)
    assert g._brand_service_area(BRAND) == []


def test_prepare_guide_blank_geography_fills_the_places():
    g, _ = _gen(call_handler=lambda p: {"places": ["Tampa Bay", "Pinellas"]})
    assert g._prepare_guide(BRAND, SEED, "", guide=True) == ""
    assert g._guide is True and g._guide_places == ["Tampa Bay", "Pinellas"]


def test_prepare_guide_with_geography_entered_stands_the_service_area_rules_down():
    g, stub = _gen(call_handler=lambda p: {"places": ["Tampa Bay"]})
    assert g._prepare_guide(BRAND, SEED, "Florida", guide=True) == "Florida"
    assert g._guide_places == [] and not stub.calls, "no lookup when a geography was entered"


def test_a_seed_that_names_the_service_area_makes_it_the_geography():
    g, _ = _gen(call_handler=lambda p: {"places": ["Tampa Bay", "Pinellas"]})
    assert g._prepare_guide(BRAND, "how to renovate a house in Tampa Bay", "", guide=True) == "Tampa Bay"
    assert g._guide_places == []


def test_prepare_guide_off_makes_no_call_and_clears_the_state():
    g, stub = _gen()
    g._guide, g._guide_places = True, ["x"]
    assert g._prepare_guide(BRAND, SEED, "", guide=False) == ""
    assert g._guide is False and g._guide_places == [] and not stub.calls


# ── the local-evidence filter ─────────────────────────────────────────────────────────────────
def _blocks():
    return [
        {"label": "CMK Construction", "url": "https://www.cmkconstructioninc.com/about",
         "text": "Serving Tampa Bay, Hillsborough and Pinellas homeowners."},
        {"label": "official · Inspections | Hillsborough County, FL – Official County Site",
         "url": "https://hcfl.gov/businesses/permits-and-records/inspections",
         "text": "Schedule inspections through Development Services."},
        {"label": "third-party · Tampa Home Renovation | Onsite Construction",
         "url": "https://onsiteconstructionfl.com/tampa-home-renovation/", "text": "Tampa crews."},
        {"label": "official · International Residential Code",
         "url": "https://codes.iccsafe.org/content/IRC2021P1",
         "text": "Model code adopted by most states, including Florida, for one- and two-family homes."},
        {"label": "provided · pinellas.gov", "url": "https://pinellas.gov/permits/",
         "text": "Pinellas permits; Pinellas inspections."},
        {"label": "research notes (user-provided)", "url": "", "text": "Tampa Bay notes, Pinellas too."},
    ]


def test_the_county_permit_page_is_dropped_and_the_brand_and_national_sources_stay():
    g, _ = _guide_gen()
    kept = [b["url"] for b in g._guide_filter_blocks(_blocks(), BRAND, "test")]
    assert "https://hcfl.gov/businesses/permits-and-records/inspections" not in kept
    assert "https://onsiteconstructionfl.com/tampa-home-renovation/" not in kept
    assert "https://www.cmkconstructioninc.com/about" in kept, "the brand's own Tampa page stays"
    assert "https://codes.iccsafe.org/content/IRC2021P1" in kept, "a national source that names a state once stays"
    assert "" in kept, "research notes the operator typed stay"
    assert "https://pinellas.gov/permits/" not in kept, "a stored known source is filtered"


def test_a_url_the_operator_typed_for_this_blog_is_never_filtered():
    g, _ = _guide_gen()
    g._guide_keep_urls = {"https://pinellas.gov/permits"}
    kept = [b["url"] for b in g._guide_filter_blocks(_blocks(), BRAND, "test")]
    assert "https://pinellas.gov/permits/" in kept


def test_the_filter_is_inert_with_a_geography_or_off():
    g, _ = _gen()
    g._guide, g._guide_places = True, []   # geography entered → no places
    assert len(g._guide_filter_blocks(_blocks(), BRAND, "t")) == len(_blocks())
    g._guide, g._guide_places = False, list(PLACES)
    assert len(g._guide_filter_blocks(_blocks(), BRAND, "t")) == len(_blocks())


def test_the_gathered_evidence_carries_no_local_page(monkeypatch):
    g, stub = _guide_gen(call_handler=lambda p: {"domains": {}})
    pages = {"https://www.cmkconstructioninc.com": "CMK Construction remodels kitchens in Tampa Bay.",
             "https://hcfl.gov/permits": "Hillsborough County permits. Hillsborough County inspections."}
    monkeypatch.setattr(BG, "_fetch_url", lambda self, u: pages.get(u.rstrip("/"), ""))
    g._guide_keep_urls = {"https://hcfl.gov/permits"}   # what generate_blog records for a typed URL
    g._gather_evidence(BRAND, SEED, source_urls=["https://hcfl.gov/permits"])
    urls = [b["url"] for b in g._evidence_blocks]
    assert "https://hcfl.gov/permits" in urls, "typed this generation → kept"
    g2, _ = _guide_gen(call_handler=lambda p: {"domains": {}})
    g2._guide_keep_urls = set()
    monkeypatch.setattr(BG, "_fetch_url", lambda self, u: pages.get(u.rstrip("/"), ""))
    g2._gather_evidence(BRAND, SEED, source_urls=["https://hcfl.gov/permits"])
    assert "https://hcfl.gov/permits" not in [b["url"] for b in g2._evidence_blocks]


# ── keywords ──────────────────────────────────────────────────────────────────────────────────
def test_location_keywords_are_dropped_and_generic_ones_kept():
    def _h(p):
        return {"title": SEED, "meta_description": "m", "body_markdown": "# x\n", "disclosure": "d",
                "keywords": ["Tampa Bay home renovation", "renovation sequence"]}
    g, _ = _guide_gen(call_handler=_h)
    res = g.generate_article(BRAND, SEED, extra_keywords=["remodeling order Tampa Bay",
                                                          "house renovation order"], guide=True)
    assert "house renovation order" in g._article_prompt
    assert "remodeling order Tampa Bay" not in g._article_prompt
    assert res["keywords"] == ["house renovation order", "renovation sequence"]


# ── evidence: no competitor machinery ─────────────────────────────────────────────────────────
def test_a_guide_fetches_no_competitor_and_derives_no_peers_but_resolves_a_seed_product(monkeypatch):
    fetched, asked = [], {}
    monkeypatch.setattr(BG, "_fetch_url", lambda self, u: (fetched.append(u) or
                                                           ("Schluter Kerdi waterproofing membrane "
                                                            "renovation bathroom" if "schluter" in u else "")))

    def _resolve(self, names, seed=None, subject=None, subject_category=None, want_subject=False):
        asked.update(names=list(names), want_subject=want_subject)
        return {"Schluter": "schluter.com"}
    monkeypatch.setattr(BG, "_resolve_brand_domains", _resolve)
    monkeypatch.setattr(BG, "_gather_independent_sources",
                        lambda self, *a, **k: pytest.fail("no peer / competitor sweep in a guide"))

    class _DB:
        def update_brand(self, *a, **k):
            pytest.fail("a guide never writes a named product back as a competitor")
    g, _ = _guide_gen()
    g.db = _DB()
    g._gather_evidence(BRAND, "how to waterproof a shower with Schluter", use_web_search=True)
    assert asked == {"names": [], "want_subject": False}
    assert not any("elitebuilderrenovation" in u for u in fetched)
    assert any("schluter.com" in u for u in fetched), "a product the seed names still resolves"


# ── sourcing: no per-tool search, no ledger, corroboration still runs ─────────────────────────
def _sourcing_call(p):
    if "extract for verification" in p:
        return {"tools": LOCAL_CONTRACTORS, "peer_tools": LOCAL_CONTRACTORS,
                "dimensions": ["Sequencing approach", "Price"], "products": ["waterproofing membrane"],
                "core_topic": "residential building code inspection sequence", "subject": "",
                "core_mechanics": [], "generic_options": [],
                "claims": [{"brand": "", "dimension": "inspections", "claim": "rough-in inspected",
                            "value": "before drywall"}]}
    return {}


def test_a_guide_sources_no_competitor_and_still_corroborates(monkeypatch):
    monkeypatch.setattr(BG, "_ensure_price_ledger", lambda *a, **k: pytest.fail("no price ledger"))
    brand = dict(BRAND, price_table=json.dumps({"revive": {"name": "Revive Kitchen & Bath", "rows": [
        {"product": "bath", "kind": "exact", "value": "$9,000", "raw": "$9,000"}]}}))
    g, stub = _guide_gen(call_handler=_sourcing_call, search_handler=lambda b, a, bl: [])
    g._evidence_blocks = [{"label": "CMK Construction", "url": "https://www.cmkconstructioninc.com",
                           "text": "CMK"}]
    sr = g._source_for_completion(brand, SEED, {"body_markdown": REAL_BODY}, guide=True)
    assert sr["tools"] == [] and sr["peers"] == [] and sr["unsourced"] == []
    assert sr["guide"] is True and sr["service_area"] == PLACES
    briefs = [s["brief"] for s in stub.searches]
    for c in LOCAL_CONTRACTORS + ["Revive Kitchen & Bath", "Lindross"]:
        assert not any(c.split()[0] in b for b in briefs), f"a search was bought for {c}"
    assert any("standards and code bodies" in b and "NATIONAL" in b for b in briefs), "corroboration ran"
    assert any("NATIONAL or GENERAL authority" in b for b in briefs), "the official brief is national"
    ext = next(c for c in stub.calls if "extract for verification" in c)
    assert "FU216: this article is a GENERAL GUIDE with NO geography" in ext


def test_a_guide_with_geography_entered_asks_for_that_geography_not_a_national_authority():
    g, stub = _gen(call_handler=_sourcing_call, search_handler=lambda b, a, bl: [])
    g._guide, g._guide_places = True, []
    sr = g._source_for_completion(BRAND, SEED, {"body_markdown": REAL_BODY}, geo="Florida", guide=True)
    assert sr["tools"] == [] and sr["service_area"] == []
    briefs = [s["brief"] for s in stub.searches]
    assert not any("NATIONAL" in b for b in briefs)
    assert any("Florida-specific guidance" in b for b in briefs)


def test_local_corroboration_results_are_filtered():
    def _search(brief, allowed, blocked):
        return [{"title": "Inspections | Hillsborough County", "url": "https://hcfl.gov/inspections",
                 "fact": "County inspections."},
                {"title": "IRC R105 permits", "url": "https://codes.iccsafe.org/r105",
                 "fact": "Residential building code: permits are required for alterations."}]
    g, _ = _guide_gen(call_handler=_sourcing_call, search_handler=_search)
    sr = g._source_for_completion(BRAND, SEED, {"body_markdown": REAL_BODY}, guide=True)
    urls = [f["url"] for f in sr["fresh"]]
    assert "https://hcfl.gov/inspections" not in urls
    assert "https://codes.iccsafe.org/r105" in urls


# ── reconcile ─────────────────────────────────────────────────────────────────────────────────
def test_the_guide_reconcile_has_no_floor_and_carries_the_guide_rule():
    g, stub = _gen(call_handler=lambda p: {"revised_body_markdown": "# x\n", "flagged": []})
    sourcing = {"name": "CMK Construction", "cat": "c", "tools": [], "peers": [], "dims": [],
                "claims": [], "fresh": [{"label": "official · IRC", "url": "u", "text": "t"}],
                "guide": True, "service_area": ["Tampa Bay", "Pinellas"]}
    g._reconcile_and_finish(BRAND, SEED, {"body_markdown": REAL_BODY}, sourcing)
    p = stub.calls[-1]
    assert "COMPETITOR FLOOR" not in p and "ENTITY-TYPE (FU98)" not in p and "PEERS: none" in p
    assert "name the POOL" not in p
    assert "GENERAL GUIDE (FU216" in p and "CMK Construction operates in Tampa Bay, Pinellas" in p
    sourcing.pop("guide"), sourcing.pop("service_area")
    g._reconcile_and_finish(BRAND, SEED, {"body_markdown": REAL_BODY}, sourcing)
    assert "COMPETITOR FLOOR" in stub.calls[-1] and "GENERAL GUIDE" not in stub.calls[-1]


# ── finalize: the guide-check, driven by the real export ──────────────────────────────────────
def test_the_real_export_trips_the_guide_check_on_every_zone():
    g, _ = _guide_gen()
    art = {"title": SEED, "body_markdown": REAL_BODY,
           "meta_description": "CMK Construction guides Tampa Bay homeowners through every phase."}
    note = g._guide_check(art, BRAND, SEED)
    assert note.startswith("guide-check:")
    for zone in ("the meta description", "heading", "the Quick answer", "the FAQ", "body paragraphs"):
        assert zone in note, zone
    assert "Elite Builder Renovation" in note and "Lindross Remodeling" in note
    assert "Revive Kitchen & Bath" in note


CLEAN = """*[Add author byline before publishing]*

# In What Order Should You Renovate a House?

Quick answer: plan and permit first, then demolition, rough-ins, waterproofing, drywall, floors,
cabinets, counters, fixtures and a final inspection.

## Why does the order matter?

Each phase creates the conditions the next depends on. Check with your local building department
for the inspections your project needs.

| Phase | Why it comes here |
|---|---|
| Rough-in | must be inspected before walls close |
| Drywall | closes the walls |
| Flooring | cabinets sit on finished floor |

## Ready to plan your remodel?

CMK Construction remodels kitchens and bathrooms across Tampa Bay — book a design session.

## FAQ

### Should the kitchen come first?

Usually, because it carries more structural and electrical scope.
"""


def test_a_clean_guide_with_one_cta_paragraph_is_silent():
    g, _ = _guide_gen()
    art = {"title": SEED, "body_markdown": CLEAN, "meta_description": "The renovation order, step by step."}
    assert g._guide_check(art, BRAND, SEED) == ""


def test_the_guide_check_ignores_places_once_a_geography_was_entered():
    g, _ = _gen()
    g._guide, g._guide_places = True, []
    art = {"title": SEED, "body_markdown": CLEAN.replace("across Tampa Bay", "across Tampa Bay. Pinellas"),
           "meta_description": "x"}
    assert g._guide_check(art, BRAND, SEED, geo="Florida") == ""


def test_finalize_suppresses_the_comparison_checks_and_adds_the_guide_check():
    g, _ = _guide_gen()
    art = {"title": SEED, "body_markdown": REAL_BODY, "meta_description": "Tampa Bay homeowners"}
    g._peer_note = "peer-check: something"
    g._finalize_article(BRAND, SEED, art, REAL_BODY, with_linkedin=False, guide=True)
    joined = " | ".join(w.get("detail", "") if isinstance(w, dict) else str(w)
                        for w in (art.get("warnings") or []))
    assert "guide-check:" in joined
    assert "competitor-check" not in joined and "peer-check" not in joined
    assert "your-competitors" not in joined
    assert art.get("guide") is True
    assert "comparison" not in {c["key"] for c in art["quality_report"]["checks"]}


def test_finalize_off_keeps_the_competitor_floor_and_no_guide_check():
    g, _ = _gen()
    body = REAL_BODY.replace("| Revive Kitchen & Bath", "| CMK Construction").replace(
        "| S&W Kitchens", "| CMK Construction").replace("| Bath World", "| CMK Construction")
    art = {"title": SEED, "body_markdown": body, "meta_description": "m"}
    g._finalize_article(BRAND, SEED, art, body, with_linkedin=False)
    joined = " | ".join(w.get("detail", "") if isinstance(w, dict) else str(w)
                        for w in (art.get("warnings") or []))
    assert "competitor-check" in joined and "guide-check" not in joined


def test_the_substance_guard_cannot_restore_a_contractor_section_to_a_guide():
    g, _ = _guide_gen()
    revised = REAL_BODY.split("## How Do Tampa Bay Renovation Contractors")[0] + \
        "## FAQ\n\n### Q?\n\nA.\n"
    art = {"title": SEED, "body_markdown": revised, "meta_description": "m"}
    g._finalize_article(BRAND, SEED, art, REAL_BODY, with_linkedin=False, guide=True)
    assert "How Do Tampa Bay Renovation Contractors" not in art["body_markdown"]
    assert "Specialists in Tampa Bay Compare" not in art["body_markdown"]


def test_the_itemlist_is_suppressed_for_a_guide():
    blog = {"title": SEED, "seed": SEED, "body_markdown": REAL_BODY, "meta_description": "m",
            "created_at": "2026-09-18 10:00:00", "updated_at": "2026-09-18 10:00:00"}
    types = lambda b: {n.get("@type") for n in build_blog_jsonld(b, BRAND)["@graph"]}
    assert "ItemList" in types(dict(blog, guide=0))
    assert "ItemList" not in types(dict(blog, guide=1))


# ── LinkedIn + resume ─────────────────────────────────────────────────────────────────────────
def test_the_guide_linkedin_post_has_no_against_interest_line():
    g, stub = _guide_gen(call_handler=lambda p: {"linkedin_text": "post"})
    g.generate_linkedin(BRAND, SEED, {"title": SEED, "body_markdown": CLEAN}, guide=True)
    p = stub.calls[-1]
    assert "AGAINST-INTEREST" not in p and "Name no company" in p
    assert "operates in Tampa Bay" in p
    g2, stub2 = _gen(call_handler=lambda p: {"linkedin_text": "post"})
    g2.generate_linkedin(BRAND, SEED, {"title": SEED, "body_markdown": CLEAN})
    assert "AGAINST-INTEREST LINE (mandatory)" in stub2.calls[-1]


def test_a_paused_guide_resumes_as_a_guide(monkeypatch):
    g, _ = _guide_gen()
    sourcing = {"name": "CMK Construction", "cat": "c", "tools": [], "peers": [], "dims": [],
                "claims": [], "fresh": [], "unsourced": [{"tool": "official · x", "facts": ["f"],
                                                          "ymyl_official": True}],
                "guide": True, "service_area": ["Tampa Bay"]}
    ck = g._pause_sentinel(sourcing, {"title": SEED, "body_markdown": CLEAN}, CLEAN)["checkpoint"]
    ck = json.loads(json.dumps(ck))
    assert ck["sourcing"]["guide"] is True and ck["sourcing"]["service_area"] == ["Tampa Bay"]
    seen = {}

    def _fin(self, brand, seed, article, draft_body, **kw):
        seen.update(kw, places=list(self._guide_places), guide_attr=self._guide)
        return article
    monkeypatch.setattr(BG, "_finalize_article", _fin)
    fresh, _ = _gen()
    fresh.finish_pending_blog(BRAND, SEED, ck, [])
    assert seen["guide"] is True and seen["guide_attr"] is True and seen["places"] == ["Tampa Bay"]


# ── db + API ──────────────────────────────────────────────────────────────────────────────────
def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _seed_db(path, guide=None):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "CMK Construction")
    blog_id = db.save_blog(bid, SEED, title=SEED, body_markdown=CLEAN, status="draft")
    if guide is not None:
        db.update_blog(blog_id, guide=guide)
    db.close()
    return bid, blog_id


def _row(path, blog_id):
    db = Database(path)
    db.connect()
    try:
        return db.get_blog(blog_id)
    finally:
        db.close()


def test_the_guide_column_round_trips_and_defaults_off():
    path = _tmp()
    try:
        _, blog_id = _seed_db(path)
        assert _row(path, blog_id)["guide"] == 0, "every existing blog stays a comparison"
        db = Database(path)
        db.connect()
        db.update_blog(blog_id, guide=1)
        db.close()
        assert _row(path, blog_id)["guide"] == 1
    finally:
        os.unlink(path)


@pytest.fixture
def app_client(monkeypatch):
    import app as appmod
    box = {"calls": []}

    def _inline(_name, fn, **kw):
        box["result"] = fn(_task_id="t") if kw.get("pass_task_id") else fn()
        return "t"
    monkeypatch.setattr(appmod, "start_task", _inline)
    monkeypatch.setattr(appmod, "ClaudeClient", lambda *a, **k: StubClaude())
    monkeypatch.setattr(appmod, "_ensure_brand_byline_logo", lambda c, db, b: b)
    monkeypatch.setattr(appmod, "_ensure_content_context", lambda c, db, b: b)
    monkeypatch.setattr(appmod, "_build_blog_writer", lambda db: (None, "off"))
    monkeypatch.setattr(appmod, "_blog_reddit_evidence", lambda *a, **k: (None, "skipped"))
    monkeypatch.setattr(BG, "_brand_service_area", lambda self, b: ["Tampa Bay"])

    def _gen_blog(self, brand, seed, **kw):
        box["calls"].append(("generate_blog", kw.get("guide")))
        return {"title": seed, "meta_description": "m", "keywords": [], "body_markdown": CLEAN,
                "claims_flagged": [], "linkedin_text": "", "gen_cost": 0}
    monkeypatch.setattr(BG, "generate_blog", _gen_blog)

    def _client(path):
        appmod.DB_PATH = path
        appmod._db_initialized = True
        return appmod.app.test_client()
    return _client, box


def test_generate_persists_the_guide_choice(app_client):
    make, box = app_client
    path = _tmp()
    try:
        bid, _ = _seed_db(path)
        cli = make(path)
        assert cli.post("/api/blogs/generate", json={"brand_id": bid, "seed": SEED,
                                                     "guide": True}).status_code == 200
        assert box["calls"][-1] == ("generate_blog", True)
        assert _row(path, box["result"]["blog_id"])["guide"] == 1
        cli.post("/api/blogs/generate", json={"brand_id": bid, "seed": SEED})
        assert box["calls"][-1] == ("generate_blog", False)
        assert _row(path, box["result"]["blog_id"])["guide"] == 0
    finally:
        os.unlink(path)


def test_regenerate_reuses_the_stored_guide_choice(app_client, monkeypatch):
    make, box = app_client
    path = _tmp()
    try:
        _, blog_id = _seed_db(path, guide=1)
        monkeypatch.setattr(BG, "_gather_evidence", lambda self, *a, **k: "")
        cli = make(path)
        assert cli.post(f"/api/blogs/{blog_id}/regenerate", json={"part": "all"}).status_code == 200
        assert box["calls"][-1] == ("generate_blog", True)
    finally:
        os.unlink(path)


def test_regenerate_article_threads_the_guide_through_every_step(app_client, monkeypatch):
    make, box = app_client
    path = _tmp()
    seen = {}
    try:
        _, blog_id = _seed_db(path, guide=1)
        monkeypatch.setattr(BG, "_gather_evidence",
                            lambda self, *a, **k: seen.update(places_at_evidence=list(self._guide_places)) or "")

        def _ga(self, *a, **k):
            seen["article"] = k.get("guide")
            return {"title": SEED, "meta_description": "m", "meta_title": "", "keywords": [],
                    "body_markdown": CLEAN}
        monkeypatch.setattr(BG, "generate_article", _ga)
        monkeypatch.setattr(BG, "verify_claims", lambda self, b, a, evidence="": None)

        def _vc(self, *a, **k):
            seen["verify"] = k.get("guide")
            return None
        monkeypatch.setattr(BG, "verify_and_complete", _vc)
        _orig_fin = BG._finalize_article

        def _fin(self, *a, **k):
            seen["finalize"] = k.get("guide")
            return _orig_fin(self, *a, **k)
        monkeypatch.setattr(BG, "_finalize_article", _fin)
        cli = make(path)
        assert cli.post(f"/api/blogs/{blog_id}/regenerate", json={"part": "article"}).status_code == 200
        assert seen == {"places_at_evidence": ["Tampa Bay"], "article": True, "verify": True,
                        "finalize": True}
    finally:
        os.unlink(path)


def test_patch_flips_the_guide_choice(app_client):
    make, _ = app_client
    path = _tmp()
    try:
        _, blog_id = _seed_db(path)
        cli = make(path)
        assert cli.patch(f"/api/blogs/{blog_id}", json={"guide": True}).get_json()["guide"] == 1
        assert cli.patch(f"/api/blogs/{blog_id}", json={"guide": False}).get_json()["guide"] == 0
    finally:
        os.unlink(path)


def test_the_matcher_joins_multi_word_places_in_slugs_and_domains():
    assert _mentions_area("renovatetampabay.com", ["Tampa Bay"]) is False   # glued into a longer word
    assert _mentions_area("https://tampabay.example/x", ["Tampa Bay"])
    assert _mentions_area("tampa-bay-remodel", ["Tampa Bay"])
    assert not _mentions_area("Pascoe Vale", ["Pasco"])
