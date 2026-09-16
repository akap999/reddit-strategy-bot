"""FU205 R1 — the two write paths that hand-rolled a DIFFERENT subset of the guards.

`regenerate part=article` and `part=verify` run BOTH body-rewriting LLM calls (`verify_claims` and
the reconcile inside `verify_and_complete`) and then called `_rebuild_sources` alone — skipping
`_restore_dropped_sections`, the guard that exists BECAUSE those two rewrites delete whole sections.
A section dropped by the reconcile was restored on the main path and lost permanently here.

Also covers the Markdown export, the one body export that did not scrub at render.
"""
import os
import tempfile

from db import Database
from generators.blog_gen import BlogGenerator, scrub_markdown_formatting
from tests.stubs import StubClaude

DRAFT = (
    "# Which agency?\n\n"
    "## Quick answer\n\nAcme is a strong fit.\n\n"
    "## Safety and compliance\n\nEvery engagement is reviewed against the state rules.\n\n"
    "## Pricing\n\nAcme costs $29 per month.\n"
)
# what the reconcile handed back — the safety section is simply gone
RECONCILED = (
    "# Which agency?\n\n"
    "## Quick answer\n\nAcme is a strong fit.\n\n"
    "## Pricing\n\nAcme costs $29 per month.\n"
)


def _gen():
    gen = BlogGenerator(StubClaude(), db=None)
    gen._evidence_blocks = []
    return gen


def test_the_substance_guard_restores_a_section_the_reconcile_dropped():
    """The Part-2 demonstration, inverted: this is what `part=article` used to lose."""
    gen = _gen()
    art = {"title": "Which agency?", "meta_description": "", "body_markdown": RECONCILED}
    gen._finalize_article({"name": "Acme"}, "which agency", art, DRAFT, with_linkedin=False)
    assert "## Safety and compliance" in art["body_markdown"], "the dropped section was not restored"
    assert "state rules" in art["body_markdown"], "the section came back empty"


def test_finalize_with_linkedin_off_skips_the_adaptation_and_nothing_else():
    """`part=article` must not silently regenerate a surface the operator did not ask for
    (`part=linkedin` exists for that), and must not pay for the extra call — while still running
    every guard and every check."""
    calls = []
    gen = _gen()
    gen.generate_linkedin = lambda *a, **k: calls.append(1) or "post"
    art = {"title": "Which agency?", "meta_description": "a — b", "body_markdown": RECONCILED}
    gen._finalize_article({"name": "Acme"}, "which agency", art, DRAFT, with_linkedin=False)
    assert not calls, "the LinkedIn adaptation ran on a partial regenerate"
    assert "linkedin_text" not in art
    # the rest of the composition still ran
    assert "## Safety and compliance" in art["body_markdown"]      # substance guard
    assert "—" not in art["meta_description"]                       # FU185 meta scrub
    assert "quality_report" in art                                  # FU151 scorecard

    calls.clear()
    gen2 = _gen()
    gen2.generate_linkedin = lambda *a, **k: calls.append(1) or "post"
    art2 = {"title": "Which agency?", "meta_description": "", "body_markdown": RECONCILED}
    gen2._finalize_article({"name": "Acme"}, "which agency", art2, DRAFT)
    assert calls == [1], "the default must stay byte-identical to today (LinkedIn regenerated)"
    assert art2.get("linkedin_text") == "post"


def test_the_h1_is_still_pinned_to_the_seed_on_the_partial_path():
    """FU88 must hold wherever the composition runs."""
    gen = _gen()
    art = {"title": "x", "meta_description": "", "body_markdown": "# Reworded heading\n\nBody.\n"}
    gen._finalize_article({"name": "Acme"}, "which agency", art, "# Reworded heading\n\nBody.\n",
                          with_linkedin=False)
    assert art["body_markdown"].lstrip().startswith("# which agency")


# ── the Markdown export ───────────────────────────────────────────────────────────────────────
def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_the_md_export_carries_the_same_source_the_html_export_renders(monkeypatch):
    """format=md was the ONE body export that did not scrub at render: an old row written before the
    DB guards landed rendered clean as HTML and shipped raw as .md."""
    import app as appmod
    path = _tmp_db()
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "which agency", title="Which agency?",
                               body_markdown="# Which agency?\n\nok\n")
        # write a PRE-R1 row straight past the sanitiser, the way an old row sits in production
        db.conn.execute("UPDATE blogs SET body_markdown = ? WHERE id = ?",
                        ("# Which agency?\n\nAcme costs $29:\n-fast\n-cheap\n", blog_id))
        db.conn.commit()
        db.close()
        appmod.DB_PATH = path
        appmod._db_initialized = True
        cli = appmod.app.test_client()
        md = cli.get(f"/api/blogs/{blog_id}/export?format=md").get_data(as_text=True)
        assert "\n- fast" in md, "the .md export still ships the raw pre-R1 body"
        # the html path renders from scrub_markdown_formatting(body) — same source, same result
        assert md == scrub_markdown_formatting(
            "# Which agency?\n\nAcme costs $29:\n-fast\n-cheap\n")[0]
    finally:
        os.unlink(path)


def test_the_rewrite_and_verify_endpoints_get_the_invisible_char_strip_for_free():
    """Both write through `db.update_blog`, so R1's choke-point fix covers them without either
    endpoint gaining a call of its own — which is the entire point of fixing it at the choke point."""
    path = _tmp_db()
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "s")
        for col in ("rewritten_body", "linkedin_rewritten", "linkedin_article_rewritten",
                    "body_markdown"):
            db.update_blog(blog_id, **{col: "clean​ body⁠here"})
            assert "​" not in (db.get_blog(blog_id)[col] or ""), col
            assert "⁠" not in (db.get_blog(blog_id)[col] or ""), col
        db.close()
    finally:
        os.unlink(path)


def test_a_finalized_body_round_trips_through_the_db_guards_byte_identical():
    """THE safety property of R1. The DB guards run on EVERY write, including the ones that follow a
    normal generation. If they changed a body that `_finalize_article` had already produced, every
    save would churn production text. They must be a pure no-op there, and only act on the bodies
    that bypassed generation (a hand edit, an import, a paused draft, a partial regenerate)."""
    draft = (
        "# which agency\n\n"
        "*[Add author byline before publishing]*\n\n"
        "## Quick answer\n\nAcme is a strong fit [S1].\n\n"
        "## What does it cost?\n\nAcme costs $29 per month [S1].\n\n"
        "| Firm | Pricing | Source |\n| --- | --- | --- |\n"
        "| Acme | $29/mo | [S1] |\n| Bravo | $49/mo | [S2] |\n\n"
        "## FAQ\n\n### Is it worth it?\n\nFor small teams, yes.\n"
    )
    gen = _gen()
    gen._evidence_blocks = [
        {"label": "Acme", "url": "https://acme.example/pricing", "text": "p"},
        {"label": "third-party · Review", "url": "https://rev.example/bravo", "text": "p"},
    ]
    art = {"title": "which agency", "meta_description": "Acme costs $29 per month.",
           "body_markdown": draft}
    gen._finalize_article({"name": "Acme", "domain_url": "https://acme.example"},
                          "which agency", art, draft, with_linkedin=False)
    finished = art["body_markdown"]

    path = _tmp_db()
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "which agency", body_markdown=finished,
                               meta_description=art["meta_description"])
        stored = db.get_blog(blog_id)["body_markdown"]
        db.close()
    finally:
        os.unlink(path)

    assert stored == finished, "the DB guards changed an already-finalized body"
    assert "*[Add author byline before publishing]*" in stored   # FU152/FU204
    assert "## Sources" in stored and "[S1]" in stored           # citations intact
    assert stored.count("\n|") == 4                              # the table survived intact
