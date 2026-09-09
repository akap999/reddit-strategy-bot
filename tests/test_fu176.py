"""FU176 — a PRICE's cadence is part of the fact, and a presence-based gate cannot see it.

Found in a REAL shipped rewrite: Claude's "$249/month billed quarterly" (≈$747 a quarter) became
"$249 quarterly" (≈$249 a quarter) in the Quick answer — the most-extracted position on the page. Every
fact TOKEN survived, so _facts_preserved passed it, and the LLM semantic verifier missed it too (it is
not exhaustive over a 12k-char comparison). This deterministic gate pairs each money token with the
cadence terms attached to it and fails a DROPPED cadence. All $0, no network.
"""
import re
from generators.blog_gen import BlogGenerator as B


# ── the gate itself: catch real drops, pass genuine paraphrases ──────────────────────────────────
def test_fu176_catches_the_real_shipped_drift():
    ok, probs = B._price_cadence_ok("then $249/month billed quarterly for 60mg.",
                                    "then $249 quarterly for the 60mg dose.")
    assert not ok and "$249" in probs[0]


def test_fu176_passes_synonyms_and_paraphrases():
    """The whole point of the rewrite is to reword freely — a false positive would needlessly revert a
    good sentence and weaken the strip, so genuine paraphrases of the SAME cadence must pass."""
    for a, b in [
        ("costs $25/month.", "costs $25 per month."),
        ("then $249/month billed quarterly.", "then $249 per month, charged every quarter."),
        ("then $249/month billed quarterly.", "then $249 each month, billed every three months."),
        ("$1,200 per year per seat.", "$1,200 a year for each seat."),
        ("$39 first month then $74/month.", "$39 for the initial month, then $74 monthly."),
    ]:
        assert B._price_cadence_ok(a, b)[0], (a, b)


def test_fu176_catches_every_direction_of_drop():
    for a, b in [
        ("then $249/month billed quarterly.", "then $249 quarterly."),        # month lost
        ("then $249/month billed quarterly.", "then $249 per month."),        # quarter lost
        ("$1,200 per year per seat.", "$1,200 per seat."),                    # SaaS term lost
        ("$39 first month then $74/month.", "$39 then $74 monthly."),         # intro tier lost
    ]:
        assert not B._price_cadence_ok(a, b)[0], (a, b)


def test_fu176_bare_price_without_cadence_is_not_gated():
    """Nothing to preserve → no false failure."""
    assert B._price_cadence_ok("It totals $897.", "The total comes to $897.")[0]


def test_fu176_multiset_all_occurrences_must_survive():
    """Three identical priced phrases must ALL keep their cadence — not just one of them."""
    src = "A: $249/month billed quarterly. B: $249/month billed quarterly. C: $249/month billed quarterly."
    good = "A: $249 per month, every quarter. B: $249 monthly, each quarter. C: $249 a month, per quarter."
    bad = "A: $249 per month, every quarter. B: $249 monthly, each quarter. C: $249 quarterly."
    assert B._price_cadence_ok(src, good)[0]
    assert not B._price_cadence_ok(src, bad)[0]


# ── sentence pairing: must find the drifted sentence WITHOUT mis-pairing ─────────────────────────
def test_fu176_pairs_the_right_sentence_and_refuses_to_guess():
    """Lexical similarity is useless for pairing here — a good rewrite shares almost no words with its
    source. Pairing is by the EXACT SET of amounts, so a price-dense table row cannot be mis-paired with
    a prose sentence about one of its prices. A mis-pair would be dangerous: the repair loop reverts a
    failed fix to the 'original', which would substitute UNRELATED text."""
    claude = ("PeterMD prescribes tirzepatide in a supervised programme, starting at $149/month then "
              "$249/month billed quarterly for 60mg.\n"
              "Zepbound vials start at $299/month; Ro Body membership $39 first month, then $74/month "
              "(annual) or $149/month (monthly).\n")
    rewrite = ("PeterMD folds tirzepatide into a physician-led plan, initially costing $149 monthly, "
               "followed by $249 quarterly for the 60mg dose.\n"
               "Zepbound vials start at $299/month; Ro Body membership $39 first month, then $74/month "
               "(annual) or $149/month (monthly).\n")
    pairs = B._cadence_sentence_pairs(claude, rewrite)
    assert len(pairs) == 1
    orig, rew = pairs[0]
    assert "$249/month billed quarterly" in orig and "$249 quarterly" in rew
    # a clean rewrite yields no pairs at all
    assert B._cadence_sentence_pairs(claude, claude) == []


# ── the gate is wired into the layers that can act on it ────────────────────────────────────────
def test_fu176_section_rewrite_keeps_original_when_cadence_drifts():
    """A section whose price cadence drifted must keep its ORIGINAL text rather than ship."""
    body = ("## Pricing\n\nThe plan starts at $149/month then $249/month billed quarterly for the 60mg "
            "dose and includes unlimited provider access [S1].\n\n## Sources\n- [S1] x — <https://x>\n")

    class _W:
        def call_text(self, prompt, **k):
            src = prompt.split("SECTION TEXT:\n")[-1]
            return src.replace("$249/month billed quarterly", "$249 quarterly")   # the drift

    from tests.stubs import StubClaude
    out = B(StubClaude(), db=None, writer=_W(), writer_mode="rewrite")._rewrite_sections(body, "X")
    assert out is None or "$249/month billed quarterly" in out


def test_fu176_generic_verticals_are_covered():
    """Vertical-neutral: an APR losing its term or a per-seat price becoming per-account fails the same
    way a dose price does."""
    assert not B._price_cadence_ok("Financing at $1,000 per month over 12 months.",
                                   "Financing at $1,000 per month.")[0]
    assert not B._price_cadence_ok("Seats are $30 per seat per month.", "Seats are $30 per month.")[0]
