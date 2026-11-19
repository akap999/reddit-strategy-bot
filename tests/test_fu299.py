"""FU299 — 40% of the budget was reserved for a leg that cannot run.

Live Search was returning 10-30 where the backend had 25-32 to give, and every search ended
the same way:

    (arctic 40 sub×term queries) ✓ (+36 passed filters out of 36 raw, 100% pass rate)
    ⏱ leg time budget reached — returning 36 result(s) gathered so far

Cut mid-yield, with nothing being rejected. The cause is the FU116 split: when a reddit-wide
top-up is coming, the named-sub legs may spend at most 60% of the budget so the top-up "always
gets a real window instead of leftovers". Sound when the top-up can produce.

It cannot right now. Its subreddit discovery calls /subreddits/search.json, which 404s and is
latched off (FU297), and a global Arctic search returns [] without a subreddit. So the top-up
returns 0 whatever window it gets, and the reserve only shortens the one leg that works —
Arctic gets 24s of a 40s budget and is truncated at a 100% pass rate.

The reserve is now conditional on discovery being alive, and comes back by itself when the
endpoint does.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.reddit_bot import RedditSearchBot  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _clean():
    RedditSearchBot._subsearch_gone_until = 0.0
    yield
    RedditSearchBot._subsearch_gone_until = 0.0


def _leg_window(discovery_down, monkeypatch):
    """Return the leg window the search would grant, by driving the real branch."""
    seen = {}
    bot = RedditSearchBot()
    RedditSearchBot._subsearch_gone_until = (time.time() + 600) if discovery_down else 0.0

    # Stop the search immediately after the deadlines are computed.
    class _Stop(Exception):
        pass

    def _spy(*a, **k):
        raise _Stop()

    monkeypatch.setattr(bot, "_search_reddit_rss", _spy)
    monkeypatch.setattr(bot, "_search_reddit_native", _spy)
    monkeypatch.setattr(bot, "_search_arctic", _spy)
    monkeypatch.setattr(bot, "_search_pullpush", _spy)
    t0 = time.time()
    try:
        bot.search("mattress", subreddit="Mattress", subreddits=["Mattress"],
                   limit=50, reddit_wide=True, api="auto")
    except Exception:
        pass
    seen["elapsed"] = time.time() - t0
    return seen


def test_the_reserve_is_skipped_while_discovery_is_down():
    """The branch is a one-line difference, so assert on the source as well as behaviour —
    the window itself is wall-clock and not directly observable from outside."""
    src = open(os.path.join(ROOT, "search", "reddit_bot.py"), encoding="utf-8").read()
    i = src.index("if (_deadline is None and reddit_wide and subreddit_path")
    block = src[i:i + 1400]
    assert "if time.time() < RedditSearchBot._subsearch_gone_until:" in block
    assert "leg_deadline = time.time() + _budget * 0.6" in block, "the reserve is gone entirely"
    # the reserve must be in the ELSE, i.e. only when discovery is alive
    assert block.index("_subsearch_gone_until") < block.index("_budget * 0.6")


def test_the_reserve_returns_when_discovery_recovers():
    """Self-healing: a latch that expires must restore the original split, not leave the
    top-up permanently starved once the endpoint is back."""
    RedditSearchBot._subsearch_gone_until = time.time() - 1      # expired
    src = open(os.path.join(ROOT, "search", "reddit_bot.py"), encoding="utf-8").read()
    assert "time.time() < RedditSearchBot._subsearch_gone_until" in src, \
        "the condition is not time-based, so it cannot recover"


def test_discovery_latch_drives_the_decision_not_a_new_flag():
    """Reusing FU297's latch means the two cannot disagree about whether discovery works."""
    src = open(os.path.join(ROOT, "search", "reddit_bot.py"), encoding="utf-8").read()
    i = src.index("if (_deadline is None and reddit_wide and subreddit_path")
    assert "_subsearch_gone_until" in src[i:i + 1400]


def test_the_full_budget_branch_is_announced():
    """A silent budget change is invisible in the logs that diagnosed this in the first place."""
    src = open(os.path.join(ROOT, "search", "reddit_bot.py"), encoding="utf-8").read()
    assert "giving the named-sub leg the FULL" in src


def test_search_still_runs_both_ways(monkeypatch):
    """Whatever the branch picks, the search must still execute rather than raise."""
    for down in (True, False):
        out = _leg_window(down, monkeypatch)
        assert out["elapsed"] < 30, "the search hung while computing its budget"
