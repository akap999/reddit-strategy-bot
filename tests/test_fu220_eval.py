"""FU220 — the blog scoreboard. $0: every model call is a StubClaude, every page fetch is canned.

The detectors are driven by REAL exports (the reviewed Jolly Search law-firms blog, the Thyseed and
v8/v9 goldens, the CMK guide) so they are measured against the product, not against strings written
next to the code. The grounding report reuses FU208 Verify unchanged; these tests pin that it reports
a contradicted claim with the page's own words and never trusts an invented quote."""
import html as _html
import importlib.util
import json
import os
import re
import tempfile

import pytest

from db import Database
from generators import blog_eval as E
from generators.blog_gen import BlogGenerator as BG
from tests.stubs import StubClaude

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")


def _fixture(name):
    p = os.path.join(FIX, name)
    if not os.path.exists(p):
        pytest.skip(f"tests/fixtures/{name} not present")
    return open(p, encoding="utf-8").read()


def _html_to_md(t):
    """The export's visible structure back into Markdown: headings, tables, lists, quotes, rules."""
    m = re.search(r"(?is)<body[^>]*>(.*)</body>", t)
    t = m.group(1) if m else t
    t = re.sub(r"(?is)<script.*?</script>|<style.*?</style>", "", t)

    def cell(x):
        return _html.unescape(re.sub(r"<[^>]+>", "", x)).strip().replace("\n", " ")

    def table(m):
        rows = []
        for tr in re.findall(r"(?is)<tr.*?>(.*?)</tr>", m.group(1)):
            cs = [cell(c) for c in re.findall(r"(?is)<t[dh][^>]*>(.*?)</t[dh]>", tr)]
            if cs:
                rows.append("| " + " | ".join(cs) + " |")
        if len(rows) >= 2:
            rows.insert(1, "| " + " | ".join(["---"] * (rows[0].count("|") - 1)) + " |")
        return "\n\n" + "\n".join(rows) + "\n\n"
    t = re.sub(r"(?is)<table.*?>(.*?)</table>", table, t)
    for n in (1, 2, 3, 4):
        t = re.sub(rf"(?is)<h{n}[^>]*>(.*?)</h{n}>", lambda m, n=n: "\n\n" + "#" * n + " " + cell(m.group(1)) + "\n\n", t)
    t = re.sub(r"(?is)<strong>(.*?)</strong>|<b>(.*?)</b>", lambda m: "**" + (m.group(1) or m.group(2) or "") + "**", t)
    t = re.sub(r"(?is)<li[^>]*>(.*?)</li>", lambda m: "\n- " + m.group(1).strip(), t)
    t = re.sub(r"(?is)<blockquote[^>]*>(.*?)</blockquote>",
               lambda m: "\n> " + re.sub(r"<[^>]+>", "", m.group(1)).strip() + "\n", t)
    t = re.sub(r"(?is)<hr\s*/?>", "\n\n---\n\n", t)
    t = re.sub(r"(?is)</?p[^>]*>", "\n\n", t)
    t = _html.unescape(re.sub(r"<[^>]+>", "", t))
    return re.sub(r"\n{3,}", "\n\n", t).strip() + "\n"


NEW_CHECKS = {"duplicate-section", "section-after-faq", "uncited-cell", "publisher-only-downside",
              "quick-answer-disclaimer", "repeated-citation"}


def _new(body, brand):
    return [it for it in E.defects_report(None, {"name": brand}, body)["items"] if it["check"] in NEW_CHECKS]


# ── the reviewed Jolly blog: every defect the reviewer found, found ──────────────────────────────────
def test_the_jolly_blog_flags_every_artifact_the_reviewer_found():
    body = _fixture("fu220_jolly_body.md")
    got = {}
    for it in _new(body, "Jolly Search"):
        got.setdefault(it["check"], []).append(it["detail"])
    assert set(got) == NEW_CHECKS, got
    assert any("Seige Media (Siege Media)" in d and '"Siege Media"' in d for d in got["duplicate-section"])
    assert any("Seige Media (Siege Media)" in d for d in got["section-after-faq"])
    assert got["uncited-cell"] == [d for d in got["uncited-cell"] if d.startswith("Siege Media")]
    assert "General; SaaS, fintech" in got["uncited-cell"][0]
    assert "Honest trade-off" in got["publisher-only-downside"][0]
    assert "does not constitute legal or financial counsel" in got["quick-answer-disclaimer"][0]
    assert got["repeated-citation"] == ['[S2] is cited twice in a row: "\u2026s more often in '
                                        'AI-generated answers and discovery searches [S2][S2]"']


def test_repeated_citation_needs_the_same_number_twice_in_a_row():
    hits = E.detect_repeated_citations("a [S1][S2] b [S3] [S3] c [S4][S5][S4] d [S6]. [S6]")
    assert [h["detail"].split(" ")[0] for h in hits] == ["[S3]", "[S4]"]


def test_the_generators_own_checks_run_on_a_stored_body_without_crashing():
    body = _fixture("fu220_jolly_body.md")
    rep = E.defects_report(BG(StubClaude(), None), {"name": "Jolly Search",
                                                    "domain_url": "https://jollysearch.com"}, body, "meta")
    assert rep["count"] == len(rep["items"]) >= 5
    assert sum(rep["by_check"].values()) == rep["count"]


# ── no cry-wolf on the other real exports ────────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,brand", [("thyseed_blog.html", "Thyseed"), ("v9_blog.html", "AI Inspo")])
def test_the_golden_exports_raise_none_of_the_new_flags(name, brand):
    assert _new(_html_to_md(_fixture(name)), brand) == []


def test_v8_flags_only_its_known_unsourced_row():
    """The v8 review: Suno had no source at all. The only uncited cells are Suno's."""
    hits = _new(_html_to_md(_fixture("v8_blog.html")), "AI Inspo")
    assert hits and all(h["check"] == "uncited-cell" and h["detail"].startswith("Suno") for h in hits)


def test_two_sections_that_differ_by_one_real_word_are_not_duplicates():
    """The CMK guide has "…Order for a Kitchen Renovation?" and "…for a Bathroom Renovation?" — two
    real sections. A plain similarity ratio called them duplicates."""
    assert _new(_fixture("fu216_cmk_guide_body.md"), "CMK Construction") == []
    assert not E.headings_match("What Is the Correct Order for a Kitchen Renovation?",
                                "What Is the Correct Order for a Bathroom Renovation?")


@pytest.mark.parametrize("a,b,same", [
    ("Seige Media", "Siege Media", True),                  # a typo
    ("Seige Media (Siege Media)", "Siege Media", True),    # a parenthetical alias
    ("How it works", "Works: how it", True),               # the same words moved
    ("Jolly Search", "Jolly Search Pricing", False),       # an extra real word
    ("Acme Payroll", "Beta Payroll", False),
])
def test_heading_matching(a, b, same):
    assert E.headings_match(a, b) is same


# ── vertical-neutral: the same shape fires in every vertical ─────────────────────────────────────────
VERTICALS = [
    ("Ledgerly", "payroll software", "Payroll Plus", "PayFast", "financial"),
    ("CMK Construction", "renovation contractors", "BuildRight", "Tampa Remodel Co", "professional"),
    ("LendWell", "equipment lenders", "CapitalOne Equipment", "FastFund", "financial"),
    ("PeterMD", "weight-loss programs", "Ro", "Noom", "medical"),
]


def _profiles(brand, cat, c1, c2, adv, *, only_brand_downside=True, disclaimer_in_qa=True):
    qa = f"**Quick answer:** For {cat}, {brand}, {c1} and {c2} are strong options [S1]."
    if disclaimer_in_qa:
        qa += f"\n\n> *This is not {adv} advice.*"
    down = "\n\n**Honest trade-off:** it is not the cheapest option."
    return (f"# Best {cat}?\n\n{qa}\n\n---\n\n## Which {cat} are best?\n\nThe options below [S1].\n\n"
            f"## Option-by-option breakdown\n\n### {brand}\n\n{brand} does X [S1].{down}\n\n"
            f"### {c1}\n\n{c1} does Y [S2]." + ("" if only_brand_downside else down) +
            f"\n\n### {c2}\n\n{c2} does Z [S3].\n\n## FAQ\n\n### Is it worth it?\n\nYes [S1].\n\n"
            "## Sources\n\n- [S1] a — <https://a.example>\n- [S2] b — <https://b.example>\n"
            "- [S3] c — <https://c.example>\n")


@pytest.mark.parametrize("brand,cat,c1,c2,adv", VERTICALS)
def test_publisher_only_downside_and_qa_disclaimer_fire_in_every_vertical(brand, cat, c1, c2, adv):
    checks = {h["check"] for h in _new(_profiles(brand, cat, c1, c2, adv), brand)}
    assert {"publisher-only-downside", "quick-answer-disclaimer"} <= checks


@pytest.mark.parametrize("brand,cat,c1,c2,adv", VERTICALS)
def test_a_balanced_page_with_no_qa_disclaimer_is_silent(brand, cat, c1, c2, adv):
    body = _profiles(brand, cat, c1, c2, adv, only_brand_downside=False, disclaimer_in_qa=False)
    assert _new(body, brand) == []


def test_a_disclaimer_below_the_quick_answer_is_fine():
    body = _profiles("Ledgerly", "payroll software", "Acme", "Beta", "x", disclaimer_in_qa=False,
                     only_brand_downside=False)
    body = body.replace("## FAQ", "> *This is not financial advice.*\n\n## FAQ")
    assert _new(body, "Ledgerly") == []


def test_a_clean_page_flags_nothing():
    body = ("*[Add author byline before publishing]*\n\n# Which CRM is best for small teams?\n\n"
            "**Quick answer:** Acme CRM suits teams under ten [S1]; Beta CRM suits larger teams [S2].\n\n"
            "---\n\n## Which CRM is best for small teams?\n\nAcme CRM is simplest to set up [S1].\n\n"
            "| CRM | Price | Best for |\n| --- | --- | --- |\n| Acme CRM | $12/user [S1] | Small teams [S1] |\n"
            "| Beta CRM | $20/user [S2] | Larger teams [S2] |\n| Gamma CRM | $15/user [S3] | Agencies [S3] |\n\n"
            "## FAQ\n\n### Is a free CRM enough?\n\nFor a team of two, often yes [S1].\n\n"
            "## Sources\n\n- [S1] Acme — <https://acme.example>\n- [S2] Beta — <https://beta.example>\n"
            "- [S3] Gamma — <https://gamma.example>\n")
    rep = E.defects_report(BG(StubClaude(), None), {"name": "Acme CRM"}, body)
    assert [it for it in rep["items"] if it["check"] in NEW_CHECKS] == []


def test_new_detector_code_names_no_vertical():
    src = open(E.__file__, encoding="utf-8").read()
    code = re.sub(r'(?s)""".*?"""', "", src)   # docstrings describe the reproduction case
    for word in ("law firm", "attorney", "GLP", "semaglutide", "payroll", "concrete", "Jolly", "Siege"):
        assert word.lower() not in code.lower(), word


# ── A. grounding ─────────────────────────────────────────────────────────────────────────────────────
GBODY = ("*[Add author byline before publishing]*\n\n# Which payroll tools are best?\n\n"
         "**Quick answer:** Acme Payroll costs $49/month [S1]. Beta HR offers a 30-day free trial [S2]. "
         "Gamma pays contractors in two days. Delta supports 40 countries [S3].\n\n"
         "## Sources\n\n- [S1] Acme — <https://acme.example/pricing>\n"
         "- [S2] Beta — <https://beta.example/trial>\n- [S3] Delta — <https://delta.example/>\n")
PAGES = {"https://acme.example/pricing": "Acme Payroll pricing. Standard plan: $59 per month, billed annually.",
         "https://beta.example/trial": "Start your Beta HR free trial today. No card required.",
         "https://delta.example/": ""}


def _ground_handler(prompt):
    if "You are auditing a FINISHED, published article" in prompt:
        return {"claims": [
            {"claim": "Acme Payroll costs $49/month", "kind": "price", "entity": "Acme Payroll",
             "value": "$49/month", "cited": ["S1"],
             "occurrences": [{"where": "quick answer", "quote": "Acme Payroll costs $49/month [S1]."}]},
            {"claim": "Beta HR offers a 30-day free trial", "kind": "figure", "entity": "Beta HR",
             "value": "30-day", "cited": ["S2"],
             "occurrences": [{"where": "quick answer", "quote": "Beta HR offers a 30-day free trial [S2]."}]},
            {"claim": "Gamma pays contractors in two days", "kind": "capability", "entity": "Gamma",
             "value": "", "cited": [],
             "occurrences": [{"where": "quick answer", "quote": "Gamma pays contractors in two days."}]},
            {"claim": "Delta supports 40 countries", "kind": "capability", "entity": "Delta",
             "value": "40", "cited": ["S3"],
             "occurrences": [{"where": "quick answer", "quote": "Delta supports 40 countries [S3]."}]},
        ], "notes": []}
    if "Check each statement below against the page text" in prompt:
        out = []
        for ref, text in re.findall(r"\[(c\d+)\] \(pages [^)]*\) (.*)", prompt):
            if text.startswith("Acme"):
                out.append({"ref": ref, "status": "contradicted", "page": "P1",
                            "page_says": "Standard plan: $59 per month", "page_value": "$59/month"})
            elif text.startswith("Beta"):   # an invented quote: the page never says 45 days
                out.append({"ref": ref, "status": "contradicted", "page": "P2",
                            "page_says": "a 45-day free trial", "page_value": "45 days"})
        return {"verdicts": out}
    return {}


def _ground():
    gen = BG(StubClaude(call_handler=_ground_handler), None)
    gen._fetch_url = lambda u: PAGES.get(u, "")
    return E.grounding_report(gen, {"name": "Ledgerly"}, GBODY, "")


def test_grounding_reports_a_contradicted_claim_with_the_pages_own_words():
    rep = _ground()
    acme = next(i for i in rep["items"] if i["claim"].startswith("Acme"))
    assert acme["status"] == "contradicted"
    assert acme["page_says"] == "Standard plan: $59 per month"
    assert acme["url"] == "https://acme.example/pricing" and acme["cited"] == ["S1"]
    assert rep["items"][0]["status"] == "contradicted", "contradicted claims are listed first"


def test_an_invented_page_quote_never_counts_as_a_contradiction():
    beta = next(i for i in _ground()["items"] if i["claim"].startswith("Beta"))
    assert beta["status"] == "not_on_page" and beta["page_says"] == ""


def test_grounding_counts_uncited_and_unreadable_claims_separately():
    c = _ground()["counts"]
    assert c == {**c, "claims": 4, "checked": 2, "contradicted": 1, "not_on_page": 1, "confirmed": 0,
                 "uncited": 1, "unreadable": 1}
    assert c["grounded_share"] == 0.0


def test_grounding_issues_no_web_search():
    gen = BG(StubClaude(call_handler=_ground_handler), None)
    gen._fetch_url = lambda u: PAGES.get(u, "")
    E.grounding_report(gen, {"name": "Ledgerly"}, GBODY, "")
    assert gen.claude.searches == [] and gen.claude.site_fact_calls == []


# ── C. rubric ────────────────────────────────────────────────────────────────────────────────────────
def test_rubric_clamps_scores_and_blanks_an_invented_quote():
    stub = StubClaude(call_handler=lambda p: {
        "scores": {"answers_question": 12, "sourcing": 6, "publisher_placement": "x", "fairness": 7,
                   "structure": -1, "polish": 8},
        "overall": 140,
        "issues": [{"issue": "price is wrong", "quote": "Acme Payroll costs $49/month", "fix": "use $59"},
                   {"issue": "made up", "quote": "a sentence that is not in the article", "fix": "-"}]},
        model="claude-opus-4-8")
    r = E.rubric_report(stub, "Ledgerly", "Which payroll tools are best?", "", GBODY)
    assert r["scores"] == {"answers_question": 10.0, "sourcing": 6.0, "publisher_placement": None,
                           "fairness": 7.0, "structure": 0.0, "polish": 8.0}
    assert r["overall"] == 100.0 and r["model"] == "claude-opus-4-8"
    assert r["issues"][0]["quote_verified"] and r["issues"][0]["quote"]
    assert not r["issues"][1]["quote_verified"] and r["issues"][1]["quote"] == ""


def test_rubric_prompt_is_vertical_neutral_and_carries_all_six_dimensions():
    p = E._rubric_prompt("Ledgerly", "t", "m", "body")
    for d in E.RUBRIC_DIMENSIONS:
        assert f'"{d}"' in p
    for word in ("drug", "clinical", "FDA", "lawyer", "attorney", "payroll", "contractor", "SaaS"):
        assert word.lower() not in p.replace("Ledgerly", "").lower(), word


def test_the_rubric_does_not_count_the_deliberate_byline_placeholder():
    assert "[Add author byline before publishing]" in E._rubric_prompt("B", "t", "m", "body")


def test_the_scoreboard_reads_more_pages_than_a_verify_click(monkeypatch):
    seen = {}
    gen = BG(StubClaude(call_handler=_ground_handler), None)
    gen._fetch_url = lambda u: PAGES.get(u, "")
    orig = gen._vx_fetch_pages
    gen._vx_fetch_pages = lambda urls, *a, **k: (seen.setdefault("urls", list(urls)), orig(urls, *a, **k))
    E.grounding_report(gen, {"name": "Ledgerly"}, GBODY, "", max_pages=2)
    assert len(seen["urls"]) == 2
    monkeypatch.delenv("BLOG_EVAL_MAX_PAGES", raising=False)
    src = open(E.__file__, encoding="utf-8").read()
    assert '"BLOG_EVAL_MAX_PAGES", "40"' in src


def test_the_rubric_runs_on_a_different_model_than_the_writer():
    from config import DEFAULT_MODEL
    assert E.EVAL_RUBRIC_MODEL != DEFAULT_MODEL


def test_evaluate_blog_keeps_the_three_reports_separate():
    gen = BG(StubClaude(call_handler=_ground_handler), None)
    gen._fetch_url = lambda u: PAGES.get(u, "")
    rub = StubClaude(call_handler=lambda p: {"scores": {}, "overall": 70, "issues": []})
    rep = E.evaluate_blog(gen, {"name": "Ledgerly"},
                          {"id": 7, "body_markdown": GBODY, "title": "t", "seed": "s"}, rubric_client=rub)
    assert {"defects", "grounding", "rubric"} <= set(rep)
    assert "score" not in rep, "the three reports are never merged into one number"
    row = E.summary_row(rep)
    assert row["contradicted"] == 1 and row["rubric"] == 70.0 and row["id"] == 7


# ── the evidence snapshot ────────────────────────────────────────────────────────────────────────────
def test_evidence_snapshot_carries_the_page_text_and_the_sourcing():
    gen = BG(StubClaude(), None)
    assert gen.evidence_snapshot() == {}
    gen._evidence_blocks = [{"label": "Acme", "url": "https://acme.example", "text": "Acme is $59."}]
    gen._last_sourcing = {"tools": ["Beta"], "fresh": [], "prices": {"beta": {"value": "$9"}}}
    gen._sources_render = {"lines": ["- [S1] Acme — <https://acme.example>"], "map": {1: 1}}
    snap = gen.evidence_snapshot()
    assert snap["blocks"][0]["text"] == "Acme is $59."
    assert snap["sourcing"]["tools"] == ["Beta"]
    assert snap["sources_render"]["map"] == {"1": 1}
    json.dumps(snap)


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _seed(path, body="*[Add author byline before publishing]*\n\n# T\n\nBody.\n"):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Ledgerly")
    blog_id = db.save_blog(bid, "Which payroll tools are best?", title="T", body_markdown=body,
                           status="draft")
    return db, bid, blog_id


def test_the_snapshot_round_trips_replaces_and_is_deleted_with_its_blog():
    path = _tmp()
    try:
        db, _, blog_id = _seed(path)
        assert db.get_blog_evidence(blog_id) == {}
        assert db.save_blog_evidence(blog_id, {"blocks": [{"text": "one"}]})
        assert db.save_blog_evidence(blog_id, {"blocks": [{"text": "two"}]})
        assert db.get_blog_evidence(blog_id) == {"blocks": [{"text": "two"}]}
        assert "snapshot" not in db.get_blog(blog_id), "the blog modal must not carry the page text"
        db.delete_blog(blog_id)
        assert db.get_blog_evidence(blog_id) == {}
        db.close()
    finally:
        os.unlink(path)


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
    monkeypatch.setattr(appmod, "_ensure_content_context", lambda c, db, b: b)
    monkeypatch.setattr(appmod, "_build_blog_writer", lambda db: (None, "off"))
    monkeypatch.setattr(appmod, "_blog_reddit_evidence", lambda *a, **k: (None, "skipped"))

    def _gen_blog(self, brand, seed, **kw):
        self._evidence_blocks = [{"label": "Acme", "url": "https://acme.example", "text": "page text"}]
        return {"title": seed, "meta_description": "m", "keywords": [], "gen_cost": 0,
                "body_markdown": "*[Add author byline before publishing]*\n\n# T\n\nBody [S1].\n",
                "claims_flagged": [], "linkedin_text": ""}
    monkeypatch.setattr(BG, "generate_blog", _gen_blog)

    def _client(path):
        appmod.DB_PATH = path
        appmod._db_initialized = True
        return appmod.app.test_client()
    return _client, box


def _snap(path, blog_id):
    db = Database(path)
    db.connect()
    try:
        return db.get_blog_evidence(blog_id)
    finally:
        db.close()


def test_generate_and_regenerate_store_the_evidence(app_client):
    make, box = app_client
    path = _tmp()
    try:
        db, bid, blog_id = _seed(path)
        db.close()
        cli = make(path)
        assert cli.post("/api/blogs/generate", json={"brand_id": bid, "seed": "Which payroll tools?"}
                        ).status_code == 200
        new_id = box["result"]["blog_id"]
        assert _snap(path, new_id)["blocks"][0]["text"] == "page text"
        assert _snap(path, blog_id) == {}
        assert cli.post(f"/api/blogs/{blog_id}/regenerate", json={"part": "all"}).status_code == 200
        assert _snap(path, blog_id)["blocks"][0]["url"] == "https://acme.example"
    finally:
        os.unlink(path)


def test_resuming_a_paused_blog_stores_the_evidence(app_client, monkeypatch):
    make, box = app_client
    path = _tmp()
    try:
        db, bid, blog_id = _seed(path)
        db.update_blog(blog_id, status="awaiting_sources", pending_state=json.dumps(
            {"missing": [{"tool": "Beta"}], "checkpoint": {"sourcing": {"tools": ["Beta"]},
                                                           "article": {"title": "T"}}}))
        db.close()

        def _finish(self, brand, seed, checkpoint, provided):
            self._evidence_blocks = [{"label": "Beta", "url": "https://beta.example", "text": "pasted"}]
            return {"title": "T", "meta_description": "m", "keywords": [], "claims_flagged": [],
                    "body_markdown": "*[Add author byline before publishing]*\n\n# T\n\nBody.\n",
                    "linkedin_text": "", "prompt_version": ""}
        monkeypatch.setattr(BG, "finish_pending_blog", _finish)
        cli = make(path)
        assert cli.post(f"/api/blogs/{blog_id}/provide-sources",
                        json={"sources": [{"tool": "Beta", "fact": "pasted"}]}).status_code == 200
        assert _snap(path, blog_id)["blocks"][0]["text"] == "pasted"
    finally:
        os.unlink(path)


# ── the runner ───────────────────────────────────────────────────────────────────────────────────────
def _runner():
    p = os.path.join(os.path.dirname(FIX), "..", "tools", "blog_eval.py")
    spec = importlib.util.spec_from_file_location("blog_eval_runner", os.path.abspath(p))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_latest_per_seed_picks_the_newest_finished_row_of_each_seed():
    R = _runner()
    path = _tmp()
    try:
        long = "x " * 1500
        db, bid, first = _seed(path, body=long)
        second = db.save_blog(bid, "which payroll tools are best? ", title="T2", body_markdown=long)
        other = db.save_blog(bid, "Another seed", title="T3", body_markdown=long)
        paused = db.save_blog(bid, "Paused seed", title="T4", body_markdown=long, status="awaiting_sources")
        short = db.save_blog(bid, "Short seed", title="T5", body_markdown="tiny")
        ids = {c["id"] for c in R._candidates(db, [bid], 40)}
        assert ids == {second, other}, ids
        assert first not in ids and paused not in ids and short not in ids
        db.close()
    finally:
        os.unlink(path)


def test_compare_lines_up_two_runs_by_blog(tmp_path, monkeypatch, capsys):
    R = _runner()
    monkeypatch.setenv("BLOG_EVAL_DIR", str(tmp_path))

    def rep(contra, rub):
        return {"blog_id": 1, "brand": "Ledgerly", "defects": {"count": 2},
                "grounding": {"counts": {"contradicted": contra, "not_on_page": 1, "uncited": 0}},
                "rubric": {"overall": rub}}
    (tmp_path / "before-1.json").write_text(json.dumps({"blogs": [rep(4, 70)]}))
    (tmp_path / "after-1.json").write_text(json.dumps({"blogs": [rep(1, 78)]}))
    R.compare("before", "after")
    out = capsys.readouterr().out
    assert "4→1" in out and "70→78" in out


# ── Round 2 offline: the fact-check applied in memory ────────────────────────────────────────────────
def test_factcheck_body_uses_no_search_and_approves_only_fact_items():
    gen = BG(StubClaude(), None)
    seen = {}

    def _analyze(brand, body, meta):
        seen["search"] = gen._vx_search(brand, [], {}, {})
        seen["content"] = gen._verify_content(brand, body)
        return {"items": [{"id": "i1", "ref": "c1", "decision": {"approved": True}},
                          {"id": "i2", "ref": "c2", "decision": {"approved": False}},
                          {"id": "i3", "ref": "", "decision": {"approved": True}}]}

    def _apply(brand, body, meta, session, decisions=None):
        seen["decisions"] = decisions
        return {"body": body.replace("$49", "$59"), "meta_description": meta,
                "report": {"applied": [{"problem": "price"}], "refused": [], "not_approved": ["i2", "i3"]}}
    gen.verify_analyze = _analyze
    gen.verify_apply = _apply
    nb, nm, st = E.factcheck_body(gen, {"name": "Ledgerly"}, "Acme costs $49.", "m")
    assert nb == "Acme costs $59." and nm == "m"
    assert seen["search"] == {} and seen["content"] == ([], {})
    assert seen["decisions"] == {"i3": {"approved": False}}, "internal-defect items are not the fact-check"
    assert st["approved"] == 1 and st["applied"] == 1


# ── Round 2: the substance guard no longer re-adds a renamed section ─────────────────────────────────
def _section(md, head):
    lines = md.split("\n")
    i = next(k for k, ln in enumerate(lines) if ln.strip() == head)
    out = [lines[i]]
    for ln in lines[i + 1:]:
        if re.match(r"^#{2,3}\s", ln):
            break
        out.append(ln)
    return "\n".join(out).rstrip()


def _guard_case(stale_head, kept_head):
    """draft = the article with the STALE section; revised = the same article where the rewrite
    renamed it — built from the real Jolly export, which carries both versions."""
    body = _fixture("fu220_jolly_body.md")
    stale = _section(body, stale_head)
    revised = body.replace("\n\n---\n\n" + stale, "")      # the export minus the re-added stale copy
    assert stale not in revised and kept_head in revised
    draft = revised.replace(_section(revised, kept_head), stale)   # the draft had the stale version
    return draft, revised


def test_the_guard_does_not_re_add_a_renamed_section():
    draft, revised = _guard_case("### Seige Media (Siege Media)", "### Siege Media")
    out = BG(StubClaude(), None)._restore_dropped_sections(draft, revised)
    assert out == revised
    assert _new(out, "Jolly Search") == [h for h in _new(out, "Jolly Search")
                                         if h["check"] not in ("duplicate-section", "section-after-faq")]


def test_the_guard_still_restores_a_genuinely_deleted_section_before_the_faq():
    body = _fixture("fu220_jolly_body.md")
    loganix = _section(body, "### Loganix")
    revised = body.replace(loganix, "")
    out = BG(StubClaude(), None)._restore_dropped_sections(body, revised)
    assert "### Loganix" in out
    assert out.index("### Loganix") < out.index("## FAQ"), "restored substance goes before the FAQ"
    assert not any(h["check"] == "section-after-faq" for h in _new(out, "Jolly Search")
                   if "Loganix" in h["detail"])


def test_a_same_name_heading_with_different_content_is_still_restored():
    draft = ("## Acme CRM: Pricing\n\nAcme charges twelve dollars per seat, billed yearly, with volume "
             "discounts above fifty seats and a free tier capped at three users.\n\n## FAQ\n\n### Q?\n\nA.\n")
    revised = ("## Acme CRM: Integrations\n\nAcme connects to email, calendars and telephony through native "
               "connectors and an open webhook interface.\n\n## FAQ\n\n### Q?\n\nA.\n")
    out = BG(StubClaude(), None)._restore_dropped_sections(draft, revised)
    assert "## Acme CRM: Pricing" in out and out.index("Pricing") < out.index("## FAQ")


def test_the_cmk_hillsborough_rename_is_recognised():
    assert E.headings_match("S&W Kitchens: Kitchen-Focused Remodeling with Showroom Design",
                            "S&W Kitchens: Kitchen-Focused Remodeling")
    assert not E.headings_match("Step 1: Plan the budget", "Step 1: Book the permits")
