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


# ── step 3b: a menu must not eat the budget ──────────────────────────────────────────────────────
# `relevant_text` keeps a HEAD plus windows around the article's terms. The head exists for a good
# reason — an FDA label's boxed warning and indications are load-bearing and sit at the top. On a
# commercial page the top is the menu, and 59% of every stored block holding 400+ characters is
# navigation-heavy or has two sentences or fewer. So the head allowance was being spent on chrome
# and the term windows, which carry the claim, were what got squeezed out.

from generators.blog_gen import _head_is_chrome, _REL_HEAD_CHARS  # noqa: E402

# verbatim from the stored evidence of a real article
_STOREFRONT = ("Products – Dr. Brown's Skip to content Pause slideshow Play slideshow Free "
               "shipping on orders over $25. See details Free shipping on orders over $25. See "
               "details Shop Bottles Pacifiers Log in Create account Cart " * 6)
_LINK_LIST = ("View and compare Baby bottles & nipples products | Philips Products Support "
              "Products Personal care Oral health Mother and child care Beauty Shaving " * 12)
_LABEL = ("HIGHLIGHTS OF PRESCRIBING INFORMATION. WARNING: RISK OF THYROID C-CELL TUMOURS. In "
          "rodents, semaglutide causes dose-dependent thyroid C-cell tumours. It is unknown "
          "whether this occurs in humans. The recommended starting dose is 0.25 mg weekly. " * 8)


def test_a_storefront_header_is_chrome():
    assert _head_is_chrome(_STOREFRONT, _REL_HEAD_CHARS)


def test_a_bare_link_list_with_no_sentences_is_chrome():
    """The other shape, which carries no navigation WORDING at all. philips.com opens with zero
    sentence ends in 1,800 characters, where a guideline page runs eight or more per thousand."""
    assert _head_is_chrome(_LINK_LIST, _REL_HEAD_CHARS)


def test_a_regulator_label_keeps_its_head():
    """The head is not a mistake — on a label the boxed warning and indications ARE the top, and
    that is why `relevant_text` reserves one."""
    assert not _head_is_chrome(_LABEL, _REL_HEAD_CHARS)


def test_prose_that_merely_mentions_shipping_keeps_its_head():
    import random
    random.seed(1)
    words = "infant colic trial endpoint crying duration parental report cohort analysis".split()
    sents = ["Free shipping of study materials was arranged for all sites."]
    sents += [" ".join(random.choice(words) for _ in range(9)).capitalize() + "." for _ in range(60)]
    assert not _head_is_chrome(" ".join(sents), _REL_HEAD_CHARS)


def test_a_menu_head_buys_a_whole_claim_window_when_it_is_dropped():
    """End to end, and measurably: the budget the menu was eating becomes coverage.

    A single claim would survive either way — it gets a window wherever it sits — so this spreads
    four of them. With the menu head kept, the allowance reaches two; without it, three. That is
    the difference the head was costing on every commercial page in the evidence set."""
    names = ["alpha", "bravo", "charlie", "delta"]
    page = (_STOREFRONT * 3) + "".join(
        f" CLAIMPOINT {n} is stated here. " + "Filler sentence. " * 120 for n in names)
    g = B.__new__(B)
    g._page_terms = ["claimpoint"]
    out = g._render_evidence_blocks(
        [{"label": "Thyseed", "url": "https://t.example/p", "text": page}], char_budget=2400)[0]
    kept = [n for n in names if f"CLAIMPOINT {n}" in out]
    assert len(kept) >= 3, f"the menu is still eating the budget: only {kept} survived"


# ── step 3c: the menu is never recorded in the first place ───────────────────────────────────────
# Skipping the head only stopped the menu EATING the budget; it was still in the stored text, still
# counted toward a block's length, and a term window landing beside it still pulled it in. The page
# says which parts are navigation — `nav`, `footer`, `aside`, `role="navigation"`, a nav-ish class —
# and the extractor was ignoring all of it, keeping only `script` and `style` out.

from generators.brand_enrichment import _extract_visible_text, _looks_blocked  # noqa: E402

_PAGE = """<html><head><title>Thyseed</title></head><body>
<nav class="site-nav">Skip to content Shop Baby Bottles About Us Search Cart</nav>
<header><h1>Thyseed PPSU Anti-Colic Baby Bottle</h1></header>
<div id="cookie-consent">We use cookies. Accept all.</div>
<div class="newsletter">Subscribe for 10% off your first order.</div>
<main><p>The base vent keeps the nipple full of milk rather than air throughout the feed.</p></main>
<aside class="sidebar">You may also like: Bottle Brush, Pacifier</aside>
<footer>Free shipping on orders over $59. Privacy Policy Terms of Service</footer>
</body></html>"""


def test_navigation_footers_and_cookie_bars_are_not_recorded():
    out = _extract_visible_text(_PAGE, 6000)
    for gone in ("Skip to content", "Shop Baby Bottles", "We use cookies", "Subscribe for 10%",
                 "You may also like", "Free shipping", "Privacy Policy"):
        assert gone not in out, gone


def test_the_content_and_the_h1_survive():
    """`header` is deliberately NOT skipped by tag — on many sites it holds the article's own H1."""
    out = _extract_visible_text(_PAGE, 6000)
    assert "Thyseed PPSU Anti-Colic Baby Bottle" in out
    assert "base vent keeps the nipple full of milk" in out


def test_a_page_with_no_semantic_tags_is_still_cleaned():
    """Sites that predate `nav` say it in the class or the role instead."""
    html = ('<body><div role="navigation">Home Shop Cart</div>'
            '<div class="mega-menu">Bottles Pacifiers</div>'
            '<div><p>The vented base reduces swallowed air during a feed.</p></div></body>')
    out = _extract_visible_text(html, 6000)
    assert "Home Shop Cart" not in out and "Bottles Pacifiers" not in out
    assert "vented base reduces swallowed air" in out


def test_the_thin_content_check_still_sees_the_whole_page():
    """Two different questions. "Is this useful evidence?" drops the menu; "did the server give us
    a document at all?" must not — or stripping navigation would push real pages under the
    thin-content floor and send the fetch ladder to the METERED residential proxy for nothing."""
    nav_only = ('<body><nav>' + "Home Shop Cart About Contact Search Login " * 30
                + '</nav><main><p>Hi.</p></main></body>')
    assert len(_extract_visible_text(nav_only, 6000)) < 200, "as evidence it is nearly empty"
    assert _looks_blocked(nav_only) == "", "but the server did return a page"
