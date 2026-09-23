"""FU267 (step 4b) — NCBI is read through NCBI's own API, not by scraping.

Measured from Railway on 30 authority URLs taken from stored articles: 20% readable with our own
fetch, 35% once Anthropic's web fetch is added. The biggest failing family was PubMed/PMC, and it
fails on EVERY rung — direct, residential (the proxy refuses to tunnel to those hosts at all) and
web fetch — because all three receive the same 376-character interstitial:

    title: Checking your browser - reCAPTCHA
    Checking your browser before accessing pubmed.ncbi.nlm.nih.gov ...

Through E-utilities, on the same host: the 1,055-infant JAMA trial an article had been mis-citing
returns 5,289 characters of abstract, and the BMC Research Notes study that article had confused it
WITH returns 37,968.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators import research as R  # noqa: E402

_ABSTRACT = ("1. JAMA Netw Open. 2026 Mar 2;9(3):e263749. doi: 10.1001/jamanetworkopen.2026.3749.\n\n"
             "Feeding Bottles With Different Venting Methods and Gastrointestinal Discomfort in "
             "Infants: A Randomized Clinical Trial.\n\nOBJECTIVE: To assess whether venting method "
             "affects gastrointestinal discomfort. " * 4)
_PMC_XML = ("<article><front><journal-meta><journal-title>BMC Res Notes</journal-title>"
            "</journal-meta></front><body><sec><title>Findings</title><p>No between-group "
            "differences in colic incidence were observed between tube-vented and standard "
            "bottles.</p></sec></body><back><ref-list><ref>Smith J. Irrelevant reference.</ref>"
            "</ref-list></back></article>" * 6)


class _Resp:
    def __init__(self, body):
        self._b = body.encode()

    def read(self):
        return self._b

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _stub(monkeypatch, body, seen=None):
    def _open(req, timeout=None):
        if seen is not None:
            seen.append(req.full_url if hasattr(req, "full_url") else str(req))
        return _Resp(body)
    monkeypatch.setattr(R._urlreq, "urlopen", _open)


def test_a_pubmed_url_is_read_through_the_api(monkeypatch):
    seen = []
    _stub(monkeypatch, _ABSTRACT, seen)
    text, how = R._ncbi_api_text("https://pubmed.ncbi.nlm.nih.gov/41885859/")
    assert how == "ncbi api" and "Randomized Clinical Trial" in text
    assert "db=pubmed&id=41885859" in seen[0] and "rettype=abstract" in seen[0]


def test_both_pmc_url_shapes_are_recognised(monkeypatch):
    """Stored articles cite PMC under two hosts, and both were being scraped."""
    for url in ("https://pmc.ncbi.nlm.nih.gov/articles/PMC3328286/",
                "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC3328286/"):
        seen = []
        _stub(monkeypatch, _PMC_XML, seen)
        text, how = R._ncbi_api_text(url)
        assert how == "ncbi api", url
        assert "db=pmc&id=3328286" in seen[0], url
        assert "No between-group differences in colic incidence" in text


def test_the_reference_list_is_not_kept_as_evidence(monkeypatch):
    """A paper's bibliography is not what the article is citing it for, and on a long paper it is
    most of the characters the budget would buy."""
    _stub(monkeypatch, _PMC_XML)
    text, _how = R._ncbi_api_text("https://pmc.ncbi.nlm.nih.gov/articles/PMC3328286/")
    assert "Irrelevant reference" not in text


def test_xml_tags_and_entities_do_not_reach_the_writer(monkeypatch):
    _stub(monkeypatch, "<article><body><p>Mothers &amp; infants at 2 weeks.</p></body></article>" * 9)
    text, _how = R._ncbi_api_text("https://pmc.ncbi.nlm.nih.gov/articles/PMC1/")
    assert "<p>" not in text and "&amp;" not in text and "Mothers & infants" in text


def test_a_non_ncbi_url_falls_straight_through(monkeypatch):
    """This must never become a new way to fail — everything else keeps the ordinary ladder."""
    _stub(monkeypatch, _ABSTRACT)
    assert R._ncbi_api_text("https://www.aafp.org/pubs/afp/issues/2015/1001/p577.html") == ("", "")


def test_an_api_failure_falls_through_rather_than_erroring(monkeypatch):
    def _boom(req, timeout=None):
        raise OSError("network down")
    monkeypatch.setattr(R._urlreq, "urlopen", _boom)
    assert R._ncbi_api_text("https://pubmed.ncbi.nlm.nih.gov/41885859/") == ("", "")


def test_a_stub_response_is_not_accepted_as_a_page(monkeypatch):
    """The failure this replaces returned 376 characters of reCAPTCHA. A short API answer must not
    quietly take its place."""
    _stub(monkeypatch, "Error: cannot get document summary")
    assert R._ncbi_api_text("https://pubmed.ncbi.nlm.nih.gov/41885859/") == ("", "")


def test_read_page_prefers_the_api_and_caches_it(monkeypatch):
    _stub(monkeypatch, _ABSTRACT)
    called = []
    monkeypatch.setattr(R, "_fetch_page", lambda *a, **k: called.append(1) or ("", "blocked"))
    cache = {}
    text, how = R.read_page("https://pubmed.ncbi.nlm.nih.gov/41885859/", cache=cache)
    assert how == "ncbi api" and not called, "the scraper must not be tried first"
    assert cache["https://pubmed.ncbi.nlm.nih.gov/41885859/"][1] == "ncbi api"


# ── the same paper, cited twice under two labels ─────────────────────────────────────────────────
# Reported on a shipped article: "[S4] is the JAMA 1,055-infant trial (the PMC copy), which is also
# [S2] via a different URL. So the post cites the same trial twice under two labels and attributes
# another trial's findings to it."
#
# Deduplication existed and could not see it. The two routes carry ids from DIFFERENT schemes — a
# PubMed url gives a PMID, the PMC copy a PMCID — and the titles were truncated at different
# lengths, so the title key missed too. Both records state the same DOI; reading it was impossible
# until those pages stopped returning 376 characters of reCAPTCHA.

from generators.blog_gen import _doc_keys, _doc_ids_in_text  # noqa: E402

_PUBMED_BLOCK = {
    "label": "reference · Feeding Bottles With Different Venting Methods and Gastrointestinal Di",
    "url": "https://pubmed.ncbi.nlm.nih.gov/41885859/",
    "text": "1. JAMA Netw Open. 2026 Mar 2;9(3):e263749. doi: 10.1001/jamanetworkopen.2026.3749.\n\n"
            "Feeding Bottles With Different Venting Methods and Gastrointestinal Discomfort."}
_PMC_BLOCK = {
    "label": "official · Feeding Bottles With Different Venting Methods and Gastrointestinal "
             "Discomfort in Infants: A Randomized Clinical Trial (PMC)",
    "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC9999999/",
    "text": "JAMA Netw Open. doi: 10.1001/jamanetworkopen.2026.3749. PMID: 41885859. Feeding "
            "Bottles With Different Venting Methods."}


def test_the_two_routes_to_one_paper_now_share_an_identity():
    shared = set(_doc_keys(_PUBMED_BLOCK)) & set(_doc_keys(_PMC_BLOCK))
    assert "doi:10.1001/jamanetworkopen.2026.3749" in shared
    assert "pmid:41885859" in shared


def test_the_truncated_title_alone_would_not_have_matched():
    """Why the existing title key missed: one label is cut at 70 characters and the other is not."""
    t1 = [k for k in _doc_keys(_PUBMED_BLOCK) if k.startswith("title:")]
    t2 = [k for k in _doc_keys(_PMC_BLOCK) if k.startswith("title:")]
    assert t1 and t2 and t1 != t2


def test_ids_are_read_from_the_head_not_the_bibliography():
    """A paper's reference list names OTHER people's identifiers. A page carrying no id of its own
    must come away with none — picking one out of the bibliography would merge two unrelated
    sources into one. (Testing this with an id in the head as well proves nothing: the search
    returns the first match whether or not the scan is bounded.)"""
    no_id_of_its_own = "Findings here. " + ("Filler sentence. " * 500) + " doi: 10.9999/someone-else"
    assert _doc_ids_in_text(no_id_of_its_own) == []
    own = "doi: 10.1000/mine. Findings. " + ("Filler. " * 500) + " doi: 10.9999/someone-else"
    assert _doc_ids_in_text(own) == ["doi:10.1000/mine"]


def test_a_block_with_no_identifier_is_unchanged():
    assert _doc_ids_in_text("A brand page about bottles.") == []
    assert _doc_ids_in_text("") == []


def test_a_trailing_full_stop_is_not_part_of_the_doi():
    assert _doc_ids_in_text("doi: 10.1001/jamanetworkopen.2026.3749.") == \
        ["doi:10.1001/jamanetworkopen.2026.3749"]
