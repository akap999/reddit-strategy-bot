"""FU268 — the article arguing against the publisher's own materials.

Reported on a shipped article, three lines in one comparison:

  "In bottom-vented designs, narrow central air passages can be easily blocked, and dried milk can
   plug the passage and prove difficult to clean [S5]."  — Thyseed's system IS a bottom vent, and
   [S5] is a USPTO patent's prior-art section, which exists to criticise competing designs.
  "PPSU bottles release microparticles ranging from 53 to 393 particles/mL"  — no citation, and
   PPSU is the publisher's other material.
  "2024 independent testing found lead in … painted glass bottles"  — cited to a retailer.

The operator's rule, stated twice: never put the brand in a negative light or highlight its
weakness. `_self_disqualification_check` is the nearest existing rule and cannot see any of these —
its trigger is REGULATORY prohibition ("may not", "prohibited", "not FDA approved").
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.blog_gen import BlogGenerator as B  # noqa: E402

BRAND = {"name": "Thyseed", "category": "baby bottles",
         "context": "premium anti-colic baby bottles in borosilicate glass and PPSU with a base vent",
         "key_facts": json.dumps({"pricing": {"items": [
             {"product": "PPSU Natural Anti-colic Baby Bottle", "value": "$28.99"}]}})}

PATENT = {"label": "official · USPTO Patent US10864144 (prior art analysis)",
          "url": "https://image-ppubs.uspto.gov/dirsearch-public/print/downloadPdf/10864144",
          "text": "x" * 500}
RETAILER = {"label": "reference · Birch", "url": "https://birch.example/research", "text": "x" * 500}
FDA = {"label": "official · FDA", "url": "https://www.fda.gov/x", "text": "y" * 500}


def _gen():
    return B.__new__(B)


def _run(body, blocks):
    return _gen()._self_disparagement_check(body, BRAND, blocks)


def test_a_defect_claim_with_no_source_at_all_is_removed():
    body = ("## Materials\n\nPPSU bottles release microparticles ranging from 53 to 393 "
            "particles/mL during 100 opening and closing cycles.\n")
    out, note = _run(body, [])
    assert "microparticles" not in out
    assert "no source at all" in note


def test_a_defect_claim_sourced_to_a_patent_is_removed():
    """A patent's background section exists to argue that everything before it was inadequate. It
    is an adversarial description of competing designs, never a neutral finding."""
    body = ("## Venting\n\nIn bottom-vented designs, narrow central air passages can be easily "
            "blocked, and dried milk can plug the passage and prove difficult to clean [S1].\n")
    out, note = _run(body, [PATENT])
    assert "easily blocked" not in out
    assert "prior-art" in note


def test_a_defect_claim_sourced_to_a_retailer_is_removed():
    body = "## Glass\n\n2024 testing found glass bottles prone to chipping at the rim [S1].\n"
    out, note = _run(body, [RETAILER])
    assert "prone to chipping" not in out
    assert "not a materials authority" in note


def test_a_real_authority_is_KEPT_and_the_operator_is_told_to_answer_it():
    """Silently deleting a true safety fact on a YMYL page is its own failure — that is FU243's
    reasoning, and it still holds. The fix is to say where the brand stands, not to hide it."""
    body = ("## Materials\n\nThe FDA notes PPSU can degrade above certain sterilisation "
            "temperatures [S1].\n")
    out, note = _run(body, [FDA])
    assert "can degrade" in out, "a real authority's safety fact must not be silently removed"
    assert "say where Thyseed stands" in note


def test_a_defect_claim_about_something_the_brand_does_not_sell_is_left_alone():
    """The whole point is the DIRECTION: an article may say a rival's material cracks."""
    body = "## Materials\n\nStandard polypropylene can warp and discolour after repeated washing.\n"
    out, note = _run(body, [])
    assert "can warp" in out and not note


def test_a_sentence_that_already_says_where_the_brand_stands_is_left_alone():
    body = ("## Venting\n\nBottom-vented designs can be blocked by dried milk, though Thyseed's "
            "base vent is a single removable piece.\n")
    out, note = _run(body, [])
    assert "can be blocked" in out and not note


def test_an_acronym_material_is_visible_to_the_check():
    """PPSU is four characters, below the offering-token floor, so the publisher's own material was
    invisible. An acronym is the opposite of the case that floor guards against."""
    toks = B._brand_offering_tokens(BRAND, min_len=4, acronyms=True)
    assert "ppsu" in toks and "vent" in toks
    assert "ppsu" not in B._brand_offering_tokens(BRAND), "the default floor still excludes it"
    # A THREE-letter acronym needs the acronym branch — four characters clears the floor on its
    # own, so testing with PPSU alone proved nothing about that branch.
    short = dict(BRAND, context="bottles moulded in PVC-free PPSU with a base vent")
    assert "pvc" in B._brand_offering_tokens(short, min_len=4, acronyms=True)
    assert "pvc" not in B._brand_offering_tokens(short, min_len=4, acronyms=False)


def test_the_rule_names_no_product_and_no_industry():
    """`fixes-must-be-general`: the defect vocabulary must work for a lender or a fabricator too."""
    pattern = B._DISPARAGE_RE.pattern.lower()
    for word in ("bottle", "ppsu", "colic", "nipple", "baby", "semaglutide", "glp", "lead", "bpa"):
        assert word not in pattern, f"{word!r} hard-codes a vertical into the defect vocabulary"
    # and it still fires on a defect claim from a different industry entirely
    lender = {"name": "Acme Lending", "category": "small business loans",
              "context": "merchant cash advances with a fixed APR and no early repayment fee"}
    g = _gen()
    body = ("## Rates\n\nMerchant cash advances are prone to compounding costs when repayment "
            "is slow.\n")
    out, note = g._self_disparagement_check(body, lender, [])
    assert "prone to compounding" not in out and "no source at all" in note
