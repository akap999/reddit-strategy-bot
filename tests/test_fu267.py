"""FU267 — the generator reads its sources.

Measured across the 27 most recent articles, for the sources they CITE:

    AUTHORITY (gov · NIH · PubMed · PMC · AAP · AAFP · journals)
       snippet only: 131      real page text:  11     ->  92% snippet
    COMMERCIAL / other
       snippet only: 122      real page text: 161     ->  43% snippet

In 18 of 25 articles every cited authority source was a snippet. The writer received a median of
7.7x more marketing copy than science, and on one article 203 characters of clinical evidence for
an 18,000-character YMYL page — so the clinical specifics had to come from memory, and did.

The cause: `base.py` asks a model for `{"title", "url", "fact": "one specific, sourced fact this
page supports"}` and the `fact` — a sentence a model wrote from search results — was stored as an
evidence block's text at 40 sites. Nothing opened the page. The read path existed and was wired
only to the subject brand and its competitors.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.blog_gen import (  # noqa: E402
    BlogGenerator as B, _source_text_cap, _AUTHORITY_TEXT_CAP, _RETAIL_TEXT_CAP,
    _EVIDENCE_TEXT_CAP)

VENT = "The vented base keeps the nipple full of milk rather than air throughout the feed."
LONG_PAGE = ("Introducing the Bottle. " + "Filler about unrelated matters. " * 300
             + VENT + " " + "More filler. " * 300)


def _gen(pages):
    g = B.__new__(B)
    g._claim_pages = dict(pages)
    g._read_sources = 0
    g._summary_sources = []
    g.claude = None
    return g


# ── the page, not a summary ──────────────────────────────────────────────────────────────────────

def test_a_source_block_carries_the_page():
    g = _gen({"https://aap.example/p": (LONG_PAGE, "direct")})
    b = g._source_block("https://aap.example/p", "official · AAP", ["vented", "nipple", "milk"],
                        gloss="a model's one-line gloss")
    assert not b["text"].startswith("[SUMMARY")
    assert b["how"] == "direct"
    assert g._read_sources == 1 and not g._summary_sources


def test_the_gloss_stops_being_the_evidence_and_becomes_the_reason():
    """It keeps the job it can do — WHY this source was picked — and loses the one it never had."""
    g = _gen({"https://aap.example/p": (LONG_PAGE, "direct")})
    b = g._source_block("https://aap.example/p", "official · AAP", ["vented"],
                        gloss="a model's one-line gloss")
    assert b["picked_because"] == "a model's one-line gloss"
    assert "a model's one-line gloss" not in b["text"]


def test_relevance_selection_reaches_a_sentence_past_the_cap():
    """A head-truncation would stop at character 6,000; the claim sits at ~9,000. This is the FU240
    selector doing the job a straight slice cannot — the same reason a 113k-char FDA label was
    being read at 5%."""
    g = _gen({"https://aap.example/p": (LONG_PAGE, "direct")})
    b = g._source_block("https://aap.example/p", "official · AAP", ["vented", "nipple"])
    assert LONG_PAGE.index(VENT) > _AUTHORITY_TEXT_CAP, "the fixture must bury the claim"
    assert "vented base keeps the nipple" in b["text"]


# ── per-class budgets ────────────────────────────────────────────────────────────────────────────
# One number for every class meant the page carrying a clinical claim got the same 2,500 characters
# as a marketplace listing that may only ever evidence a price.

def test_an_authority_page_gets_the_biggest_budget():
    assert _source_text_cap("official · FDA guidance") == _AUTHORITY_TEXT_CAP
    assert _AUTHORITY_TEXT_CAP > _EVIDENCE_TEXT_CAP


def test_a_marketplace_listing_gets_the_smallest():
    for lab in ("retail · Amazon listing", "review · Trustpilot"):
        assert _source_text_cap(lab) == _RETAIL_TEXT_CAP
    assert _RETAIL_TEXT_CAP < _EVIDENCE_TEXT_CAP


def test_a_first_party_page_is_unchanged():
    assert _source_text_cap("Thyseed") == _EVIDENCE_TEXT_CAP


def test_the_budget_is_actually_applied():
    g = _gen({"https://shop.example/x": ("Buy now. " * 4000, "direct")})
    assert len(g._source_block("https://shop.example/x", "retail · Shop")["text"]) <= _RETAIL_TEXT_CAP


# ── a page we could not read must never read as the page ─────────────────────────────────────────

def test_an_unreadable_source_is_marked_and_counted():
    g = _gen({"https://walled.example/p": ("", "blocked")})
    b = g._source_block("https://walled.example/p", "official · Journal", gloss="why it was picked")
    assert b["text"].startswith("[SUMMARY ONLY")
    assert "do not attribute any figure" in b["text"]
    assert b["how"] == "blocked"
    assert g._read_sources == 0 and g._summary_sources == [
        ("official · Journal", "https://walled.example/p", "blocked")]


def test_a_bot_wall_is_not_a_page_even_when_it_is_long():
    """A challenge page served with status 200 is real text of real length."""
    wall = "Just a moment... Enable JavaScript and cookies to continue. " * 40
    g = _gen({"https://walled.example/p": (wall, "direct")})
    b = g._source_block("https://walled.example/p", "official · Journal")
    assert b["text"].startswith("[SUMMARY ONLY") and g._read_sources == 0


def test_a_thin_page_is_not_a_page():
    g = _gen({"https://thin.example/p": ("Two words.", "direct")})
    assert g._source_block("https://thin.example/p", "official · X")["text"].startswith("[SUMMARY")


def test_a_block_with_no_url_does_not_raise():
    g = _gen({})
    b = g._source_block("", "research notes (user-provided)", gloss="the operator's own notes")
    assert b["how"] == "no-url" and b["url"] == ""


# ── the call site ────────────────────────────────────────────────────────────────────────────────

def test_the_ymyl_gather_reads_every_source_it_keeps():
    """`_gather_authoritative_sources` stored `(s["fact"] or title)` and never opened the URL. This
    is the leg that supplies the regulator label and the guideline page on a YMYL article."""
    import inspect, re
    src = inspect.getsource(B._gather_authoritative_sources)
    assert "_source_block" in src, "the authority leg must go through the reader"
    # The gloss may still be PASSED (as `gloss=`); what it may never be again is the block's text.
    assert not re.search(r'"text"\s*:', src), \
        "this leg must not build a block's text itself — `_source_block` reads the page"
