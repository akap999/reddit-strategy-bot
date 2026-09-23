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
    assert "_read_sources_parallel" in src or "_source_block" in src, \
        "the authority leg must go through the reader"
    # The gloss may still be PASSED (as `gloss=`); what it may never be again is the block's text.
    assert not re.search(r'"text"\s*:', src), \
        "this leg must not build a block's text itself — `_source_block` reads the page"


# ── the gloss is nowhere the evidence ────────────────────────────────────────────────────────────

def test_no_gather_path_stores_a_search_gloss_as_a_blocks_text():
    """The defect was not one site. `base.py` asks a model for a one-line `fact` about each search
    result, and that string was an evidence block's text at forty places. This is the guard that
    stops the next one being added: a block's text comes from `_source_block`, which reads."""
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "generators", "blog_gen.py")).read()
    bad = re.findall(r'"text":\s*(?:\(?\s*)?(?:fct|fc|fact|_ptxt)\b[^\n]*', src)
    assert not bad, "a search gloss is being stored as evidence text again:\n  " + "\n  ".join(bad)


def test_the_reader_is_used_by_the_paths_that_supply_a_ymyl_article():
    """The legs that carry a clinical claim: the regulator/guideline search, the operator's own
    "source from AAP" instruction, the primary-source completion, and the newest-change sweep."""
    import inspect
    for fn in (B._gather_authoritative_sources, B._instruction_source_blocks,
               B._gather_recent_changes, B._gather_independent_sources,
               B._guide_answer_evidence):
        src = inspect.getsource(fn)
        assert "_read_sources_parallel" in src or "_source_block" in src, fn.__name__


def test_the_reads_happen_in_parallel():
    """Serial reads would put one slow host on the critical path of every gather. A dozen sources
    at ten seconds each is the difference between a generation and a timeout."""
    import threading
    import time

    class _SlowPages(dict):
        def __init__(self):
            super().__init__()
            self.inflight = 0
            self.max_inflight = 0
            self._lock = threading.Lock()

        def __contains__(self, _url):
            return True

        def __getitem__(self, _url):
            with self._lock:
                self.inflight += 1
                self.max_inflight = max(self.max_inflight, self.inflight)
            try:
                time.sleep(0.05)
                return ("Reference page with several sentences of ordinary prose. " * 20, "direct")
            finally:
                with self._lock:
                    self.inflight -= 1

    g = B.__new__(B)
    g._claim_pages = _SlowPages()
    g._read_sources = 0
    g._summary_sources = []
    g.claude = None
    pending = [(f"https://x{i}.example/p", f"official · S{i}", "gloss") for i in range(6)]
    blocks = g._read_sources_parallel(pending, ["reference"])
    assert len(blocks) == 6 and g._read_sources == 6
    assert g._claim_pages.max_inflight >= 2, "the reads must overlap"


# ── step 3: the budget is per class, and it is spent on real page text ───────────────────────────
# The writer's evidence used to be `char_budget // len(blocks)`, hard-capped at 2,500 and applied
# as a slice. Three faults in one line: a marketplace listing got the same allowance as an FDA
# label; the ceiling undid the per-class caps as soon as a budget was set; and a slice takes the
# HEAD, which is the article's own topic, throwing away the tail where the claim lives.

from generators.blog_gen import (  # noqa: E402
    _budget_evidence, _source_weight, _EVIDENCE_MIN_PER_SOURCE)
from generators.brand_enrichment import relevant_text  # noqa: E402


def _blk(label, n):
    return {"label": label, "text": "x" * n}


MIX = ([_blk("official · FDA label", 6000) for _ in range(6)]
       + [_blk("Thyseed", 2500) for _ in range(5)]
       + [_blk("retail · Amazon", 800) for _ in range(4)])


def test_an_authority_page_outweighs_a_marketplace_listing():
    assert _source_weight("official · FDA label") == 3.0
    assert _source_weight("retail · Amazon") == 0.6
    assert _source_weight("Thyseed") == 2.0, "a first-party page sits between them"


def test_nothing_is_trimmed_when_it_all_fits():
    want = sum(len(b["text"]) for b in MIX)
    assert _budget_evidence(MIX, want + 1000) == {i: len(b["text"]) for i, b in enumerate(MIX)}


def test_a_block_that_wants_less_than_its_share_hands_the_rest_back():
    """Water-filling. An equal division would leave a listing holding budget it cannot use while
    the page carrying the clinical claim is cut."""
    a = _budget_evidence(MIX, 40000)
    assert sum(a[i] for i in range(6, 15)) == 2500 * 5 + 800 * 4, "the small blocks stay whole"
    assert sum(a[i] for i in range(6)) == 40000 - (2500 * 5 + 800 * 4), "authority takes the rest"


def test_under_real_pressure_the_listing_gives_way_first():
    a = _budget_evidence(MIX, 20000)
    per_auth = a[0]
    per_retail = a[11]
    assert per_auth > per_retail * 3, (per_auth, per_retail)
    assert sum(a.values()) <= 20000


def test_no_source_is_starved_below_the_floor():
    """A block cut to nothing is a source the writer cannot use but still sees cited."""
    many = [_blk("official · X", 6000) for _ in range(60)]
    a = _budget_evidence(many, 5000)
    assert min(a.values()) >= min(_EVIDENCE_MIN_PER_SOURCE, 6000)


def test_the_budget_is_spent_on_the_passage_not_the_head():
    """The whole point of trimming with `relevant_text` rather than a slice."""
    page = "Intro. " * 50 + "THE CLAIM IS HERE. " + "Tail. " * 500
    out = relevant_text(page, ["claim"], 800)
    assert "THE CLAIM IS HERE" in out and len(out) <= 900


def test_trimming_never_invents_text():
    """No summary, no paraphrase — every character survives from the page."""
    page = "Alpha beta. " * 200 + "THE CLAIM. " + "Gamma delta. " * 200
    out = relevant_text(page, ["claim"], 600)
    for frag in out.replace("…", "\n").split("\n"):
        frag = frag.strip(" []")
        if len(frag) > 20:
            assert frag in page, frag[:60]


def test_the_renderer_applies_the_budget():
    """Functional, and on the renderer BOTH writers share — the Claude prompt and the open-model
    one. A source-level check on one of them left the other undefended."""
    g = B.__new__(B)
    g._page_terms = ["claim"]
    # the claim must sit BEYOND the allowance, or a hard slice would keep it by luck and the test
    # would pass whether the budget is spent on relevance or on the first N characters
    page = "Intro. " * 1000 + "THE CLAIM IS HERE. " + "Tail. " * 400
    blocks = ([{"label": "official · FDA", "url": "https://x.gov/a", "text": page}] * 4
              + [{"label": "retail · Shop", "url": "https://s.example/b", "text": page}] * 4)
    rendered = g._render_evidence_blocks(blocks, char_budget=12000)
    assert len(rendered) == 8, "every source stays represented"
    body = "\n\n".join(rendered)
    assert len(body) <= 13000, len(body)
    assert len(rendered[0]) < page.index("THE CLAIM IS HERE"), \
        "the allowance must be smaller than the head, or a slice would pass this too"
    auth = len(rendered[0])
    listing = len(rendered[-1])
    assert auth > listing * 2, (auth, listing)
    assert "THE CLAIM IS HERE" in rendered[0], "the allowance buys the passage, not the head"


def test_both_writers_share_one_renderer():
    import inspect
    assert "_render_evidence_blocks" in inspect.getsource(B._writer_evidence_str)
    assert "_render_evidence_blocks" in inspect.getsource(B._gather_evidence)
