"""FU209 — a brand whose PRICE could not be confirmed can be removed, not only kept price-less.

The pause offered one way out for a price-only entry: "Skip — drop the price column". That costs every
OTHER brand its sourced price to keep one brand in the table. The operator asked for the other trade
too — drop the brand, keep the price column — which FU206b already built for brands with no data at
all. Same whole-blog removal, same comparison floor; this round only lets the price-only ask use it.
"""
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude
from tests.test_fu206_thin_competitor import _zeta_ck

PDRAFT = ("# where to buy concrete equipment\n\n*[Add author byline before publishing]*\n\n"
          "## Quick answer\n\nAcme, Bravo, Delta and Echo all ship nationwide.\n\n"
          "| Retailer | Starting Price | Financing |\n| --- | --- | --- |\n"
          "| Acme | $1,300 | in-house |\n| Bravo | $1,450 | partner lender |\n"
          "| Delta | $1,200 | partner lender |\n| Echo | $1,350 | in-house |\n| Zeta |  | partner lender |\n\n"
          "## FAQ\n\n### Does Zeta offer financing?\n\nZeta offers partner financing.\n\n"
          "### Does Acme ship?\n\nYes.\n")


def _price_ck(tools):
    ck = _zeta_ck(tools)
    ck["sourcing"]["unsourced"] = [{"tool": "Zeta", "price_only": True, "facts": ["current price"],
                                    "remaining_if_removed": len(tools) - 1}]
    ck["article"]["body_markdown"] = PDRAFT
    ck["draft_body"] = PDRAFT
    return ck


def _finish(tools, provided):
    gen = BlogGenerator(StubClaude(), None)
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": a["body_markdown"], "flagged": []}
    art = gen.finish_pending_blog({"name": "Acme"}, "where to buy concrete equipment",
                                  _price_ck(tools), provided)
    return gen, art


def test_the_price_only_pause_item_tells_the_modal_whether_removal_is_available():
    tools = ["Bravo", "Delta", "Echo", "Zeta"]

    def handler(p):
        if "core_topic" in p.lower():
            return {"tools": tools, "peer_tools": tools, "dimensions": ["Starting Price", "Financing"],
                    "claims": [], "core_topic": "x", "products": [], "generic_options": []}
        return {}

    def search(brief, allowed, blocked):
        return [{"title": t, "url": f"https://{t.lower()}.example/p",
                 "fact": f"{t} offers partner financing." + ("" if t == "Zeta" else f" From $1,200.")}
                for t in tools if t.lower() in brief.lower()]
    gen = BlogGenerator(StubClaude(call_handler=handler, search_handler=search,
                                   official_domain=lambda b, c: f"{b.lower()}.example"), None)
    gen._evidence_blocks = []
    s = gen._source_for_completion({"name": "Acme", "category": "c"}, "where to buy x",
                                   {"title": "t", "body_markdown": "# t\n"})
    price = [u for u in s["unsourced"] if u.get("price_only")]
    assert [u["tool"] for u in price] == ["Zeta"]
    assert price[0]["remaining_if_removed"] == 3


def test_removing_a_price_only_brand_keeps_the_price_column_for_everyone_else():
    gen, art = _finish(["Bravo", "Delta", "Echo", "Zeta"], [{"tool": "Zeta", "remove": True}])
    b = art["body_markdown"]
    assert gen._removed_brands == ["Zeta"]
    assert "| Retailer | Starting Price | Financing |" in b, "the price column was dropped anyway"
    assert "| Zeta |" not in b, "the price-less row survived"
    assert "Does Zeta offer financing" not in b, "the removed brand's FAQ entry survived"
    for firm, price in (("Bravo", "$1,450"), ("Delta", "$1,200"), ("Echo", "$1,350")):
        assert f"| {firm} | {price} |" in b, f"{firm} lost its sourced price"


def test_keeping_a_price_only_brand_is_still_the_default_and_never_removes_it():
    gen, art = _finish(["Bravo", "Delta", "Echo", "Zeta"], [{"tool": "Zeta", "skip": True}])
    assert gen._removed_brands == []
    gen, art = _finish(["Bravo", "Delta", "Echo", "Zeta"], [])
    assert gen._removed_brands == [], "Skip all must not remove a price-only brand"


def test_a_price_only_removal_below_the_floor_is_refused_and_reported():
    gen, art = _finish(["Bravo", "Delta", "Zeta"], [{"tool": "Zeta", "remove": True}])
    assert gen._removed_brands == [] and gen._removed_refused == ["Zeta"]
    assert any(w["check"] == "removed-brand" and "could NOT be removed" in w["detail"]
               for w in art["warnings"])


def test_a_pasted_price_beats_a_removal():
    gen, art = _finish(["Bravo", "Delta", "Echo", "Zeta"],
                       [{"tool": "Zeta", "fact": "Zeta starts at $1,100", "remove": False}])
    assert gen._removed_brands == []


# ── FU209b: a failed reconcile must never finish the blog silently ───────────────────────────
def test_a_failed_reconcile_raises_instead_of_dropping_the_supplied_price():
    """Observed in production: the operator typed Comotomo's price, the Anthropic credit balance had
    run out, the reconcile returned nothing — and the resume finalized the UNreconciled draft anyway.
    Its blank price cell dropped the whole column and the checkpoint was cleared, so the answer was
    lost. The resume must stop and leave the blog paused."""
    import pytest
    gen = BlogGenerator(StubClaude(), None)
    gen.claude.last_error = "Anthropic credit balance is too low to complete this request."
    gen._reconcile_and_finish = lambda b, s, a, so: None
    with pytest.raises(ValueError) as ei:
        gen.finish_pending_blog({"name": "Acme"}, "t", _price_ck(["Bravo", "Delta", "Echo", "Zeta"]),
                                [{"tool": "Zeta", "fact": "Zeta starts at $1,100"}])
    assert "credit balance" in str(ei.value) and "still paused" in str(ei.value)


def test_the_endpoint_keeps_the_pause_when_the_resume_fails(monkeypatch):
    import os
    import tempfile
    import app as appmod
    from db import Database
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "Acme")
        blog_id = db.save_blog(bid, "where to buy concrete equipment", title="t",
                               body_markdown="# t\n\nDraft.\n", status="awaiting_sources")
        pend = {"missing": _price_ck(["Bravo", "Delta", "Echo", "Zeta"])["sourcing"]["unsourced"],
                "checkpoint": _price_ck(["Bravo", "Delta", "Echo", "Zeta"]), "gen_cost": 1.0}
        db.update_blog(blog_id, pending_state=pend)
        db.close()
        box = {}

        def _inline(_name, fn, **kw):
            try:
                box["result"] = fn(_task_id="t") if kw.get("pass_task_id") else fn()
            except Exception as e:
                box["error"] = str(e)
            return "t"
        stub = StubClaude()
        stub.last_error = "Anthropic credit balance is too low to complete this request."
        monkeypatch.setattr(appmod, "start_task", _inline)
        monkeypatch.setattr(appmod, "ClaudeClient", lambda *a, **k: stub)
        monkeypatch.setattr(appmod, "_ensure_brand_byline_logo", lambda c, d, b: b)
        monkeypatch.setattr(BlogGenerator, "_reconcile_and_finish", lambda self, b, s, a, so: None)
        appmod.DB_PATH = path
        appmod._db_initialized = True
        appmod.app.test_client().post(f"/api/blogs/{blog_id}/provide-sources",
                                      json={"sources": [{"tool": "Zeta", "fact": "Zeta starts at $1,100"}]})
        assert "credit balance" in box.get("error", "")
        db = Database(path)
        db.connect()
        row = db.get_blog(blog_id)
        db.close()
        assert row["status"] == "awaiting_sources" and row["pending_state"].get("missing"), \
            "a failed resume cleared the pause and lost the operator's answer"
        assert row["body_markdown"] == "# t\n\nDraft.\n"
    finally:
        os.unlink(path)
