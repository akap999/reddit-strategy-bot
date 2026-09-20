"""FU228 — the substance guard must not restore a SECOND comparison table.

The guard re-appends the DRAFT's copy of any section the rewrite dropped. When the reconcile RENAMES
a section while CORRECTING its facts, the draft copy lands beside the corrected one and the article
states two values for the same metric. That shipped on a real medical page: a comparison table saying
the GLP-1R affinity is "~5x weaker" next to a restored draft table saying "~18-20x weaker", and a
"Superior to highest approved semaglutide dose" cell the rewrite had already replaced.

Measured on that article, neither available signal separates a rename from a genuinely new section:
  - headings differed by one noun ("GLP-1 agonists" -> "semaglutide"), which reads exactly like the
    "Kitchen" -> "Bathroom" case `_forms_match` deliberately treats as DIFFERENT sections;
  - content overlap of the stale duplicate was 0.66, while legitimately distinct sections of the
    same article scored as high as 0.88.
The comparison TABLE is the one unambiguous signal: a blog has exactly one by construction.
"""
import pytest

from generators.blog_gen import BlogGenerator, _has_table
from tests.stubs import StubClaude


def _gen():
    g = BlogGenerator(StubClaude(), None)
    g._dup_table_note = ""
    return g


# the real sections from the shipped article, verbatim
STALE_TABLE_SECTION = """## Tirzepatide vs. GLP-1 agonists: mechanism comparison

| Feature | Tirzepatide | Selective GLP-1 agonist (e.g. semaglutide) |
| --- | --- | --- |
| GLP-1R affinity vs. native GLP-1 | ~18-20x weaker [S25] | Comparable to native GLP-1 |
| Head-to-head weight/glucose outcome | Superior to highest approved semaglutide dose [S7] | Reference comparator |
"""

REVISED_TABLE_SECTION = """## Tirzepatide vs. semaglutide: mechanism comparison

| Feature | Tirzepatide | Semaglutide (selective GLP-1 agonist) |
| --- | --- | --- |
| GLP-1R affinity vs. native GLP-1 | ~5x weaker [S7] | GLP-1R (full agonist) [S9] |
| Head-to-head weight outcome | 20.2% vs 13.7% at 72 weeks (SURMOUNT-5) [S4] | 13.7% at 72 weeks [S4] |
"""

PROSE_SECTION = """## A note on compounded tirzepatide and pharmacy rules

Under current compounding law a 503A pharmacy may not compound a drug that is essentially a copy of
a commercially available drug. Confirm current shortage status with a licensed physician.
"""

FAQ = """## FAQ

### Is tirzepatide the same as a GLP-1?

No. It is a dual GIP and GLP-1 receptor agonist.
"""


def test_the_shipped_contradiction_cannot_happen_again():
    g = _gen()
    draft = STALE_TABLE_SECTION + "\n" + PROSE_SECTION
    revised = REVISED_TABLE_SECTION + "\n" + FAQ
    out = g._restore_dropped_sections(draft, revised)
    assert "18-20x weaker" not in out            # the stale affinity value is gone
    assert "highest approved semaglutide dose" not in out
    assert out.count("| Feature |") == 1         # exactly one comparison table
    assert "~5x weaker" in out                   # the corrected value survives
    assert g._dup_table_note                     # and the operator is told


def test_genuinely_dropped_prose_is_still_restored():
    g = _gen()
    draft = REVISED_TABLE_SECTION + "\n" + PROSE_SECTION
    out = g._restore_dropped_sections(draft, REVISED_TABLE_SECTION + "\n" + FAQ)
    assert "503A pharmacy" in out                # the FU54 guarantee is intact
    assert not g._dup_table_note


def test_a_table_section_is_restored_when_the_rewrite_kept_none():
    """The guard still rescues an outright DELETED comparison — the rule only forbids a SECOND one."""
    g = _gen()
    out = g._restore_dropped_sections(STALE_TABLE_SECTION, PROSE_SECTION + "\n" + FAQ)
    assert "| Feature |" in out
    assert not g._dup_table_note


def test_restored_substance_still_lands_before_the_faq():
    g = _gen()
    out = g._restore_dropped_sections(REVISED_TABLE_SECTION + "\n" + PROSE_SECTION,
                                      REVISED_TABLE_SECTION + "\n" + FAQ)
    assert out.index("503A pharmacy") < out.index("## FAQ")


def test_has_table_reads_a_real_table_and_not_prose():
    assert _has_table(STALE_TABLE_SECTION)
    assert not _has_table(PROSE_SECTION)
    assert not _has_table("")
    assert not _has_table("A line with | pipes | but no separator row")


def test_the_note_rides_the_pause_checkpoint():
    assert "_dup_table_note" in BlogGenerator._CHECK_NOTES


def test_collapsing_a_duplicate_document_does_not_leave_a_doubled_citation():
    """FU226 gives two markers that pointed at one document the same number. A sentence citing both
    routes to a paper would then read "[S2][S2]" -- which shipped on the article this round."""
    g = _gen()
    g._evidence_blocks = [
        {"label": "third-party · Imbalanced and biased dual agonist – PMC", "text": "t",
         "url": "https://pmc.ncbi.nlm.nih.gov/articles/PMC7526454/"},
        {"label": "official · Imbalanced and biased dual agonist – PMC / JCI Insight", "text": "t",
         "url": "https://www.ncbi.nlm.nih.gov/pmc/articles/PMC7526454/"},
        {"label": "official · ZEPBOUND label", "text": "t", "url": "https://x.gov/a.pdf"},
    ]
    out = g._rebuild_sources("## X\nBiased toward GIPR [S1][S2]. Approved for weight loss [S3].\n")
    prose = out.split("## Sources")[0]
    assert "[S1][S1]" not in prose and "[S2][S2]" not in prose
    assert "[S1]" in prose and "[S2]" in prose          # both documents still cited, once each
    assert out.count("- [S") == 2                       # one entry per document


def test_two_genuinely_different_sources_still_cite_side_by_side():
    g = _gen()
    g._evidence_blocks = [
        {"label": "official · A", "text": "t", "url": "https://a.gov/x"},
        {"label": "official · B", "text": "t", "url": "https://b.gov/y"},
    ]
    out = g._rebuild_sources("## X\nBoth agree [S1][S2].\n")
    assert "[S1][S2]" in out.split("## Sources")[0]


# the second half of the shipped duplication: a sub-section restored outside its parent
STALE_H3 = """### Why is tirzepatide more effective for weight loss than some GLP-1 drugs?

In a head-to-head trial, all three doses beat the highest approved dose of semaglutide [S7].
"""

FAQ_WITH_ANSWER = """## FAQ

### Why is tirzepatide more effective for weight loss than semaglutide?

In the SURMOUNT-5 trial tirzepatide achieved 20.2% versus 13.7% at 72 weeks [S4].
"""


def test_a_sub_section_is_not_restored_outside_its_section():
    """The draft's H3 belongs to the comparison section. That section is not restored (the rewrite
    already has one), so the H3 would land orphaned before the FAQ -- which is how a second
    'Why is X more effective?' answer shipped, carrying the draft's older trial framing."""
    g = _gen()
    draft = STALE_TABLE_SECTION + "\n" + STALE_H3
    out = g._restore_dropped_sections(draft, REVISED_TABLE_SECTION + "\n" + FAQ_WITH_ANSWER)
    assert "highest approved dose" not in out
    assert out.count("Why is tirzepatide more effective") == 1     # only the FAQ's
    assert "| Feature |" in out and out.count("| Feature |") == 1


def test_a_whole_deleted_section_still_comes_back_with_its_sub_sections():
    """The FU54 guarantee: when the PARENT is genuinely gone too, both are restored, in order."""
    g = _gen()
    draft = PROSE_SECTION + "\n" + STALE_H3
    out = g._restore_dropped_sections(draft, REVISED_TABLE_SECTION + "\n" + FAQ_WITH_ANSWER)
    assert "503A pharmacy" in out
    assert "Why is tirzepatide more effective for weight loss than some GLP-1 drugs?" in out
    assert out.index("503A pharmacy") < out.index("than some GLP-1 drugs")


def test_a_deleted_entity_profile_is_still_restored():
    """The orphan rule must not eat real substance: a competitor profile is not a question, and the
    FU54/FU220 guarantee that a genuinely deleted profile comes back is unchanged."""
    g = _gen()
    draft = "## The agencies compared\n\nIntro.\n\n### Loganix\n\nLoganix builds links [S2].\n"
    revised = "## The agencies compared\n\nIntro.\n\n" + FAQ_WITH_ANSWER
    out = g._restore_dropped_sections(draft, revised)
    assert "### Loganix" in out


def test_the_sources_separator_is_a_hyphen():
    g = _gen()
    g._evidence_blocks = [{"label": "official · A", "url": "https://a.gov/x", "text": "t"}]
    out = g._rebuild_sources("## X\nA claim [S1].\n")
    assert "- [S1] official · A - <https://a.gov/x>" in out
    assert "—" not in out.split("## Sources")[1]


def test_an_h2_that_repeats_the_h1_is_dropped():
    g = _gen()
    body = ("# Is Tirzepatide a GLP-1?\n\n## Is Tirzepatide a GLP-1?\n\n"
            "**Quick answer:** No, it is a dual agonist.\n\n## What is it?\n\nA peptide.\n")
    out = g._drop_h1_echo(body)
    assert out.count("Is Tirzepatide a GLP-1?") == 1
    assert out.startswith("# Is Tirzepatide a GLP-1?")
    assert "**Quick answer:**" in out and "## What is it?" in out


def test_a_first_h2_that_merely_resembles_the_h1_is_kept():
    g = _gen()
    body = "# Is tirzepatide a GLP-1?\n\n## So, is tirzepatide a GLP-1 drug?\n\nNo.\n"
    assert g._drop_h1_echo(body) == body
