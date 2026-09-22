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
