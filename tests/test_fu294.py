"""FU294 — "slow down" was being treated as "stop", on the only leg still working.

Live Search returned 0 results for every search. Three of the four legs are down externally,
confirmed from a residential IP against the real bot methods:

    PullPush              HTTP 521   Cloudflare "origin down" — service outage
    Reddit native JSON    HTTP 403   both hosts, all 3 UA variants — walled
    Reddit search.rss     HTTP 404   a valid XML body, not a block page — endpoint gone
    Arctic-Shift          10 rows    working

and the production log shows the rest:

    ⚠ RSS residential primary got 404 — falling back to the worker
    ⚠ Reddit RSS returned 404 for https://…workers.dev/r/melbourne/search.rss
    (HTTPError: 422 Client Error: Unprocessable Entity — retrying page)
    (Arctic-Shift error: Too many requests)
    (arctic 40 sub×term queries) No new results (0 raw, all filtered out): no posts in batch

So Arctic carries the entire feature, and the fan-out fires ~40 sub×term queries at it, 4
concurrent. Arctic throttles readily under that: it answers "Too many requests", or HTTP 422
with {"error": "Timeout. Maybe slow down a bit"} — measured directly, a single bare keyword
gets a 422 when it is being throttled, so this is not about query syntax.

`_cool_if_down` matched "too many" / "rate" and set a 300-SECOND leg-wide cooldown, and the
exception path cooled the leg unconditionally after one failed retry. One throttled page out
of forty therefore blacked out the whole search — and since every subsequent search re-tripped
it, permanently in practice.

A rate limit means back off and come back. Only maintenance means stop. That distinction, plus
backing the concurrency down from 4 to 2, is this change.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.reddit_bot import RedditSearchBot  # noqa: E402


@pytest.fixture(autouse=True)
def _clean_breaker():
    RedditSearchBot._arctic_cooldown_until = 0.0
    RedditSearchBot._arctic_rl_strikes = 0
    yield
    RedditSearchBot._arctic_cooldown_until = 0.0
    RedditSearchBot._arctic_rl_strikes = 0


# ── the classifier ─────────────────────────────────────────────────────────────────────────────
def test_the_two_real_arctic_throttle_signals_are_rate_limits():
    """Both of these were being read as an outage. They are the ones Arctic actually sends."""
    assert RedditSearchBot._arctic_err_kind("Too many requests") == "ratelimit"
    assert RedditSearchBot._arctic_err_kind("Timeout. Maybe slow down a bit") == "ratelimit"
    assert RedditSearchBot._arctic_err_kind(
        "HTTPError: 422 Client Error: Unprocessable Entity") == "ratelimit"


def test_maintenance_is_still_an_outage():
    assert RedditSearchBot._arctic_err_kind("Server under maintenance") == "outage"
    assert RedditSearchBot._arctic_err_kind("MAINTENANCE") == "outage"


def test_an_unknown_error_is_neither():
    assert RedditSearchBot._arctic_err_kind("KeyError: 'data'") == "other"
    assert RedditSearchBot._arctic_err_kind("") == "other"
    assert RedditSearchBot._arctic_err_kind(None) == "other"


# ── the behaviour that was wrong ───────────────────────────────────────────────────────────────
def _bot(monkeypatch, responses):
    """A bot whose Arctic page fetch returns `responses` in order. Each entry is either a dict
    (the parsed body) or an Exception to raise."""
    bot = RedditSearchBot()
    calls = {"n": 0}

    class _R:
        def __init__(self, payload):
            self._p = payload

        def json(self):
            return self._p

    def _req(url, params, timeout=10):
        i = min(calls["n"], len(responses) - 1)
        calls["n"] += 1
        r = responses[i]
        if isinstance(r, Exception):
            raise r
        return _R(r)

    monkeypatch.setattr(bot, "_make_request", _req)
    monkeypatch.setattr(time, "sleep", lambda *_a, **_k: None)
    import search.reddit_bot as RB
    monkeypatch.setattr(RB.time, "sleep", lambda *_a, **_k: None)
    return bot, calls


def test_a_throttled_page_is_retried_not_abandoned(monkeypatch):
    """The core fix: throttle, then success, yields results."""
    ok = {"data": [{"id": "a1", "title": "t", "created_utc": time.time(),
                    "subreddit": "Mattress", "permalink": "/r/x/1", "num_comments": 3,
                    "score": 5, "selftext": "", "author": "u"}]}
    bot, calls = _bot(monkeypatch, [{"error": "Too many requests"}, ok])
    out = bot._search_arctic("mattress", "Mattress", 365, 1)
    assert calls["n"] >= 2, "gave up without retrying a rate-limited page"
    assert len(out) == 1


def test_one_throttled_page_does_not_black_out_the_leg(monkeypatch):
    """THE regression. 40 sub×term queries share one class-level breaker: if a single
    throttled page cools it, the other 39 return nothing and the search reports 0."""
    bot, _ = _bot(monkeypatch, [{"error": "Too many requests"}])
    bot._search_arctic("mattress", "Mattress", 365, 5)
    assert RedditSearchBot._arctic_cooldown_until <= time.time(), \
        "a single rate-limited page cooled the whole leg down"


def test_maintenance_still_cools_the_leg_immediately(monkeypatch):
    """The breaker must keep working for its real purpose — don't hammer a down service."""
    bot, calls = _bot(monkeypatch, [{"error": "Server under maintenance"}])
    bot._search_arctic("mattress", "Mattress", 365, 5)
    assert RedditSearchBot._arctic_cooldown_until > time.time()
    assert calls["n"] == 1, "retried a service that said it was down"


def test_a_sustained_throttle_does_eventually_cool_down(monkeypatch):
    """Backing off forever would burn the whole time budget. Repeated give-ups still trip it."""
    assert RedditSearchBot._ARCTIC_RL_STRIKES_MAX <= 20, \
        "the strike threshold is so high the breaker would never trip"
    bot, _ = _bot(monkeypatch, [{"error": "Too many requests"}])
    # A FIXED bound, not range(_ARCTIC_RL_STRIKES_MAX): looping over the constant under test
    # means raising it makes this test spin instead of fail, which hides the very regression
    # it exists to catch.
    tripped = False
    for _ in range(20):
        RedditSearchBot._arctic_cooldown_until = 0.0     # ignore the breaker between calls
        bot._search_arctic("mattress", "Mattress", 365, 5)
        if RedditSearchBot._arctic_cooldown_until > time.time():
            tripped = True                                # stop the moment it trips, or the
            break                                         # next iteration clears it again
    assert tripped, "a sustained throttle never cooled the leg down"


def test_a_clean_page_clears_the_strike_count(monkeypatch):
    """Strikes must be CONSECUTIVE — otherwise a slow drip of throttles across an otherwise
    healthy hour eventually blacks the leg out for no reason."""
    ok = {"data": [{"id": "a1", "title": "t", "created_utc": time.time(),
                    "subreddit": "Mattress", "permalink": "/r/x/1", "num_comments": 3,
                    "score": 5, "selftext": "", "author": "u"}]}
    bot, _ = _bot(monkeypatch, [{"error": "Too many requests"}])
    for _ in range(min(RedditSearchBot._ARCTIC_RL_STRIKES_MAX, 20) - 1):
        RedditSearchBot._arctic_cooldown_until = 0.0
        bot._search_arctic("mattress", "Mattress", 365, 5)
    assert RedditSearchBot._arctic_rl_strikes > 0
    bot2, _ = _bot(monkeypatch, [ok])
    bot2._search_arctic("mattress", "Mattress", 365, 1)
    assert RedditSearchBot._arctic_rl_strikes == 0


def test_an_http_exception_is_retried_too(monkeypatch):
    """422 arrives as a raised HTTPError, not as a body error — the exception path has to
    classify it the same way, or the fix only covers half the cases."""
    ok = {"data": [{"id": "a1", "title": "t", "created_utc": time.time(),
                    "subreddit": "Mattress", "permalink": "/r/x/1", "num_comments": 3,
                    "score": 5, "selftext": "", "author": "u"}]}
    err = Exception("422 Client Error: Unprocessable Entity for url: ...")
    bot, calls = _bot(monkeypatch, [err, ok])
    out = bot._search_arctic("mattress", "Mattress", 365, 1)
    assert calls["n"] >= 2
    assert len(out) == 1
    assert RedditSearchBot._arctic_cooldown_until <= time.time()


# ── the backoff has to fit the budget it lives in ──────────────────────────────────────────────
def test_backoff_never_outlives_the_leg_deadline(monkeypatch):
    """The leg budget is ~24s shared across up to 40 sub×term pairs. An unbounded 2/4/8s
    backoff spends it on three throttled pairs and still returns nothing — the same blackout
    by a slower route. Sleeps must be clipped to what is actually left."""
    slept = []
    bot, _ = _bot(monkeypatch, [{"error": "Too many requests"}])
    import search.reddit_bot as RB
    monkeypatch.setattr(RB.time, "sleep", lambda s: slept.append(s))
    deadline = time.time() + 3.0
    bot._search_arctic("mattress", "Mattress", 365, 5, deadline)
    assert sum(slept) <= 3.0, f"slept {sum(slept):.1f}s against a 3s deadline: {slept}"


def test_no_retry_is_started_with_almost_no_budget_left(monkeypatch):
    slept = []
    bot, calls = _bot(monkeypatch, [{"error": "Too many requests"}])
    import search.reddit_bot as RB
    monkeypatch.setattr(RB.time, "sleep", lambda s: slept.append(s))
    bot._search_arctic("mattress", "Mattress", 365, 5, time.time() + 0.1)
    assert slept == [], "backed off with no budget left"
    assert calls["n"] == 1, "kept retrying past the deadline"


def test_no_deadline_still_backs_off(monkeypatch):
    """Callers that pass no deadline (and the tests above) must keep the retry behaviour."""
    slept = []
    bot, calls = _bot(monkeypatch, [{"error": "Too many requests"}])
    import search.reddit_bot as RB
    monkeypatch.setattr(RB.time, "sleep", lambda s: slept.append(s))
    bot._search_arctic("mattress", "Mattress", 365, 5)
    assert calls["n"] == RedditSearchBot._ARCTIC_PAGE_TRIES
    assert len(slept) == RedditSearchBot._ARCTIC_PAGE_TRIES - 1


def test_no_page_is_started_after_the_deadline(monkeypatch):
    """The per-page check matters on a MULTI-page query: pages 2 and 3 must not start once
    the budget is gone, even though page 1 came back clean."""
    full = {"data": [{"id": f"p{i}", "title": "t", "created_utc": time.time(),
                      "subreddit": "Mattress", "permalink": f"/r/x/{i}", "num_comments": 3,
                      "score": 5, "selftext": "", "author": "u"} for i in range(100)]}
    bot, calls = _bot(monkeypatch, [full])
    bot._search_arctic("mattress", "Mattress", 365, 500, time.time() - 1)
    assert calls["n"] == 0, "started a page with the deadline already past"


def test_a_live_deadline_still_allows_paging(monkeypatch):
    """...and the check must not stop a query that still has budget."""
    full = {"data": [{"id": f"p{i}", "title": "t", "created_utc": time.time(),
                      "subreddit": "Mattress", "permalink": f"/r/x/{i}", "num_comments": 3,
                      "score": 5, "selftext": "", "author": "u"} for i in range(100)]}
    bot, calls = _bot(monkeypatch, [full])
    bot._search_arctic("mattress", "Mattress", 365, 500, time.time() + 60)
    assert calls["n"] >= 1, "a live deadline blocked the first page"


def test_the_fan_out_passes_its_deadline_down():
    """Threading it through the ThreadPoolExecutor submit is the whole point — without it the
    pairs each back off blind and the budget check between futures comes far too late."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "search", "reddit_bot.py"), encoding="utf-8").read()
    assert "per_pair, leg_deadline)" in src
    assert "def _search_arctic(self, keyword, subreddit, max_days_old, limit, deadline=None):" in src


def test_the_backoff_base_fits_a_40s_budget():
    """0.75/1.5/3 ≈ 5s worst case per pair. At 2/4/8 ≈ 14s, three pairs exhaust the budget."""
    worst = sum(RedditSearchBot._ARCTIC_BACKOFF_BASE * (2 ** i)
                for i in range(RedditSearchBot._ARCTIC_PAGE_TRIES - 1))
    assert worst <= 6.0, f"worst-case backoff per pair is {worst:.1f}s"


# ── the burst that provokes it ─────────────────────────────────────────────────────────────────
def test_arctic_concurrency_is_bounded():
    """FU294 dropped this to 2, because a throttle then meant a 300s leg-wide blackout and
    provoking one was catastrophic. FU302 raised it back to 4 once that stopped being true —
    a throttle now costs a short, budget-clipped backoff, and concurrency had become the
    binding constraint on results. What still matters here is that it stays BOUNDED: the
    point of FU294 was never the number 2, it was not hammering a service that throttles."""
    assert 1 <= RedditSearchBot._arctic_sem._value <= 6


def test_the_old_blanket_cooldown_is_gone():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "search", "reddit_bot.py"), encoding="utf-8").read()
    assert "def _cool_if_down" not in src, "the all-signals-are-an-outage helper is still here"
    assert '"maintenance", "too many", "slow down", "rate"' not in src
