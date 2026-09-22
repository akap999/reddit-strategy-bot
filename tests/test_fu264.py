"""FU264 — the checks found these. They were warnings, so it shipped anyway.

Two audits in a row reported defects the tool had already detected. The org-position check caught
the reported claim precisely in production — three findings, three correct, no false positives —
and the article shipped because FU263 made that check warning-only, on the grounds that the
false-positive rate could not be measured. One real run has now measured it.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.blog_gen import BlogGenerator  # noqa: E402

# The authority's real page, as our own fetcher returned it — the same fixture FU263 committed, so
# these bands are calibrated against a real document rather than a hand-written stand-in.
_PAGE = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "fixtures", "fu263_authority_page.txt"), encoding="utf-8").read()


def _gen():
    g = BlogGenerator.__new__(BlogGenerator)
    g._claim_pages = {}
    return g


def _run(body, page=None):
    blocks = [{"label": "official · Guidance", "url": "https://authority.example/p",
               "text": page if page is not None else _PAGE}]
    return _gen()._org_position_check(body, blocks, set())


def _sect(head, text):
    return f"## {head}\n\n{text}\n\n"


# the claim the operator reported, three times, as it was written
_WRONG = ("The AAP states that bottles should never be warmed in a microwave, as microwave heating "
          "creates hot spots that can burn an infant's mouth. [S1]")
_WRONG2 = ("The AAP states that bottles should never be warmed in a microwave because microwave "
           "heating creates hot spots that can burn an infant's mouth. [S1]")
# claims the page genuinely supports
_RIGHT_AMOUNTS = ("The AAP notes that newborns start at 1-2 oz per feed in the first days rising to "
                  "2-3 oz every 3-4 hours as feeding establishes. [S1]")
_RIGHT_HOLD = ("The AAP advises that the bottle should be held so that milk covers the nipple and "
               "the baby does not swallow air. [S1]")


def test_the_reported_claim_is_REMOVED_not_merely_flagged():
    """It scores 0.25 — three quarters of its distinctive words are absent from the cited page."""
    body = _sect("Heat", _WRONG) + _sect("FAQ", _WRONG2)
    out, note = _run(body)
    assert "microwave" not in out, "the claim was flagged but shipped, which is the whole defect"
    assert "2 removed" in note, note
    assert "put any of them back with a source that actually states it" in note


def test_a_claim_the_page_supports_is_untouched():
    body = _sect("Amounts", _RIGHT_AMOUNTS) + _sect("Hold", _RIGHT_HOLD)
    out, note = _run(body)
    assert out == body and note == ""


def test_the_grey_band_still_only_warns():
    """A claim at 0.50 is reported, not cut. The nearest SUPPORTED claim scores 0.67, so a removal
    at the warning threshold would sit 0.17 from a true one — too close to delete on."""
    grey = ("The AAP notes that newborns should be burped frequently and that paced feeding, "
            "holding the bottle more horizontally, reduces swallowed air. [S1]")
    out, note = _run(_sect("Colic", grey))
    assert "burped frequently" in out, "a grey-band claim must not be removed"
    assert note and "removed" not in note, note


def test_both_signals_still_have_to_agree_before_anything_happens():
    """A SUPPORTED claim can have its longest word missing — "rising" is not on the page — so the
    longest-token signal alone would delete a true sentence."""
    g = _gen()
    hit, toks = g._claim_tokens_on_page(
        "newborns start at 1-2 oz per feed in the first days rising to 2-3 oz every 3-4 hours "
        "as feeding establishes", _PAGE)
    assert max(toks, key=len) not in hit, "this case exists because the signals DISAGREE here"
    assert len(hit) / len(toks) > g._ORG_SAYS_MIN_SHARE, (hit, toks)
    out, note = _run(_sect("Amounts", _RIGHT_AMOUNTS))
    assert note == "" and "1-2 oz" in out


def test_an_unreadable_page_removes_nothing():
    """Unread is UNKNOWN. A walled source must never cost a sentence."""
    body = _sect("Heat", _WRONG)
    out, note = _run(body, page="too short to judge")
    assert out == body and note == ""


def test_over_the_cap_nothing_is_removed_at_all():
    """The existing fabrication cap: too many to remove safely means remove none and say so."""
    g = _gen()
    body = "".join(_sect("S%d" % i, _WRONG) for i in range(g._FAB_MAX_DROP + 2))
    out, note = _run(body)
    assert out == body, "over the cap the body must be untouched"
    assert note and "removed" not in note


def test_a_removal_never_leaves_damage_behind():
    from generators import blog_eval as E
    body = _sect("Heat", "Warming matters. " + _WRONG + " Use a bottle warmer instead.")
    out, _note = _run(body)
    assert not E.body_damage(out), "a removal left the paragraph damaged"
