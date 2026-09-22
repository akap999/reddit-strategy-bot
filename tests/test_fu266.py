"""FU266 — a citation pointing at a real page that says something else, and a cut mid-name.

Seven defects were reported on one article. They reduce to three causes, and the two biggest are
here: a sentence splitter that treated the dot in "Dr. Brown's" as a sentence end, and a claim-
support test that scored the words naming the TOPIC instead of the words carrying the CLAIM.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.blog_eval import body_damage, detect_orphan_fragment  # noqa: E402
from generators.blog_gen import _split_sentences, _split_sentences_keep  # noqa: E402


# ── the splitter ─────────────────────────────────────────────────────────────────────────────────
# Every splitter in blog_gen was `re.split(r"(?<=[.!?])\s+", …)`. A removal pass cut
# "…simpler and cheaper than Dr." out of a shipped article and stranded "Brown's."

def test_an_abbreviation_is_not_a_sentence_end():
    """The reported case, exactly."""
    assert _split_sentences("Pigeon's vent is simpler and cheaper than Dr. Brown's.") == \
        ["Pigeon's vent is simpler and cheaper than Dr. Brown's."]


def test_the_abbreviations_that_turn_up_in_this_corpus():
    for text in ("Mrs. Smith and Mr. Jones met at St. Mary Inc. offices.",
                 "Use a slow flow, e.g. Level 1.",
                 "That is, i.e. the narrow one.",
                 "It is made in the U.S. only.",
                 "See Fig. 2 for the vent.",
                 "Compare vs. the standard bottle.",
                 "Written by J. K. Rowling."):
        assert _split_sentences(text) == [text], text


def test_real_sentence_boundaries_still_split():
    assert _split_sentences("One sentence. Two sentences. Three.") == \
        ["One sentence.", "Two sentences.", "Three."]


def test_a_single_letter_alone_is_a_sentence_end_not_an_initial():
    """Under-splitting is not free: a removal would then take the following sentence too."""
    assert _split_sentences("It scored grade A. The next one failed.") == \
        ["It scored grade A.", "The next one failed."]


def test_a_decimal_is_not_a_boundary():
    assert _split_sentences("Costs $5.40 per unit. That is cheap.") == \
        ["Costs $5.40 per unit.", "That is cheap."]


def test_the_rewriting_variant_rejoins_byte_for_byte():
    """`_claim_source_check` rewrites a line in place, so its split must be lossless."""
    t = "A costs more than Dr. Brown's. B is cheaper. "
    bits = _split_sentences_keep(t)
    assert "".join(bits) == t
    assert bits[0] == "A costs more than Dr. Brown's."
    assert bits[1::2] == [" ", " "], "odd indexes must be the separators the caller skips"


def test_empty_and_punctuationless_text_do_not_raise():
    assert _split_sentences("") == []
    assert _split_sentences("no terminal punctuation") == ["no terminal punctuation"]


# ── the damage the guard could not see ───────────────────────────────────────────────────────────
# `body_damage` is what `_apply_removals_without_damage` consults. It returned [] for the stranded
# fragment, so the guard committed the cut instead of widening it to the paragraph.

REPORTED = """## Pigeon

Pigeon is an established baby feeding brand offering a peristaltic nipple design.

**Best fit for:** Parents looking for an alternative nipple geometry.

Brown's.

## What makes a breastfed baby refuse the bottle?

Bottle refusal comes from nipple feel, flow rate, or gas.
"""


def test_the_stranded_fragment_is_damage():
    hits = detect_orphan_fragment(REPORTED)
    assert len(hits) == 1
    assert hits[0]["check"] == "orphan-fragment"
    assert "Brown's." in hits[0]["detail"]


def test_it_reaches_the_guard_through_body_damage():
    """The detector existing is not the fix — the GUARD has to see it."""
    assert [h["check"] for h in body_damage(REPORTED)] == ["orphan-fragment"]


def test_a_gutted_list_item_is_damage_too():
    """The one this detector found in the stored corpus (blog 220): a numbered item whose content
    was removed, leaving the marker behind."""
    body = "## Steps\n\n1. **Confirm your payer type.** The rules differ by category.\n\n2.\n"
    assert [h["check"] for h in detect_orphan_fragment(body)] == ["orphan-fragment"]


def test_an_intact_numbered_item_is_not_damage():
    body = ("## Steps\n\n1. **Confirm your payer type.** The rules differ by category.\n\n"
            "2. **Determine the indication.** Coverage requires it under Part D.\n")
    assert not detect_orphan_fragment(body)


def test_a_short_real_sentence_is_not_damage():
    """The floor has to let ordinary short prose through or every removal gets refused."""
    for ok in ("Prices vary by retailer.", "It costs $8.99.", "Results differ.",
               "Shipping is free."):
        assert not detect_orphan_fragment(f"## H\n\n{ok}\n"), ok


def test_a_heading_is_not_a_fragment():
    assert not detect_orphan_fragment("## Anti-colic bottles\n\nThey vent air out of the bottle.\n")


# ── the gate under everything ────────────────────────────────────────────────────────────────────
# Three regexes decided BOTH which sentences were support-checked AND which pages were fetched at
# all. Measured on the reported article: 35 of 64 cited sentences matched none of them, and 2 of 20
# cited sources were never opened — so the operator's rule ("a page unreachable by every method is
# not a source") could not be applied to them, and both shipped in the Sources list.

from generators.blog_gen import BlogGenerator as B, _looks_walled_text  # noqa: E402


def test_an_organisation_spelled_out_in_full_is_recognised():
    """The reported article's OPENING line. "The American Academy of Pediatrics" broke the
    capitalised run on the lowercase "of", so the spelled-out form — how a first mention is normally
    written — matched nothing and its page was never fetched."""
    m = B._ORG_SAYS_RE.search(
        "The American Academy of Pediatrics recommends trying a different nipple type [S1].")
    assert m and m.group(1) == "American Academy of Pediatrics"


def test_the_acronym_form_still_matches():
    m = B._ORG_SAYS_RE.search("The AAP recommends that breastfeeding be established first [S1].")
    assert m and m.group(1) == "AAP"


def test_a_name_introducing_its_own_acronym_matches():
    m = B._ORG_SAYS_RE.search(
        "The American Academy of Pediatrics (AAP) recommends exclusive breastfeeding [S20].")
    assert m and m.group(1) == "American Academy of Pediatrics"


def test_and_inside_a_name_matches_but_two_entities_do_not():
    """`and` was the risky part of widening: it is name-internal in "Food and Drug Administration"
    and a conjunction in "ChatGPT and Perplexity". Requiring TWO more capitalised words after it
    separates them — measured over the 221 stored articles the widening gained 6 matches, all real
    authorities, and lost none."""
    m = B._ORG_SAYS_RE.search("The Food and Drug Administration requires testing [S6].")
    assert m and m.group(1) == "Food and Drug Administration"
    assert not B._ORG_SAYS_RE.search("ChatGPT and Perplexity recommend checking the source [S2].")


class _StubClaude:
    def __init__(self):
        self.usage = {}

    def reset_usage(self):
        pass


def _probe_gen(pages=None):
    g = B.__new__(B)
    g._claim_pages = dict(pages or {})
    g.claude = _StubClaude()
    return g


def test_a_source_cited_only_by_ordinary_prose_is_opened():
    """It used to be skipped, so it could never be found unreachable. A block with a URL we cannot
    read and no text is recorded as unreadable ONLY if something tried to open it."""
    blocks = [{"label": "official · Guidance", "url": "", "text": ""}]
    g = _probe_gen()
    assert g._probe_cited_sources("## H\n\nBottles come in several shapes. [S1]\n", blocks) == {1}


def test_a_source_the_article_never_cites_is_not_opened():
    """The probe follows the article's citations, not the evidence pile — otherwise every gathered
    block costs a fetch."""
    blocks = [{"label": "a", "url": "", "text": ""}, {"label": "b", "url": "", "text": ""}]
    g = _probe_gen()
    assert g._probe_cited_sources("## H\n\nOnly the first is cited. [S1]\n", blocks) == {1}


def test_held_text_that_is_a_bot_wall_does_not_count_as_a_read_page():
    """"Long enough" is not "read". A challenge page served with status 200 is real text of real
    length, and holding it counted as holding the page — so the source was never re-read and never
    classed unreachable."""
    wall = "Just a moment... Enable JavaScript and cookies to continue. " * 20
    assert len(wall) >= B._PAGE_TEXT_MIN and _looks_walled_text(wall)
    g = _probe_gen()
    blocks = [{"label": "x", "url": "", "text": wall}]
    assert g._probe_cited_sources("## H\n\nA claim. [S1]\n", blocks) == {1}


def test_real_held_page_text_is_still_trusted():
    real = "Thyseed bottles use a base vent that keeps the nipple full of milk. " * 10
    assert not _looks_walled_text(real)
    g = _probe_gen()
    blocks = [{"label": "x", "url": "https://x.example/p", "text": real}]
    assert g._probe_cited_sources("## H\n\nA claim. [S1]\n", blocks) == set()


# ── a cited claim judged against the page it cites ───────────────────────────────────────────────
# Measured with the topic's own words removed from the score, on the real pages:
#
#     WRONG  "…after the first few weeks … milk supply and latch"   0.25
#     WRONG  "…transitioning … toward a cup at 12 months"           0.40
#     RIGHT  "…trying a different nipple or bottle type"            0.40
#     WRONG  cell "Internal anti-colic venting system"              0.75
#     RIGHT  the brand's own wording, "base vent …"                 0.67
#
# The WRONG cell scores higher than the RIGHT one. No share separates those, so the token score is
# a filter and `_vx_judge_pages` decides.

AAP_PAGE = ("Introducing the Bottle. Many breastfeeding parents wonder when to introduce a bottle. "
            "If your baby refuses, try a different nipple or bottle type. Have someone other than "
            "the mother offer the bottle. " * 6)
THY_PAGE = ("Thyseed PPSU Natural Anti-colic Baby Bottle. The base vent keeps the nipple full of "
            "milk rather than air throughout the feed. " * 8)


class _JudgeClaude:
    """Returns a fixed verdict per ref, and records what it was asked."""

    def __init__(self, verdicts, says):
        self._v, self._says, self.prompts = verdicts, says, []

    def call(self, prompt, **kw):
        self.prompts.append(prompt)
        refs = re.findall(r"^\[(c\d+)\]", prompt, re.M)
        return {"verdicts": [{"ref": r, "status": self._v.get(r, "confirmed"), "page": "P1",
                              "page_says": self._says.get(r, "")} for r in refs]}


import re  # noqa: E402


def _judge_gen(verdicts, says, pages):
    g = B.__new__(B)
    g.claude = _JudgeClaude(verdicts, says)
    g._claim_pages = dict(pages)
    return g


BLOCKS = [{"label": "official · AAP", "url": "https://aap.example/bottle", "text": ""},
          {"label": "Thyseed", "url": "https://thyseed.example/ppsu", "text": ""}]
PAGES = {"https://aap.example/bottle": (AAP_PAGE, "direct"),
         "https://thyseed.example/ppsu": (THY_PAGE, "direct")}
TOPIC = ["baby", "bottle", "bottles", "breastfed", "breastfeeding", "refuses", "feeding"]


def test_the_topic_vocabulary_is_not_evidence():
    """A topic word on a cited page proves nothing — the source was chosen for being about the
    topic. Scoring it is what let a fabricated AAP sentence read as 62% supported."""
    toks = B._specific_tokens(
        "breastfeeding be established before introducing a bottle, typically after the first few "
        "weeks of life, to avoid interfering with milk supply and latch", TOPIC)
    assert "breastfeeding" not in toks and "bottle" not in toks
    for w in ("established", "typically", "weeks", "interfering", "supply", "latch"):
        assert w in toks, w


def test_a_six_character_prefix_not_four():
    """FU263 measured that four characters makes it worse: "control"[:4] matches "contain"."""
    assert "control" in B._specific_tokens("control group results", ["contain"])


def test_a_claim_the_page_does_not_make_is_removed():
    body = ("## Timing\n\nThe AAP recommends that breastfeeding be established before introducing "
            "a bottle, typically after the first few weeks of life, to avoid interfering with milk "
            "supply and latch [S1].\n\nOther guidance differs.\n")
    g = _judge_gen({"c1": "not_on_page"}, {}, PAGES)
    out, note = g._cited_claim_check(body, BLOCKS, set(), TOPIC)
    assert "milk supply and latch" not in out
    assert "cited-claim: 1 claim" in note and "does not say it" in note


def test_a_table_cell_is_judged_even_when_its_words_ARE_on_the_page():
    """The reported Thyseed cell. It scores 0.75 because the page shares its vocabulary and differs
    on the one word that names the mechanism — the page says "base vent", the cell says "internal".
    A share test ranks it ABOVE a correct cell, so a cell always goes to the judge."""
    body = ("## Compare\n\n| Dimension | Thyseed |\n|---|---|\n"
            "| Anti-colic system | Internal anti-colic venting system [S2] |\n")
    g = _judge_gen({"c1": "contradicted"}, {"c1": "The base vent keeps the nipple full of milk"},
                   PAGES)
    out, note = g._cited_claim_check(body, BLOCKS, set(), TOPIC)
    assert "Internal anti-colic venting system" not in out
    assert "says something different" in note


def test_a_confirmed_claim_is_left_alone():
    body = ("## Timing\n\nThe AAP recommends trying a different nipple or bottle type when refusal "
            "occurs [S1].\n")
    g = _judge_gen({"c1": "confirmed"}, {"c1": "try a different nipple or bottle type"}, PAGES)
    out, note = g._cited_claim_check(body, BLOCKS, set(), TOPIC)
    assert out == body and not note


def test_a_claim_whose_sources_are_all_unreadable_is_not_judged():
    """"Unread is UNKNOWN, never unsupported" — an unreachable source is the walled check's job,
    and judging against a page nobody read would invent a verdict."""
    body = "## Timing\n\nThe AAP recommends something about weeks and latch and supply [S1].\n"
    g = _judge_gen({"c1": "not_on_page"}, {}, {})
    out, note = g._cited_claim_check(body, BLOCKS, set(), TOPIC)
    assert out == body and not note
    g2 = _judge_gen({"c1": "not_on_page"}, {}, PAGES)
    assert g2._cited_claim_check(body, BLOCKS, {1, 2}, TOPIC) == (body, "")


def test_an_uncited_sentence_is_not_this_checks_business():
    body = "## Timing\n\nBottles come in several shapes and sizes for newborn feeding.\n"
    g = _judge_gen({"c1": "not_on_page"}, {}, PAGES)
    assert g._cited_claim_check(body, BLOCKS, set(), TOPIC) == (body, "")


# ── an unreachable page is not a source ──────────────────────────────────────────────────────────

def test_an_org_claim_resting_only_on_an_unreadable_page_is_removed():
    """The walled rule only acted on a unit carrying a figure or an attributed label, so a claim of
    the shape "The AAP recommends X" — no number, no label — was kept while its marker was stripped,
    leaving the assertion with no source at all where there had at least been a visible one."""
    g = B.__new__(B)
    g._claim_pages = {}
    blocks = [{"label": "official · AAP", "url": "https://aap.example/p", "text": ""}]
    body = ("## Timing\n\nThe AAP recommends that breastfeeding be established before introducing "
            "a bottle [S1].\n")
    out, note = g._walled_source_check(body, blocks, {1})
    assert "The AAP recommends" not in out
    assert "[S1]" not in out


def test_a_general_statement_keeps_its_prose_and_loses_its_marker():
    """Deliberately NOT widened to every unit: the page stops being a source either way, because
    the marker is stripped and the list is rebuilt. Deleting ordinary writing that never leaned on
    the page is a different act."""
    g = B.__new__(B)
    g._claim_pages = {}
    blocks = [{"label": "x", "url": "https://x.example/p", "text": ""}]
    body = "## Basics\n\nBottles are washed before the first use [S1].\n"
    out, _note = g._walled_source_check(body, blocks, {1})
    assert "Bottles are washed before the first use" in out and "[S1]" not in out


# ── a named study: cite it or cut it ─────────────────────────────────────────────────────────────
# `_study_reference_check` already SPOTTED this — it matches "A randomised controlled trial
# found…" — but it only warned, and it de-duplicated by wording so the body copy and the FAQ copy
# were reported as one line.

_RCT = ("A randomised controlled trial found that the amount of time infants spent in colic was "
        "not statistically significantly different between those using a standard bottle and "
        "those using a fully vented anti-colic bottle design.")
_TRIAL_PAGE = ("Feeding bottles with different venting methods and gastrointestinal discomfort. "
               "A randomised clinical trial of infants found the amount of time spent in colic was "
               "not statistically significantly different between a standard bottle and a fully "
               "vented anti-colic bottle design. " * 4)


def _study_gen(pages):
    g = B.__new__(B)
    g._claim_pages = dict(pages)
    return g


def test_a_named_study_with_no_source_anywhere_is_cut():
    """The reported case. It appears in the body AND in an FAQ answer, and neither carries a
    marker — both go, because de-duplicating them is what reported two occurrences as one."""
    body = f"## Gas\n\n{_RCT}\n\n## FAQ\n\n### Do they work?\n\n{_RCT}\n"
    blocks = [{"label": "Thyseed", "url": "https://thyseed.example/p", "text": "A bottle. " * 40}]
    g = _study_gen({})
    out, note = g._uncited_study_check(body, blocks, set())
    assert "randomised controlled trial" not in out
    assert "removed 2" in note, note


def test_the_study_is_CITED_when_the_evidence_has_the_paper():
    """Cut is the fallback, not the rule — the operator asked for cite-or-cut in that order."""
    body = f"## Gas\n\n{_RCT}\n"
    blocks = [{"label": "official · Feeding bottles trial",
               "url": "https://pubmed.ncbi.nlm.nih.gov/41885859/", "text": _TRIAL_PAGE}]
    g = _study_gen({})
    out, note = g._uncited_study_check(body, blocks, set())
    assert "randomised controlled trial [S1]" in out
    assert "cited 1 from the evidence" in note


def test_a_page_that_is_not_a_paper_is_never_offered_as_a_study_source():
    """A brand page that happens to share the wording is not the trial."""
    body = f"## Gas\n\n{_RCT}\n"
    blocks = [{"label": "Thyseed", "url": "https://thyseed.example/p", "text": _TRIAL_PAGE}]
    g = _study_gen({})
    out, note = g._uncited_study_check(body, blocks, set())
    assert "randomised controlled trial" not in out and "removed 1" in note


def test_a_study_that_is_already_cited_is_left_alone():
    body = f"## Gas\n\n{_RCT[:-1]} [S1].\n"
    blocks = [{"label": "official · trial", "url": "https://pubmed.ncbi.nlm.nih.gov/41885859/",
               "text": _TRIAL_PAGE}]
    g = _study_gen({})
    out, note = g._uncited_study_check(body, blocks, set())
    assert out == body and not note


# ── the price table's Product field reaches the article ──────────────────────────────────────────
# The compared name came from the BRAND column at both levels, and FU262 collapsed the draft's
# correct product name back onto it. That is where a column headed "Philips Avent & Nipples" —
# a category — came from on an article set to compare PRODUCTS.

import json as _json  # noqa: E402

from generators.blog_gen import _priced_competitor_names  # noqa: E402

_TABLE = {"price_table": _json.dumps({
    "philips-avent": {"name": "Philips Avent",
                      "rows": [{"product": "Natural Response 4oz", "kind": "exact",
                                "value": "$29.99"}]},
    "pigeon": {"name": "Pigeon",
               "rows": [{"product": "Glass Wide Neck 5.4oz", "kind": "exact", "value": "$34.99"},
                        {"product": "PPSU Wide Neck 5.4oz", "kind": "exact", "value": "$34.99"}]}}),
    "name": "Thyseed"}


def test_at_product_level_the_compared_name_is_the_row_s_product():
    assert _priced_competitor_names(_TABLE, "Thyseed", "product")[0] == \
        "Philips Avent Natural Response 4oz"


def test_at_brand_level_nothing_changes():
    """The toggle decides, as FU263 established."""
    assert _priced_competitor_names(_TABLE, "Thyseed", "brand") == ["Philips Avent", "Pigeon"]
    assert _priced_competitor_names(_TABLE, "Thyseed") == ["Philips Avent", "Pigeon"]


def test_a_brand_whose_rows_name_several_products_stays_a_brand():
    """Naming one of them here would put the column header and the price cell on two different
    products — which is the defect reported as "the row mixes two products"."""
    assert _priced_competitor_names(_TABLE, "Thyseed", "product")[1] == "Pigeon"


def test_a_product_already_in_the_brand_name_is_not_repeated():
    t = {"name": "X", "price_table": _json.dumps({"acme": {"name": "Acme Pro", "rows": [
        {"product": "Pro", "kind": "exact", "value": "$9"}]}})}
    assert _priced_competitor_names(t, "X", "product") == ["Acme Pro"]


def test_the_rename_pass_does_not_strip_a_product_name_it_could_not_supply():
    """At product level with a bare-brand option, folding "Pigeon Glass Wide Neck" down to "Pigeon"
    would destroy exactly the specificity the toggle asked for."""
    g = B.__new__(B)
    g._compare_level = "product"
    g._compare_bare = ["Pigeon"]
    body = "## Compare\n\nPigeon Glass Wide Neck costs $34.99.\n"
    out, note = g._compare_level_pass(body, ["Pigeon"])
    assert "Pigeon Glass Wide Neck" in out and not note


def test_the_rename_pass_still_folds_a_name_the_table_did_supply():
    g = B.__new__(B)
    g._compare_level = "product"
    g._compare_bare = []
    body = "## Compare\n\nPhilips Avent Natural Response Nipple costs $29.99.\n"
    out, _note = g._compare_level_pass(body, ["Philips Avent"])
    assert "Philips Avent costs $29.99" in out


# ── a transposed table gets its price cells written ──────────────────────────────────────────────

def test_price_cells_are_written_when_the_options_are_COLUMNS():
    """FU254 gave `_strip_price_columns` and `_resolve_table_punts` this; the price writer never
    got it. On a transposed table `r[0]` is a DIMENSION name, so the ledger matched no row and not
    one price cell was code-written — the model's price text shipped beside a model-written
    material cell, free to describe a different product."""
    g = B.__new__(B)
    g._evidence_blocks = []
    g._article_tools = ["Pigeon", "Dr. Brown's"]
    body = ("## Compare\n\n| Dimension | Pigeon | Dr. Brown's |\n|---|---|---|\n"
            "| Starting price | $99.00 | $99.00 |\n| Material | Glass | PPSU |\n")
    ledger = {"Pigeon": {"value": "$34.99", "kind": "exact", "basis": "2-pack",
                         "per_unit": "$17.50 each", "source": "yours"},
              "Dr. Brown's": {"value": "$8.99", "kind": "exact", "source": "yours"}}
    out, written = g._write_price_cells(body, ledger)
    assert written >= 2, out
    assert "$34.99" in out and "$8.99" in out and "$99.00" not in out
    assert "| Dimension | Pigeon | Dr. Brown's |" in out, "the article's own orientation is kept"


def test_the_ordinary_orientation_still_works():
    g = B.__new__(B)
    g._evidence_blocks = []
    g._article_tools = ["Pigeon", "Dr. Brown's"]
    body = ("## Compare\n\n| Brand | Starting price | Material |\n|---|---|---|\n"
            "| Pigeon | $99.00 | Glass |\n| Dr. Brown's | $99.00 | PPSU |\n")
    ledger = {"Pigeon": {"value": "$34.99", "kind": "exact", "source": "yours"},
              "Dr. Brown's": {"value": "$8.99", "kind": "exact", "source": "yours"}}
    out, written = g._write_price_cells(body, ledger)
    assert written == 2 and "$34.99" in out and "$99.00" not in out


# ── a sale price you typed ───────────────────────────────────────────────────────────────────────
# `_price_is_sale` has two call sites and both are page-fetch paths: a price the OPERATOR typed was
# never tested, on the reasoning that a typed figure has nothing to re-check — while the article
# printed "regular list prices" over the whole table. Both of the reported Pigeon prices were sale
# prices.

import app as _app  # noqa: E402
from generators.blog_gen import _format_price_value  # noqa: E402


def test_the_operators_own_wording_marks_the_row_as_a_sale():
    stored, dropped, _f = _app._clean_price_rows(
        [{"brand": "Pigeon", "product": "Glass Wide Neck 5.4oz", "kind": "exact",
          "value": "$34.99, on sale from $42.99"}],
        known_names=["Pigeon"], subject="Thyseed")
    assert not dropped
    row = stored["pigeon"]["rows"][0]
    assert row["value"] == "$34.99" and row["sale"] is True


def test_an_ordinary_typed_price_is_not_marked():
    stored, _d, _f = _app._clean_price_rows(
        [{"brand": "Pigeon", "kind": "exact", "value": "$34.99", "basis": "2-pack"}],
        known_names=["Pigeon"], subject="Thyseed")
    assert stored["pigeon"]["rows"][0]["sale"] is False


def test_the_cell_says_so_where_the_figure_is():
    assert _format_price_value({"value": "$34.99", "kind": "exact", "basis": "2-pack",
                                "per_unit": "$17.50 each", "sale": True}) == \
        "$34.99 (2-pack, $17.50 each) (sale price)"
    assert _format_price_value({"value": "$34.99", "kind": "exact", "basis": "2-pack",
                                "per_unit": "$17.50 each"}) == "$34.99 (2-pack, $17.50 each)"


def test_the_flag_survives_the_trip_into_the_ledger():
    e = B._price_row_entry({"brand": "Pigeon", "kind": "exact", "value": "$34.99", "sale": True},
                           "Pigeon")
    assert e["sale"] is True and "(sale price)" in _format_price_value(e)


def test_the_footnote_stops_claiming_every_figure_is_a_list_price():
    g = B.__new__(B)
    g._evidence_blocks = []
    g._article_tools = ["Pigeon"]
    body = ("## Compare\n\n| Brand | Starting price |\n|---|---|\n| Pigeon | $99.00 |\n"
            "| Dr. Brown's | $99.00 |\n")
    out, _w = g._write_price_cells(body, {"Pigeon": {"value": "$34.99", "kind": "exact",
                                                     "source": "yours", "sale": True}})
    assert "sale price" in out and "promotional, not list" in out


# ── a brand's own page outranks a marketplace listing ────────────────────────────────────────────
# Operator's decision. The reported case: Dr. Brown's "breast-like nipple shape … eases the
# transition from breast to bottle" was cited to an Amazon gift-set listing, while the brand's own
# product page makes exactly that claim. The rule already existed twice as PROMPT text, which is
# advice, not enforcement.

_OWN_PAGE = ("Dr. Brown's Anti-Colic Options+ Wide-Neck bottle. The breast-like nipple shape eases "
             "the transition from breast to bottle and back again for a feeding baby. " * 6)


def _retail_gen(blocks, pages=None):
    g = B.__new__(B)
    g._claim_pages = dict(pages or {})
    g._article_tools = ["Dr. Brown's"]
    return g


_RETAIL_BLOCKS = [
    {"label": "retail · Dr. Brown's Gift Set | Amazon",
     "url": "https://www.amazon.com/dp/B01N34NNJK", "text": "Gift set. " * 40},
    {"label": "Dr. Brown's", "url": "https://drbrownsbaby.com/products/options-wide-neck",
     "text": _OWN_PAGE},
]


def test_a_design_claim_on_a_listing_is_repointed_to_the_brands_own_page():
    body = ("## Nipples\n\nDr. Brown's uses a breast-like nipple shape that eases the transition "
            "from breast to bottle [S1].\n")
    g = _retail_gen(_RETAIL_BLOCKS)
    out, note = g._retail_claim_check(body, _RETAIL_BLOCKS, set(), [])
    assert "[S2]" in out and "[S1]" not in out
    assert "re-pointed" in note and "own page" in note


def test_a_design_claim_no_first_party_page_states_is_removed():
    blocks = [_RETAIL_BLOCKS[0],
              {"label": "Dr. Brown's", "url": "https://drbrownsbaby.com/x", "text": "Bottles. " * 40}]
    body = ("## Nipples\n\nDr. Brown's uses a breast-like nipple shape that eases the transition "
            "from breast to bottle [S1].\n")
    g = _retail_gen(blocks)
    out, note = g._retail_claim_check(body, blocks, set(), [])
    assert "breast-like nipple shape" not in out
    assert "no first-party page states" in note


def test_a_listing_is_still_a_source_for_a_PRICE():
    """It is the seller's own copy — for what something costs, that is exactly right."""
    body = "## Price\n\nDr. Brown's starts at $8.99 [S1].\n"
    g = _retail_gen(_RETAIL_BLOCKS)
    assert g._retail_claim_check(body, _RETAIL_BLOCKS, set(), []) == (body, "")


def test_a_claim_that_already_cites_a_first_party_page_is_untouched():
    body = ("## Nipples\n\nDr. Brown's uses a breast-like nipple shape that eases the transition "
            "from breast to bottle [S2].\n")
    g = _retail_gen(_RETAIL_BLOCKS)
    assert g._retail_claim_check(body, _RETAIL_BLOCKS, set(), []) == (body, "")


# ── mutation-test gaps: four fixes nothing was defending ─────────────────────────────────────────
# Found by reverting each fix and running the suite. Each of these passed either way before.

def test_no_rewriting_pass_still_splits_on_a_bare_full_stop():
    """`_claim_source_check` rewrites a line in place from its own split. Testing the shared helper
    does not defend its CALL SITE: reverting that one line left every test green while
    "…than Dr. Brown's." went back to being two sentences inside a pass that edits the body."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "generators", "blog_gen.py")).read()
    assert r're.split(r"((?<=[.!?])\s+)"' not in src, \
        "a capturing sentence split is a rewriting pass — use _split_sentences_keep"
    assert r'''re.split(r"(?<=[.!?])\s+", ''' not in src, \
        "use _split_sentences; the bare regex treats the dot in 'Dr. Brown's' as a sentence end"


def test_the_topic_drop_is_in_the_scorer_the_checks_actually_call():
    """`_specific_tokens` having the rule does not defend `_claim_tokens_on_page`, which is what
    `_org_position_check` and the cited-claim filter score with."""
    page = "Introducing the bottle to a breastfeeding baby is common. " * 8
    claim = ("breastfeeding be established before introducing a bottle, typically after the first "
             "few weeks, to avoid interfering with milk supply and latch")
    topic = ["baby", "bottle", "bottles", "breastfed", "breastfeeding", "feeding"]
    hit_no_topic, toks_no_topic = B._claim_tokens_on_page(claim, page)
    hit, toks = B._claim_tokens_on_page(claim, page, topic)
    assert "breastfeeding" in toks_no_topic and "breastfeeding" not in toks
    assert len(hit) / len(toks) < len(hit_no_topic) / len(toks_no_topic), \
        "dropping the topic's own words must LOWER a topic-only match, or it changes nothing"


def test_a_table_cell_goes_to_the_judge_even_at_a_perfect_word_match():
    """The reported Thyseed cell scores 0.75 and a correct cell scores 0.67 — a share ranks the
    wrong one higher, so a cell must never be filtered out. The earlier test used a page whose
    share was under the filter anyway, so it passed with the filter applied to cells too."""
    page = "Internal anti-colic venting system fitted throughout. " * 12
    hit, toks = B._claim_tokens_on_page("Internal anti-colic venting system", page)
    assert len(hit) / len(toks) == 1.0, "every word is on the page — a filter would skip this"
    blocks = [{"label": "Thyseed", "url": "https://t.example/p", "text": ""}]
    g = _judge_gen({"c1": "contradicted"}, {"c1": "Internal anti-colic venting system fitted"},
                   {"https://t.example/p": (page, "direct")})
    body = ("## Compare\n\n| Dimension | Thyseed |\n|---|---|\n"
            "| Anti-colic system | Internal anti-colic venting system [S1] |\n")
    out, note = g._cited_claim_check(body, blocks, set(), [])
    assert "Internal anti-colic venting system" not in out and note


def test_the_row_pick_warning_is_collected_for_the_operator():
    """It was a print and nothing else — assigned on one line, read on the next."""
    g = B.__new__(B)
    g._claim_pages = {}
    g._price_pick_notes = []
    brand = {"name": "Thyseed", "price_table": _json.dumps({"pigeon": {"name": "Pigeon", "rows": [
        {"product": "Glass Wide Neck 5.4oz", "kind": "exact", "value": "$34.99"},
        {"product": "PPSU Wide Neck 5.4oz", "kind": "exact", "value": "$39.99"}]}})}
    ledger, _missing, _dirty = g._ensure_price_ledger(
        brand, ["Pigeon"], {}, {}, ["tirzepatide", "dosing"], "tirzepatide dosing")
    assert ledger.get("Pigeon")
    assert g._price_pick_notes and "several priced rows" in g._price_pick_notes[0]
    assert "Glass Wide Neck 5.4oz" in g._price_pick_notes[0], "it must name the product it showed"


# ── a direct replacement, instead of only deleting ───────────────────────────────────────────────
# The operator's rule has three parts and only two were implemented: nothing is written from an
# unreachable page, it is not listed as a source, and "it can look for direct replacement". Nothing
# did the third — an unreachable source was only ever deleted, taking the claim with it even when
# another page we HAD read said the same thing.

# A SPECIFIC — a claim carrying a figure. The walled rule deliberately leaves an ordinary general
# statement alone (it keeps its prose and loses its marker), so a replacement only has a job where
# a removal would otherwise have happened.
_VENT_CLAIM = ("A vented base keeps the nipple filled with milk for 95% of a feed, which reduces "
               "swallowed air [S1].")
_VENT_PAGE = ("A vented base keeps the nipple filled with milk for 95% of a feed, reducing the "
              "air a baby swallows during feeding. " * 6)


def test_a_claim_on_an_unreachable_page_moves_to_one_we_did_read():
    g = B.__new__(B)
    g._claim_pages = {}
    blocks = [{"label": "official · Guidance", "url": "https://walled.example/p", "text": ""},
              {"label": "official · Paediatrics", "url": "https://ok.example/p", "text": _VENT_PAGE}]
    out, note = g._walled_source_check(f"## Venting\n\n{_VENT_CLAIM}\n", blocks, {1})
    assert "keeps the nipple filled with milk" in out, "the claim is kept, not deleted"
    assert "[S2]" in out and "[S1]" not in out
    assert "re-pointed" in note and "a source we did read" in note


def test_it_only_moves_to_a_page_that_actually_carries_the_claim():
    """Otherwise a replacement is just a different wrong citation."""
    g = B.__new__(B)
    g._claim_pages = {}
    blocks = [{"label": "official · Guidance", "url": "https://walled.example/p", "text": ""},
              {"label": "official · Unrelated", "url": "https://ok.example/p",
               "text": "Car seats must be rear facing until the age of two. " * 8}]
    out, note = g._walled_source_check(f"## Venting\n\n{_VENT_CLAIM}\n", blocks, {1})
    assert "keeps the nipple filled with milk" not in out
    assert "re-pointed" not in note


def test_it_never_moves_a_citation_to_another_unreadable_page():
    g = B.__new__(B)
    g._claim_pages = {}
    blocks = [{"label": "a", "url": "https://w1.example/p", "text": ""},
              {"label": "b", "url": "https://w2.example/p", "text": _VENT_PAGE}]
    out, note = g._walled_source_check(f"## Venting\n\n{_VENT_CLAIM}\n", blocks, {1, 2})
    assert "keeps the nipple filled with milk" not in out and "re-pointed" not in note
