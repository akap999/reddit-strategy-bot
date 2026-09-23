"""FU272 (A3) — a rate limit is not a verdict on the page.

Measured from Railway on the thirty authority URLs of stored articles: three of the ten remaining
failures came back `too_many_requests` and became "unreadable" with no second attempt. That is how
a regulator page that was readable a minute earlier ends up in the article as a pointer — and a
pointer, by the FU267 rule, may never support a specific.

The retry is narrow on purpose. `url_not_allowed`, `url_not_accessible` and
`unsupported_content_type` are settled facts about the URL; paying for a second call to be told the
same thing is waste, and web fetch is a billed call.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import generators.base as BB  # noqa: E402
from generators.base import ClaudeClient  # noqa: E402


class _TooManyFetches(BaseException):
    pass


class _Msg:
    def __init__(self, blocks):
        self._b = blocks

    def model_dump(self, warnings=False):
        return {"content": self._b}


def _err(code):
    return [{"type": "web_fetch_tool_result", "content": {"error_code": code}}]


def _ok(text):
    return [{"type": "web_fetch_tool_result",
             "content": {"type": "web_fetch_result",
                         "content": {"source": {"data": text}}}}]


def _client(monkeypatch, replies):
    """A client whose web fetch returns `replies` in order, recording how many calls it took."""
    c = ClaudeClient.__new__(ClaudeClient)
    c.model = "m"
    c._cost_ceiling = None
    c._track = lambda m: None
    c._over_budget = lambda: False
    calls = []

    class _Messages:
        def create(self, **kw):
            calls.append(kw)
            n = sum(1 for k in calls if k != "slept")
            # Hands back each reply once and then REFUSES. A stub that repeats its last reply for
            # ever lets a retry LOOP hang the suite instead of failing it, and a hang in CI reads
            # as an infrastructure problem rather than as the bug it is. It raises OUTSIDE the
            # Exception hierarchy because `web_fetch_text` catches Exception and turns it into a
            # fetch failure — which would swallow the stub's own complaint.
            if n > len(replies):
                raise _TooManyFetches(f"fetched {n} times, {len(replies)} reply(s) provided")
            return _Msg(replies[n - 1])

    class _Client:
        messages = _Messages()

    c.client = _Client()
    monkeypatch.setattr(BB.time, "sleep", lambda s: calls.append("slept"))
    return c, calls


def test_a_rate_limit_is_tried_once_more(monkeypatch):
    c, calls = _client(monkeypatch, [_err("too_many_requests"), _ok("the page text")])
    txt, code = c.web_fetch_text("https://www.fda.gov/x")
    assert (txt, code) == ("the page text", "ok")
    assert sum(1 for k in calls if k != "slept") == 2


def test_it_waits_before_trying_again(monkeypatch):
    """Retrying instantly re-enters the same rate limit. Six workers share it, so the wait is
    jittered — without that they all come back at the same moment and trip it together."""
    c, calls = _client(monkeypatch, [_err("too_many_requests"), _ok("x" * 20)])
    waits = []
    monkeypatch.setattr(BB.time, "sleep", lambda s: waits.append(s))
    c.web_fetch_text("https://www.fda.gov/x")
    assert len(waits) == 1 and 0 < waits[0] <= BB._WEB_FETCH_RETRY_WAIT * 1.5
    assert waits[0] != BB._WEB_FETCH_RETRY_WAIT, "a fixed wait is not jittered"


def test_a_momentary_outage_is_tried_once_more(monkeypatch):
    c, calls = _client(monkeypatch, [_err("unavailable"), _ok("the page text")])
    assert c.web_fetch_text("https://www.fda.gov/x")[1] == "ok"


def test_a_url_that_is_not_allowed_is_not_paid_for_twice(monkeypatch):
    c, calls = _client(monkeypatch, [_err("url_not_allowed"), _ok("never reached")])
    txt, code = c.web_fetch_text("https://www.fda.gov/x")
    assert (txt, code) == ("", "url_not_allowed")
    assert sum(1 for k in calls if k != "slept") == 1, "a settled fact about the URL"


def test_an_unreadable_page_is_not_retried(monkeypatch):
    """`unsupported_content_type` and a missing page will say the same thing next time."""
    for code in ("unsupported_content_type", "url_not_accessible", "invalid_input"):
        c, calls = _client(monkeypatch, [_err(code), _ok("never reached")])
        assert c.web_fetch_text("https://x.example/y")[1] == code
        assert sum(1 for k in calls if k != "slept") == 1, code


def test_a_page_that_reads_first_time_is_fetched_once(monkeypatch):
    c, calls = _client(monkeypatch, [_ok("the page text")])
    assert c.web_fetch_text("https://x.example/y") == ("the page text", "ok")
    assert calls == [calls[0]], "no retry, no sleep, on a page that worked"


def test_the_retry_is_given_up_after_one_attempt(monkeypatch):
    """Two attempts, not a loop: a rate limit that is still there is a real rate limit, and a pool
    of workers spinning on it makes the limit worse rather than better. The stub provides exactly
    two replies, so a third call raises instead of spinning."""
    c, calls = _client(monkeypatch, [_err("too_many_requests"), _err("too_many_requests")])
    assert c.web_fetch_text("https://x.example/y")[1] == "too_many_requests"
    assert sum(1 for k in calls if k != "slept") == 2


def test_the_retry_is_skipped_when_the_budget_is_gone(monkeypatch):
    """The ceiling is checked again before the second call — the first one spent money, and a
    retry is a new billed call. The generation reports the skip rather than silently degrading."""
    c, calls = _client(monkeypatch, [_err("too_many_requests"), _ok("never reached")])
    seen = []
    c._over_budget = lambda: bool(seen) or seen.append(1)   # room for the first call, not the retry
    assert c.web_fetch_text("https://x.example/y")[1] == "too_many_requests"
    assert sum(1 for k in calls if k != "slept") == 1
