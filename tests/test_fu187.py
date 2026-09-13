"""FU187 — a word the author CAPITALISED FOR EMPHASIS is not a fact.

A live rewrite failed with `watermark NOT stripped — fell back to Claude (dropped facts ['AND'])`.
Claude had written "low testosterone AND a BMI indicating overweight/obesity"; the acronym harvest in
`_facts_preserved` reads any >=3-letter ALLCAPS token as a code, so "AND" had to survive the rewrite
character-for-character. Qwen wrote a natural lowercase "and", every attempt failed, and the
WATERMARKED body shipped. Fourth recurrence of the FU169 / FU174 / FU181 class: the atom layer
false-failing a rewrite the regex floor would pass. $0, no network."""
from generators.blog_gen import BlogGenerator, _is_emphasis_caps

SRC = ("Best fit for: Men with lab-confirmed low testosterone AND a BMI indicating overweight or "
       "obesity. ZEPBOUND (tirzepatide) starts at 2.5 mg once weekly, and is contraindicated with a "
       "history of MTC or Multiple Endocrine Neoplasia syndrome type 2 (MEN 2). FDA reviewed the "
       "TRAVERSE trial. PeterMD lists $149 per month.")


def test_the_reported_failure_now_passes():
    """The exact production pair: the only change is the shouted AND becoming a normal 'and'."""
    out = SRC.replace("testosterone AND a BMI", "testosterone and a BMI")
    ok, missing = BlogGenerator._facts_preserved(SRC, out, None, [])
    assert ok and missing == [], missing


def test_a_real_acronym_is_still_required_verbatim():
    for code in ("MTC", "MEN", "FDA", "BMI", "TRAVERSE", "ZEPBOUND"):
        out = SRC.replace(code, "")
        ok, missing = BlogGenerator._facts_preserved(SRC, out, None, [])
        assert not ok and code in missing, f"{code} lost its protection"


def test_men_is_never_treated_as_emphasis_even_though_men_is_an_english_word():
    """The one that would hurt: MEN is Multiple Endocrine Neoplasia on a men's-health page where the
    word 'men' also appears in lowercase everywhere."""
    assert _is_emphasis_caps("MEN", SRC) is False
    assert _is_emphasis_caps("AND", SRC) is True


def test_a_listed_word_used_ONLY_in_caps_keeps_its_protection():
    """Both conditions must hold, so a domain code that happens to be on the list is still safe."""
    body = "The CAN protocol is defined in the standard."      # no lowercase "can" anywhere
    assert _is_emphasis_caps("CAN", body) is False
    assert _is_emphasis_caps("CAN", body + " you can see it") is True


def test_the_deliberate_exclusions_stay_protected():
    """Words with a real acronym meaning are NOT on the list."""
    for code in ("ALL", "WHO", "ACT", "AID", "MEN", "CARE", "HOPE"):
        assert _is_emphasis_caps(code, "all who act aid men care hope") is False, code


def test_the_model_supplied_atom_path_is_closed_too():
    assert BlogGenerator._atom_shape_ok("AND") is False
    assert BlogGenerator._atom_shape_ok("MEN 2") is True
    assert BlogGenerator._atom_shape_ok("503B") is True
    assert BlogGenerator._gate_atoms(["AND", "MEN2", "2.5 mg"]) == ["MEN2", "2.5 mg"]


def test_a_genuinely_dropped_fact_still_fails():
    out = SRC.replace("$149", "").replace("2.5 mg", "")
    ok, missing = BlogGenerator._facts_preserved(SRC, out, None, [])
    assert not ok and any("149" in m for m in missing)
