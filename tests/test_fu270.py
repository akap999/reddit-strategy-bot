"""FU270 — rank a study by what it IS, not by the domain that served it.

Everything about source quality was a domain allowlist plus a title guess. `_weak_study_design`
reads a source's TITLE and spots only the weakest designs, and its own docstring says why:
"Nothing in the repo ranked study design; this is the minimum that makes the weakest visible."

NCBI publishes the real thing. Measured live, one batched call:

    41885859  2026  Journal Article, Randomized Controlled Trial   -> level 2
    26655941  2016  Journal Article, Meta-Analysis, Systematic Review -> level 1
    29537947  2018  Journal Article                                -> level 8

A 2016 systematic review and a 2009 single study are not interchangeable, and until now the writer
could not tell them apart — both arrived as "official · <title>".
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generators.research as R  # noqa: E402
from generators.blog_gen import BlogGenerator as B  # noqa: E402


# ── the hierarchy ────────────────────────────────────────────────────────────────────────────────

def test_the_standard_evidence_hierarchy_is_respected():
    order = [R._study_level(["Journal Article", "Meta-Analysis", "Systematic Review"])[0],
             R._study_level(["Journal Article", "Randomized Controlled Trial"])[0],
             R._study_level(["Clinical Trial"])[0],
             R._study_level(["Observational Study"])[0],
             R._study_level(["Journal Article"])[0]]
    assert order == sorted(order), order
    assert order[0] < order[-1]


def test_an_editorial_ranks_below_an_ordinary_paper():
    """The one distinction the old title-based rule could make, now made from the source itself."""
    assert R._study_level(["Editorial"])[0] > R._study_level(["Journal Article"])[0]
    assert R._study_level(["Comment"])[0] > R._study_level(["Journal Article"])[0]
    assert R._study_level(["Letter"])[1] == "editorial or letter"


def test_a_systematic_review_is_not_read_as_a_plain_review():
    """"Meta-Analysis, Systematic Review" contains the word "review"; order matters in the table."""
    assert R._study_level(["Systematic Review"])[1] == "systematic review"
    assert R._study_level(["Review"])[1] == "review"
    assert R._study_level(["Systematic Review"])[0] < R._study_level(["Review"])[0]


def test_an_unknown_type_gets_the_neutral_default():
    assert R._study_level([])[1] == "journal article"
    assert R._study_level(None)[1] == "journal article"


# ── the fetch is bounded and never fatal ─────────────────────────────────────────────────────────

def test_a_failed_lookup_does_not_stop_a_generation(monkeypatch):
    def _boom(*a, **k):
        raise OSError("ncbi down")
    monkeypatch.setattr(R._urlreq, "urlopen", _boom)
    assert R.ncbi_study_meta(["41885859"]) == {}


def test_nothing_is_requested_when_there_are_no_papers(monkeypatch):
    called = []
    monkeypatch.setattr(R._urlreq, "urlopen", lambda *a, **k: called.append(1))
    assert R.ncbi_study_meta([]) == {} and R.ncbi_study_meta(["not-a-pmid"]) == {}
    assert not called, "a call with no ids is a wasted request"


def test_the_lookup_is_one_batched_request(monkeypatch):
    """One HTTP call per ARTICLE, not per source."""
    seen = []

    class _R:
        def read(self):
            return b'{"result": {"uids": []}}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(R._urlreq, "urlopen",
                        lambda req, timeout=None: seen.append(req.full_url) or _R())
    R.ncbi_study_meta(["1", "2", "3", "4"])
    assert len(seen) == 1 and "id=1,2,3,4" in seen[0]


# ── what the writer sees ─────────────────────────────────────────────────────────────────────────

def _tagged(monkeypatch, blocks):
    monkeypatch.setattr(R, "ncbi_study_meta", lambda ids: {
        "41885859": {"level": 2, "design": "randomised trial", "year": "2026", "journal": "JAMA"},
        "26655941": {"level": 1, "design": "systematic review", "year": "2016", "journal": "JPGN"}})
    g = B.__new__(B)
    g._page_terms = []
    g._tag_study_levels(blocks)
    return g


TRIAL = {"label": "official · Feeding Bottles trial",
         "url": "https://pubmed.ncbi.nlm.nih.gov/41885859/", "text": "PMID: 41885859. " * 40}
REVIEW = {"label": "official · Infant Colic - What works",
          "url": "https://pubmed.ncbi.nlm.nih.gov/26655941/", "text": "PMID: 26655941. " * 40}
BRAND = {"label": "Thyseed", "url": "https://thyseed.example/p", "text": "Brand page. " * 40}


def test_the_design_and_year_reach_the_writer(monkeypatch):
    blocks = [TRIAL, REVIEW, BRAND]
    g = _tagged(monkeypatch, blocks)
    rendered = g._render_evidence_blocks(blocks)
    assert "[randomised trial, 2026]" in rendered[0]
    assert "[systematic review, 2016]" in rendered[1]
    assert "[" not in rendered[2].split("\n")[0].split("—")[-1], "a brand page gets no design"


def test_the_label_is_not_rewritten(monkeypatch):
    """The label is a dedup key — the title match is what connects two routes to one paper."""
    blocks = [dict(TRIAL)]
    _tagged(monkeypatch, blocks)
    assert blocks[0]["label"] == "official · Feeding Bottles trial"
    assert blocks[0]["design"] == "randomised trial"


def test_a_pmid_stated_only_in_the_page_is_enough(monkeypatch):
    """A PMC record carries the PMID in its text, not its url."""
    blocks = [{"label": "official · PMC copy", "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC1/",
               "text": "PMID: 26655941. Findings here. " * 40}]
    _tagged(monkeypatch, blocks)
    assert blocks[0]["design"] == "systematic review"
