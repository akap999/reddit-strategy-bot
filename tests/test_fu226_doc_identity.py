"""FU226 — one document, one source entry, newest edition.

Pinned against the REAL Sources list of the shipped PeterMD blog
(`is-tirzepatide-a-glp-1.html`), whose 17 entries hold only 12 distinct documents:
  - S7 / S13  — the same PMC paper under two NIH hosts (same PMC id)
  - S3 / S14  — the same PNAS paper as a publisher DOI and as a PMC mirror (different id SCHEMES,
                so only the shared title connects them)
  - S9 / S15  — the same Diabetes Therapy paper as a Springer DOI and a PubMed record (ditto)
  - S2 / S16  — the ZEPBOUND label's 2023 original and its 2026 supplement (the body cited the
                stale one)
  - S1 / S17  — the MOUNJARO label twice, with a LATER year carrying a LOWER revision number.
                Both files are real (fetched: revised May 2025 and January 2026), because the
                regulator runs separate revision series per amendment type — so the YEAR is what
                orders editions, and the 2026 filing wins.
"""
import pytest

from generators.blog_gen import (
    BlogGenerator, _doc_identity, _doc_is_newer, _doc_keys, _doc_title_key, _doc_version,
)
from tests.stubs import StubClaude


def _gen():
    return BlogGenerator(StubClaude(), None)


# the real blog's evidence, in its real order
BLOCKS = [
    ("official · FDA Official Prescribing Information – MOUNJARO (tirzepatide) [accessdata.fda.gov]",
     "https://www.accessdata.fda.gov/drugsatfda_docs/label/2025/215866s031lbl.pdf"),
    ("official · FDA Official Prescribing Information – ZEPBOUND (tirzepatide) [accessdata.fda.gov]",
     "https://www.accessdata.fda.gov/drugsatfda_docs/label/2023/217806s000lbl.pdf"),
    ("third-party · Structural determinants of dual incretin receptor agonism by tirzepatide – PNAS (National Academy of Sciences)",
     "https://www.pnas.org/doi/10.1073/pnas.2116506119"),
    ("third-party · Tirzepatide: A Novel, Once-weekly Dual GIP and GLP-1 Receptor Agonist for the Treatment of Type 2 Diabetes – PMC (NIH/NLM)",
     "https://pmc.ncbi.nlm.nih.gov/articles/PMC9354517/"),
    ("official · WEGOVY (semaglutide) injection — FDA Prescribing Information (2023 label, accessdata.fda.gov)",
     "https://www.accessdata.fda.gov/drugsatfda_docs/label/2023/215256s007lbl.pdf"),
    ("third-party · Tirzepatide – StatPearls – NCBI Bookshelf (NIH/NLM)",
     "https://www.ncbi.nlm.nih.gov/books/NBK585056/"),
    ("third-party · Tirzepatide is an imbalanced and biased dual GIP and GLP-1 receptor agonist – PMC (NIH/NLM)",
     "https://pmc.ncbi.nlm.nih.gov/articles/PMC7526454/"),
    ("third-party · Insights into the Mechanism of Action of Tirzepatide: A Narrative Review – PMC (NIH/NLM)",
     "https://pmc.ncbi.nlm.nih.gov/articles/PMC12847476/"),
    ("third-party · The Role of Tirzepatide, Dual GIP and GLP-1 Receptor Agonist, in the Management of Type 2 Diabetes: The SURPASS Clinical Trials – Diabetes Therapy (Springer Nature)",
     "https://link.springer.com/article/10.1007/s13300-020-00981-0"),
    ("official · OZEMPIC (semaglutide) injection — FDA Prescribing Information (2025 label, accessdata.fda.gov)",
     "https://www.accessdata.fda.gov/drugsatfda_docs/label/2025/209637s035,209637s037lbl.pdf"),
    ("third-party · Tirzepatide for type 2 diabetes – PMC (NIH/NLM)",
     "https://pmc.ncbi.nlm.nih.gov/articles/PMC10470858/"),
    ("official · ENP52: Incretins and Type 2 Diabetes Management | Endocrine Society",
     "https://www.endocrine.org/podcast/enp52-incretins-and-type-2-diabetes-management"),
    ("official · Tirzepatide is an imbalanced and biased dual GIP and GLP-1 receptor agonist – PMC / JCI Insight",
     "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7526454/"),
    ("official · Structural determinants of dual incretin receptor agonism by tirzepatide – PMC",
     "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC9060465/"),
    ("official · The Role of Tirzepatide, Dual GIP and GLP-1 Receptor Agonist, in the Management of Type 2 Diabetes: The SURPASS Clinical Trials – PubMed",
     "https://pubmed.ncbi.nlm.nih.gov/33325008/"),
    ("official · ZEPBOUND (tirzepatide) Full Prescribing Information – FDA accessdata (2026 label)",
     "https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/217806s042lbl.pdf"),
    ("official · MOUNJARO (tirzepatide) Prescribing Information – FDA Label (2022, updated 2026)",
     "https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/215866s009lbl.pdf"),
]


def _evidence():
    return [{"label": lab, "url": url, "text": "t"} for lab, url in BLOCKS]


def _rebuilt():
    gen = _gen()
    gen._evidence_blocks = _evidence()
    body = "## Quick answer\n" + " ".join(f"Claim {i} [S{i}]." for i in range(1, 18)) + "\n"
    return gen._rebuild_sources(body)


# ---------------------------------------------------------------- identity
def test_same_pmc_paper_under_two_hosts_has_one_identity():
    assert (_doc_identity("https://pmc.ncbi.nlm.nih.gov/articles/PMC7526454/")
            == _doc_identity("https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7526454/")
            == "pmc:7526454")


def test_identifier_schemes_are_read():
    assert _doc_identity("https://pubmed.ncbi.nlm.nih.gov/33325008/") == "pmid:33325008"
    assert _doc_identity("https://www.pnas.org/doi/10.1073/pnas.2116506119") == "doi:10.1073/pnas.2116506119"
    assert _doc_identity("https://doi.org/10.1073/pnas.2116506119") == "doi:10.1073/pnas.2116506119"
    assert _doc_identity(
        "https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/217806s042lbl.pdf") == "label:217806"
    assert _doc_identity("https://arxiv.org/abs/2403.05530") == "arxiv:2403.05530"


def test_an_ordinary_page_has_no_identity():
    assert _doc_identity("https://getpetermd.com/product/tirzepatide/") == ""
    assert _doc_identity("https://www.ncbi.nlm.nih.gov/books/NBK585056/") == ""
    assert _doc_identity("") == ""


# ---------------------------------------------------------------- title key
def test_the_same_paper_titled_by_two_publishers_shares_a_title_key():
    a = _doc_title_key("third-party · Structural determinants of dual incretin receptor agonism by "
                       "tirzepatide – PNAS (National Academy of Sciences)")
    b = _doc_title_key("official · Structural determinants of dual incretin receptor agonism by "
                       "tirzepatide – PMC")
    assert a and a == b


def test_a_spaced_hyphen_publisher_is_stripped_but_glp_1_is_not():
    k = _doc_title_key("official · The Role of Tirzepatide, Dual GIP and GLP-1 Receptor Agonist, in "
                       "the Management of Type 2 Diabetes: The SURPASS Clinical Trials - PubMed")
    assert "pubmed" not in k
    assert "glp 1 receptor agonist" in k


def test_a_short_or_generic_title_earns_no_key():
    # "FDA Official Prescribing Information" is four words — far too generic to identify a document,
    # and the two FDA labels would otherwise collide on it.
    assert _doc_title_key(BLOCKS[0][0]) == ""
    assert _doc_title_key(BLOCKS[1][0]) == ""
    assert _doc_title_key("Pricing") == ""


def test_only_a_reference_class_label_gets_a_title_key():
    lab = "Structural determinants of dual incretin receptor agonism by tirzepatide – PNAS"
    assert any(k.startswith("title:") for k in _doc_keys({"label": "third-party · " + lab, "url": ""}))
    # a BRAND page is labelled with the brand's name: two brands can both title a page the same way
    assert _doc_keys({"label": "PeterMD", "url": "https://getpetermd.com/pricing"}) == ()


# ---------------------------------------------------------------- editions
def test_a_later_filing_year_is_newer():
    assert _doc_is_newer(
        "https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/217806s042lbl.pdf",
        "https://www.accessdata.fda.gov/drugsatfda_docs/label/2023/217806s000lbl.pdf")
    assert _doc_version(
        "https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/217806s042lbl.pdf") == (2026, 42)


def test_a_later_year_wins_even_when_the_revision_number_is_lower():
    # Verified by fetching both: the 2026/s009 filing is "Revised: 01/2026" and the 2025/s031 one is
    # "Revised: 05/2025". Revision numbers run in separate series per amendment type, so they do not
    # compare across years — only the filing year orders editions.
    assert _doc_is_newer(
        "https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/215866s009lbl.pdf",
        "https://www.accessdata.fda.gov/drugsatfda_docs/label/2025/215866s031lbl.pdf")


def test_a_higher_revision_wins_inside_one_year():
    assert _doc_is_newer("https://x.gov/label/2026/217806s042lbl.pdf",
                         "https://x.gov/label/2026/217806s000lbl.pdf")


def test_an_undated_url_never_displaces_a_dated_one():
    assert not _doc_is_newer("https://pmc.ncbi.nlm.nih.gov/articles/PMC7526454/",
                             "https://www.accessdata.fda.gov/drugsatfda_docs/label/2023/x.pdf")


def test_a_scholarly_path_is_not_read_as_a_year():
    assert _doc_version("https://link.springer.com/article/10.1007/s13300-020-00981-0") == (0, 0)
    assert _doc_version("https://www.pnas.org/doi/10.1073/pnas.2116506119") == (0, 0)
    assert _doc_version("https://pubmed.ncbi.nlm.nih.gov/33325008/") == (0, 0)


# ------------------------------------------------- the real blog, end to end
def test_the_real_source_list_collapses_to_one_entry_per_document():
    out = _rebuilt()
    entries = [ln for ln in out.splitlines() if ln.startswith("- [S")]
    assert len(entries) == 12, entries          # 17 listed, 12 distinct documents
    listed = "\n".join(entries)
    # each duplicated document now has exactly ONE url in the list
    assert listed.count("PMC7526454") == 1
    assert listed.count("pnas.2116506119") + listed.count("PMC9060465") == 1
    assert listed.count("s13300-020-00981-0") + listed.count("33325008") == 1
    assert listed.count("217806") == 1
    assert listed.count("215866") == 1


def test_the_stale_zepbound_label_is_replaced_by_its_2026_edition():
    listed = "\n".join(ln for ln in _rebuilt().splitlines() if ln.startswith("- [S"))
    assert "label/2026/217806s042lbl.pdf" in listed
    assert "label/2023/217806s000lbl.pdf" not in listed


def test_the_mounjaro_label_is_replaced_by_its_2026_edition_too():
    listed = "\n".join(ln for ln in _rebuilt().splitlines() if ln.startswith("- [S"))
    assert "label/2026/215866s009lbl.pdf" in listed
    assert "label/2025/215866s031lbl.pdf" not in listed


def test_every_citation_still_resolves_and_numbering_is_contiguous():
    import re
    out = _rebuilt()
    prose = out.split("## Sources")[0]
    cited = sorted({int(m) for m in re.findall(r"\[S(\d+)\]", prose)})
    listed = sorted(int(m) for m in re.findall(r"- \[S(\d+)\]", out))
    assert listed == list(range(1, len(listed) + 1))
    assert set(cited) <= set(listed)
    assert len(cited) == 12          # the 17 markers now point at 12 numbers, none dropped


def test_a_body_with_no_duplicates_is_unchanged():
    gen = _gen()
    gen._evidence_blocks = [
        {"label": "official · A", "url": "https://a.gov/x", "text": "t"},
        {"label": "third-party · B", "url": "https://b.com/y", "text": "t"},
    ]
    out = gen._rebuild_sources("## Quick answer\nOne [S1]. Two [S2].\n")
    assert "a.gov/x" in out and "b.com/y" in out
    assert out.count("- [S") == 2


def test_the_rebuild_stays_idempotent():
    first = _rebuilt()
    gen = _gen()
    gen._evidence_blocks = _evidence()
    assert gen._rebuild_sources(first) == first
