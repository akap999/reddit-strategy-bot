"""FU205 R1 — ONE guard composition, every write path.

The audit's first structural finding: `_finalize_article` is the only place the deterministic guards
are composed, and only 2 of 11 blog write paths reach it. `PATCH /api/blogs/<id>` — the most-used
write path — had ZERO guards. `regenerate part=article` / `part=verify` ran BOTH body-rewriting LLM
calls and then skipped `_restore_dropped_sections`, the guard that exists because those two rewrites
delete sections. Blog import and the FU79 paused body went in unguarded.

These tests prove the fix at the level it was made: the two DB write choke points, so every current
AND future path is covered by construction rather than by remembering to call the guards.
"""
import os
import tempfile

import pytest

from db import Database
from generators.body_sanitize import (MARKDOWN_FIELDS, PROSE_FIELDS, sanitize_stored_body)

# The exact defect set 204 rounds removed, in one body.
DIRTY = (
    "# Which agency?\n"
    "*[Add author byline before publishing]*\n"
    "Acme costs $29:\n"
    "-fast\n"
    "-cheap\n"
    "Some text\n"
    "---\n"
    "A line — with “curly” quotes…\n"
)


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _seed(path, **cols):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(bid, "which agency", title="Which agency?", **cols)
    db.close()
    return blog_id


# ── the choke points ──────────────────────────────────────────────────────────────────────────
def test_a_hand_edit_through_update_blog_comes_back_clean():
    """This is the PATCH path — the most-used write path, and the one that had no guards at all."""
    path = _tmp_db()
    try:
        bid = _seed(path)
        db = Database(path)
        db.connect()
        db.update_blog(bid, body_markdown=DIRTY)
        body = db.get_blog(bid)["body_markdown"]
        db.close()
        assert "\n- fast\n" in body, "a marker with no space rendered as a paragraph, not a list"
        assert "\n\n- fast" in body, "a list directly under a paragraph rendered as one run-on <p>"
        assert "\n\n---" in body, "a --- under a paragraph is a setext H2, not a rule"
        assert "—" not in body.split("## Sources")[0], "em-dash survived (FU185)"
        assert "“" not in body and "”" not in body and "…" not in body, "curly punctuation survived"
    finally:
        os.unlink(path)


def test_an_imported_body_is_scrubbed_on_the_way_in():
    """Blog import (FU180) writes through save_blog — a Word doc is near-certain to carry curly
    quotes and em-dashes, and it entered completely unguarded before R1."""
    path = _tmp_db()
    try:
        bid = _seed(path, body_markdown=DIRTY)
        db = Database(path)
        db.connect()
        body = db.get_blog(bid)["body_markdown"]
        db.close()
        assert "—" not in body and "“" not in body
        assert "\n- fast\n" in body
    finally:
        os.unlink(path)


def test_the_guards_are_idempotent():
    """Generation already finalises; the DB layer then runs the same guards. Double-application must
    be a no-op, or every save would churn the body."""
    once = sanitize_stored_body("body_markdown", DIRTY)[0]
    twice = sanitize_stored_body("body_markdown", once)[0]
    assert twice == once
    assert sanitize_stored_body("body_markdown", once)[1] is False, "a clean body reports no change"


# ── per-field dispatch: the guards must not restructure a surface that isn't an article ───────
@pytest.mark.parametrize("field", sorted(PROSE_FIELDS))
def test_a_prose_surface_is_not_markdown_scrubbed(field):
    """A LinkedIn post is plain text and YouTube captions are a transcript. They get the two
    universal guarantees (no AI symbols, no invisible chars) and NO restructuring — inserting list
    blank lines into a caption track would be a change with no defect to justify it."""
    src = "line one\n-not a list\nline two"
    out, _ = sanitize_stored_body(field, src)
    assert out.count("\n") == src.count("\n"), "prose was restructured"
    assert "-not a list" in out, "a prose dash was turned into a list marker"


def test_a_prose_surface_still_loses_its_ai_symbols_and_invisible_chars():
    out, changed = sanitize_stored_body("linkedin_text", "we ship fast ​— really")
    assert changed and "​" not in out and "—" not in out


def test_the_title_is_never_touched():
    """FU88 pins the title to the operator's seed verbatim and pins the H1 to the same string.
    Scrubbing the title would break that equality — so `title` is deliberately not sanitised."""
    seed = "best tools — 2026 “edition”"
    assert sanitize_stored_body("title", seed) == (seed, False)
    path = _tmp_db()
    try:
        bid = _seed(path)
        db = Database(path)
        db.connect()
        db.update_blog(bid, title=seed)
        assert db.get_blog(bid)["title"] == seed
        db.close()
    finally:
        os.unlink(path)


def test_meta_fields_are_scrubbed_exactly_as_finalize_article_already_did():
    out, changed = sanitize_stored_body("meta_description", "the best — really “good”")
    assert changed and "—" not in out and "“" not in out


# ── the boundary: this module REPAIRS text, it never DECIDES what content deserves to exist ───
def test_a_comparison_table_is_never_deleted_by_a_save():
    """`_resolve_table_punts` drops a column — and under the FU204 collapse guard the whole table —
    when cells are unfilled. That is a SOURCING judgement made at generation time about evidence
    that could not be found. Re-running it on every save would silently delete a table an operator
    hand-wrote, so it is deliberately excluded from the sanitiser."""
    body = ("intro\n\n| Firm | Rating |\n| --- | --- |\n| A | Verify on their site |\n"
            "| B | Verify on their site |\n\n## Next\n")
    out, _ = sanitize_stored_body("body_markdown", body)
    assert "| Firm | Rating |" in out, "the table was deleted by a save"
    assert "| A |" in out and "| B |" in out, "a row was deleted by a save"
    # the punt itself is still resolved — a repair, not a deletion
    assert "Verify on their site" not in out
    assert "| — |" in out or "| —" in out


def test_citations_are_never_renumbered_or_dropped_at_save_time():
    """The `[S#]` renumber and the authoritative ## Sources rebuild need the evidence map, which
    exists only during a generation. Running them here would DELETE citations, not fix them."""
    body = "A claim [S3]. Another [S7].\n\n## Sources\n\n- [S3] Acme — https://acme.example\n"
    out, _ = sanitize_stored_body("body_markdown", body)
    assert "[S3]" in out and "[S7]" in out, "a marker was renumbered or dropped at save time"
    assert "https://acme.example" in out, "the Sources list was rebuilt from an empty evidence map"


def test_a_grouped_citation_is_split_so_a_later_renumber_can_see_it():
    out, _ = sanitize_stored_body("body_markdown", "A claim [S1, S2, S3].\n")
    assert "[S1][S2][S3]" in out


# ── failure is never fatal ────────────────────────────────────────────────────────────────────
def test_a_broken_sanitizer_never_breaks_a_save(monkeypatch):
    import generators.body_sanitize as bs

    def _boom():
        raise RuntimeError("guards unavailable")
    monkeypatch.setattr(bs, "_generator", _boom)
    assert bs.sanitize_stored_body("body_markdown", DIRTY) == (DIRTY, False)
    path = _tmp_db()
    try:
        bid = _seed(path)
        db = Database(path)
        db.connect()
        db.update_blog(bid, body_markdown=DIRTY)   # must not raise
        assert db.get_blog(bid)["body_markdown"] == DIRTY
        db.close()
    finally:
        os.unlink(path)


def test_non_string_and_unknown_values_pass_straight_through():
    assert sanitize_stored_body("body_markdown", None) == (None, False)
    assert sanitize_stored_body("gen_cost", 1.5) == (1.5, False)
    assert sanitize_stored_body("keywords", '["a"]') == ('["a"]', False)
    assert "body_markdown" in MARKDOWN_FIELDS and "keywords" not in MARKDOWN_FIELDS
