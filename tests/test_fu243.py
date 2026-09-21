"""FU243 — the page must stop arguing against the brand that published it.

Three shapes an insurance article exposed, none of which any existing check could see:

1. CATEGORY SELF-DISQUALIFICATION. The page states a true, cited regulatory rule about the
   publisher's OWN product class, says nothing about where the publisher stands inside it, and
   recommends the publisher two paragraphs later. Every check passed — the rule IS true, IS cited,
   IS about the category rather than the brand — and the page still told the reader not to buy from
   its publisher. The rule is never deleted; what is missing is the sentence about the publisher's
   position.

2. THE BEATEN PITCH. The body cited a $149/month self-pay figure for the same drug while the CTA
   sold the publisher's $270/month subscription on price. Both figures right, both sourced, and the
   page argued the reader into the cheaper option.

3. THE FRAMING ITSELF. The operator's instruction here was not "warn" but "change the way it is put
   across", so `_publisher_framing_pass` is the one pass in the file that REWRITES on a framing
   judgement — and every leg of it preserves the facts:
      a. the bold downside label only the publisher's profile carries is removed; the limitation it
         labelled stays, word for word;
      b. a comparison column the publisher can only answer with an absence is dropped — the remedy
         FU234 has recommended in words since it shipped;
      c. uniquely-harsh prose and a competitor's unearned superlative are reworded through the
         existing `_verify_repair_gate`, so a rewrite that loses a citation, a number, a name, or
         says a fact is unavailable is REFUSED and reported rather than applied.

The two guards that keep this honest are asserted as hard as the behaviour itself: a page whose
ledger is EVEN (a competitor is criticised too) is left alone, and a rewrite that drops a fact never
reaches the body.
"""
import json

import pytest

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

BRAND = {
    "name": "PeterMD", "domain_url": "https://getpetermd.com",
    "category": "telehealth clinic selling compounded semaglutide and tirzepatide programs",
    "features": ["compounded GLP-1 medication", "physician oversight"],
    "key_facts": json.dumps({"pricing": {"items": [
        {"product": "GLP-1", "value": "$270/month", "operator_set": True}]}}),
}

# a NON-medical brand, run through the same fixtures — nothing here may key on a vertical
LENDER = {
    "name": "Northgate", "domain_url": "https://northgate.example",
    "category": "equipment finance broker arranging merchant cash advances",
    "features": ["merchant cash advance", "same-day underwriting"],
    "key_facts": json.dumps({"pricing": {"items": [
        {"product": "advance", "value": "$900 origination fee", "operator_set": True}]}}),
}


def _gen(call_handler=None):
    return BlogGenerator(StubClaude(call_handler=call_handler), None)


# ══ 1. a rule that condemns the publisher's own product class ══════════════════════════════════════
_RULE_BODY = """# Does insurance cover semaglutide?

## Compounding

A 503A pharmacy may not compound a drug that is essentially a copy of a commercially available
drug [S3]. That restriction applies to compounded semaglutide across the market.

## What to do

PeterMD runs licensed physician oversight on every plan [S1].
"""


def test_a_rule_about_our_own_class_with_no_word_on_where_we_stand_is_flagged():
    note = _gen()._self_disqualification_check(_RULE_BODY, BRAND)
    assert note.startswith("self-disqualification:")
    assert "may not compound" in note
    # the remedy is NEVER "delete the rule"
    assert "Keep the rule" in note


def test_stating_where_the_brand_stands_silences_it():
    fixed = _RULE_BODY.replace(
        "That restriction applies to compounded semaglutide across the market.",
        "PeterMD works within that rule by prescribing patient-specific compounded semaglutide "
        "under the 503A exception.")
    assert _gen()._self_disqualification_check(fixed, BRAND) == ""


def test_a_rule_about_somebody_elses_class_is_silent():
    """The gate is the brand's OWN offering tokens — a rule about an adjacent category is news, not
    self-disqualification."""
    other = _RULE_BODY.replace("compound a drug", "operate a retail pharmacy chain") \
                      .replace("compounded semaglutide", "grocery pharmacy counters")
    assert _gen()._self_disqualification_check(other, BRAND) == ""


def test_the_check_is_vertical_neutral():
    body = """# Are merchant cash advances regulated?

## The rule

A broker may not arrange a merchant cash advance in this state without a lending licence [S2].
The restriction applies to every merchant cash advance sold here.

## What to do

Northgate underwrites the same day [S1].
"""
    assert _gen()._self_disqualification_check(body, LENDER).startswith("self-disqualification:")


def test_a_brand_with_no_stored_offering_is_inert():
    assert _gen()._self_disqualification_check(_RULE_BODY, {"name": "PeterMD"}) == ""


# ══ 2. the pitch the article's own figure has already beaten ═══════════════════════════════════════
_PITCH_BODY = """# Does insurance cover semaglutide?

PeterMD offers upfront pricing that is more affordable than most clinics.
Wegovy's pill is available from $149 per month on the manufacturer's site [S4].
"""


def test_a_cheaper_cited_figure_under_a_price_pitch_is_flagged():
    note = _gen()._beaten_pitch_check(_PITCH_BODY, BRAND, "semaglutide insurance")
    assert note.startswith("beaten-pitch:")
    assert "$270/month" in note and "$149" in note
    assert "Keep both figures" in note          # never hide the cheaper one


def test_the_same_figure_without_a_price_pitch_is_silent():
    body = _PITCH_BODY.replace(
        "PeterMD offers upfront pricing that is more affordable than most clinics.",
        "PeterMD includes licensed physician oversight and unlimited provider messaging.")
    assert _gen()._beaten_pitch_check(body, BRAND, "semaglutide insurance") == ""


def test_a_price_pitch_nothing_in_the_article_beats_is_silent():
    body = _PITCH_BODY.replace("$149 per month", "$499 per month")
    assert _gen()._beaten_pitch_check(body, BRAND, "semaglutide insurance") == ""


def test_our_own_figure_never_beats_our_own_pitch():
    body = """# x

PeterMD offers upfront pricing, with the first month at $99 and $270 per month after that.
"""
    assert _gen()._beaten_pitch_check(body, BRAND, "semaglutide") == ""


def test_an_order_of_magnitude_smaller_figure_is_not_the_competing_price():
    """A $10 copay or a $25 savings card is not the monthly price the pitch competes with."""
    body = _PITCH_BODY.replace("$149 per month", "$25 per month")
    assert _gen()._beaten_pitch_check(body, BRAND, "semaglutide insurance") == ""


def test_a_brand_with_no_canonical_price_is_inert():
    nokf = dict(BRAND, key_facts="{}")
    assert _gen()._beaten_pitch_check(_PITCH_BODY, nokf, "semaglutide insurance") == ""


# ══ 3a. the downside label only the publisher carries ══════════════════════════════════════════════
_PROFILE_BODY = """# Which telehealth platforms are best?

| Platform | Physician oversight | Community program |
|---|---|---|
| PeterMD | Included | None stated |
| Ro | Included | Active subreddit |
| Hims | Included | Active forum |

## How the options compare

### PeterMD

PeterMD runs licensed physician oversight on every plan [S1].

**Honest trade-off:** PeterMD does not publish a community forum [S1].

### Ro

Ro offers a large community [S2].

### Hims

Hims offers a large forum [S3].
"""


def _nollm():
    g = _gen()
    g.claude = None      # isolate the deterministic legs
    return g


def test_the_asymmetric_downside_label_is_removed_and_the_limitation_survives():
    out, note = _nollm()._publisher_framing_pass(_PROFILE_BODY, BRAND)
    assert "**Honest trade-off:**" not in out
    assert "PeterMD does not publish a community forum [S1]." in out   # the FACT is untouched
    assert "removed the downside label" in note
    assert "\n PeterMD" not in out                                     # no orphaned indent


def test_a_page_where_every_profile_states_a_limitation_is_left_alone():
    even = _PROFILE_BODY.replace(
        "Ro offers a large community [S2].",
        "Ro offers a large community [S2].\n\n**Honest trade-off:** Ro has no physician on staff [S2].")
    out, _note = _nollm()._publisher_framing_pass(even, BRAND)
    assert out.count("**Honest trade-off:**") == 2                     # symmetry, so nothing changed


# ══ 3b. the column the publisher can only answer with an absence ═══════════════════════════════════
def test_the_absence_column_is_dropped_and_the_other_columns_survive():
    out, note = _nollm()._publisher_framing_pass(_PROFILE_BODY, BRAND)
    assert "Community program" not in out
    assert "None stated" not in out
    assert "Physician oversight" in out and "| PeterMD | Included |" in out
    assert "Active subreddit" not in out                                # the whole column, not the cell
    assert "dropped the comparison column" in note


def test_a_column_the_publisher_answers_substantively_is_kept():
    kept = _PROFILE_BODY.replace("| PeterMD | Included | None stated |",
                                 "| PeterMD | Included | Active Discord |")
    out, _note = _nollm()._publisher_framing_pass(kept, BRAND)
    assert "Community program" in out and "Active Discord" in out


def test_the_last_remaining_dimension_is_never_dropped():
    one = """# x

| Platform | Community program |
|---|---|
| PeterMD | None stated |
| Ro | Active subreddit |
| Hims | Active forum |
"""
    out, _note = _nollm()._publisher_framing_pass(one, BRAND)
    assert "Community program" in out      # a one-column table is broken output, not a fix


# ══ 3c. harsh prose and an unearned superlative ════════════════════════════════════════════════════
_PROSE_BODY = """# Which telehealth platforms are best?

| Platform | Oversight |
|---|---|
| PeterMD | Included |
| Hims | Included |
| Noom Med | Included |

## Compare

PeterMD does not offer a community forum and is more expensive than the alternatives [S1].
Hims is the best option for most buyers and far cheaper than the rest [S2].
Noom Med includes coaching [S3].
"""


def _reframer(texts):
    """A stub that returns the given rewrites, and records the prompt it was handed."""
    seen = {}

    def handler(prompt, **kw):
        seen["prompt"] = prompt
        return {"rewrites": [{"n": i + 1, "text": t} for i, t in enumerate(texts)]}
    return handler, seen


def test_uniquely_harsh_publisher_prose_is_reworded_and_the_facts_hold():
    handler, seen = _reframer([
        "PeterMD is built for buyers who want physician oversight rather than a peer community, and "
        "it prices above the alternatives [S1].",
        "Hims pairs a large community with lower headline pricing [S2].",
    ])
    out, note = _gen(handler)._publisher_framing_pass(_PROSE_BODY, BRAND)
    assert "does not offer a community forum" not in out
    assert "built for buyers who want physician oversight" in out
    assert "reframed" in note
    # BOTH shapes were sent: the publisher's harsh line and the competitor's superlative
    probs = seen["prompt"]
    assert "publisher" in probs and "superlative framing" in probs
    # the prompt's hard rules are the contract the gate then enforces
    for must in ("Keep EVERY fact", "[S#]", "Never delete a true statement"):
        assert must in probs


def test_an_even_ledger_is_left_alone():
    """A page that criticises a competitor too is honest — leave it. This is what stops the pass
    becoming a machine for scrubbing every downside off the publisher."""
    even = _PROSE_BODY.replace(
        "Noom Med includes coaching [S3].",
        "Noom Med does not offer physician oversight on its cheapest plan [S3].").replace(
        "Hims is the best option for most buyers and far cheaper than the rest [S2].",
        "Hims includes a membership at $39 for the first month [S2].")
    called = {"n": 0}

    def handler(prompt, **kw):
        called["n"] += 1
        return {"rewrites": []}
    out, note = _gen(handler)._publisher_framing_pass(even, BRAND)
    assert "PeterMD does not offer a community forum" in out
    assert called["n"] == 0                       # no rewrite was even attempted
    assert "reframed" not in note


def test_a_superlative_the_publisher_also_gets_is_left_alone():
    """Overshadowing is about who is CROWNED, not about praise existing. When the publisher is
    praised too, the page is not hiding behind a competitor."""
    both = _PROSE_BODY.replace(
        "PeterMD does not offer a community forum and is more expensive than the alternatives [S1].",
        "PeterMD is the best option for buyers who want physician oversight [S1].")
    called = {"n": 0}

    def handler(prompt, **kw):
        called["n"] += 1
        return {"rewrites": []}
    out, note = _gen(handler)._publisher_framing_pass(both, BRAND)
    assert "Hims is the best option for most buyers" in out
    assert called["n"] == 0


def test_a_rewrite_that_drops_a_fact_is_refused_and_reported():
    handler, _seen = _reframer([
        "PeterMD suits buyers who want oversight.",          # dropped the [S1] citation
        "Hims is a solid pick.",                             # dropped [S2]
    ])
    out, note = _gen(handler)._publisher_framing_pass(_PROSE_BODY, BRAND)
    assert "PeterMD does not offer a community forum and is more expensive" in out   # untouched
    assert "left as written" in note and "citations" in note


def test_a_rewrite_that_says_a_fact_is_unavailable_is_refused():
    handler, _seen = _reframer([
        "PeterMD's community offering is not specified in the available sources [S1].",
    ])
    out, note = _gen(handler)._publisher_framing_pass(_PROSE_BODY, BRAND)
    assert "not specified in the available sources" not in out
    assert "left as written" in note


def test_the_pass_is_inert_without_a_brand_or_when_switched_off(monkeypatch):
    assert _gen()._publisher_framing_pass(_PROSE_BODY, {}) == (_PROSE_BODY, "")
    monkeypatch.setenv("BLOG_FRAMING_PASS", "0")
    assert _gen()._publisher_framing_pass(_PROFILE_BODY, BRAND) == (_PROFILE_BODY, "")


def test_a_clean_page_is_returned_byte_identical():
    clean = """# x

| Platform | Oversight |
|---|---|
| PeterMD | Included |
| Hims | Included |

## Compare

PeterMD includes licensed physician oversight [S1].
Hims includes a membership at $39 for the first month [S2].
"""
    called = {"n": 0}

    def handler(prompt, **kw):
        called["n"] += 1
        return {}
    out, note = _gen(handler)._publisher_framing_pass(clean, BRAND)
    assert out == clean and note == "" and called["n"] == 0


# ══ the prompt rules — the fix, of which every check above is the backstop ══════════════════════════
def test_the_writer_and_reconcile_prompts_carry_both_rules():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    assert "NEVER DISQUALIFY {name}'S OWN PRODUCT CLASS AND THEN PITCH IT" in src
    assert "DO NOT PITCH ON A POINT THIS ARTICLE'S OWN FACTS BEAT" in src
    assert "NEVER LEAVE {name}'S OWN CLASS DISQUALIFIED" in src
    assert "NEVER PITCH ON A POINT THE DRAFT'S OWN FACTS BEAT" in src


@pytest.mark.parametrize("term", ["semaglutide", "glp", "compounded", "insurance", "telehealth",
                                  "drug", "pharmacy", "clinic"])
def test_no_vertical_word_is_hard_coded_in_the_new_code(term):
    """Every fixture above is medical because that is the article that exposed this; the CODE must
    work the same for a lender, a contractor or an agency."""
    import re as _re
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    start = src.index("FU243 — the page must not argue against the brand that published it")
    end = src.index("def _press_release_position_check")
    block = src[start:end]
    # strip the docstrings and comments — those NAME the article that exposed each shape, which is
    # what makes them readable. What must be vertical-neutral is the code they describe.
    block = _re.sub(r'"""(?:.|\n)*?"""', "", block)
    block = _re.sub(r"(?m)#.*$", "", block)
    assert term not in block.lower()


# ══ the slot: a dropped column can take the last use of a source with it ═══════════════════════════
def test_dropping_a_column_does_not_leave_an_orphaned_source():
    """`_finalize_article` re-runs the Sources rebuild after this pass for exactly this reason: the
    only citation of a source can live inside the column the pass drops, and a Sources list that
    names a source nothing cites is the FU241 defect in reverse."""
    body = """# Which telehealth platforms are best?

| Platform | Oversight | Community program |
|---|---|---|
| PeterMD | Included [S1] | None stated |
| Ro | Included [S1] | Active subreddit [S2] |
| Hims | Included [S1] | Active forum [S3] |

## Sources

- [S1] PeterMD — https://getpetermd.com
- [S2] Ro — https://ro.co
- [S3] Hims — https://hims.com
"""
    g = _nollm()
    g._evidence_blocks = [
        {"label": "PeterMD", "url": "https://getpetermd.com", "text": "oversight"},
        {"label": "Ro", "url": "https://ro.co", "text": "community"},
        {"label": "Hims", "url": "https://hims.com", "text": "forum"},
    ]
    out, note = g._publisher_framing_pass(body, BRAND)
    assert "dropped the comparison column" in note
    rebuilt = g._rebuild_sources(out, BRAND)
    assert "ro.co" not in rebuilt and "hims.com" not in rebuilt   # nothing cites them any more
    assert "getpetermd.com" in rebuilt and "[S1]" in rebuilt
