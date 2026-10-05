"""FU287 — the same 3rd-party thread can be imported more than once.

One good Reddit thread is a seeding target for several brands, and sometimes for a second wave
under one brand. The import endpoint used to refuse: it looked the URL up and handed back the
first import instead ("Already imported as post #N").

Lifting that was not a matter of deleting the check. `post_urls.reddit_url` was `TEXT NOT NULL
UNIQUE`, and `link_url_to_post` resolved a collision by REPOINTING the existing row at the new
post. So dropping the guard alone would have let the second import quietly steal the first one's
anchor — leaving that post with no thread URL, its card unlinked and its +HQ/+Cmts no longer
deploying under the thread. Nothing would have errored.

So: the UNIQUE is gone (a rebuild, because SQLite will not drop a column constraint's implicit
index), and sharing a URL across posts is opt-in via `allow_shared` so every deploy path keeps
its replace-existing behaviour. The tests below are mostly about what did NOT change.
"""
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from db import Database  # noqa: E402

URL = "https://www.reddit.com/r/chipdesign/comments/1vmksub/need_bulk_cheap_ic_chips/"
URL2 = "https://www.reddit.com/r/AskElectronics/comments/1abcdef/other_thread/"


def _open(path):
    d = Database(path)
    d.connect()
    d.initialize()
    return d


@pytest.fixture()
def db(tmp_path):
    d = _open(str(tmp_path / "t.db"))
    yield d
    d.close()


def _scaffold(d, n_brands=2):
    sub = d.ensure_live_subreddit("chipdesign")
    brands = [d.add_brand(sub["id"], f"Brand{i}", context="ctx") for i in range(n_brands)]
    return sub, brands


def _import(d, sub, brand_id, url, title):
    """What the endpoint does for one third-party import."""
    pid = d.save_post(subreddit_id=sub["id"], brand_id=brand_id, title=title, body="b",
                      storyline="third-party", status="complete", suggested_post_day=0,
                      prompt_version="third-party", brand_ids=[brand_id], is_third_party=1)
    d.link_url_to_post(pid, url, sub["id"], allow_shared=True)
    return pid


# ── the thing that was asked for ───────────────────────────────────────────────────────────────
def test_the_same_thread_imports_under_two_brands(db):
    sub, (b1, b2) = _scaffold(db)
    p1 = _import(db, sub, b1, URL, "first")
    p2 = _import(db, sub, b2, URL, "second")
    assert p1 != p2
    assert db.get_url_for_post(p1) == URL
    assert db.get_url_for_post(p2) == URL


def test_the_same_thread_imports_twice_under_one_brand(db):
    """'Always allow' — no per-brand guard either."""
    sub, (b1, _) = _scaffold(db)
    p1 = _import(db, sub, b1, URL, "wave 1")
    p2 = _import(db, sub, b1, URL, "wave 2")
    assert p1 != p2
    assert db.get_url_for_post(p1) == db.get_url_for_post(p2) == URL


def test_the_first_import_keeps_its_anchor(db):
    """THE regression. A silent steal here unlinks the first post's card and breaks its
    +HQ/+Cmts deploys, with nothing raised anywhere."""
    sub, (b1, b2) = _scaffold(db)
    p1 = _import(db, sub, b1, URL, "first")
    for i in range(4):
        _import(db, sub, b2, URL, f"later {i}")
        assert db.get_url_for_post(p1) == URL, f"first import lost its anchor after import {i + 2}"


def test_each_import_gets_its_own_anchor_row(db):
    sub, (b1, b2) = _scaffold(db)
    _import(db, sub, b1, URL, "first")
    _import(db, sub, b2, URL, "second")
    n = db.conn.execute("SELECT COUNT(*) FROM post_urls WHERE reddit_url = ?", (URL,)).fetchone()[0]
    assert n == 2


def test_relinking_a_post_replaces_its_row_rather_than_adding_one(db):
    """`link_url_to_post` clears the post's existing rows first. Without that, a corrected
    URL leaves the old row behind and the post listings — which LEFT JOIN post_urls — show
    that post twice."""
    sub, (b1, _) = _scaffold(db)
    p1 = _import(db, sub, b1, URL, "first")
    for u in (URL2, URL, URL2):
        db.link_url_to_post(p1, u, sub["id"], allow_shared=True)
        rows = db.conn.execute("SELECT reddit_url FROM post_urls WHERE post_id = ?",
                               (p1,)).fetchall()
        assert len(rows) == 1, f"post {p1} has {len(rows)} anchor rows after relinking to {u}"
        assert rows[0]["reddit_url"] == u


def test_one_post_still_has_exactly_one_anchor(db):
    """Sharing a URL across posts must not let a single post accumulate rows — the post
    listings LEFT JOIN post_urls, and a second row would duplicate that post in the table."""
    sub, (b1, _) = _scaffold(db)
    p1 = _import(db, sub, b1, URL, "first")
    db.link_url_to_post(p1, URL2, sub["id"], allow_shared=True)   # corrected URL
    rows = db.conn.execute("SELECT reddit_url FROM post_urls WHERE post_id = ?", (p1,)).fetchall()
    assert len(rows) == 1 and rows[0]["reddit_url"] == URL2


# ── what must NOT have changed ─────────────────────────────────────────────────────────────────
def test_deploy_path_still_repoints_by_default(db):
    """Every deploy caller uses the default. Two of OUR OWN generated posts cannot both be the
    same live thread, so a collision there still means 'this URL moved' — not 'share it'."""
    sub, (b1, b2) = _scaffold(db)
    p1 = _import(db, sub, b1, URL, "first")
    p2 = db.save_post(subreddit_id=sub["id"], brand_id=b2, title="ours", body="b",
                      storyline="question", status="complete", suggested_post_day=0,
                      prompt_version="v1", brand_ids=[b2])
    db.link_url_to_post(p2, URL, sub["id"])          # no allow_shared
    assert db.get_url_for_post(p2) == URL
    assert db.get_url_for_post(p1) is None           # repointed, as before
    n = db.conn.execute("SELECT COUNT(*) FROM post_urls WHERE reddit_url = ?", (URL,)).fetchone()[0]
    assert n == 1


def test_add_post_url_is_still_idempotent(db):
    """Its callers call it repeatedly with the same URL and relied on the UNIQUE to swallow
    the repeat; the dedupe is now explicit and must behave identically."""
    sub, (b1, _) = _scaffold(db)
    first = db.add_post_url(sub["id"], URL, None)
    again = db.add_post_url(sub["id"], URL, None)
    assert first == again
    n = db.conn.execute("SELECT COUNT(*) FROM post_urls WHERE reddit_url = ?", (URL,)).fetchone()[0]
    assert n == 1


def test_count_posts_for_url(db):
    sub, (b1, b2) = _scaffold(db)
    assert db.count_posts_for_url(URL) == 0
    _import(db, sub, b1, URL, "first")
    assert db.count_posts_for_url(URL) == 1
    _import(db, sub, b2, URL, "second")
    assert db.count_posts_for_url(URL) == 2
    assert db.count_posts_for_url(URL2) == 0


# ── the migration ──────────────────────────────────────────────────────────────────────────────
def _legacy_db(path):
    """A post_urls in its PRE-FU287 shape.

    Built by creating the real schema and then putting the inline UNIQUE back, rather than
    hand-writing an approximation of the old database — a hand-written one drifts from the
    real schema and stops exercising the migration that actually runs.
    """
    d = _open(path)
    sub = d.ensure_live_subreddit("chipdesign")
    bid = d.add_brand(sub["id"], "B", context="c")
    pid = d.save_post(subreddit_id=sub["id"], brand_id=bid, title="kept", body="b",
                      storyline="third-party", status="complete", suggested_post_day=0,
                      prompt_version="third-party", brand_ids=[bid], is_third_party=1)
    d.close()

    c = sqlite3.connect(path)
    c.execute("PRAGMA foreign_keys = OFF")
    c.executescript("""
        CREATE TABLE post_urls_legacy (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            post_id      INTEGER REFERENCES posts(id),
            subreddit_id INTEGER NOT NULL REFERENCES subreddits(id),
            reddit_url   TEXT NOT NULL UNIQUE,
            added_at     TEXT DEFAULT (datetime('now'))
        );
        DROP TABLE post_urls;
        ALTER TABLE post_urls_legacy RENAME TO post_urls;
        CREATE INDEX idx_post_urls_sub ON post_urls(subreddit_id);
        CREATE INDEX idx_post_urls_post ON post_urls(post_id);
        CREATE INDEX idx_post_urls_added_at ON post_urls(added_at);
    """)
    c.execute("INSERT INTO post_urls (id, post_id, subreddit_id, reddit_url, added_at) "
              "VALUES (3, ?, ?, ?, '2026-01-01')", (pid, sub["id"], URL))
    c.commit()
    c.close()
    return pid, sub["id"]


def _unique_indexes(conn):
    return [r[1] for r in conn.execute("PRAGMA index_list(post_urls)") if r[3] == "u"]


def test_legacy_db_loses_the_unique_and_keeps_its_rows(tmp_path):
    path = str(tmp_path / "legacy.db")
    pid, sid = _legacy_db(path)
    pre = sqlite3.connect(path)
    assert _unique_indexes(pre), "fixture is not legacy — it has no UNIQUE to drop"
    pre.close()

    d = _open(path)
    try:
        assert _unique_indexes(d.conn) == []
        row = d.conn.execute("SELECT * FROM post_urls WHERE id = 3").fetchone()
        assert row["post_id"] == pid and row["reddit_url"] == URL
        assert row["subreddit_id"] == sid and row["added_at"] == "2026-01-01"
        # the rebuild must put every index back, not just the one in the schema block
        names = {r[1] for r in d.conn.execute("PRAGMA index_list(post_urls)")}
        assert {"idx_post_urls_sub", "idx_post_urls_post", "idx_post_urls_added_at"} <= names
        # and the table actually accepts the duplicate now
        d.conn.execute("INSERT INTO post_urls (post_id, subreddit_id, reddit_url) VALUES (?, ?, ?)",
                       (pid, sid, URL))
        d.conn.commit()
    finally:
        d.close()


def test_migration_is_idempotent(tmp_path):
    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    for _ in range(3):
        d = _open(path)
        try:
            assert _unique_indexes(d.conn) == []
            assert d.conn.execute("SELECT COUNT(*) FROM post_urls").fetchone()[0] == 1
        finally:
            d.close()


def test_the_rebuild_fires_once_and_never_on_a_fresh_db(tmp_path, capsys):
    """The end state is identical whether or not the rebuild runs, so only its announcement
    can tell them apart — and an unguarded rebuild copies the whole table on every boot."""
    MARK = "post_urls rebuilt"
    d = _open(str(tmp_path / "fresh.db"))
    d.close()
    assert MARK not in capsys.readouterr().out, "a fresh database should need no rebuild"

    path = str(tmp_path / "legacy.db")
    _legacy_db(path)
    capsys.readouterr()
    d = _open(path)
    d.close()
    assert MARK in capsys.readouterr().out, "the legacy database was never rebuilt"
    d = _open(path)
    d.close()
    assert MARK not in capsys.readouterr().out, "rebuild re-ran on an already-migrated database"


def test_a_fresh_db_is_born_with_the_right_shape(tmp_path):
    """A fresh database must come out of the schema block already correct — same indexes, no
    UNIQUE — so it never depends on the rebuild to fix it."""
    d = _open(str(tmp_path / "fresh.db"))
    try:
        assert _unique_indexes(d.conn) == []
        names = {r[1] for r in d.conn.execute("PRAGMA index_list(post_urls)")}
        assert {"idx_post_urls_sub", "idx_post_urls_post", "idx_post_urls_added_at"} <= names
    finally:
        d.close()


# ── plumbing ───────────────────────────────────────────────────────────────────────────────────
def _read(rel):
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel),
                encoding="utf-8").read()


def test_endpoint_no_longer_refuses_a_repeat():
    app = _read("app.py")
    assert '"duplicate": True' not in app
    assert "db_check.find_post_by_url(url)" not in app
    assert "prior_imports = db_check.count_posts_for_url(url)" in app
    assert 'db.link_url_to_post(post_id, url, sub["id"], allow_shared=True)' in app
    # the count only reaches the UI if the task result carries it
    assert '"prior_imports": prior_imports' in app


def test_ui_has_no_dead_duplicate_branch():
    """Three different callers hit this endpoint — the import modal, the Live Search row
    action and the Live Search bulk import — and each had its own 'already imported' branch.
    Leaving any of them in means a working import silently reports itself as a no-op.

    Matched on `res && res.duplicate`, which only those branches use; `res.duplicates`
    (plural) belongs to the unrelated Live Search save endpoint and must survive.
    """
    html = _read("templates/index.html")
    assert "res && res.duplicate" not in html
    assert "Already imported as post" not in html
    assert "Already in Live Subs as post" not in html
    assert "res.duplicates" in html, "clobbered the unrelated Live Search save counter"
    assert html.count("prior_imports") >= 3


def test_external_post_lookup_is_deterministic():
    """With several posts on one URL, 'the first row' has to mean the earliest import."""
    cg = _read(os.path.join("generators", "comment_gen.py"))
    assert ('"SELECT post_id FROM post_urls WHERE reddit_url = ? ORDER BY id LIMIT 1"' in cg)
