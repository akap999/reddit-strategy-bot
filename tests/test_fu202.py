"""FU202 — the verification layer runs on EVERY generation, keeps BOTH versions, and the Google Doc's
reference formatting becomes a guarantee that repairs itself.

Operator decisions this round:
  1. "yes I want it to run on every generation with auto fix — but if it has made some updates after
     verification, give both versions as output, original and updated."
  2. "it should never fail — and remember it is not just about applying the Manrope font but
     maintaining the overall structure which it is doing right now (the example PDF I shared)."
  3. "for formatting check it also needs to make sure spacing and everything else is correct. For
     content check, it should read the entire thing and check if any content level mistakes in general
     also should not be there, like missing links, typos ... and also give an overall quality check."
  4. "you can skip the sources url check for now" — so BOTH source-related checks (does a cited [S#]
     support its claim; is the same page listed twice) are deliberately NOT built. Locked below.

Every formatting rule was confirmed against python-markdown BEFORE it shipped: a defect that already
renders correctly is applied only as a labelled COSMETIC tidy, never sold as a fix.
"""
import os
import re
import tempfile

import markdown
import pytest

from db import Database
from generators.blog_gen import BlogGenerator, scrub_markdown_formatting
from tests.stubs import StubClaude

BRAND = {"name": "Acme"}


@pytest.fixture(autouse=True)
def _pin_verify_env(monkeypatch):
    """The suite must never depend on the AMBIENT env — a developer running with the pass switched off
    would otherwise get green tests that prove nothing. Each inert-proof test re-sets its own flag."""
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")
    monkeypatch.setenv("BLOG_VERIFY_SEMANTIC", "1")


def _g(**kw):
    g = BlogGenerator(StubClaude(**kw), db=None)
    g._evidence_blocks = [{"label": "S1", "url": "https://x.example/a", "text": "t"},
                          {"label": "S2", "url": "https://x.example/b", "text": "t"}]
    return g


def _html(md):
    return markdown.markdown(md, extensions=["tables", "fenced_code"])


def _tmp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    return path


# ══ Change 1B — SPACING and the rest of the structure ════════════════════════════════════════════
def test_a_table_glued_to_a_paragraph_is_rescued():
    """The whole table renders as literal '| A | B |' text without the blank line — verified."""
    body = "# T\n\nProse line.\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    assert "<table>" not in _html(body)
    out, fixes = scrub_markdown_formatting(body)
    assert "<table>" in _html(out) and "<td>1</td>" in _html(out)
    assert [f["kind"] for f in fixes] == ["table-spacing"]


def test_a_block_glued_under_the_last_table_row_is_rescued():
    """The FU186 shape: a following heading is eaten as another table ROW."""
    body = "# T\n\n| A | B |\n|---|---|\n| 1 | 2 |\n## Next\n\ntext\n"
    assert "<td>## Next</td>" in _html(body)
    out, _ = scrub_markdown_formatting(body)
    assert "<h2>Next</h2>" in _html(out) and "<td>## Next</td>" not in _html(out)


def test_a_marker_with_no_space_after_it_is_not_a_list_until_repaired():
    for body, tag in (("# T\n\nIntro.\n\n-item one\n-item two\n", "ul"),
                      ("# T\n\nIntro.\n\n1.item one\n2.item two\n", "ol")):
        assert f"<{tag}>" not in _html(body)
        out, fixes = scrub_markdown_formatting(body)
        assert f"<{tag}>" in _html(out) and "<li>item one</li>" in _html(out)
        assert any(f["kind"] == "marker-space" for f in fixes)


def test_a_two_space_nested_bullet_renders_as_a_sibling_until_reindented():
    body = "# T\n\n- parent\n  - child\n"
    assert _html(body).count("<ul>") == 1, "2 spaces is NOT nesting in python-markdown"
    out, fixes = scrub_markdown_formatting(body)
    assert _html(out).count("<ul>") == 2, "after the re-indent it is a real nested list"
    assert any(f["kind"] == "list-indent" for f in fixes)


def test_exotic_spaces_are_converted_but_a_hard_line_break_survives():
    out, fixes = scrub_markdown_formatting("# T\n\na b\n")
    assert out == "# T\n\na b\n" and any(f["kind"] == "exotic-space" for f in fixes)
    # two trailing spaces ARE a markdown hard break — they must NOT be stripped
    hard = "# T\n\nline one  \nline two\n"
    assert "<br />" in _html(hard)
    out2, fixes2 = scrub_markdown_formatting(hard)
    assert out2 == hard and fixes2 == []
    # one trailing space means nothing and is dirt
    out3, _ = scrub_markdown_formatting("# T\n\nline one \nline two\n")
    assert out3 == "# T\n\nline one\nline two\n"


def test_stray_spacing_inside_a_line_is_tidied():
    out, _ = scrub_markdown_formatting("# T\n\nHello , world .   Done.\n")
    assert out == "# T\n\nHello, world. Done.\n"


def test_a_hashtag_line_and_a_spaceless_heading_are_NEVER_touched():
    """'##Heading' renders correctly as an H2 already, and adding the space would turn a '#hashtag'
    line into an H1 — the exact FU197 defect. So neither is touched."""
    body = "# T\n\n##Heading\n\n#TikTokMarketing #AI\n"
    assert "<h2>Heading</h2>" in _html(body)
    out, fixes = scrub_markdown_formatting(body)
    assert "##Heading" in out and "#TikTokMarketing #AI" in out
    assert not any(f["kind"] == "marker-space" for f in fixes)


def test_the_spacing_pass_is_idempotent_and_leaves_a_clean_body_alone():
    clean = ("# Title\n\nA paragraph.\n\n## Head\n\n- one\n- two\n\n| A | B |\n|---|---|\n| 1 | 2 |\n"
             "\n## Sources\n\n- [S1] x — <https://x.example/a>\n")
    out, fixes = scrub_markdown_formatting(clean)
    assert out == clean and fixes == []
    dirty = "# T\n\nProse.\n| A | B |\n|---|---|\n| 1 | 2 |\n## Next\n-item\n"
    once, _ = scrub_markdown_formatting(dirty)
    twice, again = scrub_markdown_formatting(once)
    assert twice == once and again == []


# ══ Change 1 — the CONTENT read ══════════════════════════════════════════════════════════════════
BODY = ("# Which agency?\n\n## Quick answer\n\nAcme supports multi-state payroll filings [S1].\n\n"
        "## Detail\n\nAcme does not support multi-state payroll filings [S2].\n\n"
        "## Pricing\n\nAcme charges $49 per month [S2].\n\n"
        "## Sources\n\n- [S1] a — <https://x.example/a>\n- [S2] b — <https://x.example/b>\n")


def _stub_content(monkeypatch, issues, assessment=None):
    monkeypatch.setattr(BlogGenerator, "_verify_content",
                        lambda self, brand, body: (list(issues), dict(assessment or {})))


def test_a_contradiction_and_a_typo_are_AUTO_FIXED_on_generate(monkeypatch):
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")
    _stub_content(monkeypatch, [
        {"kind": "contradiction", "quote": "Acme does not support multi-state payroll filings [S2].",
         "problem": "contradicts the Quick answer",
         "fix": "Acme supports multi-state payroll filings in every state it operates in [S2]."},
        {"kind": "typo", "quote": "Acme supports multi-state payroll filings [S1].",
         "problem": "missing word", "fix": "Acme supports multi-state payroll tax filings [S1]."},
    ])
    art = {"body_markdown": BODY}
    rep = _g()._verify_final_article(BRAND, art)
    assert "does not support" not in art["body_markdown"]
    assert "multi-state payroll tax filings [S1]" in art["body_markdown"]
    assert rep["n_applied"] == 2 and rep["n_skipped"] == 0
    assert {a["kind"] for a in rep["applied"]} == {"contradiction", "typo"}


def test_a_price_CHANGE_is_refused_by_the_fact_gate_and_surfaced_instead(monkeypatch):
    """Honest limit, locked deliberately: `_facts_preserved` refuses any repair that drops a protected
    number, so a price inconsistency can never be auto-corrected — a model silently rewriting a figure
    is exactly what that gate exists to stop. The operator still SEES it, as a reported skip."""
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")
    _stub_content(monkeypatch, [
        {"kind": "price", "quote": "Acme charges $49 per month [S2].",
         "problem": "the meta says $99", "fix": "Acme charges $99 per month [S2]."}])
    art = {"body_markdown": BODY}
    rep = _g()._verify_final_article(BRAND, art)
    assert "$49" in art["body_markdown"], "the figure is never silently rewritten"
    assert rep["n_applied"] == 0 and rep["n_skipped"] == 1
    assert "dropped" in rep["skipped"][0]["reason"] or "fact" in rep["skipped"][0]["reason"]


def test_a_link_or_duplicate_issue_is_FLAGGED_never_applied_even_with_a_fix(monkeypatch):
    """The auto-fix/flag split is decided in CODE, not by the `kind` the model wrote — so a flag-only
    kind arriving WITH a tempting `fix` still never edits the article."""
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")
    _stub_content(monkeypatch, [
        {"kind": "link", "quote": "Acme charges $49 per month [S2].", "problem": "no link",
         "fix": "Acme charges $49 per month, see the guide [S2]."},
        {"kind": "duplicate", "quote": "Acme supports multi-state payroll filings [S1].",
         "problem": "said twice", "fix": "Acme supports multi-state payroll filings."},
    ])
    art = {"body_markdown": BODY}
    rep = _g()._verify_final_article(BRAND, art)
    assert art["body_markdown"] == BODY, "a flag-only issue must never rewrite the body"
    assert rep.get("applied") == []
    kinds = {i["kind"] for i in rep["issues"]}
    assert {"link", "duplicate"} <= kinds


def test_a_repair_that_breaks_a_gate_is_refused_and_reported(monkeypatch):
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")
    _stub_content(monkeypatch, [
        {"kind": "contradiction", "quote": "Acme charges $49 per month [S2].",
         "problem": "x", "fix": "Acme charges $49 per month."},   # DROPS the [S2] citation
    ])
    art = {"body_markdown": BODY}
    rep = _g()._verify_final_article(BRAND, art)
    assert art["body_markdown"] == BODY
    assert rep["n_skipped"] == 1 and "citation" in rep["skipped"][0]["reason"]


def test_the_whole_article_is_read_even_when_it_is_long():
    """The FU201 prompt saw only the first 14,000 characters — the operator asked for the entire
    thing. Every chunk is a contiguous substring, so a returned `quote` still matches verbatim."""
    g = _g()
    body = "".join(f"## Section {i}\n\n" + ("word " * 400) + "\n\n" for i in range(30))
    chunks = g._verify_chunks(body)
    assert len(chunks) > 1 and "".join(chunks) == body
    assert all(c in body for c in chunks)
    assert g._verify_chunks("# short\n\ntext\n") == ["# short\n\ntext\n"]


def test_exactly_one_claude_call_for_a_normal_article_and_the_prompt_stays_in_scope():
    seen = []

    def _h(prompt):
        seen.append(prompt)
        return {"issues": [], "assessment": {"score": 78, "verdict": "solid", "strengths": ["clear"],
                                             "weaknesses": ["thin FAQ"]}}
    g = _g(call_handler=_h)
    issues, assessment = g._verify_content(BRAND, BODY)
    assert len(seen) == 1, "one call for an article under the chunk threshold"
    assert issues == [] and assessment["score"] == 78
    # the operator's "skip the sources url check for now" — the prompt EXCLUDES both source checks
    p = seen[0].lower()
    assert "do not report anything about whether a [s#] source supports its claim" in p
    assert "do not report duplicate sources" in p


def test_the_editorial_score_is_reported_beside_the_structure_score_never_merged_into_it(monkeypatch):
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")
    _stub_content(monkeypatch, [], {"score": 41, "verdict": "generic", "strengths": [],
                                    "weaknesses": ["no specifics"]})
    g, art = _g(), {"body_markdown": BODY, "meta_description": "d"}
    rep = g._verify_final_article(BRAND, art)
    assert rep["assessment"]["score"] == 41
    before = g._quality_report(art, BRAND)["score"]
    art["quality_report"] = {"verify": rep}
    assert g._quality_report(art, BRAND)["score"] == before, \
        "the deterministic score must stay reproducible — an LLM opinion never moves it"


def test_the_source_checks_are_deliberately_NOT_extended(monkeypatch):
    """FU200's collapse (an EXACT normalised URL match) still works; the harder www./scheme/fragment
    normalisation was NOT added this round, by the operator's decision. Locked so a later round is a
    deliberate change, not an accident."""
    g = _g()
    g._evidence_blocks = [{"label": "A", "url": "https://x.example/p?utm=1", "text": "t"},
                          {"label": "B", "url": "https://x.example/p/", "text": "t"},
                          {"label": "C", "url": "https://www.x.example/p", "text": "t"}]
    out = g._rebuild_sources("Claim one [S1]. Claim two [S2]. Claim three [S3].\n")
    assert out.count("- [S") == 2, "FU200 collapses the query/trailing-slash pair only"
    assert "www.x.example" in out, "the www. variant is still listed separately (not extended)"


# ══ Change 1b — the free deterministic checks ════════════════════════════════════════════════════
def _flags(body, **art):
    a = {"body_markdown": body, "meta_description": ""}
    a.update(art)
    return _g()._verify_consistency(BRAND, a)


def test_a_promise_of_a_page_with_no_link_is_flagged():
    out = _flags("# T\n\n## A\n\nFor the full breakdown, see our guide on pricing.\n")
    assert any(i["kind"] == "link" for i in out)
    ok = _flags("# T\n\n## A\n\nFor the full breakdown, [see our guide](https://x.example/g).\n")
    assert not [i for i in ok if i["kind"] == "link"]


def test_a_href_that_is_not_a_url_is_flagged():
    assert any(i["kind"] == "link" for i in
               _flags("# T\n\n## A\n\nRead [the page](example.com/pricing) for detail.\n"))


def test_the_same_sentence_written_twice_is_flagged():
    s = "Acme handles multi-state payroll filings for distributed engineering teams."
    assert any(i["kind"] == "duplicate" for i in _flags(f"# T\n\n## A\n\n{s}\n\n## B\n\n{s}\n"))
    assert not [i for i in _flags(f"# T\n\n## A\n\n{s}\n\n## B\n\nSomething else entirely here.\n")
                if i["kind"] == "duplicate"]


def test_a_skipped_heading_level_is_flagged():
    out = _flags("# T\n\n## A\n\ntext\n\n#### D\n\ntext\n")
    assert any("level is skipped" in i["problem"] for i in out)


# ══ Change 2 — BOTH versions are kept ════════════════════════════════════════════════════════════
def test_the_pre_verification_body_is_stored_only_when_the_pass_changed_something(monkeypatch):
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")
    _stub_content(monkeypatch, [
        {"kind": "contradiction", "quote": "Acme does not support multi-state payroll filings [S2].",
         "problem": "x", "fix": "Acme supports multi-state payroll filings everywhere it operates [S2]."}])
    art = {"body_markdown": BODY}
    _g()._verify_final_article(BRAND, art)
    assert art["body_pre_verify"] == BODY
    assert art["body_markdown"] != BODY

    _stub_content(monkeypatch, [])
    clean = {"body_markdown": BODY}
    _g()._verify_final_article(BRAND, clean)
    assert "body_pre_verify" not in clean, "an untouched blog stores nothing"


def test_body_pre_verify_round_trips_through_the_db():
    path = _tmp_db()
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(bid, "seed", title="T", body_markdown="new")
    db.update_blog(blog_id, body_pre_verify="old")
    # FU205 (R1): stored markdown bodies are normalised to one trailing newline on write.
    assert db.get_blog(blog_id)["body_pre_verify"] == "old\n"
    db.close()
    os.unlink(path)


# ══ Change 3 — the Drive doc's formatting is a self-healing GUARANTEE ════════════════════════════
DOC_MD = ("# Which agency?\n\n## Options\n\n- Acme\n- Globex\n\n| Firm | Price |\n|---|---|\n"
          "| Acme | $99 |\n\n### A detail\n\nSome prose.\n")


@pytest.fixture()
def drive(monkeypatch):
    path = _tmp_db()
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    brand_id = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(brand_id, "which agency", title="Which Agency?", body_markdown=DOC_MD)
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
    yield appmod.app.test_client(), blog_id, sent, monkeypatch
    os.unlink(path)


def _assert_reference_spec(html):
    """The spec measured from the operator's PDF in FU196 — font, the 24/18/14/11pt scale, the
    MARGINS that give the Doc its rhythm, bordered cells, and real heading tags for the outline pane."""
    for tag, size in (("h1", "24pt"), ("h2", "18pt"), ("h3", "14pt")):
        m = re.search(rf"<{tag}[^>]*style=\"([^\"]*)\"", html)
        assert m, f"no styled <{tag}> in the uploaded doc"
        assert "Manrope" in m.group(1) and size in m.group(1)
        assert "margin:" in m.group(1), f"<{tag}> lost its spacing"
    p = re.search(r"<p[^>]*style=\"([^\"]*)\"", html)
    assert p and "Manrope" in p.group(1) and "11pt" in p.group(1) and "margin:" in p.group(1)
    td = re.search(r"<td[^>]*style=\"([^\"]*)\"", html)
    assert td and "border:" in td.group(1) and "11pt" in td.group(1)
    assert "<ul" in html and "<li" in html, "a real list, not run-on paragraphs"


def test_a_bs4_failure_no_longer_ships_an_arial_doc(drive):
    """`_apply_gdoc_inline_styles` "never raises: on any failure the fragment is returned untouched",
    and its own docstring says the consequence — Arial at Docs' defaults. Simulated here."""
    client, blog_id, sent, monkeypatch = drive
    import app as appmod
    monkeypatch.setattr(appmod, "_apply_gdoc_inline_styles", lambda frag: frag)
    r = client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={})
    assert r.status_code == 200, r.get_json()
    _assert_reference_spec(sent["html"])


def test_a_markdown_render_failure_no_longer_ships_one_pre_block(drive):
    """The other silent path: the render throws and the ENTIRE body becomes one <pre> — no headings,
    so no Docs outline pane, no table, no type scale."""
    client, blog_id, sent, monkeypatch = drive
    import markdown as _mdmod

    def _boom(*a, **kw):
        raise RuntimeError("render died")
    monkeypatch.setattr(_mdmod, "markdown", _boom)
    r = client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={})
    assert r.status_code == 200, r.get_json()
    html = sent["html"]
    assert "<pre>" not in html, "the body must not ship as one preformatted block"
    assert re.search(r"<h1[^>]*>Which agency\?</h1>", html)
    assert re.search(r"<h2[^>]*>Options</h2>", html) and re.search(r"<h3[^>]*>A detail</h3>", html)
    assert "<table" in html and "<td" in html and "<ul" in html
    _assert_reference_spec(html)


def test_a_healthy_render_is_unchanged_and_the_guard_is_idempotent(drive):
    client, blog_id, sent, _ = drive
    assert client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={}).status_code == 200
    html = sent["html"]
    _assert_reference_spec(html)
    import app as appmod
    inner = re.search(r"<body>\n(.*)\n</body>", html, re.S).group(1)
    again, repairs = appmod._gdoc_enforce_format(inner)
    assert repairs == 0 and again == inner, "an already-correct doc is never rewritten"


def test_the_upload_never_fails_because_of_formatting(drive):
    """The operator's "it should never fail". Even with BOTH silent paths broken at once."""
    client, blog_id, sent, monkeypatch = drive
    import app as appmod
    import markdown as _mdmod
    monkeypatch.setattr(appmod, "_apply_gdoc_inline_styles", lambda frag: frag)
    monkeypatch.setattr(_mdmod, "markdown", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("x")))
    r = client.post(f"/api/blogs/{blog_id}/upload-gdoc", json={})
    assert r.status_code == 200 and r.get_json().get("ok")
    _assert_reference_spec(sent["html"])


# ══ INERT PROOFS — the switches that protect every existing suite ════════════════════════════════
def test_BLOG_VERIFY_FINAL_0_makes_the_whole_pass_a_no_op(monkeypatch):
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "0")
    calls = []
    g = _g(call_handler=lambda p: calls.append(p) or {})
    art = {"body_markdown": "Prose.\n| A | B |\n|---|---|\n| 1 | 2 |\n"}
    assert g._verify_final_article(BRAND, art) == {}
    assert art["body_markdown"] == "Prose.\n| A | B |\n|---|---|\n| 1 | 2 |\n"
    assert "body_pre_verify" not in art and calls == []


def test_BLOG_VERIFY_SEMANTIC_0_keeps_the_free_half_and_makes_NO_claude_call(monkeypatch):
    """The paid half can be switched off on its own: formatting is still repaired and the free
    deterministic checks still flag — with zero API calls."""
    monkeypatch.setenv("BLOG_VERIFY_FINAL", "1")
    monkeypatch.setenv("BLOG_VERIFY_SEMANTIC", "0")
    calls = []
    g = _g(call_handler=lambda p: calls.append(p) or {})
    art = {"body_markdown": "# T\n\nProse.\n| A | B |\n|---|---|\n| 1 | 2 |\n"}
    rep = g._verify_final_article(BRAND, art)
    assert calls == [], "the paid read must not fire"
    assert "<table>" in _html(art["body_markdown"]), "formatting is still repaired for free"
    assert rep["n_fixed"] >= 1 and "applied" not in rep and "assessment" not in rep
