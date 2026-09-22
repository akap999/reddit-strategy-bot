"""FU265 — named price sets, and a blog that keeps the prices it was generated with.

Reuse already worked: `price_table` is a brands column and every new blog prefilled from it. What
did not exist was a SECOND set. `_save_price_table` wrote the one table there was, so entering
different rows for blog B overwrote it — and `_ensure_price_ledger` reads the brand's table as it
stands NOW, so regenerating blog A afterwards silently adopted B's prices.
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as _app  # noqa: E402
from db import Database  # noqa: E402
from generators.blog_gen import (  # noqa: E402
    PRICE_SET_DEFAULT, price_sets, price_set_rows, price_sets_blob, _priced_competitor_names)

FLAT = {"pigeon": {"name": "Pigeon", "rows": [{"kind": "exact", "value": "$12.99"}]}}


# ── the reshape is inert ─────────────────────────────────────────────────────────────────────────
# Every brand in production holds the OLD flat shape. Migrate-on-read means none of them has to be
# converted, and a brand nobody touches answers every question exactly as it did before.

def test_the_old_flat_table_reads_as_the_Default_set():
    b = {"price_table": json.dumps(FLAT)}
    assert list(price_sets(b)) == [PRICE_SET_DEFAULT]
    assert price_set_rows(b) == FLAT
    assert price_set_rows(b, PRICE_SET_DEFAULT) == FLAT


def test_a_flat_table_still_names_its_priced_competitors():
    """The reader every comparison goes through must not notice the reshape."""
    b = {"name": "Acme", "price_table": json.dumps(FLAT)}
    assert _priced_competitor_names(b) == ["Pigeon"]


def test_an_empty_or_broken_table_yields_nothing_rather_than_raising():
    for b in ({}, {"price_table": ""}, {"price_table": "not json"}, {"price_table": "[]"}):
        assert price_sets(b) == {} and price_set_rows(b) == {}


# ── named sets ───────────────────────────────────────────────────────────────────────────────────

def _db():
    db = Database(os.path.join(tempfile.mkdtemp(), "t.db"))
    db.initialize()
    sid = db.create_subreddit("s", "d")
    db.conn.execute("INSERT INTO brands (subreddit_id,name,context) VALUES (?,?,?)",
                    (sid, "Acme", "c"))
    db.conn.commit()
    return db


def test_saving_one_set_leaves_the_others_alone():
    """The hazard, stated directly: a second set must not overwrite the first."""
    db = _db()
    _app._save_price_table(db, db.get_brand(1),
                           [{"brand": "Pigeon", "kind": "exact", "value": "$12.99"}])
    _app._save_price_table(db, db.get_brand(1),
                           [{"brand": "Pigeon", "kind": "exact", "value": "$99.00"}],
                           set_name="Holiday")
    b = db.get_brand(1)
    assert sorted(price_sets(b)) == ["Default", "Holiday"]
    assert price_set_rows(b)["pigeon"]["rows"][0]["value"] == "$12.99", "Default was overwritten"
    assert price_set_rows(b, "Holiday")["pigeon"]["rows"][0]["value"] == "$99.00"
    db.close()


def test_a_blog_naming_a_set_that_no_longer_exists_still_generates():
    b = {"price_table": price_sets_blob({PRICE_SET_DEFAULT: FLAT})}
    assert price_set_rows(b, "Renamed") == FLAT, "it must fall back, not return nothing"


def test_the_brand_carries_which_set_this_run_uses():
    """`_brand_price_table` is THE accessor; the per-run choice is resolved there and nowhere else."""
    from generators.blog_gen import BlogGenerator
    other = {"pigeon": {"name": "Pigeon", "rows": [{"kind": "exact", "value": "$99.00"}]}}
    b = {"price_table": price_sets_blob({PRICE_SET_DEFAULT: FLAT, "Holiday": other})}
    assert BlogGenerator._brand_price_table(b)["pigeon"]["rows"][0]["value"] == "$12.99"
    assert BlogGenerator._brand_price_table(
        dict(b, _price_set="Holiday"))["pigeon"]["rows"][0]["value"] == "$99.00"


# ── the pin ──────────────────────────────────────────────────────────────────────────────────────

def test_a_set_flattens_to_the_row_list_the_operator_edits():
    b = {"price_table": price_sets_blob({PRICE_SET_DEFAULT: FLAT})}
    rows = _app._blog_price_rows_from_sets(b)
    assert rows == [{"kind": "exact", "value": "$12.99", "brand": "Pigeon"}]


def test_the_pin_columns_default_blank_so_no_stored_blog_changes():
    db = _db()
    cols = {r[1] for r in db.conn.execute("PRAGMA table_info(blogs)")}
    assert {"price_set", "price_rows"} <= cols
    bid = db.save_blog(1, "seed", title="t", body_markdown="body")
    row = db.get_blog(bid)
    assert not (row.get("price_set") or "") and not (row.get("price_rows") or "")
    db.close()


def test_a_blog_keeps_the_prices_it_was_generated_with():
    """The reason for the round, end to end at the resolution point.

    Blog A was generated with Pigeon at $12.99. Rows for blog B then replace the brand's Default
    set with $99.00. Regenerating A must still price Pigeon at $12.99 — before the pin, A silently
    adopted B's figures and a published article's prices moved.
    """
    db = _db()
    _app._save_price_table(db, db.get_brand(1),
                           [{"brand": "Pigeon", "kind": "exact", "value": "$12.99"}])
    a_rows = _app._blog_price_rows_from_sets(db.get_brand(1))
    blog_a = {"price_rows": json.dumps(a_rows), "price_set": PRICE_SET_DEFAULT}

    _app._save_price_table(db, db.get_brand(1),      # what blog B entered, into the same set
                           [{"brand": "Pigeon", "kind": "exact", "value": "$99.00"}])
    assert price_set_rows(db.get_brand(1))["pigeon"]["rows"][0]["value"] == "$99.00"

    from generators.blog_gen import BlogGenerator
    brand = _app._brand_for_regenerate(db.get_brand(1), blog_a)
    assert BlogGenerator._brand_price_table(brand)["pigeon"]["rows"][0]["value"] == "$12.99"
    db.close()


def test_a_blog_with_no_pin_falls_back_to_the_set_it_named():
    """All 226 stored blogs are in this state: the pin takes effect from the next generation on."""
    other = {"pigeon": {"name": "Pigeon", "rows": [{"kind": "exact", "value": "$99.00"}]}}
    b = {"name": "Acme", "price_table": price_sets_blob(
        {PRICE_SET_DEFAULT: FLAT, "Holiday": other})}
    from generators.blog_gen import BlogGenerator
    assert BlogGenerator._brand_price_table(
        _app._brand_for_regenerate(b, {}))["pigeon"]["rows"][0]["value"] == "$12.99"
    assert BlogGenerator._brand_price_table(
        _app._brand_for_regenerate(b, {"price_set": "Holiday"}))["pigeon"]["rows"][0]["value"] \
        == "$99.00"


def test_a_pin_that_validates_to_nothing_falls_back_instead_of_blanking():
    """FU259's lesson: a table that validated to nothing looked exactly like no table at all. Here
    it must not leave a regenerate with NO prices, which would silently drop every figure."""
    b = {"name": "Acme", "price_table": price_sets_blob({PRICE_SET_DEFAULT: FLAT})}
    from generators.blog_gen import BlogGenerator
    brand = _app._brand_for_regenerate(
        b, {"price_rows": json.dumps([{"brand": "Pigeon", "kind": "exact", "value": "cheap"}])})
    assert BlogGenerator._brand_price_table(brand)["pigeon"]["rows"][0]["value"] == "$12.99"


def test_a_corrupt_pin_does_not_break_the_regenerate():
    b = {"name": "Acme", "price_table": price_sets_blob({PRICE_SET_DEFAULT: FLAT})}
    for bad in ("not json", "[]", "null", ""):
        from generators.blog_gen import BlogGenerator
        assert BlogGenerator._brand_price_table(
            _app._brand_for_regenerate(b, {"price_rows": bad}))["pigeon"]["rows"][0]["value"] \
            == "$12.99"


# ── the save endpoint ────────────────────────────────────────────────────────────────────────────

def _put(db, bid, body):
    _app.get_db = lambda: db
    db.close = lambda: None      # the route closes it; the test still needs it afterwards
    with _app.app.test_request_context(f"/api/brands/{bid}/price-table",
                                       method="PUT", json=body):
        return _app.api_brand_price_table(bid).get_json()


def test_the_endpoint_writes_the_set_the_operator_named():
    db = _db()
    _put(db, 1, {"rows": [{"brand": "Pigeon", "kind": "exact", "value": "$12.99"}]})
    _put(db, 1, {"rows": [{"brand": "Pigeon", "kind": "exact", "value": "$99.00"}],
                 "set_name": "Holiday"})
    b = db.get_brand(1)
    assert sorted(price_sets(b)) == ["Default", "Holiday"]
    assert price_set_rows(b)["pigeon"]["rows"][0]["value"] == "$12.99"


def test_the_endpoint_hands_back_the_WHOLE_blob_not_the_one_set():
    """The UI caches `brand.price_table`. Caching the single set that was saved would read back as
    a flat table and silently drop every other set the brand holds."""
    db = _db()
    _put(db, 1, {"rows": [{"brand": "Pigeon", "kind": "exact", "value": "$12.99"}]})
    res = _put(db, 1, {"rows": [{"brand": "Pigeon", "kind": "exact", "value": "$99.00"}],
                       "set_name": "Holiday"})
    assert sorted(price_sets({"price_table": res["price_table_blob"]})) == ["Default", "Holiday"]


def test_the_pin_can_actually_be_WRITTEN_to_a_blog():
    """The generate path pins with `update_blog(price_rows=…, price_set=…)`. A column that exists
    but is not in the allowed-update set writes nothing and says nothing."""
    db = _db()
    bid = db.save_blog(1, "seed", title="t", body_markdown="body")
    db.update_blog(bid, price_rows=json.dumps([{"brand": "Pigeon", "value": "$12.99"}]),
                   price_set="Holiday")
    row = db.get_blog(bid)
    assert row["price_set"] == "Holiday"
    assert json.loads(row["price_rows"])[0]["value"] == "$12.99"
    db.close()
