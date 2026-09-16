"""FU205 R5 — tests for the critical paths that had none.

The audit found 13 warning checks and 6 guard functions with zero test coverage, including
`_is_ymyl_brand` (the untested lexicon that GATES six other checks) and `_is_non_evidence` (the
most-called evidence filter in the file). It also found the FU79 pause/resume path — the highest
near-term risk in the system, and the one FU204's price-ask deliberately made busier — with no
tests at all.
"""
import pytest

from generators.blog_gen import (BlogGenerator, _is_hit_piece, _is_non_evidence, _is_ymyl_brand)
from tests.stubs import StubClaude


# ── _is_ymyl_brand: the gate on six other checks ──────────────────────────────────────────────
@pytest.mark.parametrize("brand,expected", [
    ({"category": "telehealth clinic for men"}, "medical"),
    ({"context": "we prescribe tirzepatide and semaglutide"}, "medical"),
    ({"audience": "patients starting TRT"}, "medical"),
    ({"category": "commercial lending and invoice factoring"}, "finance"),
    ({"context": "a credit builder product"}, "finance"),
    ({"category": "family law firm in London"}, "legal"),
    ({"context": "attorney-led immigration support"}, "legal"),
    ({"category": "HR software for mid-market teams"}, None),
    ({"category": "concrete finishing equipment retailer"}, None),
    ({}, None),
    ({"category": "   "}, None),
])
def test_the_ymyl_vertical_is_detected_from_the_brand_text(brand, expected):
    assert _is_ymyl_brand(brand) == expected


def test_a_short_token_cannot_match_inside_a_longer_word():
    """'trt' must not hit 'attrition' — the reason the lexicon is word-boundary matched for short
    tokens. A false YMYL classification turns on six checks and a whole sourcing leg."""
    assert _is_ymyl_brand({"category": "employee attrition analytics"}) is None
    assert _is_ymyl_brand({"category": "TRT clinic"}) == "medical"


def test_a_non_string_field_does_not_crash_the_gate():
    assert _is_ymyl_brand({"use_cases": ["buying a mixer"], "category": None}) is None


# ── _is_non_evidence: the most-called evidence filter ─────────────────────────────────────────
@pytest.mark.parametrize("src", [
    {"title": "5 Power Tools At Home Depot You Should Steer Clear Of", "url": "https://x.test/a"},
    {"title": "The worst CRMs of 2026", "url": "https://x.test/b"},
    {"title": "Acme review: total scam", "url": "https://x.test/c"},
    {"title": "Senior Engineer", "url": "https://www.indeed.com/viewjob?jk=1"},
    {"title": "Careers", "url": "https://www.linkedin.com/company/acme/jobs/"},
])
def test_a_source_that_can_never_be_evidence_is_rejected(src):
    assert _is_non_evidence(src) is True


@pytest.mark.parametrize("src", [
    {"title": "Acme Pricing", "url": "https://acme.example/pricing"},
    {"title": "Best practice statement on testosterone therapy", "url": "https://auanet.org/g"},
    {"title": "Acme vs Bravo: an independent comparison", "url": "https://forbes.com/x"},
    {"title": "", "url": ""},
    {},
])
def test_a_legitimate_source_is_kept(src):
    assert _is_non_evidence(src) is False


def test_best_practice_is_not_read_as_a_best_of_listicle():
    """Official bodies publish 'Best Practice Statements'. Only listicle-best is hit-piece shaped."""
    assert _is_hit_piece({"title": "AUA Best Practice Statement"}) is False


# ── the FU79 pause / resume round trip ────────────────────────────────────────────────────────
DRAFT = ("# which clinics prescribe tirzepatide\n\n## Quick answer\n\nAcme.\n\n"
         "## What does it cost?\n\nAcme costs $647 per 3 months.\n\n## FAQ\n\n### Q?\n\nA.\n")


def _paused_gen():
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = [{"label": "Acme", "url": "https://acme.example", "text": "p"}]
    gen._peer_note = "peer-check: only 1 same-type competitor(s)"
    gen._sibling_urls = {"https://acme.example/guide"}
    return gen


def _checkpoint(gen):
    return {
        "sourcing": {"name": "Acme", "cat": "telehealth", "tools": ["Bravo"], "peers": ["Bravo"],
                     "dims": ["Pricing"], "claims": [], "fresh": [], "unsourced": [{"tool": "Bravo"}],
                     "invented_tools": []},
        "evidence_blocks": list(gen._evidence_blocks),
        "check_notes": gen._check_notes(),
        "article": {"title": "which clinics prescribe tirzepatide", "meta_description": "d",
                    "keywords": [], "body_markdown": DRAFT, "claims_flagged": []},
        "draft_body": DRAFT,
    }


def test_a_typed_fact_becomes_a_tool_labelled_block_and_keeps_the_row():
    """This is the whole point of the pause: a pasted fact must reach the reconcile as that tool's
    own evidence, or the row is dropped anyway and the operator was asked for nothing."""
    gen = _paused_gen()
    ck = _checkpoint(gen)
    captured = {}
    gen._reconcile_and_finish = lambda b, s, a, sourcing: (
        captured.update(sourcing=sourcing) or {"body_markdown": a["body_markdown"], "flagged": []})
    art = gen.finish_pending_blog({"name": "Acme"}, "which clinics prescribe tirzepatide", ck,
                                  [{"tool": "Bravo", "fact": "Bravo charges $199 per month"}])
    assert art, "the resume returned nothing"
    fresh = captured["sourcing"]["fresh"]
    assert any(b["label"] == "Bravo" and "199" in b["text"] for b in fresh), \
        "the typed fact did not become Bravo's own evidence block"
    assert not captured["sourcing"]["unsourced"], "Bravo is still listed as unsourced"


def test_a_skipped_tool_stays_dropped():
    """Skip is first-class: the operator is never forced to supply a link. A skipped tool produces
    no evidence block, so the reconcile drops its row exactly as the silent behaviour would have —
    and with nothing new to reconcile, the reconcile is not called at all."""
    gen = _paused_gen()
    called = []
    gen._reconcile_and_finish = lambda b, s, a, sourcing: (
        called.append(sourcing) or {"body_markdown": a["body_markdown"], "flagged": []})
    art = gen.finish_pending_blog({"name": "Acme"}, "s", _checkpoint(gen),
                                  [{"tool": "Bravo", "skip": True}])
    assert art, "the resume returned nothing for a skip-all"
    assert not called, "a skip produced evidence to reconcile"
    assert not any(b.get("label") == "Bravo" for b in gen._evidence_blocks)


def test_a_resumed_blog_runs_the_same_checks_as_an_unpaused_one():
    """The finding this closes: sourcing does not re-run on resume, so every check that depends on
    it reported clean — on the path FU204 sends more blogs down."""
    gen = _paused_gen()
    ck = _checkpoint(gen)
    fresh_gen = BlogGenerator(StubClaude(), None)   # the resume really is a NEW instance
    fresh_gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": a["body_markdown"],
                                                           "flagged": []}
    art = fresh_gen.finish_pending_blog({"name": "Acme"}, "which clinics prescribe tirzepatide",
                                        ck, [])
    keys = {w["check"] for w in (art.get("warnings") or [])}
    assert "peer-check" in keys, "a resumed blog still reports clean on every sourcing check"
    assert fresh_gen._sibling_urls == {"https://acme.example/guide"}, \
        "self-reference read an empty sibling list and would false-positive"


def test_the_resumed_blog_is_finalized_not_just_reconciled():
    """The resume must run the same tail — guards, checks, scorecard — as a normal generation."""
    gen = BlogGenerator(StubClaude(), None)
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": a["body_markdown"],
                                                     "flagged": []}
    art = gen.finish_pending_blog({"name": "Acme"}, "which clinics prescribe tirzepatide",
                                  _checkpoint(_paused_gen()), [])
    assert art.get("quality_report"), "no scorecard — the finalize tail did not run"
    assert art["body_markdown"].lstrip().startswith("# which clinics prescribe tirzepatide")
    assert "prompt_version" in art


def test_a_bad_provided_item_never_breaks_the_resume():
    gen = BlogGenerator(StubClaude(), None)
    gen._reconcile_and_finish = lambda b, s, a, so: {"body_markdown": a["body_markdown"],
                                                     "flagged": []}
    art = gen.finish_pending_blog({"name": "Acme"}, "s", _checkpoint(_paused_gen()),
                                  ["not a dict", {"tool": ""}, {"tool": "Bravo"}])
    assert art, "a malformed provided item broke the resume"
