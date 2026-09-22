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
