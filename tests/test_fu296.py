"""FU296 — the dead RSS leg ate the whole budget, so Arctic never ran.

FU294/FU295 stopped Arctic blacking itself out. Search still returned 0, and the production
log says why — Arctic is never reached at all:

    ⚠ RSS residential primary got 404 — falling back to the worker
    ⚠ Reddit RSS returned 404 for https://…workers.dev/r/melbourne/search.rss
    …~20 subreddits of this…
    No new results (0 raw, all filtered out): no posts in batch
    ⏱ leg time budget reached — returning 0 result(s) gathered so far
    ⚠ All APIs failed or returned no results

`search.rss` 404s for EVERY subreddit now — Reddit appears to have removed the endpoint. But
the existing latches do not catch a 404: only a 403 marks the RSS leg dead, only a 429 cools
it down. A 404 was read as "this particular sub does not exist", which was the right reading
when one sub 404'd and is the wrong one when twenty do. So every search re-ran ~20 subs ×
(residential + worker) = ~40 doomed requests and spent the entire 24s leg budget before Arctic
— the only leg still returning anything — got a turn.

A 404 across SEVERAL DIFFERENT subs is not a fact about the subs, it is the endpoint being
gone. One extra wrinkle: `_resi_active()` deliberately bypasses the worker-health latches,
because the residential proxy does not share the worker's IP block. That is right for 403/429
and wrong for 404 — a removed endpoint 404s from every IP, and the log shows residential
404ing too. So this latch is endpoint-level and is checked unconditionally.
"""
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from search.reddit_bot import RedditSearchBot  # noqa: E402


@pytest.fixture(autouse=True)
def _clean():
    RedditSearchBot._rss_gone_subs = set()
    RedditSearchBot._rss_gone_until = 0.0
    yield
    RedditSearchBot._rss_gone_subs = set()
    RedditSearchBot._rss_gone_until = 0.0


# ── when to believe the endpoint is gone ───────────────────────────────────────────────────────
def test_one_sub_404ing_is_still_just_that_sub():
    """The old reading was correct for a single sub — a typo'd or private subreddit 404s, and
    that must not take the whole leg down."""
    RedditSearchBot._rss_note_404("r/definitely-not-a-sub")
    assert RedditSearchBot._rss_gone_until == 0.0


def test_several_distinct_subs_404ing_means_the_endpoint_is_gone():
    for sub in ("melbourne", "sydney", "perth"):
        RedditSearchBot._rss_note_404(sub)
    assert RedditSearchBot._rss_gone_until > time.time()


def test_the_same_sub_404ing_repeatedly_is_not_evidence():
    """Otherwise one bad sub retried three times would disable the leg for everything."""
    for _ in range(8):
        RedditSearchBot._rss_note_404("melbourne")
    assert RedditSearchBot._rss_gone_until == 0.0


def test_case_and_formatting_do_not_inflate_the_count():
    for sub in ("Melbourne", "melbourne", "MELBOURNE"):
        RedditSearchBot._rss_note_404(sub)
    assert RedditSearchBot._rss_gone_until == 0.0


def test_a_single_success_clears_the_latch():
    """If Reddit restores the endpoint the leg has to come straight back."""
    for sub in ("a", "b", "c"):
        RedditSearchBot._rss_note_404(sub)
    assert RedditSearchBot._rss_gone_until > time.time()
    RedditSearchBot._rss_note_ok()
    assert RedditSearchBot._rss_gone_until == 0.0
    assert RedditSearchBot._rss_gone_subs == set()


# ── the leg actually stops running ─────────────────────────────────────────────────────────────
def _bot_that_counts(monkeypatch):
    bot = RedditSearchBot()
    calls = {"n": 0}

    def _get(*a, **k):
        calls["n"] += 1
        raise AssertionError("the RSS leg made a request while latched off")

    import search.reddit_bot as RB
    monkeypatch.setattr(RB.requests, "get", _get)
    return bot, calls


def test_the_latched_leg_makes_no_requests(monkeypatch):
    bot, calls = _bot_that_counts(monkeypatch)
    RedditSearchBot._rss_gone_until = time.time() + 600
    out = bot._search_reddit_rss("mattress", "Mattress", "relevance", "year", 10)
    assert out == []
    assert calls["n"] == 0


def test_the_residential_bypass_does_not_override_this(monkeypatch):
    """THE wrinkle. `_resi_active()` skips the worker-health latches on purpose, because
    residential does not share the worker's IP block. A removed endpoint is not an IP block —
    residential 404s too — so this latch must survive that bypass."""
    bot, calls = _bot_that_counts(monkeypatch)
    monkeypatch.setattr(bot, "_resi_active", lambda: True)
    bot._reddit_rss_dead = True
    RedditSearchBot._rss_gone_until = time.time() + 600
    assert bot._search_reddit_rss("mattress", "Mattress", "relevance", "year", 10) == []
    assert calls["n"] == 0, "the residential bypass re-enabled a leg whose endpoint is gone"


def test_an_unlatched_leg_still_runs(monkeypatch):
    """The latch must not be a permanent off switch — with it clear, the leg tries as before."""
    bot = RedditSearchBot()
    tried = {"n": 0}

    class _R:
        status_code = 404
        text = ""
        headers = {}

    import search.reddit_bot as RB

    def _get(*a, **k):
        tried["n"] += 1
        return _R()

    monkeypatch.setattr(RB.requests, "get", _get)
    bot._search_reddit_rss("mattress", "Mattress", "relevance", "year", 10)
    assert tried["n"] >= 1


def test_a_real_200_clears_the_latch_at_the_call_site(monkeypatch):
    """Asserting on _rss_note_ok() directly does not prove the 200 path CALLS it — without the
    call, a restored endpoint would stay latched off for the full 15 minutes every time."""
    bot = RedditSearchBot()
    RedditSearchBot._rss_gone_subs = {"melbourne", "sydney"}   # below the threshold, so the
    RedditSearchBot._rss_gone_until = 0.0                       # leg still runs

    class _R:
        status_code = 200
        headers = {}
        text = ('<?xml version="1.0" encoding="UTF-8"?>'
                '<feed xmlns="http://www.w3.org/2005/Atom"></feed>')

    import search.reddit_bot as RB
    monkeypatch.setattr(RB.requests, "get", lambda *a, **k: _R())
    bot._search_reddit_rss("mattress", "Mattress", "relevance", "year", 10)
    assert RedditSearchBot._rss_gone_subs == set(), \
        "a successful fetch did not clear the 404 evidence"


# ── both paths report, not just the worker ─────────────────────────────────────────────────────
def test_both_the_residential_and_worker_404s_are_counted():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "search", "reddit_bot.py"), encoding="utf-8").read()
    assert src.count("RedditSearchBot._rss_note_404(subreddit_path)") == 2, \
        "only one of the two 404 paths feeds the counter"
    assert "if presp.status_code == 404:" in src, "the residential 404 is not counted"
    assert "elif resp.status_code == 404:" in src, "the worker 404 is not counted"


def test_a_404_is_not_treated_as_a_hard_block_or_a_throttle():
    """It must keep its own handling — latching _reddit_rss_dead (403) or the 429 cooldown
    would change behaviour for cases this is not about."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "search", "reddit_bot.py"), encoding="utf-8").read()
    assert "if resp.status_code == 403:" in src
    assert "elif resp.status_code == 429:" in src


# ── FU297: the discovery endpoint is dead the same way ─────────────────────────────────────────
@pytest.fixture(autouse=True)
def _clean_subsearch():
    RedditSearchBot._subsearch_gone_terms = set()
    RedditSearchBot._subsearch_gone_until = 0.0
    yield
    RedditSearchBot._subsearch_gone_terms = set()
    RedditSearchBot._subsearch_gone_until = 0.0


def test_discovery_latches_after_several_distinct_terms():
    """/subreddits/search.json 404s for every term, finds nothing, and the reddit-wide leg
    still spends its slice of the budget before reporting 0."""
    for term in ("NDIS", "home care"):
        RedditSearchBot._subsearch_note_404(term)
    assert RedditSearchBot._subsearch_gone_until == 0.0
    RedditSearchBot._subsearch_note_404("aged care")
    assert RedditSearchBot._subsearch_gone_until > time.time()


def test_discovery_latch_is_separate_from_the_rss_one():
    """Different endpoints; either may come back without the other."""
    for term in ("a", "b", "c"):
        RedditSearchBot._subsearch_note_404(term)
    assert RedditSearchBot._subsearch_gone_until > time.time()
    assert RedditSearchBot._rss_gone_until == 0.0


def test_latched_discovery_makes_no_requests(monkeypatch):
    """COUNT the calls rather than raising: `_sub_search` wraps everything in try/except, so a
    raised assertion is swallowed and the test passes while the request still goes out."""
    bot = RedditSearchBot()
    calls = {"n": 0}
    import search.reddit_bot as RB

    def _count(*a, **k):
        calls["n"] += 1
        raise RuntimeError("network")

    monkeypatch.setattr(RB.requests, "get", _count)
    RedditSearchBot._subsearch_gone_until = time.time() + 600
    assert bot._sub_search("NDIS") == []
    assert calls["n"] == 0, "discovery made a request while latched off"


def test_a_real_discovery_404_feeds_the_counter(monkeypatch):
    """Asserting on _subsearch_note_404 directly does not prove the 404 path CALLS it."""
    bot = RedditSearchBot()

    class _R:
        status_code = 404
        headers = {}
        text = ""

        def json(self):
            return {}

    import search.reddit_bot as RB
    monkeypatch.setattr(RB.requests, "get", lambda *a, **k: _R())
    for term in ("NDIS", "home care", "aged care"):
        bot._sub_search(term)
    assert RedditSearchBot._subsearch_gone_until > time.time(), \
        "three distinct terms 404'd and the leg was not latched off"


def test_a_discovery_success_clears_its_latch(monkeypatch):
    bot = RedditSearchBot()
    RedditSearchBot._subsearch_gone_terms = {"a", "b"}

    class _R:
        status_code = 200
        headers = {}

        def json(self):
            return {"data": {"children": [
                {"data": {"display_name": "NDIS", "subscribers": 100, "over18": False}}]}}

    import search.reddit_bot as RB
    monkeypatch.setattr(RB.requests, "get", lambda *a, **k: _R())
    out = bot._sub_search("NDIS")
    assert out and out[0]["name"] == "NDIS"
    assert RedditSearchBot._subsearch_gone_terms == set()
