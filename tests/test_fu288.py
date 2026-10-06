"""FU288 — a walled proxy read as "deleted", and the archive was allowed to kill on its own.

A live comment (r/LLMDevs pcz3c2k, Sonilo) came back from Check-Live as `replace`. It was not
close to removed: its own body was intact, and even the archive agreed it was live. Two faults
stacked up, and the second one only mattered because of the first.

**Reddit 404-walls Cloudflare.** Measured from the production box:

    worker /resolve/…          200  {"url": …}        the worker is deployed and healthy
    worker /r/…/.rss           404  "Not Found"       9 bytes, every path, including .json
    www.reddit.com/…/.rss      200  valid XML         same moment, from the same box

The worker has no "Not Found" string of its own — it returns the upstream status verbatim. So
that 404 is Reddit's answer to Cloudflare's shared egress, exactly the FU130 wall. But
`_reddit_rss_fetch` counted a 404 as CONCLUSIVE ("gone") and returned it without trying anything
else. A dead proxy and a deleted post were the same event, the healthy direct leg was never
reached, and RSS could not return a verdict for any comment in the system.

**Which left the archive unopposed.** With RSS permanently inconclusive, Arctic was the only
source that ever produced a verdict — and `_resolve_live_or_orphaned` trusted an archive
"parent post removed" with no confirmation at all. `removed_by_category` is usually Reddit's
spam filter catching a post a mod cleared minutes later; the archive snapshots that window and
never re-crawls. Every comment under such a post died.

FU145 already established the principle for the other direction: a stale archive-LIVE verdict is
not positive confirmation and must not resurrect a dead row. The same staleness forbids killing
a live comment on a stale archive-REMOVED one. That symmetry is what this restores.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

os.environ.setdefault("SKIP_BG", "1")
import app as A  # noqa: E402

XML = '<?xml version="1.0" encoding="UTF-8"?><feed xmlns="http://www.w3.org/2005/Atom"></feed>'
PATH = "/r/LLMDevs/comments/1vqwdxl/.rss"


class _Resp:
    def __init__(self, status, text=""):
        self.status_code = status
        self.text = text
        self.headers = {}


@pytest.fixture()
def legs(monkeypatch):
    """Record every host _reddit_rss_fetch tries, and script what each one answers."""
    calls = []
    script = {}

    def _get(url, params=None, headers=None, timeout=None, proxies=None, **kw):
        calls.append(url)
        for frag, resp in script.items():
            if frag in url:
                if isinstance(resp, Exception):
                    raise resp
                return resp
        return _Resp(500)

    import requests as _rq
    monkeypatch.setattr(_rq, "get", _get)
    # the retry loops sleep between attempts; tests must not. `_reddit_rss_fetch` does its own
    # `import time as _t` inside the function, so patching the module reaches it.
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    return calls, script


def _with_worker(monkeypatch, url="https://worker.example"):
    monkeypatch.setattr(A, "REDDIT_PROXY_URL", url, raising=False)
    monkeypatch.setenv("REDDIT_PROXY_URL", url)


def _no_worker(monkeypatch):
    monkeypatch.setattr(A, "REDDIT_PROXY_URL", "", raising=False)
    monkeypatch.delenv("REDDIT_PROXY_URL", raising=False)


def _no_residential(monkeypatch):
    monkeypatch.setattr(A, "_reddit_http_proxies", lambda *a, **k: None, raising=False)


# ── fix 1: a proxy 404 is a wall, not a verdict ────────────────────────────────────────────────
def test_worker_404_falls_through_to_direct(legs, monkeypatch):
    """THE bug. The worker 404s every path; direct answers 200 XML. The old code returned the
    404 and never asked Reddit."""
    calls, script = legs
    _with_worker(monkeypatch)
    _no_residential(monkeypatch)
    script["worker.example"] = _Resp(404, "Not Found")
    script["www.reddit.com"] = _Resp(200, XML)

    r = A._reddit_rss_fetch(PATH)
    assert r.status_code == 200 and r.text == XML
    assert any("www.reddit.com" in c for c in calls), "never tried Reddit directly"


def test_worker_404_and_direct_404_is_still_gone(legs, monkeypatch):
    """Falling through must not make genuine removals undetectable: when Reddit itself 404s,
    that is still a verdict."""
    calls, script = legs
    _with_worker(monkeypatch)
    _no_residential(monkeypatch)
    script["worker.example"] = _Resp(404, "Not Found")
    script["www.reddit.com"] = _Resp(404, "")

    r = A._reddit_rss_fetch(PATH)
    assert r.status_code == 404
    assert any("www.reddit.com" in c for c in calls)


def test_direct_404_without_a_worker_is_conclusive(legs, monkeypatch):
    """With no proxy configured the primary leg IS Reddit, so its 404 is conclusive and nothing
    extra should be attempted."""
    calls, script = legs
    _no_worker(monkeypatch)
    _no_residential(monkeypatch)
    script["www.reddit.com"] = _Resp(404, "")

    r = A._reddit_rss_fetch(PATH)
    assert r.status_code == 404
    assert len(calls) == 1, f"a conclusive first answer should end it, got {calls}"


def test_worker_200_xml_short_circuits(legs, monkeypatch):
    """When the worker works it must still be the only call — the direct leg is a fallback,
    not an extra round trip on every read."""
    calls, script = legs
    _with_worker(monkeypatch)
    _no_residential(monkeypatch)
    script["worker.example"] = _Resp(200, XML)

    r = A._reddit_rss_fetch(PATH)
    assert r.status_code == 200
    assert not any("www.reddit.com" in c for c in calls), "fell through despite a good answer"


def test_worker_block_also_reaches_direct(legs, monkeypatch):
    """403/429/5xx were already treated as blocks; they must reach the new leg too."""
    for status in (403, 429, 503):
        calls, script = legs
        calls.clear(); script.clear()
        _with_worker(monkeypatch)
        _no_residential(monkeypatch)
        script["worker.example"] = _Resp(status, "blocked")
        script["www.reddit.com"] = _Resp(200, XML)
        r = A._reddit_rss_fetch(PATH)
        assert r.status_code == 200, f"worker {status} did not fall through"


def test_no_proxy_does_not_double_ask_reddit(legs, monkeypatch):
    """With no worker the primary leg already IS Reddit; the fallback must not ask it again."""
    calls, script = legs
    _no_worker(monkeypatch)
    _no_residential(monkeypatch)
    script["www.reddit.com"] = _Resp(403, "blocked")

    A._reddit_rss_fetch(PATH)
    direct = [c for c in calls if "www.reddit.com" in c]
    assert len(direct) == 3, f"expected the 3 primary retries only, got {len(direct)}: {calls}"


def test_a_good_direct_answer_spends_no_residential_gb(legs, monkeypatch):
    """Residential is metered. Once direct has answered conclusively the fetch must RETURN,
    not fall through and buy the same bytes again."""
    calls, script = legs
    _with_worker(monkeypatch)
    monkeypatch.setattr(A, "_reddit_http_proxies",
                        lambda *a, **k: {"http": "http://resi", "https": "http://resi"},
                        raising=False)
    script["worker.example"] = _Resp(404, "Not Found")
    script["www.reddit.com"] = _Resp(200, XML)
    script["old.reddit.com"] = _Resp(200, XML)

    r = A._reddit_rss_fetch(PATH, resi_fallback=True)
    assert r.status_code == 200
    assert not any("old.reddit.com" in c for c in calls), \
        "burned metered residential GB after direct already answered"


def test_residential_leg_still_runs_after_direct(legs, monkeypatch):
    """The new leg sits BEFORE the residential one and must not displace it — residential is
    the last resort when both the worker and direct are walled."""
    calls, script = legs
    _with_worker(monkeypatch)
    monkeypatch.setattr(A, "_reddit_http_proxies",
                        lambda *a, **k: {"http": "http://resi", "https": "http://resi"},
                        raising=False)
    script["worker.example"] = _Resp(403, "")
    script["www.reddit.com"] = _Resp(403, "")
    script["old.reddit.com"] = _Resp(200, XML)

    r = A._reddit_rss_fetch(PATH, resi_fallback=True)
    assert r.status_code == 200 and r.text == XML
    assert any("old.reddit.com" in c for c in calls)
    assert [c for c in calls].index(next(c for c in calls if "www.reddit.com" in c)) \
        < [c for c in calls].index(next(c for c in calls if "old.reddit.com" in c))


# ── fix 2: the archive cannot orphan a comment on its own ──────────────────────────────────────
class _FakeDB:
    """Records the writes `_check_live_batch` makes, so a test can see what it decided."""

    def __init__(self):
        self.dead = []
        self.live = []
        self.conn = self

    def execute(self, *a, **k):                 # posted_at lookup
        return self

    def fetchone(self):
        return {"posted_at": "2026-10-01 00:00:00"}

    def mark_comment_removed_or_replace(self, cid, posted_at_hint=None):
        self.dead.append(cid)
        return "replace"

    def set_comment_live_check(self, cid):
        self.live.append(cid)

    def __getattr__(self, _n):                  # every other db call is a no-op recorder
        return lambda *a, **k: None


CURL = "https://www.reddit.com/r/LLMDevs/comments/1vqwdxl/comment/pcz3c2k/"


def _run_batch(monkeypatch, *, post_archive, post_rss):
    """Drive the REAL _check_live_batch for one live-on-Arctic comment whose parent post the
    archive flags, and report whether it was killed.

    Deliberately end-to-end: an earlier version of this test reimplemented the branch and so
    passed against a mutated app.py — it was testing its own copy of the logic.
    """
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    # archive prefetch: the comment reads live, the parent post reads removed
    def _ids_fetch(kind, ids):
        if kind == "comments":
            return {i: {"body": "a real surviving comment body"} for i in ids}
        return {i: ({"removed_by_category": "reddit", "selftext": "x", "title": "t"}
                    if post_archive == "removed" else {"selftext": "x", "title": "t"})
                for i in ids}
    monkeypatch.setattr(A, "_arctic_ids_fetch", _ids_fetch, raising=False)
    monkeypatch.setattr(A, "_post_liveness_via_rss", lambda *a, **k: post_rss, raising=False)
    # every other door shut, so only the path under test decides
    monkeypatch.setattr(A, "_reddit_info_batch", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(A, "_fetch_old_reddit_html", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(A, "_comment_liveness_via_rss", lambda *a, **k: None, raising=False)

    db = _FakeDB()
    item = {"id": 1, "source": "comment", "status": "report", "reddit_comment_url": CURL}
    res = A._check_live_batch([item], db, log_prefix="TEST")
    return db, res


def test_archive_removed_alone_does_not_kill(monkeypatch):
    """The reported bug: archive says the parent is gone, a live check disagrees → keep it."""
    db, res = _run_batch(monkeypatch, post_archive="removed", post_rss="live")
    assert db.dead == [], "killed a live comment on the archive's word"
    assert db.live == [1]
    assert res["dead"] == 0 and res["live"] == 1


def test_archive_removed_unconfirmed_does_not_kill(monkeypatch):
    """RSS inconclusive (walled / rate-limited) is NOT confirmation — mirrors FU145."""
    db, res = _run_batch(monkeypatch, post_archive="removed", post_rss=None)
    assert db.dead == []
    assert res["dead"] == 0


def test_archive_removed_confirmed_does_kill(monkeypatch):
    """Removal detection must still work when a live source agrees."""
    db, res = _run_batch(monkeypatch, post_archive="removed", post_rss="removed")
    assert db.dead == [1], "a confirmed removal was not acted on"
    assert res["dead"] == 1


def test_archive_live_parent_keeps_the_comment(monkeypatch):
    db, res = _run_batch(monkeypatch, post_archive="live", post_rss="removed")
    assert db.dead == [] and db.live == [1]


def test_archive_live_parent_spends_no_live_fetch(monkeypatch):
    """A parent the archive already cleared must not cost a Reddit round trip."""
    called = {"n": 0}

    def _rss(*a, **k):
        called["n"] += 1
        return "live"
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda *_a, **_k: None)
    monkeypatch.setattr(A, "_arctic_ids_fetch",
                        lambda kind, ids: {i: {"body": "b"} for i in ids} if kind == "comments"
                        else {i: {"selftext": "x", "title": "t"} for i in ids}, raising=False)
    monkeypatch.setattr(A, "_post_liveness_via_rss", _rss, raising=False)
    monkeypatch.setattr(A, "_reddit_info_batch", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(A, "_fetch_old_reddit_html", lambda *a, **k: None, raising=False)
    monkeypatch.setattr(A, "_comment_liveness_via_rss", lambda *a, **k: None, raising=False)
    db = _FakeDB()
    A._check_live_batch([{"id": 1, "source": "comment", "status": "report",
                          "reddit_comment_url": CURL}], db, log_prefix="TEST")
    assert called["n"] == 0, "spent a live fetch on a parent the archive already cleared"


# ── the shape of the fix, in the source ────────────────────────────────────────────────────────
def _src():
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "app.py"), encoding="utf-8").read()


def test_the_orphan_branch_has_no_unconfirmed_kill():
    """`parent_removed = True` must not appear as a bare assignment in the orphan branch —
    that is the line that killed live comments on the archive's word alone."""
    src = _src()
    i = src.index("def _resolve_live_or_orphaned")
    body = src[i:src.index("# =====", i)]
    assert "parent_removed = True" not in body
    assert "if pav != \"live\":" in body


def test_conclusive_is_parameterised():
    src = _src()
    assert "def _conclusive(r, allow_404=True):" in src
    assert "_conclusive(r, allow_404=not proxy)" in src
