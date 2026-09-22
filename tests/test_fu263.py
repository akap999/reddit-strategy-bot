"""FU263 — a pack price read as a bottle price, and the claims built on it.

The operator entered a competitor's range and typed "2-pack" into the basis of the low end. The
research pass had independently priced the same figure as "$42.99 (2 Pack, 5.4 Oz) … $21.50 each",
and the cited page's own URL says `…-2-packs-…`. The system held the pack size twice over, and
FU255's span renderer dropped it: the label took the product OR the basis, never both, and zeroed
the per-unit price. Three statements in the finished article rested on the error.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.blog_gen import BlogGenerator, _format_price_value  # noqa: E402


def _gen(tools=()):
    g = BlogGenerator.__new__(BlogGenerator)
    g._table_punt_note = ""
    g._article_tools = list(tools)
    return g


_PACK_RANGE = [
    {"brand": "Acme", "product": "5oz Coated Glass Bottle", "kind": "span",
     "value": "$41.99", "basis": "2-pack"},
    {"brand": "Acme", "product": "8oz Coated Glass Bottle", "kind": "span",
     "value": "$42.99", "basis": ""},
]


def test_the_pack_the_operator_typed_survives_into_the_cell():
    e, _amb = _gen()._price_row_for(_PACK_RANGE, "Acme", ["bottle"])
    cell = _format_price_value(e)
    assert "2-pack" in cell, "the operator typed the pack size and it was deleted"
    assert "5oz Coated Glass Bottle" in cell, "the product must not be traded for the basis"


def test_the_per_item_price_rides_with_it():
    """The only figure in the cell comparable with a rival's single-item price."""
    cell = _format_price_value(_gen()._price_row_for(_PACK_RANGE, "Acme", ["bottle"])[0])
    assert "$21.00 each" in cell, cell


def test_an_end_with_no_pack_basis_is_unchanged():
    cell = _format_price_value(_gen()._price_row_for(_PACK_RANGE, "Acme", ["bottle"])[0])
    assert "to $42.99 (8oz Coated Glass Bottle)" in cell, cell


def test_a_range_with_no_basis_at_all_reads_as_before():
    rows = [{"brand": "B", "product": "5 oz Bottle", "kind": "span", "value": "$28.99"},
            {"brand": "B", "product": "10 oz Bottle", "kind": "span", "value": "$32.99"}]
    assert _format_price_value(_gen()._price_row_for(rows, "B", ["bottle"])[0]) == \
        "From $28.99 (5 oz Bottle) to $32.99 (10 oz Bottle)"


def test_a_basis_with_no_product_still_labels_the_end():
    rows = [{"brand": "C", "kind": "span", "value": "$10.00", "basis": "single"},
            {"brand": "C", "kind": "span", "value": "$18.00", "basis": "3-pack"}]
    cell = _format_price_value(_gen()._price_row_for(rows, "C", ["x"])[0])
    assert "(single)" in cell and "3-pack" in cell and "$6.00 each" in cell, cell


# ── Step 2 — a price ranking the page's own ledger disproves ─────────────────────────────────────
# "Pigeon's entry price is the highest of the four brands compared here, making it a premium-only
# option." On comparable units it was the second CHEAPEST — the claim rested on a 2-pack figure read
# as one bottle's. Every number needed to settle it is in the ledger, so nothing here asks a model.

_LEDGER = {
    "Acme": {"value": "$28.99", "span_ends": [{"value": "$28.99", "per_unit": ""},
                                              {"value": "$32.99", "per_unit": ""}]},
    "Budget Co": {"value": "$8.99", "per_unit": ""},
    "Mid Co": {"value": "$12.95", "per_unit": ""},
    "Packco": {"value": "$41.99", "span_ends": [{"value": "$41.99", "per_unit": "$21.00 each"},
                                                {"value": "$42.99", "per_unit": ""}]},
}


def _ranked(body, publisher="Acme"):
    g = _gen()
    g._price_ledger = dict(_LEDGER)
    return g._price_rank_check(body, {"name": publisher})


def _sect(text):
    return f"## H\n\n{text}\n"


def test_a_ranking_the_ledger_contradicts_is_removed():
    out, note = _ranked(_sect("Packco's entry price is the highest of the four brands compared "
                              "here, making it a premium-only option."))
    assert "Packco's entry price" not in out
    assert "dearest on comparable units is Acme" in note, note


def test_a_pack_price_is_not_compared_with_a_single():
    """The whole defect: $41.99 for a 2-pack is $21.00 an item, which is below two rivals."""
    g = _gen()
    assert g._comparable_price(_LEDGER["Packco"]) == 21.0
    assert g._comparable_price(_LEDGER["Acme"]) == 28.99


def test_a_true_ranking_about_a_competitor_stays():
    out, _n = _ranked(_sect("Choose Budget Co if price is the constraint: it is the cheapest "
                            "of the four."))
    assert "cheapest of the four" in out


def test_the_publisher_is_never_volunteered_as_the_dearest():
    """Removed even though the arithmetic is right. This tool does not write that sentence about
    its own client — the FU243 framing doctrine, applied to price."""
    out, note = _ranked(_sect("Acme is the most expensive bottle in this comparison."))
    assert "most expensive" not in out
    assert "makes Acme the dearest" in note, note


def test_premium_POSITIONING_is_not_a_ranking():
    """What the operator asked for: sitting in the premium range is not losing on price."""
    body = _sect("Acme sits in the premium range, with the widest size span of the four.")
    out, note = _ranked(body)
    assert out == body and note == ""


def test_a_superlative_about_something_OTHER_than_price_is_untouched():
    body = _sect("Packco has the highest number of nipple sizes of any brand here.")
    out, note = _ranked(body)
    assert out == body and note == ""


def test_a_sentence_naming_no_option_or_two_is_never_guessed_at():
    for t in ("The cheapest option here still costs more than a supermarket bottle.",
              "Packco and Budget Co are the cheapest of the four."):
        body = _sect(t)
        out, _n = _ranked(body)
        assert out == body, t


def test_it_is_inert_without_a_ledger():
    g = _gen()
    g._price_ledger = {}
    body = _sect("Packco is the most expensive of the four.")
    assert g._price_rank_check(body, {"name": "Acme"}) == (body, "")


def test_an_options_OWN_cheapest_tier_is_not_a_ranking_of_the_field():
    """"its lowest tier starts at $99" describes that option's own ladder. Measured on the stored
    corpus this shape is most of what the sentence patterns pick up, and none of it ranks anything.
    The possessive has to be ADJACENT: in "<Name>'s entry price is the highest of the four" three
    words separate them, and that one IS a ranking."""
    for t in ("Packco does not list a free plan; its lowest tier starts at $99 per month.",
              "Packco's lowest published tier is $99 per month, billed annually.",
              "Mid Co's cheapest plan costs $12.95 per month."):
        body = _sect(t)
        out, note = _ranked(body)
        assert out == body and note == "", t
    # …and the adjacency boundary still lets a real field ranking through
    out, _n = _ranked(_sect("Packco's entry price is the highest of the four brands compared here."))
    assert "entry price is the highest" not in out


# ── Step 3 — a position attributed to an organisation, against the page cited for it ─────────────
# The reported article told readers a paediatric body's guidance covered burping frequency, paced
# feeding, nipple flow and when to introduce a bottle. Its cited page covers none of them. Nothing
# could see it: the figure check skips a sentence with no number, and the page was never even
# FETCHED, because only a figure triggered a fetch.
#
# The fixture is that page as our own fetcher returned it, so these assertions rest on the real
# document rather than a hand-written stand-in.

_PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fixtures", "fu263_authority_page.txt"), encoding="utf-8").read()


def _org(body, page=None):
    g = _gen()
    g._claim_pages = {}
    blocks = [{"label": "official · Guidance", "url": "https://authority.example/p",
               "text": page if page is not None else _PAGE}]
    return g._org_position_check(body, blocks, set())


def test_a_position_the_cited_page_never_states_is_reported():
    note = _org("## H\n\nThe AAP's guidance on bottle feeding notes that newborns should be burped "
                "frequently and that paced feeding (holding the bottle more horizontally, allowing "
                "the baby to control intake) reduces swallowed air. [S1]\n")
    assert note, "the page contains none of 'burped', 'paced', 'frequently'"
    assert "'burped'" in note and "'paced'" in note, note
    assert "AAP" in note


def test_a_position_the_page_DOES_state_is_left_alone():
    assert _org("## H\n\nThe AAP advises that the bottle should be held so that milk covers the "
                "nipple and the baby does not swallow air. [S1]\n") == ""


def test_a_trailing_citation_still_belongs_to_its_sentence():
    """"…swallowed air. [S1]" — the splitter makes the marker its own sentence, so the claim would
    read as uncited and never be judged at all."""
    with_marker = ("## H\n\nThe AAP recommends burping newborns frequently during and after feeds "
                   "and using paced bottle feeding to reduce air ingestion. [S1]\n")
    assert _org(with_marker), "a trailing marker was not carried back onto its claim"


def test_an_uncited_position_is_not_judged():
    """No citation means no page to judge it against — a different defect, not this one."""
    assert _org("## H\n\nThe AAP recommends burping newborns frequently during feeds.\n") == ""


def test_an_unreadable_page_means_UNKNOWN_not_unsupported():
    assert _org("## H\n\nThe AAP recommends burping newborns frequently during feeds. [S1]\n",
                page="too short to judge") == ""


def test_two_signals_must_agree_or_nothing_is_reported():
    """Either alone is noisy. The words that DO match are the topic's — a page about the subject
    always has those — so a low share alone cannot separate a supported claim from an invented one.

    Here the share is 0.12, far below the threshold, but the claim's MOST DISTINCTIVE word is on
    the page. One signal says unsupported, the other says the page discusses it. Nothing is
    reported, and that conservatism is the point of a warning-only check whose false-positive rate
    cannot yet be measured across the corpus."""
    g = _gen()
    pred = "pediatrician visits should be scheduled quarterly alongside routine dental screening"
    hit, toks = g._claim_tokens_on_page(pred, _PAGE)
    assert len(hit) / len(toks) <= g._ORG_SAYS_MIN_SHARE, (hit, toks)
    assert max(toks, key=len) in hit, "this case exists to make the signals DISAGREE"
    assert _org("## H\n\nThe Example Health Body advises that %s. [S1]\n" % pred) == "", \
        "one signal alone must not be enough to report"


def test_the_probe_gate_fetches_a_page_an_org_claim_cites():
    """Without this the page was never read at all, so the claim could not be checked even in
    principle. A block with no text and no URL is recorded as unreadable ONLY if it was probed."""
    g = _gen()
    g._claim_pages = {}
    blocks = [{"label": "official · Guidance", "url": "", "text": ""}]
    body = ("## H\n\nThe AAP recommends burping newborns frequently during and after feeds and "
            "using paced bottle feeding to reduce air ingestion. [S1]\n")
    assert g._probe_cited_sources(body, blocks) == {1}, \
        "an organisation's position is a specific — its page has to be read"
    # …and a sentence that is neither a figure nor an attribution is still not probed
    assert g._probe_cited_sources("## H\n\nBottles come in several shapes. [S1]\n", blocks) == set()


def test_a_stem_is_matched_not_a_bare_prefix():
    """"control"[:4] matches "contain", which credits a page with words it does not have."""
    g = _gen()
    hit, _t = g._claim_tokens_on_page("control", "this page happens to contain many things")
    assert hit == []
    hit, _t = g._claim_tokens_on_page("burping", "remember to burp the baby after feeds")
    assert hit == ["burping"], "a stem must still find its own word"


def test_a_marker_only_unit_is_carried_back_not_dropped():
    """Directly: "…air. [S1]" splits into two units, and without the carry-back the claim unit has
    no citation, so nothing is judged."""
    g = _gen()
    line = ("The AAP recommends burping newborns frequently during and after feeds and using "
            "paced bottle feeding to reduce air ingestion. [S1]")
    raw = g._prose_sentences(line)
    assert raw[-1].strip() == "[S1]", "precondition: the splitter separates the marker"
    assert len(raw) == 2


# ── Step 5b (part 1) — the name YOU curated, on a blog with no pricing ───────────────────────────
# FU262 made the priced names win. The curated path had the identical bug and is the one EVERY blog
# without a price table travels: a competitor curated as a brand entered the field as whatever
# product line the draft happened to name.

import json as _json  # noqa: E402

from tests.test_fu214_price_table import BRAND as _BRAND, _sourcing  # noqa: E402


def test_the_curated_name_wins_over_the_drafts_product_line():
    brand = dict(_BRAND, manual_competitors=_json.dumps(["Dr. Brown's", "Philips Avent"]))
    _g, s = _sourcing(brand, ["Dr. Brown's Anti-Colic Options+",
                              "Philips Avent Natural Response", "Comotomo"],
                      include_pricing=False)
    assert "Dr. Brown's" in s["tools"] and "Philips Avent" in s["tools"], s["tools"]
    assert "Dr. Brown's Anti-Colic Options+" not in s["tools"], \
        "the draft's product line replaced the name the operator curated"
    assert "Philips Avent Natural Response" not in s["tools"]


def test_the_curated_name_keeps_its_place_in_the_order():
    """Replaced IN PLACE — the finalize loop and the [S#] numbering both walk this list in order."""
    brand = dict(_BRAND, manual_competitors=_json.dumps(["Philips Avent"]))
    _g, s = _sourcing(brand, ["Comotomo", "Philips Avent Natural Response", "Pigeon"],
                      include_pricing=False)
    assert s["tools"].index("Philips Avent") == 1, s["tools"]


def test_a_curated_name_the_draft_never_mentioned_is_still_added():
    brand = dict(_BRAND, manual_competitors=_json.dumps(["Comotomo"]))
    _g, s = _sourcing(brand, ["Pigeon"], include_pricing=False)
    assert "Comotomo" in s["tools"]


def test_an_exact_curated_name_is_untouched():
    brand = dict(_BRAND, manual_competitors=_json.dumps(["Pigeon"]))
    _g, s = _sourcing(brand, ["Pigeon", "Comotomo"], include_pricing=False)
    assert "Pigeon" in s["tools"]


# ── Step 5b (part 2) — the comparison LEVEL, set once by the operator ────────────────────────────
# The article compared at two levels at once: rows said "<Brand>", prose said "<Brand> <Product
# Line>", sources were that product's pages. The field is fixed before sourcing, but the DRAFT was
# written before that and had already named a product line, so fixing the field renamed the row and
# left the paragraphs behind.

_TWO_LEVEL_BODY = (
    "## Dr. Brown's Anti-Colic Options+\n\n"
    "Dr. Brown's Anti-Colic Options+ is widely used. The PPSU variant is shatterproof. [S13]\n\n"
    "| Brand | Price |\n| --- | --- |\n| Dr. Brown's | $8.99 |\n\n"
    "Choose Philips Avent Natural Response if budget is the constraint.\n\n"
    "## Sources\n\n"
    "- [S7] retail · Dr. Brown's Anti-Colic Options+ Narrow Glass Baby Bottle - <https://a.example>\n")
_TOOLS = ["Dr. Brown's", "Philips Avent", "Pigeon"]


def _level(body, level="brand", tools=None):
    g = _gen()
    g._compare_level = level
    return g._compare_level_pass(body, tools if tools is not None else _TOOLS)


def test_the_prose_and_headings_use_the_names_you_supplied():
    out, note = _level(_TWO_LEVEL_BODY)
    assert "## Dr. Brown's\n" in out, "the heading kept the draft's product line"
    assert "Dr. Brown's is widely used" in out
    assert "Choose Philips Avent if" in out
    assert "compare-level (brand)" in note


def test_a_source_TITLE_is_that_pages_own_words():
    """Renaming a citation's title would misquote the page it names."""
    out, _n = _level(_TWO_LEVEL_BODY)
    assert "Dr. Brown's Anti-Colic Options+ Narrow Glass Baby Bottle" in out


def test_a_fact_that_belongs_to_a_PRODUCT_still_names_it():
    """Brand level does not ban product names. Stating a variant's spec of the whole brand would be
    the same class of error as reading a multi-pack price as one item's."""
    out, _n = _level(_TWO_LEVEL_BODY)
    assert "The PPSU variant is shatterproof" in out


def test_it_is_inert_until_the_operator_sets_a_level():
    assert _level(_TWO_LEVEL_BODY, level="") == (_TWO_LEVEL_BODY, "")


def test_a_rename_never_runs_past_the_end_of_its_paragraph():
    """`\\s+` would let the match cross a blank line and swallow the next paragraph's first words."""
    body = "## H\n\nPigeon Wide Neck is good.\n\nPigeon Glass is also good.\n"
    out, _n = _level(body, tools=["Pigeon"])
    assert out.count("Pigeon") == 2 and "is good" in out and "is also good" in out, out


def test_the_toggle_is_authoritative_not_a_guess():
    """Nothing inspects a name to decide what kind it is — the toggle already did. A name that
    LOOKS like a product, supplied under Brand, is used exactly as supplied."""
    body = "## H\n\nDr. Brown's Options+ Narrow Glass is popular.\n"
    out, _n = _level(body, tools=["Dr. Brown's Options+"])
    assert "Dr. Brown's Options+ is popular" in out, out
