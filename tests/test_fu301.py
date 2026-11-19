"""FU301 — the Reddit-wide top-up kept spending budget it could not use.

FU300 lifted the sub×term cap and the log confirmed it: `(arctic 180 sub×term queries)`.
Results barely moved, and the same log shows where the time went:

    (arctic 180 sub×term queries) ✓ (+26 passed filters out of 26 raw, 100% pass rate)
    ⏱ leg time budget reached — returning 26 result(s) gathered so far
    (arctic 60 sub×term queries) No new results (0 raw, all filtered out): no posts in batch

Sixty more Arctic queries, zero results. That is the Reddit-wide top-up. With subreddit
discovery down (FU297) it cannot reach any sub that was not already searched: a global
Arctic query returns [] without a subreddit, so it falls back to the subs discovered from
the results it already has, re-runs the same grid, and everything it finds is a duplicate.

FU299 stopped it RESERVING 40% of the budget. It was still spending the budget by running.
Now it is skipped outright while discovery is down, and comes back by itself when the
endpoint does — same latch, same self-healing, as FU299.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.reddit_bot import RedditSearchBot  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(ROOT, "search", "reddit_bot.py"), encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _clean():
    RedditSearchBot._subsearch_gone_until = 0.0
    yield
    RedditSearchBot._subsearch_gone_until = 0.0


def _topup_runs(discovery_down):
    """Evaluate the REAL guard from the source, with everything else held favourable.

    Lifted from the file rather than restated here: a restated condition would keep passing
    if the new term were deleted from the code, which is the whole regression.
    """
    src = _src()
    i = src.index("if (reddit_wide and subreddit_path and api == \"auto\" and len(result) < limit\n"
                  "                and _topup_possible and time.time() < search_deadline):")
    cond = src[i + len("if ("):src.index("):", i)]
    cond = " ".join(line.strip() for line in cond.splitlines())

    RedditSearchBot._subsearch_gone_until = (time.time() + 600) if discovery_down else 0.0
    scope = {
        "RedditSearchBot": RedditSearchBot, "time": time,
        "reddit_wide": True, "subreddit_path": "Mattress", "api": "auto",
        "result": [], "limit": 50, "search_deadline": time.time() + 999,
        # computed by the line immediately above the guard in the source
        "_topup_possible": time.time() >= RedditSearchBot._subsearch_gone_until,
    }
    return bool(eval(cond, {"time": time}, scope))


def test_the_topup_is_skipped_while_discovery_is_down():
    assert _topup_runs(discovery_down=True) is False


def test_the_topup_still_runs_when_discovery_works():
    """It is a real feature — with discovery alive it reaches subs we never named."""
    assert _topup_runs(discovery_down=False) is True


def test_it_reuses_the_discovery_latch_not_a_new_flag():
    """Two sources of truth about whether discovery works could disagree."""
    src = _src()
    assert "_topup_possible = time.time() >= RedditSearchBot._subsearch_gone_until" in src


def test_the_decision_is_time_based_so_it_recovers():
    src = _src()
    i = src.index("_topup_possible =")
    assert "time.time() >=" in src[i:i + 120], "not time-based — it could never come back"


def test_the_skip_is_logged():
    """A silently skipped leg is invisible in the logs that diagnosed this."""
    assert "skipping the Reddit-wide top-up" in _src()


def test_the_guard_still_has_its_original_conditions():
    """Adding a condition must not drop the ones that were already there."""
    src = _src()
    i = src.index("and _topup_possible and time.time() < search_deadline):")
    guard = src[src.rindex("if (reddit_wide", 0, i):i + 80]
    for cond in ("reddit_wide", "subreddit_path", 'api == "auto"', "len(result) < limit",
                 "search_deadline"):
        assert cond in guard, f"lost the {cond!r} condition"
