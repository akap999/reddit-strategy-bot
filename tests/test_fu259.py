"""FU259 — a price row you entered that validated to nothing.

Measured on a real run: seven rows sent with a Generate, seven dropped, zero stored. The only trace
was a count in a server log — `[price-table] saved 0 row(s) ... 7 dropped` — and the article then
priced everything from automatic sourcing while the operator was told nothing at all. A table that
validated to NOTHING looked exactly like a table that was never filled in.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app  # noqa: E402

KNOWN = ["Pigeon", "Philips Avent"]


def _clean(rows, **kw):
    return app._clean_price_rows(rows, known_names=KNOWN, subject="Thyseed", **kw)


def test_a_price_you_typed_needs_no_currency_symbol():
    """The column is headed "Price" and the operator is the authority. Rejecting "28.99" dropped the
    row silently, which is how a whole table came to validate to nothing."""
    stored, dropped, _f = _clean([
        {"brand": "Thyseed", "product": "5 oz", "kind": "span", "value": "28.99"},
        {"brand": "Thyseed", "product": "10 oz", "kind": "span", "value": "32.99"}])
    assert not dropped, dropped
    assert [r["value"] for r in stored["thyseed"]["rows"]] == ["$28.99", "$32.99"]


def test_the_currency_comes_from_the_rows_beside_it():
    """A bare number takes the symbol the operator is already using, not a hardcoded dollar."""
    stored, dropped, _f = _clean([
        {"brand": "Pigeon", "kind": "exact", "value": "£21.50"},
        {"brand": "Pigeon", "kind": "exact", "value": "19.99"}])
    assert not dropped
    assert [r["value"] for r in stored["pigeon"]["rows"]] == ["£21.50", "£19.99"]


def test_a_PARSED_figure_must_still_be_verbatim_in_the_paste():
    """The leniency is for what the operator TYPED. A figure a model produced may not gain a symbol
    it never read — that is the FU214 rule this must not weaken."""
    _stored, dropped, _f = _clean([{"brand": "Pigeon", "kind": "exact", "value": "21.50"}],
                                  verbatim_text="Pigeon bottles are great")
    assert dropped and "not a price figure" in dropped[0]["why"]


def test_nonsense_is_still_rejected():
    _stored, dropped, _f = _clean([{"brand": "Pigeon", "kind": "exact", "value": "cheap"}])
    assert dropped and "not a price figure" in dropped[0]["why"]


def test_a_row_with_no_brand_is_still_rejected_and_says_so():
    _stored, dropped, _f = _clean([{"brand": "", "product": "9 oz", "kind": "exact", "value": "$12.95"}])
    assert dropped and dropped[0]["why"] == "no brand name"


def test_the_drop_reasons_reach_the_operator_verbatim():
    """The wording the generate path builds, so a change to it is visible here rather than only in
    a server log."""
    _stored, dropped, _f = _clean([
        {"brand": "Thyseed", "kind": "exact", "value": "cheap"},
        {"brand": "", "kind": "exact", "value": "$1.00"}])
    assert len(dropped) == 2
    why = "; ".join(f"“{str(d.get('raw') or d.get('brand') or '')[:40]}” — "
                    f"{d.get('why') or 'rejected'}" for d in dropped[:4])
    note = (f"price table: {len(dropped)} row(s) you entered were NOT saved ({why}). Every price "
            f"needs its currency symbol ($28.99, not 28.99) and a brand name. Any price on the page "
            f"came from automatic sourcing instead")
    assert "2 row(s) you entered were NOT saved" in note
    assert "not a price figure" in note and "no brand name" in note
