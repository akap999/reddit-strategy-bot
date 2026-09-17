"""FU210 — the competitors the operator marks as THEIRS are compared in every blog and never dropped.

A brand's competitor list was one list, and blog generation treated every name on it as a suggestion:
the writer could skip one that did not fit the subject, the sourcing cap could leave one unsourced, the
reconcile deleted a row it could not fill, and the pause offered to remove it. The operator asked for
their own competitors to always be there. The list is split — "your competitors" is a SUBSET of the
full list (so posts, comments and the curated check all still see them) — and starts EMPTY for every
existing brand, so nothing auto-added is locked in by accident.
"""
import json
import os
import tempfile

from db import Database
from generators.blog_gen import (BlogGenerator, _manual_competitors, _matches_competitor)
from tests.stubs import StubClaude
from tests.test_fu209_price_only_remove import _price_ck
from tests.test_fu206_thin_competitor import _zeta_ck

BRAND = {"name": "Acme", "category": "concrete tools", "domain_url": "https://acme.example",
         "competitors": json.dumps(["Bravo", "Delta", "Echo", "Zeta", "Omega"]),
         "manual_competitors": json.dumps(["Zeta"])}


def _plain(**kw):
    b = dict(BRAND)
    b.pop("manual_competitors")
    b.update(kw)
    return b


# ── the list itself ───────────────────────────────────────────────────────────────────────────
def test_the_manual_list_is_read_from_its_stored_json():
    assert _manual_competitors(BRAND) == ["Zeta"]
    assert _manual_competitors(_plain()) == []
    assert _manual_competitors({"manual_competitors": '["Zeta", "zeta", ""]'}) == ["Zeta"]


def test_matching_respects_word_boundaries():
    assert _matches_competitor("Noom Med", ["Noom"]) == "Noom"
    assert _matches_competitor("Rory", ["Ro"]) == ""
    assert _matches_competitor("**Zeta**", ["Zeta"]) == "Zeta"


# ── the writer is told ────────────────────────────────────────────────────────────────────────
def test_the_brand_block_separates_your_competitors_and_is_inert_without_them():
    gen = BlogGenerator(None, None)
    _, _, block = gen._brand_block(BRAND)
    assert "Competitors YOU MUST COMPARE" in block and "Zeta" in block.split("Competitors YOU MUST COMPARE")[1].split("\n")[0]
    assert "Competitors: Bravo, Delta, Echo, Omega" in block, "your competitor must not also sit in the optional list"
    _, _, plain = gen._brand_block(_plain())
    _, _, empty = gen._brand_block(_plain(manual_competitors="[]"))
    assert plain == empty and "YOU MUST COMPARE" not in plain


def test_the_article_prompt_puts_your_competitors_first_and_is_unchanged_without_them():
    def _prompt(brand):
        stub = StubClaude(call_handler=lambda p: {"title": "t", "body_markdown": "# t\n", "meta_description": "d"})
        gen = BlogGenerator(stub, None)
        gen.generate_article(brand, "where to buy concrete tools")
        return next(p for p in stub.calls if "MINIMUM COMPETITORS" in p)
    with_mine = _prompt(BRAND)
    assert "0. EVERY competitor under \"Competitors YOU MUST COMPARE\" (Zeta)" in with_mine
    without = _prompt(_plain())
    assert "YOU MUST COMPARE" not in without
    assert without == _prompt(_plain(manual_competitors="[]"))


# ── sourcing ──────────────────────────────────────────────────────────────────────────────────
def _sourcing(brand, extracted, facts=None):
    def handler(p):
        if "core_topic" in p.lower():
            return {"tools": extracted, "peer_tools": extracted, "dimensions": ["Starting Price", "Financing"],
                    "claims": [], "core_topic": "x", "products": [], "generic_options": []}
        return {}

    def search(brief, allowed, blocked):
        out = []
        for t in ["Bravo", "Delta", "Echo", "Zeta", "Omega", "Kappa", "Sigma"]:
            if t.lower() in brief.lower() and (facts is None or t in facts):
                out.append({"title": t, "url": f"https://{t.lower()}.example/p",
                            "fact": f"{t} offers partner financing. From $1,200."})
        return out
    gen = BlogGenerator(StubClaude(call_handler=handler, search_handler=search,
                                   official_domain=lambda b, c: f"{b.lower()}.example"), None)
    gen._evidence_blocks = []
    return gen, gen._source_for_completion(brand, "where to buy concrete tools",
                                           {"title": "t", "body_markdown": "# t\n"})


def test_your_competitor_is_sourced_even_when_the_draft_never_named_it():
    _, s = _sourcing(BRAND, ["Bravo", "Delta", "Echo"])
    assert "Zeta" in s["tools"] and s["manual"] == ["Zeta"]


def test_your_competitors_never_count_against_the_sourcing_cap():
    brand = dict(BRAND, manual_competitors=json.dumps(["Zeta", "Omega"]))
    _, s = _sourcing(brand, ["Bravo", "Delta", "Echo", "Kappa", "Sigma"])
    assert {"Zeta", "Omega"} <= set(s["tools"])
    assert len([t for t in s["tools"] if t not in ("Zeta", "Omega")]) == 4, "the cap still applies to the rest"


def test_a_brand_with_no_marked_competitors_sources_exactly_as_before():
    _, a = _sourcing(_plain(), ["Bravo", "Delta", "Echo"])
    _, b = _sourcing(_plain(manual_competitors="[]"), ["Bravo", "Delta", "Echo"])
    assert a["tools"] == b["tools"] == ["Bravo", "Delta", "Echo"] and a["manual"] == []


def test_the_pause_never_offers_to_remove_your_competitor():
    _, s = _sourcing(BRAND, ["Bravo", "Delta", "Echo", "Zeta"], facts=["Bravo", "Delta", "Echo"])
    z = [u for u in s["unsourced"] if u["tool"] == "Zeta"]
    assert z and all(u.get("manual") for u in z)
    assert not any(u.get("thin_coverage") and u["tool"] == "Zeta" for u in s["unsourced"])


# ── the resume never removes it ───────────────────────────────────────────────────────────────
def _finish(ck, provided, brand=BRAND):
    gen = BlogGenerator(StubClaude(), None)
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": a["body_markdown"], "flagged": []}
    art = gen.finish_pending_blog(brand, "where to buy concrete equipment", ck, provided)
    return gen, art


def test_skipping_your_empty_competitor_keeps_it():
    ck = _zeta_ck(["Bravo", "Delta", "Echo", "Zeta"])
    ck["sourcing"]["unsourced"][0]["manual"] = True
    ck["sourcing"]["manual"] = ["Zeta"]
    gen, art = _finish(ck, [{"tool": "Zeta", "skip": True}])
    assert gen._removed_brands == []
    assert gen._removed_protected == [], "a skip is not a removal request — it must not warn as refused"


def test_an_explicit_removal_of_your_competitor_is_refused_and_reported():
    ck = _price_ck(["Bravo", "Delta", "Echo", "Zeta"])
    gen, art = _finish(ck, [{"tool": "Zeta", "remove": True}])
    assert gen._removed_brands == [] and gen._removed_protected == ["Zeta"]
    assert any("one of your competitors" in w["detail"] for w in art["warnings"])
    assert "| Zeta |" in art["body_markdown"], "your competitor's row was stripped"


def test_a_competitor_marked_while_the_blog_was_paused_is_protected_too():
    ck = _price_ck(["Bravo", "Delta", "Echo", "Zeta"])   # checkpoint written before it was marked
    gen, _ = _finish(ck, [{"tool": "Zeta", "remove": True}])
    assert gen._removed_protected == ["Zeta"]


def test_the_reconcile_is_told_never_to_drop_your_competitors():
    gen = BlogGenerator(StubClaude(call_handler=lambda p: {"revised_body_markdown": "# t\n", "flagged": []}), None)
    gen._evidence_blocks = []
    src = {"name": "Acme", "cat": "c", "tools": ["Bravo", "Zeta"], "peers": [], "dims": ["Price"],
           "claims": [], "fresh": [{"label": "Bravo", "url": "u", "text": "t"}], "manual": ["Zeta"]}
    gen._reconcile_and_finish(BRAND, "t", {"body_markdown": "# t\n\nBody.\n"}, src)
    p = gen.claude.calls[-1]
    assert "OPERATOR'S COMPETITORS (hard rule, OVERRIDES every row-removal rule above)" in p
    assert 'YOUR COMPETITORS (set by the operator' in p and '"Zeta"' in p
    gen2 = BlogGenerator(StubClaude(call_handler=lambda p: {"revised_body_markdown": "# t\n", "flagged": []}), None)
    gen2._evidence_blocks = []
    gen2._reconcile_and_finish(_plain(), "t", {"body_markdown": "# t\n\nBody.\n"}, dict(src, manual=[]))
    assert "OPERATOR'S COMPETITORS" not in gen2.claude.calls[-1]


def test_a_finished_article_missing_your_competitor_is_flagged():
    gen = BlogGenerator(StubClaude(), None)
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": "# t\n\nBravo and Delta only.\n",
                                                     "flagged": []}
    ck = _zeta_ck(["Bravo", "Delta", "Echo", "Zeta"])
    ck["sourcing"]["unsourced"] = []
    ck["sourcing"]["fresh"] = [{"label": "Bravo", "url": "u", "text": "t"}]
    ck["article"]["body_markdown"] = ck["draft_body"] = "# t\n\nBravo and Delta only.\n"
    art = gen.finish_pending_blog(BRAND, "t", ck, [])
    assert any(w["check"] == "your-competitors" and "Zeta" in w["detail"] for w in art["warnings"])


def test_a_removal_only_resume_with_no_new_facts_still_finishes():
    """FU209b must only stop when the reconcile had something to write."""
    ck = _zeta_ck(["Bravo", "Delta", "Echo", "Zeta"])
    ck["sourcing"]["fresh"] = []
    gen = BlogGenerator(StubClaude(), None)
    art = gen.finish_pending_blog(_plain(), "t", ck, [{"tool": "Zeta", "remove": True}])
    assert art and gen._removed_brands == ["Zeta"]


# ── verify (FU208) cannot delete its row ──────────────────────────────────────────────────────
def test_a_verify_correction_can_change_your_competitors_row_but_not_delete_it():
    body = "# t\n\n| Firm | Price |\n| --- | --- |\n| Zeta | $10 [S1] |\n| Bravo | $20 [S2] |\n"
    gen = BlogGenerator(None, None)
    assert "one of your competitors" in gen._vx_gate(body, "| Zeta | $10 [S1] |", "", BRAND, "$10", "remove")
    assert gen._vx_gate(body, "| Zeta | $10 [S1] |", "| Zeta | $12 [S1] |", BRAND, "$10", "correct") == ""
    assert gen._vx_gate(body, "| Bravo | $20 [S2] |", "", BRAND, "$20", "remove") == ""


# ── Edit Brand storage ────────────────────────────────────────────────────────────────────────
def test_saving_your_competitors_keeps_them_in_the_full_list(monkeypatch):
    import app as appmod
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "Acme")
        db.update_brand(bid, competitors=json.dumps(["Bravo", "Delta"]))
        assert db.get_brand(bid).get("manual_competitors") in (None, ""), "existing brands start empty"
        db.close()
        appmod.DB_PATH = path
        appmod._db_initialized = True
        cli = appmod.app.test_client()
        r = cli.put(f"/api/brands/{bid}", json={"manual_competitors": ["Zeta", "Bravo"]})
        assert r.status_code == 200
        db = Database(path)
        db.connect()
        b = db.get_brand(bid)
        db.close()
        assert json.loads(b["manual_competitors"]) == ["Zeta", "Bravo"]
        assert json.loads(b["competitors"]) == ["Bravo", "Delta", "Zeta"]
        r = cli.post(f"/api/subreddits/{sub['id']}/brands", json={"name": "New", "manual_competitors": ["X"]})
        assert r.status_code == 200, "creating a brand must ignore the field, not crash"
    finally:
        os.unlink(path)
