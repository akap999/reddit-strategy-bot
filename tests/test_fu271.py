"""FU271 — what a page says about ITSELF.

Source quality was a domain allowlist plus a title shape, so a listicle on a domain nobody had
listed passed as an ordinary third-party source. That is how BestReviews, Mom Loves Best, Today's
Parent and Birch came to carry clinical claims on a reported article.

Now that the pages are read, they say it themselves. Measured live on those sources:

    babygearlab.com   36,007c   affiliate · author · dated · method · BUY
    todaysparent.com  22,474c   affiliate · author · dated · BUY
    birchstore.com    35,154c   author · dated
    aafp.org          22,851c   author · CITES SOURCES

Vertical-neutral: "we may earn a commission" and "add to cart" mean the same on a mortgage
comparison site as on a bottle roundup, and neither names an industry.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.blog_gen import BlogGenerator as B  # noqa: E402

FILLER = "Filler sentence about the topic. " * 200
LISTICLE = ("Best Bottles of 2026. We may earn a commission from links on this page. "
            "Check price on Amazon. Add to cart. " + FILLER)
PAPER = ("Methods: we evaluated 12 trials (n = 1,055). References. Smith et al. doi:10.1000/x "
         "[1] [2] [3] confidence interval p < 0.05. " + FILLER)
BRAND_SHOP = ("Add to cart. Buy now. Free shipping on orders over $59. " * 6
              + "Our bottle uses a base vent. " * 40)


def test_an_affiliate_listicle_is_recognised_without_knowing_its_domain():
    intent, why = B._page_intent(LISTICLE)
    assert intent == "commerce"
    assert "affiliate" in why


def test_a_document_that_cites_its_sources_is_recognised():
    assert B._page_intent(PAPER)[0] == "reference"


def test_a_brands_own_shop_is_NOT_a_listicle():
    """The discriminator is earning a commission on OTHER people's products. Measured:
    drbrownsbaby.com shows 13 buy prompts and no affiliate disclosure — a brand selling its own
    product is a legitimate first-party source for its own specs."""
    assert B._page_intent(BRAND_SHOP)[0] == ""


def test_one_stray_shipping_line_does_not_make_a_shop():
    body = "Free shipping on orders over 50. " + "References [1] [2] [3] et al. doi: methods n = 40. " * 10
    assert B._page_intent(body)[0] == "reference"


def test_a_retailer_that_heavily_cites_is_left_unclassified_rather_than_guessed():
    body = ("We may earn a commission. Add to cart. "
            + "References [1] et al. doi: methods n=5 p < 0.05 sample size confidence interval. " * 20)
    assert B._page_intent(body)[0] == ""


def test_the_escape_is_relative_not_an_absolute_floor():
    """An absolute floor of six rigour markers was wrong on real data: babygearlab.com scores
    com=1, buy=3, rig=7 — a monetised review site that says "we tested" a few times — and the
    escape handed it a free pass."""
    body = ("We may earn a commission. " + "Check price. " * 3
            + "We tested methods references [1] [2] et al. doi: " * 2 + FILLER)
    assert B._page_intent(body)[0] == "commerce"


def test_a_page_too_short_to_judge_is_not_judged():
    """The fixture has to be one that WOULD classify but for its length — "hi" has no signals at
    all and passes whether the length gate is there or not."""
    dense = "We may earn a commission. Add to cart. Check price. "      # ~51 chars, com=1 buy=2
    assert B._page_intent(dense)[0] == "", "under the floor, nothing is judged"
    assert B._page_intent(dense * 40)[0] == "commerce", "the same page, long enough to judge"
    assert B._page_intent("")[0] == ""


def test_a_single_affiliate_line_is_not_enough_on_its_own():
    """One boilerplate disclosure in a footer does not make a serious outlet a listicle — the rule
    wants corroboration, either a second disclosure or buy language."""
    body = "We may earn a commission. " + FILLER
    assert B._page_intent(body)[0] == ""


def test_one_or_two_citation_markers_do_not_make_a_reference():
    """Almost any page has a bracket or the word "methods" somewhere."""
    assert B._page_intent("See references. " + FILLER)[0] == ""
    assert B._page_intent("See references [1]. " + FILLER)[0] == ""


def test_the_rule_names_no_industry():
    for rx in (B._COMMERCE_RE, B._BUY_RE, B._RIGOR_RE):
        low = rx.pattern.lower()
        for word in ("bottle", "baby", "colic", "semaglutide", "glp", "bestreviews", "babygearlab"):
            assert word not in low, f"{word!r} in {rx.pattern[:40]}"


# ── it reaches the block and the writer ──────────────────────────────────────────────────────────

def _gen(pages):
    g = B.__new__(B)
    g._claim_pages = dict(pages)
    g._read_sources = 0
    g._summary_sources = []
    g._commerce_sources = []
    g._page_terms = []
    g.claude = None
    return g


def test_the_block_records_it_and_the_writer_is_told():
    g = _gen({"https://round.example/best": (LISTICLE, "direct")})
    b = g._source_block("https://round.example/best", "third-party · Best Bottles 2026")
    assert b["intent"] == "commerce"
    line = g._render_evidence_blocks([b])[0]
    assert "affiliate/commerce page" in line and "nothing else" in line
    assert g._commerce_sources and g._commerce_sources[0][1] == "https://round.example/best"


def test_the_operator_is_told_which_sources_were_listings():
    g = _gen({"https://round.example/best": (LISTICLE, "direct")})
    g._source_block("https://round.example/best", "third-party · Best Bottles 2026")
    note = g._source_read_note([], "")
    assert "monetised listing" in note and "Best Bottles 2026" in note


def test_a_reference_page_is_marked_too():
    g = _gen({"https://j.example/p": (PAPER, "direct")})
    b = g._source_block("https://j.example/p", "official · A Paper")
    assert b["intent"] == "reference"
    assert "cites its own sources" in g._render_evidence_blocks([b])[0]


# ── FU271b: dropped, not merely reported ─────────────────────────────────────────────────────────
# Detection alone left the article still citing BestReviews, Mom Loves Best and Today's Parent for
# clinical claims. The point of reading a page is to be able to act on what it says about itself.

def test_a_monetised_listing_never_becomes_evidence():
    g = _gen({})
    blocks = [{"label": "official · AAFP", "url": "https://aafp.org/x", "intent": "reference"},
              {"label": "reference · BestReviews", "url": "https://bestreviews.com/x",
               "intent": "commerce"},
              {"label": "Thyseed", "url": "https://thyseed.example/p", "intent": ""}]
    kept = g._drop_commerce_sources(blocks)
    assert [b["label"] for b in kept] == ["official · AAFP", "Thyseed"]


def test_a_brands_own_shop_survives_the_drop():
    """It is never classified commerce — the classifier requires an affiliate marker, and a brand
    selling its own product carries none."""
    g = _gen({"https://shop.example/p": (BRAND_SHOP, "direct")})
    b = g._source_block("https://shop.example/p", "Thyseed")
    assert b["intent"] == "" and g._drop_commerce_sources([b]) == [b]


def test_the_note_says_they_were_dropped_not_merely_noticed():
    g = _gen({"https://round.example/best": (LISTICLE, "direct")})
    g._source_block("https://round.example/best", "third-party · Best Bottles 2026")
    note = g._source_read_note([], "")
    assert "DROPPED as monetised listing" in note
    assert "that claim now has none" in note, "the consequence has to be stated"


def test_the_drop_runs_on_sources_added_after_the_draft_too():
    """A listing that arrives late is no more citable than one that arrived early."""
    import inspect
    src = inspect.getsource(B._reconcile_and_finish)
    assert "_drop_commerce_sources(fresh)" in src


def test_the_drop_runs_on_the_MAIN_gather_too():
    """Testing the late path alone left the main one undefended — it is where most sources arrive."""
    import inspect
    src = inspect.getsource(B._gather_evidence)
    assert "_drop_commerce_sources(blocks)" in src
    i = src.index("_drop_commerce_sources(blocks)")
    assert "self._evidence_blocks = list(blocks)" in src[i:i + 400], \
        "it has to run BEFORE the blocks become the evidence set"
