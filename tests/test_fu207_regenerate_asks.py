"""FU207 — regenerating a blog asks, exactly like the first Generate.

Only the first Generate could pause. "Regen all", "Regen article" and "Re-verify" handed an empty
competitor straight to the reconcile, and measured: at best its row went while its FAQ entry and prose
mentions stayed with no warning; at worst its blank row deleted the whole comparison table.

The operator asked to be asked. The promise that makes a regenerate pause safe is that the EXISTING
blog stays exactly as it is until they answer — a published blog keeps serving its finished version
while the question waits. These tests hold the endpoints to that, byte for byte.
"""
import json
import os
import tempfile

import pytest

from db import Database
from generators.blog_gen import BlogGenerator as BG
from tests.stubs import StubClaude

LIVE_BODY = "# which agency\n\n*[Add author byline before publishing]*\n\nThe finished, published body.\n"
DRAFT = ("# which agency\n\n*[Add author byline before publishing]*\n\n## Quick answer\n\nAcme.\n\n"
         "| Firm | Price |\n| --- | --- |\n| Acme | $1 |\n| Bravo | $2 |\n| Delta | $3 |\n"
         "| Echo | $4 |\n| Zeta |  |\n")
SOURCING = {"name": "Acme", "cat": "c", "tools": ["Bravo", "Delta", "Echo", "Zeta"], "peers": [],
            "dims": ["Price"], "claims": [], "fresh": [{"label": "Bravo", "url": "u", "text": "t"}],
            "unsourced": [{"tool": "Zeta", "facts": ["Price"], "remaining_if_removed": 3}]}


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _seed(path, status="published", pending=None):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(bid, "which agency", title="Which agency?",
                           meta_description="The live meta description.",
                           body_markdown=LIVE_BODY, linkedin_text="The live LinkedIn post.",
                           status=status, gen_cost=1.25)
    if pending is not None:
        db.update_blog(blog_id, pending_state=pending)
    snap = {k: db.get_blog(blog_id)[k] for k in
            ("title", "meta_description", "body_markdown", "linkedin_text", "status", "gen_cost")}
    db.close()
    return blog_id, snap


def _row(path, blog_id):
    db = Database(path)
    db.connect()
    try:
        return db.get_blog(blog_id)
    finally:
        db.close()


@pytest.fixture
def app_client(monkeypatch):
    import app as appmod
    box = {}

    def _inline(_name, fn, **kw):
        box["result"] = fn(_task_id="t") if kw.get("pass_task_id") else fn()
        return "t"
    monkeypatch.setattr(appmod, "start_task", _inline)
    monkeypatch.setattr(appmod, "ClaudeClient", lambda *a, **k: StubClaude())
    monkeypatch.setattr(appmod, "_ensure_brand_byline_logo", lambda c, db, b: b)
    monkeypatch.setattr(appmod, "_build_blog_writer", lambda db: (None, "off"))
    monkeypatch.setattr(appmod, "_blog_reddit_evidence", lambda *a, **k: (None, "skipped"))
    monkeypatch.setattr(BG, "_gather_evidence", lambda self, *a, **k: "")
    monkeypatch.setattr(BG, "_build_link_targets", lambda self, *a, **k: [])
    # the article + verify paths reach the REAL verify_and_complete, which must pause on its own
    monkeypatch.setattr(BG, "generate_article", lambda self, *a, **k: {
        "title": "Which agency (regenerated)?", "meta_description": "New meta.",
        "meta_title": "", "keywords": [], "body_markdown": DRAFT})
    monkeypatch.setattr(BG, "verify_claims", lambda self, b, a, evidence="": {
        "body_markdown": a["body_markdown"], "flagged": []})
    monkeypatch.setattr(BG, "_source_for_completion", lambda self, *a, **k: json.loads(json.dumps(SOURCING)))

    def _gen_blog(self, brand, seed, **kw):
        assert kw.get("allow_pause") is True, "Regen all must be allowed to pause"
        return {"_pending": self._pause_sentinel(json.loads(json.dumps(SOURCING)),
                                                 {"title": "t", "body_markdown": DRAFT}, DRAFT)}
    monkeypatch.setattr(BG, "generate_blog", _gen_blog)

    def _client(path):
        appmod.DB_PATH = path
        appmod._db_initialized = True
        return appmod.app.test_client()
    return _client, box


# ── the promise ───────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("part", ["all", "article", "verify"])
def test_a_paused_regenerate_leaves_the_blog_exactly_as_it_was(app_client, part):
    make, box = app_client
    path = _tmp()
    try:
        blog_id, before = _seed(path)
        cli = make(path)
        assert cli.post(f"/api/blogs/{blog_id}/regenerate", json={"part": part}).status_code == 200
        res = box["result"]
        assert res["needs_sources"] and res["regenerate"] and res["part"] == part
        assert [m["tool"] for m in res["missing"]] == ["Zeta"]
        after = _row(path, blog_id)
        for k, v in before.items():
            assert after[k] == v, f"a paused {part} changed the live blog's {k}"
        assert after["pending_state"]["mode"] == "regenerate"
        assert after["pending_state"]["part"] == part
    finally:
        os.unlink(path)


def test_the_list_can_find_a_paused_regenerate_without_changing_status(app_client):
    make, box = app_client
    path = _tmp()
    try:
        blog_id, _ = _seed(path, status="published")
        make(path).post(f"/api/blogs/{blog_id}/regenerate", json={"part": "all"})
        db = Database(path)
        db.connect()
        row = next(b for b in db.get_all_blogs() if b["id"] == blog_id)
        db.close()
        assert row["status"] == "published", "a pause un-published the blog"
        assert row["regen_pending"] == 1
    finally:
        os.unlink(path)


# ── answering ─────────────────────────────────────────────────────────────────────────────────
def _answer(make, box, path, blog_id, part, monkeypatch, art):
    monkeypatch.setattr(BG, "finish_pending_blog", lambda self, b, s, ck, p: dict(art))
    make(path).post(f"/api/blogs/{blog_id}/provide-sources",
                    json={"sources": [{"tool": "Zeta", "remove": True}]})
    return box["result"], _row(path, blog_id)


FINISHED = {"title": "Which agency (regenerated)?", "meta_description": "New meta.",
            "meta_title": "New title tag", "keywords": ["k"],
            "body_markdown": "# which agency\n\n*[Add author byline before publishing]*\n\nRegenerated.\n",
            "linkedin_text": "A regenerated LinkedIn post.", "claims_flagged": [],
            "prompt_version": "v", "quality_report": {"score": 90, "checks": [], "warnings": []}}


def test_answering_regen_all_writes_everything_and_keeps_it_published(app_client, monkeypatch):
    make, box = app_client
    path = _tmp()
    try:
        blog_id, _ = _seed(path, status="published")
        make(path).post(f"/api/blogs/{blog_id}/regenerate", json={"part": "all"})
        res, row = _answer(make, box, path, blog_id, "all", monkeypatch, FINISHED)
        assert "Regenerated." in row["body_markdown"]
        assert row["title"] == "Which agency (regenerated)?"
        assert row["linkedin_text"] == "A regenerated LinkedIn post."
        assert row["status"] == "published", "answering un-published the blog"
        assert row["pending_state"] == {}
        assert res["regenerate"] and res["part"] == "all"
    finally:
        os.unlink(path)


def test_answering_regen_article_never_touches_the_linkedin_post(app_client, monkeypatch):
    """'Regen article' never wrote the LinkedIn post. Its resume must not either."""
    make, box = app_client
    path = _tmp()
    try:
        blog_id, before = _seed(path)
        make(path).post(f"/api/blogs/{blog_id}/regenerate", json={"part": "article"})
        _, row = _answer(make, box, path, blog_id, "article", monkeypatch, FINISHED)
        assert "Regenerated." in row["body_markdown"]
        assert row["title"] == "Which agency (regenerated)?"
        assert row["linkedin_text"] == before["linkedin_text"]
    finally:
        os.unlink(path)


def test_answering_reverify_changes_only_the_body(app_client, monkeypatch):
    """'Re-verify' never changed the title, meta or LinkedIn post."""
    make, box = app_client
    path = _tmp()
    try:
        blog_id, before = _seed(path)
        make(path).post(f"/api/blogs/{blog_id}/regenerate", json={"part": "verify"})
        _, row = _answer(make, box, path, blog_id, "verify", monkeypatch, FINISHED)
        assert "Regenerated." in row["body_markdown"]
        for k in ("title", "meta_description", "linkedin_text"):
            assert row[k] == before[k], f"Re-verify's resume changed {k}"
    finally:
        os.unlink(path)


def test_reverify_resume_scores_the_live_meta_not_a_blank_one(app_client, monkeypatch):
    """Re-verify's checkpoint never carried the title or meta. The resume fills them from the live
    row, or the scorecard would mark the meta description missing on a blog that has one."""
    make, box = app_client
    path = _tmp()
    try:
        blog_id, _ = _seed(path)
        make(path).post(f"/api/blogs/{blog_id}/regenerate", json={"part": "verify"})
        seen = {}
        monkeypatch.setattr(BG, "finish_pending_blog",
                            lambda self, b, s, ck, p: seen.update(article=dict(ck["article"])) or dict(FINISHED))
        make(path).post(f"/api/blogs/{blog_id}/provide-sources", json={"sources": []})
        assert seen["article"]["meta_description"] == "The live meta description."
    finally:
        os.unlink(path)


def test_the_resume_reports_what_the_regenerate_cost(app_client, monkeypatch):
    make, box = app_client
    path = _tmp()
    try:
        blog_id, _ = _seed(path)
        db = Database(path)
        db.connect()
        db.update_blog(blog_id, pending_state={"mode": "regenerate", "part": "all", "gen_cost": 0.4,
                                               "missing": [{"tool": "Zeta"}],
                                               "checkpoint": {"sourcing": {}, "article": {}}})
        db.close()
        res, row = _answer(make, box, path, blog_id, "all", monkeypatch, FINISHED)
        assert res["gen_cost"] == 0.4, "the blog's ORIGINAL cost was added to the regenerate's"
    finally:
        os.unlink(path)


# ── walking away ──────────────────────────────────────────────────────────────────────────────
def test_cancelling_a_paused_regenerate_keeps_the_blog(app_client):
    make, box = app_client
    path = _tmp()
    try:
        blog_id, before = _seed(path)
        cli = make(path)
        cli.post(f"/api/blogs/{blog_id}/regenerate", json={"part": "all"})
        r = cli.post(f"/api/blogs/{blog_id}/discard-pending")
        assert r.get_json() == {"ok": True, "discarded": True}
        row = _row(path, blog_id)
        assert row["pending_state"] == {}
        for k, v in before.items():
            assert row[k] == v
    finally:
        os.unlink(path)


def test_a_first_generation_pause_cannot_be_cancelled(app_client):
    """That blog has no finished version to fall back to."""
    make, box = app_client
    path = _tmp()
    try:
        blog_id, _ = _seed(path, status="awaiting_sources",
                           pending={"missing": [{"tool": "Zeta"}], "checkpoint": {"article": {}}})
        r = make(path).post(f"/api/blogs/{blog_id}/discard-pending")
        assert r.status_code == 400
    finally:
        os.unlink(path)


def test_a_regenerate_that_completes_clears_a_stale_pause(app_client, monkeypatch):
    """Left in place, the banner would keep asking — and answering it would overwrite the fresh
    result with the stale checkpoint's."""
    make, box = app_client
    path = _tmp()
    try:
        blog_id, _ = _seed(path, status="awaiting_sources",
                           pending={"missing": [{"tool": "Zeta"}], "checkpoint": {"article": {}}})
        monkeypatch.setattr(BG, "generate_blog", lambda self, *a, **k: dict(FINISHED))
        make(path).post(f"/api/blogs/{blog_id}/regenerate", json={"part": "all"})
        row = _row(path, blog_id)
        assert row["pending_state"] == {}
        assert row["status"] == "draft", "a completed regenerate left the blog marked as paused"
    finally:
        os.unlink(path)


# ── the first Generate is untouched ───────────────────────────────────────────────────────────
def test_a_first_generation_checkpoint_carries_no_part():
    gen = BG(StubClaude(), None)
    pend = gen._pause_sentinel(json.loads(json.dumps(SOURCING)), {"title": "t", "body_markdown": DRAFT}, DRAFT)
    assert "part" not in pend["checkpoint"]


@pytest.mark.parametrize("part,expect", [(None, True), ("all", True), ("article", False), ("verify", False)])
def test_only_regen_all_and_generate_rebuild_the_linkedin_post_on_resume(part, expect):
    gen = BG(StubClaude(), None)
    seen = {}
    gen._finalize_article = lambda *a, **k: seen.update(k) or {"body_markdown": ""}
    ck = {"sourcing": {"tools": [], "fresh": [], "unsourced": []}, "article": {}, "draft_body": ""}
    if part:
        ck["part"] = part
    gen.finish_pending_blog({"name": "Acme"}, "s", ck, [])
    assert seen["with_linkedin"] is expect
