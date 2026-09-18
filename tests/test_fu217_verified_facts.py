"""FU217 — the operator's VERIFIED FACTS for their own brand and the competitors it names, used word
for word wherever a blog names those brands.

The case that started it: CMK Construction's operator had licence numbers, licence classes,
certifications, BuildZoom scores, a legal-name correction and a franchise note for CMK and four
competitors. Pasted into Content instructions, most of it vanished on save (20-line cap, near-duplicate
merge, 300-character cut) and what survived was framed as a rule, not a fact. The real paste is the
fixture here (tests/fixtures/fu217_cmk_verified_paste.txt).

The operator's four decisions, each locked below:
  1. a verified fact may cite ANY source the operator gives (BuildZoom, BBB, the state registry),
     the brand's own site preferred when it states the fact;
  2. an unreadable source is cited anyway, with a warning;
  3. a differing legal name reads "Trade (licensed as Legal)" on first mention, the trade name after;
  4. the operator's own wording ships verbatim — "verify with the specific franchisee" included —
     while the punt ban still applies to everything the model writes.

Everything is $0: StubClaude, temp SQLite, stubbed page fetches.
"""
import json
import os
import tempfile

import pytest

import generators.blog_gen as bgmod
from db import Database
from generators.blog_gen import (BlogGenerator as BG, _vfact_clean, _vfact_entries, _vfact_tokens,
                                 _vfact_prompt_block, _vfact_legal_differs, _vfact_license_hits,
                                 _kf_slug)
from generators.brand_enrichment import ci_mine_report
from tests.stubs import StubClaude

_FIX = os.path.join(os.path.dirname(__file__), "fixtures", "fu217_cmk_verified_paste.txt")
PASTE = open(_FIX, encoding="utf-8").read()

KNOWN = ["Revive Kitchen & Bath", "S&W Kitchens", "Bath World", "Bath Fitter"]
FRANCHISE = "franchise model; licensing held by local operator, verify with the specific franchisee."

# What a well-behaved parse of the real paste looks like (spacing deliberately lost on CMK's first
# licence — the model's habit — so the gate's respacing is exercised).
PARSED = [
    {"name": "CMK Construction, Inc.", "legal_name": "CMK Construction, Inc.",
     "sources": [{"url": "cmkconstructioninc.com/awards-licenses", "name": ""},
                 {"url": "", "name": "BBB profile"}, {"url": "", "name": "BuildZoom"}],
     "rows": [{"fact": "CGC1516665 — State Certified General Contractor", "kind": "license"},
              {"fact": "CFC 1430533 — State Certified Plumbing Contractor", "kind": "license"},
              {"fact": "ER 13016498 — Registered Electrical Contractor", "kind": "license"},
              {"fact": "Also a licensed roofing contractor, GAF-certified, EPA Lead-Safe certified.",
               "kind": "certification"},
              {"fact": "BuildZoom score 106, top 7% of Florida contractors.", "kind": "rating"}]},
    {"name": "Revive Design and Renovation", "trade_name": "Revive Kitchen & Bath",
     "legal_name": "Revive Design and Renovation",
     "rows": [{"fact": "CBC1264856 — Certified Building Contractor, Current/Active, licensed 07/22/2021",
               "kind": "license"},
              {"fact": "The page's name is wrong — it says \"Revive Kitchen & Bath\" throughout; the "
                       "licensed entity is Revive Design and Renovation.",
               "kind": "correction", "wrong": "Revive Kitchen & Bath"}]},
    {"name": "S&W Kitchens, Inc.",
     "rows": [{"fact": "CBC1262059 — Certified Building Contractor (confirmed on their own site footer, "
                       "BBB, and Houzz)", "kind": "license"},
              {"fact": "Correction needed: the page calls CBC1262059 a \"General Building Contractor\" "
                       "licence.", "kind": "correction", "wrong": "General Building Contractor"}]},
    {"name": "Bath World LLC", "aliases": ["Bath World and More"],
     "rows": [{"fact": "CRC1333808 — Certified Residential Contractor", "kind": "license"}]},
    {"name": "Bath Fitter", "rows": [{"fact": FRANCHISE, "kind": "note"}]},
    {"name": "Re-Bath", "rows": [{"fact": FRANCHISE, "kind": "note"}]},
]


def _clean(parsed=PARSED, paste=PASTE):
    return _vfact_clean(parsed, known_names=KNOWN, subject="CMK Construction", verbatim_text=paste)


BRAND = {
    "id": 11, "name": "CMK Construction", "domain_url": "https://cmkconstructioninc.com",
    "category": "kitchen and bathroom remodeling contractor",
    "competitors": json.dumps(KNOWN),
}


def _brand(store=None, **kw):
    b = dict(BRAND, **kw)
    if store is None:
        store = _clean()[0]
    b["verified_facts"] = json.dumps(store) if not isinstance(store, str) else store
    return b


def _joined(article):
    return " | ".join(w.get("detail", "") for w in (article.get("warnings") or []))


# ── the parse: the model parses, CODE decides ────────────────────────────────────────────────
def test_a_licence_number_keeps_the_spacing_you_typed():
    m, dropped, _f, _o = _clean()
    facts = [r["fact"] for r in m["cmk-construction"]["rows"]]
    assert facts[0].startswith("CGC 1516665 "), "the model's 'CGC1516665' must be re-spaced to the paste"
    assert any(f.startswith("ER 13016498") for f in facts)
    # Revive's number was typed WITHOUT a space and stays that way
    assert m["revive-kitchen-bath"]["rows"][0]["fact"].startswith("CBC1264856 ")


def test_an_invented_licence_number_and_a_reworded_row_are_dropped_with_a_reason():
    parsed = json.loads(json.dumps(PARSED))
    parsed[0]["rows"] += [{"fact": "CGC 1516666 — made up", "kind": "license"},
                          {"fact": "A leading remodeler across the region", "kind": "note"}]
    m, dropped, _f, _o = _clean(parsed)
    whys = {d["raw"]: d["why"] for d in dropped}
    assert "not in the text you pasted" in whys["CGC 1516666 — made up"]
    assert "reworded" in whys["A leading remodeler across the region"]
    assert all("1516666" not in r["fact"] for r in m["cmk-construction"]["rows"])


def test_revive_keeps_its_trade_name_with_the_legal_name_and_cmk_and_bath_world_get_none():
    m, dropped, flagged, _o = _clean()
    assert m["revive-kitchen-bath"]["name"] == "Revive Kitchen & Bath"
    assert m["revive-kitchen-bath"]["legal_name"] == "Revive Design and Renovation"
    assert m["cmk-construction"]["legal_name"] == "" and m["cmk-construction"]["subject"] is True
    assert m["bath-world"]["legal_name"] == "" and m["bath-world"]["aliases"] == ["Bath World and More"]
    # the naming note becomes the trade + legal name, never a banned phrase ("Revive Kitchen & Bath")
    assert all(r.get("wrong") != "Revive Kitchen & Bath" for r in m["revive-kitchen-bath"]["rows"])
    assert any("explains the brand's name" in d["why"] for d in dropped)


def test_the_suffix_only_legal_name_never_differs_but_a_new_name_does():
    assert not _vfact_legal_differs("CMK Construction", "CMK Construction, Inc.")
    assert not _vfact_legal_differs("Bath World", "Bath World LLC")
    assert _vfact_legal_differs("Revive Kitchen & Bath", "Revive Design and Renovation")


def test_bath_fitter_and_re_bath_are_two_entries_and_an_unknown_name_is_flagged_not_dropped():
    m, _d, flagged, _o = _clean()
    assert {"bath-fitter", "re-bath"} <= set(m)
    assert m["bath-fitter"]["rows"][0]["fact"] == FRANCHISE == m["re-bath"]["rows"][0]["fact"]
    assert flagged == ["Re-Bath"], "Re-Bath is not on the brand's competitor list — flagged, kept"


def test_the_sources_line_becomes_sources_and_a_link_not_in_the_paste_is_refused():
    parsed = json.loads(json.dumps(PARSED))
    parsed[0]["sources"].append({"url": "https://invented.example/cmk", "name": ""})
    m, dropped, _f, _o = _clean(parsed)
    srcs = m["cmk-construction"]["sources"]
    assert {"url": "https://cmkconstructioninc.com/awards-licenses", "name": ""} in srcs
    assert {"url": "", "name": "BBB profile"} in srcs
    assert not any("invented.example" in s["url"] for s in srcs)
    assert any("link is not in the text" in d["why"] for d in dropped)


def test_a_correction_keeps_its_wrong_phrase():
    m, _d, _f, _o = _clean()
    rows = m["s-w-kitchens"]["rows"]
    assert any(r["kind"] == "correction" and r["wrong"] == "General Building Contractor" for r in rows)


def test_every_cap_reports_a_reason_and_a_long_fact_is_dropped_not_cut():
    ent = {"name": "Bath World", "rows": [{"fact": f"Fact number {i}", "kind": "note"} for i in range(17)]
           + [{"fact": "x" * 260, "kind": "note"}]}
    m, dropped, _f, _o = _vfact_clean([ent], known_names=KNOWN, subject="CMK Construction")
    assert len(m["bath-world"]["rows"]) == 15
    assert sum("more than 15 facts" in d["why"] for d in dropped) == 2
    assert any("longer than 240" in d["why"] for d in dropped)


def test_a_subject_row_already_in_brand_facts_is_reported_as_an_overlap():
    _m, _d, _f, overlaps = _vfact_clean(
        PARSED[:1], known_names=KNOWN, subject="CMK Construction", verbatim_text=PASTE,
        brand_fact_lines=["BuildZoom score 106, top 7% of Florida contractors"])
    assert overlaps == ["BuildZoom score 106, top 7% of Florida contractors."]


def test_every_bad_stored_shape_reads_as_nothing():
    for raw in (None, "", "{}", "[]", "garbage", json.dumps({"x": {"name": "Solo", "rows": []}})):
        assert _vfact_entries({"name": "CMK Construction", "verified_facts": raw}) == []
        assert _vfact_prompt_block({"name": "CMK Construction", "verified_facts": raw}, "article", []) == ""


# ── storage and the endpoints ────────────────────────────────────────────────────────────────
def _db():
    d = tempfile.mkdtemp()
    db = Database(os.path.join(d, "t.db"))
    db.initialize()
    for s in (db.create_subreddit("s", "d"), db.create_subreddit("s2", "d2")):
        db.conn.execute("INSERT INTO brands (subreddit_id,name,context,competitors) VALUES (?,?,?,?)",
                        (s, "CMK Construction", "c", json.dumps(KNOWN)))
    db.conn.commit()
    return db


def _with_db(db, fn, claude=None):
    import app as _app
    orig = (_app.get_db, _app.ClaudeClient, _app.ANTHROPIC_API_KEY)
    _app.get_db = lambda: db
    if claude is not None:
        _app.ClaudeClient = lambda *a, **k: claude
    _app.ANTHROPIC_API_KEY = "test-key"
    db.close_real, db.close = db.close, (lambda: None)
    try:
        _app.app.config["TESTING"] = True
        return fn(_app.app.test_client())
    finally:
        _app.get_db, _app.ClaudeClient, _app.ANTHROPIC_API_KEY = orig
        db.close = db.close_real


def test_the_parse_endpoint_makes_one_call_no_search_and_returns_entries_subject_first():
    db = _db()
    stub = StubClaude(call_handler=lambda p: {"brands": PARSED})
    r = _with_db(db, lambda c: c.post("/api/brands/1/verified-facts/parse", json={"text": PASTE}), stub)
    body = r.get_json()
    assert r.status_code == 200 and len(stub.calls) == 1 and stub.searches == []
    assert body["entities"][0]["name"] == "CMK Construction" and body["entities"][0]["subject"]
    assert "copy every" in stub.calls[0].lower() and "CGC 1516665" in stub.calls[0]
    assert db.get_brand(1).get("verified_facts") in (None, ""), "the parse saves nothing"
    db.close()


def test_the_parse_endpoint_refuses_an_empty_paste():
    db = _db()
    r = _with_db(db, lambda c: c.post("/api/brands/1/verified-facts/parse", json={"text": " "}))
    assert r.status_code == 400
    db.close()


def test_saving_fans_out_to_every_copy_and_the_list_sent_is_the_complete_list():
    db = _db()
    ents = [{"name": "CMK Construction", "subject": True,
             "rows": [{"fact": "CGC 1516665 — State Certified General Contractor", "kind": "license"}]},
            {"name": "Revive Kitchen & Bath", "legal_name": "Revive Design and Renovation",
             "rows": [{"fact": "CBC1264856 — Certified Building Contractor", "kind": "license"}]}]
    r = _with_db(db, lambda c: c.put("/api/brands/1/verified-facts", json={"entities": ents}))
    assert r.status_code == 200 and r.get_json()["rows"] == 2
    for bid in (1, 2):
        assert set(json.loads(db.get_brand(bid)["verified_facts"])) == {"cmk-construction",
                                                                        "revive-kitchen-bath"}
    _with_db(db, lambda c: c.put("/api/brands/1/verified-facts", json={"entities": ents[:1]}))
    assert set(json.loads(db.get_brand(2)["verified_facts"])) == {"cmk-construction"}
    db.close()


def test_edit_brand_and_clear_cached_data_never_touch_the_verified_facts():
    db = _db()
    db.update_brand(1, verified_facts=json.dumps(_clean()[0]))
    before = db.get_brand(1)["verified_facts"]
    _with_db(db, lambda c: c.put("/api/brands/1", json={"context": "new"}))
    _with_db(db, lambda c: c.post("/api/brands/1/reset-cached-data"))
    assert db.get_brand(1)["verified_facts"] == before
    db.close()


# ── Content instructions: no line lost silently ──────────────────────────────────────────────
def test_the_content_instructions_report_counts_dropped_merged_and_cut_lines():
    lines = [f"Rule {i}" for i in range(22)] + ["rule 3."] + ["y" * 320]
    rep = ci_mine_report(lines)
    assert rep["kept"] == 20 and rep["limit"] == 20
    assert rep["dropped"] == ["Rule 20", "Rule 21", "y" * 320]
    assert rep["duplicates"] == ["rule 3."]
    assert rep["truncated"] == []   # the long line is past 20, so it is dropped rather than cut
    assert ci_mine_report(["x" * 310])["truncated"] == ["x" * 310]


def test_the_edit_brand_save_reports_content_instructions_only_when_something_is_lost():
    db = _db()
    ok = _with_db(db, lambda c: c.put("/api/brands/1", json={"content_context": {
        "mine": ["Say parents"], "auto": [], "dismissed": []}})).get_json()
    assert "content_instructions" not in ok
    over = _with_db(db, lambda c: c.put("/api/brands/1", json={"content_context": {
        "mine": [f"line {i}" for i in range(25)], "auto": [], "dismissed": []}})).get_json()
    assert len(over["content_instructions"]["dropped"]) == 5
    db.close()


# ── evidence: appended last, cited to the best page, warned when unconfirmed ─────────────────
PAGES = {
    "https://cmkconstructioninc.com": "CMK Construction. Licensed CGC 1516665. Kitchens and baths.",
    "https://cmkconstructioninc.com/awards-licenses": ("Awards and licenses. CGC 1516665, CFC 1430533, "
                                                       "ER 13016498. GAF certified roofing."),
}


def _gather(brand, pages=None, **kw):
    pages = PAGES if pages is None else pages
    g = BG(StubClaude(), db=None)
    for k, v in kw.items():
        setattr(g, k, v)
    calls = []

    def _fetch(url):
        calls.append(url)
        return pages.get(url, "")
    g._fetch_url = _fetch
    ev = g._gather_evidence(brand, "kitchen remodel contractors in Tampa")
    return g, ev, calls


def _store(**overrides):
    base = _clean()[0]
    base["revive-kitchen-bath"]["sources"] = [{"url": "https://www.buildzoom.com/contractor/revive",
                                               "name": "BuildZoom"}]
    base.update(overrides)
    return base


def test_no_stored_facts_leaves_the_evidence_byte_identical():
    plain = dict(BRAND)
    g0, ev0, _ = _gather(plain)
    for raw in (None, "", "{}", "garbage"):
        g1, ev1, _ = _gather(dict(plain, verified_facts=raw))
        assert ev1 == ev0 and g1._evidence_blocks == g0._evidence_blocks
        assert g1._vfact_note == ""


def test_fact_blocks_are_appended_after_every_existing_block():
    g0, _ev0, _ = _gather(dict(BRAND))
    g1, ev1, _ = _gather(_brand(_store()))
    n = len(g0._evidence_blocks)
    assert g1._evidence_blocks[:n] == g0._evidence_blocks, "no existing [S#] may move"
    assert all(b["label"].startswith("fact · ") for b in g1._evidence_blocks[n:])
    assert "fact · Revive Kitchen & Bath · buildzoom.com" in ev1


def test_the_brands_own_page_is_cited_when_it_states_the_fact():
    g, _ev, _ = _gather(_brand(_store()))
    cmk = [b for b in g._evidence_blocks if b["label"].startswith("fact · CMK Construction")]
    home = [b for b in cmk if b["url"] == "https://cmkconstructioninc.com"]
    assert home and "CGC 1516665" in home[0]["text"], "the homepage states CGC 1516665 — cite it"
    other = [b for b in cmk if b["url"].endswith("/awards-licenses")]
    assert other and "CFC 1430533" in other[0]["text"]


def test_an_unreadable_link_is_cited_anyway_with_a_warning():
    g, _ev, _ = _gather(_brand(_store()))
    rev = [b for b in g._evidence_blocks if b["label"] == "fact · Revive Kitchen & Bath · buildzoom.com"]
    assert rev and rev[0]["url"] == "https://www.buildzoom.com/contractor/revive"
    assert "(licensed as Revive Design and Renovation)" in rev[0]["text"]
    assert "Revive Kitchen & Bath: couldn't read buildzoom.com" in g._vfact_note


def test_a_page_read_that_does_not_show_the_fact_is_cited_with_the_other_warning():
    pages = dict(PAGES, **{"https://www.buildzoom.com/contractor/revive": "Revive. Great reviews."})
    g, _ev, _ = _gather(_brand(_store()), pages=pages)
    assert "buildzoom.com was read but doesn't show the fact" in g._vfact_note


def test_a_fact_with_no_link_gets_no_block_is_marked_no_source_and_warned():
    g, _ev, _ = _gather(_brand(_store()))
    assert not [b for b in g._evidence_blocks if "S&W Kitchens" in b["label"]]
    assert "S&W Kitchens: no link given" in g._vfact_note
    block = _vfact_prompt_block(_brand(_store()), "article", g._evidence_blocks)
    sw = block.split("- S&W Kitchens:")[1].split("\n- ")[0]
    assert "(no source" in sw


def test_the_source_fetch_is_capped_at_eight_pages():
    ent = {"name": "Bath World", "rows": [
        {"fact": f"CRC13338{i:02d} — row", "kind": "license", "source_url": f"https://r{i}.example/p"}
        for i in range(12)]}
    store, _d, _f, _o = _vfact_clean([ent], known_names=KNOWN, subject="CMK Construction")
    g = BG(StubClaude(), db=None)
    seen = []
    g._fetch_url = lambda u: seen.append(u) or ""
    blocks = g._vfact_blocks(_brand(store), [], {})
    assert len(seen) == 8 and len(blocks) == 12


def test_a_general_guide_uses_only_the_subjects_facts_minus_the_service_area():
    store = _store()
    store["cmk-construction"]["rows"].append(
        {"fact": "Design Studio in Tampa, 4,000 sq ft", "kind": "note", "wrong": "", "source_url": ""})
    g, _ev, _ = _gather(_brand(store), _guide=True, _guide_places=["Tampa", "Tampa Bay"])
    labels = [b["label"] for b in g._evidence_blocks if b["label"].startswith("fact ·")]
    assert labels and all("CMK Construction" in x for x in labels)
    assert not any("Design Studio" in b["text"] for b in g._evidence_blocks)
    assert "Revive" not in _vfact_prompt_block(_brand(store), "article", g._evidence_blocks, guide=True)


def test_the_guide_check_catches_a_competitor_named_only_by_its_legal_name():
    names = BG._guide_competitor_names(_brand(_store()))
    assert "Revive Design and Renovation" in names and "Bath World and More" in names


# ── the three writers ────────────────────────────────────────────────────────────────────────
def _blocks_after_gather():
    g, _ev, _ = _gather(_brand(_store()))
    return g._evidence_blocks


def test_the_article_prompt_carries_the_facts_their_citations_and_the_overrides():
    blocks = _blocks_after_gather()
    g = BG(StubClaude(call_handler=lambda p: {"title": "t", "meta_description": "m", "keywords": [],
                                              "body_markdown": "# t\n", "disclosure": "d"}), db=None)
    g._evidence_blocks = blocks
    g.generate_article(_brand(_store()), "kitchen remodel contractors in Tampa")
    p = g._article_prompt
    n = next(i for i, b in enumerate(blocks, 1) if b["label"] == "fact · Revive Kitchen & Bath · buildzoom.com")
    assert "VERIFIED FACTS (supplied by the publisher" in p
    assert f"CBC1264856 — Certified Building Contractor, Current/Active, licensed 07/22/2021 [S{n}]" in p
    assert "\"Revive Kitchen & Bath (licensed as Revive Design and Renovation)\"" in p
    assert "OVERRIDE, for these facts only" in p
    assert "never write \"General Building Contractor\" about S&W Kitchens" in p
    assert "(publisher's note — use this wording as written" in p
    assert "comparison-table column for licensing is allowed only when EVERY compared" in p
    assert "never add a brand to the article" in p


def test_the_fact_check_is_told_the_facts_are_supported():
    blocks = _blocks_after_gather()
    stub = StubClaude(call_handler=lambda p: {"revised_body_markdown": "x", "flagged": []})
    g = BG(stub, db=None)
    g._evidence_blocks = blocks
    g.verify_claims(_brand(_store()), {"body_markdown": "# t\n\nCMK Construction holds CGC1516665."})
    assert "These facts are SUPPORTED — never hedge" in stub.calls[-1]


def test_the_reconcile_carries_the_block_and_drops_a_removed_brand():
    from tests.prompt_capture import ARTICLE, SOURCING
    blocks = _blocks_after_gather()
    stub = StubClaude(call_handler=lambda p: {"revised_body_markdown": "x", "flagged": []})
    g = BG(stub, db=None)
    g._evidence_blocks = blocks
    g._removed_brands = ["Revive Kitchen & Bath"]
    g._reconcile_and_finish(_brand(_store()), "seed", dict(ARTICLE), json.loads(json.dumps(SOURCING)))
    p = stub.calls[-1]
    assert "VERIFIED FACTS (supplied by the publisher" in p and "CGC 1516665" in p
    assert "CBC1264856" not in p, "a brand the operator removed from the blog is not re-injected"


def test_the_proof_read_knows_the_licensed_name_is_not_an_inconsistency():
    stub = StubClaude(call_handler=lambda p: {"issues": [], "assessment": {}})
    g = BG(stub, db=None)
    g._verify_content(_brand(_store()), "# t\n\nBody.")
    assert "NAMES: \"Revive Kitchen & Bath (licensed as Revive Design and Renovation)\"" in stub.calls[-1]
    assert "VERIFIED LICENCE NUMBERS" in stub.calls[-1] and "CGC 1516665" in stub.calls[-1]
    stub2 = StubClaude(call_handler=lambda p: {"issues": [], "assessment": {}})
    BG(stub2, db=None)._verify_content(dict(BRAND), "# t\n\nBody.")
    assert "NAMES:" not in stub2.calls[-1] and "VERIFIED LICENCE" not in stub2.calls[-1]


@pytest.mark.parametrize("raw", [None, "", "{}", "[]", "garbage",
                                 json.dumps({"solo": {"name": "Solo", "rows": []}})])
def test_the_fu205_goldens_hold_with_every_empty_store(monkeypatch, raw):
    import tests.prompt_capture as pc
    monkeypatch.setattr(pc, "BRAND_MIN", dict(pc.BRAND_MIN, verified_facts=raw))
    monkeypatch.setattr(pc, "BRAND_RICH", dict(pc.BRAND_RICH, verified_facts=raw))
    got = pc.capture()
    gold = os.path.join(os.path.dirname(__file__), "fixtures", "prompts")
    for name, text in got.items():
        with open(os.path.join(gold, name + ".txt"), encoding="utf-8") as fh:
            assert text == fh.read(), name


# ── the backstops ────────────────────────────────────────────────────────────────────────────
BODY = """*[Add author byline before publishing]*

# kitchen remodel contractors in Tampa

## Quick answer

CMK Construction (CGC1516665) and **Revive Kitchen & Bath** both remodel kitchens [S1].

## Who holds which licence?

Revive Kitchen & Bath's licence is CBC-1264856 [S1].

| Contractor | Licence |
|---|---|
| Revive Kitchen & Bath | CBC1264856 |
| CMK Construction | cgc 1516665 |

## Sources

- [S1] CMK Construction — <https://cmkconstructioninc.com>
"""


def test_licence_spacing_is_normalised_everywhere_but_sources_and_it_is_idempotent():
    g = BG(StubClaude(), db=None)
    out, n = g._vfact_enforce(BODY, _brand(_store()))
    assert "CMK Construction (CGC 1516665)" in out and "CBC1264856 [S1]" in out
    assert "| CMK Construction | CGC 1516665 |" in out
    assert "CGC1516665" not in out and "CBC-1264856" not in out
    again, n2 = g._vfact_enforce(out, _brand(_store()))
    assert again == out and n2 == 0


def test_licensed_as_is_added_once_to_the_first_prose_mention_only():
    g = BG(StubClaude(), db=None)
    out, _ = g._vfact_enforce(BODY, _brand(_store()))
    assert out.count("(licensed as Revive Design and Renovation)") == 1
    assert "**Revive Kitchen & Bath** (licensed as Revive Design and Renovation) both" in out
    assert "| Revive Kitchen & Bath | CBC1264856 |" in out, "never inside a table"
    already = BODY.replace("**Revive Kitchen & Bath** both",
                           "**Revive Kitchen & Bath** (Revive Design and Renovation) both")
    out2, _ = g._vfact_enforce(already, _brand(_store()))
    assert "(licensed as" not in out2, "the legal name is already there — nothing added"


def test_a_possessive_first_mention_is_skipped_for_the_next_one():
    body = "# t\n\nRevive Kitchen & Bath's showroom is new. Revive Kitchen & Bath remodels baths.\n"
    out, _ = BG(StubClaude(), db=None)._vfact_enforce(body, _brand(_store()))
    assert "Revive Kitchen & Bath's showroom" in out
    assert "Revive Kitchen & Bath (licensed as Revive Design and Renovation) remodels" in out


def test_the_warnings_name_a_wrong_licence_a_borrowed_one_and_the_banned_phrase():
    body = ("# t\n\nS&W Kitchens holds a General Building Contractor licence, CBC1264856.\n\n"
            "CMK Construction also holds CGC 9999999.\n\n"
            "Revive Design and Renovation remodels baths.\n\nNobody here has ER 13016498 on file.\n")
    notes = BG(StubClaude(), db=None)._vfact_checks(body, _brand(_store()))
    lic = next(n for n in notes if n.startswith("license-check:"))
    assert "CBC1264856 appears with S&W Kitchens but is Revive Kitchen & Bath's licence" in lic
    assert "CGC 9999999 is stated for CMK Construction but is not among its verified licences" in lic
    assert "\"General Building Contractor\" appears with S&W Kitchens" in lic
    assert "ER 13016498" not in lic, "a sentence naming no verified brand is not judged"
    name = next(n for n in notes if n.startswith("name-check:"))
    assert "\"Revive Design and Renovation\" is used on its own" in name


def test_a_clean_body_has_no_verified_facts_warning():
    g = BG(StubClaude(), db=None)
    clean, _ = g._vfact_enforce(BODY, _brand(_store()))
    assert g._vfact_checks(clean, _brand(_store())) == []


def test_your_wording_survives_the_punt_scrub_and_a_model_punt_does_not():
    store = _store()
    store["bath-fitter"]["rows"].append({"fact": "Check the local franchisee's licence with the state board",
                                         "kind": "note", "wrong": "", "source_url": ""})
    body = ("# t\n\n| Brand | Licensing | Notes |\n|---|---|---|\n"
            "| Bath Fitter | franchise model — verify with the specific franchisee | Nationwide |\n"
            "| Revive Kitchen & Bath | CBC1264856 | Verify on their site |\n"
            "| CMK Construction | CGC 1516665 | Tampa |\n\n"
            "Bath Fitter works through franchisees. Check the local franchisee's licence with the state board.\n\n"
            "Most remodelers are similar. Check the local franchisee's licence with the state board.\n")
    g = BG(StubClaude(), db=None)
    out = g._rebuild_sources(body, _brand(store))
    assert "| Bath Fitter | franchise model; verify with the specific franchisee |" in out
    assert "Verify on their site" not in out and "Notes" not in out, "the model's punt still goes"
    assert "Bath Fitter works through franchisees. Check the local franchisee's licence" in out
    assert "Most remodelers are similar." in out and out.count("Check the local franchisee") == 1
    plain = g._rebuild_sources(body, dict(BRAND))
    assert plain.count("Check the local franchisee") == 0, "without the stored note it is a punt"


def test_the_repair_gate_refuses_a_respaced_number_and_a_dropped_licensed_as():
    g = BG(StubClaude(), db=None)
    b = _brand(_store())
    body = ("# t\n\nCMK Construction holds CGC 1516665.\n\n"
            "Revive Kitchen & Bath (licensed as Revive Design and Renovation) remodels.\n")
    why = g._verify_repair_gate(body, "CMK Construction holds CGC 1516665.",
                                "CMK Construction holds CGC1516665.", b)
    assert "CGC 1516665" in why
    why2 = g._verify_repair_gate(body, "Revive Kitchen & Bath (licensed as Revive Design and Renovation) remodels.",
                                 "Revive Kitchen & Bath remodels.", b)
    assert "licensed as" in why2
    assert g._verify_repair_gate(body, "CMK Construction holds CGC 1516665.",
                                 "CMK Construction, a remodeler, holds CGC 1516665.", b) == ""


def test_the_finished_blog_carries_the_exact_values_and_one_licensed_as():
    blocks = _blocks_after_gather()
    n = next(i for i, x in enumerate(blocks, 1) if x["label"] == "fact · Revive Kitchen & Bath · buildzoom.com")
    body = ("*[Add author byline before publishing]*\n\n# kitchen remodel contractors in Tampa\n\n"
            f"## Quick answer\n\nCMK Construction (CGC1516665) and Revive Kitchen & Bath [S{n}] both "
            "remodel kitchens.\n\n## Licences\n\nRevive Kitchen & Bath holds CBC1264856 "
            f"[S{n}].\n\n## FAQ\n\n### Who is licensed?\n\nBoth are.\n")
    g = BG(StubClaude(call_handler=lambda p: {}), db=None)
    g._evidence_blocks = blocks
    b = _brand(_store())
    art = {"title": "kitchen remodel contractors in Tampa", "body_markdown": body,
           "meta_description": "CMK Construction CGC1516665 and more."}
    g._finalize_article(b, "kitchen remodel contractors in Tampa", art, body, with_linkedin=False)
    out = art["body_markdown"]
    assert "CGC 1516665" in out and "CGC1516665" not in out
    assert out.count("(licensed as Revive Design and Renovation)") == 1
    assert "https://www.buildzoom.com/contractor/revive" in out.split("## Sources")[1]
    assert art["meta_description"] == "CMK Construction CGC 1516665 and more."
    assert "license-check" not in _joined(art) and "name-check" not in _joined(art)


# ── Brand facts (FU178): a linked page off the brand's own site is not "its own site" ────────
def test_an_off_domain_brand_fact_is_labelled_as_the_page_it_is(monkeypatch):
    from generators.blog_gen import _parse_fact_lines
    fact = "BuildZoom score 106, top 7% of Florida contractors"
    monkeypatch.setattr(bgmod, "_fetch_homepage", lambda u, **k: f"<html>{fact}.</html>")
    monkeypatch.setattr(bgmod, "_extract_visible_text", lambda h: h or "")

    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": ["Rival"], "peer_tools": ["Rival"], "dimensions": ["pricing"],
                    "products": [], "claims": [], "core_topic": "remodeling"}
        return {}
    g = BG(StubClaude(call_handler=call_h, search_handler=lambda b, a, bl: []), db=None)
    brand = {"name": "Acme", "domain_url": "https://acme.com", "category": "remodeling",
             "key_facts": json.dumps({"facts": {"items": _parse_fact_lines(
                 f"{fact} | https://www.buildzoom.com/contractor/acme")}})}
    s = g._source_for_completion(brand, "best remodelers",
                                 {"body_markdown": "| A | P |\n|---|---|\n| Acme | ? |\n| Rival | ? |\n"},
                                 ymyl=None)
    hit = [f for f in s["fresh"] if "BuildZoom score 106" in f["text"]]
    assert hit and hit[0]["label"] == "fact · Acme · buildzoom.com"
    assert "own site" not in hit[0]["text"] and "the page the publisher linked" in hit[0]["text"]


# ── E: a verified licence buys no software-licence rescue ────────────────────────────────────
def _source_with_licence_dim(store):
    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": ["Rival"], "peer_tools": ["Rival"], "dimensions": ["License"],
                    "products": [], "claims": [], "core_topic": "remodeling"}
        if '"domains"' in p:
            return {"domains": {"Rival": "rival.com"}}
        return {}
    stub = StubClaude(call_handler=call_h, search_handler=lambda b, a, bl: [])
    g = BG(stub, db=None)
    brand = {"name": "Acme", "domain_url": "https://acme.com", "category": "remodeling",
             "verified_facts": json.dumps(store)}
    g._source_for_completion(brand, "best remodelers",
                             {"body_markdown": "| A | License |\n|---|---|\n| Acme | ? |\n| Rival | ? |\n"},
                             ymyl=None)
    return [s["brief"] for s in stub.searches]


def test_a_verified_licence_is_never_searched_for():
    store, _d, _f, _o = _vfact_clean(
        [{"name": "Rival", "rows": [{"fact": "CBC1234567 — Certified Building Contractor", "kind": "license"}]}],
        known_names=["Rival"], subject="Acme")
    briefs = _source_with_licence_dim(store)
    assert not any("commercial-use / license / key terms" in b for b in briefs)
    assert not any(b.startswith("Rival: License —") for b in briefs)
    without = _source_with_licence_dim({})
    assert any("commercial-use / license / key terms" in b for b in without), "unchanged without facts"


# ── the pause carries the warning; the citations come back from the evidence alone ───────────
def test_the_pause_carries_the_warning_and_a_resume_rebuilds_the_citations():
    g, _ev, _ = _gather(_brand(_store()))
    notes = g._check_notes()
    assert notes["_vfact_note"] == g._vfact_note
    g2 = BG(StubClaude(), db=None)
    g2._restore_check_notes(json.loads(json.dumps(notes)))
    assert g2._vfact_note == g._vfact_note
    blocks = json.loads(json.dumps(g._evidence_blocks))
    assert (_vfact_prompt_block(_brand(_store()), "reconcile", blocks)
            == _vfact_prompt_block(_brand(_store()), "reconcile", g._evidence_blocks))


def test_a_zip_code_is_not_a_licence_number():
    assert _vfact_license_hits("Tampa, FL 33607") == []
    assert [k for _m, k in _vfact_license_hits("NMLS 123456 and CGC 1516665")] == ["NMLS123456", "CGC1516665"]
    assert _vfact_tokens({"rows": [{"fact": "CGC 1516665 x"}]}) == {"CGC1516665": "CGC 1516665"}
    assert _kf_slug("Revive Kitchen & Bath") == "revive-kitchen-bath"
