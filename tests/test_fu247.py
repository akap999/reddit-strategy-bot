"""FU247 — five mechanical defects that shipped in one comparison article.

The layout and the brand placement were right. What went out was broken in ways nothing in the
pipeline could see, because each one sits in a guard that was almost right:

  1. "Hims is named in the brand context as a GLP-1 telehealth provider for men. com for current
     plan details." Two failures in one paragraph. The second half is a punt sentence the scrub cut
     in half: `[^.\\n]*` stops at the first dot it meets, and the first dot was the one in
     "hims.com", so "…visit hims." was removed and " com for current plan details." shipped as a
     sentence. The first half is the prompt talking about itself, in an article that has no prompt.
  2. The section carrying both of those had no citation at all. The reconcile had dropped it — a
     compared option with no tool-specific fresh fact is removed, by its own rule — and the FU54
     substance guard put the draft's copy back, after the call to action.
  3. The "Medication Included in Fee?" column repeated the monthly price for three of four rows.
     That header contains "fee", so the price-cell writer treated the column that asks WHETHER
     medication is included as the column that says what it COSTS.
  4. Three FAQ answers in the JSON-LD stopped mid-word — "cardiovascular histo", "This is not a ",
     "Men evaluating programs th" — a hard 700-character slice through whatever letter sat there.

Everything below is verified against the real export.
"""
import re

import pytest

from generators.blog_gen import (BlogGenerator, _PRICE_DIM_RE, _is_price_column, _parse_faq_pairs,
                                 _trim_to_sentence)
from tests.stubs import StubClaude


def _gen():
    return BlogGenerator(StubClaude(), None)


# ══ 1. the punt scrub cut inside a domain ══════════════════════════════════════════════════════════
SHIPPED = ("Hims is named in the brand context as a GLP-1 telehealth provider for men. "
           "Hims pricing is not confirmed in the available sources; visit hims.com for current "
           "plan details.")


def test_the_punt_sentence_goes_whole_instead_of_leaving_its_tail():
    out = _gen()._scrub_punts(SHIPPED)
    assert "com for current plan details" not in out, "the fragment that shipped"
    assert "hims.com" not in out
    assert "Hims is named in the brand context" in out          # the OTHER defect, fix 2's job


@pytest.mark.parametrize("sent", [
    "Pricing is not confirmed in the available sources; visit hims.com for current plan details.",
    "Plan pricing varies; check ro.co/weight-loss/pricing for current plan details.",
    "See example.co.uk for pricing and terms.",
    "Refer to noom.com for current plan pricing.",
])
def test_a_punt_carrying_a_domain_is_removed_in_one_piece(sent):
    out = _gen()._scrub_punts("The programme is online. " + sent)
    assert out.strip() == "The programme is online."


def test_a_real_sentence_break_still_ends_the_match():
    """The dot rule must not swallow the sentence that follows the punt."""
    body = "Pricing varies by plan. The programme ships nationwide and includes consults."
    out = _gen()._scrub_punts(body)
    assert "The programme ships nationwide and includes consults." in out


def test_a_real_availability_fact_is_not_a_punt():
    """"not available in AL and ID" is a state-availability FACT this very article needed to carry.
    Widening the punt lexicon to catch more domain-bearing sentences would have eaten it."""
    body = "The subscription is not available in AL, ID, AK or HI."
    assert _gen()._scrub_punts(body) == body


def test_an_ordinary_sentence_with_a_domain_is_untouched():
    body = "Orders are placed at ro.co and shipped within two business days to the patient."
    assert _gen()._scrub_punts(body) == body


# ══ 2. the prompt talking about itself ═════════════════════════════════════════════════════════════
@pytest.mark.parametrize("sent", [
    "Hims is named in the brand context as a GLP-1 telehealth provider for men.",
    "Acme is listed in the brand context as a competitor in this category.",
    "Per the brand context, Acme serves small manufacturers across the region.",
    "The brand context names Acme as the leading option for this use case.",
])
def test_prompt_narration_is_scrubbed(sent):
    out = _gen()._scrub_meta("The market has several options. " + sent)
    assert "brand context" not in out
    assert "The market has several options." in out


def test_ordinary_prose_about_context_survives():
    body = ("Acme is named in the report as a leading supplier. The brand has served the sector "
            "for a decade.")
    assert _gen()._scrub_meta(body) == body


def test_the_whole_shipped_paragraph_disappears():
    out = _gen()._scrub_meta(_gen()._scrub_punts(SHIPPED))
    assert out.strip() == ""


# ══ 3. an uncited profile the reconcile dropped is not "substance the rewrite lost" ════════════════
DRAFT = ("## How does each programme work?\n\nIntro.\n\n"
         "### Acme\n\nAcme runs a men's programme [S1].\n\n"
         "### Bravo\n\nBravo is named in the brand context as a provider. It has offered this.\n\n"
         "## Checklist\n\nConfirm your prescriber is licensed in your state.\n")
REVISED = ("## How does each programme work?\n\nIntro.\n\n"
           "### Acme\n\nAcme runs a men's programme [S1].\n")


def test_an_uncited_dropped_competitor_profile_is_not_restored():
    g = _gen()
    g._article_tools = ["Acme", "Bravo"]
    out = g._restore_dropped_sections(DRAFT, REVISED)
    assert "### Bravo" not in out
    assert "brand context" not in out


def test_an_uncited_section_that_is_not_a_compared_option_is_still_restored():
    """FU54 exists to bring back a checklist or a policy section the rewrite deleted. That is
    unchanged — the new rule is scoped to a profile of a NAMED compared option."""
    g = _gen()
    g._article_tools = ["Acme", "Bravo"]
    out = g._restore_dropped_sections(DRAFT, REVISED)
    assert "## Checklist" in out
    assert "Confirm your prescriber is licensed in your state." in out


def test_a_dropped_competitor_profile_that_IS_cited_is_still_restored():
    g = _gen()
    g._article_tools = ["Acme", "Bravo"]
    draft = DRAFT.replace("It has offered this.", "It has offered this since 2019 [S4].")
    out = g._restore_dropped_sections(draft, REVISED)
    assert "### Bravo" in out


def test_the_rule_is_inert_when_the_article_uses_no_citations_at_all():
    g = _gen()
    g._article_tools = ["Acme", "Bravo"]
    draft = DRAFT.replace(" [S1]", "")
    out = g._restore_dropped_sections(draft, REVISED.replace(" [S1]", ""))
    assert "### Bravo" in out


# ══ 4. the yes/no column is not the price column ═══════════════════════════════════════════════════
@pytest.mark.parametrize("header,want", [
    ("Monthly Cost (GLP-1)", True),
    ("Starting price", True),
    ("Cost per month", True),
    ("Medication Included in Fee?", False),      # the reported one — contains "fee"
    ("Is medication covered?", False),
    ("Does the plan include shipping?", False),
    ("Source", False),
    ("Men's-Specific Care", False),
])
def test_only_a_money_column_is_a_price_column(header, want):
    assert _is_price_column(header) is want


def test_the_reported_header_used_to_match_and_no_longer_does():
    assert _PRICE_DIM_RE.search("Medication Included in Fee?")      # why it broke
    assert not _is_price_column("Medication Included in Fee?")      # why it will not again


def test_the_price_writer_and_the_column_droppers_share_the_test():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    code = re.sub(r"(?m)#.*$", "", re.sub(r'"""(?:.|\n)*?"""', "", src))
    assert 'if _PRICE_DIM_RE.search(header[ci] or "")' not in code
    assert code.count("_is_price_column(header[ci])") >= 2


# ══ 5. a schema answer must not stop mid-word ══════════════════════════════════════════════════════
def test_the_three_reported_answers_end_on_a_sentence():
    long = "Sentence one is here. " * 40                      # well past the 700-char cap
    for tail in ("and goals. This is not a decision to make without clinical evaluation.",
                 "with their provider. Men evaluating programmes should confirm compliance.",
                 "or other medications. This content does not constitute medical advice."):
        out = _trim_to_sentence(long + tail, 700)
        assert len(out) <= 700
        assert out.endswith((".", "!", "?")), out[-40:]


def test_a_short_answer_is_returned_whole():
    s = "A complete short answer."
    assert _trim_to_sentence(s, 700) == s


def test_text_with_no_sentence_end_falls_back_to_a_word_boundary_not_a_letter():
    s = "word " * 300
    out = _trim_to_sentence(s, 700)
    assert len(out) <= 700 and not out.endswith("wor") and out.endswith("word")


def test_the_real_faq_answers_all_end_cleanly():
    """Driven by the real export: six answers, three of which used to stop mid-word."""
    import html as _h
    raw = open("/Users/anishkapoor/Downloads/best-glp-1-telehealth-programs-for-men-in-the-us-2026.html",
               encoding="utf-8", errors="ignore").read()
    if "<h2>FAQ</h2>" not in raw:
        pytest.skip("the export is not on this machine")
    seg = raw.split("<h2>FAQ</h2>", 1)[1].split("<h2>Sources</h2>", 1)[0]
    md = re.sub(r"(?is)<h3[^>]*>(.*?)</h3>",
                lambda m: "\n\n### " + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "\n", seg)
    md = "## FAQ\n" + _h.unescape(re.sub(r"(?s)<[^>]+>", "", re.sub(r"(?is)</p>", "\n\n", md)))
    pairs = _parse_faq_pairs(md)
    assert len(pairs) == 6
    for p in pairs:
        assert p["a"].endswith((".", "!", "?")), (p["q"], p["a"][-50:])
