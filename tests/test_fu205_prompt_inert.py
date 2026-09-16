"""FU205 — THE INERT PROOF. Nothing else in this round ships until this passes.

R8 (consolidating the 52-rule article prompt) is DEFERRED by operator decision — re-confirmed after
reading the rule inventory. Every other FU205 change is plumbing, storage or a check, so "generation
is untouched" must be DEMONSTRATED, not claimed. These tests capture the three writer prompts from
fixed inputs (tests/prompt_capture.py) and compare them BYTE-FOR-BYTE against committed goldens.

If one of these fails, the diff is the answer: either an intended prompt change slipped into a
plumbing round (revert it), or R8 was deliberately opened (regenerate the goldens and say so in the
commit). Regenerate with:

    PYTHONPATH=. REGEN_PROMPT_GOLDENS=1 python3 -m pytest tests/test_fu205_prompt_inert.py -q
"""
import difflib
import os

import pytest

from tests.prompt_capture import capture

_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "prompts")
_VARIANTS = [
    "generate_article.minimal",   # the BASE template, every optional fragment inert
    "generate_article.rich",      # geo + qualifier + evidence + internal links + siblings + canonical facts
    "generate_article.ymyl",      # the YMYL AUTHORITY fragment
    "verify_claims",
    "reconcile_and_finish",
]

_CAPTURED = capture()


def _golden_path(name):
    return os.path.join(_DIR, name + ".txt")


def _regen():
    return os.environ.get("REGEN_PROMPT_GOLDENS") == "1"


@pytest.mark.parametrize("variant", _VARIANTS)
def test_the_writer_prompt_is_byte_identical_to_the_committed_golden(variant):
    got = _CAPTURED[variant]
    path = _golden_path(variant)
    if _regen():
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(got)
        pytest.skip(f"regenerated {variant}")
    assert os.path.exists(path), f"missing golden for {variant} — regenerate deliberately"
    with open(path, encoding="utf-8") as fh:
        want = fh.read()
    if got != want:
        diff = "\n".join(difflib.unified_diff(
            want.splitlines(), got.splitlines(),
            fromfile=f"golden/{variant}", tofile=f"captured/{variant}", lineterm="", n=2))[:6000]
        raise AssertionError(
            f"{variant} prompt CHANGED — generation is not inert.\n"
            f"Revert the prompt edit, or regenerate the goldens deliberately "
            f"(REGEN_PROMPT_GOLDENS=1) and say so in the commit.\n\n{diff}")


def test_the_goldens_actually_carry_the_rule_set_they_are_protecting():
    """A golden that silently became empty/short would pass the comparison while protecting nothing."""
    base = _CAPTURED["generate_article.minimal"]
    assert len(base) > 20000, "the article prompt collapsed — the inert proof is guarding nothing"
    assert base.count("\n  - ") >= 45, "the 52-rule bullet set is no longer present"
    for must in ("Quick answer", "MINIMUM COMPETITORS", "THE PUNT BAN IS ON MEANING",
                 "EVERY COMPARISON COLUMN MUST ANSWER FOR EVERY OPTION",
                 "PRICING PRODUCT-MATCH", "## Sources"):
        assert must in base, f"the article prompt lost its {must!r} rule"
    assert "Fact-check a FIRST-PARTY article" in _CAPTURED["verify_claims"]
    assert "FRESH FACTS" in _CAPTURED["reconcile_and_finish"]


def test_the_capture_is_deterministic():
    """Two captures in the same process must be identical — otherwise the proof is noise."""
    again = capture()
    for variant in _VARIANTS:
        assert again[variant] == _CAPTURED[variant], f"{variant} capture is not deterministic"
