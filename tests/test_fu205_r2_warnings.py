"""FU205 R2 — the detection layer's output channel was lossy.

22 deterministic warning checks fed ONE `"; "`-joined string, and `_quality_report` then
`.split(";")` it back apart to count and score. Four of those checks join their OWN sub-hits with
`"; "`, so three real problems were counted as five, docked -25 instead of -15, and the operator was
shown a context-free fragment as though it were a warning in its own right.

Two more findings from the same part of the audit, both closed here:
  * `verify_report` was computed on EVERY generation and thrown away — no column, no writer anywhere.
  * the FU79 resume returned `{blog_id, resumed, gen_cost}` and wrote no scorecard, so every warning
    computed on a resumed blog was invisible. FU204's price-ask routes MORE blogs down that path.
"""
import os
import tempfile

from db import Database
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

# A real citation-check note: FOUR sub-hits, joined the way that check joins them.
MULTI = ("citation-check: \"Borosilicate glass\" names Dr Brown's but cites amazon.com [S5]; "
         "\"clinically proven\" names Tommee Tippee but cites aap.org [S8]; "
         "\"From $16.99\" names Nanobebe but cites tommeetippee.com [S11] "
         "— re-cite to that brand's own source or drop the specific")
GEO = "geo-check: this page targets 'the US' but the body mentions it only 1× — possible doorway output"
PEER = "peer-check: only 1 same-type competitor(s) — add competitors in Edit Brand or regenerate"


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


# ── the count, and the score it drives ────────────────────────────────────────────────────────
def test_three_real_problems_count_as_three_not_five():
    art = {}
    for note in (MULTI, GEO, PEER):
        BlogGenerator._warn(art, note)
    assert len(art["warnings"]) == 3
    # the old channel: re-splitting the joined string finds FIVE, because the citation-check note
    # carries two internal "; " separators of its own
    assert len(art["geo_warning"].split(";")) == 5, "the distortion this fixes"


def test_the_score_is_docked_for_the_real_count():
    gen = BlogGenerator(StubClaude(), None)
    body = "# t\n\n## Quick answer\n\nyes.\n\n## Is it good?\n\nyes.\n\n## FAQ\n\n### Q?\n\nA.\n"
    art = {"title": "t", "meta_description": "d", "body_markdown": body}
    for note in (MULTI, GEO, PEER):
        BlogGenerator._warn(art, note)
    qr = gen._quality_report(art, {"name": "Acme"})
    assert len(qr["warnings"]) == 3, "the scorecard shredded a warning back into fragments"
    assert qr["warnings"][0] == MULTI, "a whole warning, not a fragment"

    # the same article scored the OLD way — string only, no structured list
    old = {k: v for k, v in art.items() if k != "warnings"}
    qr_old = gen._quality_report(old, {"name": "Acme"})
    assert len(qr_old["warnings"]) == 5, "the back-compat path still behaves as it did"
    assert qr["score"] - qr_old["score"] == 10, "3 warnings dock 15, the 5 fragments docked 25"


def test_an_article_dict_without_the_structured_list_still_scores():
    """Back-compat: an older stored report, or a caller that builds the dict by hand."""
    gen = BlogGenerator(StubClaude(), None)
    qr = gen._quality_report({"title": "t", "body_markdown": "# t\n", "geo_warning": "a; b"},
                             {"name": "Acme"})
    assert qr["warnings"] == ["a", "b"]


# ── the display string must not change ────────────────────────────────────────────────────────
def test_the_joined_string_is_exactly_what_it_always_was():
    """Every existing reader — the toast, the task result, the stored scorecard — keeps working."""
    art = {}
    for note in (MULTI, GEO, PEER):
        BlogGenerator._warn(art, note)
    assert art["geo_warning"] == "; ".join([MULTI, GEO, PEER])


def test_an_empty_note_records_nothing():
    art = {}
    BlogGenerator._warn(art, "")
    BlogGenerator._warn(art, None)
    assert "warnings" not in art and "geo_warning" not in art


def test_each_warning_carries_its_own_check_key():
    art = {}
    for note in (MULTI, GEO, "a note with no prefix"):
        BlogGenerator._warn(art, note)
    assert [w["check"] for w in art["warnings"]] == ["citation-check", "geo-check", "check"]


def test_a_warning_with_its_own_toast_is_recorded_but_not_shown_twice():
    """`key_facts_warning` has its own toast. It must reach the scorecard — it never did before —
    without also appearing in the geo toast the operator reads beside it."""
    art = {}
    BlogGenerator._warn(art, "⚠ pricing changed", fold_string=False, key="key-facts")
    assert art["warnings"] == [{"check": "key-facts", "detail": "⚠ pricing changed"}]
    assert "geo_warning" not in art


def test_the_pricing_conflict_alert_reaches_the_scorecard():
    gen = BlogGenerator(StubClaude(), None)
    art = {"title": "t", "meta_description": "d", "body_markdown": "# t\n",
           "key_facts_warning": "⚠ Acme's tirzepatide pricing changed: was $149, now $249"}
    gen._finalize_article({"name": "Acme"}, "t", art, "# t\n", with_linkedin=False)
    assert any(w["check"] == "key-facts" for w in art["warnings"]), "still toast-only"
    assert "pricing changed" not in (art.get("geo_warning") or ""), "shown to the operator twice"
    assert any("pricing changed" in w for w in art["quality_report"]["warnings"])


# ── storage ───────────────────────────────────────────────────────────────────────────────────
def test_verify_report_and_warnings_round_trip_through_the_db():
    """`verify_report` was computed every single run and had nowhere to go: no column, no writer."""
    path = _tmp_db()
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "s")
        assert db.get_blog(blog_id)["verify_report"] == {}
        assert db.get_blog(blog_id)["warnings"] == []
        rep = {"applied": [{"kind": "typo", "problem": "teh"}],
               "skipped": [{"reason": "would drop a price"}],
               "assessment": {"score": 78, "verdict": "solid"}}
        db.update_blog(blog_id, verify_report=rep,
                       warnings=[{"check": "geo-check", "detail": GEO}])
        got = db.get_blog(blog_id)
        assert got["verify_report"] == rep
        assert got["warnings"] == [{"check": "geo-check", "detail": GEO}]
        db.close()
    finally:
        os.unlink(path)


# ── the FU79 resume path ──────────────────────────────────────────────────────────────────────
def test_a_resumed_blog_reports_and_persists_its_warnings(monkeypatch):
    """The resume endpoint returned {blog_id, resumed, gen_cost} and wrote no scorecard, so every
    warning computed on a paused blog was invisible — on exactly the path FU204 made busier."""
    import app as appmod
    from generators.blog_gen import BlogGenerator as BG
    path = _tmp_db()
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "which agency", status="awaiting_sources")
        db.update_blog(blog_id, pending_state={"checkpoint": {"article": {"title": "t"}}})
        db.close()

        appmod.DB_PATH = path
        appmod._db_initialized = True
        box = {}

        def _inline(_name, fn, **kw):
            box["result"] = fn(_task_id="t") if kw.get("pass_task_id") else fn()
            return "t"
        monkeypatch.setattr(appmod, "start_task", _inline)
        monkeypatch.setattr(appmod, "ClaudeClient", lambda *a, **k: StubClaude())
        monkeypatch.setattr(BG, "finish_pending_blog", lambda self, b, s, c, p: {
            "title": "t", "meta_description": "d", "keywords": [], "body_markdown": "# t\n",
            "linkedin_text": "", "claims_flagged": [], "prompt_version": "v",
            "geo_warning": GEO, "warnings": [{"check": "geo-check", "detail": GEO}],
            "quality_report": {"score": 80, "checks": [], "warnings": [GEO]},
            "verify_report": {"applied": [], "skipped": []},
        })
        cli = appmod.app.test_client()
        r = cli.post(f"/api/blogs/{blog_id}/provide-sources", json={"sources": []})
        assert r.status_code == 200
        res = box["result"]
        assert res["geo_warning"] == GEO, "a resumed blog still reports nothing"
        assert res["warnings"] == [{"check": "geo-check", "detail": GEO}]
        assert res["quality_report"]["score"] == 80

        db = Database(path)
        db.connect()
        b = db.get_blog(blog_id)
        db.close()
        assert (b["quality_report"] or {}).get("score") == 80, "the scorecard was not written"
        assert b["warnings"] == [{"check": "geo-check", "detail": GEO}]
    finally:
        os.unlink(path)
