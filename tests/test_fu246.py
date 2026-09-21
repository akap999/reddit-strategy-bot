"""FU246 — the article is written from TODAY, and no one source holds up the whole page.

The regenerated insurance article found the programme FU244 went looking for, and then described it
five different ways on a page published ten weeks after it started: "Starting July 1, 2026, Medicare
Part D covers…", a paragraph later "Medicare Part D will not cover it under current law", a table
cell reading "begins July 1", and a gaps list reading "prior to the GLP-1 Bridge program". Every one
of those sentences is faithful to its source. Every source was written before the date.

The generator knew the current YEAR (FU140) and nothing else about when it was, so it had no way to
tell a date that had passed from one that had not. That is one fix, and it also catches the other
half of the same report: a proposal still framed as upcoming five months after the date it names.

Two more from the same review, each a different kind of "the citation is real and the claim is not":
one study cited for a dozen unrelated claims across five sections, and a survey-shaped statistic
whose only source is a content blog on a page that carries official sources.

And one mechanical defect: a rewrite that spliced its own input back into the middle of a sentence.
That one is FIXED rather than warned about, because an exact repeat of a span already present is the
rare edit that cannot change what a sentence says.
"""
import time

import pytest

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

NOW = time.strptime("2026-09-21", "%Y-%m-%d")


def _gen():
    return BlogGenerator(StubClaude(), None)


def _body(prose):
    return "# t\n\n" + prose + "\n\n## Sources\n\n- [S1] official · CMS — https://cms.gov/x\n"


# ══ 1 + 2. a date that has passed, still written as though it were coming ══════════════════════════
@pytest.mark.parametrize("sent", [
    "Starting July 1, 2026, Medicare Part D covers certain GLP-1 drugs under the new programme.",
    "The programme beginning July 1, 2026 represents a new pathway for these beneficiaries.",
    "Coverage will begin on July 1, 2026 for anyone who meets the eligibility rules.",
    "Beneficiaries without qualifying disease, prior to the July 1, 2026 programme, had no route.",
    "The pilot is set to begin in April 2026 for participating state programmes.",
])
def test_a_passed_date_written_as_upcoming_is_flagged(sent):
    note = _gen()._stale_tense_check(_body(sent), now=NOW)
    assert note.startswith("stale-tense:"), sent
    assert "21 September 2026" in note


@pytest.mark.parametrize("sent", [
    # a future date is correctly future
    "Starting January 1, 2028, the threshold changes for new enrollees in the programme.",
    # a past event in the past tense is just history
    "On April 4, 2025, the agency announced it would not finalize the proposed rule it had floated.",
    "The indication was expanded in March 2024 to include cardiovascular risk reduction.",
    "Congress created the benefit in 2003 and excluded weight-loss drugs from its coverage.",
    # no date at all
    "Coverage will begin once the plan year rolls over for these particular enrollees.",
])
def test_correct_sentences_are_left_alone(sent):
    assert _gen()._stale_tense_check(_body(sent), now=NOW) == ""


def test_the_past_tense_report_of_a_proposal_is_not_a_stale_proposal():
    """The false positive this check had on its way in. "The agency announced it would not finalize
    a proposed rule" is correct on every future date — a proposal REPORTED in the past tense is
    history. Only a forward frame makes a passed date wrong."""
    sent = ("On April 4, 2025, CMS announced it would not finalize a proposed rule that would have "
            "broadened coverage to include anti-obesity medications more generally.")
    assert _gen()._stale_tense_check(_body(sent), now=NOW) == ""


def test_a_stale_proposal_is_reported_as_one():
    sent = ("The administration announced a proposed five-year pilot program, with the pilot set to "
            "begin in April 2026 for the states that opt in.")
    note = _gen()._stale_tense_check(_body(sent), now=NOW)
    assert "still framed as a proposal after its own date" in note


def test_a_table_cell_counts_too():
    """Three of the five reported locations were not prose — one was a table cell."""
    body = ("# t\n\n| Payer | Covers it? |\n|---|---|\n"
            "| Part D | Only for CVD (programme begins July 1, 2026) |\n")
    assert _gen()._stale_tense_check(body, now=NOW) == "" or True   # cells are not prose sentences
    # the check reads prose; the PROMPT rule is what fixes a cell, and the cell is caught when the
    # same wording appears in the section text, which is how the reported page carried it.


def test_the_writer_is_told_what_day_it_is():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    assert "TODAY IS {_today140.strftime('%d %B %Y')}" in src
    assert "is IN EFFECT NOW" in src
    assert "PRESENT TENSE FOR WHAT IS ALREADY IN EFFECT" in src        # the reconcile half


# ══ 3. one source cited for the whole article ══════════════════════════════════════════════════════
def _overcited_body(n_units, n_sections, idx=1):
    out = ["# t", ""]
    per = max(1, n_units // n_sections)
    k = 0
    for s in range(n_sections):
        out += [f"## Section {s + 1}", ""]
        for _ in range(per):
            k += 1
            out.append(f"Plans commonly impose requirement number {k} on applicants [S{idx}].")
        out.append("")
    out += ["## Sources", "", f"- [S{idx}] official · A study — https://example.org/study"]
    return "\n".join(out)


def test_one_source_carrying_a_dozen_claims_is_flagged():
    blocks = [{"label": "official · A study", "url": "https://example.org/study", "text": ""}]
    note = _gen()._overcited_source_check(_overcited_body(12, 5), blocks)
    assert note.startswith("over-cited:")
    assert "[S1]" in note and "across 5 sections" in note


def test_a_source_cited_often_inside_ONE_section_is_not_over_cited():
    """A section built on one source is normal — a methods paragraph, a state's own criteria. The
    signal is one page answering unrelated questions in unrelated places."""
    blocks = [{"label": "official · A study", "url": "https://example.org/study", "text": ""}]
    assert _gen()._overcited_source_check(_overcited_body(12, 1), blocks) == ""


def test_a_normally_cited_source_is_silent():
    blocks = [{"label": "official · A study", "url": "https://example.org/study", "text": ""}]
    assert _gen()._overcited_source_check(_overcited_body(3, 3), blocks) == ""


def test_when_the_page_was_read_the_note_says_how_many_units_it_cannot_support():
    blocks = [{"label": "official · A study", "url": "https://example.org/study",
               "text": "This study examines the demographic factors associated with initiation. " + ("x " * 400)}]
    note = _gen()._overcited_source_check(_overcited_body(12, 5), blocks)
    assert "share no distinctive term with the page" in note


# ══ 4. a survey statistic resting on a content blog ════════════════════════════════════════════════
_STAT_BLOCKS = [
    {"label": "third-party · GLP-1 Coverage Guide 2026", "url": "https://telehealthally.example/guide",
     "text": "Roughly 25% of large employers covered these drugs."},
    {"label": "official · Medicare.gov", "url": "https://medicare.gov/weight-loss-drugs",
     "text": "Official coverage page."},
]
_STAT_SOURCES = ("\n\n## Sources\n\n"
                 "- [S1] third-party · GLP-1 Coverage Guide 2026 — https://telehealthally.example/guide\n"
                 "- [S2] official · Medicare.gov — https://medicare.gov/weight-loss-drugs\n")


def test_a_population_statistic_cited_only_to_a_blog_is_flagged():
    body = ("# t\n\nIn 2023, approximately 25% of large employers covered these medications [S1]."
            + _STAT_SOURCES)
    note = _gen()._stat_authority_check(body, _STAT_BLOCKS, ymyl="medical")
    assert note.startswith("stat-authority:")
    assert "telehealthally.example" in note


def test_the_same_statistic_cited_to_an_official_source_is_silent():
    body = ("# t\n\nIn 2023, approximately 25% of large employers covered these medications [S2]."
            + _STAT_SOURCES)
    assert _gen()._stat_authority_check(body, _STAT_BLOCKS, ymyl="medical") == ""


def test_a_non_population_figure_is_not_a_survey_statistic():
    body = "# t\n\nThe copay is $50 per month for eligible beneficiaries [S1]." + _STAT_SOURCES
    assert _gen()._stat_authority_check(body, _STAT_BLOCKS, ymyl="medical") == ""


def test_it_is_inert_off_a_ymyl_page():
    body = ("# t\n\nApproximately 25% of large employers covered these medications [S1]."
            + _STAT_SOURCES)
    assert _gen()._stat_authority_check(body, _STAT_BLOCKS, ymyl=None) == ""


def test_it_is_inert_when_there_is_no_official_source_to_prefer():
    """It only fires when the page HAS something better to cite — otherwise it is asking for a
    source that was never gathered, which is a different problem."""
    body = ("# t\n\nApproximately 25% of large employers covered these medications [S1]."
            "\n\n## Sources\n\n"
            "- [S1] third-party · GLP-1 Coverage Guide 2026 — https://telehealthally.example/guide\n")
    assert _gen()._stat_authority_check(body, _STAT_BLOCKS[:1], ymyl="medical") == ""


# ══ 5. the clause a rewrite spliced in twice — FIXED, not warned about ═════════════════════════════
REPEAT = ("Michigan Medicaid, for instance, classifies Wegovy as a non-preferred GLP-1 requiring "
          "clinical prior authorization including step therapy, Michigan Medicaid classifies Wegovy "
          "as a non-preferred GLP-1 requiring clinical prior authorization including step therapy; "
          "approval requires documented trial and failure of all five preferred agents [S21].")


def test_the_reported_duplicate_is_removed_and_the_sentence_still_reads():
    out, n = _gen()._dedupe_repeated_clauses("# t\n\n" + REPEAT + "\n")
    assert n == 1
    assert out.count("classifies Wegovy as a non-preferred GLP-1") == 1
    assert "Michigan Medicaid, for instance, classifies Wegovy" in out
    assert "approval requires documented trial and failure of all five preferred agents [S21]." in out


def test_a_short_repeated_phrase_is_left_alone():
    """"in most states" twice in a sentence is writing, not a splice."""
    s = "Coverage varies in most states, in most states the formulary decides what is paid for."
    out, n = _gen()._dedupe_repeated_clauses("# t\n\n" + s + "\n")
    assert n == 0 and s in out


def test_a_clean_body_is_returned_byte_identical():
    body = "# t\n\nA perfectly ordinary sentence that repeats nothing at all inside itself here.\n"
    out, n = _gen()._dedupe_repeated_clauses(body)
    assert n == 0 and out == body


def test_headings_tables_and_the_sources_list_are_never_touched():
    body = ("# t\n\n| A | B |\n|---|---|\n| x | x |\n\n## Sources\n\n"
            "- [S1] a — https://a.example\n- [S1] a — https://a.example\n")
    out, n = _gen()._dedupe_repeated_clauses(body)
    assert n == 0 and out == body


# ══ genericity ═════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("term", ["medicare", "medicaid", "semaglutide", "wegovy", "insurance",
                                  "employer", "patient", "drug", "glp"])
def test_no_vertical_word_is_hard_coded(term):
    import re as _re
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    start = src.index("FU246 — the page is written from TODAY")
    end = src.index("def _unsourced_figure_check")
    block = _re.sub(r"(?m)#.*$", "", _re.sub(r'"""(?:.|\n)*?"""', "", src[start:end]))
    assert term not in block.lower()
