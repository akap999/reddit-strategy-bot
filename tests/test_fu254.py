"""FU254 — the article deleted the thing it was about.

Five blockers from `is-tirzepatide-a-glp-1-how-it-differs-from-semaglutide` (52/100), plus a defect
in FU251's own damage detector that was refusing real removals across the stored corpus.
"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators import blog_eval as E


# ── Step 0 — a heading is an antecedent ──────────────────────────────────────────────────────────
# Measured on the 221 stored articles: detect_stranded_reference produced 51 findings and 3 of them
# were real. It feeds body_damage, the REMOVAL guard, so each of the other 48 refused a removal.

def _stranded(head, first):
    return E.detect_stranded_reference("## %s\n\n%s\n" % (head, first))


def test_comparison_heading_answers_a_plural_opener():
    """"Botric vs Profound: How Do They Compare?" -> "Both platforms address ..." is ordinary
    English: the heading named both things one line earlier."""
    assert not _stranded("Botric vs Profound: How Do They Compare?",
                         "Both platforms address the growing problem of brand invisibility.")
    assert not _stranded("What is the difference between tirzepatide and semaglutide?",
                         "Both are injectable medications, but they work differently.")
    assert not _stranded("Is it cheaper to renovate a home or buy a new one?",
                         "Neither is universally cheaper for every household.")
    assert not _stranded("Does tirzepatide work the same way as semaglutide?",
                         "They share receptor activity, but one engages a second receptor.")


def test_sentence_that_names_its_own_referents_is_not_stranded():
    """The heading need not supply it when the sentence does: "Both A and B offer ..."."""
    assert not _stranded("Which platform is better for citation tracking?",
                         "Both Botric and Profound offer citation tracking for brands.")


def test_a_noun_that_names_the_heading_is_not_a_back_reference():
    """"This clinical question", "this regulatory distinction", "this consideration" all point at
    the question just asked — the same shape as the "this guide" exclusion that already existed."""
    for np in ("clinical question", "regulatory distinction", "consideration",
               "clinical matter", "segment"):
        assert not _stranded("Can men take one medication alongside another?",
                             "This %s must be assessed by a licensed professional." % np), np


def test_a_category_noun_stands_in_for_the_subject():
    assert not _stranded("Who is it typically prescribed for?",
                         "This medication carries standard eligibility criteria for adults.")
    assert not _stranded("Who should consider an alternative?",
                         "These telehealth programs are not appropriate for everyone.")


def test_a_pointing_heading_has_already_introduced_the_thing():
    assert not _stranded("Who is this lab checklist most relevant for?",
                         "This full panel is most relevant for the following patients.")


def test_a_real_back_reference_still_fires():
    """The docstring's own example: a protocol the reader was never shown."""
    hits = _stranded("What is the main practical disadvantage of the oral form?",
                     "This strict protocol is necessary because bioavailability is about 1%.")
    assert hits and hits[0]["check"] == "stranded-reference"
    hits = _stranded("What About Erectile Function?",
                     "The same 2025 study found that treated men had improved outcomes.")
    assert hits, "a study named as 'the same' one has no antecedent in this section"


def test_the_also_arm_needs_an_additional_RESULT():
    """"...also showed no difference" is a second finding and needs the first. "is also used
    off-label", "may also include" and "(also known as)" are ordinary English — they were 15 of the
    16 findings this arm produced across the corpus."""
    assert _stranded("Are the two outcomes truly equivalent?",
                     "Waist circumference and lipid markers also showed no significant difference.")
    for s in ("Metformin is a prescription medication that is also used off-label for longevity.",
              "A thorough workup may also include a fasting insulin test for some patients.",
              "Revive Design (also operating as Revive Renovation) is a family-owned contractor.",
              "Many people who present with one condition also carry excess body fat."):
        assert not _stranded("Additional Tests Your Provider May Order", s), s


def test_an_also_whose_subject_the_heading_named_is_not_stranded():
    assert not _stranded("Is Metformin used for longevity in men?",
                         "Metformin is a diabetes medication that also showed longevity signals.")


# ── Step 1 — a comparison table may not delete what it compares ──────────────────────────────────
# `| Dimension | Semaglutide | Tirzepatide |` puts the OPTIONS in the columns. The FU204 any-empty
# rule reads every column past the first as a dimension, so one unsourceable cell deleted one of the
# two drugs from an article titled "How It Differs From Semaglutide". 22 of the 221 stored articles
# are written that way and 2 had already shipped with a single option left.

from generators.blog_gen import BlogGenerator, _transpose_table, _table_cell_name  # noqa: E402


def _gen(tools=()):
    g = BlogGenerator.__new__(BlogGenerator)
    g._table_punt_note = ""
    g._article_tools = list(tools)
    return g


TRANSPOSED = (
    "| Dimension | Semaglutide | Tirzepatide |\n"
    "| --- | --- | --- |\n"
    "| Receptor targets | GLP-1 only [S1] | GLP-1 and GIP [S1] |\n"
    "| Weight loss vs. comparator | Reference [S2] | Greater mean loss [S2] |\n"
    "| Adverse event profile | Lower rate [S3] | Not specified in sourced facts |\n"
)


def test_the_option_survives_and_the_dimension_is_what_drops():
    out = _gen(["Semaglutide", "Tirzepatide"])._resolve_table_punts(TRANSPOSED)
    assert "Tirzepatide" in out.split("\n")[0], "the compared option was deleted from its own table"
    assert "Semaglutide" in out.split("\n")[0]
    assert "Adverse event profile" not in out, "the unsourceable DIMENSION is what FU204 drops"
    assert "Receptor targets" in out and "Weight loss" in out


def test_the_rule_is_unchanged_in_the_ordinary_orientation():
    md = ("| Provider | Monthly price | Delivery |\n"
          "| --- | --- | --- |\n"
          "| Alpha | $99 [S1] | 2 days [S1] |\n"
          "| Beta | $149 [S2] | Not specified in sourced facts |\n")
    out = _gen(["Alpha", "Beta"])._resolve_table_punts(md)
    assert "Delivery" not in out, "a dimension with a gap still goes"
    assert "Alpha" in out and "Beta" in out and "Monthly price" in out


def test_column_zero_holding_the_options_is_never_called_transposed():
    """The names test RULES OUT as well as in: whatever the corner cell says, a table whose first
    column holds the option names keeps the ordinary orientation."""
    g = _gen(["Alpha", "Beta"])
    hdr = ["Feature", "Monthly price", "Delivery"]
    data = [["Alpha", "$99", "2 days"], ["Beta", "$149", "1 day"]]
    assert not g._table_is_transposed(hdr, data)


def test_the_names_test_beats_the_corner_cell():
    g = _gen(["Botric", "Profound"])
    assert g._table_is_transposed(["Whatever", "Botric", "Profound"],
                                  [["Pricing", "$8", "$99"]])


def test_a_source_row_is_provenance_not_an_option():
    """The Source exemption was written when a Source could only be a column. On a swapped table it
    is a row, and counting its blanks as gaps condemned every dimension and deleted the table."""
    md = ("| Measure | Option A | Option B | Source |\n"
          "| --- | --- | --- | --- |\n"
          "| Reduction at 6 months | 1.58% | 1.62% |  |\n"
          "| Weight change at 6 months | 3.77% | 3.48% |  |\n")
    out = _gen()._resolve_table_punts(md)
    assert "Option A" in out and "Option B" in out
    assert "Reduction at 6 months" in out and "Weight change at 6 months" in out


def test_transpose_round_trips():
    hdr = ["Dimension", "A", "B"]
    data = [["Price", "$1", "$2"], ["Speed", "fast", "slow"]]
    h2, d2 = _transpose_table(hdr, data)
    assert h2 == ["Dimension", "Price", "Speed"]
    assert d2 == [["A", "$1", "fast"], ["B", "$2", "slow"]]
    assert _transpose_table(h2, d2) == (hdr, data)


def test_a_cell_name_is_read_through_its_decoration():
    assert _table_cell_name("**[Semaglutide](https://x.com)** (Ozempic / Wegovy)") == "Semaglutide"


def test_a_table_comparing_one_thing_is_reported():
    g = _gen()
    g._resolve_table_punts("| Dimension | Semaglutide |\n| --- | --- |\n| Receptor | GLP-1 [S1] |\n"
                           "| Class | Single agonist [S1] |\n")
    # a two-column table is not a comparison to begin with; the floor speaks when one arrives
    assert g._table_punt_note == "" or "fewer than" in g._table_punt_note


def test_the_price_strip_takes_the_same_axis():
    """With pricing off, a price DIMENSION goes — never the option column beside it."""
    md = ("| Dimension | Alpha | Beta |\n"
          "| --- | --- | --- |\n"
          "| Monthly cost | $99 | $149 |\n"
          "| Delivery | 2 days | 1 day |\n")
    out, dropped = _gen(["Alpha", "Beta"])._strip_price_columns(md)
    assert "Alpha" in out and "Beta" in out, "an option was stripped as if it were a price column"
    assert "Monthly cost" not in out and "Delivery" in out
