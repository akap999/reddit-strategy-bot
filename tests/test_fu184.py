"""FU184 — a competitor the model INVENTED to hit the floor of 3 must be FLAGGED, the dimension
rescue must label its sources honestly, and the affiliate filter must read the URL as well as the
title. $0, no network (StubClaude). Driven by the real reported case: a live PeterMD blog compared
"Calibrate", which is on neither the brand's competitor list nor in any gathered evidence."""
import json

from generators.blog_gen import BlogGenerator, _is_affiliate_review
from tests.stubs import StubClaude

SEED = "which telehealth platforms offer doctor-led glp-1 programs"
BODY = ("## Quick answer\nx\n\n| Platform | Pricing |\n|---|---|\n"
        "| PeterMD | $149 |\n| Ro | ? |\n| Noom Med | ? |\n| Calibrate | ? |\n")


def _brand(competitors, **extra):
    b = {"name": "PeterMD", "domain_url": "https://getpetermd.com", "category": "telehealth",
         "competitors": competitors}
    b.update(extra)
    return b


def _source(brand, tools, *, dims=("pricing",), evidence=None, search_h=None, domains=None):
    """Run the real _source_for_completion with a scripted extraction. Returns (gen, sourcing)."""
    doms = domains or {t: t.lower().replace(" ", "") + ".com" for t in tools}

    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": list(tools), "peer_tools": list(tools), "dimensions": list(dims),
                    "products": [], "claims": [], "core_topic": "glp-1 programs"}
        if '"domains"' in p:
            return {"domains": dict(doms)}
        return {}

    gen = BlogGenerator(StubClaude(call_handler=call_h,
                                   search_handler=(search_h or (lambda b, a, bl: []))), db=None)
    gen._evidence_blocks = list(evidence or [])
    return gen, gen._source_for_completion(brand, SEED, {"body_markdown": BODY}, ymyl=None)


# --- Change 1: the reported case — Calibrate is flagged as model-named -------------------------

def test_invented_competitor_is_flagged():
    """The live case: the list says Ro + Noom; the draft compares Ro, Noom Med and Calibrate.
    "Noom Med" is the curated Noom carrying a product suffix, so only Calibrate is model-named."""
    _, sourcing = _source(_brand(["Ro", "Noom"]), ["Ro", "Noom Med", "Calibrate"])
    assert sourcing["invented_tools"] == ["Calibrate"]


def test_a_short_curated_name_does_not_swallow_an_unrelated_tool():
    """Curated "Ro" must not mark "Rory" as curated — the suffix match lands on a hyphen boundary."""
    _, sourcing = _source(_brand(["Ro"]), ["Rory"])
    assert sourcing["invented_tools"] == ["Rory"]


def test_curated_competitors_are_not_flagged():
    """Only the names actually on the brand's list (or in the evidence) escape the flag."""
    _, sourcing = _source(_brand(["Ro", "Noom Med"]), ["Ro", "Noom Med"])
    assert sourcing["invented_tools"] == []


def test_tool_named_in_gathered_evidence_counts_as_backed_not_invented():
    ev = [{"label": "third-party · GLP-1 roundup", "url": "https://health.usnews.com/x",
           "text": "Calibrate runs a metabolic-reset program with clinician visits."}]
    _, sourcing = _source(_brand(["Ro"]), ["Ro", "Calibrate"], evidence=ev)
    assert sourcing["invented_tools"] == []


def test_invented_note_reaches_geo_warning():
    gen, sourcing = _source(_brand(["Ro", "Noom"]), ["Ro", "Noom Med", "Calibrate"])
    art = {"body_markdown": BODY, "meta_description": "m"}
    out = gen._finalize_article(_brand(["Ro", "Noom"]), SEED, art, BODY)
    w = out.get("geo_warning") or ""
    assert "competitor-check:" in w and "Calibrate" in w
    assert "Edit Brand" in w


def test_no_note_when_every_tool_is_curated():
    gen, _ = _source(_brand(["Ro", "Noom Med"]), ["Ro", "Noom Med"])
    art = {"body_markdown": BODY, "meta_description": "m"}
    out = gen._finalize_article(_brand(["Ro", "Noom Med"]), SEED, art, BODY)
    assert "competitor-check:" not in (out.get("geo_warning") or "")


# --- the JSON-STRING trap (what production actually supplies) -----------------------------------

def test_competitors_stored_as_a_json_string_still_classify_as_curated():
    """db.get_brand returns dict(row) with NO JSON parsing, so brand['competitors'] is a STRING.
    A list() implementation would iterate CHARACTERS and mark every tool invented."""
    brand = _brand(json.dumps(["Ro", "Noom Med"]))
    assert isinstance(brand["competitors"], str)
    _, sourcing = _source(brand, ["Ro", "Noom Med", "Calibrate"])
    assert sourcing["invented_tools"] == ["Calibrate"]


def test_competitors_as_a_comma_string_still_classify_as_curated():
    _, sourcing = _source(_brand("Ro, Noom Med"), ["Ro", "Noom Med", "Calibrate"])
    assert sourcing["invented_tools"] == ["Calibrate"]


# --- Change 2: the dimension rescue labels by the SOURCE, not the tool --------------------------

def _dim_rescue_source(result_url, result_title, *, tool="Calibrate", tool_domain="calibrate.com"):
    """Give every tool a bland own-site block (so nothing is 'unsourced' and the pricing dimension is
    still missing), then answer ONLY the dimension-rescue brief with `result_url`."""
    def search_h(brief, allowed, blocked):
        if "the specific, current value/details" in brief:          # the dim-rescue brief
            return [{"url": result_url, "title": result_title, "fact": f"{tool} plans start at $199/month"}]
        if allowed:                                                  # Tier-1 own-site baseline
            d = str(allowed[0])
            return [{"url": f"https://{d}/about", "title": f"{d} about",
                     "fact": f"{d.split('.')[0]} is a doctor-led metabolic program."}]
        return []

    brand = _brand(["Ro", "Noom Med", tool])
    return _source(brand, [tool], dims=("pricing",), search_h=search_h,
                   domains={tool: tool_domain})


def test_dim_rescue_keeps_the_tool_name_on_its_own_domain():
    _, sourcing = _dim_rescue_source("https://calibrate.com/pricing", "Calibrate Pricing")
    labels = [f["label"] for f in sourcing["fresh"] if "calibrate.com/pricing" in f["url"]]
    assert labels == ["Calibrate"]


def test_dim_rescue_labels_a_third_party_page_honestly():
    """The real shape: a healthynexercise.com page about Calibrate must NOT be labelled 'Calibrate'."""
    _, sourcing = _dim_rescue_source("https://healthynexercise.com/calibrate-plans", "Calibrate plans 2026")
    hits = [f for f in sourcing["fresh"] if "healthynexercise.com" in f["url"]]
    assert hits, "the third-party rescue result should still be kept, just labelled honestly"
    assert all(f["label"] != "Calibrate" for f in hits)
    assert all(f["label"].startswith("third-party · ") for f in hits)


def test_dim_rescue_drops_a_review_shaped_third_party_url():
    """The exact live source: an affiliate review slug on a non-reputable domain is now dropped."""
    _, sourcing = _dim_rescue_source(
        "https://healthynexercise.com/calibrate-weight-loss-program-review/", "Calibrate plans 2026")
    assert not [f for f in sourcing["fresh"] if "healthynexercise.com" in f["url"]]


# --- Change 3: _is_affiliate_review reads the URL path too --------------------------------------

def test_review_shaped_url_with_an_innocuous_title_is_caught():
    src = {"url": "https://healthynexercise.com/calibrate-weight-loss-program-review/",
           "title": "Calibrate weight loss program 2026"}          # title carries no review word
    assert _is_affiliate_review(src)


def test_review_shaped_url_on_the_competitors_own_site_is_kept():
    src = {"url": "https://calibrate.com/reviews", "title": "Calibrate member stories"}
    assert not _is_affiliate_review(src, own_domain="calibrate.com")


def test_review_shaped_url_on_a_reputable_domain_is_kept():
    assert not _is_affiliate_review({"url": "https://www.forbes.com/health/calibrate-review/",
                                     "title": "Calibrate"})
    assert not _is_affiliate_review({"url": "https://health.usnews.com/calibrate-review",
                                     "title": "Calibrate"})


def test_ordinary_urls_are_not_caught():
    assert not _is_affiliate_review({"url": "https://someblog.com/calibrate-pricing",
                                     "title": "Calibrate pricing 2026"})
    assert not _is_affiliate_review({"url": "https://auanet.org/guidelines/best-practice-statement",
                                     "title": "Testosterone Deficiency Guideline"})


# --- Change 1a: the writer prompt exhausts the curated list before inventing ---------------------

def test_prompt_requires_the_curated_list_first():
    brand = _brand(["Ro", "Noom Med"])
    gen = BlogGenerator(StubClaude(call_handler=lambda p: {
        "title": SEED, "meta_description": "m", "keywords": [],
        "body_markdown": "## Quick answer\nx", "disclosure": "d"}), db=None)
    gen.generate_article(brand, SEED)
    flat = " ".join(next(p for p in gen.claude.calls if "MINIMUM COMPETITORS (FU105" in p).split())
    # the floor itself is UNCHANGED (removing it resurrects the self-crowning two-competitor table)
    assert "AT LEAST 3 REAL competitors" in flat
    assert "Skip this rule ONLY when the article genuinely contains no comparison" in flat
    # ordering: the operator's curated list is exhausted first, free-naming is the last step
    assert 'name EVERY curated competitor that genuinely fits' in flat
    assert "ONLY IF the floor is still short" in flat
    # a name the model adds itself must still be trading in this model today
    assert "CURRENT, actively-operating provider" in flat
    assert "a shorter honest field beats a plausible-sounding wrong peer" in flat
