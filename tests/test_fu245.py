"""FU245 — a comparison entity whose name has a slash in it could not be matched to any page.

The report, from a live generation pause:

    Commercial/Employer-Sponsored Insurance (option, not a company)
    No public reference found for Commercial/Employer-Sponsored Insurance — it's an option, not a
    company, so there's no page to link. Needed for the comparison: Covers Semaglutide for Weight
    Loss? + Key Conditions / Limits + Step therapy requirements + Prior authorization requirements
    + Behavioral modification requirements + BMI eligibility thresholds.

That is an article entirely about insurance coverage, asking a human to hand-type the prior
authorization rules for the whole commercial payer category, while its own evidence — a KFF
employer survey — already said them. FU189 was right that a payer type has no homepage; the pause
was not about that. Three filters decide whether a page names an entity, and this name broke all
three at once:

  * the dimension rescue tested `name.lower().split()[0]`, which here is the string
    "commercial/employer-sponsored" — not a word, present on no page ever written. Every result it
    was handed for this entity was discarded. Any name with a slash or a leading hyphenated
    compound was unmatchable;
  * the option keep filter and the pause re-check both demanded EVERY token of the name on one
    page. The survey that answers it says "employer-sponsored insurance" and never once says
    "commercial";
  * and the reference search was anchored on the BRAND's category, so a payer type was searched
    "in the context of telehealth".

So the entity was unsourceable by construction, and the only symptom was an impossible ask. The
three filters now share one predicate, which is the point: they disagreed, and a compound category
name fell through the gap.
"""
import pytest

from generators.blog_gen import (BlogGenerator, _entity_match_tokens, _entity_named_in,
                                 _product_tokens)
from tests.stubs import StubClaude

NAME = "Commercial/Employer-Sponsored Insurance"
SEED = "does insurance cover semaglutide for weight loss in the us"
# what the page that actually answers this entity says — note it never says "commercial"
KFF = ("Employer Health Benefits Survey: 19% of firms with 200 or more workers offering "
       "employer-sponsored insurance cover GLP-1 drugs for weight loss; most require prior "
       "authorization and step therapy.")


# ══ the reproduction ═══════════════════════════════════════════════════════════════════════════════
def test_the_old_first_token_filter_looked_for_a_string_no_page_contains():
    """Not a near miss — a token with a slash through the middle of it."""
    assert NAME.lower().split()[0] == "commercial/employer-sponsored"
    assert NAME.lower().split()[0] not in KFF.lower()


def test_the_old_all_tokens_rule_needed_a_word_the_answering_page_never_uses():
    toks = [x for x in _product_tokens(NAME) if len(x) >= 3]
    assert not all(x in KFF.lower() for x in toks)
    assert [x for x in toks if x not in KFF.lower()] == ["commercial"]


def test_the_page_that_answers_the_entity_is_now_kept():
    assert _entity_named_in(NAME, KFF) is True


# ══ the tokens ═════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("name,want", [
    (NAME, ["commercial", "employer", "sponsored", "insurance"]),
    ("TRT (Testosterone Replacement Therapy)", ["trt"]),          # the SHORTEST reading wins
    ("term loan", ["term", "loan"]),
    ("Medicare Part D", ["medicare", "part"]),
])
def test_the_matcher_splits_on_slashes_and_hyphens_and_prefers_the_short_form(name, want):
    assert _entity_match_tokens(name) == want


def test_a_name_with_nothing_distinctive_matches_nothing():
    assert _entity_match_tokens("a/an") == []
    assert _entity_named_in("a/an", "anything at all") is False


# ══ the predicate: a compound needs a majority, a short name still needs all of it ═════════════════
@pytest.mark.parametrize("name,blob,want", [
    # 1-2 tokens: unchanged, still all-or-nothing
    ("term loan", "a line of credit is revolving credit", False),
    ("term loan", "a term loan is repaid over a fixed term", True),
    ("TRT (Testosterone Replacement Therapy)", "TRT raises serum testosterone", True),
    # 3+ tokens: a majority, and never fewer than two
    (NAME, KFF, True),
    (NAME, "car insurance quotes and roadside cover", False),          # one shared word is not enough
    (NAME, "Medicare Part D covers these drugs for enrollees.", False),
    ("high deductible health plan", "a high deductible plan pairs with an HSA", True),
])
def test_the_shared_predicate(name, blob, want):
    assert _entity_named_in(name, blob) is want


def test_two_shared_words_out_of_four_is_the_floor_not_one():
    """The majority rule must not become "any overlap" — that is how an unrelated page becomes a
    competitor's sole citation."""
    assert _entity_named_in(NAME, "employer insurance") is True
    assert _entity_named_in(NAME, "employer of record services") is False


# ══ the three call sites now share it ══════════════════════════════════════════════════════════════
BODY = ("## Quick answer\nx\n\n| Payer | Covers semaglutide? |\n|---|---|\n"
        "| PeterMD | a |\n| Medicare | b |\n| " + NAME + " | c |\n")


def _run(search_h, options=(NAME,), dims=("Covers semaglutide?", "BMI eligibility thresholds")):
    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": ["Medicare", NAME], "peer_tools": ["Medicare", NAME],
                    "dimensions": list(dims), "generic_options": list(options),
                    "products": [], "claims": [], "core_topic": "insurance coverage of semaglutide"}
        if '"domains"' in p:
            return {"domains": {}}
        return {}
    stub = StubClaude(call_handler=call_h, search_handler=search_h)
    gen = BlogGenerator(stub, db=None)
    gen._evidence_blocks = []
    brand = {"name": "PeterMD", "domain_url": "https://getpetermd.com",
             "category": "telehealth", "competitors": []}
    return stub, gen._source_for_completion(brand, SEED, {"body_markdown": BODY}, ymyl=None)


def test_the_reported_pause_no_longer_fires():
    """End to end: the reference search returns the page that answers the entity, and it is kept
    instead of discarded, so there is nothing to ask the operator for."""
    def search_h(brief, allowed, blocked):
        if NAME in brief and "NOT a vendor sales page" in brief:
            return [{"title": "Employer Health Benefits Survey", "url": "https://kff.org/ehbs",
                     "fact": KFF}]
        return []
    _stub, sourcing = _run(search_h)
    asked = [u["tool"] for u in (sourcing.get("unsourced") or [])]
    assert NAME not in asked, f"still pausing for {NAME}: {sourcing.get('unsourced')}"
    kept = " ".join((b.get("url") or "") for b in sourcing["fresh"])
    assert "kff.org/ehbs" in kept


def test_it_still_pauses_when_nothing_answers_the_entity():
    """The pause is not the bug and must survive — an entity nothing can source still asks."""
    _stub, sourcing = _run(lambda brief, allowed, blocked: [])
    asked = [u for u in (sourcing.get("unsourced") or []) if u["tool"] == NAME]
    assert asked and asked[0].get("generic_option") is True


def test_an_off_topic_page_is_still_refused_for_the_entity():
    """Scoped to the option's OWN reference search: the article-level corroboration search is
    deliberately not entity-filtered, so answering it here would test nothing."""
    def search_h(brief, allowed, blocked):
        if NAME in brief and "NOT a vendor sales page" in brief:
            return [{"title": "Car insurance quotes", "url": "https://carinsure.example/q",
                     "fact": "Compare car insurance quotes and roadside cover."}]
        return []
    _stub, sourcing = _run(search_h)
    kept = " ".join((b.get("url") or "") for b in sourcing["fresh"])
    assert "carinsure.example" not in kept
    assert NAME in [u["tool"] for u in (sourcing.get("unsourced") or [])]


def test_the_option_search_is_anchored_on_the_article_not_the_brands_category():
    """A payer type searched "in the context of telehealth" returns nothing about payers. The
    article's own subject is what the option is being compared FOR."""
    seen = []

    def search_h(brief, allowed, blocked):
        seen.append(brief)
        return []
    _run(search_h)
    ref = [b for b in seen if NAME in b and "NOT a vendor sales page" in b]
    assert ref, "the option reference search did not run"
    assert "telehealth" not in ref[0], ref[0][:200]


def test_the_three_filters_share_one_predicate():
    import re as _re
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    code = _re.sub(r"(?m)#.*$", "", _re.sub(r'"""(?:.|\n)*?"""', "", src))
    assert "t.lower().split()[0] in blob" not in code       # the bug, gone for good
    assert code.count("_entity_named_in(") >= 4             # helper + its three call sites


@pytest.mark.parametrize("term", ["insurance", "semaglutide", "medicare", "employer", "telehealth"])
def test_no_vertical_word_is_hard_coded_in_the_matcher(term):
    import re as _re
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    start = src.index("def _entity_match_tokens")
    end = src.index("def _named_as_option")
    block = _re.sub(r"(?m)#.*$", "", _re.sub(r'"""(?:.|\n)*?"""', "", src[start:end]))
    assert term not in block.lower()
