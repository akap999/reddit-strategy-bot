"""FU229 — a cited claim must be ON its cited page, and should cite the best page that states it.

`search_sources` asks the model for "one specific, sourced fact this page SUPPORTS" and never fetches
the page, so an `official ·` / `third-party ·` block carries a model-written one-liner rather than
anything the page states -- and nothing downstream checked it. On the article that exposed this, three
ClinicalTrials.gov protocol PDFs were cited for a half-life the prescribing information in the SAME
source list states three times.

Two deterministic passes, no model call and no network. Page texts below are real excerpts.
"""
import pytest

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

def _page(text):
    """A real fetched page is thousands of characters; these fixtures are excerpts, so pad them to
    page length. The filler carries no figures of its own."""
    return text + " " + ("Additional background detail follows in the full document. " * 12)


LABEL = ("official · FDA Prescribing Information – ZEPBOUND (tirzepatide)",
         "https://www.accessdata.fda.gov/drugsatfda_docs/label/2026/217806s042lbl.pdf",
         "A period of observation may be necessary, taking into account the half-life of tirzepatide "
         "of approximately 5 days. The elimination half-life is approximately 5-6 days.")
PROTO_A = ("third-party · ClinicalTrials.gov Protocol — NCT04004988",
           "https://cdn.clinicaltrials.gov/large-docs/88/NCT04004988/Prot_000.pdf",
           "Washout is based on tirzepatide's elimination half-life of approximately 5 days.")
PROTO_B = ("third-party · ClinicalTrials.gov Protocol — NCT04050553",
           "https://cdn.clinicaltrials.gov/large-docs/53/NCT04050553/Prot_000.pdf",
           "To ensure sufficient washout of the study drug based on tirzepatide's elimination "
           "half-life of approximately 5 days.")
COHORT = ("third-party · Comparative effectiveness of tirzepatide and semaglutide — PMC",
          "https://pmc.ncbi.nlm.nih.gov/articles/PMC12924827/",
          "A 6-month retrospective cohort of adults with obesity treated in routine practice.")
TRIAL = ("third-party · Tirzepatide as Compared with Semaglutide (SURMOUNT-5) — NEJM",
         "https://pubmed.ncbi.nlm.nih.gov/40353578/",
         "Mean weight reduction was 20.2% with tirzepatide and 13.7% with semaglutide at week 72.")


def _gen(*pages, pad=True):
    g = BlogGenerator(StubClaude(), None)
    g._evidence_blocks = [{"label": l, "url": u, "text": _page(t) if pad else t}
                          for l, u, t in pages]
    return g


def _run(g, body, brand=None):
    """Finalize's real order: check the claims on RAW indices, then render the sources."""
    fixed, note = g._claim_source_check(body, g._evidence_blocks, brand or {"name": "PeterMD"})
    return g._rebuild_sources(fixed), note


def _prose(out):
    return out.split("## Sources")[0]


# ---------------------------------------------- the shipped case
def test_the_prescribing_information_takes_the_half_life_cell():
    """The shipped article cited three trial protocols for a half-life the label states three times.
    In a CELL the column header is the whole claim, so the most authoritative page that states the
    figure is the right source."""
    g = _gen(LABEL, PROTO_A, PROTO_B)
    out, note = _run(g, "## X\n\n| Feature | Tirzepatide |\n| --- | --- |\n"
                        "| Half-life | ~5 days [S2][S3] |\n")
    row = [l for l in out.splitlines() if "Half-life" in l][0]
    assert row.count("[S") == 1 and "[S1]" in row
    assert row.startswith("| Half-life |") and row.count("|") == 3   # the row survives intact
    assert "217806s042lbl.pdf" in out
    assert "NCT04004988" not in out and "NCT04050553" not in out     # uncited now, so unlisted
    assert "authoritative" in note


def test_in_PROSE_a_better_source_is_only_ADVISED():
    """A sentence qualifies its claim in ways a figure match cannot see. Measured on the real
    article, preferring the regulator's label by authority alone MIS-CITED a trial outcome: the
    label states "72 weeks" but never mentions the trial or its secondary endpoints."""
    trial_news = ("third-party · Tirzepatide Bests Semaglutide — news", "https://news.example/x",
                  "Tirzepatide was superior for all key secondary endpoints at 72 weeks.")
    label72 = ("official · FDA label", "https://x.gov/label/2026/1lbl.pdf",
               "Patients were treated for 72 weeks. The half-life is approximately 5 days.")
    g = _gen(trial_news, label72)
    body = "## X\n\nIt was superior for all key secondary endpoints at 72 weeks [S1].\n"
    out, note = _run(g, body)
    assert "news.example/x" in out                      # the citation did NOT move
    assert "consider citing" in note                    # the operator is told, and decides


# ---------------------------------------------- pass 1: wrong source
def test_a_citation_its_page_does_not_support_is_repointed():
    """The cohort study states none of the trial's numbers; the trial states both."""
    g = _gen(COHORT, TRIAL)
    out, note = _run(g, "## X\n\nTirzepatide reached 20.2% versus 13.7% at 72 weeks [S1].\n")
    assert "re-pointed" in note
    assert "pubmed.ncbi.nlm.nih.gov/40353578" in out      # the trial now carries the citation
    assert "PMC12924827" not in out                       # the cohort is no longer cited or listed
    # honest limit, pinned: the page writes "at week 72", the body "72 weeks" -- a figure phrased
    # differently is REPORTED, never silently re-pointed
    assert "no gathered source states 72 weeks" in note


def test_a_figure_no_gathered_source_states_is_reported_not_moved():
    g = _gen(COHORT, TRIAL)
    out, note = _run(g, "## X\n\nThe dose escalates to 15 mg [S1].\n")
    assert "[S1]" in _prose(out)              # left exactly as the writer cited it
    assert "no gathered source states" in note and "15 mg" in note


# ---------------------------------------------- negative controls
def test_the_best_source_already_cited_is_left_alone():
    g = _gen(LABEL, PROTO_A)
    out, note = _run(g, "## X\n\nHalf-life is approximately 5 days [S1].\n")
    assert "[S1]" in _prose(out) and "[S2]" not in _prose(out)
    assert not note


def test_a_sentence_with_no_figure_is_untouched():
    g = _gen(COHORT, TRIAL)
    body = "## X\n\nTirzepatide is a dual agonist rather than a selective one [S1].\n"
    out, note = _run(g, body)
    assert "[S1]" in _prose(out) and not note


def test_two_figures_from_two_pages_keep_both_citations():
    a = ("official · A", "https://a.gov/x", "The terminal half-life is approximately 5 days.")
    b = ("official · B", "https://b.gov/y", "Mean reduction was 20.2% at week 72.")
    g = _gen(a, b)
    out, _ = _run(g, "## X\n\nIt runs 5 days and reached 20.2% [S1][S2].\n")
    assert "[S1]" in _prose(out) and "[S2]" in _prose(out)


def test_a_brands_own_page_keeps_its_own_figure():
    """An official source that never states the price must not take a first-party price citation."""
    own = ("PeterMD", "https://getpetermd.com/product/tirzepatide/", "Tirzepatide from $149 per month.")
    g = _gen(LABEL, own)
    out, note = _run(g, "## X\n\nPeterMD lists tirzepatide at $149 per month [S2].\n",
                     brand={"name": "PeterMD", "domain_url": "https://getpetermd.com"})
    assert "getpetermd.com/product/tirzepatide/" in out
    assert "$149" in _prose(out) and not note


def test_a_nearby_number_is_not_mistaken_for_the_figure():
    near = ("official · Near", "https://n.gov/x", "The study ran for 15 days and enrolled 5 sites.")
    real = ("third-party · Real", "https://r.com/y", "The terminal half-life is approximately 5 days.")
    g = _gen(near, real)
    out, note = _run(g, "## X\n\nHalf-life is approximately 5 days [S2].\n")
    cited = _prose(out)
    listed = [l for l in out.splitlines() if l.startswith("- [S")]
    n = [l for l in listed if "r.com/y" in l][0].split("]")[0].lstrip("- [S")
    assert f"[S{n}]" in cited        # "15 days" / "5 sites" never satisfy "5 days"
    assert not note


# ---------------------------------------------- mechanics
def test_running_it_twice_changes_nothing_more():
    g = _gen(LABEL, PROTO_A, PROTO_B)
    cell = "## X\n\n| F | T |\n| --- | --- |\n| Half-life | ~5 days [S2][S3] |\n"
    once, _ = _run(g, cell)
    twice, note = _run(g, once)
    assert twice == once and not note


def test_the_kill_switch_makes_it_inert(monkeypatch):
    monkeypatch.setattr(BlogGenerator, "_CLAIM_CHECK_ON", False)
    g = _gen(LABEL, PROTO_A, PROTO_B)
    body = "## X\n\nHalf-life is approximately 5 days [S2][S3].\n"
    assert g._claim_source_check(body, g._evidence_blocks, {"name": "X"}) == (body, "")


def test_figure_matching_tolerates_how_a_page_writes_it():
    ok = BlogGenerator._atom_in
    assert ok("5 days", "a half-life of approximately 5 days.")
    assert ok("5 days", "the 5-day washout period")            # hyphenated, singular
    assert ok("$1,000", "priced at $1000 per course")          # thousands separator
    assert not ok("5 days", "the elimination half-life is 5-6 days")
    assert not ok("5 days", "the study ran 15 days")


def test_a_one_line_summary_block_is_never_judged():
    """Most official/third-party blocks carry the one-liner `search_sources` wrote, not the page. The
    absence of a figure from one sentence says nothing about the page, so such a citation is left
    alone -- otherwise a body whose sources were never fetched would have most of its citations
    rewritten on no evidence."""
    summary = ("third-party · SURMOUNT-5 coverage", "https://news.example/x",
               "A head-to-head trial found tirzepatide superior to semaglutide.")
    page = ("official · FDA label", "https://x.gov/label/2026/1lbl.pdf",
            "Patients were treated for 72 weeks. " + ("Clinical detail. " * 40))
    g = _gen(summary, pad=False)
    g._evidence_blocks.append({"label": page[0], "url": page[1], "text": _page(page[2])})
    out, note = _run(g, "## X\n\nIt was superior for all key endpoints at 72 weeks [S1].\n")
    assert "news.example/x" in out and not note      # untouched: we cannot judge a summary


def test_a_cited_page_is_read_when_the_block_is_only_a_summary(monkeypatch):
    """The real defect this exists for: a review states "HbA1c was reduced in SURPASS 1-5 by between
    1.69 to 2.58%" and the body claimed "1.24-2.58%". The block held only the one-liner
    `search_sources` wrote, so there was nothing to check the figure against. Read the page."""
    import generators.blog_gen as BG
    page = ("HbA1c was reduced in SURPASS 1-5 by between 1.69 to 2.58% and body weight by 5.4 to "
            "11.7 kg across the 5 to 15 mg doses. " + ("Further discussion follows. " * 20))
    calls = []

    def _read(url, claude=None, cache=None, max_chars=20000):
        calls.append(url)
        return page, "web fetch"

    monkeypatch.setattr(BG._research, "read_page", _read)
    review = ("reference · Tirzepatide, a dual GIP/GLP-1 receptor co-agonist",
              "https://link.springer.com/article/10.1186/s12933-022-01604-7",
              "A review of the SURPASS programme.")          # the one-liner, far below page length
    g = _gen(review, pad=False)
    out, note = _run(g, "## X\n\nTirzepatide reduced HbA1c by 1.24-2.58% across SURPASS [S1].\n")
    assert calls == [review[1]]                               # the cited page was read, once
    assert "no gathered source states" in note and "1.24" in note
    assert "2.58%" not in note                                # the bound that IS on the page passes
    assert "[S1]" in _prose(out)                              # nothing moved: we only report


def test_a_page_is_not_read_for_a_claim_with_no_figure(monkeypatch):
    import generators.blog_gen as BG
    calls = []
    monkeypatch.setattr(BG._research, "read_page",
                        lambda url, **kw: (calls.append(url), ("text " * 200, "direct"))[1])
    g = _gen(("reference · R", "https://r.example/x", "A short summary."), pad=False)
    _run(g, "## X\n\nIt is a dual agonist rather than a selective one [S1].\n")
    assert calls == []
