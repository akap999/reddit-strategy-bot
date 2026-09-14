"""FU200 — the operator's rejection, locked as tests.

The regenerated Osbornes article shipped a comparison with ELEVEN columns, 16 of
its 50 cells reading "No <x> found in public sources" (32%), and three columns a
majority of the field could not answer. Two causes, both mine:

  1. FU198's "honest cell" sentinel. It was ordinary non-empty text, so
     `_resolve_table_punts`' column-drop rule read it as FILLED and the one
     mechanism that deletes a dimension nobody can answer was disabled. The
     operator had already banned data-unavailable wording four times
     (FU47/FU49/FU138) — I should have named that conflict when I offered the
     option instead of carving an exception around it.

  2. Nothing capped the dimension count, and the reconcile actively said
     "KEEP them all, and add a dimension if the facts support a useful one".

The fixture is the REAL shipped table.
"""
import os
import re

from generators.blog_gen import BlogGenerator, _DIM_CAP
from tests.stubs import StubClaude

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "fu200_osbornes_table.md")
SENT_RE = re.compile(r"found in public sources", re.I)


def _table():
    with open(FIXTURE, encoding="utf-8") as fh:
        return fh.read()


def _cols(md):
    return [c.strip() for c in md.splitlines()[0].strip().strip("|").split("|")]


def _rows(md):
    return [l for l in md.splitlines() if l.startswith("|")][2:]


def _resolve(md):
    return BlogGenerator(StubClaude(), db=None)._resolve_table_punts(md)


# ── the fixture is the failure, verbatim ──────────────────────────────────────────────────────
def test_the_shipped_table_is_the_failure_the_operator_reported():
    md = _table()
    assert len(_cols(md)) == 11
    assert len(SENT_RE.findall(md)) == 16
    assert len(_rows(md)) == 5


# ── the fix, measured against it ──────────────────────────────────────────────────────────────
def test_every_sentinel_cell_is_stripped():
    assert SENT_RE.search(_resolve(_table())) is None


def test_the_table_comes_back_within_the_dimension_cap():
    out = _cols(_resolve(_table()))
    assert len(out) == _DIM_CAP + 1, "first column + at most _DIM_CAP dimensions"
    assert len(out) < 11


def test_no_row_is_lost_and_withers_still_appears():
    out = _resolve(_table())
    assert len(_rows(out)) == 5, "a thin row is never dropped by the punt resolver"
    assert "Withers" in out, "the worst-sourced firm (7/10 unsourced) survives"


def test_the_surviving_dimensions_are_the_best_evidenced_ones():
    out = _cols(_resolve(_table()))
    assert out[0].strip().lower() in ("firm", "firm ")
    for kept in ("Legal 500", "Hague", "Jurisdiction"):
        assert any(kept.lower() in c.lower() for c in out), kept
    # the three columns a majority of the field could not answer are gone
    for dropped in ("IAFL", "dual-qualified", "surrogacy"):
        assert not any(dropped.lower() in c.lower() for c in out), dropped


def test_the_operator_is_told_what_was_dropped():
    gen = BlogGenerator(StubClaude(), db=None)
    gen._resolve_table_punts(_table())
    note = gen._table_punt_note or ""
    assert "column" in note and "unsourced" in note


# ── the cap is a cap, not a floor ─────────────────────────────────────────────────────────────
def test_a_table_already_within_the_cap_is_untouched():
    md = ("| Firm | Ranking | Coverage |\n| --- | --- | --- |\n"
          "| A | Tier 1 | UK, US |\n| B | Tier 2 | UK, EU |\n| C | Tier 1 | Global |\n")
    assert _resolve(md) == md


def test_a_source_column_is_never_traded_away_for_a_dimension():
    """Sources are provenance, not a comparison dimension — they survive the trim."""
    dims = ["D%d" % i for i in range(1, _DIM_CAP + 3)]
    hdr = ["Firm"] + dims + ["Source"]
    rows = [["F%d" % r] + ["v%d" % i for i in range(len(dims))] + ["[S1]"] for r in range(3)]
    md = ("| " + " | ".join(hdr) + " |\n| " + " | ".join("---" for _ in hdr) + " |\n"
          + "".join("| " + " | ".join(r) + " |\n" for r in rows))
    out = _cols(_resolve(md))
    assert any("source" in c.lower() for c in out)
    assert len(out) <= _DIM_CAP + 2  # name + dims + the exempt Source column


# ── Change 3: one page cannot occupy two source numbers ───────────────────────────────────────
def _rebuild(blocks, body):
    gen = BlogGenerator(StubClaude(), db=None)
    gen._evidence_blocks = blocks
    return gen._rebuild_sources(body)


def test_two_evidence_blocks_sharing_a_url_collapse_to_one_marker():
    url = "https://chambers.com/law-firm/dawson-cornwell-uk-1:109"
    blocks = [
        {"label": "Chambers", "url": url, "text": "ranked"},
        {"label": "Legal 500", "url": "https://legal500.com/firms/877/", "text": "tier 1"},
        {"label": "Chambers again", "url": url + "?utm=x", "text": "same page"},
    ]
    out = _rebuild(blocks, "Alpha [S1]. Beta [S2]. Gamma [S3].\n")
    assert out.count(url.split("?")[0]) == 1, "the same page must not occupy two numbers"
    assert "[S3]" not in out
    assert "[S1]" in out and "[S2]" in out


def test_distinct_urls_keep_distinct_markers():
    blocks = [
        {"label": "A", "url": "https://a.example/one", "text": "x"},
        {"label": "B", "url": "https://b.example/two", "text": "y"},
    ]
    out = _rebuild(blocks, "One [S1]. Two [S2].\n")
    assert "[S1]" in out and "[S2]" in out
    assert "a.example/one" in out and "b.example/two" in out
