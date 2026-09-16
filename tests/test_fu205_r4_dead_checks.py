"""FU205 R4 — checks that could not fire, and a function with three passing tests and no callers.

Three findings from the audit:

  * `_canonical_price_item` was DEAD CODE. Its docstring has claimed since FU158 that it makes an
    operator-set price "the single source of truth for the SUBJECT's pricing cell AND the JSON-LD
    Offer". Its only references in the entire repo were its own tests. A green suite proving a
    function works while the product hand-rolls the same rule beside it is the purest form of the
    complaint that started this round.
  * the orphan-citation check was DEAD BY CONSTRUCTION — it compared `[S#]` against
    `len(self._evidence_blocks)` while running AFTER the renumber that makes that impossible.
  * six check notes were never initialised and never rebuilt on resume, so a paused blog reported
    clean on every check that depends on sourcing — the path FU204 deliberately made busier.
"""
import json

import pytest

from generators.blog_gen import BlogGenerator, _canonical_price_item, build_blog_jsonld
from tests.stubs import StubClaude

OPERATOR = {"product": "tirzepatide", "value": "$647 / 3 months", "operator_set": True,
            "source_url": "https://acme.example"}
SCRAPED = {"product": "tirzepatide", "value": "$29 per month", "operator_set": False,
           "source_url": "https://acme.example/tirzepatide"}
STALE_GENERAL = {"product": "", "value": "$79/mo", "operator_set": False,
                 "source_url": "https://acme.example/mens-trt"}


def _brand(items):
    return {"name": "Acme", "domain_url": "https://acme.example",
            "key_facts": json.dumps({"pricing": {"items": items}})}


def _offers(graph):
    for node in graph["@graph"]:
        if node.get("@type") == "Product":
            o = node["offers"]
            return o if isinstance(o, list) else [o]
    return []


# ── the dead function, wired ──────────────────────────────────────────────────────────────────
def test_the_operator_price_wins_the_jsonld_offer_over_a_scraped_one():
    """FU158 + FU163's intent, now actually enforced by the function that claimed to enforce it."""
    g = build_blog_jsonld({"seed": "which clinics prescribe tirzepatide", "title": "Tirzepatide"},
                          _brand([SCRAPED, STALE_GENERAL, OPERATOR]))
    offers = _offers(g)
    assert len(offers) == 1
    assert offers[0]["price"] == "647", "the scraped or stale price was advertised"
    assert "647" in offers[0]["description"]


def test_the_selection_is_the_shared_function_not_a_second_copy_of_the_rule():
    """A test that fails if the call site is removed again — the three existing tests prove the
    function WORKS, not that anything USES it, which is exactly how it rotted."""
    import inspect
    src = inspect.getsource(build_blog_jsonld)
    assert "_canonical_price_item(" in src, "the JSON-LD Offer stopped using the shared rule"
    # and it agrees with the hand-rolled precedence it replaced
    item = _canonical_price_item({"pricing": {"items": [SCRAPED, STALE_GENERAL, OPERATOR]}},
                                 "which clinics prescribe tirzepatide")
    assert item and item["value"] == OPERATOR["value"] and item["operator_set"]


def test_a_specific_product_with_no_canonical_item_never_borrows_another_price():
    assert _canonical_price_item({"pricing": {"items": [STALE_GENERAL]}}, "tirzepatide") is None


# ── the canonical-price check (the SUBJECT's pricing cell) ────────────────────────────────────
def _finalize(body, brand, seed="which clinics prescribe tirzepatide"):
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    art = {"title": seed, "meta_description": "d", "body_markdown": body}
    gen._finalize_article(brand, seed, art, body, with_linkedin=False)
    return art


TABLE = ("# which clinics prescribe tirzepatide\n\n## Quick answer\n\nAcme.\n\n"
         "| Clinic | Starting Price |\n| --- | --- |\n"
         "| Acme | {price} |\n| Bravo | $199/mo |\n| Delta | $249/mo |\n| Echo | $299/mo |\n\n"
         "## FAQ\n\n### Q?\n\nA.\n")


def test_a_subject_cell_that_contradicts_the_operator_price_is_flagged():
    art = _finalize(TABLE.format(price="$29/mo"), _brand([OPERATOR]))
    hits = [w for w in art.get("warnings", []) if w["check"] == "canonical-price"]
    assert hits, "the operator's canonical price is still not authoritative for the cell"
    assert "$647" in hits[0]["detail"]


def test_a_subject_cell_that_matches_the_operator_price_is_silent():
    art = _finalize(TABLE.format(price="$647 / 3 months"), _brand([OPERATOR]))
    assert not [w for w in art.get("warnings", []) if w["check"] == "canonical-price"]


def test_no_operator_price_means_no_check():
    art = _finalize(TABLE.format(price="$29/mo"), _brand([SCRAPED]))
    assert not [w for w in art.get("warnings", []) if w["check"] == "canonical-price"]


# ── the orphan-citation check, made reachable ─────────────────────────────────────────────────
def test_a_marker_with_no_sources_entry_is_now_caught():
    """The old form compared against `_evidence_blocks` AFTER the renumber that makes the condition
    impossible. The reachable question is whether the marker resolves in the list the READER sees —
    which is exactly what a hand edit or an import can break."""
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    body = ("# t\n\nA claim [S1]. Another [S4].\n\n"
            "## Sources\n\n- [S1] Acme — https://acme.example\n")
    issues = gen._verify_consistency({"name": "Acme"}, {"body_markdown": body})
    orphans = [i for i in issues if i["kind"] == "citation" and "Sources list" in i["problem"]]
    assert orphans and "[S4]" in orphans[0]["detail"]


def test_a_body_whose_markers_all_resolve_is_silent():
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    body = ("# t\n\nA claim [S1].\n\n## Sources\n\n- [S1] Acme — https://acme.example\n")
    issues = gen._verify_consistency({"name": "Acme"}, {"body_markdown": body})
    assert not [i for i in issues if i["kind"] == "citation"]


# ── the six notes that vanished on resume ─────────────────────────────────────────────────────
NOTE_ATTRS = ("_peer_note", "_auth_note", "_facts_note", "_price_warn", "_invented_note",
              "_table_punt_note", "_core_mechanics", "_sibling_urls", "_article_tools")


@pytest.mark.parametrize("attr", NOTE_ATTRS)
def test_every_check_note_is_declared_on_a_fresh_generator(attr):
    """`getattr(self, x, default)` means an absent attribute silently reads as 'clean'."""
    assert hasattr(BlogGenerator(StubClaude(), None), attr), f"{attr} is only ever implied"


def test_the_check_notes_survive_a_pause_and_resume():
    """Sourcing does NOT re-run on a FU79 resume, so without the checkpoint every check that
    depends on it reports clean — on exactly the path FU204's price-ask made busier."""
    paused = BlogGenerator(StubClaude(), None)
    paused._peer_note = "peer-check: only 1 same-type competitor"
    paused._price_warn = "price-check: Acme's price was not confirmed"
    paused._core_mechanics = ["the race to issue", "forum non conveniens"]
    paused._sibling_urls = {"https://acme.example/guide"}
    notes = paused._check_notes()
    assert json.loads(json.dumps(notes)) == notes, "the checkpoint must stay JSON-safe"

    resumed = BlogGenerator(StubClaude(), None)
    assert resumed._peer_note == "" and resumed._sibling_urls == set()
    resumed._restore_check_notes(notes)
    assert resumed._peer_note == "peer-check: only 1 same-type competitor"
    assert resumed._price_warn == "price-check: Acme's price was not confirmed"
    assert resumed._core_mechanics == ["the race to issue", "forum non conveniens"]
    assert resumed._sibling_urls == {"https://acme.example/guide"}


def test_a_resumed_blog_reports_the_warnings_sourcing_found():
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    gen._restore_check_notes({"_peer_note": "peer-check: only 1 same-type competitor",
                              "_price_warn": "price-check: not confirmed"})
    art = {"title": "t", "meta_description": "d", "body_markdown": "# t\n\nBody.\n"}
    gen._finalize_article({"name": "Acme"}, "t", art, "# t\n\nBody.\n", with_linkedin=False)
    keys = {w["check"] for w in art.get("warnings", [])}
    assert {"peer-check", "price-check"} <= keys, "a resumed blog still reports clean"
