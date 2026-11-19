"""FU302 — concurrency, not coverage, was the missing throughput.

Measured across real runs in the production log:

    keywords  pairs  raw  budget hit?  subs in results
    5         135    156  NO           10
    15        180     22  yes           3
    15        180     30  yes           7
    10        180     11  yes           1
    10        180     37  yes           3

The single search that FIT INSIDE the budget returned 156. Every search that scheduled 180
pairs was cut at 11-37. The cap FU300 raised is now writing cheques the budget cannot cash:
180 pairs at concurrency 2 does not finish in 120s.

That concurrency is from FU294, which dropped it 4 → 2 because a throttle then meant a
300-second leg-wide blackout — provoking one was catastrophic. FU294/FU295 changed that: a
throttle now backs off 0.75/1.5/3s, clipped to the remaining budget, and only a SUSTAINED
one trips the breaker. So the trade has inverted — being throttled costs a few seconds, and
not being throttled costs most of the result set.

The thread pool and the semaphore now read the same constant. They were set independently
(pool 4, semaphore 2) and the smaller silently won, which is the kind of disagreement that
makes a concurrency change look like it did nothing.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.reddit_bot import RedditSearchBot  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(ROOT, "search", "reddit_bot.py"), encoding="utf-8").read()


def test_concurrency_is_raised():
    assert RedditSearchBot._ARCTIC_CONCURRENCY == 4
    assert RedditSearchBot._arctic_sem._value == RedditSearchBot._ARCTIC_CONCURRENCY


def test_the_pool_and_the_semaphore_read_the_same_constant():
    """Set independently they disagreed — pool 4, semaphore 2 — and the smaller one won
    silently, so raising either alone would have changed nothing."""
    src = _src()
    assert "_arctic_sem = threading.Semaphore(_ARCTIC_CONCURRENCY)" in src
    assert "RedditSearchBot._ARCTIC_CONCURRENCY)) as ex:" in src
    assert "ThreadPoolExecutor(max_workers=min(len(pairs), 4))" not in src, \
        "the pool still hardcodes its own width"


def test_the_throttle_handling_that_justifies_this_is_still_present():
    """Raising concurrency is only safe because a throttle is now cheap. If the backoff or
    the strike-based breaker were removed, this would be back to FU294's blackout."""
    src = _src()
    assert "_arctic_err_kind" in src, "the rate-limit/outage split is gone"
    assert "_ARCTIC_PAGE_TRIES" in src, "the per-page retry is gone"
    assert "_ARCTIC_RL_STRIKES_MAX" in src, "the consecutive-strike breaker is gone"
    # attribute, not substring: "_ARCTIC_MIN_RETRY_LEFTX" contains "_ARCTIC_MIN_RETRY_LEFT"
    assert isinstance(getattr(RedditSearchBot, "_ARCTIC_MIN_RETRY_LEFT", None), float), \
        "the budget-aware backoff clip is gone"


def test_a_throttle_is_still_not_an_outage():
    """The specific guarantee that makes higher concurrency affordable."""
    assert RedditSearchBot._arctic_err_kind("Too many requests") == "ratelimit"
    assert RedditSearchBot._arctic_err_kind(
        "HTTPError: 422 Client Error: Unprocessable Entity") == "ratelimit"
    assert RedditSearchBot._arctic_err_kind("under maintenance") == "outage"


def test_concurrency_stays_bounded():
    """Unbounded would simply move the throttle from occasional to constant."""
    assert 1 <= RedditSearchBot._ARCTIC_CONCURRENCY <= 6
