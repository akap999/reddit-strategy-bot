"""FU250 — an imported version attached to an EXISTING blog, and a rewrite of that.

`/api/blogs/import` turns a file into a whole new blog. That is the wrong shape when the blog is
already here and what arrived is another copy of IT — the client's edited draft, the version that
actually went live, a rewrite someone did elsewhere. Importing that as a second row splits one piece
of work in two, each with its own publish state, its own platforms and its own watermark-free
version, and nothing says which of them is the real one.

So the imported copy lives beside the generated body on the same row, and the watermark-free pass
runs on whichever body is real. The generated body is never touched by any of it.
"""
import io
import json
import os
import tempfile

import pytest

from db import Database
from generators.blog_gen import _WRITER_SURFACES
from generators.body_sanitize import MARKDOWN_FIELDS


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


def _seed(path, body="# Generated\n\nThe body we wrote [S1].\n"):
    db = _open(path)
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(bid, "what happens when you stop", title="What happens when you stop",
                           body_markdown=body)
    db.close()
    return blog_id


def _client(path):
    import app as appmod
    appmod.DB_PATH = path
    appmod._db_initialized = True
    return appmod.app.test_client()


def _upload(client, blog_id, text, name="client-final.md"):
    return client.post(f"/api/blogs/{blog_id}/import-version",
                       data={"file": (io.BytesIO(text.encode()), name)},
                       content_type="multipart/form-data")


# ── attaching ────────────────────────────────────────────────────────────────────────────────────
def test_a_file_is_attached_to_the_blog_that_is_already_there(dbp):
    blog_id = _seed(dbp)
    r = _upload(_client(dbp), blog_id, "# Client's edit\n\nThe copy that actually went live.\n")
    assert r.status_code == 200
    blog = r.get_json()
    assert "actually went live" in blog["imported_body"]
    assert blog["imported_meta"]["name"] == "client-final.md"
    assert blog["imported_meta"]["chars"] > 0


def test_the_generated_body_is_never_touched(dbp):
    """The whole reason it lives on the same row instead of replacing anything."""
    blog_id = _seed(dbp)
    _upload(_client(dbp), blog_id, "# Client's edit\n\nA different article entirely.\n")
    db = _open(dbp)
    try:
        blog = db.get_blog(blog_id)
        assert "The body we wrote [S1]." in blog["body_markdown"]
        assert "different article" in blog["imported_body"]
    finally:
        db.close()


def test_text_can_be_pasted_instead_of_uploaded(dbp):
    blog_id = _seed(dbp)
    r = _client(dbp).post(f"/api/blogs/{blog_id}/import-version",
                          json={"text": "# Pasted\n\nThe version that shipped.\n"})
    assert r.status_code == 200 and "that shipped" in r.get_json()["imported_body"]


def test_html_is_read_as_the_article_not_as_markup(dbp):
    """An imported copy usually arrives as the exported HTML or a Word file."""
    blog_id = _seed(dbp)
    html = ("<html><head><title>T</title></head><body><h1>Live version</h1>"
            "<p>What the client published.</p></body></html>")
    r = _upload(_client(dbp), blog_id, html, "live.html")
    body = r.get_json()["imported_body"]
    assert "What the client published." in body and "<p>" not in body


def test_re_uploading_replaces_the_import_and_drops_its_stale_rewrite(dbp):
    """A rewrite of a body that no longer exists is worse than no rewrite — it reads as current."""
    blog_id = _seed(dbp)
    c = _client(dbp)
    _upload(c, blog_id, "# One\n\nFirst upload.\n")
    db = _open(dbp)
    db.update_blog(blog_id, imported_rewritten="A rewrite of the FIRST upload.")
    db.close()

    _upload(c, blog_id, "# Two\n\nSecond upload.\n", "v2.md")
    db = _open(dbp)
    try:
        blog = db.get_blog(blog_id)
        assert "Second upload" in blog["imported_body"]
        assert not (blog["imported_rewritten"] or "").strip()
        assert blog["imported_meta"]["name"] == "v2.md"
    finally:
        db.close()


def test_removing_the_import_leaves_the_blog_alone(dbp):
    blog_id = _seed(dbp)
    c = _client(dbp)
    _upload(c, blog_id, "# Client's edit\n\nLive copy.\n")
    assert c.delete(f"/api/blogs/{blog_id}/import-version").status_code == 200
    db = _open(dbp)
    try:
        blog = db.get_blog(blog_id)
        assert not (blog["imported_body"] or "").strip()
        assert "The body we wrote [S1]." in blog["body_markdown"]
    finally:
        db.close()


@pytest.mark.parametrize("payload", [{}, {"text": "   "}])
def test_an_empty_request_is_refused(dbp, payload):
    blog_id = _seed(dbp)
    assert _client(dbp).post(f"/api/blogs/{blog_id}/import-version",
                             json=payload).status_code == 400


def test_a_file_with_no_article_in_it_is_refused(dbp):
    blog_id = _seed(dbp)
    r = _upload(_client(dbp), blog_id, "   \n\n  \n", "blank.md")
    assert r.status_code == 400 and "no article text" in r.get_json()["error"]


def test_a_missing_blog_is_a_404(dbp):
    _seed(dbp)
    assert _upload(_client(dbp), 9999, "# X\n\nY.\n").status_code == 404


def test_an_imported_body_gets_the_same_guards_as_a_generated_one(dbp):
    """An outside file is the likeliest source of a curly quote or an em-dash in the whole system."""
    assert "imported_body" in MARKDOWN_FIELDS and "imported_rewritten" in MARKDOWN_FIELDS
    blog_id = _seed(dbp)
    r = _upload(_client(dbp), blog_id, "# X\n\nIt costs “$149” — per month.\n")
    body = r.get_json()["imported_body"]
    assert "“" not in body and "—" not in body
    assert "149" in body                       # the fact survives the symbol scrub


# ── rewriting it ─────────────────────────────────────────────────────────────────────────────────
def test_the_rewrite_reads_and_writes_the_imported_columns(dbp):
    """The real endpoint, with only the writer pass itself stubbed — so the guards, the Sources
    rebuild and the column wiring are all the shipped ones."""
    import app as appmod
    import generators.blog_gen as BG

    blog_id = _seed(dbp)
    c = _client(dbp)
    _upload(c, blog_id, "# Client's edit\n\nThe copy that went live.\n")

    seen = {}

    def _fake_task(kind, fn, pass_task_id=False):
        seen["result"] = fn(_task_id="t1") if pass_task_id else fn()
        return "t1"

    def _fake_pass(self, article, body, brand, seed, surface="blog"):
        seen["surface"], seen["source"] = surface, body
        article["writer_mode_used"] = "rewrite"
        article["writer_overlap"] = 0.04
        article["writer_grade"] = "thorough"
        return "# Client's edit\n\nThe copy that shipped, reworded.\n"

    def _fake_h1(self, body, seed):
        seen["h1_pinned"] = True
        return body

    _saved = (appmod.start_task, appmod._resolve_writer_config,
              BG.BlogGenerator._apply_writer_pass, BG.BlogGenerator._force_h1)
    appmod.start_task = _fake_task
    appmod._resolve_writer_config = lambda db: {"endpoint_url": "http://w.test", "key": "k",
                                                "model": "m", "mode": "rewrite"}
    BG.BlogGenerator._apply_writer_pass = _fake_pass
    BG.BlogGenerator._force_h1 = _fake_h1
    try:
        r = c.post(f"/api/blogs/{blog_id}/rewrite", json={"surface": "imported"})
        assert r.status_code == 200
    finally:
        (appmod.start_task, appmod._resolve_writer_config,
         BG.BlogGenerator._apply_writer_pass, BG.BlogGenerator._force_h1) = _saved

    assert seen["surface"] == "imported"
    assert "went live" in seen["source"]               # it rewrote the IMPORT, not the generated body
    assert "h1_pinned" not in seen                     # the operator's heading is left as uploaded
    db = _open(dbp)
    try:
        blog = db.get_blog(blog_id)
        assert "reworded" in blog["imported_rewritten"]
        assert "The body we wrote [S1]." in blog["body_markdown"]      # untouched
        assert not (blog["rewritten_body"] or "").strip()              # the blog's own is untouched
        assert blog["rewrites_meta"]["imported"]["grade"] == "thorough"
    finally:
        db.close()


def test_an_unknown_surface_is_still_refused(dbp):
    blog_id = _seed(dbp)
    r = _client(dbp).post(f"/api/blogs/{blog_id}/rewrite", json={"surface": "nonsense"})
    assert r.status_code == 400


# ── living alongside everything else ─────────────────────────────────────────────────────────────
@pytest.mark.parametrize("use,expect", [
    ("imported", "the copy that went live"),
    ("imported_rewritten", "reworded copy"),
    ("", "the body we wrote"),                 # no `use` → the generated blog, as always
])
def test_each_body_exports_on_its_own(dbp, use, expect):
    blog_id = _seed(dbp)
    db = _open(dbp)
    db.update_blog(blog_id, imported_body="# I\n\nThe copy that went live.\n",
                   imported_rewritten="# I\n\nA reworded copy.\n")
    db.close()
    r = _client(dbp).get(f"/api/blogs/{blog_id}/export?format=md" + (f"&use={use}" if use else ""))
    assert r.status_code == 200 and expect in r.data.decode().lower()


def test_an_export_of_an_import_that_does_not_exist_falls_back_to_the_blog(dbp):
    blog_id = _seed(dbp)
    r = _client(dbp).get(f"/api/blogs/{blog_id}/export?format=md&use=imported")
    assert "the body we wrote" in r.data.decode().lower()


def test_a_hand_edit_to_either_body_persists(dbp):
    blog_id = _seed(dbp)
    c = _client(dbp)
    _upload(c, blog_id, "# X\n\nOriginal upload.\n")
    c.patch(f"/api/blogs/{blog_id}", json={"imported_body": "# X\n\nEdited by hand.\n",
                                           "imported_rewritten": "# X\n\nEdited rewrite.\n"})
    db = _open(dbp)
    try:
        blog = db.get_blog(blog_id)
        assert "Edited by hand" in blog["imported_body"]
        assert "Edited rewrite" in blog["imported_rewritten"]
    finally:
        db.close()


def test_the_list_says_which_blogs_have_one(dbp):
    """FU248's row icons answer "what exists for this blog"; an attached import is one of them."""
    blog_id = _seed(dbp)
    plain = _seed(dbp)
    _upload(_client(dbp), blog_id, "# X\n\nLive copy.\n")
    db = _open(dbp)
    try:
        rows = {r["id"]: r for r in db.get_all_blogs()}
        assert rows[blog_id]["has_import"] == 1 and rows[plain]["has_import"] == 0
        assert [r["id"] for r in db.get_all_blogs(has="import_version")] == [blog_id]
    finally:
        db.close()


def test_an_attached_import_is_not_the_same_as_an_imported_blog(dbp):
    """Two different questions: this blog CAME from a file, versus this blog HAS a file attached."""
    attached = _seed(dbp)
    _upload(_client(dbp), attached, "# X\n\nLive copy.\n")
    db = _open(dbp)
    try:
        sub = db.ensure_live_subreddit("t")
        whole = db.save_blog(db.add_brand(sub["id"], "Beta"), "s", title="Wholly imported",
                             body_markdown="# W\n\nFrom a file.\n", prompt_version="imported")
        assert [r["id"] for r in db.get_all_blogs(has="imported")] == [whole]
        assert [r["id"] for r in db.get_all_blogs(has="import_version")] == [attached]
    finally:
        db.close()
