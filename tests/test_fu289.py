"""FU289 — "+HQ generated 0 comments" on third-party posts with a title-only body.

Generating an HQ cluster on an imported third-party post returned nothing, every time. The
production log said what happened but not why:

    [HQ] main generated in 0.0s
    [HQ] main returned no body — retrying once
    [HQ] main generated in 0.0s
    HQ: main API failed twice — aborting thread

0.0s, twice — no API call was made at all, and no exception was raised, so nothing in the log
named a cause. `generate_comments` opened with:

    if not comments and not post_body:
        return {"generated_comments": [], ...}

For an HQ ROOT there is no thread yet, so `comments` is []. And a title-only Reddit post — the
question in the title, selftext empty — imports with `body = ""`. Both falsy, instant return.
6 of 110 third-party posts in production are title-only, which is an ordinary shape on Reddit
and exactly what a verbatim import is supposed to preserve.

The guard means "there is nothing to react to", and a title is something to react to. The
prompt was always ready for it: `post_body_text` already drops the POST BODY block when the
body is empty, so a title-only post produces a clean, shorter prompt rather than a broken one.
Only the guard stopped it from ever being built.

Not a regression from the FU287 import work — this has been true for any title-only post since
the guard was written. It surfaced now because third-party imports are the path that routinely
carries one.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.comment_gen import CommentGenerator  # noqa: E402
from tests.stubs import StubClaude  # noqa: E402

TITLE = "best premium glass baby bottles in us"
BODY = "We have been through four brands and the venting is still wrong."


class _DB:
    def get_subreddit(self, _sid):
        return {"id": 1, "name": "NewMomStuff", "domain": "parenting"}

    def __getattr__(self, _n):
        return lambda *a, **k: None


def _gen(handler=None):
    claude = StubClaude(call_handler=handler or (lambda p: {
        "generated_comments": ["a real comment body"],
        "strategies_used": ["x"],
    }))
    g = CommentGenerator(claude, _DB())
    return g, claude


def _call(g, *, title=TITLE, body=BODY, comments=None):
    return g.generate_comments(
        post_title=title, post_body=body, subreddit="NewMomStuff",
        comments=comments or [], brand_name="Thyseed", brand_context="baby bottles",
        num_comments=1, mention_brand_flags=[True])


# ── the bug ────────────────────────────────────────────────────────────────────────────────────
def test_title_only_post_still_generates(claude_free=None):
    """THE regression: an HQ root on a title-only post. No thread, no body — but a title."""
    g, claude = _gen()
    out = _call(g, body="", comments=[])
    assert claude.calls, "no prompt was built — bailed before reaching the model"
    assert out["generated_comments"] == ["a real comment body"]


def test_title_only_prompt_carries_the_title_and_omits_the_body_block():
    """The prompt must be coherent without a body, not merely non-empty."""
    g, claude = _gen()
    _call(g, body="", comments=[])
    prompt = claude.calls[-1]
    assert TITLE in prompt
    # the literal data block, not the words "POST BODY" which also appear in the instructions
    assert 'POST BODY: """' not in prompt, "emitted an empty POST BODY block"


def test_whitespace_only_body_is_treated_as_empty_not_as_context():
    g, claude = _gen()
    _call(g, body="   \n  ", comments=[])
    assert claude.calls
    assert 'POST BODY: """' not in claude.calls[-1]


# ── what the guard must still catch ────────────────────────────────────────────────────────────
def test_nothing_at_all_still_short_circuits():
    """The guard's real purpose: no title, no body, no thread = nothing to react to. It must
    still return without spending an API call."""
    g, claude = _gen()
    out = _call(g, title="", body="", comments=[])
    assert out["generated_comments"] == []
    assert claude.calls == [], "spent an API call on a post with no content at all"


def test_whitespace_only_title_does_not_rescue_an_empty_post():
    g, claude = _gen()
    out = _call(g, title="   ", body="", comments=[])
    assert out["generated_comments"] == []
    assert claude.calls == []


# ── pre-existing behaviour, unchanged ──────────────────────────────────────────────────────────
def test_a_body_alone_still_works():
    g, claude = _gen()
    out = _call(g, title="", body=BODY, comments=[])
    assert claude.calls and out["generated_comments"]


def test_an_existing_thread_alone_still_works():
    """A reply slot: no body, no title, but sibling comments to react to."""
    g, claude = _gen()
    out = _call(g, title="", body="",
                comments=[{"body": "I used the wide-neck ones", "author": "someone",
                           "score": 4}])
    assert claude.calls and out["generated_comments"]


# ── end to end through the HQ path ─────────────────────────────────────────────────────────────
def test_hq_cluster_on_a_title_only_post(monkeypatch):
    """The reported symptom, driven through generate_hq_comment itself: an imported post with
    a real title and no body must produce a cluster, not '0 comments generated'."""
    import generators.comment_gen as CG
    monkeypatch.setattr(CG.time, "sleep", lambda *_a, **_k: None, raising=False)

    saved = []

    class _SaveDB(_DB):
        def save_comment(self, *a, **k):
            saved.append(k or a)
            return len(saved)

        def get_comment(self, cid):
            return {"id": cid, "body": "a real comment body", "parent_comment_id": None}

    claude = StubClaude(call_handler=lambda p: {
        "generated_comments": ["a real comment body"], "strategies_used": ["x"]})
    g = CommentGenerator(claude, _SaveDB())
    post = {"id": 929, "subreddit_id": 1, "title": TITLE, "body": "",
            "storyline": "third-party", "suggested_post_day": 0, "intent": None,
            "concept_checklist": None, "is_third_party": 1}
    out = g.generate_hq_comment(post, {"id": 1, "name": "Thyseed", "context": "baby bottles"},
                                num_replies=1)
    assert claude.calls, "the HQ root never reached the model"
    assert out, "HQ returned 0 comments for a title-only post"


# ── the guard's shape, in the source ───────────────────────────────────────────────────────────
def test_the_guard_consults_the_title():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "generators", "comment_gen.py"), encoding="utf-8").read()
    assert 'if not comments and not _body and not (post_title or "").strip():' in src
    assert "if not comments and not post_body:\n" not in src
    # the block and the guard must read the SAME normalised body, or they disagree on
    # whitespace and one of them is wrong
    assert '_body = (post_body or "").strip()' in src
    assert 'POST BODY: """{_body[:800]}"""' in src
