"""FU167 — hardened REWRITE watermark-strip: the verification MEASURES the removal LEVEL from the
researched SynthID-Text mechanism factors (n=5 discretionary-prose overlap + longest-shared verbatim run
+ invisible-char strip), keeps the LOWEST-(overlap,run) of N escalating attempts, grades honestly, and
fixes grouped-citation orphans. All $0, deterministic, no network."""
from generators.blog_gen import BlogGenerator as B
from tests.stubs import StubClaude


class _ScriptWriter:
    """Returns a scripted output per attempt (clamped to the last)."""
    def __init__(self, outs):
        self.outs = outs
        self.prompts = []

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        i = len(self.prompts)
        self.prompts.append(prompt)
        return self.outs[min(i, len(self.outs) - 1)]


def _gen(writer, mode="rewrite"):
    return B(StubClaude(), db=None, writer=writer, writer_mode=mode)


# ── R1: n=5 prose overlap grades by DISCRETIONARY prose ───────────────────────────────────────
def test_r1_prose_overlap_grades():
    claude = "# H\n\nThe organization reviewed its yearly finances and approved additional support for research."
    reworded = "# H\n\nMembers examined this year's budget and greenlit extra money aimed at scientific study."
    r = B._watermark_removal_report(claude, reworded)
    assert r["n5_prose_overlap"] < 0.05 and r["grade"] == "thorough"
    near = claude   # near-copy → high overlap → not-confirmed
    r2 = B._watermark_removal_report(claude, near)
    assert r2["n5_prose_overlap"] >= 0.15 and r2["grade"] == "not-confirmed"


# ── R2: longest shared run > 4 FAILS even when the overall overlap is low ──────────────────────
def test_r2_longest_run_fails_even_when_overlap_low():
    claude = " ".join("w%d" % i for i in range(60))                      # 60 distinct words
    out = " ".join("z%d" % i for i in range(40)) + " " + " ".join("w%d" % i for i in range(20, 27))  # a 7-word run
    r = B._watermark_removal_report(claude, out)
    assert r["n5_prose_overlap"] < 0.15            # only a couple of 5-grams overlap → low average
    assert r["longest_shared_run"] >= 7            # but a 7-word verbatim run survived
    assert r["grade"] == "not-confirmed"           # the run guard fails it despite the low overlap


# ── R3: context BROKEN around a preserved atom (the mechanism-derived rule) ────────────────────
def test_r3_context_around_preserved_atom():
    claude = "The recommended maintenance dose is 15 mg once weekly for most adult patients starting therapy."
    bad = "The recommended maintenance dose is 15 mg once weekly for most adult patients starting therapy now."
    good = "Clinicians usually settle on 15 mg weekly once someone has begun treatment and tolerates it well."
    assert B._longest_shared_run(claude, bad) > 4      # original words kept adjacent to "15 mg" → long run
    assert B._longest_shared_run(claude, good) <= 4    # reworded right up to the atom → run broken


# ── R4: invisible-character strip (never alters visible text) ──────────────────────────────────
def test_r4_invisible_char_strip():
    zwsp, rlo, bom = chr(0x200B), chr(0x202E), chr(0xFEFF)   # zero-width space, RLO, ZWNBSP/BOM
    body = f"Visible{zwsp} text stays{rlo} the same{bom} here."
    cleaned, n = B._strip_invisible_chars(body)
    assert n == 3
    assert cleaned == "Visible text stays the same here."         # visible text unchanged
    assert B._strip_invisible_chars("plain normal text")[1] == 0
    # a report on an un-sanitized output counts the invisibles → not-confirmed
    assert B._watermark_removal_report("a b c d e", "x y z" + zwsp)["invisible_chars"] == 1


# ── R5: the loop OPTIMIZES the level — keeps the lowest-(overlap,run) valid attempt, stops at thorough ──
CLAUDE_R5 = ("# Guide\n\n"
             "The organization reviewed its yearly finances and approved additional support for scientific "
             "study programs across several departments this quarter [S1].\n\n"
             "## Sources\n- [S1] X — <https://x>\n")
NEAR = CLAUDE_R5                                                  # valid rewrite but a verbatim near-copy
THOROUGH = ("# Guide\n\n"
            "Members examined this year's budget and greenlit extra money for research efforts that span "
            "multiple teams during the current period [S1].\n\n"
            "## Sources\n- [S1] X — <https://x>\n")


def test_r5_loop_keeps_lowest_and_stops_at_thorough():
    w = _ScriptWriter([NEAR, THOROUGH])                          # attempt0 near-copy, attempt1 thorough
    gen = _gen(w, "rewrite")
    art = {"body_markdown": CLAUDE_R5}
    out = gen._apply_writer_pass(art, CLAUDE_R5, {"name": "X"}, "guide")
    assert out == THOROUGH                                        # shipped the LOWER-overlap attempt, not the last-scripted
    assert art["writer_grade"] == "thorough"
    assert art["writer_warning"] == ""                           # thorough → no warning
    assert 0 <= art["writer_overlap"] < 0.05 and art["writer_longest_run"] <= 4
    assert len(w.prompts) == 2                                    # stopped early once thorough (didn't burn all 4)


# ── R6: honest grade + warning wiring (not-confirmed carries the proxy caveat) ─────────────────
def test_r6_not_confirmed_grade_and_caveat():
    w = _ScriptWriter([CLAUDE_R5])                               # only ever returns the verbatim copy → never thorough
    gen = _gen(w, "rewrite")
    art = {"body_markdown": CLAUDE_R5}
    out = gen._apply_writer_pass(art, CLAUDE_R5, {"name": "X"}, "guide")
    assert out == CLAUDE_R5 and art["writer_mode_used"] == "rewrite"
    assert art["writer_grade"] == "not-confirmed"
    assert "not fully confirmed" in art["writer_warning"] and "proxy" in art["writer_warning"]
    assert len(w.prompts) == 4                                    # never thorough → all 4 escalating attempts


# ── Change 4: grouped citations split so _rebuild_sources can renumber them ────────────────────
def test_grouped_citation_split():
    assert B._split_grouped_citations("risk [S21, S22, S23] here") == "risk [S21][S22][S23] here"
    assert B._split_grouped_citations("[S1,S2,S3]") == "[S1][S2][S3]"
    assert B._split_grouped_citations("[S4, 5, 6]") == "[S4][S5][S6]"
    assert B._split_grouped_citations("a single [S7] marker") == "a single [S7] marker"   # untouched


# ── FU168: reword factual sentences too (safely) — prompt + deterministic fact-integrity gate ──
def test_fu168_rewrite_prompt_rewords_factual_with_safety_fallback():
    w = _ScriptWriter([CLAUDE_R5])
    gen = _gen(w, "rewrite")
    gen._apply_writer_pass({"body_markdown": CLAUDE_R5}, CLAUDE_R5, {"name": "X"}, "guide")
    p = w.prompts[0]
    assert "and factual claim" not in p                         # no longer preserve whole factual SENTENCES
    assert "REWORD the wording of EVERY sentence" in p          # reword factual/regulatory/FAQ sentences too
    assert "SAFETY FALLBACK" in p and "keep THAT sentence VERBATIM" in p
    assert "contraindicated" in p and "negation" in p.lower()   # negations/directives kept exact


def test_fu168_facts_preserved_gate():
    claude = "Dose is 2.5 mg weekly; BMI 30; 503A/503B; MTC and MEN2; from $149 [S1]."
    brand = {"name": "PeterMD", "competitors": ["Ro"]}
    assert B._facts_preserved(claude,
        "reworded 2.5 mg per week, BMI 30, 503A 503B, MTC MEN2, at $149 [S1] PeterMD Ro", brand)[0]
    ok, miss = B._facts_preserved(claude,
        "reworded weekly dose, BMI 30, 503A, MTC, at $149 [S1] PeterMD Ro", brand)   # drops 2.5 / 503B / MEN2
    assert not ok and "2.5" in miss and "503B" in miss and "MEN2" in miss


def test_fu169_bare_and_rhetorical_numbers_not_gated():
    """FU169: an incidental/rhetorical number the rewrite validly rephrases away must NOT block the ship
    (the '100' false positive that made the on-demand rewrite always fall back). Load-bearing numbers
    (decimals, currency, unit-qualified doses) STILL hard-gate; comma/format variance is normalized."""
    claude = ("Nearly 100% of patients tolerate it, and 3 of the top 100 clinics offer it; the starting "
              "dose is 2.5 mg weekly and it costs $1,000 per year [S1].")
    # reworded "100% → virtually all", "3 of the top 100 → several", dose+price KEPT (as $1000, "2.5mg")
    ok, miss = B._facts_preserved(claude,
        "Virtually all patients tolerate it; several leading clinics offer it — starting at 2.5mg a week, $1000 yearly [S1].")
    assert ok and miss == []                                  # bare 100 / 3 / rhetorical % no longer block; $1,000≡$1000, 15 mg≡15mg
    # but DROPPING the dose or the price still fails
    ok2, miss2 = B._facts_preserved(claude, "Virtually all tolerate it; several clinics offer it yearly [S1].")
    assert not ok2 and "2.5" in miss2 and "1000" in miss2
    # a genuine unit-qualified dose (100 mg) IS load-bearing even though bare 100 is not
    ok3, miss3 = B._facts_preserved("Take 100 mg once daily [S1].", "Take it once daily [S1].")
    assert not ok3 and "100" in miss3


def test_fu168_loop_rejects_fact_dropping_attempt():
    body = ("# H\n\nThe starting dose is 2.5 mg once weekly for adults beginning therapy this year [S1].\n\n"
            "## Sources\n- [S1] x — <https://x>\n")
    bad = ("# H\n\nClinicians begin weekly dosing once an adult starts treatment during the year [S1].\n\n"
           "## Sources\n- [S1] x — <https://x>\n")   # DROPS the 2.5 mg dose → unsafe
    good = ("# H\n\nClinicians begin at 2.5 mg weekly once an adult starts treatment during the year [S1].\n\n"
            "## Sources\n- [S1] x — <https://x>\n")
    w = _ScriptWriter([bad, good])
    out = _gen(w, "rewrite")._apply_writer_pass({"body_markdown": body}, body, {"name": "X"}, "dose")
    assert out == good and "2.5" in out                         # the fact-dropping attempt was rejected → shipped the safe one
    assert len(w.prompts) == 2
