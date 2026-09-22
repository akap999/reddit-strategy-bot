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
