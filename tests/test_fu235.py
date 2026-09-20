"""FU235 — the video conceded the buyer's OUTCOME and spent a fifth of its runtime on work the
publisher does not do.

FU215 built the two-part close on purpose: concede the non-deciding axis to the field, resolve the
deciding one to the publisher. What it could not check is WHICH axis got conceded. On the reviewed
construction package the conceded half was lead volume — the result an agency buyer is actually
buying — and the half kept was AI citation visibility, which is the ROUTE to it:

    "If the gap you are closing is lead volume from Google, any of the agencies above will move it.
     If the gap is AI citation visibility for a California construction brand, that is what Jolly
     Search is built for."

    "Straight North is the better fit if your primary metric is cost-per-lead."

So the script says the competitors do the thing that pays and the publisher does the interesting new
thing — the same concession FU215 exists to prevent, one level up. The brand's own positioning runs
the other way ("be the brand ChatGPT names when your buyers ask who to hire" is a lead claim).

And segment 1 spent 75 seconds of a 5:45 script — 22% — on CSLB licensing, CEQA, seismic code and ADA
standards, naming no option at all, closing on "any agency you evaluate needs to understand those
four layers": a fifth of the video building an evaluation checklist its own publisher fails.

Nothing here calls a model.
"""
import pathlib
import re

import pytest

from generators.blog_gen import BlogGenerator, _SEG_AIRTIME_MAX
from tests.prompt_capture import capture

_P = capture()
NAME = "Jolly Search"


def _fx(n):
    return (pathlib.Path(__file__).parent / "fixtures" / n).read_text()


def _script():
    return _fx("fu235_construction_script.md")


def _table():
    return _fx("fu232_agency_table.md")


# ── the rules ────────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_the_blog_axis_is_the_readers_outcome_not_a_mechanism(variant):
    p = _P["generate_article." + variant]
    assert "THE DECIDING AXIS IS THE READER'S OUTCOME, NOT A MECHANISM YOU SELL" in p
    assert "NEVER CONCEDE THE OUTCOME TO THE FIELD" in p
    assert "ROUTE to the" in p
    assert "DO NOT UNDERSTATE" in p


@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_a_competitor_win_may_not_be_conditioned_on_the_outcome(variant):
    """The politest form of the concession, and the one that shipped."""
    assert "is a concession of the outcome, however politely it is phrased" in _P["generate_article." + variant]


@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_a_claim_about_how_a_platform_works_needs_a_source(variant):
    p = _P["generate_article." + variant]
    assert "A CLAIM ABOUT HOW A THIRD-PARTY SYSTEM WORKS NEEDS A SOURCE" in p
    assert "own working model" in p


def test_the_reconcile_carries_the_outcome_rules():
    p = _P["reconcile_and_finish"]
    assert "NEVER CONCEDE THE OUTCOME (FU235)" in p
    assert "A CLAIM ABOUT HOW A THIRD-PARTY SYSTEM WORKS NEEDS A SOURCE (FU235)" in p
    assert "STAY FAIR, NOT HEDGED" in p          # FU231's rule it sits beside survives


def test_the_youtube_prompt_carries_the_outcome_and_airtime_rules():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("THE DECIDING AXIS IS THE BUYER'S OUTCOME, NOT A MECHANISM {name} SELLS")
    block = src[i:src.index("{name}'s LIMIT: SAY WHAT IT IS BUILT FOR", i)]
    assert "NEVER CONCEDE THE OUTCOME" in block
    assert "concedes the outcome" in block
    j = src.index("EVERY SEGMENT MUST EARN ITS AIRTIME")
    seg = src[j:src.index("Structure the script in clear SEGMENTS", j)]
    assert "sentence of framing" in seg
    assert "any provider you evaluate needs to" in seg     # the shipped shape, named


def test_the_rules_name_no_vertical_and_no_brand():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("THE DECIDING AXIS IS THE READER'S OUTCOME")
    block = src[i:src.index("The limitation {name} states therefore sits", i)]
    for term in ("jolly", "construction", "contractor", "seo", "medical", "legal", "saas"):
        assert not re.search(r"\b%s\b" % term, block, re.I), f"'{term}' hardcoded in the FU235 rules"


# ── check 1: the outcome conceded ────────────────────────────────────────────────────────────────
def test_the_real_script_concedes_the_outcome():
    note = BlogGenerator._outcome_concession_note(_script(), _table(), NAME)
    assert note.startswith("outcome-concession:")
    assert "hands the outcome to the whole field" in note, "the blanket close"
    assert "conditions a competitor's win on the outcome" in note, "the cost-per-lead condition"


@pytest.mark.parametrize("sent,why", [
    ("Straight North is the better fit if you also want paid media managed by the same partner.",
     "a different SERVICE — the balance rules REQUIRE this shape"),
    ("Blue Corona is the better fit if your firm is in residential remodeling, roofing or HVAC.",
     "a buyer profile, not the outcome"),
    ("FATJOE is the better fit if you want to buy individual productized deliverables.",
     "an engagement model"),
    ("Any of these will handle a website rebuild for you.",
     "a blanket concession of something that is not the outcome"),
])
def test_a_legitimate_concession_is_silent(sent, why):
    assert BlogGenerator._outcome_concession_note(sent, _table(), NAME) == "", why


def test_the_publishers_own_conditioned_win_is_not_a_concession():
    s = ("Jolly Search is the better fit when the gap you are closing is AI citation visibility and "
         "the clients that come from it.")
    assert BlogGenerator._outcome_concession_note(s, _table(), NAME) == ""


def test_an_outcome_word_alone_is_not_enough():
    """The check needs a concession SHAPE too, or every sentence naming leads would fire."""
    s = "The system is built to turn AI citations into leads for a contracting brand."
    assert BlogGenerator._outcome_concession_note(s, _table(), NAME) == ""


# ── check 2: a segment that does not earn its airtime ────────────────────────────────────────────
def test_the_real_script_background_segment_is_flagged():
    note = BlogGenerator._segment_airtime_note(_script(), _table(), NAME)
    assert note.startswith("segment-airtime:")
    assert "SEGMENT 1" in note
    assert "22%" in note, "the operator should see the share, not just the name"


def test_a_segment_that_names_an_option_is_silent_however_long():
    s = ("## OPENING (0:00-0:30)\n\nShort.\n\n"
         "## SEGMENT 1 (0:30-3:00)\n\n" + ("Jolly Search does the work here. " * 20) +
         "\n\n## CLOSING (3:00-3:30)\n\nDone here now.\n")
    assert BlogGenerator._segment_airtime_note(s, _table(), NAME) == ""


def test_a_short_background_segment_is_silent():
    s = ("## OPENING (0:00-0:30)\n\nJolly Search is the pick.\n\n"
         "## SEGMENT 1 (0:30-0:50)\n\n" + ("Background about the state of the market. " * 8) +
         "\n\n## CLOSING (0:50-5:00)\n\nJolly Search again here.\n")
    assert BlogGenerator._segment_airtime_note(s, _table(), NAME) == ""


def test_a_script_without_timestamped_segments_is_inert():
    s = "## OPENING\n\nSome words here that go on.\n\n## SEGMENT 1\n\nMore words here entirely.\n"
    assert BlogGenerator._segment_airtime_note(s, _table(), NAME) == ""


def test_the_threshold_is_tunable_and_sane():
    assert 0.05 <= _SEG_AIRTIME_MAX <= 0.5


def test_both_checks_are_inert_without_a_brand_name():
    assert BlogGenerator._outcome_concession_note(_script(), _table(), "") == ""
    assert BlogGenerator._segment_airtime_note(_script(), _table(), "") == ""


def test_a_discourse_connective_is_not_a_concession():
    """The one false positive this check made on a real body: "In all of these cases, a licensed
    physician determines the course of action" — "cases" meaning situations, and "in all of these"
    being a connective rather than a concession."""
    s = ("In all of these cases, a licensed physician, not a self-assessment, determines the "
         "appropriate course of action.")
    assert BlogGenerator._outcome_concession_note(s, _table(), NAME) == ""


@pytest.mark.parametrize("name,brand", [
    ("fu232_agency_table.md", "Jolly Search"), ("fu220_jolly_body.md", "Jolly Search"),
    ("fu200_osbornes_table.md", "Osbornes"), ("fu216_cmk_guide_body.md", "CMK"),
    ("fu218_glp1_guide_body.md", "PeterMD"), ("fu221_158_original.md", "PeterMD"),
    ("fu233_glp1_body.md", "PeterMD"),
])
def test_the_outcome_check_is_silent_on_every_other_real_body(name, brand):
    """A warning nobody trusts is worse than no warning."""
    body = _fx(name)
    assert BlogGenerator._outcome_concession_note(body, body, brand) == "", name
