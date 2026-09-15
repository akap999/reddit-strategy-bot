"""FU201 — the final-output verification layer.

Ask 3, deferred since FU197:
    "Add a verification layer which reads the content of final output of the blog (claude version) and
     reading includes contetn, formating, verifying things like price/brand consisteny etc and if there
     are any issues, should fix those. Add a manual verify button for rewritten (qwen) blogs also, which
     will check the same thing using claude but it correct it using qwen"

Operator decisions this round: scope = FORMATTING; **fix formatting, FLAG the rest**. So the automatic
pass is 100% deterministic (no LLM, no cost) — it repairs formatting and reports everything else. The
paid semantic read and every PROSE repair live on the manual button, behind `_verify_repair_gate`.

SUPERSEDED IN PART BY FU202 (see tests/test_fu202.py): the operator then asked for the content read to
run automatically WITH auto-fix on every generation, for the formatting pass to cover spacing, and for
both versions to be kept. What still holds here, unchanged, is the discipline: every formatting rule is
confirmed against python-markdown first, and a defect that renders correctly is never sold as a "fix"
(see test_a_heading_under_a_paragraph_is_a_cosmetic_fix_never_a_render_fix).
"""
import os
import re

import markdown
import pytest

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

BRAND = {"name": "Acme"}


def _g(**kw):
    g = BlogGenerator(StubClaude(**kw), db=None)
    g._evidence_blocks = [{"label": "S1", "url": "https://x.example/a", "text": "t"}]
    return g


def _html(md):
    return markdown.markdown(md, extensions=["tables", "fenced_code"])


# ══ formatting — AUTO-FIXED ══════════════════════════════════════════════════════════════════════
def test_a_list_under_a_paragraph_is_repaired_in_the_stored_body():
    """The real bug this round fixes: `_normalize_md_lists` ran at EXPORT time only, so the STORED
    markdown kept the defect and the .md export / gdoc upload / CMS paste all shipped a run-on
    paragraph."""
    body = "Some text here:\n- one\n- two\n"
    assert "<ul>" not in _html(body), "fixture must actually be broken"
    out, fixes = _g()._verify_format_fix(body)
    assert "<ul>" in _html(out)
    assert any(f["kind"] == "list-spacing" for f in fixes)


def test_a_rule_under_a_paragraph_stays_a_rule():
    """`---` directly under prose is parsed as a setext H2 — it EATS the paragraph into a heading and
    the divider disappears."""
    body = "Some prose\n---\nAfter\n"
    assert "<h2>Some prose</h2>" in _html(body), "fixture must actually be broken"
    out, fixes = _g()._verify_format_fix(body)
    assert "<hr" in _html(out) and "<h2>Some prose</h2>" not in _html(out)
    assert any(f["kind"] == "thematic-break" for f in fixes)


def test_an_unclosed_bold_marker_is_closed():
    out, fixes = _g()._verify_format_fix("This is **unclosed bold\n")
    assert out.count("**") % 2 == 0
    assert "<strong>" in _html(out)
    assert any(f["kind"] == "unclosed-bold" for f in fixes)


def test_runs_of_blank_lines_collapse():
    out, fixes = _g()._verify_format_fix("a\n\n\n\n\nb\n")
    assert "\n\n\n" not in out
    assert any(f["kind"] == "blank-lines" for f in fixes)


def test_bullet_markers_are_normalised_to_the_dominant_one():
    out, _ = _g()._verify_format_fix("- one\n- two\n* three\n")
    assert "* three" not in out and "- three" in out


def test_a_heading_under_a_paragraph_is_a_cosmetic_fix_never_a_render_fix():
    """Verified against python-markdown BOTH ways: a heading glued to the paragraph above it renders
    correctly, so FU202 normalises it only as a labelled COSMETIC tidy — the rendered HTML must be
    identical before and after. A verifier that changes what the page LOOKS like here would be wrong."""
    body = "Some text here\n## A Heading\nmore\n"
    assert "<h2>A Heading</h2>" in _html(body)
    out, fixes = _g()._verify_format_fix(body)
    assert out == "Some text here\n\n## A Heading\n\nmore\n"
    assert _html(out) == _html(body)                      # render-identical: cosmetic, not a fix
    assert [f["kind"] for f in fixes] == ["block-spacing"]
    assert "cosmetic" in fixes[0]["detail"]


def test_the_sources_section_and_code_fences_are_never_touched():
    """`## Sources` carries deliberate em-dash separators written by `_rebuild_sources`, and a source
    title containing '*' would false-positive the unclosed-bold rule."""
    body = ("text\n\n```\n- not a real list\nunclosed ** in code\n```\n\n"
            "## Sources\n- [S1] A *starred* title — https://x.example/a\n")
    out, _ = _g()._verify_format_fix(body)
    assert "- [S1] A *starred* title — https://x.example/a" in out
    assert "unclosed ** in code" in out


def test_the_formatting_pass_is_idempotent():
    body = "Text:\n- one\n\n\n\n\nMore\n---\nEnd **oops\n"
    once, _ = _g()._verify_format_fix(body)
    twice, fixes2 = _g()._verify_format_fix(once)
    assert twice == once and fixes2 == []


# ══ consistency — FLAGGED, never silently changed ════════════════════════════════════════════════
BODY_BAD = ("# T\n\n## Quick answer\nAcme costs $29 a month.\n\n"
            "## Compare\n| Firm | Price |\n| --- | --- |\n| Acme | $79 |\n\n"
            "## Sources\n- [S1] x — https://x.example/a\n")


def _flags(brand=BRAND, **art):
    a = {"meta_description": "", "body_markdown": BODY_BAD}
    a.update(art)
    return _g()._verify_consistency(brand, a)


def test_three_different_prices_for_one_brand_are_flagged():
    """The FU163 failure: meta said $647, the table said $29, the JSON-LD said $79."""
    kinds = _flags(meta_description="Plans from $647.")
    price = [i for i in kinds if i["kind"] == "price"]
    assert price, "a price collision must be reported"
    assert "$647" in price[0]["detail"] and "$29" in price[0]["detail"]


def test_a_price_collision_does_NOT_edit_the_body():
    """'Flag the rest' — the operator decides which price is right, not the tool."""
    art = {"meta_description": "Plans from $647.", "body_markdown": BODY_BAD}
    g = _g()
    g._verify_consistency(BRAND, art)
    assert art["body_markdown"] == BODY_BAD


def test_inconsistent_brand_casing_is_flagged_but_not_corrected():
    """FU84 ("Outsail" vs "OutSail"). Not auto-corrected: a quoted source title may legitimately differ."""
    art = {"meta_description": "", "body_markdown": "# T\n\nOutsail and OutSail both appear.\n"}
    out = _g()._verify_consistency({"name": "OutSail"}, art)
    brand = [i for i in out if i["kind"] == "brand"]
    assert brand and "Outsail" in brand[0]["detail"]
    assert "Outsail and OutSail" in art["body_markdown"]


def test_a_citation_pointing_at_a_missing_source_is_flagged():
    art = {"meta_description": "", "body_markdown": "# T\n\nA claim [S9].\n"}
    assert any(i["kind"] == "citation" for i in _g()._verify_consistency(BRAND, art))


def test_a_ragged_table_row_is_reported_not_reshaped():
    """A missing CELL needs a fact, not a formatting fix."""
    art = {"meta_description": "",
           "body_markdown": "# T\n\n| A | B |\n| --- | --- |\n| 1 | 2 |\n| 1 | 2 | 3 |\n"}
    assert any(i["kind"] == "table" for i in _g()._verify_consistency(BRAND, art))


def test_an_empty_section_is_flagged_but_normal_nesting_is_not():
    """`# Title` → `## Quick answer` is this pipeline's normal shape and must never be flagged."""
    normal = {"meta_description": "", "body_markdown": "# T\n\n## A\ntext\n"}
    assert not [i for i in _g()._verify_consistency(BRAND, normal) if i["kind"] == "structure"]
    empty = {"meta_description": "", "body_markdown": "# T\n\n## A\n\n## B\ntext\n"}
    assert [i for i in _g()._verify_consistency(BRAND, empty) if i["kind"] == "structure"]


def test_a_clean_article_reports_nothing():
    art = {"meta_description": "A guide.",
           "body_markdown": "# T\n\n## A\n\ntext [S1]\n\n## Sources\n- [S1] x — https://x.example/a\n"}
    g = _g()
    assert g._verify_consistency(BRAND, art) == []
    out, fixes = g._verify_format_fix(art["body_markdown"])
    assert out == art["body_markdown"] and fixes == []


# ══ the repair gates — the half that can do damage ═══════════════════════════════════════════════
GBODY = ("# The Title\n\n*Reviewed by Jane*\n\nAcme costs $29 a month [S1].\n\n"
         "A plain sentence here.\n\n## Sources\n- [S1] x — https://x.example/a\n")
SENT = "Acme costs $29 a month [S1]."


@pytest.mark.parametrize("label,quote,fix", [
    ("banned not-found wording", SENT, "Acme pricing is not found in public sources [S1]."),
    ("adds a citation",          SENT, "Acme costs $29 a month [S1][S2]."),
    ("drops the price",          SENT, "Acme is competitively priced [S1]."),
    ("reintroduces an em-dash",  SENT, "Acme costs $29 a month — billed monthly [S1]."),
    ("edits the H1",             "# The Title", "# A Better Title"),
    ("edits the byline",         "*Reviewed by Jane*", "*Reviewed by Dr Jane*"),
    ("edits ## Sources",         "- [S1] x — https://x.example/a", "- [S1] y — https://y.example/b"),
])
def test_a_repair_that_would_break_a_guarantee_is_refused(label, quote, fix):
    why = _g()._verify_repair_gate(GBODY, quote, fix, BRAND)
    assert why, f"{label} must be refused"


def test_a_legitimate_repair_is_allowed_and_applied():
    g = _g()
    assert g._verify_repair_gate(GBODY, "A plain sentence here.", "A clearer sentence here.", BRAND) == ""
    body, applied, skipped = g._verify_apply_repairs(
        GBODY, [{"kind": "readability", "problem": "unclear",
                 "quote": "A plain sentence here.", "fix": "A clearer sentence here."}], BRAND)
    assert "A clearer sentence here." in body and len(applied) == 1 and not skipped


def test_a_refused_repair_leaves_the_original_and_is_reported():
    """Never silent: the operator sees what it could NOT fix."""
    g = _g()
    body, applied, skipped = g._verify_apply_repairs(
        GBODY, [{"kind": "price", "problem": "x", "quote": SENT,
                 "fix": "Acme pricing is not found in public sources [S1]."}], BRAND)
    assert body == GBODY and not applied
    assert len(skipped) == 1 and "banned" in skipped[0]["reason"]


# ══ the inert proof — what protects every existing suite ═════════════════════════════════════════
def test_the_whole_pass_is_inert_when_disabled(monkeypatch):
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "0")
    art = {"meta_description": "", "body_markdown": "Text:\n- a\n- b\n"}
    before = art["body_markdown"]
    assert _g()._verify_final_article(BRAND, art) == {}
    assert art["body_markdown"] == before


def test_enabled_it_fixes_formatting_and_flags_the_rest(monkeypatch):
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")   # pin it — the suite must not depend on the ambient env
    art = {"meta_description": "From $647.",
           "body_markdown": "# T\n\n## Quick answer\nAcme costs $29:\n- a\n- b\n"}
    rep = _g()._verify_final_article(BRAND, art)
    assert rep["n_fixed"] >= 1 and rep["n_flagged"] >= 1
    assert "<ul>" in _html(art["body_markdown"]), "formatting WAS repaired"
    assert "$29" in art["body_markdown"] and "$647" in art["meta_description"], "prices left for the operator"


def test_it_never_raises_on_a_broken_article():
    assert isinstance(_g()._verify_final_article(None, {"body_markdown": None}), dict)


# ══ the endpoint — Claude finds, Claude or QWEN repairs, and it persists ═════════════════════════
import tempfile   # noqa: E402

from db import Database   # noqa: E402


def _tmp_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return p


def _seed(path, **cols):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(bid, "which agency", title="Which agency?",
                           body_markdown="# Which agency?\n\nAcme costs $29:\n- fast\n- cheap\n")
    if cols:
        db.update_blog(blog_id, **cols)
    db.close()
    return blog_id


def _verify_client(monkeypatch, path, semantic=None):
    import app as appmod
    from generators.blog_gen import BlogGenerator as BG
    appmod.DB_PATH = path
    appmod._db_initialized = True
    box = {}

    def _inline(_name, fn, **kw):
        box["result"] = fn(_task_id="t") if kw.get("pass_task_id") else fn()
        return "t"
    monkeypatch.setattr(appmod, "start_task", _inline)
    monkeypatch.setattr(BG, "_verify_content", lambda self, b, body: (list(semantic or []), {}))
    return appmod.app.test_client(), box


def test_the_endpoint_repairs_formatting_and_persists_the_report(monkeypatch):
    path = _tmp_db()
    try:
        bid = _seed(path)
        cli, box = _verify_client(monkeypatch, path)
        r = cli.post(f"/api/blogs/{bid}/verify", json={"surface": "blog", "use": "original"})
        assert r.status_code == 200
        res = box["result"]
        assert res["ok"] and res["n_fixed"] >= 1
        db = Database(path)
        db.connect()
        b = db.get_blog(bid)
        db.close()
        assert "<ul>" in _html(b["body_markdown"]), "the stored body was repaired"
        assert (b["quality_report"] or {}).get("verify"), "the report persisted"
    finally:
        os.unlink(path)


def test_the_endpoint_applies_a_gated_repair_and_reports_a_refused_one(monkeypatch):
    path = _tmp_db()
    try:
        bid = _seed(path)
        issues = [
            {"kind": "readability", "problem": "unclear", "quote": "Acme costs $29:",
             "fix": "Acme costs $29 per month:"},
            {"kind": "price", "problem": "bad", "quote": "- fast",
             "fix": "- pricing is not found in public sources"},
        ]
        cli, box = _verify_client(monkeypatch, path, semantic=issues)
        cli.post(f"/api/blogs/{bid}/verify", json={"surface": "blog", "use": "original"})
        res = box["result"]
        assert res["n_applied"] == 1 and res["n_skipped"] == 1
        assert "banned" in res["skipped"][0]["reason"]
        db = Database(path)
        db.connect()
        b = db.get_blog(bid)
        db.close()
        assert "$29 per month" in b["body_markdown"]
        assert "not found in public sources" not in b["body_markdown"]
    finally:
        os.unlink(path)


def test_the_endpoint_never_touches_the_other_body(monkeypatch):
    """A `use="rewritten"` run must not edit body_markdown, and vice versa."""
    path = _tmp_db()
    try:
        bid = _seed(path, rewritten_body="# Which agency?\n\nQwen text:\n- a\n")
        cli, box = _verify_client(monkeypatch, path)
        db = Database(path); db.connect(); before = db.get_blog(bid)["body_markdown"]; db.close()
        import app as appmod
        monkeypatch.setattr(appmod, "_resolve_writer_config",
                            lambda db: {"endpoint_url": "http://x", "key": "k", "model": "m"})
        cli.post(f"/api/blogs/{bid}/verify", json={"surface": "blog", "use": "rewritten"})
        db = Database(path); db.connect(); b = db.get_blog(bid); db.close()
        assert b["body_markdown"] == before, "the original must be untouched"
        assert "<ul>" in _html(b["rewritten_body"]), "the rewrite was repaired"
        assert (b["rewrites_meta"] or {}).get("blog", {}).get("verify", {}).get("repaired_by") == "qwen"
    finally:
        os.unlink(path)


def test_a_bad_surface_or_use_is_rejected(monkeypatch):
    path = _tmp_db()
    try:
        bid = _seed(path)
        cli, _ = _verify_client(monkeypatch, path)
        assert cli.post(f"/api/blogs/{bid}/verify", json={"surface": "nope"}).status_code == 400
        assert cli.post(f"/api/blogs/{bid}/verify", json={"use": "nope"}).status_code == 400
    finally:
        os.unlink(path)


# ══ the Drive upload — the surface the operator actually hands to a client ═══════════════════════
# The QWEN body is the one the Drive button sends (FU182), and it is the ONE body that never passed
# through `_finalize_article` — the on-demand rewrite endpoint stores it directly. So the formatting
# repair has to reach the upload path, not just the generation path.
BROKEN_QWEN = ("# Which agency?\n\n"
               "Here is what matters:\n"          # list with no blank line → one run-on paragraph
               "- fast turnaround\n- fair pricing\n\n"
               "A closing thought\n"              # '---' under prose → eats it into a heading
               "---\n"
               "Next section with **unclosed bold\n")


@pytest.fixture()
def drive(monkeypatch):
    path = _tmp_db()
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    brand_id = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(brand_id, "which agency", title="Which Agency?",
                           body_markdown="# Which agency?\n\nClaude body.\n")
    db.update_blog(blog_id, rewritten_body=BROKEN_QWEN)
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
    yield appmod.app.test_client(), blog_id, sent
    os.unlink(path)


def test_the_qwen_body_reaches_drive_with_its_formatting_REPAIRED(drive):
    """The whole point: a client's Google Doc must not contain a run-on paragraph, a paragraph
    silently turned into a heading, or literal asterisks."""
    client, blog_id, sent = drive
    # the stored body really is broken
    assert "<ul>" not in _html(BROKEN_QWEN)
    assert "<h2>A closing thought</h2>" in _html(BROKEN_QWEN)

    r = client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={"use": "rewritten"})
    assert r.status_code == 200, r.get_json()
    html = sent["html"]
    # NB the gdoc renderer adds inline styles (FU196), so tags arrive as `<ul style="...">` —
    # assert on the tag opener, never on a bare `<ul>`.
    assert "<ul" in html and "<li" in html, "the list must render as a list"
    assert re.search(r"<p[^>]*>A closing thought</p>", html), \
        "the paragraph must survive as a paragraph, not be eaten into a heading"
    assert not re.search(r"<h[1-6][^>]*>A closing thought", html)
    assert "**unclosed bold" not in html and "<strong" in html, \
        "the asterisks must not render literally"


def test_the_upload_still_sends_the_qwen_body_not_claudes(drive):
    """FU182's guarantee must survive the formatting pass."""
    client, blog_id, sent = drive
    client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={"use": "rewritten"})
    assert "Claude body." not in sent["html"]
    assert "fair pricing" in sent["html"]


def test_the_rewrite_endpoint_stores_a_formatted_body(monkeypatch):
    """Belt-and-braces at the SOURCE: a rewrite produced from now on is stored already-clean, so every
    consumer (the .md export, the gdoc, a CMS paste) gets it — not just the render path."""
    path = _tmp_db()
    try:
        bid = _seed(path)
        import app as appmod
        from generators.blog_gen import BlogGenerator as BG
        appmod.DB_PATH = path
        appmod._db_initialized = True
        box = {}

        def _inline(_name, fn, **kw):
            box["result"] = fn(_task_id="t") if kw.get("pass_task_id") else fn()
            return "t"
        monkeypatch.setattr(appmod, "start_task", _inline)
        monkeypatch.setattr(appmod, "_resolve_writer_config",
                            lambda db: {"endpoint_url": "http://x", "key": "k", "model": "m"})
        monkeypatch.setattr(BG, "_apply_writer_pass",
                            lambda self, article, body, brand, seed, surface="blog": BROKEN_QWEN)
        cli = appmod.app.test_client()
        cli.post(f"/api/blogs/{bid}/rewrite", json={"surface": "blog"})
        db = Database(path)
        db.connect()
        stored = db.get_blog(bid)["rewritten_body"]
        db.close()
        assert "<ul>" in _html(stored), "the STORED rewrite is already clean"
        assert "<h2>A closing thought</h2>" not in _html(stored)
    finally:
        os.unlink(path)
