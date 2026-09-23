"""FU273 — the source TIER: what a source is WORTH, once the page has actually been read.

Everything the writer knew about a source's standing was its label — a domain allowlist plus a
title shape, decided before the page was opened. Three signals arrived since and nothing ranked by
them: the page is now READ (FU267), an NCBI paper carries its design and year (FU270), and a page
is asked what it says about itself (FU271). They sat on the block as three separate sentences that
could not be compared with each other.

TWO AXES. Scope — what a class of source may be used to evidence — is absolute and enforced
elsewhere: the brand's own page is the best source in the world for its own price and a worthless
one for a clinical claim. Tier is the weight WITHIN a scope. Collapsing them into one number is why
a single "source score" cannot work, so this returns only the second.

The worked example, from the reported anti-colic article:

    TIER 1   pubmed 26655941   systematic review, 2016
    TIER 1   fda.gov           regulator
    TIER 1   aafp.org          guideline body      <- labelled `third-party ·` on the shipped page
    TIER 2   pmc 3328286       journal article
    TIER 3   thyseed.com       first-party — its own specs only
    TIER 4   somereview.com    cites its own sources
    TIER 5   birchstore.com    no citations
    POINTER  publications.aap.org (blocked)
    DROPPED  BestReviews · Mom Loves Best · Today's Parent
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators import blog_gen as BG  # noqa: E402
from generators.blog_gen import BlogGenerator as B  # noqa: E402

PAGE = "Real page text about the topic. " * 40


def _bl(**kw):
    b = {"label": "third-party · A Page", "url": "https://example.com/x", "text": PAGE,
         "how": "direct", "intent": "", "intent_why": ""}
    b.update(kw)
    return b


def _pointer(**kw):
    return _bl(text=B._SUMMARY_PREFIX + "a gloss of the page", how="thin", **kw)


# ── the table, one fixture per row ───────────────────────────────────────────────────────────────

def test_a_systematic_review_is_tier_1():
    assert BG._source_tier(_bl(level=1, design="systematic review", year=2016))[0] == 1


def test_a_randomised_trial_is_tier_1():
    assert BG._source_tier(_bl(level=2, design="randomised trial", year=2026))[0] == 1


def test_a_regulator_is_tier_1_by_its_domain():
    tier, what = BG._source_tier(_bl(url="https://www.fda.gov/media/12345/download"))
    assert tier == 1 and what == "regulator"


def test_a_guideline_body_is_tier_1_even_when_the_label_got_it_wrong():
    """The known weakness this closes. aafp.org — the American Academy of Family Physicians, and
    already in `_AUTHORITY_ORGS` — was labelled `third-party ·` on the shipped article. Reading the
    DOMAIN instead of the label means a guideline body cannot be filed below a blog."""
    b = _bl(url="https://www.aafp.org/pubs/afp/2015/p577.html", label="third-party · AAFP")
    tier, what = BG._source_tier(b)
    assert tier == 1 and what == "guideline body"


def test_a_practice_guideline_is_tier_1_wherever_it_is_published():
    assert BG._source_tier(_bl(level=5, design="practice guideline", year=2021))[0] == 1


def test_an_ordinary_journal_article_is_tier_2():
    tier, what = BG._source_tier(_bl(level=6, design="journal article", year=2009,
                                     url="https://pmc.example.org/PMC1"))
    assert tier == 2 and "journal article" in what


def test_an_editorial_is_tier_2_not_tier_1():
    """A commentary in a journal is still a commentary. Getting this wrong is easy — the first
    version of the study hierarchy ranked an editorial ABOVE a research article."""
    assert BG._source_tier(_bl(level=8, design="editorial or letter", year=2020))[0] == 2


def test_a_vendors_own_page_is_tier_3():
    tier, what = BG._source_tier(_bl(url="https://thyseed.com/products/bottle", label=""),
                                 first_party={"thyseed.com"})
    assert tier == 3 and "never comparative or clinical" in what


def test_a_competitors_own_page_is_first_party_too():
    """Tier 3 is about what a page IS, not whose side it is on: a rival's product page is the best
    source for the rival's specs and no source at all for a comparison between the two."""
    assert BG._source_tier(_bl(url="https://drbrownsbaby.com/p/1"),
                           first_party={"thyseed.com", "drbrownsbaby.com"})[0] == 3


def test_a_third_party_that_cites_its_sources_is_tier_4():
    tier, what = BG._source_tier(_bl(intent="reference"))
    assert tier == 4 and what == "cites its own sources"


def test_a_third_party_with_no_citations_is_tier_5():
    assert BG._source_tier(_bl())[0] == 5


def test_a_page_we_could_not_read_is_a_pointer_not_a_tier():
    """A page unreachable by every method is not a source. It cannot be ranked against pages that
    were read, because there is nothing to rank — so it gets no number at all."""
    tier, what = BG._source_tier(_pointer(url="https://publications.aap.org/x"))
    assert tier == "pointer" and "NOT READ" in what


def test_an_unread_regulator_is_still_only_a_pointer():
    """The domain earns tier 1 only for a page we actually opened. An FDA URL we never read is a
    pointer like any other — otherwise the strongest tier would be handed out for a hostname."""
    assert BG._source_tier(_pointer(url="https://www.fda.gov/media/1/download"))[0] == "pointer"


def test_an_unread_paper_is_a_pointer_even_with_its_design_known():
    """`ncbi_study_meta` can name the design from the summary API while the full text stays
    unreadable. Design without text is a citation we cannot check."""
    assert BG._source_tier(_pointer(level=1, design="systematic review", year=2016))[0] == "pointer"


def test_a_monetised_listing_is_dropped_before_it_is_tiered():
    tier, what = BG._source_tier(_bl(intent="commerce"))
    assert tier == "dropped" and "nothing else" in what


def test_a_commerce_page_on_a_gov_domain_is_still_dropped():
    """First match wins, and the commission comes first. A monetised page does not launder itself
    through a strong domain."""
    assert BG._source_tier(_bl(intent="commerce", url="https://shop.example.gov/p"))[0] == "dropped"


# ── what the writer is shown ─────────────────────────────────────────────────────────────────────

def _render(block, first_party=()):
    g = B.__new__(B)
    g._page_terms = ["topic"]
    g._first_party_domains = set(first_party)
    return g._render_evidence_blocks([block])[0].split("\n")[0]


def test_the_tier_reaches_the_writer_on_the_evidence_line():
    line = _render(_bl(level=1, design="systematic review", year=2016))
    assert "tier 1" in line and "systematic review, 2016" in line


def test_a_pointer_says_so_where_the_tier_would_be():
    assert "NOT READ" in _render(_pointer())


def test_one_bracket_not_three():
    """FU270's design, FU271's intent and the tier were three separate sentences on one line; a
    reader could not compare them with each other. They are one bracket now."""
    line = _render(_bl(level=1, design="systematic review", year=2016, intent="reference"))
    assert line.split("] ", 1)[1].count("[") == 1, line   # past the [S#] marker's own bracket


def test_a_source_above_tier_4_keeps_the_fact_that_it_cites_its_sources():
    """Tier 4 IS "cites its own sources", so folding the signal into the tier would silently throw
    it away for everything stronger — and that is exactly where it is most useful, because a source
    that shows its own working can be followed to the primary paper."""
    line = _render(_bl(url="https://www.fda.gov/media/1/download", intent="reference"))
    assert "tier 1" in line and "cites its own sources" in line


def test_the_subjects_own_domain_is_registered_as_first_party():
    g = B.__new__(B)
    g._page_terms = []
    g._first_party_domains = {"thyseed.com"}
    line = g._render_evidence_blocks([_bl(url="https://www.thyseed.com/p/1", label="")])[0]
    assert "tier 3" in line, line


def test_nothing_is_reordered_yet():
    """Step 1 computes and SHOWS the tier; step 2 reorders on it. Reordering changes which source
    gets [S1], which touches marker renumbering and every check that maps a marker to a block — so
    it is a separate change with the replay's damage count as its test."""
    g = B.__new__(B)
    g._page_terms = []
    g._first_party_domains = set()
    weak, strong = _bl(url="https://weak.example/a"), _bl(level=1, design="systematic review")
    out = g._render_evidence_blocks([weak, strong])
    assert out[0].startswith("[S1] third-party · A Page — https://weak.example/a")
    assert "tier 1" in out[1]
