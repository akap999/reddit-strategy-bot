"""FU205 R6 + R7 — the three signals the pipeline never produced.

R6: past the $3 web-search ceiling, `search_sources` / `fetch_site_facts` / `find_official_domain`
    silently return []/"" and NOTHING told the operator. Every downstream symptom — thin sources,
    unsourced competitors, blank cells, punts, the mass-pause — then looks like a logic bug, and has
    been debugged as one for ~15 rounds.

R7: "answer-first" is the single property that earns the citation, and it was prompt-only for the
    blog body (deterministic for LinkedIn and YouTube only). And FU152's byline placeholder is what
    forces a human pass before publishing — FU204 stopped it being CORRUPTED, nothing stopped it
    being DELETED by one of the two body-rewriting calls.
"""
import pytest

from generators.base import ClaudeClient
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

BRAND = {"name": "Acme"}
BYLINE = "*[Add author byline before publishing]*"


def _gen(skipped=0):
    stub = StubClaude()
    stub._skipped = skipped
    gen = BlogGenerator(stub, None)
    gen._evidence_blocks = []
    return gen


def _finalize(body):
    gen = _gen()
    art = {"title": "t", "meta_description": "d", "body_markdown": body}
    gen._finalize_article(BRAND, "t", art, body, with_linkedin=False)
    return art


def _keys(art):
    return {w["check"] for w in art.get("warnings", [])}


# ── R6: budget starvation ─────────────────────────────────────────────────────────────────────
def _client():
    c = ClaudeClient("test-key")
    c.reset_usage()
    return c


def test_the_ceiling_counts_every_search_it_skips():
    c = _client()
    assert c.skipped_searches() == 0
    assert c._over_budget() is False and c.skipped_searches() == 0, "no ceiling → never counted"
    c.set_cost_ceiling(0.0)   # any spend at all is over
    assert c._over_budget() is True
    assert c._over_budget() is True
    assert c.skipped_searches() == 2


def test_reset_usage_clears_the_skip_count():
    c = _client()
    c.set_cost_ceiling(0.0)
    c._over_budget()
    assert c.skipped_searches() == 1
    c.reset_usage()
    assert c.skipped_searches() == 0, "a new generation inherits the last one's starvation"


def test_a_starved_generation_says_so():
    gen = _gen(skipped=7)
    body = f"# t\n\n{BYLINE}\n\n## Quick answer\n\nAcme.\n"
    art = {"title": "t", "meta_description": "d", "body_markdown": body}
    gen._finalize_article(BRAND, "t", art, body, with_linkedin=False)
    hits = [w for w in art["warnings"] if w["check"] == "budget-check"]
    assert hits, "the search budget ran out and nothing told the operator"
    assert "7 search(es) were SKIPPED" in hits[0]["detail"]


def test_a_healthy_generation_is_silent_about_the_budget():
    art = _finalize(f"# t\n\n{BYLINE}\n\n## Quick answer\n\nAcme.\n")
    assert "budget-check" not in _keys(art)


# ── R7: answer-first ──────────────────────────────────────────────────────────────────────────
STALLS = [
    "The honest answer is: it depends on your team size.",
    "Let us go through the three platforms one at a time.",
    "Let me break this down.",
    "It depends on what you need.",
    "There is no single right answer here.",
    "Before we dive in, some background.",
]


@pytest.mark.parametrize("stall", STALLS)
def test_a_question_heading_answered_with_a_stall_is_flagged(stall):
    body = f"# t\n\n{BYLINE}\n\n## Which platform should I use?\n\n{stall}\n\nMore text.\n"
    assert "answer-first" in _keys(_finalize(body))


def test_a_question_heading_answered_properly_is_silent():
    body = (f"# t\n\n{BYLINE}\n\n## Which platform should I use?\n\n"
            "Acme, Bravo and Delta are the three worth shortlisting, though only Acme finances "
            "in-house.\n")
    assert "answer-first" not in _keys(_finalize(body))


def test_a_statement_heading_is_not_judged():
    """The rule is about QUESTION headings — the chunk an engine lifts for a question."""
    body = f"# t\n\n{BYLINE}\n\n## Background\n\nIt depends on your setup.\n"
    assert "answer-first" not in _keys(_finalize(body))


def test_a_question_heading_followed_by_a_table_is_not_judged():
    body = (f"# t\n\n{BYLINE}\n\n## Which platform?\n\n| A | B |\n| --- | --- |\n| x | y |\n")
    assert "answer-first" not in _keys(_finalize(body))


def test_the_note_names_the_offending_headings():
    body = (f"# t\n\n{BYLINE}\n\n## Which platform should I use?\n\nIt depends.\n\n"
            "## What does it cost?\n\nLet me break this down.\n")
    art = _finalize(body)
    note = next(w["detail"] for w in art["warnings"] if w["check"] == "answer-first")
    assert "2 question heading(s)" in note
    assert "Which platform should I use?" in note


# ── R7: byline present ────────────────────────────────────────────────────────────────────────
def test_a_body_whose_byline_a_rewrite_deleted_is_flagged():
    body = "# t\n\n## Quick answer\n\nAcme is a strong fit.\n"
    assert "byline-check" in _keys(_finalize(body))


def test_the_byline_placeholder_is_accepted():
    assert "byline-check" not in _keys(_finalize(f"# t\n\n{BYLINE}\n\n## Quick answer\n\nAcme.\n"))


def test_a_real_reviewer_or_disclosure_line_is_accepted():
    body = ("# t\n\n*Reviewed by Jane Doe, MD · Disclosure: published by Acme*\n\n"
            "## Quick answer\n\nAcme.\n")
    assert "byline-check" not in _keys(_finalize(body))


def test_a_bold_line_is_not_mistaken_for_a_byline():
    body = "# t\n\n**Key takeaway**\n\n## Quick answer\n\nAcme.\n"
    assert "byline-check" in _keys(_finalize(body))
