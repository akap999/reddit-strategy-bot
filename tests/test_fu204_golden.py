"""FU204 golden anchor — the REAL Thyseed export the operator handed back.

Why this file exists at all. FU54 Change 6 committed `v9_blog.html` as a permanent anchor for exactly
this class of defect, and then nothing was ever added to it. Every round after FU54 verified itself
against fixtures written by the same person who wrote the code under test, and four defects shipped
green through 588 passing tests:

  1. `scrub_markdown_formatting` rewrote `*[Add author byline before publishing]*` — prepended to
     EVERY article by FU152 — into `- [Add author byline before publishing]*`. 28 FU202 tests passed
     because not one of them ran the formatter over a body the pipeline actually produces.
  2. Six claims cited sources belonging to a different brand or a different class of page.
  3. Trustpilot RATING pages were "reputable" evidence for a material claim, because trustpilot.com
     sat in a SaaS-era `_THIRD_PARTY_DOMAINS` list that exempts a domain from the affiliate filter.
  4. Half the Starting Price column was `—` and the column shipped anyway, under a sentence
     apologising that the prices "were not retrievable".

So every assertion below runs the REAL pipeline functions over the REAL shipped bytes. Pure/offline
($0). If one of these ever fails again, the product is broken — not the fixture.
"""
import html
import os
import re

import pytest

from generators.blog_gen import BlogGenerator, scrub_markdown_formatting
from tests.stubs import StubClaude

FIXTURE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "thyseed_blog.html")

BRAND = {
    "name": "Thyseed",
    "domain_url": "https://thyseed.com",
    "competitor_domains": {"Dr Brown's": "drbrowns.com", "Tommee Tippee": "tommeetippee.com",
                           "Nanobebe": "nanobebe.com", "Philips Avent": "philips.com"},
}
TOOLS = ["Dr Brown's", "Tommee Tippee", "Nanobebe", "Philips Avent", "MAM", "Pigeon"]

# The byline FU152 prepends to every article, verbatim. The fixture proves it reached the reader
# MANGLED; this is the markdown that produced it.
BYLINE_MD = "*[Add author byline before publishing]*"


def _load():
    if not os.path.exists(FIXTURE):
        pytest.skip("tests/fixtures/thyseed_blog.html not present")
    return open(FIXTURE, encoding="utf-8").read()


def _gen():
    return BlogGenerator(StubClaude(), None)


def _table_to_markdown(htmltext):
    """The export's FIRST <table> back into Markdown — one `|`-row per <tr>, tags stripped."""
    tbl = re.search(r"<table.*?>(.*?)</table>", htmltext, re.DOTALL | re.IGNORECASE)
    assert tbl, "no comparison <table> in the export"
    rows = []
    for tr in re.findall(r"<tr.*?>(.*?)</tr>", tbl.group(1), re.DOTALL | re.IGNORECASE):
        cells = re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", tr, re.DOTALL | re.IGNORECASE)
        cells = [html.unescape(re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
        if cells:
            rows.append("| " + " | ".join(cells) + " |")
    # a rendered <table> has no separator row; the resolver needs one
    if len(rows) >= 2:
        ncol = rows[0].count("|") - 1
        rows.insert(1, "| " + " | ".join(["---"] * ncol) + " |")
    return "\n".join(rows) + "\n"


def _data_cells(md):
    out = []
    for ln in md.splitlines()[2:]:
        if ln.strip().startswith("|"):
            out.extend(c.strip() for c in ln.strip().strip("|").split("|"))
    return out


# ── the fixture really is the failure ────────────────────────────────────────────────────────────
def test_the_export_is_the_failure_the_operator_reported():
    raw = _load()
    assert "Add author byline before publishing" in raw
    assert "not retrievable" in raw, "the punt sentence shipped"
    md = _table_to_markdown(raw)
    assert "Starting Price" in md.splitlines()[0]
    assert sum(1 for c in _data_cells(md) if c in ("—", "-", "")) == 3, "3 of 6 prices were blank"


# ── Change 1 — the byline is an italic line, not a bullet ────────────────────────────────────────
def test_the_byline_survives_the_formatter_unchanged():
    assert scrub_markdown_formatting(BYLINE_MD)[0].strip() == BYLINE_MD


def test_the_byline_renders_as_emphasis_not_a_list_item():
    """Through the EXPORT chain, because that chain is what produced the shipped HTML."""
    md = pytest.importorskip("markdown")
    import app as appmod
    body = scrub_markdown_formatting(BYLINE_MD + "\n\nSome opening paragraph.\n")[0]
    rendered = md.markdown(appmod._linkify_md_urls(appmod._normalize_md_lists(
        appmod._escape_md_hashtag_lines(body))), extensions=["tables"])
    assert "<li>" not in rendered, "the byline rendered as a list item"
    assert "<em>[Add author byline before publishing]</em>" in rendered


def test_the_reviewer_and_disclosure_lines_are_protected_too():
    for line in ("*Reviewed by Dr. Jane Roe, MD*", "*Disclosure: Thyseed sells the products discussed.*"):
        assert scrub_markdown_formatting(line)[0].strip() == line


def test_a_genuine_bullet_missing_its_space_is_still_fixed():
    assert scrub_markdown_formatting("*item missing a space")[0].strip() == "- item missing a space"
    assert scrub_markdown_formatting("-item")[0].strip() == "- item"
    assert scrub_markdown_formatting("1.item")[0].strip() == "1. item"


def test_a_stray_trailing_bold_marker_does_not_become_four_asterisks():
    assert scrub_markdown_formatting("Some **bold** text**")[0].strip() == "Some **bold** text"
    assert scrub_markdown_formatting("Some **bold text")[0].strip() == "Some **bold text**"


# ── Change 4 — no comparison cell may be empty ───────────────────────────────────────────────────
def test_the_real_table_comes_back_with_no_empty_cell():
    md = _table_to_markdown(_load())
    out = _gen()._resolve_table_punts(md)
    assert out.strip(), "the table must not collapse — three dimensions are fully answerable"
    assert all(c and c not in ("—", "-") for c in _data_cells(out))
    assert "Starting Price" not in out.splitlines()[0], "the half-blank price column is gone"
    for kept in ("Best For", "Material", "Anti-Colic"):
        assert kept in out.splitlines()[0], kept
    assert len([r for r in out.splitlines() if r.startswith("|")]) - 2 == 6, "no row is ever dropped"


def test_a_single_gap_is_enough_to_drop_a_column():
    md = ("| Bottle | Material | Price |\n| --- | --- | --- |\n"
          "| A | Glass | $10 |\n| B | PP | $12 |\n| C | PP | — |\n")
    out = _gen()._resolve_table_punts(md)
    assert "Price" not in out.splitlines()[0]
    assert "Material" in out.splitlines()[0]


def test_a_fully_filled_table_is_untouched_and_the_pass_is_idempotent():
    md = ("| Bottle | Material | Price |\n| --- | --- | --- |\n"
          "| A | Glass | $10 |\n| B | PP | $12 |\n")
    assert _gen()._resolve_table_punts(md) == md
    once = _gen()._resolve_table_punts(_table_to_markdown(_load()))
    assert _gen()._resolve_table_punts(once) == once


def test_a_table_with_no_answerable_dimension_is_removed_not_shipped_one_column_wide():
    gen = _gen()
    out = gen._resolve_table_punts("| Firm | A | B |\n| --- | --- | --- |\n| X | v | — |\n| Y | — | v |\n")
    assert "|" not in out, "a name-only table is broken output, not a comparison"
    assert "no dimension" in (gen._table_punt_note or "")


# ── Change 4 — the punt sentences that shipped under that table ──────────────────────────────────
def test_the_two_real_punt_sentences_do_not_survive():
    gen = _gen()
    for sent in ("Note: prices are not confirmed from a first-party source in the available evidence.",
                 "Tommee Tippee's exact USD pricing was not retrievable."):
        assert gen._scrub_punts(sent).strip() == "", sent


def test_ordinary_prose_that_merely_contains_those_words_survives():
    """`_PUNT_MEANING_RE` contains `unknown` and `n/a`; deleting a whole SENTENCE on those would eat
    real consumer prose. The prose rule requires a sourcing-context word for exactly this reason."""
    gen = _gen()
    for sent in ("The cause of colic is unknown.",
                 "Dr. Brown's confirmed the PPSU bottle is rated to 180C.",
                 "Parents could not agree on which teat was best.",
                 "Thyseed lists the 2-pack at $29.99 on its own site."):
        assert gen._scrub_punts(sent).strip() == sent, sent


# ── Change 2 — a brand-specific claim must cite that brand ───────────────────────────────────────
def _blocks():
    return [
        {"label": "Thyseed", "url": "https://thyseed.com/", "text": "Thyseed bottles"},
        {"label": "Dr Brown's", "url": "https://drbrownsbaby.com/", "text": "Dr Brown's"},
        {"label": "review · Thyseed rated Great", "url": "https://trustpilot.com/review/thyseed.com",
         "text": "rated Great 3.8/5"},
        {"label": "review · rated Great 3.8/5", "url": "https://trustpilot.com/review/x", "text": "rating"},
        {"label": "retail · Amazon listing", "url": "https://amazon.com/dp/B0", "text": "bottle 3-pack"},
        {"label": "Dr Brown's", "url": "https://drbrowns.com/glass", "text": "glass options"},
        {"label": "retail · Babylist", "url": "https://babylist.com/x", "text": "glass bottles"},
        {"label": "official · AAP", "url": "https://aap.org/guide", "text": "feeding guidance"},
        {"label": "official · HealthyChildren", "url": "https://healthychildren.org/x", "text": "guidance"},
        {"label": "Dr Brown's", "url": "https://drbrowns.com/", "text": "Dr Brown's home"},
        {"label": "Tommee Tippee", "url": "https://tommeetippee.com/", "text": "Tommee Tippee"},
    ]


@pytest.mark.parametrize("sentence", [
    "Nanobebe uses medical-grade silicone and is BPA-free [S9][S10].",
    "Nanobebe starts From $16.99 [S11].",
    "Tommee Tippee is listed as a best seller on tommeetippee.com [S8].",
])
def test_a_claim_cited_to_another_brands_source_is_flagged(sentence):
    note = _gen()._citation_attribution_check(sentence, _blocks(), BRAND, TOOLS)
    assert note.startswith("citation-check:"), sentence


@pytest.mark.parametrize("sentence", [
    "Dr Brown's lists its Options+ range on its own site [S2].",   # cites its OWN source
    "Nanobebe is lighter than Dr Brown's [S11].",                   # two brands → ambiguous
    "Glass bottles are heavier than polypropylene ones.",           # no citation at all
])
def test_a_correctly_cited_or_ambiguous_sentence_is_silent(sentence):
    assert _gen()._citation_attribution_check(sentence, _blocks(), BRAND, TOOLS) == ""


# ── Change 3 — a rating or retail page is not evidence for a spec ────────────────────────────────
@pytest.mark.parametrize("sentence", [
    "Thyseed's vent is clinically proven to reduce colic [S5].",                 # retail listing
    "Borosilicate glass, heat and thermal shock-resistant, is used here [S3][S4].",  # rating pages
])
def test_a_spec_claim_resting_only_on_a_rating_or_retail_page_is_flagged(sentence):
    assert _gen()._source_class_check(sentence, _blocks()).startswith("source-class:"), sentence


def test_the_same_claim_cited_to_the_brands_own_page_is_silent():
    assert _gen()._source_class_check("The bottle is BPA-free [S2].", _blocks()) == ""


def test_a_rating_page_is_no_longer_reputable_evidence_but_a_listing_still_prices():
    from generators.blog_gen import _is_affiliate_review, _source_class
    assert _is_affiliate_review({"url": "https://www.trustpilot.com/review/thyseed.com",
                                 "title": "Thyseed rated 'Great' 3.8/5"}) is True
    assert _is_affiliate_review({"url": "https://www.amazon.com/dp/B01",
                                 "title": "Tommee Tippee 3-pack"}) is False
    assert _source_class("https://www.trustpilot.com/review/x") == "review"
    assert _source_class("https://www.amazon.com/dp/B01") == "retail"
    assert _source_class("https://www.forbes.com/x") == ""


# ── Change 6 — the price ask must reach the operator ─────────────────────────────────────────────
def test_a_price_only_pause_is_not_deleted_just_because_the_brand_was_found():
    """The bug that answered the operator's question. FU161 queued "current price" for a competitor
    with no confirmed price; FU189's re-check then dropped any pause item whose brand appears in the
    evidence — always true for a price-only item, since it is BY DEFINITION about a brand we sourced
    and are missing one fact from. So the ask was generated and deleted, every time."""
    gen = _gen()
    fresh = [{"label": "Tommee Tippee", "url": "https://tommeetippee.com/closer-to-nature",
              "text": "Closer to Nature bottle, anti-colic valve."},
             {"label": "retail · Amazon", "url": "https://amazon.com/dp/B01",
              "text": "Tommee Tippee 3-pack, $24.99"}]
    named = gen._blocks_naming("Tommee Tippee", fresh)
    assert named, "the brand IS present in the evidence — that is what used to delete the ask"
    assert gen._has_confirmed_price(named, "tommeetippee.com") is False, (
        "a retail listing is not a confirmed price")


def test_the_ask_is_dropped_once_a_real_price_arrives():
    gen = _gen()
    fresh = [{"label": "Tommee Tippee", "url": "https://tommeetippee.com/pricing", "text": "From $24.99"}]
    assert gen._has_confirmed_price(gen._blocks_naming("Tommee Tippee", fresh), "tommeetippee.com") is True


def test_a_reputable_third_party_price_counts_but_an_affiliate_one_does_not():
    gen = _gen()
    rep = [{"label": "third-party · Forbes", "url": "https://forbes.com/x", "text": "Nanobebe from $16.99"}]
    aff = [{"label": "retail · Amazon", "url": "https://amazon.com/dp/B0", "text": "Nanobebe $16.99"}]
    assert gen._has_confirmed_price(gen._blocks_naming("Nanobebe", rep), "nanobebe.com") is True
    assert gen._has_confirmed_price(gen._blocks_naming("Nanobebe", aff), "nanobebe.com") is False


def test_the_checks_read_markers_the_way_the_reader_does():
    """Both checks run AFTER `_rebuild_sources` renumbers every `[S#]` by first appearance, so
    indexing into the un-renumbered `_evidence_blocks` names the WRONG source. Found by an end-to-end
    smoke: a sentence citing Tommee Tippee was reported as citing amazon.com. The rebuilt `## Sources`
    list is the authoritative map, and these checks must use it."""
    gen = _gen()
    body = ("Nanobebe starts From $16.99 [S2].\n\n"
            "## Sources\n\n"
            "- [S1] retail · Amazon listing — <https://amazon.com/dp/B0>\n"
            "- [S2] Tommee Tippee — <https://tommeetippee.com/>\n")
    stale = [{"label": "Thyseed", "url": "https://thyseed.com/", "text": ""},
             {"label": "retail · Amazon", "url": "https://amazon.com/dp/B0", "text": ""}]
    note = gen._citation_attribution_check(body, stale, BRAND, TOOLS)
    assert "tommeetippee.com" in note, "must name the source the RENDERED list maps S2 to"
    assert "amazon.com" not in note, "the stale block order must not leak into the warning"


def test_a_body_with_no_sources_section_still_uses_the_raw_blocks():
    gen = _gen()
    note = gen._citation_attribution_check("Nanobebe starts From $16.99 [S1].", _blocks(), BRAND, TOOLS)
    assert note.startswith("citation-check:")
