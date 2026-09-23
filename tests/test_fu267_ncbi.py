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


# ── FU272: one request per database, not one per source ──────────────────────────────────────────
# Measured from Railway: seven of ten remaining authority failures were NCBI papers that had been
# readable minutes earlier. NCBI allows 3 requests/second without a key and sources are fetched on a
# pool of six workers, so we rate-limited ourselves; the exception was caught and the source
# silently degraded to the scraper, which gets a reCAPTCHA.
#
#   ONE efetch call, 6 pmids  ->  18,473 chars in 0.3s
#   ONE efetch call, 4 pmcids -> 241,428 chars in 0.8s

LONG_ABSTRACT = "Background and methods described at length. " * 120      # > 4,000 chars

_PUBMED_BLOB = (
    "1. JAMA Netw Open. 2026;9(3):e263749.\n\nFeeding Bottles trial.\n\n" + LONG_ABSTRACT
    + "\n\nPMID: 41885859 [Indexed for MEDLINE]\n\n"
    # NOTE the parentheses: adjacent string literals concatenate BEFORE `* 20`, so without them
    # the whole second record repeats twenty times and the batch looks like 21 papers.
    + "2. J Pediatr Gastroenterol Nutr. 2016;62(5):668.\n\nInfant Colic - What works.\n\n"
    + ("A systematic review of interventions. " * 20) + "\n\nPMID: 26655941 [Indexed for MEDLINE]\n")

_PMC_BLOB = (
    '<pmc-articleset><article xmlns="x"><front><article-id pub-id-type="pmc">3328286</article-id>'
    '</front><body><p>' + "Infant feeding bottle design findings. " * 40 + '</p></body>'
    '<back><ref-list><ref>PMC9999999 someone else</ref></ref-list></back></article>'
    '<article xmlns="x"><front><article-id pub-id-type="pmc">5857083</article-id></front>'
    '<body><p>' + "Sucking behaviour with and without an anticolic system. " * 40 + '</p></body>'
    '</article></pmc-articleset>')


def _stub_efetch(monkeypatch, blobs, seen=None):
    """Returns only the records whose ids the URL actually asked for. A stub that hands back
    everything regardless cannot tell a batched request from N single ones — which is the whole
    property under test."""
    def _open(req, timeout=None):
        u = req.full_url if hasattr(req, "full_url") else str(req)
        if seen is not None:
            seen.append(u)
        asked = set(re.search(r"[?&]id=([^&]*)", u).group(1).split(","))
        if "db=pubmed" in u:
            recs = [r for r in R._PUBMED_SPLIT_RE.split(_PUBMED_BLOB)
                    if (R._PUBMED_ID_RE.search(r) or [None]) and
                    (R._PUBMED_ID_RE.search(r).group(1) if R._PUBMED_ID_RE.search(r) else "") in asked]
            return _Resp("\n\n".join(recs))
        recs = [p for p in R._PMC_SPLIT_RE.split(_PMC_BLOB)
                if p.startswith("<article ")
                and (R._PMC_ID_RE.search(p[:4000]).group(1) if R._PMC_ID_RE.search(p[:4000]) else "") in asked]
        return _Resp("<pmc-articleset>" + "".join(recs) + "</pmc-articleset>")
    monkeypatch.setattr(R._urlreq, "urlopen", _open)


import re  # noqa: E402


def test_every_paper_arrives_in_one_request_per_database(monkeypatch):
    seen = []
    _stub_efetch(monkeypatch, None, seen)
    cache = {}
    urls = ["https://pubmed.ncbi.nlm.nih.gov/41885859/",
            "https://pubmed.ncbi.nlm.nih.gov/26655941/",
            "https://pmc.ncbi.nlm.nih.gov/articles/PMC3328286/",
            "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC5857083/"]
    assert R.ncbi_prefetch(urls, cache) == 4
    assert len(seen) == 2, f"one per database, got {len(seen)}"
    assert all(cache[u][1] == "ncbi api" for u in urls)


def test_a_long_abstract_is_not_lost_to_the_id_bound(monkeypatch):
    """PubMed prints "PMID:" at the END of a record. Bounding the id search to the head silently
    dropped the LONGEST abstract in a batch — the one most worth having. That is how the 1,055-
    infant JAMA trial went missing from a batch where everything else arrived."""
    _stub_efetch(monkeypatch, None)
    cache = {}
    R.ncbi_prefetch(["https://pubmed.ncbi.nlm.nih.gov/41885859/"], cache)
    assert len(_PUBMED_BLOB.split("PMID: 41885859")[0]) > 4000, "the fixture must bury the id"
    assert "https://pubmed.ncbi.nlm.nih.gov/41885859/" in cache


def test_a_pmc_reference_list_does_not_claim_the_record(monkeypatch):
    """The opposite bound: PMC states its own id in <front> at the top, and a reference list names
    OTHER papers' ids. A record with no id of its own must come away with none — searching the
    whole document would hand it the first id in its bibliography and cache someone else's paper
    under this URL."""
    orphan = ('<pmc-articleset><article xmlns="x"><front><journal-title>J</journal-title></front>'
              # the bibliography must fall BEYOND the head bound, or the fixture proves nothing
              '<body><p>' + "Findings without a stated article id. " * 200 + '</p></body>'
              '<back><ref-list><ref>PMC3328286 a different paper entirely</ref></ref-list>'
              '</article></pmc-articleset>')
    monkeypatch.setattr(R._urlreq, "urlopen", lambda req, timeout=None: _Resp(orphan))
    cache = {}
    assert R.ncbi_prefetch(["https://pmc.ncbi.nlm.nih.gov/articles/PMC3328286/"], cache) == 0
    assert cache == {}, "a bibliography id must never key a record"


def test_the_prefetch_runs_before_the_read_pool():
    """Six workers each making their own call is what tripped NCBI's 3/second limit."""
    import inspect
    from generators.blog_gen import BlogGenerator as B
    src = inspect.getsource(B._read_sources_parallel)
    assert "ncbi_prefetch" in src
    assert src.index("ncbi_prefetch") < src.index("ThreadPoolExecutor"), \
        "it has to run BEFORE the pool, or the pool makes the calls it was meant to replace"


def test_the_prefetch_runs_for_the_cited_sources_too():
    import inspect
    from generators.blog_gen import BlogGenerator as B
    assert "ncbi_prefetch" in inspect.getsource(B._probe_cited_sources)


def test_a_non_ncbi_url_is_left_for_the_ordinary_ladder(monkeypatch):
    seen = []
    _stub_efetch(monkeypatch, None, seen)
    cache = {}
    assert R.ncbi_prefetch(["https://www.aafp.org/pubs/afp/2015/p577.html"], cache) == 0
    assert not seen and not cache


def test_a_url_already_in_the_cache_is_not_refetched(monkeypatch):
    seen = []
    _stub_efetch(monkeypatch, None, seen)
    cache = {"https://pubmed.ncbi.nlm.nih.gov/41885859/": ("already here", "direct")}
    assert R.ncbi_prefetch(list(cache), cache) == 0 and not seen


def test_a_prefetch_failure_leaves_the_ladder_exactly_as_it_was(monkeypatch):
    def _boom(*a, **k):
        raise OSError("ncbi down")
    monkeypatch.setattr(R._urlreq, "urlopen", _boom)
    cache = {}
    assert R.ncbi_prefetch(["https://pubmed.ncbi.nlm.nih.gov/41885859/"], cache) == 0
    assert cache == {}, "a failed prefetch must not poison the cache"


def test_the_pmc_split_does_not_break_on_article_title(monkeypatch):
    """`<article-title>` starts with "<article" and a \\b boundary matches before the hyphen — which
    split one paper into nine fragments, none of them carrying an id."""
    assert R._PMC_SPLIT_RE.split("<article-title>x</article-title>") == ["<article-title>x</article-title>"]
    assert len([p for p in R._PMC_SPLIT_RE.split(_PMC_BLOB) if p.startswith("<article ")]) == 2


def test_a_record_we_did_not_ask_for_is_not_cached(monkeypatch):
    """A batch is answered as one document, so the reply can carry records beyond the ids in the
    request — an id translation, a companion correction. Only the ids we asked for have a URL to be
    filed under; anything else must be dropped rather than keyed by guesswork."""
    both = ('<pmc-articleset>'
            '<article xmlns="x"><front><article-id pub-id-type="pmc">3328286</article-id></front>'
            '<body><p>' + "The paper we asked for. " * 40 + '</p></body></article>'
            '<article xmlns="x"><front><article-id pub-id-type="pmc">9999999</article-id></front>'
            '<body><p>' + "A paper nobody requested. " * 40 + '</p></body></article>'
            '</pmc-articleset>')
    monkeypatch.setattr(R._urlreq, "urlopen", lambda req, timeout=None: _Resp(both))
    asked = "https://pmc.ncbi.nlm.nih.gov/articles/PMC3328286/"
    cache = {}
    assert R.ncbi_prefetch([asked], cache) == 1
    assert list(cache) == [asked], "an unrequested record has no URL and must be dropped"
    assert "nobody requested" not in cache[asked][0]


def test_an_empty_record_does_not_pre_empt_the_ladder(monkeypatch):
    """PMC answers a paper it cannot serve in full with a stub carrying the right id and almost no
    text. Caching that would be worse than failing: `read_page` reads the cache first, so a
    60-character stub would stand in for a page the web fetch could still have read."""
    stub = ('<pmc-articleset><article xmlns="x">'
            '<front><article-id pub-id-type="pmc">3328286</article-id></front>'
            '<body><p>The publisher of this article does not allow downloading.</p></body>'
            '</article></pmc-articleset>')
    monkeypatch.setattr(R._urlreq, "urlopen", lambda req, timeout=None: _Resp(stub))
    cache = {}
    assert len(R._ncbi_clean(stub, "pmc")) < 200, "the fixture must actually be a stub"
    assert R.ncbi_prefetch(["https://pmc.ncbi.nlm.nih.gov/articles/PMC3328286/"], cache) == 0
    assert cache == {}, "an empty record must leave the URL to the rest of the ladder"
