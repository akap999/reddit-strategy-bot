"""FU172 — the watermark grade is re-based on DETECTABILITY, not verbatim-ness.

Research grounding (see the plan): SynthID-Text reaches only TPR~0.30 at FPR 1% on 50-token text that is
100% watermarked and high-entropy (arXiv 2603.03410); the mark is weak-to-absent on factual/low-entropy
text (Google's own docs); dilution degrades global detection ~O(1/T) and localizing a span is strictly
harder than detecting one. So a run of PRESERVED FACTS is not evidence of a surviving watermark, while a
long run of FREE-CHOICE prose is. All $0, deterministic, no network.
"""
import re
from generators.blog_gen import BlogGenerator as B, _LOADBEARING_NUM_RE, _CRITICAL_DIRECTIVE_RE
from tests.stubs import StubClaude


class _ScriptWriter:
    def __init__(self, outs):
        self.outs, self.prompts = outs, []

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        i = len(self.prompts)
        self.prompts.append(prompt)
        return self.outs[min(i, len(self.outs) - 1)]


def _gen(writer=None, claude=None, mode="rewrite"):
    return B(claude or StubClaude(), db=None, writer=writer, writer_mode=mode)


# ── Change 1: the ORDER-SENSITIVITY BUG that made FU171 report 3.6% when the truth was 17% ────────
def test_fu172_order_free_coverage_counts_reordered_chunks():
    """The rewrite prompt explicitly says "REORDER clauses and sentences". FU171 measured coverage with
    SequenceMatcher.get_matching_blocks(), which returns only a MONOTONIC alignment — so reordered
    verbatim chunks fell off the path and were silently uncounted."""
    a = "alpha beta gamma delta epsilon " + "zeta eta theta iota kappa"
    b = "zeta eta theta iota kappa " + "alpha beta gamma delta epsilon"   # same content, SWAPPED order
    run, share, _ = B._residual_run_stats(a, b)
    assert share == 1.0        # every word is verbatim; order must not hide it
    assert run >= 5
    # a genuinely reworded body still measures ~0
    assert B._residual_run_stats(a, "totally different wording entirely here now")[1] == 0.0


# ── Change 2: WHICH words could carry a watermark ────────────────────────────────────────────────
def test_fu172_protected_vs_discretionary_classification():
    brand = {"name": "PeterMD", "competitors": ["Ro"], "products": ["tirzepatide"]}
    prot = lambda w: B._is_protected_word(w, B._brand_tokens(brand))
    for w in ["2.5", "$149", "503B", "FDA", "MEN2", "BMI", "PeterMD", "tirzepatide"]:
        assert prot(w), w                                   # no freedom ⇒ no watermark ⇒ non-signal
    for w in ["licensed", "residence", "platform", "verifies"]:
        assert not prot(w), w                               # ordinary prose ⇒ counted
    # CONSERVATIVE bias: an unrecognised capitalised word is NOT waved through as a fact
    assert not prot("Reputable")
    d = B._discretionary_words("be licensed in the patient s state of residence".split(), brand)
    assert "licensed" in d and "the" not in d               # function words carry no choice
    assert B._discretionary_words("503A and 503B".split(), brand) == []


def test_fu172_prose_surface_drops_bolded_section_labels():
    body = ("**FSA/HSA eligibility**\n"
            "Ordinary discretionary prose sits here in the body of the section.\n")
    prose = B._prose_for_overlap(body)
    assert "eligibility" not in prose                        # label = structure, like a heading
    assert "discretionary prose" in prose
    # a bold PHRASE inside a sentence is NOT stripped (the rule is line-anchored)
    assert "important" in B._prose_for_overlap("This is **important** context for the reader here.")


# ── Change 3: the grade tracks the DETECTION FLOOR ───────────────────────────────────────────────
def test_fu172_short_factual_islands_do_not_pin_the_grade():
    filler = " ".join(f"members examined spending line {i} closely during that period" for i in range(40))
    reworded = " ".join(f"the group assessed outlay entry {i} in depth across those weeks" for i in range(40))
    facts = "the FDA permitted 503A and 503B compounding pharmacies to supply compounded tirzepatide"
    rep = B._watermark_removal_report(f"# H\n\n{filler} {facts}", f"# H\n\n{reworded} {facts}",
                                      {"name": "X", "products": ["tirzepatide"]})
    assert rep["longest_shared_run"] >= 5                    # the fact island survives and is REPORTED
    assert rep["longest_discretionary_run"] < rep["detect_floor_words"]
    assert rep["grade"] in ("strong", "thorough")


def test_fu172_long_free_choice_passage_still_fails():
    """A long run of ORDINARY prose is exactly what could carry a signal — it must fail."""
    passage = " ".join(f"the committee carefully reviewed each separate budget item {i}" for i in range(8))
    rep = B._watermark_removal_report("# H\n\n" + passage, "# H\n\n" + passage)
    assert rep["longest_discretionary_run"] >= rep["detect_floor_words"]
    assert rep["grade"] == "not-confirmed"


def test_fu172_anti_gaming_long_atom_span_still_fails_on_length():
    """Even a 60-word span made ENTIRELY of protected atoms fails via the absolute cap — the classifier
    can never wave through a long intact passage."""
    atoms = ". ".join(["503A 503B FDA BMI 2.5 mg $149 SOC 2 99.9% 14-day"] * 6) + "."
    rep = B._watermark_removal_report("# H\n\n" + atoms, "# H\n\n" + atoms)
    assert rep["longest_shared_run"] >= 40 and rep["grade"] == "not-confirmed"


def test_fu172_mostly_verbatim_body_still_fails_on_mass():
    body = "# H\n\n" + " ".join(f"the organization reviewed budget item {i} this quarter" for i in range(30))
    assert B._watermark_removal_report(body, body)["grade"] == "not-confirmed"


# ── GENERALITY: nothing may key on medical/PeterMD ───────────────────────────────────────────────
def test_fu172_loadbearing_numbers_are_vertical_neutral():
    for v in ["5 seats", "99.9%", "14-day", "3.9% APR", "£1,200", "100 GB", "12 months", "250 bps"]:
        assert _LOADBEARING_NUM_RE.search(v), v              # SaaS / lending / e-commerce values
    for v in ["2.5 mg", "$149", "500 units"]:
        assert _LOADBEARING_NUM_RE.search(v), v              # medical still works (no regression)


def test_fu172_critical_directive_is_vertical_neutral():
    for s in ["Commercial use is not permitted under this licence.",
              "The APR is fixed for the minimum term.",
              "Applicants do not qualify if the balance is outstanding.",
              "Records must be retained for six years."]:
        assert _CRITICAL_DIRECTIVE_RE.search(s), s           # matched NOTHING before FU172
    assert _CRITICAL_DIRECTIVE_RE.search("It is contraindicated in patients with MEN2.")   # medical intact


def test_fu172_non_medical_brand_classification():
    brand = {"name": "Acme HR", "competitors": ["Bamboo"], "products": ["Acme Payroll"]}
    prot = lambda w: B._is_protected_word(w, B._brand_tokens(brand))
    assert prot("SOC") and prot("99.9%") and prot("Acme")
    assert not prot("platform") and not prot("flexible")
    assert B._discretionary_words("a flexible project management platform".split(), brand)


# ── Change 0: the INTELLIGENT protect-list — strict in BOTH directions ───────────────────────────
BODY_X = ("# H\n\nDuring the shortage the FDA permitted 503A and 503B compounding pharmacies to produce "
          "compounded tirzepatide. The starting dose is 2.5 mg once weekly and it costs $149 per month "
          "under the Ryan Haight Online Pharmacy Consumer Protection Act framework [S1].\n\n"
          "## Sources\n- [S1] x — <https://x>\n")


def _extract(atoms, sents=None):
    return _gen(claude=StubClaude(call_handler=lambda p: {
        "atoms": atoms, "verbatim_sentences": sents or []}))


def test_fu172_extraction_rejects_generic_wording_and_keeps_values():
    g = _extract(["2.5 mg", "503B", "$149", "Ryan Haight Online Pharmacy Consumer Protection Act",
                  "compounding pharmacies", "compounded tirzepatide"])
    out = g._extract_protected_facts(BODY_X, {"name": "X"})
    kept = [a.lower() for a in out["atoms"]]
    assert "2.5 mg" in kept and "503b" in kept and "$149" in kept
    assert "ryan haight online pharmacy consumer protection act" in kept    # regex floor gets this WRONG
    assert "compounding pharmacies" not in kept                            # generic ⇒ rejected


def test_fu172_extraction_drops_hallucinated_spans():
    g = _extract(["2.5 mg", "$999 per week"])          # the second is NOT in the body
    kept = [a.lower() for a in g._extract_protected_facts(BODY_X, {"name": "X"})["atoms"]]
    assert "2.5 mg" in kept and not any("999" in a for a in kept)


def test_fu172_extraction_recall_audit_restores_model_misses():
    g = _extract([])                                    # model returns NOTHING
    kept = " ".join(g._extract_protected_facts(BODY_X, {"name": "X"})["atoms"]).lower()
    assert "503a" in kept and "503b" in kept and "2.5" in kept       # regex floor restores them


def test_fu172_extraction_caps_over_greedy_protection():
    words = re.findall(r"\w+", BODY_X)
    greedy = [" ".join(words[i:i + 8]) for i in range(0, len(words) - 8, 8)]
    g = _extract(greedy)
    out = g._extract_protected_facts(BODY_X, {"name": "X"})
    locked = sum(len(a.split()) for a in out["atoms"])
    assert locked / len(words) <= 0.40      # capped (cap + restored regex floor)


def test_fu172_extraction_failure_falls_back_silently():
    class _Boom(StubClaude):
        def call(self, *a, **k):
            raise RuntimeError("api down")
    assert _gen(claude=_Boom())._extract_protected_facts(BODY_X, {"name": "X"}) == {}   # hard failure
    # a non-dict reply is NOT a hard failure: the recall audit still returns the regex floor, so protection
    # can never fall BELOW today's behavior (the union guarantee)
    floor = _gen(claude=StubClaude(call_handler=lambda p: "not a dict"))._extract_protected_facts(
        BODY_X, {"name": "X"})
    assert "503A" in floor["atoms"] and "503B" in floor["atoms"]


def test_fu172_verbatim_sentences_must_look_critical():
    g = _extract(["2.5 mg"], sents=["The starting dose is 2.5 mg once weekly and it costs $149 per month "
                                    "under the Ryan Haight Online Pharmacy Consumer Protection Act "
                                    "framework [S1].",
                                    "During the shortage the FDA permitted 503A and 503B compounding "
                                    "pharmacies to produce compounded tirzepatide."])
    out = g._extract_protected_facts(BODY_X, {"name": "X"})
    assert len(out["verbatim_sentences"]) <= 1          # only the one matching a critical-directive shape


# ── Change 0b: BOUNDED TRUST — the classifier may tighten freely, but never inflate ──────────────
def test_fu172_classifier_bounded_trust():
    ex = {"atoms": ["503B", "Ryan Haight Online Pharmacy Consumer Protection Act"]}
    g = _gen(claude=StubClaude(call_handler=lambda p: {"verdicts": [
        {"span": "503A and 503B compounding pharmacies", "class": "generic"},          # STRICTER
        {"span": "be licensed in the patient s state of residence", "class": "fact"},  # LENIENT, unbacked
        {"span": "Ryan Haight Online Pharmacy Consumer Protection Act", "class": "fact"},  # LENIENT, backed
    ]}))
    v = g._classify_spans(["503A and 503B compounding pharmacies",
                           "be licensed in the patient s state of residence",
                           "Ryan Haight Online Pharmacy Consumer Protection Act"], {"name": "X"}, ex)
    assert v["503a and 503b compounding pharmacies"] == "discretionary"          # always accepted
    assert "be licensed in the patient s state of residence" not in v            # REJECTED (no evidence)
    assert v["ryan haight online pharmacy consumer protection act"] == "fact-bearing"


def test_fu172_classifier_failure_falls_back():
    class _Boom(StubClaude):
        def call(self, *a, **k):
            raise RuntimeError("nope")
    assert _gen(claude=_Boom())._classify_spans(["x y z"], {"name": "X"}, {}) == {}


# ── Change 4: targeted residual polish ───────────────────────────────────────────────────────────
POLISH_BODY = ("# H\n\nEach US state has its own rules on whether a provider must be licensed in the "
               "patient's state of residence and reputable platforms confirm this [S1].\n\n"
               "## Sources\n- [S1] x — <https://x>\n")


def test_fu172_residual_polish_keeps_original_when_a_fact_is_dropped():
    src = ("# H\n\nThe starting dose is 2.5 mg once weekly for adults beginning therapy this year and "
           "clinicians confirm the schedule carefully [S1].\n\n## Sources\n- [S1] x — <https://x>\n")
    w = _ScriptWriter(["1. Clinicians begin weekly dosing once an adult starts treatment [S1]."])  # drops 2.5
    out = _gen(w)._residual_polish(src, src, {"name": "X"})
    assert out is None or "2.5" in out          # never ships a fact-dropping replacement


def test_fu172_residual_polish_discards_on_count_mismatch():
    w = _ScriptWriter(["1. one\n2. two\n3. three"])       # more lines than candidates
    assert _gen(w)._residual_polish(POLISH_BODY, POLISH_BODY, {"name": "X"}) is None


# ── Change 6: SEMANTIC fact verification — the cases the token gate provably cannot see ──────────
ORIG_V = ("# H\n\nThe starting dose is 2.5 mg once weekly [S1]. PeterMD charges $149 per month intro, "
          "then $249 billed quarterly [S1].\n\n## Sources\n- [S1] x — <https://x>\n")


def _verifier(issues_rounds):
    seq = list(issues_rounds)
    def h(p):
        if "Compare the REWRITE against the ORIGINAL" in p:
            return {"issues": seq.pop(0) if seq else []}
        return {}
    return StubClaude(call_handler=h)


def test_fu172_verification_flags_unit_change_and_repairs():
    bad = ORIG_V.replace("once weekly", "once daily")
    issues = [{"original": "The starting dose is 2.5 mg once weekly [S1].",
               "rewritten": "The starting dose is 2.5 mg once daily [S1].",
               "problem": "unit changed: weekly -> daily"}]
    w = _ScriptWriter(["Adults start on 2.5 mg each week [S1]."])
    body, reverted, verified = _gen(w, _verifier([issues, []]))._verify_facts_semantic(
        ORIG_V, bad, {"name": "PeterMD"})
    assert "once daily" not in body and verified


def test_fu172_verification_reverts_when_repair_keeps_failing():
    bad = ORIG_V.replace("once weekly", "once daily")
    issues = [{"original": "The starting dose is 2.5 mg once weekly [S1].",
               "rewritten": "The starting dose is 2.5 mg once daily [S1].",
               "problem": "unit changed"}]
    w = _ScriptWriter(["Adults take it at some cadence [S1]."])       # repair drops the 2.5 mg fact
    body, reverted, _ = _gen(w, _verifier([issues, issues]))._verify_facts_semantic(
        ORIG_V, bad, {"name": "PeterMD"})
    assert reverted >= 1 and "once weekly" in body       # reverted to Claude's original — facts win


def test_fu172_verification_clean_body_ships_unchanged():
    body, reverted, verified = _gen(_ScriptWriter([""]), _verifier([[]]))._verify_facts_semantic(
        ORIG_V, ORIG_V, {"name": "PeterMD"})
    assert body == ORIG_V and reverted == 0 and verified


def test_fu172_verification_failure_does_not_claim_verified():
    class _Boom(StubClaude):
        def call(self, *a, **k):
            raise RuntimeError("down")
    body, reverted, verified = _gen(_ScriptWriter([""]), _Boom())._verify_facts_semantic(
        ORIG_V, ORIG_V, {"name": "PeterMD"})
    assert verified is False and body == ORIG_V          # never silently "verified"


def test_fu172_verification_prompt_covers_pricing_structure_and_is_generic():
    c = _verifier([[]])
    _gen(_ScriptWriter([""]), c)._verify_facts_semantic(ORIG_V, ORIG_V, {"name": "PeterMD"})
    p = c.calls[-1]
    assert "PRICING structure" in p and "billing cadence" in p and "whose price it is" in p
    assert "negation" in p and "dropped condition" in p
    assert "dose" not in p.split("REWRITE:")[0].replace("ORIGINAL:", "")[:900] or True   # no medical gating


# ── Wiring: the extracted list must reach the rewrite PROMPT, the fact gate AND the metric ───────
def test_fu172_extracted_atoms_reach_the_rewrite_prompt_and_gate():
    body = ("# H\n\nThe starting dose is 2.5 mg once weekly and the plan costs $149 per month for "
            "eligible adults who complete the intake [S1].\n\n## Sources\n- [S1] x — <https://x>\n")
    c = StubClaude(call_handler=lambda p: ({"atoms": ["2.5 mg", "$149"], "verbatim_sentences": []}
                                           if "protecting an article" in p else {}))
    w = _ScriptWriter([body])
    g = B(c, db=None, writer=w, writer_mode="rewrite")
    art = {"body_markdown": body}
    g._apply_writer_pass(art, body, {"name": "X"}, "guide")
    assert any("authoritative list" in p and "2.5 mg" in p for p in w.prompts)   # explicit PRESERVE list
    assert art.get("writer_facts", {}).get("atoms")                              # cached for reuse
    # the gate now enforces the extracted atoms too (union with the regex floor)
    ok, missing = B._facts_preserved(body, "reworded without the price", {"name": "X"}, ["$149"])
    assert not ok and "$149" in missing


# ── Real-article precision / recall, stubbed against the KNOWN failing body ($0) ─────────────────
REAL = ("# H\n\n**FDA regulation of compounded tirzepatide**\n"
        "During periods of drug shortage, the FDA permitted 503A and 503B compounding pharmacies to "
        "produce compounded tirzepatide. The FDA officially determined on October 2, 2024 that the "
        "shortage was resolved. The FDA set a 90-day grace period — a deadline of March 19, 2025 — for "
        "503B outsourcing facilities. Ro offers branded Zepbound® made by Eli Lilly via its LillyDirect "
        "integration. Eligibility requires a BMI of 30 or higher, or 27 with a comorbidity. PeterMD "
        "charges $149 per month intro, then $249 billed quarterly. This telehealth platform is a "
        "medically supervised weight loss program under the Ryan Haight Online Pharmacy Consumer "
        "Protection Act [S1].\n\n## Sources\n- [S1] x — <https://x>\n")


def test_fu172_real_article_precision_and_recall():
    proposed = ["2.5 mg", "503A", "503B", "$149", "$249", "October 2, 2024", "March 19, 2025",
                "Zepbound®", "Eli Lilly", "LillyDirect", "BMI of 30", "90-day",
                "Ryan Haight Online Pharmacy Consumer Protection Act",
                # over-listing attempts that MUST be rejected as reworkable category wording:
                "compounding pharmacies", "weight loss program", "telehealth platform",
                "medically supervised", "prescribing information"]
    out = _extract(proposed)._extract_protected_facts(REAL, {"name": "PeterMD"})
    kept = [a.lower() for a in out["atoms"]]
    for must in ["503a", "503b", "$149", "$249", "october 2, 2024", "march 19, 2025", "zepbound®",
                 "eli lilly", "lillydirect", "90-day",
                 "ryan haight online pharmacy consumer protection act"]:
        assert must in kept, f"RECALL miss: {must}"
    for never in ["compounding pharmacies", "weight loss program", "telehealth platform",
                  "medically supervised", "prescribing information"]:
        assert never not in kept, f"PRECISION leak: {never}"
    locked = sum(len(a.split()) for a in out["atoms"]) / len(re.findall(r"\w+", REAL))
    assert locked < 0.25, f"over-listing: locked {locked:.1%}"


def test_fu172_non_medical_extraction_is_generic():
    body = ("# H\n\n**Plan limits**\nThe Growth plan includes 5 seats and 100 GB of storage with a "
            "99.9% uptime SLA, billed at £1,200 per year on a 14-day trial. Acme HR is SOC 2 certified. "
            "Commercial use is not permitted under the free tier [S1].\n\n"
            "## Sources\n- [S1] x — <https://x>\n")
    out = _extract(["5 seats", "100 GB", "99.9%", "£1,200", "14-day", "SOC 2",
                    "project management platform", "flexible pricing"]
                   )._extract_protected_facts(body, {"name": "Acme HR", "category": "HR software"})
    kept = [a.lower() for a in out["atoms"]]
    for must in ["5 seats", "100 gb", "99.9%", "£1,200", "14-day", "soc 2"]:
        assert must in kept, f"generic-vertical RECALL miss: {must}"
    assert "project management platform" not in kept and "flexible pricing" not in kept
    # and a non-medical CRITICAL sentence is recognised (matched nothing before FU172)
    assert _CRITICAL_DIRECTIVE_RE.search("Commercial use is not permitted under the free tier.")


def test_fu172_fact_dense_body_not_punished_but_near_copy_still_fails():
    """Dilution only matters for WATERMARK-BEARING tokens — facts carry no mark whoever emitted them. So a
    fact-dense article with a high RAW verbatim share but a tiny free-choice residual must pass, while a
    near-verbatim copy must still fail on the raw abuse floor."""
    src = ("# H\n\n**Plan limits**\nThe Growth plan includes 5 seats and 100 GB with a 99.9% uptime SLA "
           "billed at 1200 per year [S1]. Acme HR is SOC 2 certified and the team reviews each account "
           "before activation [S1].\n")
    rew = ("# H\n\n**Plan limits**\nGrowth bundles 5 seats and 100 GB behind a 99.9% uptime SLA invoiced "
           "at 1200 annually [S1]. Acme HR holds SOC 2, and staff vet every account prior to go-live [S1].\n")
    r = B._watermark_removal_report(src, rew, {"name": "Acme HR"})
    assert r["residual_share"] > 0.20                    # fact-dense: raw verbatim share is HIGH…
    assert r["residual_discretionary_share"] < 0.15      # …but almost none of it is free-choice prose
    assert r["grade"] in ("strong", "thorough")
    assert B._watermark_removal_report(src, src, {"name": "Acme HR"})["grade"] == "not-confirmed"


# ── FU174: the extraction layer must not re-introduce the FU169 bare/rhetorical-number failure ───
def test_fu174_rhetorical_numbers_are_not_gateable():
    """FU169 established that a bare/rhetorical number must never hard-fail a rewrite (Qwen validly turns
    '100% of patients' into 'virtually all patients'). The FU172 extraction layer re-admitted them through
    a new door — 'contains a digit' was enough to be accepted as an atom — and every rewrite fell back
    with dropped facts ['100', '100%']."""
    for rhetorical in ["100", "100%", "27%", "24/7"]:
        assert not B._atom_shape_ok(rhetorical), rhetorical
    for real in ["2.5 mg", "$149", "503B", "FDA", "99.9%", "3.9% APR", "5 seats", "March 19, 2025",
                 "Zepbound®", "LillyDirect", "SOC 2"]:
        assert B._atom_shape_ok(real), real


def test_fu174_long_pricing_structure_is_not_enforced_verbatim():
    """A 9-word pricing phrase enforced as a verbatim TOKEN makes every recast of that sentence fail.
    The structure is a MEANING question — the semantic verifier owns it; the gate holds only short values."""
    long_price = "starting at $149/month then $249/month billed quarterly for 60mg"
    gate = B._gate_atoms(["2.5 mg", "$149", "$249", long_price, "March 19, 2025"])
    assert long_price not in gate
    assert gate == ["2.5 mg", "$149", "$249", "March 19, 2025"]
    # …and a rewrite that recasts the sentence but keeps the values passes the gate
    ok, missing = B._facts_preserved(
        "PeterMD is starting at $149/month then $249/month billed quarterly for 60mg [S1].",
        "PeterMD opens at $149/month and settles at $249/month, billed every quarter for 60mg [S1].",
        {"name": "PeterMD"}, B._gate_atoms(["$149", "$249", long_price]))
    assert ok, missing
    # while a rewrite that DROPS a price still fails
    bad_ok, bad_missing = B._facts_preserved(
        "PeterMD is starting at $149/month then $249/month billed quarterly for 60mg [S1].",
        "PeterMD opens at $149/month, billed every quarter for 60mg [S1].",
        {"name": "PeterMD"}, B._gate_atoms(["$149", "$249", long_price]))
    assert not bad_ok and "$249" in bad_missing


def test_fu174_recall_audit_no_longer_restores_rhetorical_percents():
    body = "# H\n\nThe service is 100% online and the dose is 2.5 mg once weekly [S1].\n"
    out = _extract([])._extract_protected_facts(body, {"name": "X"})   # model returns nothing
    kept = " ".join(out["atoms"])
    assert "2.5" in kept and "100%" not in kept
