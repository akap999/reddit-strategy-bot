"""FU182 — upload the WATERMARK-FREE version of a blog (or either LinkedIn surface) to Google Drive.

The Drive bridge already carried `variant`; FU179 taught the two LinkedIn variants to honour
`use='rewritten'`, but the BLOG — the surface actually handed to a client — still uploaded Claude's
body, so its watermark-free version could not reach Drive at all. The doc is now NAMED
"… (watermark-free)" when a rewrite was really sent, so two uploads of the same blog are
distinguishable instead of colliding on one filename. $0, no network (the Apps Script POST is stubbed).
"""
import json
import os
import tempfile

import pytest

from db import Database

CLAUDE_BODY = "# Which agency?\n\nClaude wrote this body with $10,000 claims.\n"
REWRITTEN = "# Which agency?\n\nQwen reworded it; claims still reach $10,000.\n"
LI_POST, LI_POST_RW = "A LinkedIn post by Claude.", "A reworded LinkedIn post by Qwen."
LI_ART, LI_ART_RW = "## Claude article body", "## Qwen reworded article body"


@pytest.fixture()
def env(monkeypatch):
    """A temp DB with a configured Drive bridge, a seeded blog, and the Apps Script POST captured."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    brand_id = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(brand_id, "which agency", title="Which Agency?",
                           body_markdown=CLAUDE_BODY, linkedin_text=LI_POST)
    db.update_blog(blog_id, linkedin_article=LI_ART, linkedin_article_title="An Article")
    db.meta_set("gdoc_script_url", "https://script.example/exec")
    db.meta_set("gdoc_secret", "s3cret")
    db.close()

    import app as appmod
    appmod.DB_PATH = path
    appmod._db_initialized = True
    sent = {}

    class _Resp:
        status_code = 200
        text = '{"ok":true,"url":"https://docs.google.com/document/d/abc"}'

        def json(self):
            return {"ok": True, "url": "https://docs.google.com/document/d/abc"}

    def _fake_post(url, json=None, timeout=None, **kw):
        sent.clear()
        sent.update(json or {})
        return _Resp()

    import requests
    monkeypatch.setattr(requests, "post", _fake_post)
    yield appmod.app.test_client(), blog_id, path, sent
    os.unlink(path)


def _upload(client, blog_id, **body):
    r = client.post(f"/api/blogs/{blog_id}/upload-gdoc", json=body)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _set(path, blog_id, **cols):
    db = Database(path)
    db.connect()
    db.update_blog(blog_id, **cols)
    db.close()


# ── the blog: the gap this closes ────────────────────────────────────────────────────────────────
def test_blog_uploads_the_rewritten_body_when_asked(env):
    client, blog_id, path, sent = env
    _set(path, blog_id, rewritten_body=REWRITTEN)
    _upload(client, blog_id, use="rewritten")
    assert "Qwen reworded it" in sent["html"], "the watermark-free body must be what reaches Drive"
    assert "Claude wrote this body" not in sent["html"]


def test_the_drive_doc_is_named_so_the_two_versions_are_distinguishable(env):
    client, blog_id, path, sent = env
    _set(path, blog_id, rewritten_body=REWRITTEN)
    _upload(client, blog_id, use="rewritten")
    assert sent["title"] == "Which Agency? (watermark-free)"
    _upload(client, blog_id)                       # the original, same blog
    assert sent["title"] == "Which Agency?"


def test_without_use_the_original_still_uploads(env):
    """The existing button must be untouched."""
    client, blog_id, path, sent = env
    _set(path, blog_id, rewritten_body=REWRITTEN)
    _upload(client, blog_id)
    assert "Claude wrote this body" in sent["html"] and "Qwen reworded" not in sent["html"]


def test_asking_for_a_rewrite_that_does_not_exist_falls_back_and_is_not_mislabelled(env):
    """The dangerous case: silently uploading Claude's body under a 'watermark-free' name."""
    client, blog_id, path, sent = env          # no rewritten_body set
    _upload(client, blog_id, use="rewritten")
    assert "Claude wrote this body" in sent["html"]
    assert sent["title"] == "Which Agency?", "must NOT claim to be watermark-free"


# ── the two LinkedIn surfaces (FU179 wired the body; FU182 adds the label) ────────────────────────
def test_linkedin_post_and_article_upload_their_rewrites(env):
    client, blog_id, path, sent = env
    _set(path, blog_id, linkedin_rewritten=LI_POST_RW, linkedin_article_rewritten=LI_ART_RW)

    _upload(client, blog_id, variant="linkedin_post", use="rewritten")
    assert "reworded LinkedIn post by Qwen" in sent["html"]
    assert sent["title"].endswith("(watermark-free)")

    _upload(client, blog_id, variant="linkedin_article", use="rewritten")
    assert "Qwen reworded article body" in sent["html"]
    assert sent["title"] == "An Article (watermark-free)"


def test_linkedin_without_use_is_unchanged(env):
    client, blog_id, path, sent = env
    _set(path, blog_id, linkedin_rewritten=LI_POST_RW)
    _upload(client, blog_id, variant="linkedin_post")
    assert "LinkedIn post by Claude" in sent["html"]
    assert "(watermark-free)" not in sent["title"]


def test_drive_upload_still_requires_configuration(env):
    client, blog_id, path, _sent = env
    db = Database(path)
    db.connect()
    db.meta_set("gdoc_script_url", "")
    db.close()
    r = client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={"use": "rewritten"})
    assert r.status_code == 400 and r.get_json()["error"] == "not_configured"
