"""FU252 — the rewording changes what the article asserts, and almost nothing checks that it didn't.

Two delivered articles came back at 70/100 and 73/100 with twelve defects between them. Every one
was introduced by the REWRITE pass, and every one sits outside what that pass checks: a length band,
[S#] set containment, a numeric fact gate, and the FU221 guard's citations/figures/key-terms test.

The measured shape of it, comparing every stored body with its own rewrite on the production
database — 68 pairs, 60 of them carrying at least one finding:

    73  figure-modifier-lost         26  acronym-expansion-invented    2  link-flattened
    39  claim-strengthened           16  hedge-lost                    1  citation-invented
    11  bold-lead-broken              5  link-lost                     1  acronym-expansion-changed

Two mechanisms are worth naming because they are exact.

THE "+" IS INVISIBLE END TO END. No regex in the repo escapes a literal "+" for this.
`_LOADBEARING_NUM_RE` (the rewrite's own fact gate), `_CLAIM_NUM_RE` and the guard's `_NUM_RE` all
fail the same way: `DR 70+`, `300+ firms` and `10+ years` match NOTHING, and `25,000+` matches as
`25,000`. `_nums("DR 70+ sites") == _nums("DR 70 sites")` is True. The figure's VALUE survives, which
is all any gate looks at; the modifier carrying the meaning does not.

THE BROKEN LEAD-INS ARE EXACTLY THE BOLD PHRASES WITHOUT A COLON. `**Quick answer:**`,
`**Best fit for:**` and `**Honest trade-off:**` all survive. `**California's environmental permit
requirements**` does not — because it is not a label, it is the sentence's SUBJECT. The guard
compares only the label's text and splices it in front of an independently reworded clause, so the
subject loses its verb.

The fixtures are the operator's four files, unedited: the two originals and the two rewrites.

A note on the detectors themselves. The first version of this file reported 171 figure findings; 98
of them were mine, not the rewriter's, and each false-positive class is pinned below as its own test:
a bound word binds the NEAREST number, "over the past 10 years" is a span and not a floor, "70 and
over 25,000" belongs to 25,000, and `[S14]` is a citation and not the value fourteen.
"""
import glob
import os

import pytest

from generators.blog_eval import (body_damage, detect_acronym_expansion_changed,
                                  detect_bold_lead_broken, detect_claim_strengthened,
                                  detect_figure_modifier_lost, detect_link_lost,
                                  detect_trailing_orphan, rewrite_findings, _figure_bounds)

HERE = os.path.dirname(__file__)


def _fx(name):
    with open(os.path.join(HERE, "fixtures", f"fu252_{name}.md"), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def law():
    """"Which AI SEO Agencies Are Best for California Law Firms?" — (original, rewrite)."""
    return _fx("lawfirms_original"), _fx("lawfirms_rewrite")


@pytest.fixture(scope="module")
def con():
    """"Which AI SEO Agencies Work Best for Construction Businesses?" — (original, rewrite)."""
    return _fx("construction_original"), _fx("construction_rewrite")


# ── 1. every reported defect is detected on the real pair ────────────────────────────────────────
def test_the_wrong_acronym_expansion_is_detected(law):
    """GEO came back as "global engagement optimization" in an article whose publisher SELLS GEO.
    The guard could not see it: it treats an acronym and its spelled-out form as interchangeable on
    purpose, so "GEO" being present satisfied it while the words around it were rewritten."""
    hits = detect_acronym_expansion_changed(*law)
    assert hits and hits[0]["severity"] == "blocking"
    assert "global engagement optimization" in hits[0]["detail"]
    assert "generative engine optimization" in hits[0]["detail"]


def test_one_wrong_expansion_among_four_right_ones_is_still_found(law):
    """The rewrite keeps the CORRECT expansion four times and adds the wrong one once. Any test that
    asks "does at least one expansion still match" — a set intersection — reports nothing."""
    o, r = law
    assert r.lower().count("generative engine optimization") == 4
    assert r.lower().count("global engagement optimization") == 1
    assert detect_acronym_expansion_changed(o, r)


def test_the_dropped_case_studies_link_is_detected(law):
    """"have used this system [S2], with [case studies showing organic traffic growth](…/case-studies/)"
    became "resulting in documented organic traffic growth … [S15]" — the link to the publisher's own
    evidence gone, and the claim turned causal. The URL gate that would catch this exists but is
    switched off for the blog surface."""
    hits = detect_link_lost(*law)
    assert any("jollysearch.com/case-studies" in h["detail"] for h in hits)
    assert all(h["severity"] == "blocking" for h in hits if h["check"] == "link-lost")


@pytest.mark.parametrize("fixture,expect", [("law", "make it suitable"), ("con", "ensur")])
def test_the_claim_got_stronger(request, fixture, expect):
    """"this is the layer that determines whether your brand appears" became "This ensures that your
    brand is mentioned when a property developer asks an AI assistant" — a capability turned into a
    guarantee nobody can make. And "can be relevant for" became "make it suitable for"."""
    o, r = request.getfixturevalue(fixture)
    hits = detect_claim_strengthened(o, r)
    assert hits and hits[0]["severity"] == "blocking"
    assert expect in hits[0]["detail"]


def test_all_three_broken_lead_ins_are_detected(con):
    """And only those three — the file's seven `**Best fit for:**` labels are untouched."""
    hits = detect_bold_lead_broken(*con)
    assert len(hits) == 3
    joined = " ".join(h["detail"] for h in hits)
    for lab in ("environmental permit requirements", "seismic and building code references",
                "Reddit brand mention campaigns"):
        assert lab in joined
    assert "Best fit for" not in joined


def test_a_colon_label_is_never_treated_as_a_subject(con):
    """`**Quick answer:**` and `**Best fit for:**` are labels a reader scans; the guard restores them
    verbatim and that is correct. Only a bold phrase with no colon is the sentence's subject."""
    _o, r = con
    assert r.count("**Best fit for:**") >= 5
    assert not any("Best fit for" in h["detail"] for h in detect_bold_lead_broken(*con))


@pytest.mark.parametrize("fixture,value", [("law", "10"), ("con", "10"), ("con", "70")])
def test_a_figure_that_lost_its_bound_is_detected(request, fixture, value):
    """"10+ years" → "10 years" and "DR 70+" → "an average DR of 70". The value survived, which is
    all any existing gate looks at; "at least 70" and "an average of 70" are different claims."""
    o, r = request.getfixturevalue(fixture)
    hits = detect_figure_modifier_lost(o, r)
    assert any(f'"{value}"' in h["detail"] for h in hits), [h["detail"] for h in hits]
    assert all(h["severity"] == "blocking" for h in hits)


def test_the_orphaned_fragment_is_detected(law):
    """"…is not enough to ensure a strong online presence for AI citations. Community Mentions" —
    a phrase stuck on the end with no sentence around it. Every other damage detector is anchored to
    the START of a unit or needs a punctuation seam, and this shape has neither."""
    _o, r = law
    hits = detect_trailing_orphan(r)
    assert len(hits) == 1 and "Community Mentions" in hits[0]["detail"]


def test_the_whole_report_for_each_audited_pair(law, con):
    """The gate the project did not have for this pass: deterministic, free, no model, no network."""
    assert {h["check"] for h in rewrite_findings(*law)} == {
        "figure-modifier-lost", "link-lost", "claim-strengthened", "acronym-expansion-changed"}
    assert {h["check"] for h in rewrite_findings(*con)} == {
        "figure-modifier-lost", "bold-lead-broken", "claim-strengthened"}


# ── 2. an article compared with itself changes nothing ───────────────────────────────────────────
@pytest.mark.parametrize("name", ["lawfirms_original", "lawfirms_rewrite",
                                  "construction_original", "construction_rewrite"])
def test_a_body_against_itself_reports_nothing(name):
    """The control that makes every number above meaningful. A rewording is allowed to change every
    word; these detectors must only fire on a change to what the article ASSERTS."""
    body = _fx(name)
    assert rewrite_findings(body, body) == []


def test_the_orphan_detector_does_not_fire_on_ordinary_prose():
    """Across every fixture in the repo — four verticals — it fires once, on the reported case.
    A list item legitimately ends without a full stop, which is why the rule needs BOTH an internal
    sentence boundary and a missing terminal punctuation mark."""
    fired = {os.path.basename(f) for f in glob.glob(os.path.join(HERE, "fixtures", "*.md"))
             if detect_trailing_orphan(open(f, encoding="utf-8").read())}
    assert fired == {"fu252_lawfirms_rewrite.md"}


# ── 3. a bound word binds the NEAREST number ─────────────────────────────────────────────────────
# Every case below was a live false positive on the production corpus. Together they took the figure
# class from 171 findings to 73 — 98 of the original count were the detector's fault, not the
# rewriter's, which is the whole reason this is measured against 68 real pairs before being gated on.
@pytest.mark.parametrize("text,num,want", [
    # the modifier, in each of its shapes
    ("rating based on 20,000+ reviews", "20000", "lower"),
    ("keyword backlinks averaging DR 70+", "70", "lower"),
    ("rating based on over 20,000 reviews", "20000", "lower"),
    ("trusted by more than 400,000 users", "400000", "lower"),
    ("each domain has a minimum DR of 50", "50", "lower"),
    ("adults with a BMI of 30 or higher", "30", "lower"),
    ("achieved a 10% or greater weight loss", "10", "lower"),
    ("achieved at least a **15%** reduction", "15", "lower"),
    ("guidelines (BMI ≥ 30, or ≥ 27 with", "30", "lower"),
    ("costs up to 250 dollars", "250", "upper"),
    ("baseline weight at approximately 1.5 years", "1.5", "approx"),
    ("initial weight within roughly 1.5 years", "1.5", "approx"),
    # …and the shapes that are NOT a bound
    ("an average placement of DR 70", "70", "exact"),
    ("helped brands grow over the past 10 years", "10", "exact"),
    ("helped brands grow over a period of 10 years", "10", "exact"),
    ("an average DR of 70 and over 25,000 monthly visitors", "70", "exact"),
    ("reached at least 15% against 56.2%", "56.2", "exact"),
])
def test_the_bound_classifier(text, num, want):
    got = _figure_bounds(text).get(num) or {}
    assert want in got, f"{text!r} → {dict(got)}"


@pytest.mark.parametrize("text,num", [
    ("free discreet home delivery included. [S1] Other options", "1"),
    ("patients can receive care from home [S14]. For men who", "14"),
])
def test_a_citation_marker_is_not_a_figure(text, num):
    """`rewrite_guard._nums` already strips these before comparing numbers. Without the same strip
    here, "[S1]" and "[S14]" were read as the values one and fourteen."""
    assert not (_figure_bounds(text).get(num) or {})


def test_a_correct_rewording_of_a_bound_is_not_a_finding():
    """The check is on MEANING, not characters. "25,000+" may become "over 25,000" or "at least
    25,000" freely — it may not become nothing."""
    a = "The agency built 25,000+ backlinks and serves 650+ brands."
    b = "The agency has built over 25,000 backlinks and serves more than 650 brands."
    assert detect_figure_modifier_lost(a, b) == []
    c = "The agency has built 25,000 backlinks and serves 650 brands."
    assert detect_figure_modifier_lost(a, c)


def test_a_dropped_repetition_is_not_a_lost_bound():
    """A rewrite that merges two sentences and drops a repeat has tightened the prose, not changed
    the claim — so the figure must have SURVIVED as often as before for this to be a finding."""
    a = "We serve 650+ brands. Our 650+ brands span every vertical."
    b = "We serve more than 650 brands across every vertical."
    assert detect_figure_modifier_lost(a, b) == []


# ── 4. generality ────────────────────────────────────────────────────────────────────────────────
def test_the_detectors_name_no_vertical():
    """Every rule here is shape-based. A rule that keys on a topic would work for one client only.

    Comments and docstrings are stripped with the tokenizer before the check, so quoting the real
    audited example in a docstring is fine — it is the LOGIC that may not name a vertical."""
    import io as _io
    import re
    import tokenize
    src = open(os.path.join(HERE, "..", "generators", "blog_eval.py"), encoding="utf-8").read()
    i = src.index("# ── FU252")
    block = src[i:src.index("def detect_publisher_only_downside", i)]
    kept = []
    for tok in tokenize.generate_tokens(_io.StringIO(block).readline):
        if tok.type == tokenize.COMMENT:
            continue
        if tok.type == tokenize.STRING and tok.line.lstrip()[:3] in ('"""', "'''"):
            continue                          # a docstring may quote the audited example
        kept.append(tok.string)
    code = " ".join(kept)
    for term in ("law", "legal", "seo", "construction", "medical", "clinic", "drug", "glp",
                 "wegovy", "backlink"):
        assert not re.search(r"\b%s\b" % term, code, re.I), f"'{term}' hard-coded in the detectors"


def test_every_finding_carries_a_severity(law, con):
    """The operator's rule for this round: a limit and criteria, not a binary. Severity is what the
    budget in Step 7 is applied to, so a finding without one cannot be graded."""
    for o, r in (law, con):
        for h in rewrite_findings(o, r):
            assert h["severity"] in ("blocking", "budgeted"), h
