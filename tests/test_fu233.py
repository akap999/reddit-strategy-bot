"""FU233 — the source says something NARROWER than the sentence does.

Every existing check confirms a claim is attached to a real, relevant, official page. None tests
whether the page SAYS that. FU229's claim check does read the cited pages, but it matches FIGURES —
and on the article that exposed this, every defect was in a sentence with no number in it:

  * The Endocrine Society's obesity guideline recommends GLP-1 analogs in ONE population —
    recommendation 1.7, "In patients with T2DM who are overweight or obese". The FAQ said the
    guideline is NOT limited to that population, the exact negation of its own citation.
  * The same fact was written three ways by three full-body rewrites: the body carried the
    qualifier, the Quick answer dropped it, the FAQ reversed it.
  * "Exercise cannot be optional for people taking these medications" is Sajana Maharjan, M.D.,
    study lead, at ENDO 2026 — a researcher's conclusion the Society published in its news room.
    It shipped as "The Endocrine Society emphasizes".
  * Two sections of real length carried no citation at all, in the same confident register as the
    trial results beside them.

Nothing here reads a page or calls a model. Fixture: the real shipped body.
"""
import pathlib
import re

import pytest

from generators.blog_gen import BlogGenerator
from tests.prompt_capture import capture
from tests.stubs import StubClaude

_P = capture()


def _gen():
    return BlogGenerator(StubClaude(), None)


def _real():
    return (pathlib.Path(__file__).parent / "fixtures" / "fu233_glp1_body.md").read_text()


# ── the rules that stop it being written ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_the_writer_must_carry_the_sources_own_scope(variant):
    p = _P["generate_article." + variant]
    assert "CARRY A SOURCE'S OWN SCOPE" in p
    assert "broadly, not only" in p                  # the shipped phrasing, banned by name
    assert "qualifying population" in p


@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_the_writer_may_not_promote_a_news_item_to_a_position(variant):
    p = _P["generate_article." + variant]
    assert "A NEWS ITEM IS NOT AN ORGANISATION'S POSITION" in p
    assert "press release" in p and "news-room item" in p
    assert "Never upgrade a" in p


def test_the_reconcile_carries_both_rules():
    """Three passes rewrite the full body; the LAST one has to hold the rule or it reverts."""
    p = _P["reconcile_and_finish"]
    assert "CARRY A SOURCE'S OWN SCOPE" in p
    assert "A NEWS ITEM IS NOT A POSITION" in p
    assert "Quick answer, the body and the FAQ must agree" in p


def test_the_scrutiny_pass_looks_for_both():
    p = _P["verify_claims"]
    assert "AN AUTHORITY'S SCOPE WIDENED" in p
    assert "AN ORGANISATION'S POSITION TAKEN FROM A NEWS ITEM" in p


def test_neither_rule_names_a_vertical():
    """Standing rule: a generator fix works in every vertical."""
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("CARRY A SOURCE'S OWN SCOPE, WORD FOR WORD")
    block = src[i:src.index("THE PUNT BAN IS ON MEANING", i)]
    for term in ("medical", "clinical", "drug", "patient", "legal", "law", "saas", "contractor"):
        assert not re.search(r"\b%s\b" % term, block, re.I), f"'{term}' hardcoded in the FU233 rules"


# ── the citation-placement helper (found while testing: both checks were blind) ──────────────────
def test_a_citation_after_the_full_stop_belongs_to_the_claim_before_it():
    """The real article writes "…who are obese. [S1] Men face…". Splitting on the full stop hands
    every marker to the FOLLOWING sentence, so a check reading citations out of a sentence sees the
    claim with none and the next sentence carrying somebody else's."""
    got = _gen()._sentence_citations("A recommends X for Y. [S1] B is unrelated.")
    assert got[0][1] == [1], "the marker belongs to the claim it follows"
    assert got[1][1] == [], "and must not be re-attributed to the next sentence"
    assert "[S1]" not in got[1][0]


def test_a_citation_inside_the_sentence_still_works():
    got = _gen()._sentence_citations("A recommends X for Y [S1]. B is unrelated.")
    assert got[0][1] == [1] and got[1][1] == []


# ── check 1: a press release is not a position ───────────────────────────────────────────────────
NEWS = {"label": "official · Exercise decreases among people taking GLP-1 medication",
        "url": "https://www.endocrine.org/news-and-advocacy/news-room/2026/maharjan-press-release", "text": ""}
GUIDE = {"label": "official · Pharmacological Management of Obesity Guideline",
         "url": "https://www.endocrine.org/clinical-practice-guidelines/pharmacological-management-of-obesity",
         "text": ""}


def test_a_position_attributed_to_a_press_release_is_flagged():
    note = _gen()._press_release_position_check(
        "The Endocrine Society emphasizes that exercise must accompany treatment. [S1]", [NEWS])
    assert note.startswith("position-check:") and "endocrine.org" in note


@pytest.mark.parametrize("sent,why", [
    ("The Endocrine Society recommends this approach for that population. [S1]",
     "a guideline page IS the organisation's position"),
    ("Research presented at the Endocrine Society's ENDO 2026 meeting found the largest declines "
     "among men. [S1]", "a research verb, correctly attributed to the study"),
    ("Data from the 2026 meeting shows a decline in activity. [S1]", "no organisation-position verb"),
])
def test_a_correctly_attributed_sentence_is_silent(sent, why):
    blocks = [GUIDE] if "guideline" in why else [NEWS]
    assert _gen()._press_release_position_check(sent, blocks) == "", why


@pytest.mark.parametrize("sent,url", [
    # construction / trades
    ("The National Roofing Association requires annual membership renewal. [S1]",
     "https://www.example-trade-body.org/press/2026/conference-round-up"),
    # finance
    ("The Financial Conduct Authority requires firms to hold that buffer. [S1]",
     "https://www.example-regulator.gov/newsroom/2026/statement"),
    # SaaS / analyst
    ("Gartner Research recommends this migration path for mid-market buyers. [S1]",
     "https://www.example-analyst.com/blog/2026/migration-notes"),
    # medical (the reproduction case)
    ("The Endocrine Society emphasizes that exercise must accompany treatment. [S1]",
     "https://www.endocrine.org/news-and-advocacy/news-room/2026/press-release"),
])
def test_the_check_is_vertical_neutral(sent, url):
    """Standing rule: a generator fix works in every vertical. The shape — an organisation's
    position taken from its own news surface — is the same for a trade body, a regulator, an
    analyst and a medical society."""
    b = [{"label": "official · Item", "url": url, "text": ""}]
    assert _gen()._press_release_position_check(sent, b).startswith("position-check:")


# ── measured precision on every OTHER real body we have committed ────────────────────────────────
_OTHER_BODIES = ["fu200_osbornes_table.md", "fu216_cmk_guide_body.md", "fu218_glp1_guide_body.md",
                 "fu220_jolly_body.md", "fu221_158_original.md", "fu221_158_rewrite.md",
                 "fu232_agency_table.md"]


@pytest.mark.parametrize("name", _OTHER_BODIES)
def test_the_prose_checks_are_silent_on_every_other_real_body(name):
    """A warning nobody trusts is worse than no warning. Both prose checks fire on the one article
    that carries the defect and on none of the other real bodies in the fixture set."""
    body = (pathlib.Path(__file__).parent / "fixtures" / name).read_text(errors="replace")
    g = _gen()
    assert g._press_release_position_check(body, []) == "", name
    assert g._cited_scope_check(body, []) == "", name


# ── check 2: an authority's scope, widened or restated three ways ────────────────────────────────
def test_asserting_what_a_source_does_not_limit_itself_to_is_flagged():
    note = _gen()._cited_scope_check(
        "The Society recommends this treatment for overweight and obese patients broadly, not only "
        "those with type 2 diabetes. [S1]", [GUIDE])
    assert note.startswith("scope-check:") and "does NOT limit itself to" in note


def test_a_dropped_qualifier_is_flagged_across_sections():
    """The Quick answer and the body cite the same source and state a different scope. This is the
    only check that reads across both at once — which is where three full-body rewrites leak."""
    body = ("The guideline recommends this treatment for patients with condition X who are also "
            "overweight or obese. [S1]\n\n"
            "## Quick answer\n\n"
            "The guideline recommends this treatment for patients who are overweight or obese. [S1]\n")
    note = _gen()._cited_scope_check(body, [GUIDE])
    assert note.startswith("scope-check:") and "drops a qualifier" in note
    assert "condition X" in note, "the operator must see the qualifier that was dropped"


@pytest.mark.parametrize("body,why", [
    ("The guideline recommends this for patients with condition X. [S1]\n\n"
     "The guideline recommends this for patients with condition X. [S1]\n", "identical, not a drop"),
    ("The guideline recommends this for patients with condition X. [S1]\n\n"
     "A separate body advises something different for another group entirely. [S2]\n",
     "different sources are never compared"),
    ("This applies broadly, not only in one place.", "no citation at all"),
])
def test_a_consistent_or_uncited_body_is_silent(body, why):
    assert _gen()._cited_scope_check(body, [GUIDE, NEWS]) == "", why


# ── check 3: an uncited section on a YMYL page ───────────────────────────────────────────────────
def test_a_long_section_with_no_citation_is_flagged():
    body = "## Sourced\n\n" + ("Word " * 60) + "[S1]\n\n## Reasoning\n\n" + ("Word " * 60) + "\n"
    note = _gen()._uncited_section_check(body)
    assert note.startswith("uncited-section:") and '"Reasoning"' in note and "Sourced" not in note


@pytest.mark.parametrize("body,why", [
    ("## Lead-in\n\nA short bridging line.\n", "too short to be making claims"),
    ("## Sourced\n\n" + ("Word " * 60) + "[S2]\n", "cites something"),
    ("## FAQ\n\n### A question?\n\n" + ("Word " * 60) + "\n", "the FAQ stops the scan"),
])
def test_a_short_cited_or_faq_section_is_silent(body, why):
    assert _gen()._uncited_section_check(body) == "", why


# ── the real shipped article, as a permanent anchor ──────────────────────────────────────────────
def test_the_real_article_press_release_defect_is_caught():
    note = _gen()._press_release_position_check(_real(), [])
    assert "The Endocrine Society emphasizes" in note
    assert "endocrine.org" in note


def test_the_real_article_scope_defects_are_caught():
    note = _gen()._cited_scope_check(_real(), [])
    assert "does NOT limit itself to" in note, "the FAQ reversal"
    assert "drops a qualifier" in note, "the Quick answer dropping 'with type 2 diabetes'"
    assert "type 2 diabetes" in note, "the operator must see the qualifier"


def test_the_real_articles_uncited_sections_are_named():
    note = _gen()._uncited_section_check(_real())
    assert "Lean-mass preservation" in note
    assert "compounding and legal" in note


def test_the_real_articles_correctly_attributed_research_is_not_flagged():
    """Precision on real output: the two AUA findings ARE cited to press pages and ARE correctly
    attributed to the research, not to the association. They must stay silent."""
    note = _gen()._press_release_position_check(_real(), [])
    assert "Urological" not in note and "auanet" not in note
