"""FU240 — the article cited documents nobody had read past 5% of.

A semaglutide safety article told readers fatigue affects about 5% of patients and called hair loss
an "emerging, underexplored signal", both cited to the Wegovy label [S1]. The label says fatigue
11% versus 5% and lists Hair Loss at 3% versus 1% as a labeled adverse reaction. It reported
pancreatitis as 8 versus 10 (0.27 vs 0.33 per 100 patient-years) cited to a CYSTIC FIBROSIS trial
protocol; the Ozempic label says 7 versus 3 (0.3 vs 0.2). It attributed ischemic optic neuropathy to
an ALCOHOL USE DISORDER trial protocol.

None of it was invented freely. The labels ARE fetched and ARE converted to text — and then cut to
their first 6,000 characters, which for a drug label is the HIGHLIGHTS page:

    Wegovy label   117,128 chars   fatigue table at 29,241 · hair loss at 29,465
    Ozempic label  127,944 chars   adjudicated pancreatitis at 27,153
    writer saw     first 6,000     = 5.3% of the document
    checker read   first 20,000    = short of all three

So the model had the label's name and its highlights, wrote the specifics from memory, and attached
the label's [S#]. Meanwhile clinicaltrials.gov is a .gov, so protocol PDFs — long, readable and
dense with numbers — collected `official ·` and the citations that follow it.

$0, no network.
"""
import os
import re

import pytest

from generators.brand_enrichment import relevant_text
from generators.blog_gen import BlogGenerator, _is_trial_protocol, _official_source_ok

PINS = ["fda.gov", "accessdata.fda.gov", "dailymed.nlm.nih.gov", "nih.gov", "cdc.gov"]
FIX = os.path.join(os.path.dirname(__file__), "fixtures", "fu240_label_slice.txt")
TERMS = ["fatigue", "hair loss", "alopecia", "pancreatitis", "semaglutide", "thyroid"]


@pytest.fixture(scope="module")
def label():
    return open(FIX, encoding="utf-8").read()


# ── 1. keep the passages about THIS article, not the first N bytes ───────────────────────────────
def test_the_head_alone_misses_what_the_article_is_about(label):
    """The premise. If this ever fails the fixture stopped reproducing the problem."""
    assert "Hair Loss" not in label[:6000]
    assert "Hair Loss" in label


def test_the_adverse_reaction_table_now_reaches_the_writer(label):
    out = relevant_text(label, TERMS, 6000)
    assert len(out) <= 6000
    assert "Hair Loss" in out and "Fatigue" in out
    assert "WEGOVY 2.4 mg" in out, "the column header, without which the numbers are unreadable"


def test_the_head_is_still_kept(label):
    """A label's boxed warning and indications are load-bearing; this replaces the truncation, not
    the head."""
    assert "HIGHLIGHTS" in relevant_text(label, TERMS, 6000)


def test_a_rare_term_outranks_a_frequent_one(label):
    """Counting distinct terms rewards boilerplate: 'semaglutide' occurs ~100 times in a label and
    'hair loss' twice, so density by count keeps the highlights and drops the table."""
    assert label.lower().count("semaglutide") > 20 * label.lower().count("hair loss")
    assert "Hair Loss" in relevant_text(label, TERMS, 6000)


def test_a_short_document_is_returned_untouched():
    assert relevant_text("a short page", ["page"], 6000) == "a short page"


def test_no_terms_degrades_to_the_truncation_it_replaces(label):
    assert relevant_text(label, [], 6000) == label[:6000]


def test_a_term_that_never_appears_degrades_the_same_way(label):
    assert relevant_text(label, ["kryptonite"], 6000) == label[:6000]


def test_the_gap_between_passages_is_marked(label):
    """Two passages 20,000 characters apart must not read as continuous prose."""
    assert "[…]" in relevant_text(label, TERMS, 6000)


def test_the_budget_is_never_exceeded(label):
    for cap in (1000, 2500, 6000, 12000):
        assert len(relevant_text(label, TERMS, cap)) <= cap


# ── the terms themselves are derived from the article, and are vertical-neutral ───────────────────
def test_terms_come_from_the_articles_own_question():
    t = BlogGenerator._seed_page_terms(
        "is semaglutide safe? hair loss, fatigue and the cancer question", {"category": "telehealth"})
    for w in ("semaglutide", "hair", "loss", "fatigue", "cancer"):
        assert w in t


def test_terms_assume_no_vertical():
    t = BlogGenerator._seed_page_terms("which concrete sealer is best for a garage floor",
                                       {"category": "construction supplies"})
    assert "concrete" in t and "sealer" in t
    assert not any(w in t for w in ("semaglutide", "fatigue", "clinical"))


def test_filler_words_are_not_terms():
    t = BlogGenerator._seed_page_terms("what is the best online program", {})
    assert "best" not in t and "online" not in t


# ── 2. the figure checker reads the whole document ───────────────────────────────────────────────
def test_the_checker_reads_far_past_the_old_cap():
    """It matches in Python, never in a prompt, so stopping at 20,000 characters bought nothing and
    cost every figure past it."""
    assert BlogGenerator._CLAIM_PAGE_CHARS >= 100000
    assert BlogGenerator._CLAIM_PAGE_CHARS > 20000


# ── 3. a trial protocol is not an authority ──────────────────────────────────────────────────────
@pytest.mark.parametrize("url,title", [
    ("https://cdn.clinicaltrials.gov/large-docs/65/NCT05788965/Prot_SAP_000.pdf",
     "Semaglutide in CFRD - ClinicalTrials.gov Protocol"),
    ("https://cdn.clinicaltrials.gov/large-docs/75/NCT05520775/Prot_SAP_001.pdf",
     "Semaglutide for Alcohol Use Disorder Protocol"),
])
def test_the_two_protocols_that_carried_clinical_facts_are_not_official(url, title):
    assert _is_trial_protocol(url, title)
    assert not _official_source_ok(url, title, "PeterMD", "getpetermd.com", PINS)


@pytest.mark.parametrize("url,title", [
    ("https://clinicaltrials.gov/study/NCT03548987", "STEP 2 study record"),
    ("https://www.accessdata.fda.gov/drugsatfda_docs/label/2023/215256s007lbl.pdf",
     "WEGOVY prescribing information"),
    ("https://www.ncbi.nlm.nih.gov/books/NBK548574/", "Semaglutide - LiverTox"),
    ("https://www.endocrine.org/clinical-practice-guidelines/pharmacological-management-of-obesity",
     "Pharmacological Management of Obesity"),
])
def test_a_registry_record_a_label_and_a_guideline_stay_official(url, title):
    """The rejection is of the PLANNING document, not of clinicaltrials.gov."""
    assert _official_source_ok(url, title, "PeterMD", "getpetermd.com", PINS)


def test_a_protocol_on_another_host_is_not_caught():
    """Scoped deliberately: this is about clinicaltrials.gov documents, not the word 'protocol'."""
    assert not _is_trial_protocol("https://www.nejm.org/doi/full/10.1056/NEJMoa2032183",
                                  "Once-Weekly Semaglutide in Adults — study protocol available")
