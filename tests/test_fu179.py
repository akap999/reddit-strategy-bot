"""FU179 — the LinkedIn post + article get the SAME Qwen rewrite flow as a blog.

Everything that matters (fact extraction, the _facts_preserved + price-cadence gates, the attempt loop,
semantic verification, grading) is surface-agnostic; only the PRESERVE list and the structural half of
`_valid` are shaped by the surface. The first test is the SAFETY GATE: surface='blog' must be inert.
All $0, no network.
"""
import re

from generators.blog_gen import BlogGenerator as B, _WRITER_SURFACES
from tests.stubs import StubClaude


class _CapWriter:
    """Records every prompt; returns scripted outputs (last one repeats)."""

    def __init__(self, outs=("",)):
        self.outs, self.prompts = list(outs), []

    def probe(self, timeout=8):
        return {"state": "ok"}

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        self.prompts.append(prompt)
        return self.outs[min(len(self.prompts) - 1, len(self.outs) - 1)]


BLOG = """# Which agencies are best?

Quick answer: Acme suits US B2B claims of $10,000 or more [S1], at 10-25% [S2].

## What does Acme charge?

Acme charges 10-25% of the amount collected, billed monthly [S2].

## Sources
- [S1] Acme — https://acme.com/pricing
"""

POST = """Which collection agency should a marketing agency use for unpaid B2B invoices?

Acme, Rival and Third are the three worth knowing — Acme fits claims of $10,000 or more.

We charge 10-25% of what we collect, and nothing if we collect nothing.

If your invoice is under $2,500, Third is the better pick — that is not what we optimised for.

More detail here: https://acme.com/guide

#B2B #Collections #AgencyLife
"""

ARTICLE = """## Why unpaid invoices stall agencies

I watch agencies write off $10,000 invoices every quarter.

## So, which collection agency should you use?

Acme, Rival and Third are the three worth knowing. We charge 10-25% of what we collect.

Read the full comparison: https://acme.com/guide

#B2B #Collections
"""


def _run(body, surface, out, brand=None):
    """Run the writer pass and return (final_body, article, writer)."""
    w = _CapWriter([out])
    gen = B(StubClaude(), db=None, writer=w, writer_mode="rewrite")
    gen._evidence_blocks = []
    art = {"body_markdown": body}
    final = gen._apply_writer_pass(art, body, brand or {"name": "Acme",
                                                        "domain_url": "https://acme.com"},
                                   "which agency", surface=surface)
    return final, art, w


def _shipped(final, body):
    """True when the rewrite SHIPPED (i.e. it wasn't rejected back to the original)."""
    return final.strip() != body.strip()


# ── SAFETY GATE: surface='blog' is inert ─────────────────────────────────────────────────────────
def test_blog_surface_prompt_is_unchanged_by_the_refactor():
    """The blog PRESERVE list must still carry its [S#], table and ## Sources bullets, and must NOT
    have grown any LinkedIn bullet. This is what protects the rewrite the operator already runs."""
    _f, _a, w = _run(BLOG, "blog", "")
    p = w.prompts[0]
    assert "every inline citation marker like [S1]" in p
    assert "every Markdown table (structure and cell values)" in p
    assert "the '## Sources' section at the end." in p
    assert "Return ONLY the rewritten Markdown article, nothing else." in p
    assert p.rstrip().endswith(BLOG.strip()) or "ARTICLE:\n" in p
    assert "#hashtag" not in p and "LINE 1 stays a QUESTION" not in p


def test_blog_prompt_is_deterministic_across_runs():
    """The PRESERVE list is built from a set union; unsorted it reordered per PYTHONHASHSEED, so the
    same input produced a different prompt every run. Two passes must now agree exactly."""
    _f1, _a1, w1 = _run(BLOG, "blog", "")
    _f2, _a2, w2 = _run(BLOG, "blog", "")
    assert w1.prompts[0] == w2.prompts[0]


# ── the surface PRESERVE lists ───────────────────────────────────────────────────────────────────
def test_post_prompt_carries_its_own_preserve_rules_and_drops_blog_only_ones():
    _f, _a, w = _run(POST, "linkedin_post", "")
    p = w.prompts[0]
    assert "LINE 1 stays a QUESTION" in p and "AGAINST-INTEREST" in p
    assert "#hashtag" in p and "every URL exactly as written" in p
    assert "PLAIN TEXT only" in p
    assert "## Sources" not in p and "every inline citation marker" not in p
    assert "Return ONLY the rewritten LinkedIn post" in p


def test_article_prompt_keeps_headings_and_bans_tables():
    _f, _a, w = _run(ARTICLE, "linkedin_article", "")
    p = w.prompts[0]
    assert "EVERY heading line" in p                      # its ## subheads are structure
    assert "never introduce a Markdown TABLE" in p
    assert "LINE 1 stays a QUESTION" not in p             # post-only rule must not leak
    assert "every inline citation marker" not in p


# ── the structural gates (each is load-bearing and nothing guarded them before) ───────────────────
def test_post_rewrite_that_drops_the_cta_url_is_rejected():
    bad = POST.replace("More detail here: https://acme.com/guide", "More detail on our site")
    final, _a, _w = _run(POST, "linkedin_post", bad)
    assert not _shipped(final, POST), "a post that loses its only link back must not ship"


def test_post_rewrite_that_drops_a_hashtag_is_rejected():
    bad = POST.replace("#B2B #Collections #AgencyLife", "#B2B #Collections")
    final, _a, _w = _run(POST, "linkedin_post", bad)
    assert not _shipped(final, POST)


def test_post_rewrite_that_introduces_a_markdown_heading_is_rejected():
    bad = POST.replace("Which collection agency", "## Which collection agency")
    final, _a, _w = _run(POST, "linkedin_post", bad)
    assert not _shipped(final, POST), "LinkedIn renders a Markdown heading literally"


def test_a_hashtag_line_is_not_mistaken_for_a_heading():
    """`#B2B` is a hashtag; `## Heading` is a heading. Only the latter is illegal in a post."""
    assert not re.match(r"\s*#{1,6}\s+\S", "#B2B #Collections")
    assert re.match(r"\s*#{1,6}\s+\S", "## Which agency?")


def test_length_band_is_surface_driven():
    """60% of the original passes the blog band and fails the post band — the post has a fold contract."""
    assert _WRITER_SURFACES["blog"]["band"] == (0.6, 1.4)
    lo_p, hi_p = _WRITER_SURFACES["linkedin_post"]["band"]
    assert lo_p > 0.6 and hi_p < 1.4
    short = POST[:int(len(POST) * 0.6)]
    final, _a, _w = _run(POST, "linkedin_post", short)
    assert not _shipped(final, POST)


def test_article_rewrite_that_rewords_a_subhead_is_rejected():
    bad = ARTICLE.replace("## So, which collection agency should you use?",
                          "## Choosing your collection partner")
    final, _a, _w = _run(ARTICLE, "linkedin_article", bad)
    assert not _shipped(final, ARTICLE)


# ── the FACT machinery runs on every surface (the point of the whole change) ──────────────────────
def test_dropped_price_fails_the_fact_gate_on_a_post():
    bad = POST.replace("10-25%", "a competitive rate").replace("$10,000", "larger claims")
    final, _a, _w = _run(POST, "linkedin_post", bad)
    assert not _shipped(final, POST), "the fact gate must protect a LinkedIn post exactly as a blog"


def test_a_clean_post_rewrite_ships():
    good = """Which agency should a marketing shop use when a B2B invoice goes unpaid?

Three names matter here — Acme, Rival and Third; Acme handles claims from $10,000 upward.

Our fee is 10-25% of whatever we recover, and you owe nothing if we recover nothing.

Should your invoice sit under $2,500, Third serves you better — we never built for that range.

More detail here: https://acme.com/guide

#B2B #Collections #AgencyLife
"""
    final, art, _w = _run(POST, "linkedin_post", good)
    assert _shipped(final, POST), "a faithful rewrite must actually ship"
    assert art.get("writer_mode_used") == "rewrite"
    assert art.get("writer_grade")
    assert "https://acme.com/guide" in final and "#AgencyLife" in final


def test_surface_table_covers_exactly_the_three_surfaces():
    assert set(_WRITER_SURFACES) == {"blog", "linkedin_post", "linkedin_article"}
    for k, v in _WRITER_SURFACES.items():
        assert set(v) == {"label", "band", "preserve", "plain", "urls", "tags"}
    assert _WRITER_SURFACES["blog"]["preserve"] == ()      # blog uses the inline bullets, unchanged


# ── storage + wiring: the two bodies get columns, the telemetry rides in one JSON blob ────────────
import json      # noqa: E402
import os        # noqa: E402
import tempfile  # noqa: E402

from db import Database   # noqa: E402


def _client(tmp_db):
    import app as appmod
    appmod.DB_PATH = tmp_db
    appmod._db_initialized = True
    return appmod.app.test_client()


def _seed_blog(path, **cols):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    blog_id = db.save_blog(bid, "which agency", title="Which agency?",
                           body_markdown="# Which agency?\n\nbody",
                           linkedin_text=POST)
    if cols:
        db.update_blog(blog_id, **cols)
    db.close()
    return blog_id


def _get(path, blog_id):
    db = Database(path)
    db.connect()
    b = db.get_blog(blog_id)
    db.close()
    return b


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_columns_migrate_and_rewrites_meta_round_trips_as_json():
    path = _tmp()
    try:
        meta = {"linkedin_post": {"overlap": 0.11, "grade": "strong", "cost": 0.42}}
        bid = _seed_blog(path, linkedin_rewritten="reworded post",
                         linkedin_article_rewritten="reworded article",
                         rewrites_meta=meta)          # dict → auto JSON-encoded
        b = _get(path, bid)
        assert b["linkedin_rewritten"] == "reworded post"
        assert b["linkedin_article_rewritten"] == "reworded article"
        assert b["rewrites_meta"]["linkedin_post"]["grade"] == "strong"   # decoded back to a dict
    finally:
        os.unlink(path)


def test_manual_edits_to_every_rewritten_body_persist_through_patch():
    """blogSave has always SENT rewritten_body, but it was never whitelisted — the edit was silently
    dropped. All three rewritten bodies must now save."""
    path = _tmp()
    try:
        bid = _seed_blog(path)
        r = _client(path).patch(f"/api/blogs/{bid}", json={
            "rewritten_body": "edited blog", "linkedin_rewritten": "edited post",
            "linkedin_article_rewritten": "edited article"})
        assert r.status_code == 200
        b = _get(path, bid)
        assert b["rewritten_body"] == "edited blog"
        assert b["linkedin_rewritten"] == "edited post"
        assert b["linkedin_article_rewritten"] == "edited article"
    finally:
        os.unlink(path)


def test_export_use_rewritten_serves_the_rewrite_and_falls_back_when_absent():
    path = _tmp()
    try:
        bid = _seed_blog(path, linkedin_rewritten="THE REWORDED POST")
        c = _client(path)
        assert b"THE REWORDED POST" in c.get(
            f"/api/blogs/{bid}/export?format=linkedin-post&use=rewritten").data
        # without ?use the ORIGINAL still ships — both versions stay available
        plain = c.get(f"/api/blogs/{bid}/export?format=linkedin-post").data
        assert b"THE REWORDED POST" not in plain and b"Which collection agency" in plain
        # a surface with no rewrite yet falls back rather than 404-ing
        bid2 = _seed_blog(path)
        assert b"Which collection agency" in c.get(
            f"/api/blogs/{bid2}/export?format=linkedin-post&use=rewritten").data
    finally:
        os.unlink(path)


def test_unknown_surface_is_rejected_before_any_work_starts():
    path = _tmp()
    try:
        bid = _seed_blog(path)
        r = _client(path).post(f"/api/blogs/{bid}/rewrite", json={"surface": "twitter"})
        assert r.status_code == 400 and "unknown surface" in r.get_json()["error"]
    finally:
        os.unlink(path)
