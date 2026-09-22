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


# ── FU260 — the publisher's own brand is in its own price table ──────────────────────────────────
# `_save_price_table` popped the subject's rows OUT of price_table and routed them to key_facts, so
# everything built on that column skipped the publisher's own brand: it could not have a marked
# range, and its rows disappeared from the price-table UI after saving because the UI reads that
# same column. They are now kept in BOTH — canonical pricing AND the table.

import json as _json  # noqa: E402

from generators.blog_gen import (  # noqa: E402
    BlogGenerator, _priced_competitor_names, _format_price_value)

_SUBJECT_SPAN = [
    {"brand": "Thyseed", "product": "5 oz PPSU Anti-colic Bottle", "kind": "span", "value": "28.99"},
    {"brand": "Thyseed", "product": "10 oz PPSU Transition Bottle", "kind": "span", "value": "32.99"},
    {"brand": "Pigeon", "product": "5.4 oz 2-pack", "kind": "exact", "value": "$42.99"},
]


# These go through `_save_price_table`, NOT `_clean_price_rows`: the subject was popped out AFTER
# cleaning, so a test of the cleaner passes whether the pop is there or not.
import os as _os  # noqa: E402
import tempfile as _tempfile  # noqa: E402

from db import Database  # noqa: E402


def _seeded_db():
    d = _tempfile.mkdtemp()
    db = Database(_os.path.join(d, "t.db"))
    db.initialize()
    sid = db.create_subreddit("s", "d")
    db.conn.execute("INSERT INTO brands (subreddit_id,name,context) VALUES (?,?,?)",
                    (sid, "Thyseed", "c"))
    db.conn.commit()
    return db


def _saved(rows):
    db = _seeded_db()
    app._save_price_table(db, db.get_brand(1), rows)
    brand = db.get_brand(1)
    db.close()
    return brand


def test_the_subjects_rows_stay_in_the_price_table():
    brand = _saved(_SUBJECT_SPAN)
    pt = _json.loads(brand["price_table"])
    assert "thyseed" in pt and "pigeon" in pt
    assert [r["value"] for r in pt["thyseed"]["rows"]] == ["$28.99", "$32.99"]


def test_the_subjects_rows_still_reach_canonical_pricing():
    """Both, not either: the canonical store is what a per-product article prices from."""
    brand = _saved(_SUBJECT_SPAN)
    vals = {i["product"]: i["value"] for i in
            (_json.loads(brand["key_facts"]).get("pricing") or {}).get("items", [])}
    assert vals["5 oz PPSU Anti-colic Bottle"] == "$28.99 (5 oz PPSU Anti-colic Bottle)"
    assert vals["10 oz PPSU Transition Bottle"] == "$32.99 (10 oz PPSU Transition Bottle)"


def test_the_subject_is_still_not_a_competitor():
    """What the pop was really protecting. The competitor field excludes it by NAME, so keeping its
    rows in the table cannot crowd the comparison."""
    assert _priced_competitor_names(_saved(_SUBJECT_SPAN)) == ["Pigeon"]


def test_the_publisher_can_now_have_a_marked_RANGE():
    """The whole point: marking the cheapest and dearest thing the PUBLISHER sells did nothing at
    all, because the rows never reached the column the range is built from."""
    blocks = BlogGenerator._price_span_block(_saved(_SUBJECT_SPAN))
    assert len(blocks) == 1, blocks
    assert ("Thyseed: From $28.99 (5 oz PPSU Anti-colic Bottle) to "
            "$32.99 (10 oz PPSU Transition Bottle)") in blocks[0]["text"]


def test_a_brand_whose_rows_all_die_loses_its_stale_entry():
    """Sent with nothing surviving means the entry goes — the subject included, now that it is an
    ordinary entry in the column."""
    db = _seeded_db()
    app._save_price_table(db, db.get_brand(1), _SUBJECT_SPAN)
    assert "thyseed" in _json.loads(db.get_brand(1)["price_table"])
    app._save_price_table(db, db.get_brand(1),
                          [{"brand": "Thyseed", "kind": "exact", "value": "cheap"}])
    assert "thyseed" not in _json.loads(db.get_brand(1)["price_table"])
    db.close()


# ── FU261 — the price you entered, found by the name you typed ───────────────────────────────────
# `tools` carries PRODUCT-QUALIFIED competitor names ("<Brand> <Product Line>") while an operator
# types the BRAND. The ledger looked its rows up by exact slug, so "Dr. Brown's Anti-Colic Options+"
# never found the "Dr. Brown's" row — and the run PAUSED asking for a price already in the table.

_TABLE_MAP = {
    "dr-brown-s": {"name": "Dr. Brown's",
                   "rows": [{"brand": "Dr. Brown's", "kind": "exact", "value": "$9.99"}]},
    "philips-avent": {"name": "Philips Avent",
                      "rows": [{"brand": "Philips Avent", "kind": "exact", "value": "$12.95"}]},
}


def _entry(m, tool):
    from generators.blog_gen import _kf_slug
    return BlogGenerator._operator_entry_for(m, tool, _kf_slug(tool))


def test_a_product_qualified_tool_finds_the_brand_row_you_typed():
    assert [r["value"] for r in _entry(_TABLE_MAP, "Dr. Brown's Anti-Colic Options+")["rows"]] \
        == ["$9.99"]
    assert [r["value"] for r in _entry(_TABLE_MAP, "Philips Avent Natural Response")["rows"]] \
        == ["$12.95"]


def test_an_exact_name_still_matches_first():
    assert [r["value"] for r in _entry(_TABLE_MAP, "Dr. Brown's")["rows"]] == ["$9.99"]


def test_a_brand_you_never_priced_is_still_not_found():
    """Otherwise the pause that asks for a real gap would stop firing."""
    assert _entry(_TABLE_MAP, "Comotomo") == {}
    assert _entry({}, "Anything") == {}


def test_the_most_specific_stored_name_wins():
    m = {"a": {"name": "Dr. Brown's", "rows": [{"value": "$9.99"}]},
         "b": {"name": "Dr. Brown's Options+", "rows": [{"value": "$14.99"}]}}
    assert [r["value"] for r in _entry(m, "Dr. Brown's Options+ 4oz")["rows"]] == ["$14.99"]


def test_a_short_name_does_not_swallow_a_different_brand():
    """The whole-word rule the comparison field already uses: "Ro" never matches "Rory"."""
    m = {"ro": {"name": "Ro", "rows": [{"value": "$349"}]}}
    assert _entry(m, "Rory") == {}
    assert _entry(m, "Rocket Health") == {}
    assert [r["value"] for r in _entry(m, "Ro")["rows"]] == ["$349"]


def test_the_same_resolver_serves_the_price_LINKS():
    """The links map had the identical exact-slug lookup, so a page you pasted was ignored too."""
    links = {"dr-brown-s": {"name": "Dr. Brown's", "urls": ["https://drbrowns.example/p"]}}
    assert _entry(links, "Dr. Brown's Anti-Colic Options+")["urls"] == \
        ["https://drbrowns.example/p"]
