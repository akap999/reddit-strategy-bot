"""FU206 — when ONE competitor cannot answer a dimension, drop the dimension or drop the competitor?

FU200 locked the first answer: "a thin row is never dropped by the punt resolver" — a badly-sourced
competitor costs DIMENSIONS, never its seat, because shrinking the compared field is the
self-crowning risk FU98 and the FU105 floor exist to prevent.

Measured against the two real tables, that is right sometimes and wrong sometimes:

  Osbornes (11 cols × 5 firms) — Withers has 7/10 cells empty and is the ONLY firm missing two
  dimensions the other four can all answer. Keeping it ships 3 dimensions × 5 rows = 15 cells of
  real data; removing it ships 5 × 4 = 20.

  Thyseed (5 cols × 6 bottles) — the gaps are SCATTERED: three different bottles, all missing price.
  Removing rows would cost three competitors to save one column, and the floor blocks it anyway.

So neither answer is always right, and the operator decides per blog at the pause they already see.
A removal is from the ENTIRE blog — row, evidence, prose, FAQ — and can never breach the floor.
"""
import pytest

from generators.blog_gen import BlogGenerator, _MIN_COMPARISON_BRANDS
from tests.stubs import StubClaude

BRAND = {"name": "Osbornes Law", "domain_url": "https://osbornes.example",
         "category": "London law firm", "competitors": ["Dawson", "Withers", "Charles", "Irwin"]}
DIMS = ["Legal 500 ranking", "Hague Convention expertise", "Jurisdiction coverage"]


def _sourcing_gen(tool_texts):
    """Drive the detection with a KNOWN coverage map (what `tool_texts` holds after the rescue)."""
    def handler(prompt):
        if "core_topic" in prompt.lower() or "TOOLS" in prompt:
            return {"tools": ["Dawson", "Withers", "Charles", "Irwin"],
                    "peer_tools": ["Dawson", "Withers", "Charles", "Irwin"],
                    "dimensions": DIMS, "claims": [], "core_topic": "international family law",
                    "products": [], "generic_options": []}
        return {}
    gen = BlogGenerator(StubClaude(call_handler=handler), None)
    gen._evidence_blocks = []
    gen._forced_tool_texts = tool_texts
    return gen


def _thin_items(tool_texts):
    """Run the real sourcing stage, then read the thin-coverage pause items it produced."""
    gen = _sourcing_gen(tool_texts)
    article = {"title": "t", "body_markdown": "# t\n"}
    # seed the coverage map the way the rescue would have
    orig = gen._source_for_completion

    s = orig(BRAND, "best UK firms for international family law", article)
    return [u for u in s["unsourced"] if u.get("thin_coverage")]


# ── the floor is never breached ───────────────────────────────────────────────────────────────
def test_removal_is_never_offered_when_it_would_breach_the_comparison_floor():
    """The reason FU200 locked this. With exactly the floor's worth of competitors there is nothing
    to trade — a thinner field is the self-crowning failure, so the option simply is not offered."""
    gen = BlogGenerator(StubClaude(), None)
    ck = {"sourcing": {"name": "Acme", "tools": ["Bravo", "Delta", "Echo"], "peers": [],
                       "dims": ["Pricing"], "claims": [], "fresh": [], "unsourced": []},
          "evidence_blocks": [], "article": {"title": "t", "body_markdown": "# t\n"},
          "draft_body": "# t\n"}
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": a["body_markdown"],
                                                     "flagged": []}
    art = gen.finish_pending_blog({"name": "Acme"}, "t", ck, [{"tool": "Bravo", "remove": True}])
    assert gen._removed_brands == [], "the floor was breached"
    assert gen._removed_refused == ["Bravo"]
    assert any(w["check"] == "removed-brand" and "could NOT be removed" in w["detail"]
               for w in art["warnings"]), "a refused removal was silently ignored"


def test_a_removal_above_the_floor_is_applied_everywhere():
    """'Remove from the blog' has to mean the whole blog — row, evidence and prose."""
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = [{"label": "Withers", "url": "https://w.example", "text": "x"},
                            {"label": "Dawson", "url": "https://d.example", "text": "y"}]
    ck = {"sourcing": {"name": "Osbornes", "tools": ["Dawson", "Withers", "Charles", "Irwin"],
                       "peers": ["Dawson", "Withers", "Charles", "Irwin"], "dims": DIMS,
                       "claims": [], "unsourced": [{"tool": "Withers", "thin_coverage": True}],
                       "fresh": [{"label": "Withers", "url": "https://w.example", "text": "x"},
                                 {"label": "Dawson", "url": "https://d.example", "text": "y"}]},
          "evidence_blocks": list(gen._evidence_blocks),
          "article": {"title": "t", "body_markdown": "# t\n"}, "draft_body": "# t\n"}
    captured = {}
    gen._reconcile_and_finish = lambda b, s, a, so: (
        captured.update(sourcing=so) or {"body_markdown": "# t\n\nDawson and Charles.\n",
                                         "flagged": []})
    gen.finish_pending_blog({"name": "Osbornes"}, "t", ck, [{"tool": "Withers", "remove": True}])
    so = captured["sourcing"]
    assert "Withers" not in so["tools"] and "Withers" not in so["peers"]
    assert not any(f["label"] == "Withers" for f in so["fresh"]), "its evidence survived"
    assert not any(b["label"] == "Withers" for b in gen._evidence_blocks)
    assert gen._removed_brands == ["Withers"]


def test_the_reconcile_runs_even_when_nothing_new_was_provided():
    """A removal with no pasted source still needs the writer — it is the writer that takes the
    brand out of the prose the draft already contains."""
    gen = BlogGenerator(StubClaude(), None)
    called = []
    gen._reconcile_and_finish = lambda b, s, a, so: (
        called.append(1) or {"body_markdown": a["body_markdown"], "flagged": []})
    ck = {"sourcing": {"name": "A", "tools": ["B", "C", "D", "E"], "peers": [], "dims": ["P"],
                       "claims": [], "fresh": [], "unsourced": []},
          "evidence_blocks": [], "article": {"title": "t", "body_markdown": "# t\n"},
          "draft_body": "# t\n"}
    gen.finish_pending_blog({"name": "A"}, "t", ck, [{"tool": "B", "remove": True}])
    assert called == [1], "the prose was never rewritten, so the brand is still in the blog"


# ── the reconcile is TOLD, and then VERIFIED ──────────────────────────────────────────────────
def test_the_reconcile_is_told_to_delete_every_trace():
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    gen._removed_brands = ["Withers"]
    gen._reconcile_and_finish({"name": "Osbornes"}, "t",
                              {"title": "t", "body_markdown": "# t\n"},
                              {"name": "Osbornes", "tools": ["Dawson"], "dims": DIMS,
                               "claims": [], "fresh": [{"label": "Dawson", "url": "u", "text": "t"}]})
    prompt = gen.claude.calls[-1]
    assert "REMOVED BY THE OPERATOR" in prompt and "Withers" in prompt
    assert "OVERRIDES PRESERVE SUBSTANCE" in prompt, \
        "PRESERVE SUBSTANCE would protect the sentences naming the removed brand"


def test_a_removal_that_did_not_take_is_reported():
    """The reconcile is an LLM and PRESERVE SUBSTANCE pulls the other way. 'Removed from the blog'
    quietly meaning 'removed from the table' is the silent half-fix this exists to stop."""
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    gen._removed_brands = ["Withers"]
    body = "# t\n\n*[Add author byline before publishing]*\n\nWithers is also worth a look.\n"
    art = {"title": "t", "meta_description": "d", "body_markdown": body}
    gen._finalize_article({"name": "Osbornes"}, "t", art, body, with_linkedin=False)
    hits = [w for w in art["warnings"] if w["check"] == "removed-brand"]
    assert hits and "still appear" in hits[0]["detail"]


def test_a_clean_removal_is_silent():
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    gen._removed_brands = ["Withers"]
    body = "# t\n\n*[Add author byline before publishing]*\n\nDawson and Charles are the picks.\n"
    art = {"title": "t", "meta_description": "d", "body_markdown": body}
    gen._finalize_article({"name": "Osbornes"}, "t", art, body, with_linkedin=False)
    assert not [w for w in art.get("warnings", []) if w["check"] == "removed-brand"], \
        "a clean removal was reported as a problem"


# ── the prompt stays inert when nothing was removed ───────────────────────────────────────────
def test_no_removal_leaves_the_reconcile_prompt_untouched():
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    gen._reconcile_and_finish({"name": "Osbornes"}, "t",
                              {"title": "t", "body_markdown": "# t\n"},
                              {"name": "Osbornes", "tools": ["Dawson"], "dims": DIMS,
                               "claims": [], "fresh": [{"label": "Dawson", "url": "u", "text": "t"}]})
    prompt = gen.claude.calls[-1]
    assert "REMOVED BY THE OPERATOR" not in prompt and "REMOVED BRANDS" not in prompt


def test_the_floor_constant_is_the_fu105_floor():
    assert _MIN_COMPARISON_BRANDS == 3


def test_the_thin_coverage_ask_survives_the_fu189_recheck():
    """The FU204 Change 6 bug, in a new shape — and the end-to-end run caught it before this shipped.

    The FU189 re-check asks "is this ENTITY sourced?" and drops the pause item when it is. For a
    thin-coverage item that test is ALWAYS true — the tool IS sourced, it just cannot answer some
    dimensions — so it would delete every thin-coverage ask ever queued, exactly as it once deleted
    every price ask. Locked so the same entity-vs-fact confusion cannot return a third time."""
    DIMS5 = ["Legal 500 ranking", "Hague Convention expertise", "Jurisdiction coverage",
             "HNW cross-border divorce", "Multi-jurisdictional children matters"]
    TOOLS4 = ["Dawson Cornwell", "Withers", "Charles Russell", "Irwin Mitchell"]

    def handler(p):
        if "core_topic" in p.lower():
            return {"tools": TOOLS4, "peer_tools": TOOLS4, "dimensions": DIMS5, "claims": [],
                    "core_topic": "international family law", "products": [], "generic_options": []}
        return {}

    def facts(t):
        return " ".join(d for d in DIMS5 if not (t == "Withers" and d in (
            "Hague Convention expertise", "Multi-jurisdictional children matters"))).lower()

    stub = StubClaude(
        call_handler=handler,
        search_handler=lambda b, a, x: [
            {"title": t, "url": f"https://{t.split()[0].lower()}.example", "fact": facts(t)}
            for t in TOOLS4 if t.split()[0].lower() in b.lower()],
        official_domain=lambda b, c: f"{b.split()[0].lower()}.example")
    gen = BlogGenerator(stub, None)
    gen._evidence_blocks = []
    s = gen._source_for_completion(
        {"name": "Osbornes Law", "domain_url": "https://osbornes.example",
         "category": "London law firm", "competitors": TOOLS4},
        "best UK firms for international family law", {"title": "t", "body_markdown": "# t\n"})
    thin = [u for u in s["unsourced"] if u.get("thin_coverage")]
    assert thin, "the thin-coverage ask was deleted before it could reach the operator"
    u = thin[0]
    assert u["tool"] == "Withers"
    assert u["covered"] == 3 and u["total"] == 5
    assert set(u["blocks_dims"]) == {"Hague Convention expertise",
                                     "Multi-jurisdictional children matters"}
    assert u["remaining_if_removed"] == 3, "the floor is exactly met, so removal is still offerable"


# ── FU206b: a competitor with NO information at all ───────────────────────────────────────────
ZDRAFT = ("# where to buy concrete equipment\n\n*[Add author byline before publishing]*\n\n"
          "## Quick answer\n\nAcme, Bravo, Delta and Echo all ship nationwide; Zeta is a newer option.\n\n"
          "| Retailer | Starting Price | Financing |\n| --- | --- | --- |\n"
          "| Acme | $1,300 | in-house |\n| Bravo | $1,450 | partner lender |\n"
          "| Delta | $1,200 | partner lender |\n| Echo | $1,350 | in-house |\n| **Zeta** |  |  |\n\n"
          "## FAQ\n\n### Is Zeta any good?\n\nZeta is a newer retailer.\n\n### Does Acme ship?\n\nYes.\n")


def _zeta_ck(tools):
    return {"sourcing": {"name": "Acme", "cat": "c", "tools": tools, "peers": list(tools),
                         "dims": ["Starting Price", "Financing"], "claims": [], "fresh": [],
                         "unsourced": [{"tool": "Zeta", "facts": ["Starting Price", "Financing"]}]},
            "evidence_blocks": [],
            "article": {"title": "where to buy concrete equipment", "meta_description": "d",
                        "keywords": [], "body_markdown": ZDRAFT, "claims_flagged": []},
            "draft_body": ZDRAFT}


def _finish_zeta(tools, provided):
    """The reconcile here IGNORES the removal rule entirely — the worst case an LLM can produce."""
    gen = BlogGenerator(StubClaude(), None)
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": a["body_markdown"],
                                                     "flagged": []}
    art = gen.finish_pending_blog({"name": "Acme"}, "where to buy concrete equipment",
                                  _zeta_ck(tools), provided)
    return gen, art


def test_skip_on_a_brand_with_no_data_drops_the_brand_not_the_table():
    """The bug: 'Skip this one — drop it from the comparison' did the OPPOSITE. The brand's blank row
    stayed, every column had a gap, every column was dropped, and the collapse guard deleted the
    whole table — while the brand stayed in the prose and the FAQ. Three fully-sourced competitors
    lost their table to the one that had nothing."""
    gen, art = _finish_zeta(["Bravo", "Delta", "Echo", "Zeta"], [{"tool": "Zeta", "skip": True}])
    b = art["body_markdown"]
    assert gen._removed_brands == ["Zeta"]
    assert "| Retailer | Starting Price | Financing |" in b, "the comparison table was destroyed"
    assert "Zeta |" not in b, "the empty row survived"
    for firm in ("Bravo", "Delta", "Echo"):
        assert f"| {firm} |" in b, f"{firm}'s fully-sourced row was lost"


def test_skip_all_sends_nothing_and_still_removes_the_empty_brand():
    """'Skip all & finish now' posts an EMPTY sources list — an unanswered entry is a skip too."""
    gen, art = _finish_zeta(["Bravo", "Delta", "Echo", "Zeta"], [])
    assert gen._removed_brands == ["Zeta"]
    assert "| Retailer |" in art["body_markdown"]


def test_a_section_about_the_removed_brand_is_not_restored_by_the_substance_guard():
    """FU54's guard restores a section present in the draft and missing from the revision. Every ###
    is its own section, so a compliant reconcile deleting '### Is Zeta any good?' was overruled and
    the entry put straight back. The draft is stripped too, so there is nothing to restore."""
    gen, art = _finish_zeta(["Bravo", "Delta", "Echo", "Zeta"], [{"tool": "Zeta", "skip": True}])
    b = art["body_markdown"]
    assert "Is Zeta any good" not in b, "the substance guard restored the removed brand's FAQ"
    assert "Does Acme ship" in b, "an unrelated FAQ entry was lost"


def test_prose_is_left_to_the_writer_and_verified_not_deleted():
    """A sentence naming the removed brand usually carries facts about OTHER brands too — deleting it
    deterministically would destroy real content. It stays the reconcile's job, and when the
    reconcile misses it the operator is told."""
    gen, art = _finish_zeta(["Bravo", "Delta", "Echo", "Zeta"], [{"tool": "Zeta", "skip": True}])
    b = art["body_markdown"]
    assert "Acme, Bravo, Delta and Echo all ship nationwide" in b, "other brands' facts were deleted"
    hits = [w for w in art["warnings"] if w["check"] == "removed-brand"]
    assert hits and "still appear" in hits[0]["detail"], "a leftover mention was not reported"


def test_at_the_floor_a_skipped_empty_brand_cannot_be_removed_and_says_why():
    """Three competitors, one empty. Removing it leaves two — below the floor — so it is refused,
    its gaps drop every column, and the operator is told plainly that no comparison could ship."""
    gen, art = _finish_zeta(["Bravo", "Delta", "Zeta"], [{"tool": "Zeta", "skip": True}])
    assert gen._removed_brands == [] and gen._removed_refused == ["Zeta"]
    keys = [w["check"] for w in art["warnings"]]
    assert "removed-brand" in keys and "comparison table" in keys


def test_a_supplied_fact_beats_the_implicit_removal():
    gen, art = _finish_zeta(["Bravo", "Delta", "Echo", "Zeta"],
                            [{"tool": "Zeta", "fact": "Zeta starts at $1,100 with partner financing"}])
    assert gen._removed_brands == [], "a brand the operator sourced was removed"


def test_skipping_a_price_only_brand_still_just_drops_the_price_column():
    """A price-only entry is otherwise fully sourced. Skipping it has always meant 'drop the price
    column' — it must never be read as removing the brand."""
    gen = BlogGenerator(StubClaude(), None)
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": a["body_markdown"],
                                                     "flagged": []}
    ck = _zeta_ck(["Bravo", "Delta", "Echo", "Zeta"])
    ck["sourcing"]["unsourced"] = [{"tool": "Zeta", "price_only": True, "facts": ["current price"]}]
    gen.finish_pending_blog({"name": "Acme"}, "t", ck, [{"tool": "Zeta", "skip": True}])
    assert gen._removed_brands == []


def test_the_strip_never_touches_the_h1_or_an_unrelated_row():
    body = ("# Acme vs Zeta\n\n| Firm | X |\n| --- | --- |\n| Acme | a |\n| Zeta | z |\n"
            "| Zetamax | q |\n\n## About Zeta\n\nZeta text.\n\n## Pricing\n\nAcme costs $1.\n")
    out, rows, secs = BlogGenerator._strip_removed_brands(body, ["Zeta"])
    assert out.startswith("# Acme vs Zeta"), "the H1 is pinned to the seed and must never be stripped"
    assert "| Zeta | z |" not in out and rows == 1
    assert "| Zetamax | q |" in out, "a word-boundary miss stripped a different brand"
    assert "## About Zeta" not in out and "Zeta text." not in out and secs == 1
    assert "## Pricing" in out and "Acme costs $1." in out


def test_an_explicit_keep_opts_an_empty_brand_out_of_removal():
    """Both choices are real. Keeping an empty brand usually costs the whole table — the modal says
    so — but it is the operator's call, and an explicit keep must be honoured."""
    gen, art = _finish_zeta(["Bravo", "Delta", "Echo", "Zeta"], [{"tool": "Zeta", "keep": True}])
    assert gen._removed_brands == [], "an explicit keep was overridden"


def test_the_pause_item_tells_the_modal_whether_removal_is_available():
    handler_tools = ["Bravo", "Delta", "Echo"]

    def handler(p):
        if "core_topic" in p.lower():
            return {"tools": handler_tools, "peer_tools": handler_tools, "dimensions": ["Price"],
                    "claims": [], "core_topic": "x", "products": [], "generic_options": []}
        return {}
    gen = BlogGenerator(StubClaude(call_handler=handler), None)
    gen._evidence_blocks = []
    s = gen._source_for_completion({"name": "Acme", "category": "c"}, "x",
                                   {"title": "t", "body_markdown": "# t\n"})
    empties = [u for u in s["unsourced"] if not u.get("thin_coverage") and not u.get("price_only")]
    assert empties and all(u["remaining_if_removed"] == 2 for u in empties), \
        "the modal cannot tell that removal would breach the floor"
