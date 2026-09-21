"""FU242 — the statistics that carry an article were invisible, and a sourced article can still be
out of date.

An insurance-coverage article published September 2026 opened with "among employers with 1,000 or
more workers, roughly 1 in 4 (25%) covers GLP-1 prescriptions". The KFF page it cites is readable,
19,480 characters, and contains no "25%" anywhere — it says 19% of firms with 200+ workers and 43%
of firms with 5,000+, up from 28% in 2024. Every check shipped in the three previous rounds stayed
silent, because `_LOADBEARING_NUM_RE` deliberately skips a bare-integer percent: FU174 added that
exclusion after the Qwen rewrite gate kept failing on a rhetorical "100% online". Right for the
rewrite gate, wrong for a claim check, and the same regex served both.

The same article described Medicare, Medicaid and cash prices as they stood in 2024, and omitted the
Medicare GLP-1 Bridge — $50/month, live since July 2026, three months before it published. Nothing
was fabricated and nothing was mis-cited; the sources say exactly what the article says they say.
They are simply old, and no check in the system could see that.

$0, no network.
"""
import time

import pytest

from generators.blog_gen import (BlogGenerator, _CLAIM_NUM_RE, _LOADBEARING_NUM_RE, _asof_label)
from tests.stubs import StubClaude

NOW = time.strptime("2026-09-21", "%Y-%m-%d")


def _gen():
    return BlogGenerator(StubClaude(), None)


# ── 1. the figures a claim check must see ────────────────────────────────────────────────────────
@pytest.mark.parametrize("text", ["25% of employers", "19% of firms", "43% of large firms",
                                  "13 state Medicaid programs", "592,624 additional fills"])
def test_the_statistics_this_article_turns_on_are_now_checkable(text):
    assert _CLAIM_NUM_RE.search(text), text


@pytest.mark.parametrize("text", ["25% of employers", "13 state Medicaid programs",
                                  "592,624 additional fills"])
def test_and_were_invisible_before(text):
    """The premise. Every one of these passed every figure check silently."""
    assert not _LOADBEARING_NUM_RE.search(text), text


def test_the_rewrite_gate_is_deliberately_left_alone():
    """FU174: the Qwen gate must keep ignoring a rhetorical percent, or rewrites fail and the
    watermarked body ships. Two consumers, two regexes — that is the whole point."""
    assert not _LOADBEARING_NUM_RE.search("100% online")
    assert _LOADBEARING_NUM_RE.search("2.5 mg") and _LOADBEARING_NUM_RE.search("$149")


@pytest.mark.parametrize("text", ["covers GLP-1 prescriptions", "COVID-19 patients",
                                  "Wegovy 2.4 mg pen"])
def test_a_digit_inside_a_product_name_does_not_become_a_statistic(text):
    """"GLP-1 prescriptions" produced the atom "1 prescriptions" on the first run of this."""
    assert "prescriptions" not in " ".join(m.group(0) for m in _CLAIM_NUM_RE.finditer(text))
    assert "patients" not in " ".join(m.group(0) for m in _CLAIM_NUM_RE.finditer(text))


KFF = ("One-in-five (19%) firms with 200 or more workers, including 43% of firms with 5,000 or more "
       "workers, cover GLP-1 drugs for weight loss in their largest health plan in 2025, up from 28% "
       "in 2024. Employers cite cost as the main barrier to covering these medications. ") * 4
BLOCKS = [{"label": "third-party · Peterson-KFF", "url": "https://x/kff", "text": KFF}]


def test_the_employer_figure_that_is_not_on_the_page_is_removed():
    body = ("Among employers with 1,000 or more workers, roughly 1 in 4 (25%) covers GLP-1 "
            "coverage when used primarily for weight loss [S1].\n")
    out, note = _gen()._unsourced_figure_check(body, BLOCKS)
    assert "25%" not in out
    assert "unsourced-figures" in note


def test_the_figure_that_IS_on_the_page_survives():
    body = "One in five (19%) firms with 200 or more workers cover GLP-1 drugs [S1].\n"
    out, note = _gen()._unsourced_figure_check(body, BLOCKS)
    assert "19%" in out and not note


# ── 2. an article whose state-of-the-world claims have aged ──────────────────────────────────────
def test_the_real_stale_claim_is_flagged():
    body = "As of August 2024, 13 state Medicaid programs cover GLP-1s for obesity [S11].\n"
    note = _gen()._staleness_check(body, NOW)
    assert "staleness" in note and "25 months" in note


def test_a_dated_historical_event_is_not_stale():
    """"In March 2024 the FDA approved…" is history. Flagging it would make the check noise, and
    noise is how a warning gets ignored."""
    body = "In March 2024, the FDA approved Wegovy for cardiovascular risk reduction [S8].\n"
    assert not _gen()._staleness_check(body, NOW)


def test_a_current_as_of_claim_is_not_flagged():
    body = "As of August 2026, the Medicare GLP-1 Bridge charges $50 per month [S2].\n"
    assert not _gen()._staleness_check(body, NOW)


def test_an_undated_claim_is_not_flagged():
    assert not _gen()._staleness_check("Coverage varies by payer and plan [S1].\n", NOW)


def test_the_oldest_claim_is_the_one_reported():
    body = ("As of March 2025, some plans cover it [S1]. As of August 2024, 13 states do [S2].\n")
    note = _gen()._staleness_check(body, NOW)
    assert "August 2024" in note


def test_a_year_only_as_of_is_read_conservatively():
    """"as of 2025" is treated as December 2025 — the latest it could mean — so the check never
    overstates how old a claim is."""
    assert not _gen()._staleness_check("As of 2025, coverage was limited [S1].\n", NOW)
    assert _gen()._staleness_check("As of 2024, coverage was limited [S1].\n", NOW)


def test_the_sources_list_is_not_scanned():
    body = "A claim [S1].\n\n## Sources\n\n- [S1] A brief as of August 2024 — https://x/kff\n"
    assert not _gen()._staleness_check(body, NOW)


def test_the_brief_asks_for_the_current_position():
    assert _asof_label(NOW) == "September 2026"
