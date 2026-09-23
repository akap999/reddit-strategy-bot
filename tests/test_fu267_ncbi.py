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
