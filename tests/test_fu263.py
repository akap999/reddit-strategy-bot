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
