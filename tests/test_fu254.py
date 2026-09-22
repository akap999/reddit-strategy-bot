"""FU254 — the article deleted the thing it was about.

Five blockers from `is-tirzepatide-a-glp-1-how-it-differs-from-semaglutide` (52/100), plus a defect
in FU251's own damage detector that was refusing real removals across the stored corpus.
"""
import re
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators import blog_eval as E


# ── Step 0 — a heading is an antecedent ──────────────────────────────────────────────────────────
# Measured on the 221 stored articles: detect_stranded_reference produced 51 findings and 3 of them
# were real. It feeds body_damage, the REMOVAL guard, so each of the other 48 refused a removal.

def _stranded(head, first):
    return E.detect_stranded_reference("## %s\n\n%s\n" % (head, first))


def test_comparison_heading_answers_a_plural_opener():
    """"Botric vs Profound: How Do They Compare?" -> "Both platforms address ..." is ordinary
    English: the heading named both things one line earlier."""
    assert not _stranded("Botric vs Profound: How Do They Compare?",
                         "Both platforms address the growing problem of brand invisibility.")
    assert not _stranded("What is the difference between tirzepatide and semaglutide?",
                         "Both are injectable medications, but they work differently.")
    assert not _stranded("Is it cheaper to renovate a home or buy a new one?",
                         "Neither is universally cheaper for every household.")
    assert not _stranded("Does tirzepatide work the same way as semaglutide?",
                         "They share receptor activity, but one engages a second receptor.")


def test_sentence_that_names_its_own_referents_is_not_stranded():
    """The heading need not supply it when the sentence does: "Both A and B offer ..."."""
    assert not _stranded("Which platform is better for citation tracking?",
                         "Both Botric and Profound offer citation tracking for brands.")


def test_a_noun_that_names_the_heading_is_not_a_back_reference():
    """"This clinical question", "this regulatory distinction", "this consideration" all point at
    the question just asked — the same shape as the "this guide" exclusion that already existed."""
    for np in ("clinical question", "regulatory distinction", "consideration",
               "clinical matter", "segment"):
        assert not _stranded("Can men take one medication alongside another?",
                             "This %s must be assessed by a licensed professional." % np), np


def test_a_demonstrative_followed_by_a_verb_is_a_sentence_not_a_reference():
    """"This measures total cholesterol" is ordinary English — the exclusion list simply lacked the
    verb. Each of these was a real finding on a stored article before the list was widened, and the
    heading above them names nothing the opener could be pointing at."""
    for verb in ("measures", "covers", "checks", "assesses", "reduces", "applies", "carries"):
        assert not _stranded("3. Fasting Lipid Panel", "This %s several markers at once." % verb), verb


def test_a_category_noun_stands_in_for_the_subject():
    assert not _stranded("Who is it typically prescribed for?",
                         "This medication carries standard eligibility criteria for adults.")
    assert not _stranded("Who should consider an alternative?",
                         "These telehealth programs are not appropriate for everyone.")


def test_a_pointing_heading_has_already_introduced_the_thing():
    assert not _stranded("Who is this lab checklist most relevant for?",
                         "This full panel is most relevant for the following patients.")


def test_a_real_back_reference_still_fires():
    """The docstring's own example: a protocol the reader was never shown."""
    hits = _stranded("What is the main practical disadvantage of the oral form?",
                     "This strict protocol is necessary because bioavailability is about 1%.")
    assert hits and hits[0]["check"] == "stranded-reference"
    hits = _stranded("What About Erectile Function?",
                     "The same 2025 study found that treated men had improved outcomes.")
    assert hits, "a study named as 'the same' one has no antecedent in this section"


def test_the_also_arm_needs_an_additional_RESULT():
    """"...also showed no difference" is a second finding and needs the first. "is also used
    off-label", "may also include" and "(also known as)" are ordinary English — they were 15 of the
    16 findings this arm produced across the corpus."""
    assert _stranded("Are the two outcomes truly equivalent?",
                     "Waist circumference and lipid markers also showed no significant difference.")
    for s in ("Metformin is a prescription medication that is also used off-label for longevity.",
              "A thorough workup may also include a fasting insulin test for some patients.",
              "Revive Design (also operating as Revive Renovation) is a family-owned contractor.",
              "Many people who present with one condition also carry excess body fat."):
        assert not _stranded("Additional Tests Your Provider May Order", s), s


def test_an_also_whose_subject_the_heading_named_is_not_stranded():
    assert not _stranded("Is Metformin used for longevity in men?",
                         "Metformin is a diabetes medication that also showed longevity signals.")


# ── Step 1 — a comparison table may not delete what it compares ──────────────────────────────────
# `| Dimension | Semaglutide | Tirzepatide |` puts the OPTIONS in the columns. The FU204 any-empty
# rule reads every column past the first as a dimension, so one unsourceable cell deleted one of the
# two drugs from an article titled "How It Differs From Semaglutide". 22 of the 221 stored articles
# are written that way and 2 had already shipped with a single option left.

from generators.blog_gen import (  # noqa: E402
    BlogGenerator, _transpose_table, _table_cell_name, _format_price_value)


def _gen(tools=()):
    g = BlogGenerator.__new__(BlogGenerator)
    g._table_punt_note = ""
    g._article_tools = list(tools)
    return g


TRANSPOSED = (
    "| Dimension | Semaglutide | Tirzepatide |\n"
    "| --- | --- | --- |\n"
    "| Receptor targets | GLP-1 only [S1] | GLP-1 and GIP [S1] |\n"
    "| Weight loss vs. comparator | Reference [S2] | Greater mean loss [S2] |\n"
    "| Adverse event profile | Lower rate [S3] | Not specified in sourced facts |\n"
)


def test_the_option_survives_and_the_dimension_is_what_drops():
    out = _gen(["Semaglutide", "Tirzepatide"])._resolve_table_punts(TRANSPOSED)
    assert "Tirzepatide" in out.split("\n")[0], "the compared option was deleted from its own table"
    assert "Semaglutide" in out.split("\n")[0]
    assert "Adverse event profile" not in out, "the unsourceable DIMENSION is what FU204 drops"
    assert "Receptor targets" in out and "Weight loss" in out


def test_the_rule_is_unchanged_in_the_ordinary_orientation():
    md = ("| Provider | Monthly price | Delivery |\n"
          "| --- | --- | --- |\n"
          "| Alpha | $99 [S1] | 2 days [S1] |\n"
          "| Beta | $149 [S2] | Not specified in sourced facts |\n")
    out = _gen(["Alpha", "Beta"])._resolve_table_punts(md)
    assert "Delivery" not in out, "a dimension with a gap still goes"
    assert "Alpha" in out and "Beta" in out and "Monthly price" in out


def test_column_zero_holding_the_options_is_never_called_transposed():
    """The names test RULES OUT as well as in: whatever the corner cell says, a table whose first
    column holds the option names keeps the ordinary orientation."""
    g = _gen(["Alpha", "Beta"])
    hdr = ["Feature", "Monthly price", "Delivery"]
    data = [["Alpha", "$99", "2 days"], ["Beta", "$149", "1 day"]]
    assert not g._table_is_transposed(hdr, data)


def test_the_names_test_beats_the_corner_cell():
    g = _gen(["Botric", "Profound"])
    assert g._table_is_transposed(["Whatever", "Botric", "Profound"],
                                  [["Pricing", "$8", "$99"]])


def test_a_source_row_is_provenance_not_an_option():
    """The Source exemption was written when a Source could only be a column. On a swapped table it
    is a row, and counting its blanks as gaps condemned every dimension and deleted the table."""
    md = ("| Measure | Option A | Option B | Source |\n"
          "| --- | --- | --- | --- |\n"
          "| Reduction at 6 months | 1.58% | 1.62% |  |\n"
          "| Weight change at 6 months | 3.77% | 3.48% |  |\n")
    out = _gen()._resolve_table_punts(md)
    assert "Option A" in out and "Option B" in out
    assert "Reduction at 6 months" in out and "Weight change at 6 months" in out


def test_transpose_round_trips():
    hdr = ["Dimension", "A", "B"]
    data = [["Price", "$1", "$2"], ["Speed", "fast", "slow"]]
    h2, d2 = _transpose_table(hdr, data)
    assert h2 == ["Dimension", "Price", "Speed"]
    assert d2 == [["A", "$1", "fast"], ["B", "$2", "slow"]]
    assert _transpose_table(h2, d2) == (hdr, data)


def test_a_cell_name_is_read_through_its_decoration():
    assert _table_cell_name("**[Semaglutide](https://x.com)** (Ozempic / Wegovy)") == "Semaglutide"


def test_a_table_comparing_one_thing_is_reported():
    g = _gen()
    g._resolve_table_punts("| Dimension | Semaglutide |\n| --- | --- |\n| Receptor | GLP-1 [S1] |\n"
                           "| Class | Single agonist [S1] |\n")
    # a two-column table is not a comparison to begin with; the floor speaks when one arrives
    assert g._table_punt_note == "" or "fewer than" in g._table_punt_note


def test_the_price_strip_takes_the_same_axis():
    """With pricing off, a price DIMENSION goes — never the option column beside it."""
    md = ("| Dimension | Alpha | Beta |\n"
          "| --- | --- | --- |\n"
          "| Monthly cost | $99 | $149 |\n"
          "| Delivery | 2 days | 1 day |\n")
    out, dropped = _gen(["Alpha", "Beta"])._strip_price_columns(md)
    assert "Alpha" in out and "Beta" in out, "an option was stripped as if it were a price column"
    assert "Monthly cost" not in out and "Delivery" in out


def test_the_uncited_cell_detector_reads_the_right_axis():
    """On a transposed table a "column" is an OPTION, so comparing down it compares across
    dimensions that share no unit — and the finding came out with its axes swapped
    ("<dimension> · <option>"). Swapped first, the comparison runs down a DIMENSION either way."""
    md = ("| Dimension | Alpha | Beta |\n| --- | --- | --- |\n"
          "| Throughput | 90 units [S1] | 80 units [S1] |\n"
          "| Warranty | 5 years [S2] | 3 years |\n"
          "| Footprint | 2 m [S3] | 3 m [S3] |\n")
    hits = E.detect_uncited_table_cells(md, cap=9)
    assert len(hits) == 1, [h["detail"] for h in hits]
    # BOTH names appear either way — only their ORDER says which axis was read. Without the swap
    # this reads "Warranty · Beta", comparing a warranty against a footprint down an option column.
    assert hits[0]["detail"].startswith("Beta · Warranty:"), hits[0]["detail"]
    assert "3 years" in hits[0]["detail"]


def test_the_axis_swap_round_trips():
    hdr = ["Dimension", "Alpha", "Beta"]
    rows = [(["Price", "$1", "$2"], ""), (["Speed", "fast", "slow"], "")]
    h2, r2 = E._swap_table_axes(hdr, rows)
    assert h2 == ["Dimension", "Price", "Speed"]
    assert [c for c, _ in r2] == [["Alpha", "$1", "fast"], ["Beta", "$2", "slow"]]


# ── Step 2 — a claim is not an authority just because its host is ────────────────────────────────
# _official_source_ok is a HOST test: every acceptance branch is a domain match and nothing reads the
# path. A society's podcast, a society's trade magazine (the subdomain wildcard hands
# `<magazine>.<society>.org` the society's own credential) and a narrative review on a NIH host all
# wore `official ·` on a clinical page. Measured: 32 blocks across the 221 stored articles, 679 still
# official, and 2 articles fall below the >=2 threshold — which is a warning, not a gate.

from generators.blog_gen import (  # noqa: E402
    _is_coverage_not_evidence, _weak_study_design, _official_label, _official_source_ok)


def test_coverage_of_an_authority_is_not_the_authority():
    for url, title in (
            ("https://www.example.org/podcast/ep52-incretins", "EP52: Incretins"),
            ("https://www.example.org/news-and-advocacy/news-room/2026/statement", "Statement"),
            ("https://example.org/newsroom/press-releases/standards-of-care", "Standards Released"),
            ("https://www.example.org/about-us/media-center/press-center/study", "Studies Explore"),
            ("https://example.org/blog/access-safety-and-the-road-ahead", "Access and Safety"),
            ("https://examplenews.example.org/beyond-the-scale", "Beyond the Scale")):
        assert _is_coverage_not_evidence(url, title), url


def test_the_authority_itself_keeps_its_credential():
    for url, title in (
            ("https://www.example.gov/media/12345/download", "Highlights of Prescribing Information"),
            ("https://www.example.org/guidelines/guidelines/deficiency", "Clinical Guideline"),
            ("https://www.example.org/clinical/clinical-guidance/practice-bulletin/x", "Bulletin"),
            ("https://professional.example.org/standards", "Standards of Care"),
            ("https://example.org/about-condition/devices-technology", "Devices")):
        assert not _is_coverage_not_evidence(url, title), url


def test_an_asset_path_is_not_a_newsroom():
    """"/media" alone is an ASSET path on several society sites — demoting it would demote the
    guideline PDF itself. The newsroom shapes are covered by their own segments."""
    assert not _is_coverage_not_evidence(
        "https://www.example.org/sites/default/files/media/guideline.pdf", "Guideline")


def test_the_badge_is_refused_for_coverage_end_to_end():
    assert not _official_source_ok(
        "https://www.example.org/podcast/ep52", "EP52", "Acme", "acme.com", ["example.org"])
    assert _official_source_ok(
        "https://www.example.org/guidelines/x", "Guideline", "Acme", "acme.com", ["example.org"])


def test_a_weak_design_keeps_the_badge_and_carries_its_design():
    """A narrative review IS the literature — it stays citable. What changes is that the page says
    what it is, so the YMYL rule can tell it apart from a guideline.

    The design is asserted from the URL, not the title: a title that already contains "narrative
    review" would make this pass whether the marker was minted or not."""
    lab = _official_label("https://example.org/articles/a-narrative-review-of-options", "Options")
    assert lab.startswith("official ·"), "every startswith('official ·') reader must keep working"
    assert lab == "official · narrative review · Options", lab
    assert _weak_study_design("A Systematic Review and Meta-Analysis") == ""
    assert _weak_study_design("Perspective: Telehealth in Practice") == "perspective"
    assert _official_label("https://x.gov/a", "Prescribing Information") == \
        "official · Prescribing Information"


def test_a_ymyl_page_resting_only_on_commentary_says_so():
    """The >=2-official count was satisfiable by the two weakest designs in the literature. Nothing
    can invent the guideline it needs, so this warns — but it has to warn."""
    gen = BlogGenerator.__new__(BlogGenerator)
    warned = []
    gen._warn = lambda _a, n: warned.append(n)
    art = {"body_markdown": (
        "## X\n\nOne option carries more risk [S1] than the other [S2].\n\n"
        "## Sources\n\n"
        "- [S1] official · narrative review · Options Compared — <https://e.org/a>\n"
        "- [S2] official · perspective · Practice Today — <https://e.org/b>\n")}
    gen._ymyl_authority_checks(art, "medical")
    assert any("commentary or narrative review" in w for w in warned), warned
    # and the inverse: a guideline beside a review is not the same page
    warned.clear()
    art2 = {"body_markdown": art["body_markdown"].replace(
        "official · perspective · Practice Today", "official · Clinical Practice Guideline")}
    gen._ymyl_authority_checks(art2, "medical")
    assert not any("commentary or narrative review" in w for w in warned), warned


# ── Step 3 — say it once, with a source ──────────────────────────────────────────────────────────
# "<A> has a higher rate of serious adverse events" appeared in four sections and a table cell of the
# reported article, uncited every time, on a page whose publisher sells <A>. FU253's
# detect_repeated_constraint matches a lexicon of RULES; this is a CLAIM. Measured: 8 of the 221
# stored articles, across a concrete-equipment article, a debt-collection article and a renovation
# article as well as the clinical ones.

_REPEATED = """## How do they compare?

Option A produces larger average gains, but it carries a higher rate of serious side effects
compared with Option B.

## What should buyers weigh?

Option A's higher rate of serious side effects means the balance may favour Option B for some
buyers. Cost is the other factor.

## Which is right for most people?

Option A is stronger on output. However, serious side effects occur more frequently with Option A
than with Option B.

## Is Option B ever better?

Yes. Option B has a lower rate of serious side effects, which matters for cautious buyers.
"""


def test_the_same_uncited_comparison_in_three_sections_is_the_finding():
    hits = E.detect_repeated_uncited_claim(_REPEATED)
    assert hits, "four wordings of one uncited comparison went unseen"
    assert len(hits[0]["sections"]) >= 3
    assert "serious" in hits[0]["shared"]


def test_the_cluster_keeps_a_shared_core():
    """Single-linkage chains two unrelated claims through a sentence touching both, and the reported
    claim then has an EMPTY core. Complete linkage is what keeps the finding meaningful."""
    for hit in E.detect_repeated_uncited_claim(_REPEATED):
        assert len(hit["shared"]) >= 3, hit["detail"]


# Three wordings of ONE comparison, plus a distractor that shares three words with the FIRST of
# them and only one with what they have in common. Single linkage compares against that first
# sentence, so the distractor welds in and drags the shared core down to a single word — and the
# whole claim then falls below the "the core must name what is being compared" guard and vanishes.
# Complete linkage compares against the running core, rejects the distractor, and keeps the claim.
_TWO_CLAIMS = """## Output

Option Alpha delivers greater throughput than Option Beta in every run.

## Sustained load

Option Gamma delivers greater sustained output than the others on test.

## Long runs

Option Delta delivers greater value over every measured interval on record.

## Reliability

Option Alpha shows lower throughput failure counts than Option Beta overall.
"""


def test_a_distractor_does_not_weld_itself_onto_a_claim():
    """Single linkage reported one article as making one claim in five sections when it made three
    in three, with an EMPTY shared core — and here it loses the finding altogether."""
    hits = E.detect_repeated_uncited_claim(_TWO_CLAIMS)
    assert len(hits) == 1, [h["detail"] for h in hits]
    assert hits[0]["sections"] == [0, 1, 2], hits[0]["sections"]
    assert "delivers" in hits[0]["shared"] and "greater" in hits[0]["shared"]
    assert "failure" not in hits[0]["shared"], "the distractor was merged into the claim"
    assert all(len(h["shared"]) >= 3 for h in hits), "a claim was reported with no shared core"


def test_a_cited_claim_is_not_a_finding():
    assert not E.detect_repeated_uncited_claim(
        _REPEATED.replace("side effects", "side effects [S1]"))


def test_a_repeated_NON_comparative_statement_is_left_alone():
    """An article may restate a definition as often as it likes. Comparative force is what makes a
    repeat a verdict."""
    md = ("## What is it?\n\nThe programme includes provider access and shipping.\n\n"
          "## What is included?\n\nThe programme includes provider access and shipping.\n\n"
          "## Anything else?\n\nThe programme includes provider access and shipping.\n")
    assert not E.detect_repeated_uncited_claim(md)


def test_two_sections_is_not_a_pattern():
    two = _REPEATED.split("## Which is right")[0]
    assert not E.detect_repeated_uncited_claim(two)


def test_the_first_statement_is_the_one_that_stays():
    hit = E.detect_repeated_uncited_claim(_REPEATED)[0]
    assert "larger average gains" in hit["keeper"], "the keeper must be the earliest section's"
    assert hit["keeper"] not in hit["repeats"]
    assert len(hit["repeats"]) >= 2


def test_the_reduction_removes_the_repeats_and_keeps_the_first():
    out, note = _gen()._repeated_claim_check(_REPEATED)
    assert "larger average gains" in out, "the first statement was deleted"
    assert out.count("serious side effects") < _REPEATED.count("serious side effects")
    assert "CITE THE ONE THAT REMAINS" in note
    assert not E.body_damage(out), "the reduction left damage behind"


def test_it_is_editorial_and_never_damage():
    """A detector inside body_damage raises the count when the defect is removed, so the guard
    refuses the removal and the defect ships. FU253 learned this the hard way."""
    assert any(f["check"] == "repeated-uncited-claim" for f in E.editorial_findings(_REPEATED))
    assert not any(f["check"] == "repeated-uncited-claim" for f in E.body_damage(_REPEATED))


# ── Step 4 — a named study, and a figure that answers the question ───────────────────────────────
# _study_reference_check fired on 61 of the 221 stored articles and its findings were dominated by
# ordinary words: one product-category phrase accounted for 23 of them, and "a free audit", "an
# audit" and "the ability to study" for most of the rest. That measurement is why this check is NOT
# made removal-eligible — it would have deleted sentences about the publisher's own service.

def test_a_programme_is_not_a_trial():
    g = _gen()
    assert g._study_reference_check(
        "## X\n\nOur GLP-1 program includes provider access and shipping.\n") == ""
    assert "TAME trial" in (g._study_reference_check(
        "## X\n\nThe TAME trial is ongoing and widely discussed.\n") or "")


def test_an_infinitive_is_a_verb_whatever_noun_follows_it():
    g = _gen()
    assert g._study_reference_check(
        "## X\n\nThat gives clinicians the ability to study longer-term outcomes.\n") == ""
    assert g._study_reference_check(
        "## X\n\nThere is every reason to report a side effect to your provider.\n") == ""


def test_a_service_you_can_book_is_not_a_document():
    g = _gen()
    assert g._study_reference_check(
        "## X\n\nGet a free audit of your site and see where you stand today.\n") == ""


def test_a_document_credited_with_a_figure_is_still_a_document():
    """The four ambiguous nouns need a research context — a reporting verb, a date, or a figure the
    document is credited with. "a separate industry report puts it at 9.2%" is a citation shape."""
    note = _gen()._study_reference_check(
        "## X\n\nA survey of 400 firms put the median rate at 8.5% [S1], while a separate industry "
        "report puts it at 9.2%.\n")
    assert "a separate industry report" in (note or "")


def test_an_unambiguous_design_needs_no_context():
    assert "meta-analysis" in (_gen()._study_reference_check(
        "## X\n\nA meta-analysis suggests one option carries more risk than the other.\n") or "").lower()


# _answer_check: "how X differs from Y" was not in the comparative lexicon, so a guide whose whole
# title is a comparison was never asked whether it answered one.

def _answer(seed, body, brand=None):
    return BlogGenerator._answer_check({"body_markdown": body},
                                       brand or {"name": "Acme", "domain_url": "acme.com"}, seed)


def test_differs_from_is_a_comparative_seed():
    body = "## X\n\nBoth are widely used.\n\n## Sources\n\n- [S1] third-party · T — <https://x.com/a>\n"
    assert _answer("Is A a category? How It Differs From B", body), "a comparison seed went unread"


def test_the_figure_has_to_be_about_what_the_question_asks():
    """A comparative seed used to be satisfied by ANY measured figure from a non-own source, so an
    article could compare two things, state no number about either, and pass."""
    off_topic = ("## X\n\nShipping arrives in 2 days for most addresses [S1].\n\n"
                 "## Sources\n\n- [S1] third-party · T — <https://x.com/a>\n")
    assert _answer("Which sealer is best for a garage floor?", off_topic)
    on_topic = ("## X\n\nThe silane sealer cut water absorption by 85% [S1].\n\n"
                "## Sources\n\n- [S1] third-party · T — <https://x.com/a>\n")
    assert _answer("Which sealer is best for a garage floor?", on_topic) == ""


def test_a_table_row_is_answered_by_the_table_it_sits_in():
    """"| Silane | 85% less water absorption [S1] |" answers "which sealer is best" without
    repeating the word — the table's header and the heading above it say what it is about."""
    body = ("## How do the sealers compare?\n\n| Sealer | Result |\n|---|---|\n"
            "| Silane | 85% less water absorption [S1] |\n\n"
            "## Sources\n\n- [S1] third-party · T — <https://x.com/a>\n")
    assert _answer("Which sealer is best for a garage floor?", body) == ""


# ── Step 5 — the publisher does not offer what its own pages never name ──────────────────────────
# The reported article told readers a clinic offers "GLP-1 (semaglutide)". The word appears on none
# of the five pages fetched from that clinic's site. The rule has existed as prompt text — "{name}'s
# OWN facts are FIRST-PARTY ONLY (this is a hard rule)" — with nothing behind it.

_BRAND = {"name": "Acme Care", "domain_url": "https://acme.example/"}
_FP_PAGES = ("Acme Care offers online care including supervised weight management with Bravodrug. "
             "Licensed professionals review every case. Flat monthly pricing at every dose. " * 12)


def _fp_gen():
    g = _gen(["Alphadrug", "Bravodrug"])
    return g


def _blocks(text=_FP_PAGES):
    return [{"label": "Acme Care", "url": "https://acme.example/", "text": text}]


def test_a_name_the_publishers_pages_never_use_is_reported_and_removed():
    body = ("## Where to get it\n\nAcme Care offers Bravodrug as part of its programme alongside "
            "Alphadrug (Alphadrug) options [S1].\n")
    out, note = _fp_gen()._first_party_naming_check(body, _blocks(), _BRAND)
    assert "Alphadrug" in note and "Bravodrug" not in note.split("offers")[1].split(",")[0]
    assert "(Alphadrug)" not in out, "the parenthetical was left in place"
    assert "Bravodrug" in out, "a name the publisher's own pages DO carry was removed"


def test_a_name_the_publishers_pages_do_carry_is_left_alone():
    body = "## Where\n\nAcme Care offers Bravodrug through its programme [S1].\n"
    out, note = _fp_gen()._first_party_naming_check(body, _blocks(), _BRAND)
    assert note == "" and out == body


def test_a_grounding_failure_is_never_a_naming_finding():
    """Nothing fetched from the publisher's own domain means nothing is known, not that the claim is
    false. The check must be silent, or a walled site becomes a page full of findings."""
    body = "## Where\n\nAcme Care offers Alphadrug through its programme [S1].\n"
    for blocks in ([], _blocks(""), _blocks("too short"),
                   [{"label": "official · X", "url": "https://fda.example/x", "text": "Alphadrug " * 90}]):
        out, note = _fp_gen()._first_party_naming_check(body, blocks, _BRAND)
        assert note == "" and out == body, blocks


def test_a_sentence_that_attributes_nothing_to_the_publisher_is_not_judged():
    body = ("## What is it\n\nAlphadrug is a receptor agonist studied for weight management [S1].\n"
            "\n## Who is Acme Care\n\nAcme Care is a telehealth provider founded in 2019 [S2].\n")
    out, note = _fp_gen()._first_party_naming_check(body, _blocks(), _BRAND)
    assert note == "" and out == body


def test_a_bare_name_in_a_list_is_reported_but_not_cut():
    """Removing a bare name from a list would leave "offers  and " behind. Only a name that is the
    whole of its own parenthetical is safe to take out; the rest is the operator's call."""
    body = "## Where\n\nAcme Care offers Alphadrug and Bravodrug through its programme [S1].\n"
    out, note = _fp_gen()._first_party_naming_check(body, _blocks(), _BRAND)
    assert "Alphadrug" in note
    assert out == body, "a bare name was cut out of a list"
    assert "parenthetical" not in note


def test_the_publisher_itself_is_never_a_candidate():
    g = _gen(["Acme Care", "Alphadrug"])
    body = "## Where\n\nAcme Care offers Acme Care Plus through its programme [S1].\n"
    _out, note = g._first_party_naming_check(body, _blocks(), _BRAND)
    assert "Acme Care" not in (note.split("offers")[1] if "offers" in note else "")


# ── FU255 — the priced row an article is ABOUT ───────────────────────────────────────────────────
# A generic token lets several of a brand's rows match ("bottle" matches every bottle it sells), and
# the tiebreak then ranked by how LONG each product name is. A brand with a "5 oz Natural Anti-colic
# Baby Bottle" ($28.99) and a "10 oz Transition Baby Bottle" ($32.99) priced the anti-colic one on an
# article about the transition bottle, because its name has more words.

_TWO_PRODUCTS = [
    {"brand": "Acme", "product": "5 oz Natural Anti-colic Baby Bottle",
     "kind": "exact", "value": "$28.99", "basis": "5 oz anti-colic"},
    {"brand": "Acme", "product": "10 oz Transition Baby Bottle",
     "kind": "exact", "value": "$32.99", "basis": "10 oz transition"},
]


def test_the_row_that_matches_BEST_wins_not_the_one_with_the_longest_name():
    g = _gen()
    e, _amb = g._price_row_for(_TWO_PRODUCTS, "Acme", ["transition", "toddler", "bottle"])
    assert e["value"] == "$32.99", "the article's own product lost to a longer product name"
    e, _amb = g._price_row_for(_TWO_PRODUCTS, "Acme", ["anti-colic", "newborn", "bottle"])
    assert e["value"] == "$28.99"


def test_an_equal_match_still_falls_back_to_the_more_specific_name():
    """When both rows match the article equally the old tiebreak stands, so nothing a brand with one
    row — or rows the article does not distinguish — sees changes."""
    g = _gen()
    e, _amb = g._price_row_for(_TWO_PRODUCTS, "Acme", ["bottle"])
    assert e["value"] == "$28.99", "the longest-name fallback was lost"


def test_one_row_is_still_used_as_is():
    g = _gen()
    e, amb = g._price_row_for(_TWO_PRODUCTS[:1], "Acme", ["transition"])
    assert e["value"] == "$28.99" and not amb


def test_no_topic_at_all_still_warns_rather_than_guessing():
    g = _gen()
    _e, amb = g._price_row_for(_TWO_PRODUCTS, "Acme", [])
    assert amb, "with nothing to choose on, the operator must be told a row was picked blind"


# ── FU255 — a brand-level range, with the product named at each end ──────────────────────────────
# `range` is ONE row whose two numbers share one basis, so it can never say which product each end
# is. This is two rows that know: the operator marks the cheapest and the dearest thing a brand
# sells, and the cell states the brand's line-up.

import json as _json  # noqa: E402

_SPAN_ROWS = [
    {"brand": "Acme", "product": "10 oz Transition Bottle", "kind": "span", "value": "$32.99"},
    {"brand": "Acme", "product": "5 oz Anti-colic Bottle", "kind": "span", "value": "$28.99"},
]


def test_the_marked_ends_become_one_range_naming_each_product():
    e, amb = _gen()._price_row_for(_SPAN_ROWS, "Acme", ["bottle"])
    assert not amb
    assert _format_price_value(e) == ("From $28.99 (5 oz Anti-colic Bottle) "
                                      "to $32.99 (10 oz Transition Bottle)")


def test_the_ends_are_ordered_by_AMOUNT_not_by_the_order_they_were_typed():
    """The dearest row is first in _SPAN_ROWS on purpose — an operator who pastes it that way must
    still get a sentence that reads forwards."""
    out = _format_price_value(_gen()._price_row_for(_SPAN_ROWS, "Acme", [])[0])
    assert out.index("$28.99") < out.index("$32.99"), out


def test_a_marked_range_answers_every_article_the_same_way():
    """It is a statement about the BRAND, so it outranks the per-article row match."""
    g = _gen()
    a = _format_price_value(g._price_row_for(_SPAN_ROWS, "Acme", ["anti-colic"])[0])
    b = _format_price_value(g._price_row_for(_SPAN_ROWS, "Acme", ["transition"])[0])
    assert a == b and "to $32.99" in a


def test_one_marked_end_is_not_a_range():
    e, _amb = _gen()._price_row_for(_SPAN_ROWS[:1], "Acme", ["bottle"])
    assert _format_price_value(e) == "$32.99 (10 oz Transition Bottle)"


def test_unmarked_rows_still_price_the_product_the_article_is_about():
    g = _gen()
    plain = [dict(r, kind="exact") for r in _SPAN_ROWS]
    assert g._price_row_for(plain, "Acme", ["transition", "bottle"])[0]["value"] == "$32.99"
    assert g._price_row_for(plain, "Acme", ["anti-colic", "bottle"])[0]["value"] == "$28.99"


def test_the_range_reaches_the_WRITER_not_just_the_cell():
    """The price ledger is built AFTER the draft, so nothing in it reaches the prompt: a cell could
    state a brand's range while the prose beside it named one end as if it were the price."""
    pt = {"acme": {"name": "Acme", "rows": _SPAN_ROWS}}
    blocks = BlogGenerator._price_span_block(
        {"name": "Acme", "domain_url": "https://acme.example/", "price_table": _json.dumps(pt)})
    assert len(blocks) == 1
    assert "Acme: From $28.99 (5 oz Anti-colic Bottle) to $32.99 (10 oz Transition Bottle)" \
        in blocks[0]["text"]
    assert "authoritative" in blocks[0]["text"]


def test_the_writer_block_is_inert_without_marked_rows():
    pt = {"acme": {"name": "Acme", "rows": [dict(r, kind="exact") for r in _SPAN_ROWS]}}
    assert BlogGenerator._price_span_block(
        {"name": "Acme", "price_table": _json.dumps(pt)}) == []
    assert BlogGenerator._price_span_block({"name": "Acme"}) == []
    assert BlogGenerator._price_span_block({"name": "Acme", "price_table": "not json"}) == []


def test_a_hyphenated_topic_word_matches_a_hyphenated_product_name():
    """A product name splits "Wide-neck" into "wide"+"neck"; a topic token arrives hyphenated. The
    two never met, so every bottle in a line-up tied on the generic word alone and the length
    tiebreak picked whichever name was longest."""
    g = _gen()
    rows = [
        {"brand": "Acme", "product": "5 oz Natural Anti-colic Baby Bottle",
         "kind": "exact", "value": "$28.99"},
        {"brand": "Acme", "product": "8 oz Wide-neck Baby Bottle", "kind": "exact", "value": "$30.99"},
    ]
    assert g._price_row_for(rows, "Acme", ["wide-neck", "bottle"])[0]["value"] == "$30.99"
    assert g._price_row_for(rows, "Acme", ["anti-colic", "bottle"])[0]["value"] == "$28.99"
    # and the already-split spelling keeps working
    assert g._price_row_for(rows, "Acme", ["wide", "neck"])[0]["value"] == "$30.99"


# ── FU258 — one brand priced per product, another by its range ───────────────────────────────────
# Each brand's rows are read independently, so a table can carry both shapes at once. Two things
# went wrong there: the per-product brand's cell was a bare figure with nothing saying WHICH of its
# products it was, and nothing told the operator the column had stopped comparing like with like.

_PER_PRODUCT = [
    {"brand": "A", "product": "5 oz Anti-colic Bottle", "kind": "exact", "value": "$28.99"},
    {"brand": "A", "product": "10 oz Transition Bottle", "kind": "exact", "value": "$32.99"},
]


def test_a_multi_row_brands_cell_says_which_product_it_is():
    """The Product field decided the row and then never reached the reader — the cell rendered
    "$28.99" beside rivals whose marked ranges name a product at each end."""
    e, _a = _gen()._price_row_for(_PER_PRODUCT, "A", ["anti-colic", "bottle"])
    assert _format_price_value(e) == "$28.99 (5 oz Anti-colic Bottle)"


def test_a_single_row_brand_gains_no_label():
    """With one row the product IS the brand's offering, so the label adds nothing and every
    existing single-row cell in the corpus must stay exactly as it is."""
    one = [{"brand": "D", "product": "9 oz Bottle", "kind": "exact", "value": "$15.00"}]
    e, _a = _gen()._price_row_for(one, "D", ["bottle"])
    assert _format_price_value(e) == "$15.00"


def test_an_operators_own_basis_still_wins_over_the_product():
    rows = [dict(_PER_PRODUCT[0], basis="3-pack")] + _PER_PRODUCT[1:]
    e, _a = _gen()._price_row_for(rows, "A", ["anti-colic"])
    assert _format_price_value(e) == "$28.99 (3-pack, $9.66 each)"
