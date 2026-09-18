"""FU218 — a General guide must still ANSWER its question with evidence.

The case that started it: PeterMD's "Which GLP-1 is best for weight loss?" was generated with General
guide ON and YMYL ON. It carried no efficacy figure and no trial name; its Quick answer deferred ("not
a one-size-fits-all ranking… a physician's assessment"); its table compared mechanism, not outcomes.
FU216 had dropped EVERY compared entity from a guide, the options with the providers, and skipped the
evidence sweep with nothing in its place. That export is the fixture (fu218_glp1_guide_body.md).

The operator's rule for this round: every fix is GENERAL. So each behaviour is exercised across four
verticals — the medical repro, a contractor, a SaaS brand and a lender — and a genericity test proves
the new code and prompt text name no vertical, topic or drug.

Everything is a StubClaude, $0. The OFF path is byte-identical (FU205 goldens + the HEAD-diff script).
"""
import inspect
import os
import re

import pytest

from generators.blog_gen import (BlogGenerator as BG, _YMYL_OFFICIAL_DOMAINS, _answer_evidence_brief)
from tests.stubs import StubClaude

_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "fu218_glp1_guide_body.md")
GLP1_BODY = open(_FIX, encoding="utf-8").read()

# One comparative guide per vertical: the brand, its YMYL vertical (or None), the question, the two
# OPTIONS it compares, one PROVIDER the draft happens to name, an outcome dimension, and the page that
# reports the answer.
VERTICALS = {
    "medical": dict(
        brand={"id": 1, "name": "PeterMD", "domain_url": "https://getpetermd.com",
               "category": "men's telehealth clinic"},
        ymyl="medical", seed="Which GLP-1 is best for weight loss?",
        options=["semaglutide", "tirzepatide"], provider="Ro", dim="Average weight loss",
        good={"title": "Tirzepatide versus semaglutide for obesity: head-to-head trial",
              "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1234567/",
              "fact": "Tirzepatide produced 20.2% mean weight loss versus 13.7% for semaglutide at 72 weeks."},
        figure="Tirzepatide produced 20.2% mean weight loss versus 13.7% for semaglutide [S1]."),
    "construction": dict(
        brand={"id": 2, "name": "CMK Construction", "domain_url": "https://www.cmkconstructioninc.com",
               "category": "remodeling contractor"},
        ymyl=None, seed="Which concrete sealer is best for a garage floor?",
        options=["penetrating silane sealer", "epoxy coating"], provider="SealPro Supply",
        dim="Water absorption reduction",
        good={"title": "Independent test: penetrating sealers versus epoxy coatings on garage slabs",
              "url": "https://www.concretenetwork.com/sealer-tests",
              "fact": "A silane sealer cut concrete water absorption by 85% versus 60% for an epoxy coating."},
        figure="A silane sealer cut water absorption by 85% and cures in 24 hours [S1]."),
    "saas": dict(
        brand={"id": 3, "name": "Taskly", "domain_url": "https://taskly.io",
               "category": "project management software"},
        ymyl=None, seed="Which project management method works best for small teams?",
        options=["Scrum", "Kanban"], provider="Asana", dim="Delivery cycle time",
        good={"title": "Kanban and Scrum in small teams: a field study",
              "url": "https://www.researchgate.net/publication/kanban-scrum-small-teams",
              "fact": "Small Kanban teams cut cycle time by 32% versus 18% for Scrum teams."},
        figure="Small Kanban teams cut cycle time by 32% versus 18% for Scrum [S1]."),
    "finance": dict(
        brand={"id": 4, "name": "LendRight", "domain_url": "https://lendright.com",
               "category": "equipment financing lender"},
        ymyl="finance", seed="Which loan type is best for buying equipment?",
        options=["equipment loan", "equipment lease"], provider="Balboa Capital",
        dim="Ownership at term end",
        good={"title": "Equipment financing: loans and leases compared",
              "url": "https://www.consumerfinance.gov/small-business/equipment-financing/",
              "fact": "Equipment loans averaged a 6.5% APR versus an implied 9.1% for equipment leases."},
        figure="Equipment loans averaged a 6.5% APR versus 9.1% for leases [S1]."),
}
NAMES = sorted(VERTICALS)

# Words the NEW code and prompt text must never carry — the proof that nothing keys on one vertical.
_VERTICAL_WORDS = re.compile(r"drug|dose|therap|clinical|\bfda\b|glp|pubmed|semaglutide|tirzepatide|"
                             r"weight loss|doctor|physician", re.IGNORECASE)


def _gen(v=None, call_handler=None, search_handler=None):
    stub = StubClaude(call_handler=call_handler or (lambda p: {}), search_handler=search_handler)
    g = BG(stub, db=None)
    if v is not None:
        g._guide, g._guide_places, g._ci_ymyl = True, [], v["ymyl"]
    return g, stub


def _is_answer_brief(b):
    return "INDEPENDENT evidence that ANSWERS" in (b or "")


# ── Change 1: the pre-draft answer evidence ──────────────────────────────────────────────────
@pytest.mark.parametrize("name", NAMES)
def test_one_answer_evidence_search_keeps_the_page_that_reports_the_answer(name):
    v = VERTICALS[name]
    pins = _YMYL_OFFICIAL_DOMAINS.get(v["ymyl"] or "") or []

    def _search(brief, allowed, blocked):
        return [v["good"]]
    g, stub = _gen(v, search_handler=_search)
    out = g._guide_answer_evidence(v["brand"], v["seed"])
    answer_searches = [s for s in stub.searches if _is_answer_brief(s["brief"])]
    assert len(answer_searches) == 1, "ONE search when the first attempt keeps something"
    first = answer_searches[0]
    assert v["seed"] in first["brief"]
    assert "COMPARATIVE results" in first["brief"], "a which / best question asks for comparative results"
    if pins:
        assert first["allowed"] == pins, "a YMYL page tries its vertical's EXISTING pins first"
    else:
        assert first["allowed"] is None and "getpetermd" not in str(first["blocked"])
        assert any(v["brand"]["domain_url"].split("//")[1].replace("www.", "") in d
                   for d in first["blocked"]), "the brand's own site is excluded"
    assert [b["url"] for b in out] == [v["good"]["url"]]
    label = out[0]["label"]
    official = name in ("medical", "finance")   # PMC (NIH) and consumerfinance.gov earn the badge
    assert label.startswith("official ·" if official else "third-party ·"), label


@pytest.mark.parametrize("name", ["medical", "finance"])
def test_a_ymyl_guide_keeps_only_official_pages_and_retries_unpinned(name):
    v = VERTICALS[name]
    blog = {"title": "The best options, reviewed and ranked", "url": "https://www.somehealthblog.com/x",
            "fact": v["good"]["fact"]}

    def _search(brief, allowed, blocked):
        return [] if allowed else [blog, v["good"]]   # the pinned attempt finds nothing
    g, stub = _gen(v, search_handler=_search)
    out = g._guide_answer_evidence(v["brand"], v["seed"])
    tags = ["pinned" if s["allowed"] else "open" for s in stub.searches if _is_answer_brief(s["brief"])]
    assert tags == ["pinned", "open"]
    assert [b["url"] for b in out] == [v["good"]["url"]]
    assert out[0]["label"].startswith("official ·")


def test_a_ymyl_guide_rejects_a_non_official_page_even_on_the_pinned_attempt():
    v = VERTICALS["medical"]
    blog = {"title": "GLP-1 guide", "url": "https://www.healthline.com/glp1", "fact": "weight loss 15%"}
    g, _ = _gen(v, search_handler=lambda b, a, bl: [blog, v["good"]])
    out = g._guide_answer_evidence(v["brand"], v["seed"])
    assert [b["url"] for b in out] == [v["good"]["url"]]


def test_the_existing_filters_apply_to_every_answer_evidence_result():
    v = VERTICALS["construction"]
    results = [
        {"title": "Worst garage floor sealers to avoid", "url": "https://floorblog.com/worst",
         "fact": "concrete sealer failures"},
        {"title": "Best Garage Floor Sealers of 2026", "url": "https://garagetopia.com/best-sealers",
         "fact": "concrete sealer picks"},
        {"title": "Our sealing service", "url": "https://www.cmkconstructioninc.com/sealing",
         "fact": "we seal concrete garage floors"},
        {"title": "Sealer install", "url": "https://elitebuilderrenovation.com/sealer",
         "fact": "concrete sealer install"},
        {"title": "Quarterly market update", "url": "https://markets.example.com/q3", "fact": "stocks rose 3%"},
        {"title": "Durability of concrete sealers", "url": "https://www.nist.gov/concrete-sealer-durability",
         "fact": "Silane-treated concrete absorbed 70% less chloride."},
        v["good"],
    ]
    g, stub = _gen(v, search_handler=lambda b, a, bl: results)
    out = g._guide_answer_evidence(v["brand"], v["seed"], ["elitebuilderrenovation.com"])
    assert [b["url"] for b in out] == ["https://www.nist.gov/concrete-sealer-durability", v["good"]["url"]]
    assert out[0]["label"].startswith("official ·") and out[1]["label"].startswith("third-party ·")
    blocked = stub.searches[0]["blocked"]
    assert "elitebuilderrenovation.com" in blocked and "cmkconstructioninc.com" in blocked


def test_the_brief_follows_the_guides_geography():
    q = VERTICALS["construction"]["seed"]
    assert "national or general sources" in _answer_evidence_brief(q, "")
    fl = _answer_evidence_brief(q, "Florida")
    assert "apply to Florida" in fl and "national" not in fl
    assert "COMPARATIVE" not in _answer_evidence_brief("In what order should you renovate a house?", "")


def test_prepare_guide_records_the_effective_geography():
    g, _ = _gen()
    brand = dict(VERTICALS["construction"]["brand"], context="")
    g._brand_service_area = lambda b: []
    g._prepare_guide(brand, "Which concrete sealer is best for a garage floor?", "Florida", guide=True)
    assert g._guide_geo == "Florida"
    g._prepare_guide(brand, "Which concrete sealer is best for a garage floor?", "", guide=True)
    assert g._guide_geo == ""
    g._prepare_guide(brand, "x", "Florida", guide=False)
    assert g._guide_geo == ""


@pytest.mark.parametrize("guide", [True, False])
def test_the_answer_evidence_runs_before_the_draft_only_for_a_guide(monkeypatch, guide):
    v = VERTICALS["construction"]
    monkeypatch.setattr(BG, "_fetch_url", lambda self, u: "")
    monkeypatch.setattr(BG, "_resolve_brand_domains", lambda self, *a, **k: {})
    monkeypatch.setattr(BG, "_gather_independent_sources", lambda self, *a, **k: [])
    g, stub = _gen(v if guide else None, search_handler=lambda b, a, bl: [v["good"]])
    ev = g._gather_evidence(v["brand"], v["seed"], use_web_search=True)
    answered = [s for s in stub.searches if _is_answer_brief(s["brief"])]
    if guide:
        assert len(answered) == 1 and v["good"]["url"] in ev, "the answer reaches the writer's EVIDENCE"
    else:
        assert answered == [] and v["good"]["url"] not in (ev or "")


def test_no_answer_evidence_search_when_web_search_is_off(monkeypatch):
    v = VERTICALS["construction"]
    monkeypatch.setattr(BG, "_fetch_url", lambda self, u: "")
    monkeypatch.setattr(BG, "_resolve_brand_domains", lambda self, *a, **k: {})
    g, stub = _gen(v, search_handler=lambda b, a, bl: [v["good"]])
    g._gather_evidence(v["brand"], v["seed"], use_web_search=False)
    assert not any(_is_answer_brief(s["brief"]) for s in stub.searches)


# ── Change 2: options survive, providers do not ──────────────────────────────────────────────
def _extract_handler(v, extra_tools=None, generic=True, products=None):
    def _h(p):
        if "extract for verification" in p:
            return {"tools": list(v["options"]) + [v["provider"]] + list(extra_tools or []),
                    "peer_tools": [], "dimensions": [v["dim"]], "products": list(products or []),
                    "core_topic": "", "subject": "", "core_mechanics": [],
                    "generic_options": list(v["options"]) if generic else [],
                    "claims": []}
        return {}
    return _h


def _option_search(v):
    """Reference results for each option (naming it but not the outcome dimension), and a measured
    result for each option's dimension-rescue brief."""
    def _s(brief, allowed, blocked):
        for o in v["options"]:
            if brief.startswith(f'{o} for the question'):
                return [{"title": f"{o} overview", "url": f"https://reference.example.org/{o.split()[0]}",
                         "fact": f"{o} described by a reference source"}]
            if brief.startswith(f"{o}: {v['dim']} — the measured value"):
                return [{"title": f"{o} study", "url": v["good"]["url"], "fact": f"{o}: {v['good']['fact']}"}]
        return []
    return _s


@pytest.mark.parametrize("name", NAMES)
def test_a_guide_sources_its_options_on_the_question_and_drops_the_provider(name):
    v = VERTICALS[name]
    g, stub = _gen(v, call_handler=_extract_handler(v), search_handler=_option_search(v))
    sr = g._source_for_completion(v["brand"], v["seed"], {"body_markdown": "# x\n\nbody"},
                                  guide=True, ymyl=v["ymyl"])
    assert sr["tools"] == v["options"]
    assert set(sr["options"]) == {o.lower() for o in v["options"]}
    briefs = [s["brief"] for s in stub.searches]
    for o in v["options"]:
        assert any(b.startswith(f'{o} for the question "{v["seed"]}"') for b in briefs), o
    assert not any(v["provider"] in b for b in briefs), "no search is bought for a provider"
    assert stub.domain_calls == [], "an option is never hunted for a website"
    assert "invented_tools" in sr and sr["invented_tools"] == []


@pytest.mark.parametrize("name", NAMES)
def test_the_dimension_rescue_runs_for_the_options_only(name):
    v = VERTICALS[name]
    g, stub = _gen(v, call_handler=_extract_handler(v), search_handler=_option_search(v))
    sr = g._source_for_completion(v["brand"], v["seed"], {"body_markdown": "# x\n\nbody"},
                                  guide=True, ymyl=v["ymyl"])
    briefs = [s["brief"] for s in stub.searches]
    for o in v["options"]:
        assert any(b.startswith(f"{o}: {v['dim']} — the measured value for the question") for b in briefs), o
    assert not any(b.startswith(f"{v['brand']['name']}: {v['dim']}") for b in briefs), "never the subject"
    assert not any(b.startswith(f"{v['provider']}:") for b in briefs), "never a provider"
    rescued = [f for f in sr["fresh"] if f["url"] == v["good"]["url"]]
    assert rescued, "the measured result reaches the reconcile"
    official = name in ("medical", "finance")
    assert all(f["label"].startswith("official ·" if official else "third-party ·") for f in rescued)


def test_the_products_backstop_keeps_an_option_the_extraction_did_not_label():
    v = VERTICALS["medical"]
    g, _ = _gen(v, call_handler=_extract_handler(v, generic=False, products=["semaglutide", "tirzepatide"]),
                search_handler=_option_search(v))
    sr = g._source_for_completion(v["brand"], v["seed"], {"body_markdown": "# x"}, guide=True, ymyl="medical")
    assert sr["tools"] == v["options"]


def test_a_guide_neither_reads_nor_writes_the_competitor_cache():
    import json
    v = VERTICALS["construction"]
    brand = dict(v["brand"], competitor_facts=json.dumps({
        "epoxy-coating": {"domain": "", "verified_at": "2099-01-01T00:00:00Z",
                          "blocks": [{"label": "reference · old", "url": "https://old.example/epoxy",
                                      "text": "cached epoxy facts"}]}}))
    written = []

    class _DB:
        def update_brand(self, *a, **k):
            written.append(k)
    g, _ = _gen(v, call_handler=_extract_handler(v), search_handler=_option_search(v))
    g.db = _DB()
    sr = g._source_for_completion(brand, v["seed"], {"body_markdown": "# x"}, guide=True)
    assert "https://old.example/epoxy" not in [f["url"] for f in sr["fresh"]]
    assert not any("competitor_facts" in k for k in written)


# ── Change 3: the writers answer the question ────────────────────────────────────────────────
def _article_stub(p):
    return {"title": "t", "meta_description": "m", "keywords": [], "body_markdown": "# x\n", "disclosure": "d"}


_WRITER_RULES = ("ANSWER THE QUESTION WITH EVIDENCE (FU218)", "NO DEFERRAL", "OUTCOME COLUMN", "SCOPE: cover",
                 "DEPTH: every section", "the Quick answer NAMES the option the EVIDENCE favours")


@pytest.mark.parametrize("name", NAMES)
def test_the_guide_writer_must_answer_with_evidence(name):
    v = VERTICALS[name]
    g, _ = _gen(v, call_handler=_article_stub)
    g.generate_article(v["brand"], v["seed"], guide=True)
    for rule in _WRITER_RULES:
        assert rule in g._article_prompt, rule
    assert "GENERAL GUIDE (FU216" in g._article_prompt
    off, _ = _gen(call_handler=_article_stub)
    off.generate_article(v["brand"], v["seed"])
    for rule in _WRITER_RULES:
        assert rule not in off._article_prompt, rule


def test_verify_never_hedges_a_sourced_outcome_in_a_guide_only():
    v = VERTICALS["saas"]
    g, stub = _gen(v, call_handler=lambda p: {"revised_body_markdown": "x", "flagged": []})
    g.verify_claims(v["brand"], {"body_markdown": "# x"})
    assert "never hedge, soften or remove a sourced outcome figure" in stub.calls[-1]
    assert "GENERAL GUIDE: this article is a generic guide" in stub.calls[-1]
    off, stub2 = _gen(call_handler=lambda p: {"revised_body_markdown": "x", "flagged": []})
    off.verify_claims(v["brand"], {"body_markdown": "# x"})
    assert "sourced outcome figure" not in stub2.calls[-1]


def test_the_guide_reconcile_compares_the_options_on_outcomes():
    v = VERTICALS["finance"]
    g, stub = _gen(call_handler=lambda p: {"revised_body_markdown": "# x\n", "flagged": []})
    sourcing = {"name": v["brand"]["name"], "cat": "c", "tools": v["options"], "peers": [], "dims": [],
                "claims": [], "options": [o.lower() for o in v["options"]],
                "fresh": [{"label": "official · CFPB", "url": v["good"]["url"], "text": v["good"]["fact"]}],
                "guide": True, "service_area": []}
    g._reconcile_and_finish(v["brand"], v["seed"], {"body_markdown": "# x\n\nbody"}, sourcing)
    p = stub.calls[-1]
    assert "ANSWER THE QUESTION (FU218)" in p and "GENERIC OPTIONS" in p
    assert "never remove an option's row or its figures" in p
    sourcing.pop("guide"), sourcing.pop("service_area")
    g._reconcile_and_finish(v["brand"], v["seed"], {"body_markdown": "# x\n\nbody"}, sourcing)
    assert "FU218" not in stub.calls[-1]


def test_the_guide_extraction_lists_the_options_even_outside_the_brands_function():
    v = VERTICALS["construction"]
    g, stub = _gen(v, call_handler=_extract_handler(v), search_handler=_option_search(v))
    g._source_for_completion(v["brand"], v["seed"], {"body_markdown": "# x"}, guide=True)
    ext = next(c for c in stub.calls if "extract for verification" in c)
    assert "FU218: this article is a GENERAL GUIDE — the entities it compares are the OPTIONS" in ext
    off, stub2 = _gen(call_handler=_extract_handler(v))
    off._source_for_completion(v["brand"], v["seed"], {"body_markdown": "# x"})
    ext2 = next(c for c in stub2.calls if "extract for verification" in c)
    assert "FU218" not in ext2


# ── genericity: nothing new keys on a vertical ───────────────────────────────────────────────
def _slice(text, start, end):
    i = text.index(start)
    return text[i:text.index(end, i)]


def test_the_new_code_and_prompt_text_name_no_vertical():
    v = VERTICALS["construction"]
    texts = [inspect.getsource(BG._guide_answer_evidence), inspect.getsource(_answer_evidence_brief),
             inspect.getsource(BG._answer_check), _answer_evidence_brief(v["seed"], "")]
    g, _ = _gen(v, call_handler=_article_stub)
    g.generate_article(v["brand"], v["seed"], guide=True)
    p = g._article_prompt
    texts.append(_slice(p, "ANSWER THE QUESTION WITH EVIDENCE (FU218)", "IS THE EXPERT VOICE"))
    texts.append(_slice(p, "When the seed asks which option", "(AI engines lift"))
    gv, stubv = _gen(v, call_handler=lambda q: {"revised_body_markdown": "x", "flagged": []})
    gv.verify_claims(v["brand"], {"body_markdown": "# x"})
    texts.append(_slice(stubv.calls[-1], "The OPTIONS the question compares", "These changes"))
    gs, stubs = _gen(v, call_handler=_extract_handler(v), search_handler=_option_search(v))
    gs._source_for_completion(v["brand"], v["seed"], {"body_markdown": "# x"}, guide=True)
    ext = next(c for c in stubs.calls if "extract for verification" in c)
    texts.append(_slice(ext, "FU218: this article is a GENERAL GUIDE", "- DIMENSIONS"))
    texts += [s["brief"] for s in stubs.searches if "for the question" in s["brief"]]
    gr, stubr = _gen(call_handler=lambda q: {"revised_body_markdown": "# x\n", "flagged": []})
    gr._reconcile_and_finish(v["brand"], v["seed"], {"body_markdown": "# x\n\nbody"},
                             {"name": v["brand"]["name"], "cat": "c", "tools": [], "peers": [], "dims": [],
                              "claims": [], "fresh": [{"label": "l", "url": "u", "text": "t"}],
                              "guide": True, "service_area": []})
    texts.append(_slice(stubr.calls[-1], "ANSWER THE QUESTION (FU218)", "is the expert voice"))
    assert len(texts) >= 9
    for t in texts:
        m = _VERTICAL_WORDS.search(t)
        assert not m, f"vertical-specific word {m.group(0)!r} in: {t[:160]}"


# ── Change 4: the answer-check ───────────────────────────────────────────────────────────────
def test_the_reviewed_export_fires_the_answer_check():
    note = BG._answer_check({"body_markdown": GLP1_BODY}, VERTICALS["medical"]["brand"],
                            VERTICALS["medical"]["seed"])
    assert note.startswith("answer-check:") and "Which GLP-1 is best for weight loss?" in note


def _body(v, sentence, cite_own=False):
    b = v["brand"]
    s = sentence.replace("[S1]", "[S2]") if cite_own else sentence
    return (f"# {v['seed']}\n\nQuick answer: {s}\n\n## Sources\n\n"
            f"- [S1] third-party · Study — <{v['good']['url']}>\n"
            f"- [S2] {b['name']} — <{b['domain_url']}>\n")


@pytest.mark.parametrize("name", NAMES)
def test_a_cited_independent_figure_silences_the_check_in_every_vertical(name):
    v = VERTICALS[name]
    assert BG._answer_check({"body_markdown": _body(v, v["figure"])}, v["brand"], v["seed"]) == ""


@pytest.mark.parametrize("name", NAMES)
def test_a_figure_cited_only_to_the_brands_own_site_is_not_an_answer(name):
    v = VERTICALS[name]
    note = BG._answer_check({"body_markdown": _body(v, v["figure"], cite_own=True)}, v["brand"], v["seed"])
    assert note.startswith("answer-check:")


def test_a_table_row_with_a_cited_figure_counts():
    v = VERTICALS["construction"]
    body = (f"# {v['seed']}\n\n| Sealer | Result |\n|---|---|\n| Silane | 85% less water absorption [S1] |\n\n"
            f"## Sources\n\n- [S1] third-party · Test — <{v['good']['url']}>\n")
    assert BG._answer_check({"body_markdown": body}, v["brand"], v["seed"]) == ""


def test_identifiers_and_years_are_not_measured_results():
    v = VERTICALS["saas"]
    body = (f"# {v['seed']}\n\nA 2025 study of Model-3 teams under ISO 9001 compared both methods [S1].\n\n"
            f"## Sources\n\n- [S1] third-party · Study — <{v['good']['url']}>\n")
    assert BG._answer_check({"body_markdown": body}, v["brand"], v["seed"]).startswith("answer-check:")


def test_a_how_to_guide_never_fires():
    assert BG._answer_check({"body_markdown": GLP1_BODY}, VERTICALS["construction"]["brand"],
                            "In what order should you renovate a house?") == ""


@pytest.mark.parametrize("guide", [True, False])
def test_finalize_raises_the_answer_check_only_for_a_guide(guide):
    v = VERTICALS["medical"]
    g, _ = _gen(v if guide else None)
    art = {"title": v["seed"], "body_markdown": GLP1_BODY, "meta_description": "m"}
    g._finalize_article(v["brand"], v["seed"], art, GLP1_BODY, with_linkedin=False, guide=guide)
    joined = " | ".join(w.get("detail", "") for w in (art.get("warnings") or []) if isinstance(w, dict))
    assert ("answer-check:" in joined) is guide
