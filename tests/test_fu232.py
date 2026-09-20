"""FU232 — two gaps a hand-corrected article exposed.

1. A comparison TABLE row citing a DIFFERENT brand's source. `_prose_sentences` skips table rows, and
   a cell names no brand of its own (the brand is the ROW, in the first column), so FU204's
   `_citation_attribution_check` structurally could not see it. On the article that exposed this, the
   Straight North row's Reddit cell cited a bluecorona.com page.

2. The PUBLISHER's own site starved by the per-domain web-fetch cap. `_WEB_FETCH_PER_DOMAIN = 3`
   exists so a walled competitor can't run up cost on guessed paths; applied to the subject's own
   walled domain it silently drops 5 of the 8 `_EVIDENCE_PATHS` — including /case-studies, the page
   that carried the construction proof the corrected article cited.

Deterministic: no model call, no network.
"""
import pathlib

import pytest

from generators.blog_gen import (BlogGenerator, _WEB_FETCH_OWN_DOMAIN, _WEB_FETCH_PER_DOMAIN)
from tests.stubs import StubClaude


def _gen():
    return BlogGenerator(StubClaude(), None)


BRAND = {"name": "Jolly Search", "domain_url": "https://jollysearch.com",
         "competitor_domains": {"Straight North": "straightnorth.com",
                                "Blue Corona": "bluecorona.com",
                                "Loganix": "loganix.com"}}
TOOLS = ["Straight North", "Blue Corona", "Loganix"]


def _blocks():
    return [
        {"label": "Jolly Search", "url": "https://jollysearch.com/", "text": "GEO"},
        {"label": "Jolly Search", "url": "https://jollysearch.com/case-studies", "text": "proof"},
        {"label": "third-party · Clutch — Top SEO Agencies 2026", "url": "https://clutch.co/seo", "text": "list"},
        {"label": "Straight North", "url": "https://straightnorth.com/services/", "text": "Straight North"},
        {"label": "Blue Corona", "url": "https://bluecorona.com/services/seo/", "text": "Blue Corona SEO"},
    ]


def _table(cell):
    return ("| Agency | Reddit & community |\n"
            "|---|---|\n"
            "| **Jolly Search** | Reddit mention density [S1] |\n"
            f"| **Straight North** | {cell} |\n")


# ── 1. the reported defect ───────────────────────────────────────────────────────────────────────
def test_a_row_citing_another_brands_source_is_flagged():
    """The exact shape from the corrected article: Straight North's row, Blue Corona's page."""
    note = _gen()._citation_attribution_check(_table("User-generated content programs [S5]"),
                                              _blocks(), BRAND, TOOLS)
    assert note.startswith("citation-check:")
    assert "Straight North row" in note and "bluecorona.com" in note
    assert "Blue Corona" in note


def test_the_column_header_is_named_so_the_operator_can_find_the_cell():
    note = _gen()._citation_attribution_check(_table("UGC work [S5]"), _blocks(), BRAND, TOOLS)
    assert '"Reddit & community" cell' in note


# ── 2. precision: what must stay silent ──────────────────────────────────────────────────────────
@pytest.mark.parametrize("cell,why", [
    ("Runs its own community program [S4]", "cites Straight North's OWN page"),
    ("Listed among the top agencies [S3]", "a third-party page that belongs to nobody in particular"),
    ("No dedicated program stated", "no citation at all"),
])
def test_a_correctly_cited_or_neutral_cell_is_silent(cell, why):
    assert _gen()._citation_attribution_check(_table(cell), _blocks(), BRAND, TOOLS) == "", why


def test_a_row_naming_two_brands_is_ambiguous_and_skipped():
    body = ("| Agency | Reddit |\n|---|---|\n"
            "| **Straight North** vs **Blue Corona** | shared program [S5] |\n")
    assert _gen()._citation_attribution_check(body, _blocks(), BRAND, TOOLS) == ""


def test_the_publishers_own_row_is_checked_the_same_way():
    """The rule is symmetric — the publisher's row citing a competitor's page is the same defect."""
    body = ("| Agency | Reddit |\n|---|---|\n"
            "| **Jolly Search** | Community mentions [S5] |\n")
    note = _gen()._citation_attribution_check(body, _blocks(), BRAND, TOOLS)
    assert "Jolly Search row" in note and "bluecorona.com" in note


def test_a_body_with_no_table_behaves_exactly_as_before():
    prose = "Straight North runs a community program [S5]."
    assert _gen()._citation_attribution_check(prose, _blocks(), BRAND, TOOLS).startswith("citation-check:")
    clean = "Straight North runs a community program [S4]."
    assert _gen()._citation_attribution_check(clean, _blocks(), BRAND, TOOLS) == ""


# ── 3. the publisher's own domain gets a bigger web-fetch budget ─────────────────────────────────
class _Walled:
    """Every direct fetch is blocked; Anthropic's web fetch always reads the page."""
    def __init__(self):
        self.read = []

    def web_fetch_text(self, url):
        self.read.append(url)
        return ("The page text. " * 40, 200)


def _walled_gen(monkeypatch, own=""):
    import generators.blog_gen as bg
    monkeypatch.setattr(bg, "_fetch_page", lambda url: ("", "blocked"))
    g = BlogGenerator(_Walled(), None)
    g._own_domain = own
    return g


def test_the_subject_own_domain_is_not_capped_at_the_competitor_budget(monkeypatch):
    g = _walled_gen(monkeypatch, own="jollysearch.com")
    paths = ("", "/pricing", "/features", "/about", "/testimonials", "/customers",
             "/case-studies", "/reviews")
    got = [p for p in paths if g._fetch_url(f"https://jollysearch.com{p}")]
    assert len(got) == len(paths), "every own-site evidence path must be readable"
    assert "/case-studies" in "".join(got)
    assert len(paths) <= _WEB_FETCH_OWN_DOMAIN


def test_a_competitor_domain_keeps_the_smaller_budget(monkeypatch):
    g = _walled_gen(monkeypatch, own="jollysearch.com")
    paths = ("", "/pricing", "/features", "/about", "/testimonials")
    got = [p for p in paths if g._fetch_url(f"https://bluecorona.com{p}")]
    assert len(got) == _WEB_FETCH_PER_DOMAIN < len(paths)


def test_with_no_subject_domain_known_nothing_changes(monkeypatch):
    """A generation that never resolved an own domain behaves exactly as it did before FU232."""
    g = _walled_gen(monkeypatch, own="")
    got = [p for p in ("", "/a", "/b", "/c", "/d") if g._fetch_url(f"https://jollysearch.com{p}")]
    assert len(got) == _WEB_FETCH_PER_DOMAIN


def test_the_own_domain_budget_covers_subdomains(monkeypatch):
    g = _walled_gen(monkeypatch, own="jollysearch.com")
    assert g._fetch_url("https://www.jollysearch.com/case-studies")
    assert g._fetch_url("https://blog.jollysearch.com/x")


# ── 4. the REAL shipped article, as a permanent anchor ───────────────────────────────────────────
REAL_BRAND = {"name": "Jolly Search", "domain_url": "https://jollysearch.com",
              "competitor_domains": {"Straight North": "straightnorth.com",
                                     "Blue Corona": "bluecorona.com", "Fatjoe": "fatjoe.com",
                                     "Loganix": "loganix.com", "Siege Media": "siegemedia.com",
                                     "IPullRank": "ipullrank.com"}}
REAL_TOOLS = ["Straight North", "Blue Corona", "Fatjoe", "Loganix", "Siege Media", "IPullRank"]


def _real_body():
    return (pathlib.Path(__file__).parent / "fixtures" / "fu232_agency_table.md").read_text()


def test_the_real_articles_miscited_cell_is_caught():
    """The shipped construction-agency article: the Straight North row's community cell cites
    [S12], a bluecorona.com blog post. A hand correction replaced it with Straight North's own
    [S4]; nothing in the generator could see it, because the prose check skips table rows."""
    note = _gen()._citation_attribution_check(_real_body(), [], REAL_BRAND, REAL_TOOLS)
    assert "Straight North row" in note
    assert "bluecorona.com" in note
    assert '"Reddit / Community Signals" cell' in note


def test_the_real_articles_other_23_cells_are_silent():
    """Precision on real output: one hit, not a wall of them. Every other cell in that 7-row,
    5-column table cites its own row's brand."""
    note = _gen()._citation_attribution_check(_real_body(), [], REAL_BRAND, REAL_TOOLS)
    assert note.count("cell in the") == 1, note


# ── 5. the Sources parser must read OUR OWN rendered list ────────────────────────────────────────
def test_blocks_from_sources_reads_the_hyphen_separator_we_now_render():
    """FU228b stopped `_rebuild_sources` writing an em-dash in the source list, but the parser still
    required one — so every check that maps `[S#]` through the RENDERED list silently fell back to
    the raw, un-renumbered evidence blocks. That is the exact mis-mapping FU204 built it to prevent:
    the body says [S2] and the checker reads evidence block 2, which after renumbering is somebody
    else's source."""
    body = ("Text [S1][S2].\n\n## Sources\n\n"
            "- [S1] Jolly Search - <https://jollysearch.com/>\n"
            "- [S2] third-party · Clutch - Top SEO Agencies - <https://clutch.co/seo>\n")
    got = BlogGenerator._blocks_from_sources(body, [{"label": "WRONG", "url": "https://wrong.test"}])
    assert [b["url"] for b in got] == ["https://jollysearch.com/", "https://clutch.co/seo"]
    assert got[1]["label"].endswith("Top SEO Agencies"), "a hyphen inside the label must not split it"


def test_the_em_dash_separator_still_parses():
    body = "T [S1].\n\n## Sources\n\n- [S1] Jolly Search — <https://jollysearch.com/>\n"
    assert BlogGenerator._blocks_from_sources(body, [])[0]["url"] == "https://jollysearch.com/"


def test_a_body_with_no_sources_section_still_falls_back_to_the_raw_blocks():
    raw = [{"label": "Jolly Search", "url": "https://jollysearch.com/", "text": ""}]
    assert BlogGenerator._blocks_from_sources("Some text [S1].", raw) == raw
