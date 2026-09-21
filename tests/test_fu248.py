"""FU248 — the blog LIST could not answer the three questions it is looked at to answer.

Everything about a blog other than its title lived one click inside it. So:

  * whether a watermark-free rewrite / LinkedIn post / LinkedIn article / YouTube package had been
    made for a blog could only be learned by opening that blog, one at a time;
  * "which of these still need a watermark-free version" had no answer at all — there was no filter
    on what exists, only on brand and status;
  * marking a blog published required opening it AND pasting a live URL, because the endpoint
    refused the mark without one — so a piece that had shipped but whose link nobody had to hand
    could not be recorded as shipped, and the list stayed wrong until someone chased the URL;
  * a blog had no stable name. Rows are ordered newest-first and re-order under a filter, so "the
    third one" meant a different blog after the next generation.

The flags, the filter and the serial all come from ONE place — the list query — so an icon and the
filter that claims to find it can never disagree about what counts as present.
"""
import json
import os
import tempfile

import pytest

from db import Database


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def dbp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        yield path
    finally:
        os.remove(path)


def _open(path):
    db = Database(path)
    db.connect()
    db.initialize()
    return db


def _brand(db, name="Acme"):
    sub = db.ensure_live_subreddit("t")
    return db.add_brand(sub["id"], name)


def _blog(db, bid, title, **cols):
    blog_id = db.save_blog(bid, seed=title.lower(), title=title,
                           prompt_version=cols.pop("prompt_version", ""))
    if cols:
        db.update_blog(blog_id, **cols)
    return blog_id


def _by_title(rows):
    return {r["title"]: r for r in rows}


# ── the serial ───────────────────────────────────────────────────────────────────────────────────
def test_every_blog_gets_the_next_serial_and_it_is_global(dbp):
    """The list's default view spans every brand, so the serial has to be unique across brands —
    per-brand numbering would print several '#1's side by side and identify nothing."""
    db = _open(dbp)
    try:
        a, b = _brand(db, "Acme"), _brand(db, "Beta")
        _blog(db, a, "First")
        _blog(db, b, "Second")     # a DIFFERENT brand — must still take the next number
        _blog(db, a, "Third")
        rows = _by_title(db.get_all_blogs())
        assert [rows["First"]["blog_number"], rows["Second"]["blog_number"],
                rows["Third"]["blog_number"]] == [1, 2, 3]
    finally:
        db.close()


def test_the_serial_does_not_move_when_the_list_is_filtered(dbp):
    """The whole point of a serial over a row position: a position changes the moment you filter."""
    db = _open(dbp)
    try:
        a, b = _brand(db, "Acme"), _brand(db, "Beta")
        _blog(db, a, "First")
        _blog(db, b, "Second")
        _blog(db, a, "Third")
        filtered = _by_title(db.get_all_blogs(brand_id=a))
        assert filtered["Third"]["blog_number"] == 3     # not 2, its position in THIS view
        assert filtered["First"]["blog_number"] == 1
    finally:
        db.close()


def test_existing_blogs_are_backfilled_in_creation_order(dbp):
    """A database that predates the column gets numbered as a history, not restarted at today."""
    db = _open(dbp)
    try:
        bid = _brand(db)
        for t in ("Oldest", "Middle", "Newest"):
            _blog(db, bid, t)
        # stamp distinct creation times, then take the column away to look like a pre-FU248 db
        for t, when in (("Oldest", "2026-01-01 00:00:00"), ("Middle", "2026-02-01 00:00:00"),
                        ("Newest", "2026-03-01 00:00:00")):
            db.conn.execute("UPDATE blogs SET created_at = ? WHERE title = ?", (when, t))
        db.conn.execute("ALTER TABLE blogs DROP COLUMN blog_number")
        db.conn.commit()
        db.close()

        db = _open(dbp)                                   # re-run migrations → re-add + backfill
        rows = _by_title(db.get_all_blogs())
        assert rows["Oldest"]["blog_number"] == 1
        assert rows["Middle"]["blog_number"] == 2
        assert rows["Newest"]["blog_number"] == 3
    finally:
        db.close()


def test_a_blog_saved_after_the_backfill_continues_the_sequence(dbp):
    db = _open(dbp)
    try:
        bid = _brand(db)
        for t in ("A", "B"):
            _blog(db, bid, t)
        db.conn.execute("ALTER TABLE blogs DROP COLUMN blog_number")
        db.conn.commit()
        db.close()

        db = _open(dbp)
        _blog(db, _brand(db, "Other"), "C")
        assert _by_title(db.get_all_blogs())["C"]["blog_number"] == 3
    finally:
        db.close()


# ── the asset flags ──────────────────────────────────────────────────────────────────────────────
def test_the_list_reports_what_exists_without_returning_any_of_it(dbp):
    """Flags, not bodies: drawing four icons for 200 blogs must not drag 200 rewrites over the
    wire. So the row carries has_* booleans and none of the text behind them."""
    db = _open(dbp)
    try:
        bid = _brand(db)
        _blog(db, bid, "Loaded", rewritten_body="x" * 50, linkedin_text="post",
              linkedin_article="article", youtube_script="script")
        _blog(db, bid, "Bare")
        rows = _by_title(db.get_all_blogs())
        loaded, bare = rows["Loaded"], rows["Bare"]
        assert [loaded["has_rewritten"], loaded["has_linkedin"],
                loaded["has_li_article"], loaded["has_youtube"]] == [1, 1, 1, 1]
        assert [bare["has_rewritten"], bare["has_linkedin"],
                bare["has_li_article"], bare["has_youtube"]] == [0, 0, 0, 0]
        for col in ("rewritten_body", "linkedin_article", "youtube_script"):
            assert col not in loaded          # the body itself never rides in the list row
    finally:
        db.close()


def test_an_empty_string_is_not_generated(dbp):
    """A column written as '' by a failed run must read as 'not made yet', or the icon lies."""
    db = _open(dbp)
    try:
        bid = _brand(db)
        _blog(db, bid, "Empty", rewritten_body="", youtube_script="")
        row = _by_title(db.get_all_blogs())["Empty"]
        assert row["has_rewritten"] == 0 and row["has_youtube"] == 0
    finally:
        db.close()


# ── the filter ───────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("has,expect", [
    ("rewritten", {"Rewritten"}),
    ("linkedin", {"LinkedIn"}),
    ("li_article", {"Article"}),
    ("youtube", {"YouTube"}),
    ("imported", {"Imported"}),
])
def test_each_filter_returns_exactly_the_blogs_that_have_that_thing(dbp, has, expect):
    db = _open(dbp)
    try:
        bid = _brand(db)
        _blog(db, bid, "Rewritten", rewritten_body="x")
        _blog(db, bid, "LinkedIn", linkedin_text="x")
        _blog(db, bid, "Article", linkedin_article="x")
        _blog(db, bid, "YouTube", youtube_script="x")
        _blog(db, bid, "Imported", prompt_version="imported")
        _blog(db, bid, "Plain")
        assert {r["title"] for r in db.get_all_blogs(has=has)} == expect
    finally:
        db.close()


def test_the_filter_agrees_with_the_icon_it_claims_to_find(dbp):
    """The failure this guards: a filter written against a different predicate than the flag, so
    'has a watermark-free version' returns a blog whose icon is greyed out."""
    db = _open(dbp)
    try:
        bid = _brand(db)
        _blog(db, bid, "Rewritten", rewritten_body="x")
        _blog(db, bid, "Plain")
        for has, flag in (("rewritten", "has_rewritten"), ("linkedin", "has_linkedin"),
                          ("li_article", "has_li_article"), ("youtube", "has_youtube")):
            _blog(db, bid, f"Only {has}", **{
                {"rewritten": "rewritten_body", "linkedin": "linkedin_text",
                 "li_article": "linkedin_article", "youtube": "youtube_script"}[has]: "x"})
            for row in db.get_all_blogs(has=has):
                assert row[flag] == 1
    finally:
        db.close()


def test_published_and_unpublished_split_the_whole_list(dbp):
    db = _open(dbp)
    try:
        bid = _brand(db)
        live = _blog(db, bid, "Live")
        _blog(db, bid, "Draft")
        db.upsert_blog_platform(live, "website", published_url="https://x.com/p")
        assert {r["title"] for r in db.get_all_blogs(has="published")} == {"Live"}
        assert {r["title"] for r in db.get_all_blogs(has="unpublished")} == {"Draft"}
    finally:
        db.close()


def test_a_blog_published_without_a_link_still_counts_as_published(dbp):
    """The filter keys on the platform ROW, not on whether anyone has pasted the URL yet —
    otherwise the new URL-less mark would be invisible in exactly the view that records it."""
    db = _open(dbp)
    try:
        bid = _brand(db)
        live = _blog(db, bid, "LiveNoLink")
        db.upsert_blog_platform(live, "website", published_url=None)
        assert {r["title"] for r in db.get_all_blogs(has="published")} == {"LiveNoLink"}
        assert db.get_all_blogs(has="unpublished") == []
    finally:
        db.close()


def test_an_unknown_filter_is_ignored_rather_than_returning_nothing(dbp):
    """A stale bookmark or a typo'd query must not read as 'you have no blogs'."""
    db = _open(dbp)
    try:
        _blog(db, _brand(db), "One")
        assert len(db.get_all_blogs(has="no_such_thing")) == 1
    finally:
        db.close()


def test_the_filter_composes_with_brand_and_status(dbp):
    db = _open(dbp)
    try:
        a, b = _brand(db, "Acme"), _brand(db, "Beta")
        _blog(db, a, "AcmeRewritten", rewritten_body="x")
        _blog(db, b, "BetaRewritten", rewritten_body="x")
        _blog(db, a, "AcmePlain")
        assert {r["title"] for r in db.get_all_blogs(brand_id=a, has="rewritten")} == {"AcmeRewritten"}
    finally:
        db.close()


# ── publishing without the link ──────────────────────────────────────────────────────────────────
def _client(path):
    import app as appmod
    appmod.DB_PATH = path
    appmod._db_initialized = True
    return appmod.app.test_client()


def test_publish_no_longer_requires_a_url(dbp):
    """Shipping a piece and having its link to hand are two different moments. Refusing the mark
    until the URL arrived left the record wrong for however long that took."""
    db = _open(dbp)
    blog_id = _blog(db, _brand(db), "Shipped")
    db.close()

    r = _client(dbp).post(f"/api/blogs/{blog_id}/publish", json={"platform": "website"})
    assert r.status_code == 200

    db = _open(dbp)
    try:
        blog = db.get_blog(blog_id)
        assert blog["status"] == "published"
        site = [p for p in blog["platforms"] if p["platform"] == "website"][0]
        assert site["status"] == "published"
        assert not (site["published_url"] or "")       # recorded as live, link still unknown
    finally:
        db.close()


def test_the_url_is_still_stored_when_it_is_given(dbp):
    db = _open(dbp)
    blog_id = _blog(db, _brand(db), "Shipped")
    db.close()

    _client(dbp).post(f"/api/blogs/{blog_id}/publish",
                      json={"platform": "website", "published_url": "https://x.com/p"})
    db = _open(dbp)
    try:
        site = [p for p in db.get_blog(blog_id)["platforms"] if p["platform"] == "website"][0]
        assert site["published_url"] == "https://x.com/p"
    finally:
        db.close()


def test_the_link_can_be_added_after_the_mark(dbp):
    """The sequence the change exists for: mark it live now, paste the URL when it turns up."""
    db = _open(dbp)
    blog_id = _blog(db, _brand(db), "Shipped")
    db.close()
    client = _client(dbp)

    client.post(f"/api/blogs/{blog_id}/publish", json={"platform": "website"})
    client.post(f"/api/blogs/{blog_id}/publish",
                json={"platform": "website", "published_url": "https://x.com/p"})

    db = _open(dbp)
    try:
        sites = [p for p in db.get_blog(blog_id)["platforms"] if p["platform"] == "website"]
        assert len(sites) == 1                          # updated in place, not duplicated
        assert sites[0]["published_url"] == "https://x.com/p"
    finally:
        db.close()


def test_an_unknown_platform_is_still_refused(dbp):
    db = _open(dbp)
    blog_id = _blog(db, _brand(db), "Shipped")
    db.close()
    r = _client(dbp).post(f"/api/blogs/{blog_id}/publish", json={"platform": "tiktok"})
    assert r.status_code == 400


def test_the_list_endpoint_passes_the_filter_through(dbp):
    db = _open(dbp)
    bid = _brand(db)
    _blog(db, bid, "Rewritten", rewritten_body="x")
    _blog(db, bid, "Plain")
    db.close()

    client = _client(dbp)
    assert {b["title"] for b in client.get("/api/blogs?has=rewritten").get_json()} == {"Rewritten"}
    assert len(client.get("/api/blogs").get_json()) == 2
