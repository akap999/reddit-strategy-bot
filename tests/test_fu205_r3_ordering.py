"""FU205 R3 — check the body that actually ships, and never report a repair that gets erased.

Two findings from the audit's "checks that cannot fire" section:

  * `_verify_final_article` MUTATED the body after all ~20 read-only checks had run. After a prose
    repair it re-runs `_rebuild_sources`, so a comparison table could be narrowed — or removed by the
    FU204 collapse guard — with no toast, because `_table_punt_note`'s only reader had already run.
    The Sources-derived checks likewise validated a `## Sources` block that was then rebuilt.
  * `_verify_repair_gate` screened the fix against the two CELL-level punt regexes only. A PROSE
    punt passed the gate, was reported as `applied`, and was then silently deleted by `_scrub_punts`
    — leaving a hole exactly where the defect had been.
"""
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

BRAND = {"name": "Acme", "domain_url": "https://acme.example"}
BODY = ("# which agency\n\n## Quick answer\n\nAcme is a strong fit.\n\n"
        "## What does it cost?\n\nAcme costs $29 per month.\n")


def _gen(**kw):
    gen = BlogGenerator(StubClaude(), None, **kw)
    gen._evidence_blocks = []
    return gen


# ── ordering ──────────────────────────────────────────────────────────────────────────────────
def test_the_checks_run_after_the_verification_pass_has_finished_changing_the_body():
    """The body a check reads must be the body that ships."""
    seen = {}
    gen = _gen()

    def _verify(brand, article):
        article["body_markdown"] = article["body_markdown"] + "\n## Added by the repair\n\nX.\n"
        return {"applied": [{"kind": "typo"}]}
    gen._verify_final_article = _verify
    real_qr = gen._quality_report
    gen._quality_report = lambda article, brand, link_targets=None: (
        seen.update(body=article["body_markdown"]) or real_qr(article, brand, link_targets))

    art = {"title": "t", "meta_description": "d", "body_markdown": BODY}
    gen._finalize_article(BRAND, "which agency", art, BODY, with_linkedin=False)
    assert "## Added by the repair" in seen["body"], "a check judged a body the pass then changed"
    assert "## Added by the repair" in art["body_markdown"]


def test_a_table_dropped_by_the_post_repair_rebuild_still_reaches_the_operator():
    """`_resolve_table_punts` RESETS its note on every call, and the verification pass re-runs the
    rebuild after a prose repair. A column dropped by the FIRST rebuild must not be silently
    overwritten by a second, quieter one."""
    gen = _gen()

    def _verify(brand, article):
        # what the real pass does after applying a repair: re-run the rebuild. This one finds
        # nothing left to drop, so it clears the note the first rebuild set.
        gen._table_punt_note = ""
        return {"applied": [{"kind": "typo"}]}
    gen._verify_final_article = _verify

    # a table whose only dimension column has a gap → the first rebuild drops it and sets the note
    body = (BODY + "\n| Firm | Rating |\n| --- | --- |\n| Acme | A+ |\n| Bravo |  |\n\n## Next\n\nx.\n")
    art = {"title": "t", "meta_description": "d", "body_markdown": body}
    gen._finalize_article(BRAND, "which agency", art, body, with_linkedin=False)
    assert any("comparison table" in w["detail"] for w in art.get("warnings", [])), \
        "the table drop was reported by neither rebuild"


def test_the_verification_report_still_reaches_the_article():
    gen = _gen()
    gen._verify_final_article = lambda brand, article: {"n_fixed": 2, "applied": []}
    art = {"title": "t", "meta_description": "d", "body_markdown": BODY}
    gen._finalize_article(BRAND, "which agency", art, BODY, with_linkedin=False)
    assert art["verify_report"]["n_fixed"] == 2


# ── the repair gate ───────────────────────────────────────────────────────────────────────────
def test_a_prose_punt_repair_is_refused_instead_of_applied_then_erased():
    gen = _gen()
    body = "Acme is a strong fit. Pricing is straightforward.\n"
    quote = "Pricing is straightforward."
    for fix in ("Pricing varies by plan — check their site for current pricing.",
                "Pricing is not specified in the sourced facts."):
        why = gen._verify_repair_gate(body, quote, fix, BRAND)
        assert why, f"a repair the scrub would delete was allowed: {fix!r}"
        # …and the scrub really would have deleted it, which is why the gate has to catch it
        assert gen._scrub_punts(fix).strip() != fix.strip()


def test_a_repair_that_narrates_an_edit_is_refused():
    gen = _gen()
    body = "Acme is a strong fit. Pricing is straightforward.\n"
    why = gen._verify_repair_gate(
        body, "Pricing is straightforward.",
        "Bravo's row is removed per sourcing rules.", BRAND)
    assert "narrates an editing decision" in why


def test_a_clean_repair_is_still_allowed():
    gen = _gen()
    body = "Acme is a strong fit. Acme costs $29 per month.\n"
    assert gen._verify_repair_gate(
        body, "Acme costs $29 per month.", "Acme costs $29 per month, billed monthly.", BRAND) == ""


def test_the_original_six_refusals_still_fire():
    """R3 widens the gate; it must not weaken what was already refused."""
    gen = _gen()
    body = "# which agency\n\nAcme costs $29 per month. [S1]\n"
    q = "Acme costs $29 per month. [S1]"
    assert "citations" in gen._verify_repair_gate(body, q, "Acme costs $29 per month.", BRAND)
    assert "em-dash" in gen._verify_repair_gate(body, q, "Acme costs $29 — monthly. [S1]", BRAND)
    assert "dropped" in gen._verify_repair_gate(body, q, "Acme costs a bit. [S1]", BRAND)
    assert "not found exactly once" in gen._verify_repair_gate(body, "nope", "x", BRAND)
