"""FU291 — casual was outvoted by the instructions it couldn't see.

FU290 added a casual block that says "short is fine", "leave things out", "no listicles".
The output stayed formal. The block was never going to win, because three other things fire on
ANY truthy `ai_crawl` — and all three are concrete where the block is prose:

  1. the persona/structure override forced a pick from AI_CRAWL_REC_*, which contains
     "professional", "data_nerd" and **list_format** — a listicle, the exact shape the casual
     block bans;
  2. HQ-MAIN forced the root into a brand-first framing ('"X is what handles this"') in a
     fixed 50-90 words — marketing voice, on the one comment that matters most;
  3. the HQ length target asked for avg_chars 500 / max 800, while the intent classifier was
     separately setting avg_words to 25-50. The stats contradicted each other, and the char
     figures pulled long.

A whitelist and a number are countable; "write it the way a person would" is a judgement call.
The countable instruction wins every time — the same failure that produced the dense AI-crawl
block in the first place.

Retrievability is deliberately untouched: casual still has to restate the post's question in
its own words, still has to land on an answer, and still names the brand once with what it
actually did. What changed is voice, length and shape — none of which is what makes a comment
retrievable.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.comment_gen import (  # noqa: E402
    AI_CRAWL_REC_STRUCTURES,
    CASUAL_REC_PERSONAS,
    CASUAL_REC_STRUCTURES,
    CommentGenerator,
)
from tests.stubs import StubClaude  # noqa: E402

REC_TITLE = "best premium glass baby bottles in us"


class _DB:
    def get_subreddit(self, _sid):
        return {"id": 1, "name": "NewMomStuff", "domain": "parenting"}

    def __getattr__(self, _n):
        return lambda *a, **k: None


def _run(ai_crawl, n=30):
    """Return (structures, personas, prompts) actually chosen across n generations."""
    claude = StubClaude(call_handler=lambda p: {"generated_comments": ["b"],
                                                "strategies_used": ["s"]})
    g = CommentGenerator(claude, _DB())
    structures, personas = [], []
    for _ in range(n):
        out = g.generate_comments(
            post_title=REC_TITLE, post_body="four brands in and the venting is still wrong",
            subreddit="NewMomStuff", comments=[], brand_name="Thyseed",
            brand_context="baby bottles", num_comments=1, mention_brand_flags=[True],
            ai_crawl=ai_crawl)
        structures += out.get("_structures") or []
        personas += out.get("_personas") or []
    return set(structures), set(personas), claude.calls


# ── 1. the whitelist ───────────────────────────────────────────────────────────────────────────
def test_casual_never_gets_a_listicle_structure():
    """list_format is in the AI-crawl pool and directly contradicts the casual block."""
    structures, _, _ = _run("casual")
    assert "list_format" not in structures
    assert structures <= CASUAL_REC_STRUCTURES, f"escaped the casual pool: {structures}"


def test_casual_never_gets_a_formal_persona():
    _, personas, _ = _run("casual")
    assert "professional" not in personas and "data_nerd" not in personas
    assert personas <= CASUAL_REC_PERSONAS, f"escaped the casual pool: {personas}"


def test_full_mode_keeps_its_own_pool():
    """This tunes casual; it must not quietly change full."""
    structures, _, _ = _run(True)
    assert structures <= AI_CRAWL_REC_STRUCTURES
    assert "list_format" in structures or len(structures) >= 3


def test_the_casual_pool_can_still_answer():
    """Retrievability: the pool must retain shapes that actually deliver a recommendation,
    or a casual comment stops being findable for the query."""
    assert {"direct_answer", "short_punchy"} <= CASUAL_REC_STRUCTURES
    assert "list_format" not in CASUAL_REC_STRUCTURES
    assert "professional" not in CASUAL_REC_PERSONAS
    assert "switcher" not in CASUAL_REC_PERSONAS, "a comparison voice that swaps to a formal one"


def test_the_brand_mention_swap_stays_casual():
    """A comparison persona on a brand-mention slot is swapped out, and the default
    replacement pool contains "professional". The casual pools no longer FEED that branch, so
    this reaches it directly through slot_overrides — otherwise the guard is untested and the
    formal voice silently returns the moment anyone puts a comparison persona back in the pool.
    """
    claude = StubClaude(call_handler=lambda p: {"generated_comments": ["b"],
                                                "strategies_used": ["s"]})
    g = CommentGenerator(claude, _DB())
    got = set()
    for _ in range(25):
        out = g.generate_comments(
            post_title=REC_TITLE, post_body="x", subreddit="NewMomStuff", comments=[],
            brand_name="Thyseed", brand_context="baby bottles", num_comments=1,
            mention_brand_flags=[True], ai_crawl="casual",
            slot_overrides=[{"persona_id": "switcher"}])
        got |= set(out.get("_personas") or [])
    assert "switcher" not in got, "the swap did not fire — the test no longer reaches the guard"
    assert "professional" not in got, f"formal voice reinstated on a casual brand mention: {got}"


def test_the_brand_mention_swap_is_unchanged_for_full():
    claude = StubClaude(call_handler=lambda p: {"generated_comments": ["b"],
                                                "strategies_used": ["s"]})
    g = CommentGenerator(claude, _DB())
    got = set()
    for _ in range(25):
        out = g.generate_comments(
            post_title=REC_TITLE, post_body="x", subreddit="NewMomStuff", comments=[],
            brand_name="Thyseed", brand_context="baby bottles", num_comments=1,
            mention_brand_flags=[True], ai_crawl=True,
            slot_overrides=[{"persona_id": "switcher"}])
        got |= set(out.get("_personas") or [])
    assert got <= {"helper", "professional", "veteran_terse"}, got


# ── 2. the HQ-MAIN brand-first framing ─────────────────────────────────────────────────────────
def _hq_root_prompt(ai_crawl):
    claude = StubClaude(call_handler=lambda p: {"generated_comments": ["b"],
                                                "strategies_used": ["s"]})
    CommentGenerator(claude, _DB()).generate_comments(
        post_title=REC_TITLE, post_body="x", subreddit="NewMomStuff", comments=[],
        brand_name="Thyseed", brand_context="baby bottles", num_comments=1,
        mention_brand_flags=[True], ai_crawl=ai_crawl,
        hq_main=(ai_crawl is True))     # what generate_hq_comment now passes per mode
    return claude.calls[-1]


def test_casual_root_has_no_brand_first_framing():
    p = _hq_root_prompt("casual")
    assert "HQ-MAIN OVERRIDE" not in p
    assert "50-90 words" not in p


def test_full_root_still_has_it():
    # assert the stable heading, not one of the eight random framings inside it
    p = _hq_root_prompt(True)
    assert "HQ-MAIN OVERRIDE" in p


def test_hq_main_is_gated_on_full_not_truthy():
    """`is_main_local and ai_crawl` sends "casual" straight into the marketing framing."""
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "generators", "comment_gen.py"), encoding="utf-8").read()
    assert "hq_main=is_main_local and _is_full_crawl(ai_crawl)," in src
    assert "hq_main=is_main_local and ai_crawl," not in src


# ── 3. the length target ───────────────────────────────────────────────────────────────────────
def _hq_stats(ai_crawl, monkeypatch):
    """Capture the comment_stats generate_hq_comment hands to the root."""
    seen = {}
    claude = StubClaude(call_handler=lambda p: {"generated_comments": ["b"],
                                                "strategies_used": ["s"]})
    g = CommentGenerator(claude, _DB())
    real = g.generate_comments

    def _spy(**kw):
        if "comment_stats" in kw and "comment_stats" not in seen:
            seen["comment_stats"] = kw["comment_stats"]
        return real(**kw)

    monkeypatch.setattr(g, "generate_comments", _spy)
    import generators.comment_gen as CG
    monkeypatch.setattr(CG.time, "sleep", lambda *_a, **_k: None, raising=False)
    g.generate_hq_comment({"id": 1, "subreddit_id": 1, "title": REC_TITLE, "body": "x",
                           "storyline": "question", "suggested_post_day": 0, "intent": None,
                           "concept_checklist": None},
                          {"id": 1, "name": "Thyseed", "context": "baby bottles"},
                          num_replies=0, ai_crawl=ai_crawl)
    return seen.get("comment_stats") or {}


def test_casual_hq_root_is_targeted_short(monkeypatch):
    casual = _hq_stats("casual", monkeypatch)
    full = _hq_stats(True, monkeypatch)
    assert casual["avg_chars"] < full["avg_chars"]
    assert casual["max_chars"] < full["max_chars"]
    assert casual["avg_chars"] <= 260, f"still long: {casual}"


def test_full_hq_root_length_is_unchanged(monkeypatch):
    full = _hq_stats(True, monkeypatch)
    assert full["avg_chars"] == 500 and full["max_chars"] == 800


# ── retrievability is NOT what was loosened ────────────────────────────────────────────────────
def test_casual_still_carries_everything_retrieval_needs():
    """The operator asked for conversational AND retrievable. These three are what make a
    comment findable; none of them was touched."""
    _, _, prompts = _run("casual", n=1)
    p = prompts[-1]
    assert REC_TITLE in p, "lost the long-tail query it has to reinforce"
    assert "your own words" in p, "lost the restatement requirement"
    assert "land on one" in p, "lost the instruction to actually answer"
    assert "Thyseed" in p, "lost the brand mention"
