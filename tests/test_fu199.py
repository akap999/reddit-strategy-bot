"""FU199 — WHICH competitors get compared was decided at brand level.

FU198 fixed how a named competitor is researched. It did not touch who gets named, and on the
Osbornes international-family-law page that was the rest of the problem: the five firms compared were
the brand's firm-level rivals and only one had any family-law standing. Three selectors chose them,
all brand-level and all running before the draft — the operator's flat competitor list rendered
identically on every article, a peer-discovery search that asked for rivals of the FIRM while the seed
sat unused one line away, and the writer's own knowledge as a last resort.

The subject and its specialists now ride along on `_resolve_brand_domains`, the only unconditional
non-search LLM call in the pre-draft path, so this costs no new call and no new search. Everything is
gated on the subject contributing a token the brand's category does not already carry, which is what
keeps the narrow-category brands (most of the roster) byte-identical.

$0, no network (StubClaude).
"""
import re

import pytest

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

SEED = "which are the best UK law firms for international family law"
SUBJECT = "international family law"
PEERS = {"Vardags": "vardags.com", "Payne Hicks Beach": "phb.co.uk"}

BROAD = {"name": "Osbornes Law", "domain_url": "https://osborneslaw.com",
         "category": "full-service London solicitors for individuals and businesses",
         "competitors": '["Irwin Mitchell", "Taylor Wessing", "Farrer & Co"]'}
NARROW = dict(BROAD, category="international family law solicitors")


def _resolved(brand, subject=SUBJECT, peers=None, want=True):
    """Run the pre-draft domain resolve and hand back the generator carrying the stash."""
    def h(p):
        if '"domains"' in p:
            return {"domains": {"Irwin Mitchell": "irwinmitchell.com"},
                    "subject": subject, "peers": dict(peers or PEERS)}
        return {}
    gen = BlogGenerator(StubClaude(call_handler=h), db=None)
    gen._resolve_brand_domains(["Irwin Mitchell"], seed=SEED, subject=brand["name"],
                               subject_category=brand["category"], want_subject=want)
    return gen


def _comp_lines(gen, brand):
    return [l for l in gen._brand_block(brand)[2].split("\n")
            if l.startswith("Competitors") or l.startswith("Specialists")]


# ── INERT PROOF first: this is what protects every existing suite ────────────────────────────
def test_a_brand_whose_category_covers_the_subject_stays_exactly_as_today():
    gen = _resolved(NARROW)
    assert gen._subject_phrase == "" and gen._subject_peers == {}
    assert _comp_lines(gen, NARROW) == ["Competitors: Irwin Mitchell, Taylor Wessing, Farrer & Co"]


def test_the_post_draft_call_site_never_asks_for_a_subject():
    """`_source_for_completion` resolves domains too; its prompt must not move."""
    gen = _resolved(BROAD, want=False)
    p = next(p for p in gen.claude.calls if '"domains"' in p)
    assert '"subject"' not in p and '"peers"' not in p


def test_no_subject_returned_means_nothing_activates():
    gen = _resolved(BROAD, subject="")
    assert gen._subject_phrase == "" and gen._subject_peers == {}
    assert _comp_lines(gen, BROAD) == ["Competitors: Irwin Mitchell, Taylor Wessing, Farrer & Co"]


# ── the reported case, locked ────────────────────────────────────────────────────────────────
def test_the_pre_draft_call_asks_for_the_subject_and_its_specialists():
    gen = _resolved(BROAD)
    p = next(p for p in gen.claude.calls if '"domains"' in p)
    assert '"subject"' in p and '"peers"' in p
    assert "NARROWER than the category" in p
    assert "currently-operating" in p, "a stale specialist is worse than a short list"


def test_the_brand_block_offers_the_specialists_beside_the_curated_list():
    gen = _resolved(BROAD)
    lines = _comp_lines(gen, BROAD)
    assert any("ONLY if it genuinely does international family law" in l for l in lines)
    assert any("Specialists in international family law" in l and "Vardags" in l for l in lines)


def test_the_writer_is_told_subject_fit_beats_list_position():
    gen = _resolved(BROAD)
    gen.claude._call_handler = lambda p: {"title": "T", "meta_description": "m", "keywords": [],
                                          "body_markdown": "## Quick answer\nx", "disclosure": "d"}
    gen.claude.calls.clear()
    gen.generate_article(BROAD, SEED)
    p = gen.claude.calls[0]
    assert "SUBJECT FIT OVERRIDES LIST POSITION" in p
    assert "has no standing in international family law" in p
    # the floor and its guardrails must survive untouched (FU105 / FU184 regression)
    assert "AT LEAST 3 REAL competitors" in p
    assert "Naming a brand as an option needs no source" in p
    assert "CURRENT, actively-operating provider" in p


def test_an_inert_brand_gets_no_subject_fit_rule():
    gen = _resolved(NARROW)
    gen.claude._call_handler = lambda p: {"title": "T", "meta_description": "m", "keywords": [],
                                          "body_markdown": "## Quick answer\nx", "disclosure": "d"}
    gen.claude.calls.clear()
    gen.generate_article(NARROW, SEED)
    assert "SUBJECT FIT OVERRIDES LIST POSITION" not in gen.claude.calls[0]


# ── peer discovery stops hunting rivals of the firm ──────────────────────────────────────────
def _pbrief(gen):
    gen.claude.searches.clear()
    gen._gather_independent_sources("Osbornes Law", ["Irwin Mitchell"], SEED,
                                    BROAD["category"], ["osborneslaw.com"])
    return next(s["brief"] for s in gen.claude.searches if "DIRECT COMPETITORS" in s["brief"])


def test_peer_discovery_is_aimed_at_the_subject():
    assert "for international family law specifically" in _pbrief(_resolved(BROAD))


def test_peer_discovery_is_unchanged_when_inert():
    b = _pbrief(_resolved(NARROW))
    assert "specifically" not in b and BROAD["category"] in b


# ── a found specialist is not "invented" ─────────────────────────────────────────────────────
def _invented(tools, peers):
    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": list(tools), "peer_tools": list(tools), "dimensions": ["rankings"],
                    "products": [], "claims": [], "core_topic": "", "subject": SUBJECT,
                    "core_mechanics": []}
        if '"domains"' in p:
            return {"domains": {t: "x.com" for t in tools}}
        return {}
    gen = BlogGenerator(StubClaude(call_handler=call_h, search_handler=lambda b, a, bl: []), db=None)
    gen._subject_peers = dict(peers)
    body = "| Firm | R |\n|---|---|\n" + "".join(f"| {t} | ? |\n" for t in tools)
    return gen._source_for_completion(BROAD, SEED, {"body_markdown": body},
                                      ymyl=None).get("invented_tools") or []


def test_a_found_specialist_is_not_flagged_as_invented():
    assert "Vardags" not in _invented(["Vardags"], PEERS)


def test_a_name_from_nowhere_is_still_flagged():
    assert "Ghost Legal LLP" in _invented(["Ghost Legal LLP"], PEERS)


# ── CROSS-VERTICAL: the standing genericity gate ─────────────────────────────────────────────
@pytest.mark.parametrize("cat,subject", [
    ("multi-module HR and payroll software platform", "payroll integration"),
    ("general contractor for residential builds", "bathroom remodeling"),
])
def test_any_multi_line_brand_gets_subject_scoped_selection(cat, subject):
    brand = dict(BROAD, category=cat)
    gen = _resolved(brand, subject=subject, peers={"SpecCo": "specco.com"})
    assert gen._subject_phrase == subject
    assert any(f"genuinely does {subject}" in l for l in _comp_lines(gen, brand))


def test_the_new_code_carries_no_vertical_term():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    for marker in ("SUBJECT FIT OVERRIDES LIST POSITION", "subj_ask = ("):
        i = src.index(marker)
        block = src[i:i + 1600]
        for term in ("law", "solicitor", "medical", "clinic", "payroll", "remodel"):
            assert not re.search(r"\b%s\b" % term, block, re.I), f"'{term}' hardcoded near {marker!r}"
