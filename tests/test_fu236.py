"""FU236 — three defects in the third generation of one video.

Two fixtures, the same video, two generations:
  * `fu235_construction_script.md`  (v2) — conceded the buyer's OUTCOME to the field.
  * `fu236_construction_script_v3.md` (v3) — that concession fixed by FU235's rule, and three
    things either regressed or survived.

1. A LOCAL CREDENTIAL INVENTED FOR A COMPETITOR. v3 says Straight North serves the Bay Area "with
   over 25 years of experience in California" and calls it "a California-based team". Straight North
   is an Illinois company — Downers Grove HQ, a Chicago office — and the blog's source is a San
   Francisco LOCATION page plus a company-wide tenure figure. Two true facts merged into a false
   sentence, on a video whose entire premise is California. It was flagged in v1, fixed in v2, and
   came back in v3, which is what makes it a check and not a prompt rule alone.

2. THE FLAGGED SEGMENT SHIPPED ANYWAY. FU235's airtime check fired and the package still went out
   with segment 1 at 22%. One retry was not enough: cutting the segment leaves the script under its
   length target, and the model padded instead of shortening. Fixed by saying the target is a budget
   and not a quota, and by giving the loop a second retry.

3. THE PUBLISHER'S LANE IS STILL A MECHANISM. "The gap you need to close is appearing in
   AI-generated answers" is a visibility promise. FU235 stopped the outcome being conceded to the
   field; it did not make the publisher's own line state what its mechanism produces.

Nothing here calls a model.
"""
import pathlib
import re

import pytest

from generators.blog_gen import BlogGenerator, _YT_RETRIES
from tests.prompt_capture import capture

_P = capture()
NAME, GEO = "Jolly Search", "California"


def _fx(n):
    return (pathlib.Path(__file__).parent / "fixtures" / n).read_text()


V2, V3 = "fu235_construction_script.md", "fu236_construction_script_v3.md"


def _table():
    return _fx("fu232_agency_table.md")


# ── 1. the invented local credential ─────────────────────────────────────────────────────────────
def test_the_invented_california_credential_is_caught():
    note = BlogGenerator._geo_credential_note(_fx(V3), _table(), NAME, GEO)
    assert note.startswith("geo-credential:")
    assert "Straight North" in note
    assert "California credential the article never states" in note


def test_both_instances_are_caught_including_the_pronoun_one():
    """"Straight North has a page … They also serve the Bay Area with 25 years of experience in
    California." The second sentence names nobody — a per-sentence check reads it as unattributed,
    which is where half the claim hid."""
    note = BlogGenerator._geo_credential_note(_fx(V3), _table(), NAME, GEO)
    assert "They also serve" in note, "the pronoun-led sentence must be attributed to its subject"
    assert "California-based team" in note or "Straight North is the better fit" in note


def test_the_version_without_the_claim_is_silent():
    """v2 said only "they serve the Bay Area specifically" — true, and not a credential claim. The
    check has to tell the two generations apart or it is just noise."""
    assert BlogGenerator._geo_credential_note(_fx(V2), _table(), NAME, GEO) == ""


def test_an_alias_does_not_make_every_sentence_ambiguous():
    """Found while testing: "Straight North" matches both its full name and its first-word alias, and
    counting those as two options silenced the check on every profile sentence."""
    names = [("Straight North", [re.compile(r"\bStraight North\b"), re.compile(r"\bStraight\b")])]
    got = BlogGenerator._option_sentences("Straight North is the better fit for that buyer.", names)
    assert got and got[0][1] == "Straight North"


def test_two_options_in_one_sentence_stay_ambiguous():
    names = [("Straight North", [re.compile(r"\bStraight North\b")]),
             ("Blue Corona", [re.compile(r"\bBlue Corona\b")])]
    assert BlogGenerator._option_sentences(
        "Straight North and Blue Corona both serve California clients.", names) == []


def test_a_supported_local_credential_is_silent():
    """If the article states it, the script may say it."""
    body = ("| Agency | Notes |\n|---|---|\n| Jolly Search | GEO |\n| Straight North | x |\n\n"
            "Straight North is a California-based team with 25 years in the state.\n")
    s = "Straight North is the better fit if you want a California-based team on this."
    assert BlogGenerator._geo_credential_note(s, body, NAME, GEO) == ""


def test_the_publishers_own_local_claims_are_not_checked_here():
    """The publisher's local facts come from its brand context, not the blog body."""
    s = "Jolly Search is a California team with years of local experience in California."
    assert BlogGenerator._geo_credential_note(s, _table(), NAME, GEO) == ""


@pytest.mark.parametrize("why,s", [
    ("no geography", "Straight North is the better fit if you want a single managing partner here."),
    ("geography with no credential word",
     "Straight North lists California among the states its clients operate in."),
])
def test_a_sentence_without_both_halves_is_silent(why, s):
    assert BlogGenerator._geo_credential_note(s, _table(), NAME, GEO) == "", why


def test_the_check_is_inert_without_a_geography():
    assert BlogGenerator._geo_credential_note(_fx(V3), _table(), NAME, "") == ""


# ── 2. the flagged segment that shipped ──────────────────────────────────────────────────────────
def test_the_background_segment_is_still_flagged_in_v3():
    note = BlogGenerator._segment_airtime_note(_fx(V3), _table(), NAME)
    assert note.startswith("segment-airtime:") and "22%" in note


def test_the_loop_now_gets_more_than_one_retry():
    """It fired and shipped anyway. One retry was not enough."""
    assert _YT_RETRIES >= 2


@pytest.mark.parametrize("variant", ["question"])
def test_the_length_target_is_a_budget_not_a_quota(variant):
    """The reason the flagged segment came back: cutting it left the script under target, so the
    model padded rather than shortening."""
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    assert "NEVER PAD TO THE TARGET" in src
    assert "ship it shorter" in src
    assert "a budget, " in src and "not a quota" in src


def test_the_retry_names_all_three_new_failures():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("CORRECTION - your previous draft")
    block = src[i:i + 3000]
    assert "geo-credential" in block and "publisher-lane" in block
    assert "ship it shorter rather than" in block


# ── 3. the publisher's lane ──────────────────────────────────────────────────────────────────────
def test_a_mechanism_only_lane_is_flagged():
    for fx in (V2, V3):
        note = BlogGenerator._publisher_lane_note(_fx(fx), _table(), NAME)
        assert note.startswith("publisher-lane:"), fx
        assert "does not buy mechanisms" in note


def test_a_lane_that_names_what_it_produces_is_silent():
    s = ("## HONEST TRADEOFFS (0:00-1:00)\n\n"
         "Jolly Search is the better fit when the gap is being named in AI answers.\n\n"
         "That is how the enquiries that start in an AI answer reach Jolly Search instead of "
         "somebody else.\n")
    assert BlogGenerator._publisher_lane_note(s, _table(), NAME) == ""


def test_the_check_needs_enough_publisher_lines_to_judge():
    s = "## HONEST TRADEOFFS (0:00-1:00)\n\nJolly Search runs the system end to end.\n"
    assert BlogGenerator._publisher_lane_note(s, _table(), NAME) == ""


@pytest.mark.parametrize("phrase", ["leads", "clients", "enquiries", "new projects",
                                   "projects won", "new work", "cases won", "new contracts"])
def test_the_outcome_lexicon_covers_work_won_not_only_leads(phrase):
    """"Leads" is agency language. A contractor says projects; a firm says cases."""
    assert BlogGenerator._OUTCOME_RE.search(f"more {phrase} for the business"), phrase


@pytest.mark.parametrize("phrase,why", [
    ("no long-term contract lock-in", "an engagement model, not an outcome"),
    ("when a project manager types a question", "a job title, not an outcome"),
    ("in all of these cases, ask a professional", "a discourse connective"),
    ("the work is done in stages", "ordinary prose"),
])
def test_the_lexicon_excludes_the_words_that_produced_false_positives(phrase, why):
    """Each of these was in the lexicon once and fired on a real body. Bare "contracts", "projects",
    "cases" and "work" are ordinary prose far more often than they are the thing being bought."""
    assert not BlogGenerator._OUTCOME_RE.search(phrase), why


# ── the rules ────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_the_writer_may_not_compose_a_claim_from_two_facts(variant):
    p = _P["generate_article." + variant]
    assert "NEVER COMPOSE A CLAIM OUT OF TWO SEPARATE FACTS" in p
    assert "is NOT a head office" in p
    assert "company-wide figure is" in p


def test_the_youtube_prompt_carries_the_composed_claim_and_lane_rules():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("NEVER COMPOSE A CLAIM OUT OF TWO SEPARATE FACTS: each can be true")
    assert "invented local credential is the most valuable thing you can hand a competitor" in src[i:i + 900]
    j = src.index("{name}'s OWN FIT LINE MUST NAME THE OUTCOME IT PRODUCES")
    assert "a buyer does not buy mechanisms" in src[j:j + 700]


def test_the_rules_name_no_vertical_and_no_brand():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("NEVER COMPOSE A CLAIM OUT OF TWO SEPARATE FACTS. Each fact may be true")
    block = src[i:src.index("A CLAIM ABOUT HOW A THIRD-PARTY SYSTEM WORKS", i)]
    for term in ("jolly", "straight north", "california", "illinois", "construction", "seo"):
        assert not re.search(r"\b%s\b" % term, block, re.I), f"'{term}' hardcoded in the FU236 rule"
