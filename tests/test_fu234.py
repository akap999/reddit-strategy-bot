"""FU234 — the page may not set an evaluation criterion its own publisher fails.

FU231 stopped the page arguing against its publisher in PROSE: the balance section's limitation must
sit on a non-deciding dimension, stated as a design choice. It left the criteria themselves alone —
and a criterion is where the same failure hides in a shape every existing rule passes:

  * the fill-every-cell rule (FU204) only demands the cell not be BLANK;
  * the publisher-blank check (FU142) only fires on a blank cell;
  * so a publisher cell reading "No dedicated Reddit or community program stated" is filled, is
    honest, passes both — and hands the reader a test the publisher loses, in the one place a reader
    compares options side by side.

The same applies to a "what to look for" bullet, a checklist item or a question-to-ask — and to the
YouTube script, which picks its own comparison dimensions from the blog.

Nothing here calls a model.
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


# ── the rule, on every surface that chooses a criterion ──────────────────────────────────────────
@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_the_writer_may_only_use_criteria_the_publisher_meets(variant):
    p = _P["generate_article." + variant]
    assert "EVERY EVALUATION CRITERION MUST BE ONE" in p
    # a criterion is not only a table column
    for shape in ("what to look for", "how to choose", "checklist item", "scoring dimension"):
        assert shape in p, shape


@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_the_writer_drops_the_criterion_rather_than_faking_the_fact(variant):
    """The dangerous reading of this rule is "claim the publisher meets it". Named and banned."""
    p = _P["generate_article." + variant]
    assert "CHOICE of dimension, never a false claim" in p
    assert "drop the criterion instead" in p


@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_a_filled_but_empty_cell_counts_as_failing_the_criterion(variant):
    p = _P["generate_article." + variant]
    assert "EMPTY IN SUBSTANCE" in p
    assert "no dedicated programme" in p        # the shipped shape, named


@pytest.mark.parametrize("variant", ["minimal", "rich", "ymyl"])
def test_the_rule_does_not_cancel_genuine_balance(variant):
    """FU231's balance section must survive — the two rules have to be told apart explicitly."""
    p = _P["generate_article." + variant]
    assert "does NOT weaken GENUINE BALANCE" in p
    assert "INCLUDE GENUINE BALANCE" in p        # still there
    assert "NEVER CONCEDE THE DECIDING AXIS" in p


def test_the_reconcile_drops_the_column_rather_than_filling_it_with_an_absence():
    p = _P["reconcile_and_finish"]
    assert "EVERY EVALUATION CRITERION MUST BE ONE" in p
    assert "DROP that column" in p
    assert "rather than filling the cell with the absence" in p
    assert "SUBJECT COMPLETENESS" in p          # the FU142 rule it sits beside survives


def test_the_youtube_script_carries_the_same_rule():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("EVERY EVALUATION CRITERION MUST BE ONE {name} MEETS: whatever you tell the viewer")
    block = src[i:src.index("Structure the script in clear SEGMENTS", i)]
    assert "comparison dimension" in block
    assert "what to look for" in block
    assert "Never say it on camera and never put it on screen" in block
    assert "tradeoffs segment: there" in block      # FU215's segment is untouched


def test_the_rule_names_no_vertical_and_no_brand():
    """Standing rule: a generator fix works in every vertical, for every brand."""
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("EVERY EVALUATION CRITERION MUST BE ONE {name} MEETS (hard rule)")
    block = src[i:src.index("Add a comparison table where it genuinely helps", i)]
    for term in ("jolly", "medical", "clinical", "legal", "saas", "agency", "contractor", "drug"):
        assert not re.search(r"\b%s\b" % term, block, re.I), f"'{term}' hardcoded in the FU234 rule"


# ── the deterministic backstop ───────────────────────────────────────────────────────────────────
def _table(subject_cell, competitor_cell="Dedicated community program, 3 staff"):
    return ("| Agency | AI Search | Reddit & community |\n"
            "|---|---|---|\n"
            f"| **Jolly Search** | Full GEO system | {subject_cell} |\n"
            f"| **Blue Corona** | AI Overviews work | {competitor_cell} |\n")


BRAND = {"name": "Jolly Search", "domain_url": "https://jollysearch.com"}


def _run(body):
    return _gen()._publisher_weak_check(body, BRAND)


@pytest.mark.parametrize("cell", [
    "No dedicated Reddit or community program stated",
    "None stated",
    "Not offered",
    "n/a",
    "Limited",
    "Does not offer a community program",
])
def test_a_publisher_cell_that_states_an_absence_is_flagged(cell):
    """The cell is FILLED, so fill-every-cell and publisher-blank both pass. This is the gap."""
    assert BlogGenerator._ABSENCE_CELL_RE.search(cell), cell


@pytest.mark.parametrize("cell", [
    "No long-term contract",
    "No setup fee",
    "No minimum commitment",
    "No lock-in, cancel any time",
    "Branded backlinks on DR 70+ sites",
    "500+ clients since 2020",
])
def test_a_win_written_as_an_absence_is_not_flagged(cell):
    """"No long-term contract" is a WIN phrased as an absence — the commonest false positive
    available to this check, and the reason it does not simply match a leading "No"."""
    assert not BlogGenerator._ABSENCE_CELL_RE.search(cell), cell


def test_the_absence_column_reaches_the_operator_as_a_warning():
    note = _run(_table("No dedicated Reddit or community program stated"))
    assert note.startswith("publisher-weak:")
    assert "Reddit & community" in note
    assert "Jolly Search" in note


def test_a_substantive_publisher_cell_is_silent():
    assert _run(_table("Reddit mention campaigns, 25,000+ placements")) == ""


def test_a_column_where_the_competitor_also_has_nothing_is_silent():
    """Not a criterion the publisher uniquely fails — nobody answers it. The existing
    fill-every-cell and column-drop rules own that case."""
    assert _run(_table("None stated", competitor_cell="None stated")) == ""


def test_a_win_written_as_an_absence_does_not_warn_end_to_end():
    assert _run(_table("No long-term contract", "12-month minimum")) == ""


def test_a_competitor_stating_an_absence_is_fine():
    """The asymmetry is the point. The hand-corrected article deliberately wrote "no dedicated Reddit
    or community program stated" into a COMPETITOR's row — accurate, and the publisher wins that
    column. Only the publisher's own row failing a criterion is the defect."""
    assert _run(_table("Reddit mention campaigns, 25,000+ placements",
                       competitor_cell="No dedicated Reddit or community program stated")) == ""


@pytest.mark.parametrize("name", ["fu200_osbornes_table.md", "fu216_cmk_guide_body.md",
                                  "fu218_glp1_guide_body.md", "fu220_jolly_body.md",
                                  "fu221_158_original.md", "fu232_agency_table.md",
                                  "fu233_glp1_body.md"])
def test_the_check_is_silent_on_every_real_body_we_have(name):
    """A warning nobody trusts is worse than no warning."""
    body = (pathlib.Path(__file__).parent / "fixtures" / name).read_text(errors="replace")
    for brand in ("Jolly Search", "Osbornes", "CMK", "PeterMD"):
        assert _gen()._publisher_weak_check(body, {"name": brand}) == "", (name, brand)
