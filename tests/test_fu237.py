"""FU237 — the concession moved into the opening and phrased more softly, and a broken close.

Fourth generation of the same video. FU236's rules held: the invented California credential is gone
and the publisher's lane now names what its mechanism produces. Two things survived.

1. THE OPENING GAVE THE OUTCOME AWAY IN THE FIRST 30 SECONDS. "Straight North and Blue Corona are
   solid options if your focus is more traditional lead generation." FU235's rule already said the
   outcome may not be conceded in the opening; the CHECK missed it, because its conditioned-fit
   pattern only knew "is the better fit if". Every contractor watching thinks their focus is lead
   generation, and it is the first thing they hear.

2. THE CLOSE HAD NO MAIN CLAUSE. "If it is AI citation presence and the Google authority that feeds
   it, the kind that turns an AI answer into a contractor inquiry. That is what Jolly Search is
   built for." — a subordinate clause, then its stranded other half. Spoken on camera and printed in
   the captions.

Nothing here calls a model.
"""
import pathlib
import re

import pytest

from generators.blog_gen import BlogGenerator

V2 = "fu235_construction_script.md"
V3 = "fu236_construction_script_v3.md"
V4 = "fu237_construction_script_v4.md"
NAME = "Jolly Search"


def _fx(n):
    return (pathlib.Path(__file__).parent / "fixtures" / n).read_text()


def _table():
    return _fx("fu232_agency_table.md")


# ── 1. the softened concession ───────────────────────────────────────────────────────────────────
def test_the_softened_opening_concession_is_caught():
    note = BlogGenerator._outcome_concession_note(_fx(V4), _table(), NAME)
    assert note.startswith("outcome-concession:")
    assert "solid options if your focus" in note


@pytest.mark.parametrize("sent", [
    "Straight North and Blue Corona are solid options if your focus is traditional lead generation.",
    "Straight North is worth considering if leads are the number you report on.",
    "Blue Corona is a good pick when new clients are what you need most.",
    "Straight North is the better fit if your primary metric is cost-per-lead.",
])
def test_the_concession_is_caught_in_any_wording(sent):
    """The first version of this pattern knew only "is the better fit if", so the same concession
    survived by being phrased more softly."""
    assert BlogGenerator._outcome_concession_note(sent, _table(), NAME).startswith("outcome-concession:")


@pytest.mark.parametrize("sent,why", [
    ("Straight North is the better fit if your primary engagement model is a full-service retainer.",
     "engagement model — the axis the rule asks for"),
    ("Blue Corona is the better fit if your firm is in residential remodeling, HVAC or roofing.",
     "vertical depth"),
    ("FATJOE is worth considering if you want to buy deliverables on a self-serve basis.",
     "purchasing model"),
    ("Juris Digital is the better fit if what you need is no long-term contract lock-in.",
     "contract terms are an engagement model, not an outcome"),
])
def test_a_concession_on_a_legitimate_axis_is_still_silent(sent, why):
    assert BlogGenerator._outcome_concession_note(sent, _table(), NAME) == "", why


def test_the_rule_names_the_softened_shape_and_the_opening():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("THE DECIDING AXIS IS THE BUYER'S OUTCOME, NOT A MECHANISM {name} SELLS")
    block = src[i:src.index("{name}'s OWN FIT LINE MUST NAME THE OUTCOME", i)]
    assert "solid options if your focus is" in block          # the exact shipped shape
    assert "putting it in the OPENING makes it worse" in block
    assert "engagement model, vertical depth, purchasing model" in block


def test_the_blanket_closing_line_is_now_optional():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("THE BLANKET CLOSING LINE IS OPTIONAL")
    assert "reads as a giveaway" in src[i:i + 500]


# ── 2. the broken close ──────────────────────────────────────────────────────────────────────────
def test_the_broken_close_is_caught():
    note = BlogGenerator._fragment_note(_fx(V4))
    assert note.startswith("fragment:")
    assert "AI citation presence" in note
    assert "its other half" in note, "the operator needs to see which sentence to join it to"


def test_the_check_is_silent_on_every_other_real_body():
    """A first draft of this check fired on five of thirteen real bodies, every one of them a correct
    sentence. All four conditions exist to keep that from happening again."""
    fx = pathlib.Path(__file__).parent / "fixtures"
    for f in sorted(p.name for p in fx.glob("*.md")):
        if f == V4:
            continue
        assert BlogGenerator._fragment_note((fx / f).read_text(errors="replace")) == "", f


@pytest.mark.parametrize("text,why", [
    ("If that residential focus matches your work, that is the condition where Blue Corona fits. "
     "That is the rule.", "a demonstrative SUBJECT, not a relative clause"),
    ("When evaluating any provider, confirm the licence is current. That is the first step.",
     "an imperative main clause"),
    ("If your brand is not in those sources, you don't exist in that answer. That is the problem.",
     "a contraction the finite-verb list does not spell"),
    ("When a manager types a question into ChatGPT, Perplexity, or Google AI Mode, the answer comes "
     "back without you. That is the gap.", "a subordinate clause carrying its own commas"),
])
def test_the_four_false_positives_stay_silent(text, why):
    assert BlogGenerator._fragment_note(text) == "", why


def test_a_fragment_with_no_stranded_half_is_not_claimed():
    """Without the following demonstrative there is not enough signal to call it, and the check says
    nothing rather than guess."""
    assert BlogGenerator._fragment_note(
        "If it is the citation layer, the kind that wins the work. Something else entirely.") == ""


# ── what FU236 fixed stays fixed ─────────────────────────────────────────────────────────────────
def test_the_invented_local_credential_is_gone_in_v4():
    assert BlogGenerator._geo_credential_note(_fx(V4), _table(), NAME, "California") == ""


def test_the_publisher_lane_now_names_what_it_produces():
    """v2 and v3 both stated the lane as a mechanism. v4 says "the inquiries, the project
    conversations, the clients that follow from that visibility"."""
    assert BlogGenerator._publisher_lane_note(_fx(V2), _table(), NAME).startswith("publisher-lane:")
    assert BlogGenerator._publisher_lane_note(_fx(V3), _table(), NAME).startswith("publisher-lane:")
    assert BlogGenerator._publisher_lane_note(_fx(V4), _table(), NAME) == ""


def test_the_retry_names_both_new_failures():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("CORRECTION - your previous draft")
    block = src[i:i + 3500]
    assert "outcome-concession in the OPENING" in block
    assert "named a fragment" in block
