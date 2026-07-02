"""FU54 Tier-0 tests — pure, deterministic, $0 (no API, no network).

They pin the guarantees that stop the per-round blog quality DECLINE:
  - a dropped substantive section is restored (substance can't silently vanish);
  - a concrete stat that went missing is flagged;
  - an authoritative `official ·` source is force-kept in ## Sources even if uncited;
  - a brand-promo FAQ is excluded from FAQPage schema (kept topic-focused);
  - www / non-www domains normalize equal (so vendor pages aren't mislabelled third-party);
  - the generation cost math is correct.
"""
import os
import sqlite3

import pytest

from generators.base import ClaudeClient
from generators.blog_gen import BlogGenerator, _norm_domain, _parse_faq_pairs, build_blog_jsonld
from tests.stubs import StubClaude


def _gen():
    return BlogGenerator(StubClaude(), None)


DRAFT = """## Quick answer
Short answer.

## TikTok's labeling rules
You must label AI-generated content. Enforcement: 8,600 accounts and 51,000 videos actioned.

## Pre-posting checklist
- Label the video
- Use royalty-free audio

## FAQ
### Is AI music safe?
Yes.
"""


# --- Change 2: substance guard --------------------------------------------------
def test_restore_dropped_sections_brings_back_deleted():
    gen = _gen()
    revised = "## Quick answer\nShort answer.\n\n## FAQ\n### Is AI music safe?\nYes.\n"
    out = gen._restore_dropped_sections(DRAFT, revised)
    assert "TikTok's labeling rules" in out
    assert "Pre-posting checklist" in out
    assert "8,600" in out and "51,000" in out


def test_restore_noop_when_nothing_dropped():
    gen = _gen()
    assert gen._restore_dropped_sections(DRAFT, DRAFT) == DRAFT


def test_restore_inserts_before_sources_section():
    gen = _gen()
    revised = "## Quick answer\nHi.\n\n## Sources\n- [S1] x — <https://x.com>\n"
    out = gen._restore_dropped_sections(DRAFT, revised)
    assert out.index("TikTok's labeling rules") < out.index("## Sources")


def test_dropped_stats_flags_missing_numbers():
    gen = _gen()
    revised = "## Quick answer\nShort answer.\n"
    missing = gen._dropped_stats(DRAFT, revised)
    assert "8,600" in missing and "51,000" in missing


# --- Change 1c: force-keep the authoritative source ------------------------------
def test_rebuild_sources_force_keeps_official_uncited():
    gen = _gen()
    gen._evidence_blocks = [
        {"label": "official · TikTok AI-content policy",
         "url": "https://www.tiktok.com/legal/ai-content", "text": "Must label realistic AI content."},
    ]
    out = gen._rebuild_sources("## Quick answer\nYou must label AI-generated audio.\n")  # no [S#]
    assert "## Sources" in out
    assert "tiktok.com/legal/ai-content" in out
    assert "official · TikTok AI-content policy" in out


def test_rebuild_sources_force_keeps_community_uncited():
    gen = _gen()
    gen._evidence_blocks = [
        {"label": "community discussion · r/x thread",
         "url": "https://reddit.com/r/x/comments/1/", "text": "t"},
    ]
    out = gen._rebuild_sources("## Quick answer\nHi.\n")
    assert "reddit.com/r/x" in out


def test_rebuild_sources_drops_uncited_thirdparty():
    gen = _gen()
    gen._evidence_blocks = [
        {"label": "third-party · Some Review", "url": "https://g2.com/x", "text": "t"},
    ]
    out = gen._rebuild_sources("## Quick answer\nHi.\n")
    assert "## Sources" not in out   # not cited, not forced → left untouched


# --- Change 3b: FAQPage schema hygiene ------------------------------------------
V8_FAQ_BODY = """## Quick answer
Some answer.

## FAQ
### What AI music generators won't get your TikTok monetization flagged?
Original royalty-free tracks are safest.

### Is AI-generated music royalty-free?
Usually, on paid plans.

### Can I use AI Inspo music on TikTok monetized videos?
Yes.

### Does using an AI cover generator risk a TikTok copyright flag?
Potentially.

### What is the cheapest AI Inspo plan that includes commercial use?
The Lite plan.
"""


def test_parse_faq_pairs_counts():
    assert len(_parse_faq_pairs(V8_FAQ_BODY)) == 5


def test_faqpage_excludes_brand_promo_questions():
    blog = {"title": "t", "body_markdown": V8_FAQ_BODY, "created_at": "2026-07-02"}
    brand = {"name": "AI Inspo", "domain_url": "ai-inspo.com"}
    graph = build_blog_jsonld(blog, brand)["@graph"]
    faqpage = [n for n in graph if n.get("@type") == "FAQPage"]
    assert faqpage, "expected a FAQPage node"
    questions = [q["name"] for q in faqpage[0]["mainEntity"]]
    assert not any("ai inspo" in q.lower() for q in questions), questions   # v8's two promos gone
    assert any("royalty-free" in q.lower() for q in questions)              # topic Qs survive
    assert len(questions) == 3


# --- Change 4a: domain normalization --------------------------------------------
def test_norm_domain_strips_scheme_path_and_www():
    assert _norm_domain("https://www.suno.ai/pricing") == "suno.ai"
    assert _norm_domain("suno.ai") == "suno.ai"
    assert _norm_domain("http://Suno.AI/") == "suno.ai"
    # www / non-www compare EQUAL — what makes a vendor page match its resolved domain (both directions)
    assert _norm_domain("www.suno.ai") == _norm_domain("suno.ai") == "suno.ai"
    assert _norm_domain("https://tryprofound.com") != _norm_domain("profound.com")


# --- Change 6f: cost math -------------------------------------------------------
def test_usage_cost_math():
    c = ClaudeClient("dummy-key")   # constructor makes no network call
    c.model = "claude-sonnet-4-6"
    c._usage = {"input_tokens": 1_000_000, "output_tokens": 200_000, "web_search_requests": 10}
    # 1M in @ $3  +  0.2M out @ $15  +  10 * $0.01  =  3 + 3 + 0.10  =  6.10
    assert abs(c.usage_cost() - 6.10) < 1e-6
    c.reset_usage()
    assert c.usage_cost() == 0.0


# --- Golden anchor: the real v8 blog, if it's in the local DB -------------------
def test_v8_body_from_db_has_no_brand_promo_in_schema():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(root, "strategy_bot.db")
    if not os.path.exists(db_path):
        pytest.skip("strategy_bot.db not present")
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        cols = [r[1] for r in con.execute("PRAGMA table_info(blogs)")]
        if "body_markdown" not in cols:
            pytest.skip("no blogs table / body_markdown column")
        row = con.execute(
            "SELECT b.*, br.name AS brand_name FROM blogs b "
            "LEFT JOIN brands br ON br.id = b.brand_id "
            "WHERE b.body_markdown LIKE '%FAQ%' "
            "AND (b.title LIKE '%TikTok%' OR b.seed LIKE '%TikTok%' OR b.title LIKE '%monetization%') "
            "ORDER BY b.id DESC LIMIT 1").fetchone()
    finally:
        con.close()
    if not row or not (row["brand_name"] or "").strip():
        pytest.skip("no matching TikTok blog with a brand in strategy_bot.db")
    blog = dict(row)
    brand = {"name": blog["brand_name"], "domain_url": ""}
    graph = build_blog_jsonld(blog, brand)["@graph"]
    faqpage = [n for n in graph if n.get("@type") == "FAQPage"]
    if not faqpage:
        pytest.skip("that blog produced no FAQPage")
    bn = brand["name"].lower()
    qs = [q["name"].lower() for q in faqpage[0]["mainEntity"]]
    assert not any(bn in q for q in qs), f"brand-promo FAQ leaked into schema: {qs}"
