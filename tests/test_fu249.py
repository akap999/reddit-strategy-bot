"""FU249 — the article may not describe evidence it does not have, and a figure without its unit
cannot be checked at all.

Five defects were reported in one shipped article. Four are one disease and the fifth is the reason
none of them was caught:

  * "0.8 kg/month, back to baseline in about 1.5 years" was credited to the PeerJ meta-analysis.
    Those are a DIFFERENT paper's numbers; PeerJ supports only the 6 trials, 8,993 patients and
    9.11%. Both are described in the article as different studies, and both point at [S1];
  * the meta description promised "55-65% regained" and the Quick answer said "60-66%", and neither
    figure is in the source both cite;
  * "Both analyses agree" followed a single meta-analysis, and an FAQ credited "a separate
    systematic review" for 1.7 years with no marker at all;
  * the cardiometabolic section named the STEP 1 trial extension and cited nothing.

Every figure check passed all of it. Measured against the live regex, the checks saw "0.8 kg per
month" as the atom `0.8`, "1.5 years" as `1.5`, "2.5 mg" as `2.5`, and "60-66%" as NOTHING — a
percentage range matched no alternative at all. So "does the cited page state this figure" was
really asking whether a 40,000-character clinical paper contains the digits "0.8" somewhere, which
it always does, and an invented range was never a question anyone asked.

Two causes: the decimal alternative comes before the unit ones and wins, and none of the unit
alternatives accepts a decimal; and the bare percent's `(?<![\\w-])` guard — there so "GLP-1
prescriptions" cannot yield "1 prescriptions" — also rejects the second half of "55-65%".

The rest is not a figure problem at all. A meta-analysis discusses other meta-analyses, so a rival
paper's numbers really are printed on the page they were wrongly attributed to, and no amount of
reading pages separates them. What gives the mis-attribution away is that the article describes TWO
studies and points both at ONE source — which is checkable without reading anything.
"""
import os
import re

import pytest

from generators.blog_gen import _CLAIM_NUM_RE, BlogGenerator

HERE = os.path.dirname(__file__)


@pytest.fixture(scope="module")
def shipped():
    """The article as published, converted to the markdown it was rendered from."""
    with open(os.path.join(HERE, "fixtures", "fu249_semaglutide_body.md")) as f:
        return f.read()


@pytest.fixture
def gen():
    return BlogGenerator.__new__(BlogGenerator)


def _atoms(s):
    return [m.group(0).strip() for m in _CLAIM_NUM_RE.finditer(s)]


# ── a figure keeps its unit ──────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("text,atom", [
    ("a regain rate of 0.8 kg per month", "0.8 kg"),
    ("return to baseline at approximately 1.5 years", "1.5 years"),
    ("the starting dose is 2.5 mg once weekly", "2.5 mg"),
    ("patients lost 12.4 lbs on average", "12.4 lbs"),
    ("uptime held at 99.9% through the quarter", "99.9%"),
])
def test_a_decimal_carries_its_unit(text, atom):
    """Before this, every one of these was seen as a bare decimal — and a bare decimal appears in
    any long document, so the figure checks could not fail."""
    assert atom in _atoms(text)


@pytest.mark.parametrize("text,atom", [
    ("approximately 60-66% of the weight lost", "60-66%"),
    ("averaging 55-65% of lost weight", "55-65%"),
    ("95% CI 7.91-10.30%", "7.91-10.30%"),
    ("regain of 0.4-0.8 kg per month", "0.4-0.8 kg"),
    ("a course of 12 to 18 months", "12 to 18 months"),
])
def test_a_range_is_one_figure(text, atom):
    """A percentage range matched NOTHING, so an invented one was invisible to every check."""
    assert atom in _atoms(text)


@pytest.mark.parametrize("text", ["GLP-1 prescriptions rose", "COVID-19 patients", "a 503B facility"])
def test_a_number_inside_a_name_is_still_not_a_figure(text):
    """The guard the new alternatives must not break: FU242 added it because "GLP-1 prescriptions"
    was yielding the atom "1 prescriptions"."""
    assert not [a for a in _atoms(text) if a.startswith("1 ") or a.startswith("19 ")]


def test_the_checks_can_now_fail_on_a_page_that_does_not_state_the_figure():
    """The whole point: `0.8` is in every clinical paper ever written; `0.8 kg` is a claim."""
    page = "adjusted odds ratio 0.8 (95% CI 0.6-1.1); mean difference 1.5 points; follow-up 60 weeks"
    assert BlogGenerator._atom_in("0.8", page)             # the old atom — unfalsifiable
    assert not BlogGenerator._atom_in("0.8 kg", page)      # the new one — a real test
    assert not BlogGenerator._atom_in("1.5 years", page)
    assert not BlogGenerator._atom_in("60-66%", page)


@pytest.mark.parametrize("atom,page", [
    ("0.8 kg", "body weight fell 0.8kg over the period"),            # no space
    ("7.91-10.30%", "95% CI 7.91 to 10.30 across the trials"),       # written as a span
    ("12 to 18 months", "a 12-18 month course"),                     # written as a dash
    ("1.5 years", "returned to baseline at 1.5 Years"),              # case
])
def test_a_typesetting_choice_does_not_make_a_true_figure_look_invented(atom, page):
    """A false negative here DELETES a true sentence, so the tolerance matters as much as the rigour."""
    assert BlogGenerator._atom_in(atom, page)


def test_a_range_may_lose_its_unit_but_a_single_figure_may_not():
    """The two numbers identify a range; a lone number identifies nothing, which is the blind spot."""
    assert BlogGenerator._atom_in("60-66%", "regain was 60-66 percent")   # range, unit implied
    assert not BlogGenerator._atom_in("0.8 kg", "odds ratio was 0.8")     # no fallback to the digits


# ── the article's own account of its evidence ────────────────────────────────────────────────────
def test_the_shipped_article_is_caught_end_to_end(gen, shipped):
    """All four reported defects, from the real body, in one check."""
    note = gen._study_reference_check(shipped)
    assert note
    assert "STEP 1 trial extension" in note                  # named, cited nothing
    assert "a separate systematic review" in note            # named, cited nothing
    assert "Both analyses" in note                           # a plurality of one
    assert "[S1]" in note and "different studies" in note     # two papers, one marker


def test_a_study_named_with_no_citation_is_reported(gen):
    note = gen._study_reference_check(
        "## X\n\nThe STEP 1 trial extension showed that improvements reversed after stopping.\n")
    assert "STEP 1 trial extension" in note and "cites no source" in note


def test_a_second_study_in_a_cited_sentence_is_still_uncited(gen):
    """One marker does not cover two papers — the FAQ that credited 1.7 years to nothing."""
    note = gen._study_reference_check(
        "## X\n\nA meta-analysis projects return at roughly 1.5 years [S1], while a separate "
        "systematic review projects return at approximately 1.7 years.\n")
    assert "a separate systematic review" in note


def test_two_different_studies_on_one_marker_are_reported(gen):
    """The headline defect. Neither page needs reading: the article says these are two papers."""
    note = gen._study_reference_check(
        "## X\n\n- A systematic review and meta-analysis of 6 trials (8,993 patients) found an "
        "average gain of 9.11% [S1].\n"
        "- A meta-analysis of novel incretin mimetics estimated a rate of 0.8 kg per month [S1].\n")
    assert "different studies" in note and "[S1]" in note


def test_one_study_described_the_same_way_twice_is_not_a_defect(gen):
    """A source cited in two sections is ordinary; the check must not punish it."""
    note = gen._study_reference_check(
        "## X\n\nThe Endocrine Society's clinical practice guideline recommends ongoing management "
        "[S2].\n\n## Y\n\nThe Endocrine Society's guideline notes that agonists are recommended "
        "[S2].\n")
    assert "different studies" not in note


def test_a_plurality_needs_that_many_sources(gen):
    body = ("## X\n\nA meta-analysis estimated 0.8 kg per month [S1]. Both analyses agree that "
            "regain is rapid.\n")
    assert "Both analyses" in gen._study_reference_check(body)


def test_a_plurality_with_the_sources_to_back_it_is_left_alone(gen):
    body = ("## X\n\nOne analysis estimated 0.8 kg per month [S1] and another 0.4 kg [S3]. Both "
            "analyses agree that regain is rapid.\n")
    assert "Both analyses" not in gen._study_reference_check(body)


def test_a_body_of_evidence_is_not_a_named_study(gen):
    """"documented in discontinuation trials" gestures at a literature rather than claiming one
    document. Flagging it would be noise, and noise is how a warning stops being read."""
    note = gen._study_reference_check(
        "## X\n\nA clinician should monitor for the changes documented in discontinuation trials.\n")
    assert not note


def test_a_cited_study_reference_is_silent(gen):
    note = gen._study_reference_check(
        "## X\n\nA systematic review of 6 trials found an average gain of 9.11% [S1].\n")
    assert not note


def test_an_article_that_cites_nothing_at_all_is_not_swamped(gen):
    """A page with no citations anywhere has a different problem, reported elsewhere."""
    note = gen._study_reference_check("## X\n\nStopping the drug tends to bring the weight back.\n")
    assert not note


# ── the meta description ─────────────────────────────────────────────────────────────────────────
def test_the_meta_is_corrected_to_the_body(gen, shipped):
    """The reported case, with the real body: the meta promised 55-65% and the article said
    60-66%. The body is the half that went through the source checks, so it is the one to match."""
    art = {"meta_description": "Stopping semaglutide causes weight regain, averaging 55-65% of lost "
                               "weight within one year.",
           "body_markdown": shipped}
    note = gen._meta_figure_check(art)
    assert "55-65%" in note and "60-66%" in note
    assert "60-66%" in art["meta_description"] and "55-65%" not in art["meta_description"]


def test_an_ambiguous_meta_figure_is_reported_rather_than_guessed_at(gen):
    art = {"meta_description": "Regain runs 55-65% of lost weight.",
           "body_markdown": "## X\n\nRegain ran 60-66% in one cohort and 40-50% in another.\n"}
    note = gen._meta_figure_check(art)
    assert "never says" in note
    assert "55-65%" in art["meta_description"]          # untouched — two candidates, no guess

def test_a_meta_that_repeats_the_body_is_silent(gen):
    art = {"meta_description": "Regain runs 60-66% of lost weight within a year.",
           "body_markdown": "## X\n\nRoughly 60-66% of the weight is regained within a year [S1].\n"}
    assert not gen._meta_figure_check(art)
    assert art["meta_description"] == "Regain runs 60-66% of lost weight within a year."


def test_the_sources_list_is_not_mistaken_for_the_article(gen):
    """A figure that appears only in a source TITLE has not been stated by the article."""
    art = {"meta_description": "Regain runs 60-66% of lost weight.",
           "body_markdown": "## X\n\nWeight comes back.\n\n## Sources\n- [S1] A trial of 60-66% "
                            "retention - https://x.test/a\n"}
    assert "never says" in gen._meta_figure_check(art)


# ── the checks are general, not medical ──────────────────────────────────────────────────────────
def test_nothing_here_keys_on_medicine(gen):
    """Every vertical cites studies, surveys, filings and guidelines; a lending or SaaS article that
    invents a second report must fail the same way."""
    note = gen._study_reference_check(
        "## X\n\nA survey of 400 firms put the median APR at 8.5% [S1], while a separate industry "
        "report puts it at 9.2%.\n")
    assert "a separate industry report" in note

    src = open(os.path.join(HERE, "..", "generators", "blog_gen.py")).read()
    body = src[src.index("_STUDY_NOUN_RE = re.compile"):src.index("def _meta_figure_check")]
    body = re.sub(r'""".*?"""', "", body, flags=re.S)
    body = "\n".join(ln for ln in body.split("\n") if not ln.lstrip().startswith("#"))
    for word in ("semaglutide", "glp", "weight", "obesity", "patient dose", "mg per"):
        assert word not in body.lower(), f"{word!r} hard-coded into the check"


# ── what the widened figure now buys the checks that already existed ─────────────────────────────
def test_an_invented_range_is_removed_from_the_body(gen):
    """The other half of the meta/body disagreement. `_unsourced_figure_check` has deleted a figure
    no gathered source contains since FU239 — it simply could not see a percentage range, so an
    invented one shipped as a cited clinical outcome while the true figure beside it verified fine."""
    gen._claim_pages = {}
    blocks = [{"label": "third-party · meta-analysis", "url": "https://pmc.test/a",
               "text": "A meta-analysis of 6 trials (8,993 patients) found a mean weight gain of "
                       "9.11% (95% CI 7.91 to 10.30%) after discontinuation. " + "x " * 400}]
    body = ("## How much is regained?\n\nRoughly 60-66% of the weight lost is regained within one "
            "year [S1]. The pooled gain was 9.11% across the trials [S1].\n")
    out, note = gen._unsourced_figure_check(body, blocks)
    assert "60-66%" in note and "60-66%" not in out          # the invention goes
    assert "9.11%" in out                                     # the figure that IS on the page stays


def test_a_dose_keeps_its_unit_for_the_same_check(gen):
    """The same blind spot on the figure a YMYL page can least afford to get wrong: "2.5 mg" was
    checked as "2.5", and any paper mentioning 2.5 of anything satisfied it."""
    gen._claim_pages = {}
    blocks = [{"label": "official · label", "url": "https://fda.test/l",
               "text": "Initiate at 0.25 mg once weekly for 4 weeks. " + "x " * 400}]
    out, note = gen._unsourced_figure_check(
        "## Dosing\n\nTreatment starts at 2.5 mg once weekly [S1].\n", blocks)
    assert "2.5 mg" in note and "2.5 mg" not in out
