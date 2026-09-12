"""FU181 — the fact gate swallowed a TRAILING COMMA into the fact, so a rewrite that preserved every
number perfectly failed all four attempts and shipped Claude's WATERMARKED body.

Reported live as: `dropped facts ['$1,300,', '$10,000,']` — note the commas.

This is the third appearance of one class (FU169 bare numbers, FU174 a long pricing phrase, now
punctuation): the ATOM layer false-fails a rewrite the regex FLOOR would pass, and the fallback ships
the un-stripped body. The tests below lock the specific bug AND the invariant that ends the class.
All $0, deterministic, no network.
"""
import re

from generators.blog_gen import _LOADBEARING_NUM_RE, BlogGenerator as B


SRC = "Claims between $1,300, and $10,000, are handled in-house."
REWRITE = "In-house handling covers claims from $1,300 up to $10,000."


def _atoms(text):
    """What the recall audit in _extract_protected_facts feeds the gate."""
    return sorted({m.group(0) for m in _LOADBEARING_NUM_RE.finditer(text)})


# ── the reported failure ─────────────────────────────────────────────────────────────────────────
def test_a_number_never_swallows_the_sentence_comma():
    assert _atoms(SRC) == ["$1,300", "$10,000"]
    assert not any(a.endswith(",") for a in _atoms(SRC))


def test_the_exact_reported_rewrite_now_passes():
    """The rewrite kept both numbers; only the comma moved. It must ship."""
    ok, missing = B._facts_preserved(SRC, REWRITE, None, B._gate_atoms(_atoms(SRC)))
    assert ok, f"a faithful rewrite must not fall back — still missing {missing}"


def test_the_currency_alternative_only_changes_the_broken_cases():
    cases = {
        "$1,300, and": "$1,300",        # was "$1,300,"  ← the bug
        "€2,500, plus": "€2,500",       # was "€2,500,"  ← the bug
        "$10,000.": "$10,000",
        "$149": "$149",
        "$2.50": "$2.50",
        "$1,000,000 total": "$1,000,000",
        "$10,000 or more": "$10,000",
    }
    for text, expected in cases.items():
        m = _LOADBEARING_NUM_RE.search(text)
        assert m and m.group(0) == expected, f"{text!r} -> {m.group(0) if m else None!r}"


# ── the invariant that ends the class ────────────────────────────────────────────────────────────
def test_the_atom_layer_never_fails_what_the_regex_floor_passes():
    """The FU169/FU174/FU181 failure mode in one assertion: the intelligent protect-list is an
    ADDITION to the floor, so for numbers it must never be STRICTER than the floor."""
    src = ("Plans start at $1,300, rising to $10,000, with a 2.5 mg starting dose and "
           "a 14-day trial across 5 seats.")
    # every number preserved, every one REFORMATTED
    out = ("Pricing opens at $1300 and reaches $10,000; the starting dose is 2.5mg, "
           "the trial runs 14 days, and it covers 5 seats.")
    gated = B._gate_atoms(_atoms(src))
    with_atoms = B._facts_preserved(src, out, None, gated)
    floor_only = B._facts_preserved(src, out, None, [])
    assert with_atoms[0] == floor_only[0], (
        f"the atom layer disagreed with the floor: atoms={with_atoms[1]} floor={floor_only[1]}")


def test_gate_atoms_drops_floor_covered_numbers_but_keeps_semantic_atoms():
    kept = B._gate_atoms(["$10,000,", "$1,300", "14-day", "2.5 mg", "503B", "Zepbound®", "Eli Lilly"])
    assert "$10,000," not in kept and "$1,300" not in kept and "14-day" not in kept
    assert kept == ["2.5 mg", "503B", "Zepbound®", "Eli Lilly"]


def test_a_digit_atom_matches_across_a_formatting_difference():
    """"2.5 mg" vs "2.5mg" and "$1,300" vs "$1300" are the SAME fact — a latent false failure."""
    ok, missing = B._facts_preserved("x", "dosed at 2.5mg for $1300", None, ["2.5 mg", "$1,300"])
    assert ok, missing


# ── protection is NOT weakened (the half that matters) ───────────────────────────────────────────
def test_a_dropped_or_altered_price_still_fails():
    for out in ("Claims from $1,300 are handled in-house.",          # $10,000 dropped
                "Claims from $1,300 up to $1,000 are handled."):     # $10,000 altered
        ok, missing = B._facts_preserved(SRC, out, None, B._gate_atoms(_atoms(SRC)))
        assert not ok and "10000" in missing, out


def test_semantic_atoms_are_still_enforced_verbatim():
    src = "Compounded under 503B rules with Zepbound® at a 2.5 mg dose."
    for out, gone in (("Compounded under 503A rules with Zepbound® at 2.5 mg.", "503B"),
                      ("Compounded under 503B rules with the drug at 2.5 mg.", "Zepbound®")):
        ok, missing = B._facts_preserved(src, out, None, B._gate_atoms([gone]))
        assert not ok and gone in missing, out


def test_a_non_digit_atom_is_never_loosened_by_the_normalizer():
    """The normalized retry is gated on the atom containing a digit — a name must still match."""
    ok, missing = B._facts_preserved("x", "written by EliLilly", None, ["Eli Lilly"])
    assert not ok and missing == ["Eli Lilly"]


# ═════════════════════════════════════════════════════════════════════════════════════════════════
# FU181b — an audit for OTHER instances of the same class, after the first one was found. Each case
# below was a live defect: a gate that false-fails a faithful rewrite (→ the watermarked body ships),
# or — in one case — a safety check that had never fired at all.
# ═════════════════════════════════════════════════════════════════════════════════════════════════
import json as _json

from generators.blog_gen import _MONEY_RE
from tests.stubs import StubClaude


# ── A. the FU176 price-cadence gate had the same comma bug, on the amount KEY ─────────────────────
def test_money_regex_never_ends_on_a_comma():
    assert [m.group(0) for m in _MONEY_RE.finditer("Acme charges $10,000, billed quarterly.")] == ["$10,000"]
    assert [m.group(0) for m in _MONEY_RE.finditer("From $1,300 to $2.50 and €2,500, more")] == \
        ["$1,300", "$2.50", "€2,500"]


def test_cadence_gate_passes_a_rewrite_that_only_moved_the_comma():
    """'$10,000, billed quarterly' keyed as '$10,000,' while the rewrite keyed '$10,000', so the
    multiset reported a cadence the rewrite had actually KEPT as lost."""
    ok, problems = B._price_cadence_ok("Acme charges $10,000, billed quarterly, for the annual plan.",
                                       "Acme bills $10,000 on a quarterly basis for the annual plan.")
    assert ok, problems


def test_cadence_gate_still_catches_a_genuinely_dropped_cadence():
    ok, problems = B._price_cadence_ok("Acme charges $10,000, billed quarterly.",
                                       "Acme charges $10,000.")
    assert not ok and "10,000" in problems[0]


# ── B. competitor-name protection had NEVER fired, and the naive fix would have false-failed ──────
def _brand():
    """Exactly what production supplies: db.get_brand returns dict(row), so `competitors` is the raw
    JSON STRING from the column — not a list."""
    return {"name": "OutSail", "competitors": _json.dumps(["Rippling", "Gusto"])}


def test_a_dropped_competitor_name_is_now_caught():
    """`list(json_string)` iterated CHARACTERS, and the len>=3 filter then dropped every one — so the
    check silently passed everything. It has to actually protect the name."""
    ok, missing = B._facts_preserved("OutSail beats Rippling on payroll.",
                                     "OutSail leads on payroll.", _brand(), [])
    assert not ok and missing == ["Rippling"]


def test_a_name_the_article_never_mentions_is_not_required():
    """The other half: parsing the list correctly WITHOUT scoping to the body would fail every
    rewrite for a brand whose stored competitors include anyone this article doesn't discuss."""
    ok, missing = B._facts_preserved("OutSail compares HR systems for mid-market teams.",
                                     "HR systems for mid-market teams are compared by OutSail.",
                                     _brand(), [])
    assert ok, missing


def test_a_dropped_brand_name_is_still_caught():
    ok, missing = B._facts_preserved("OutSail compares HR systems.", "HR systems get compared.",
                                     _brand(), [])
    assert not ok and missing == ["OutSail"]


# ── C. the FU179 LinkedIn URL gate ate the sentence's full stop ───────────────────────────────────
def _post_valid(src, out):
    """Drive the real surface gate through _apply_writer_pass and report whether the rewrite SHIPPED."""
    class _W:
        def probe(self, timeout=8): return {"state": "ok"}
        def call_text(self, prompt, **k): return out
    g = B(StubClaude(), db=None, writer=_W(), writer_mode="rewrite")
    g._evidence_blocks = []
    art = {"body_markdown": src}
    final = g._apply_writer_pass(art, src, {"name": "Acme"}, "q", surface="linkedin_post")
    return final.strip() != src.strip()


POST_SRC = ("Which agency handles unpaid B2B invoices?\n\n"
            "Acme, Rival and Third are the three worth knowing for claims over $10,000.\n\n"
            "If your invoice is under $2,500, Third is the better pick.\n\n"
            "Full guide: https://acme.com/guide.\n\n#B2B #Collections")


def test_a_post_rewrite_that_moves_the_link_mid_sentence_still_ships():
    out = ("Who should you call about unpaid B2B invoices?\n\n"
           "Three names are worth knowing for claims over $10,000 — Acme, Rival and Third.\n\n"
           "Where an invoice sits under $2,500, Third suits you better.\n\n"
           "Read https://acme.com/guide for the full write-up.\n\n#B2B #Collections")
    assert _post_valid(POST_SRC, out), "moving the URL off the end of a sentence is not dropping it"


def test_a_post_rewrite_that_really_drops_the_link_still_fails():
    out = ("Who should you call about unpaid B2B invoices?\n\n"
           "Three names are worth knowing for claims over $10,000 — Acme, Rival and Third.\n\n"
           "Where an invoice sits under $2,500, Third suits you better.\n\n"
           "More on our site.\n\n#B2B #Collections")
    assert not _post_valid(POST_SRC, out)


# ── D. the dropped-stat WARNING fired on a phantom ────────────────────────────────────────────────
def test_no_phantom_dropped_stat_from_a_trailing_comma():
    g = B(StubClaude(), db=None)
    assert g._dropped_stats("Enforcement hit 8,600, accounts last year.",
                            "Some 8,600 accounts were hit.") == []
    assert g._dropped_stats("Enforcement hit 8,600 accounts.",
                            "Enforcement hit many accounts.") == ["8,600"]
