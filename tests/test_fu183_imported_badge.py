"""FU183 — an IMPORTED blog (FU180 upload) is visibly marked as such.

It matters beyond bookkeeping: an uploaded body has no [S#] evidence chain, no Sources section of its
own and no quality scorecard, so without a marker "why has this one got no sources?" has no answer.
The marker is prompt_version="imported" (stamped by the import endpoint). A regenerate that REPLACES
the body clears it, so the badge can't keep asserting an origin the text no longer has. $0, no network.
"""
import io
import os
import tempfile

import pytest

from db import Database
from generators.blog_gen import PROMPT_VERSION

HTML = (b'<!doctype html><html><head><title>Meta</title></head><body>'
        b'<h1>An Imported Piece</h1><p>Body text from a file.</p></body></html>')


def _client(tmp_db):
    import app as appmod
    appmod.DB_PATH = tmp_db
    appmod._db_initialized = True
    return appmod.app.test_client()


@pytest.fixture()
def env():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    db.close()
    yield _client(path), bid, path
    os.unlink(path)


def _import(client, brand_id):
    r = client.post("/api/blogs/import",
                    data={"file": (io.BytesIO(HTML), "piece.html"), "brand_id": str(brand_id)},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    return r.get_json()["blog_id"]


def _row(path, blog_id):
    """The row as the LIST endpoint serves it — the badge reads from here, so the column must be in
    that query, not just in get_blog."""
    db = Database(path)
    db.connect()
    rows = db.get_all_blogs()
    db.close()
    return next(r for r in rows if r["id"] == blog_id)


def test_an_imported_blog_is_marked():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path); db.connect(); db.initialize()
        sub = db.ensure_live_subreddit("t"); bid = db.add_brand(sub["id"], "Acme"); db.close()
        c = _client(path)
        blog_id = _import(c, bid)
        assert _row(path, blog_id)["prompt_version"] == "imported"
    finally:
        os.unlink(path)


def test_the_list_query_actually_returns_the_marker():
    """get_all_blogs selects an explicit column list — the badge silently never renders if
    prompt_version isn't in it."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path); db.connect(); db.initialize()
        sub = db.ensure_live_subreddit("t"); bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "seed", title="T", prompt_version="imported")
        rows = db.get_all_blogs(); db.close()
        assert "prompt_version" in rows[0], "the list row must carry the field the badge reads"
        assert rows[0]["prompt_version"] == "imported"
    finally:
        os.unlink(path)


def test_a_generated_blog_is_not_marked():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path); db.connect(); db.initialize()
        sub = db.ensure_live_subreddit("t"); bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "seed", title="T", prompt_version=PROMPT_VERSION)
        rows = db.get_all_blogs(); db.close()
        assert rows[0]["prompt_version"] != "imported"
    finally:
        os.unlink(path)


def test_regenerating_the_body_clears_the_marker():
    """The honesty half: once Claude has rewritten the article it is no longer imported, so the badge
    must stop claiming it is. Simulates what the regenerate task writes."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path); db.connect(); db.initialize()
        sub = db.ensure_live_subreddit("t"); bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "seed", title="T", prompt_version="imported")
        db.update_blog(blog_id, body_markdown="# regenerated", prompt_version=PROMPT_VERSION)
        rows = db.get_all_blogs(); db.close()
        assert rows[0]["prompt_version"] == PROMPT_VERSION
    finally:
        os.unlink(path)


def test_a_partial_regenerate_keeps_the_marker():
    """Regenerating only the LinkedIn post doesn't touch the imported BODY, so the origin stands."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path); db.connect(); db.initialize()
        sub = db.ensure_live_subreddit("t"); bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "seed", title="T", prompt_version="imported")
        db.update_blog(blog_id, linkedin_text="a new post")     # what part='linkedin' writes
        rows = db.get_all_blogs(); db.close()
        assert rows[0]["prompt_version"] == "imported"
    finally:
        os.unlink(path)
