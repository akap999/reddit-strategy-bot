"""FU212 — content instructions per brand: your lines + the tool's auto lines, followed by every blog.

The operator asked for a place to steer content ("should source from AAP") plus an auto mode that fills
it in without ever touching their own lines. A source instruction can't work as prompt text alone —
the writer may only cite pages in the EVIDENCE — so the named organisation's pages are fetched first.
All $0: StubClaude, no network.
"""
import json
import os
import tempfile

import pytest

from generators import brand_enrichment as be
from generators.blog_gen import (BlogGenerator, _content_instructions, _content_instructions_block,
                                 _instruction_sources)
from tests.stubs import StubClaude

AAP_LINE = "Should source from AAP"
AAP_READ = {"text": AAP_LINE, "kind": "source",
            "sources": [{"name": "AAP", "domains": ["aap.org", "healthychildren.org"]}]}
PARENTS = {"text": 'Say "parents", never "moms"', "kind": "writing"}
FDA_AUTO = {"text": "Cite FDA for bottle material safety", "kind": "source",
            "sources": [{"name": "FDA", "domains": ["fda.gov"]}]}
COLIC_AUTO = {"text": "Attribute every colic claim", "kind": "writing"}
SEED = "best baby bottles for newborns"


def _brand(ctx, **kw):
    b = {"id": 7, "name": "Thyseed", "domain_url": "https://thyseed.com",
         "content_context": json.dumps(ctx)}
    b.update(kw)
    return b


CTX = {"mine": [AAP_READ, PARENTS], "auto": [FDA_AUTO, COLIC_AUTO], "dismissed": [],
       "auto_generated_at": "2026-09-17T00:00:00Z"}


# ── storage + merge rules ─────────────────────────────────────────────────────────────────────
def test_yours_come_first_and_duplicates_and_dismissed_auto_lines_are_dropped():
    ctx = {"mine": [AAP_READ], "auto": [dict(AAP_READ, text="should source from AAP."), FDA_AUTO, COLIC_AUTO],
           "dismissed": ["attribute every colic claim"]}
    got = be.ci_merged(be.ci_load(ctx))
    assert [(i["text"], i["origin"]) for i in got] == [(AAP_LINE, "yours"),
                                                       ("Cite FDA for bottle material safety", "auto")]


def test_caps_are_applied():
    ctx = {"mine": [f"rule {i}" for i in range(30)], "auto": [f"auto {i}" for i in range(20)]}
    loaded = be.ci_load(ctx)
    assert len(loaded["mine"]) == be.CI_MAX_MINE and len(loaded["auto"]) == be.CI_MAX_AUTO


def test_a_ui_edit_keeps_readings_and_dismisses_removed_auto_lines():
    merged = be.ci_merge_state(CTX, mine_texts=[AAP_LINE, "Cite FDA for bottle material safety", "New rule"],
                               auto_items=[])
    by = {m["text"]: m for m in merged["mine"]}
    assert by[AAP_LINE]["sources"][0]["domains"] == ["aap.org", "healthychildren.org"]
    assert by["Cite FDA for bottle material safety"]["kind"] == "source", "kept-as-mine lost its reading"
    assert by["New rule"]["kind"] == ""
    assert merged["auto"] == []
    assert merged["dismissed"] == ["attribute every colic claim"], \
        "a removed auto line must be dismissed; one moved to yours must not"
    assert 'say "parents", never "moms"' not in merged["dismissed"]


# ── reading lines + auto generation ───────────────────────────────────────────────────────────
def test_a_typed_domain_is_read_without_the_model():
    stub = StubClaude(call_handler=lambda p: pytest.fail("the model must not be called"))
    ctx = be.build_content_context(stub, _brand({"auto_generated_at": "x"}),
                                   mine_texts=["Should source from AAP (aap.org, healthychildren.org)"])
    assert ctx["mine"][0]["kind"] == "source"
    assert ctx["mine"][0]["sources"] == [{"name": "AAP", "domains": ["aap.org", "healthychildren.org"]}]
    assert stub.calls == []


def test_nothing_to_do_means_no_model_call():
    stub = StubClaude(call_handler=lambda p: pytest.fail("no call expected"))
    assert be.build_content_context(stub, _brand(CTX)) == be.ci_load(CTX)


def test_unread_lines_are_read_once_and_empty_domains_fall_back_to_domain_lookup():
    def handler(p):
        assert "READ each of the operator's lines" in p and "PROPOSE" not in p
        return {"read": [{"n": 1, "kind": "source", "sources": [{"name": "Mayo Clinic", "domains": []}]},
                         {"n": 2, "kind": "writing"}]}
    stub = StubClaude(call_handler=handler, official_domain=lambda b, c: "mayoclinic.org")
    ctx = be.build_content_context(stub, _brand({"mine": [AAP_READ], "auto_generated_at": "x"}),
                                   mine_texts=[AAP_LINE, "Cite Mayo Clinic for symptoms", "Keep it short"])
    by = {m["text"]: m for m in ctx["mine"]}
    assert by[AAP_LINE] == AAP_READ, "an unchanged line's reading must be reused, not re-read"
    assert by["Cite Mayo Clinic for symptoms"]["sources"] == [{"name": "Mayo Clinic", "domains": ["mayoclinic.org"]}]
    assert by["Keep it short"]["kind"] == "writing"
    assert len(stub.calls) == 1 and stub.domain_calls[0]["brand"] == "Mayo Clinic"


def test_regenerate_replaces_only_auto_and_never_reproposes_a_dismissed_line():
    seen = {}

    def handler(p):
        seen["prompt"] = p
        return {"auto": [{"text": "Cite FDA for bottle material safety", "kind": "source",
                          "sources": [{"name": "FDA", "domains": ["fda.gov"]}]},
                         {"text": "Attribute every colic claim", "kind": "writing"},
                         {"text": AAP_LINE, "kind": "source", "sources": []},
                         {"text": "Explain paced bottle feeding", "kind": "writing"}]}
    stored = dict(CTX, dismissed=["attribute every colic claim"])
    ctx = be.build_content_context(StubClaude(call_handler=handler), _brand(stored), regenerate_auto=True,
                                   vertical="medical")
    assert ctx["mine"] == be.ci_load(stored)["mine"], "your lines changed"
    assert [a["text"] for a in ctx["auto"]] == ["Cite FDA for bottle material safety",
                                                "Explain paced bottle feeding"]
    assert ctx["auto_generated_at"] != stored["auto_generated_at"]
    assert "attribute every colic claim" in seen["prompt"] and "NEVER propose" in seen["prompt"]
    assert "Regulated vertical: medical" in seen["prompt"]


def test_a_failed_call_leaves_the_stored_value_and_does_not_stamp():
    stored = {"mine": [PARENTS], "auto": [], "dismissed": []}
    ctx = be.build_content_context(StubClaude(call_handler=lambda p: {}), _brand(stored))
    assert ctx["auto"] == [] and ctx["auto_generated_at"] == ""


# ── fetching pages for source instructions ────────────────────────────────────────────────────
def _search(results):
    def handler(brief, allowed, blocked):
        return results.get(tuple(allowed or ()), [])
    return handler


def test_a_source_instruction_searches_its_own_sites_and_labels_pages_honestly():
    results = {("aap.org", "healthychildren.org"): [
        {"title": "Choosing baby bottles", "url": "https://www.healthychildren.org/English/bottles.aspx",
         "fact": "Glass and plastic bottles are both safe for newborns."},
        {"title": "AAP vaccine schedule", "url": "https://www.aap.org/vaccines", "fact": "Immunization."},
        {"title": "Bottle feeding newborns", "url": "https://www.tiktok.com/aap-bottles", "fact": "x"}]}
    brand = _brand({"mine": [AAP_READ, {"text": "Use BabyGearLab for bottle testing", "kind": "source",
                                        "sources": [{"name": "BabyGearLab", "domains": ["babygearlab.com"]}]}]})
    results[("babygearlab.com",)] = [{"title": "Best baby bottles tested",
                                      "url": "https://www.babygearlab.com/topics/feeding/best-baby-bottle",
                                      "fact": "We tested 20 bottles."}]
    stub = StubClaude(search_handler=_search(results))
    gen = BlogGenerator(stub, None)
    blocks = gen._instruction_source_blocks(brand, SEED)
    assert {tuple(s["allowed"]) for s in stub.searches} == {("aap.org", "healthychildren.org"), ("babygearlab.com",)}
    labels = {b["url"]: b["label"] for b in blocks}
    assert labels["https://www.healthychildren.org/English/bottles.aspx"].startswith("official · ")
    assert labels["https://www.babygearlab.com/topics/feeding/best-baby-bottle"].startswith("preferred · BabyGearLab · ")
    assert "https://www.aap.org/vaccines" not in labels, "an off-topic page was kept"
    assert "https://www.tiktok.com/aap-bottles" not in labels, "a page off the named sites was kept"


def test_at_most_three_organisations_and_yours_first():
    mine = [{"text": f"Source from Org{i}", "kind": "source",
             "sources": [{"name": f"Org{i}", "domains": [f"org{i}.org"]}]} for i in range(5)]
    got = _instruction_sources(_brand({"mine": mine[:2], "auto": mine[2:]}))
    assert [g["name"] for g in got] == ["Org0", "Org1", "Org2"]
    assert [g["origin"] for g in got] == ["yours", "yours", "auto"]


def test_a_ymyl_brand_does_not_search_an_organisation_the_official_legs_already_cover():
    stub = StubClaude(search_handler=_search({}))
    gen = BlogGenerator(stub, None)
    gen._ci_ymyl = "medical"
    gen._instruction_source_blocks(_brand({"mine": [AAP_READ], "auto": [FDA_AUTO]}), SEED)
    assert [tuple(s["allowed"]) for s in stub.searches] == [("aap.org", "healthychildren.org")]


def test_no_instructions_means_no_search():
    stub = StubClaude(search_handler=lambda *a: pytest.fail("no search expected"))
    assert BlogGenerator(stub, None)._instruction_source_blocks({"name": "Thyseed"}, SEED) == []


# ── the instructions reach every writer ──────────────────────────────────────────────────────
ART = {"title": SEED, "body_markdown": "# best baby bottles for newborns\n\n## Quick answer\n\nGlass [S1].\n"}


def _writers(brand):
    handler = lambda p: {"title": SEED, "meta_description": "m", "keywords": ["k"], "body_markdown": "# x\n",
                         "disclosure": "d", "revised_body_markdown": "# x\n", "flagged": [],
                         "linkedin_text": "post", "script_markdown": "s"}
    prompts = {}
    gen = BlogGenerator(StubClaude(call_handler=handler), None)
    gen.generate_article(brand, SEED)
    prompts["article"] = gen._article_prompt
    for name, fn in (("verify", lambda g: g.verify_claims(brand, dict(ART))),
                     ("reconcile", lambda g: g._reconcile_and_finish(brand, SEED, dict(ART), {
                         "name": "Thyseed", "tools": ["Comotomo"], "dims": ["Material"], "claims": [],
                         "fresh": [{"label": "Comotomo", "url": "https://comotomo.com", "text": "silicone"}]})),
                     ("linkedin", lambda g: g.generate_linkedin(brand, SEED, dict(ART))),
                     ("linkedin_article", lambda g: g.generate_linkedin_article(brand, dict(ART))),
                     ("youtube", lambda g: g.generate_youtube_script(brand, dict(ART)))):
        g = BlogGenerator(StubClaude(call_handler=handler), None)
        g._evidence_blocks = []
        fn(g)
        prompts[name] = g.claude.calls[0]
    return prompts


def test_every_writer_prompt_carries_the_instructions_with_yours_and_auto_tags():
    for name, p in _writers(_brand(CTX)).items():
        assert "CONTENT INSTRUCTIONS for Thyseed" in p, name
        assert "[yours] Should source from AAP" in p and '[yours] Say "parents", never "moms"' in p, name
        assert "[auto] Attribute every colic claim" in p, name
        assert "never invent" in p.lower() or "never attribute anything else" in p, name
    blog = _writers(_brand(CTX))["article"]
    assert "cite the pages from aap.org / healthychildren.org in the EVIDENCE" in blog


def test_no_instructions_means_no_block_anywhere():
    for name, p in _writers({"name": "Thyseed", "domain_url": "https://thyseed.com"}).items():
        assert "CONTENT INSTRUCTIONS" not in p, name
    assert _content_instructions_block({"name": "x"}) == ""


class _CapWriter:
    def __init__(self):
        self.prompts = []

    def probe(self, timeout=8):
        return {"state": "ok"}

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        self.prompts.append(prompt)
        return ""


def test_the_watermark_rewrite_keeps_following_the_writing_instructions_only():
    w = _CapWriter()
    gen = BlogGenerator(StubClaude(), db=None, writer=w, writer_mode="rewrite")
    gen._evidence_blocks = []
    body = "# t\n\n## Quick answer\n\nParents like glass bottles [S1].\n"
    gen._apply_writer_pass({"body_markdown": body}, body, _brand(CTX), SEED)
    p = w.prompts[0]
    assert "KEEP following every one while you reword" in p and 'Say "parents", never "moms"' in p
    assert AAP_LINE not in p, "a source line has no place in a rewrite prompt"
    w2 = _CapWriter()
    gen2 = BlogGenerator(StubClaude(), db=None, writer=w2, writer_mode="rewrite")
    gen2._evidence_blocks = []
    gen2._apply_writer_pass({"body_markdown": body}, body, {"name": "Thyseed"}, SEED)
    assert "CONTENT INSTRUCTIONS" not in w2.prompts[0]


def test_the_content_check_asks_about_writing_instructions_and_fixes_them():
    stub = StubClaude(call_handler=lambda p: {"issues": [], "assessment": {}})
    gen = BlogGenerator(stub, None)
    gen._verify_content(_brand(CTX), "# t\n\nMoms love glass.\n")
    assert "- instruction: the article breaks" in stub.calls[0] and 'Say "parents", never "moms"' in stub.calls[0]
    assert "Should source from AAP" not in stub.calls[0]
    assert "instruction" in BlogGenerator._VF_FIXABLE
    stub2 = StubClaude(call_handler=lambda p: {"issues": [], "assessment": {}})
    BlogGenerator(stub2, None)._verify_content({"name": "Thyseed"}, "# t\n\nMoms love glass.\n")
    assert "instruction" not in stub2.calls[0]


# ── the unmet-source warning ─────────────────────────────────────────────────────────────────
def _body(cite_n, sources):
    return ("# t\n\nGlass bottles are safe" + (f" [S{cite_n}]" if cite_n else "") + ".\n\n## Sources\n"
            + "".join(f"- [S{i}] {lab} — <{u}>\n" for i, (lab, u) in enumerate(sources, 1)))


def test_warns_when_no_page_was_found_and_when_found_but_not_cited_and_is_silent_when_cited():
    brand = _brand({"mine": [AAP_READ]})
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = [{"label": "Thyseed", "url": "https://thyseed.com", "text": "x"}]
    none = gen._content_instruction_warnings(brand, _body(1, [("Thyseed", "https://thyseed.com")]))
    assert len(none) == 1 and "no page from aap.org / healthychildren.org" in none[0]

    aap = "https://www.healthychildren.org/English/bottles.aspx"
    gen._evidence_blocks.append({"label": "official · Choosing bottles", "url": aap, "text": "y"})
    uncited = gen._content_instruction_warnings(
        brand, _body(1, [("Thyseed", "https://thyseed.com"), ("official · Choosing bottles", aap)]))
    assert len(uncited) == 1 and "found 1 page(s)" in uncited[0] and "cites none" in uncited[0]

    cited = gen._content_instruction_warnings(
        brand, _body(2, [("Thyseed", "https://thyseed.com"), ("official · Choosing bottles", aap)]))
    assert cited == []


# ── API ───────────────────────────────────────────────────────────────────────────────────────
@pytest.fixture
def tmp_app(monkeypatch):
    import app as appmod
    from db import Database
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Thyseed", domain_url="https://thyseed.com", category="baby bottles")
    db.close()
    appmod.DB_PATH = path
    appmod._db_initialized = True
    yield appmod, path, bid
    os.unlink(path)


def _read(path, bid):
    from db import Database
    db = Database(path)
    db.connect()
    try:
        return db.get_brand(bid)
    finally:
        db.close()


def test_save_merges_readings_and_dismisses_removed_auto_lines(tmp_app):
    appmod, path, bid = tmp_app
    from db import Database
    db = Database(path)
    db.connect()
    db.update_brand(bid, content_context=json.dumps(CTX))
    db.close()
    r = appmod.app.test_client().put(f"/api/brands/{bid}", json={"content_context": {
        "mine": [AAP_LINE, "Cite FDA for bottle material safety"], "auto": [], "dismissed": []}})
    assert r.status_code == 200
    ctx = be.ci_load(_read(path, bid)["content_context"])
    assert ctx["mine"][0] == AAP_READ and ctx["mine"][1]["kind"] == "source"
    assert ctx["auto"] == [] and ctx["dismissed"] == ["attribute every colic claim"]


def test_regenerate_endpoint_keeps_your_lines(tmp_app, monkeypatch):
    appmod, path, bid = tmp_app
    stub = StubClaude(call_handler=lambda p: {"auto": [{"text": "Cite CDC for sterilizing", "kind": "source",
                                                        "sources": [{"name": "CDC", "domains": ["cdc.gov"]}]}]})
    monkeypatch.setattr(appmod, "ClaudeClient", lambda *a, **k: stub)
    r = appmod.app.test_client().post(f"/api/brands/{bid}/content-context/regenerate",
                                      json={"mine": [AAP_LINE + " (aap.org)"], "auto": [], "dismissed": []})
    body = r.get_json()
    assert r.status_code == 200 and "warning" not in body
    ctx = be.ci_load(_read(path, bid)["content_context"])
    assert [m["text"] for m in ctx["mine"]] == [AAP_LINE + " (aap.org)"]
    assert [a["text"] for a in ctx["auto"]] == ["Cite CDC for sterilizing"]


def test_lazy_fill_runs_once_and_then_costs_nothing(tmp_app):
    appmod, path, bid = tmp_app
    from db import Database
    db = Database(path)
    db.connect()
    try:
        stub = StubClaude(call_handler=lambda p: {"auto": [COLIC_AUTO]})
        brand = appmod._ensure_content_context(stub, db, db.get_brand(bid))
        assert be.ci_load(brand["content_context"])["auto"][0]["text"] == COLIC_AUTO["text"]
        again = StubClaude(call_handler=lambda p: pytest.fail("second generation must not call the model"))
        appmod._ensure_content_context(again, db, brand)
    finally:
        db.close()


def test_auto_analyze_draft_carries_auto_instructions(tmp_app, monkeypatch):
    appmod, path, bid = tmp_app
    monkeypatch.setattr(be, "enrich_brand", lambda c, n, d: {"name": "Thyseed", "category": "baby bottles"})
    monkeypatch.setattr(be, "generate_brand_personas", lambda *a, **k: [])
    monkeypatch.setattr(be, "fetch_brand_byline_logo", lambda *a, **k: {})
    monkeypatch.setattr(appmod, "ClaudeClient",
                        lambda *a, **k: StubClaude(call_handler=lambda p: {"auto": [COLIC_AUTO]}))
    box = {}

    def _inline(_name, fn, **kw):
        box["result"] = fn()
        return "t"
    monkeypatch.setattr(appmod, "start_task", _inline)
    appmod.app.test_client().post("/api/brands/enrich", json={"domain_url": "https://thyseed.com"})
    assert box["result"]["content_context"]["auto"][0]["text"] == COLIC_AUTO["text"]


def test_the_generate_task_reads_new_lines_before_writing():
    import inspect
    import app as appmod
    src = inspect.getsource(appmod)
    assert src.count("brand = _ensure_content_context(claude, bg, brand)") == 3


# ── wiring: the fetch runs inside evidence gathering, the warning inside finalize (resume path) ─
def test_evidence_gathering_fetches_the_instruction_sources():
    aap = "https://www.healthychildren.org/English/bottles.aspx"
    stub = StubClaude(search_handler=_search({("aap.org", "healthychildren.org"): [
        {"title": "Choosing baby bottles", "url": aap, "fact": "Glass and plastic baby bottles are both safe."}]}))
    gen = BlogGenerator(stub, None)
    evidence = gen._gather_evidence({"name": "Thyseed", "content_context": json.dumps({"mine": [AAP_READ]})}, SEED)
    assert any(b["url"] == aap and b["label"].startswith("official · ") for b in gen._evidence_blocks)
    assert aap in evidence


def test_a_resumed_blog_that_cites_no_aap_page_carries_the_warning():
    from tests.test_fu210_your_competitors import _zeta_ck
    gen = BlogGenerator(StubClaude(), None)
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": "# t\n\nBravo and Delta only.\n",
                                                     "flagged": []}
    ck = _zeta_ck(["Bravo", "Delta", "Echo", "Zeta"])
    ck["sourcing"]["unsourced"] = []
    ck["sourcing"]["fresh"] = [{"label": "Bravo", "url": "https://bravo.example", "text": "t"}]
    ck["article"]["body_markdown"] = ck["draft_body"] = "# t\n\nBravo and Delta only.\n"
    art = gen.finish_pending_blog(_brand({"mine": [AAP_READ]}, name="Acme"), "t", ck, [])
    assert any(w["check"] == "content-instruction" and "Should source from AAP" in w["detail"]
               for w in art["warnings"])
