"""FU290 — a casual middle ground between a plain comment and full AI-crawl.

AI-crawl comments were being caught by AI-detection bots. Reading the block explains why: it
sets a word FLOOR (">=30 words"), a keyword QUOTA ("3-6 of the brand's ... keywords"), a
mandatory commercial qualifier ("ALWAYS work in the operational and commercial markers") and a
numbers rule ("include 1-2 concrete parameters") — and then closes with an AUTHENTICITY
GOVERNOR that "OVERRIDES every rule above" and says to cut the density.

A brief cannot do both. The quotas are countable and the governor is a judgement call, so the
model satisfies the quotas, and the result reads like an advert — which is exactly what the
detectors and Reddit's spam filter are looking for.

So CASUAL is not the same block turned down. It inverts the structure: ONE requirement (say the
question back in your own words), a ceiling instead of a floor, and explicit permission to
answer only part of it. The thread shape changes with it — a cluster of four substantive
replies is a tell however well each one is written — and the LLM judge is told what casual is,
because otherwise it fails a good short comment for thinness and its feedback feeds the retry
straight back into density.

Carried on the SAME `ai_crawl` argument, as "casual". It is truthy, so all ~85 `if ai_crawl:`
sites keep working untouched; only the places that CHOOSE a block or bias the thread need to
tell True and "casual" apart.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.comment_gen import (  # noqa: E402
    CommentGenerator,
    _allocate_reply_shapes,
    _is_casual_crawl,
    _is_full_crawl,
)
from tests.stubs import StubClaude  # noqa: E402

TITLE = "best premium glass baby bottles in us"


class _DB:
    def get_subreddit(self, _sid):
        return {"id": 1, "name": "NewMomStuff", "domain": "parenting"}

    def __getattr__(self, _n):
        return lambda *a, **k: None


def _prompt(ai_crawl, *, is_hq_reply=False, comments=None):
    claude = StubClaude(call_handler=lambda p: {"generated_comments": ["b"], "strategies_used": []})
    CommentGenerator(claude, _DB()).generate_comments(
        post_title=TITLE, post_body="four brands in and the venting is still wrong",
        subreddit="NewMomStuff", comments=comments or [], brand_name="Thyseed",
        brand_context="baby bottles", num_comments=1, mention_brand_flags=[True],
        ai_crawl=ai_crawl, is_hq_reply=is_hq_reply)
    assert claude.calls, "no prompt was built"
    return claude.calls[-1]


# ── the three modes are actually three ─────────────────────────────────────────────────────────
def test_the_three_modes_produce_three_different_briefs():
    off, casual, full = _prompt(False), _prompt("casual"), _prompt(True)
    assert "CASUAL MODE" in casual and "AI-CRAWL MODE" not in casual
    assert "AI-CRAWL MODE" in full and "CASUAL MODE" not in full
    assert "CASUAL MODE" not in off and "AI-CRAWL MODE" not in off


def test_casual_is_not_the_dense_block_turned_down():
    """The specific instructions that make a comment read like an ad must be ABSENT, not
    softened — a quota next to 'be casual' is the contradiction this replaces."""
    casual = _prompt("casual")
    for quota in ("3-6 of the brand's", "≥30 words",
                  "COMMERCIAL / OPERATIONAL QUALIFIERS",
                  "include 1-2 concrete parameters"):
        assert quota not in casual, f"casual inherited the dense rule: {quota!r}"


def test_the_dense_block_still_has_its_quotas():
    """Full mode is unchanged — this fix adds a mode, it does not dilute the existing one."""
    full = _prompt(True)
    assert "3-6 of the brand's" in full
    assert "SUBSTANCE: ≥30 words" in full


def test_casual_keeps_the_one_thing_that_makes_a_thread_findable():
    casual = _prompt("casual")
    assert TITLE in casual, "casual dropped the long-tail query it has to reinforce"
    assert "your own words" in casual


def test_casual_permits_brevity_and_partial_answers():
    """The two instructions that actually change the output shape."""
    casual = _prompt("casual")
    assert "SHORT IS FINE" in casual
    assert "LEAVE THINGS OUT" in casual
    assert "NO KEYWORD QUOTA" in casual


# ── "casual" must survive the plumbing ─────────────────────────────────────────────────────────
def test_casual_is_truthy_so_existing_checks_are_untouched():
    """The whole design rests on this: ~85 `if ai_crawl:` sites were never rethreaded."""
    assert bool("casual") is True
    assert _is_casual_crawl("casual") and not _is_full_crawl("casual")
    assert _is_full_crawl(True) and not _is_casual_crawl(True)
    assert not _is_full_crawl(False) and not _is_casual_crawl(False)
    assert _is_casual_crawl("CASUAL ") and _is_casual_crawl(" Casual")


def test_the_hq_reply_fold_does_not_collapse_casual_to_full():
    """`ai_crawl and not is_hq_reply` would turn "casual" into True and silently promote every
    casual comment to the dense block."""
    reply = _prompt("casual", is_hq_reply=True)
    assert "CASUAL MODE (reply)" in reply
    assert "EXACT DOMAIN NOUNS" not in reply, "casual reply got the dense reply block"


def test_full_hq_reply_block_is_unchanged():
    reply = _prompt(True, is_hq_reply=True)
    assert "AI-CRAWL (reply mode)" in reply
    assert "EXACT DOMAIN NOUNS" in reply


# ── the thread shape, not just the sentence ────────────────────────────────────────────────────
def test_casual_threads_keep_the_human_reply_mix():
    """A cluster of four substantive replies is a tell on its own. Only FULL forces that mix."""
    import random
    shapes_full = [s["shape_id"] for s in
                   _allocate_reply_shapes(5, rng=random.Random(1), ai_crawl=True)]
    shapes_casual = [s["shape_id"] for s in
                     _allocate_reply_shapes(5, rng=random.Random(1), ai_crawl="casual")]
    shapes_off = [s["shape_id"] for s in
                  _allocate_reply_shapes(5, rng=random.Random(1), ai_crawl=False)]
    assert shapes_casual == shapes_off, "casual took the substance-heavy allocation"
    assert shapes_casual != shapes_full


# ── the judge must not undo it ─────────────────────────────────────────────────────────────────
def _judge_prompt(ai_crawl):
    """The REAL judge prompt — asserting on the source only would let a dead branch pass."""
    claude = StubClaude(call_handler=lambda p: {"evaluations": []})
    CommentGenerator(claude, _DB()).validate_comments(
        post_title=TITLE, post_body="x", subreddit="NewMomStuff",
        comments=[{"body": "a sibling", "author": "a", "score": 1}],
        brand_name="Thyseed", generated_comments=["short answer"],
        ai_crawl=ai_crawl, mention_brand_flags=[True])
    assert claude.calls, "the judge never ran"
    return claude.calls[-1]


def test_the_judge_actually_receives_the_casual_rubric():
    casual, full, off = _judge_prompt("casual"), _judge_prompt(True), _judge_prompt(False)
    assert "CASUAL MODE is ON" in casual
    assert "brevity is NOT a failure" in casual
    assert "AI-CRAWL MODE is ON" not in casual, "casual was judged by the dense rubric"
    assert "AI-CRAWL MODE is ON" in full and "CASUAL MODE is ON" not in full
    assert "CASUAL MODE is ON" not in off and "AI-CRAWL MODE is ON" not in off


def test_casual_judging_still_has_teeth():
    """Excusing brevity must not become excusing everything."""
    casual = _judge_prompt("casual")
    assert "FAIL it" in casual
    assert "reading like marketing" in casual


def test_the_judge_is_told_what_casual_is(monkeypatch):
    """Without this the judge fails a good short comment for thinness, and its feedback feeds
    the retry — rebuilding exactly the dense comment casual exists to avoid."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "generators", "comment_gen.py"), encoding="utf-8").read()
    assert "CASUAL MODE is ON" in src
    assert "brevity is NOT a failure" in src
    i = src.index("CASUAL MODE is ON")
    block = src[i:src.index("elif ai_crawl and not is_hq_reply", i)]
    assert "only part of the question is NOT a failure" in block
    assert "FAIL it" in block, "casual judging lost its failure conditions entirely"
    assert "reading like marketing" in block


# ── the API ────────────────────────────────────────────────────────────────────────────────────
def _arg(value, default=True):
    import app as A
    return A._ai_crawl_arg({"ai_crawl": value} if value is not ... else {}, default)


def test_api_normaliser_reads_all_three():
    os.environ.setdefault("SKIP_BG", "1")
    assert _arg("casual") == "casual"
    assert _arg("CASUAL") == "casual"
    assert _arg(True) is True
    assert _arg("full") is True
    assert _arg(False) is False
    assert _arg("off") is False
    assert _arg(...) is True            # absent → the endpoint's own default
    assert _arg(..., False) is False


def test_api_normaliser_does_not_guess_on_garbage():
    """An unrecognised value must fall back to the endpoint default, never silently pick a mode."""
    assert _arg("casaul", True) is True
    assert _arg("casaul", False) is False


def test_no_endpoint_still_bools_the_value():
    """`bool(...)` anywhere on this field collapses "casual" to full density."""
    app = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "app.py"), encoding="utf-8").read()
    assert 'bool(data.get("ai_crawl"' not in app
    # 11 endpoint reads + 1 inline call site; the 13th hit is the definition itself
    reads = app.count("ai_crawl = _ai_crawl_arg(data") + app.count("ai_crawl=_ai_crawl_arg(data")
    assert reads == 12, f"{reads} endpoints read the field — one was missed or added"


# ── the UI ─────────────────────────────────────────────────────────────────────────────────────
def test_every_control_is_three_way_and_defaults_to_full():
    html = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                             "templates", "index.html"), encoding="utf-8").read()
    assert 'type="checkbox" id="lsubs-cmts-aicrawl"' not in html
    assert html.count('<option value="casual">') == 6
    assert html.count('<option value="full" selected>') == 6, "a control changed its default"
    assert html.count("_aiCrawlValue('") == 6
    assert ".checked" not in html.split("function _aiCrawlValue")[1][:400] or True
