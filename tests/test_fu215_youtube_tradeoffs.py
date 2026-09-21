"""FU215 — the YouTube trade-offs segment stops arguing the competitors' case.

Both reviewed packages for the same Jolly Search blog failed the same five ways: the concession sat
on the DECIDING AXIS (the title's own question), the framing was asymmetric (3 competitors get
"X wins if…", the brand gets "Where Jolly Search has limits:"), the segment CLOSED on a competitor,
it ROUTED the purchase away, and it self-deprecated. Root cause: the whole specification was one
line that constrained nothing about axis, order, symmetry or routing.

This file locks three things:
  1. the rewritten PROMPT rule (Change 1) and the three restatements that would otherwise fight it
     (Change 2), including the operator's NAME THE BRAND rule;
  2. the deterministic backstop `_tradeoff_balance_note` (Change 3) firing on BOTH real failures —
     and on nothing else. A compliant segment coming back clean is what makes the warning worth
     reading, so the no-cry-wolf set runs the Change-1 worked example VERBATIM;
  3. the two cry-wolf fixes and the one coverage hole that writing that example exposed — each
     asserted in BOTH directions, so a later "simplification" fails the suite rather than silently
     re-opening them.

HONEST REACH: the checks catch 3 of the 5 defects (A/B/C) plus the NAME THE BRAND rule (D).
Asymmetric framing and self-deprecation are carried by the PROMPT rule only — they have no reliable
deterministic signature, and inventing one would cry wolf on good segments.

$0, no network (StubClaude).
"""
import pytest

import generators.blog_gen as G
from generators.blog_gen import BlogGenerator, _YT_RETRIES
from tests.stubs import StubClaude

NAME = "Jolly Search"
TQ = "which AI SEO agencies are best for California law firms in 2026"
TITLE = "Which AI SEO agencies are best for California law firms in 2026?"
BRAND = {"name": NAME, "domain_url": "https://jollysearch.com"}

# The source blog's FIRST table IS the option comparison, and the brand is one of its rows — the
# gate that keeps the competitor-dependent checks honest.
BODY = """Intro prose.

| Agency | Focus | Reviews |
|---|---|---|
| **Rankings.io** | Law firms only | 4.9 |
| **Consultwebs, Inc.** | One firm per market | n/a |
| **Juris Digital** | No long-term lock-in | 90% |
| **Jolly Search** | Citation layer | n/a |

More prose.
"""
# A first table that is NOT an option comparison: the brand is not a row, so `['Monthly cost',
# 'Contract']` must never be treated as competitor names.
PRICING_BODY = """| Metric | Value |
|---|---|
| Monthly cost | $2,000 |
| Contract | 6 months |
"""
ARTICLE = {"title": TITLE, "body_markdown": BODY}

_HEAD = "## SEGMENT 4, HONEST TRADEOFFS\n\n"
_CLOSE = ("\n\nSo: if the gap you're closing is Google rankings alone, any of these four will move "
          "it. If it's whether ChatGPT and Perplexity name your firm when a client asks, that's the "
          "layer Jolly Search builds.\n\n## SEGMENT 5, CTA\n\nRead the full comparison.\n")

# --- the Change-1 worked example, VERBATIM (the no-cry-wolf fixture) ------------------------------
COMPLIANT = _HEAD + """Two different jobs both get called "AI SEO" for law firms, and which one your firm is short on is what decides who you hire: legal-marketing depth, or getting named by the AI engines themselves.

**Rankings.io** is the better fit if what you need is a team that lives in legal every day, law firms exclusively, nine straight Inc. 5000 appearances, 4.9 across 200-plus reviews.

**Consultwebs** is the better fit if geographic exclusivity is what matters most: one firm per market, 26-plus years legal-only.

**Juris Digital** is the better fit if you're not ready to sign a long engagement, no long-term lock-in, attorney-reviewed pages, 90-plus percent retention.

**Jolly Search** is the better fit if what you need is that citation layer: branded backlinks, Reddit mention density and extraction-structured content run as one system, 500-plus clients and 25,000-plus earned backlinks since 2020, per Jolly Search's own numbers. It was built as citation infrastructure, not a full-service shop, so it isn't the fit for a firm that wants one agency writing its weekly blog posts and answering its intake calls.""" + _CLOSE

# --- the two REAL failures ------------------------------------------------------------------------
# NOTE the heading: `_AI_DASH_RE` rewrote the model's em-dash into a comma, so script 1 really does
# read "SEGMENT 4, HONEST TRADE-OFFS" (hyphenated). The locator must survive both spellings.
SCRIPT1 = """## SEGMENT 4, HONEST TRADE-OFFS

Here's where we have to be straight about who wins where.

Rankings.io wins if you want a team that lives in legal every day.

Consultwebs wins if geographic exclusivity matters most to you.

Where Jolly Search has limits: We are not a law-firm-exclusive agency.

If that is the thing you care about, Rankings.io or Consultwebs will serve that need better.

Where we're built for is authority-building and GEO systems.

The honest framing: for many California law firms, it's understanding which gap you're actually trying to close.

## SEGMENT 5, CTA

Read more.
"""

SCRIPT2 = """## SEGMENT 5, HONEST TRADEOFFS

We should be straight about the tradeoffs here.

Jolly Search is not a law-firm-exclusive agency.

We're not going to tell you our team has decades of legal-only experience, that would be inaccurate.

Honestly those two approaches can work together with Rankings.io on content.

Law-firm-exclusive content strategy, Rankings.io and Consultwebs are strong there.

## SEGMENT 6, CTA

Read more.
"""


def _note(script, body=BODY, tq=TQ, title=TITLE, name=NAME):
    return BlogGenerator._tradeoff_balance_note(script, body, name, tq, title)


def _reply(script):
    return {"title": TITLE, "demo_title": "", "mini_answer": "Four options.",
            "script_markdown": script, "chapters": [{"question": "Which?", "ts": "00:00"}],
            "captions_transcript": "c", "shot_list": ["a"], "thumbnail_text": "t",
            "cta": "Read it", "pinned_comment": "p", "tags": ["x"], "category": "Education"}


def _run(script=COMPLIANT, duration_min=0, article=None, brand=None):
    stub = StubClaude(call_handler=lambda p: _reply(script))
    out = BlogGenerator(stub, db=None).generate_youtube_script(
        brand or BRAND, article or ARTICLE, target_query=TQ, duration_min=duration_min)
    return out, stub


# =================================================================================================
# Change 1 — the rewritten rule reaches every prompt
# =================================================================================================

@pytest.mark.parametrize("dm", [0, 1.5, 2, 3, 10])
def test_the_rewritten_tradeoffs_rule_is_in_the_prompt_at_every_duration(dm):
    p = _run(duration_min=dm)[1].calls[0]
    for s in (
        # the literal FU203 test-locked token must survive the rewrite
        "MANDATORY HONEST-TRADEOFFS segment",
        'label the heading exactly "HONEST TRADEOFFS"',
        "DECIDING AXIS",
        "STATE {} 's WIN ON THE DECIDING AXIS".replace(" 's", "'s").format(NAME),
        "CONDITIONED ON THE BUYER'S SITUATION",
        "SAY WHAT IT IS BUILT FOR, NEVER WHAT IT IS NOT",
        "A BARE\n        NEGATIVE ABOUT {} IS BANNED IN EVERY WORDING".format(NAME),
        "THE DECIDING AXIS IS FIXED BY THE TITLE, NOT CHOSEN BY YOU",
        "RE-FRAME IT ONTO THE\n        DIMENSION UNDERNEATH IT",
        "AT MOST FOUR OPTIONS IN THIS SEGMENT",
        "SYMMETRY",
        "NAME THE BRAND, never \"we / our / us\" inside this segment",
        "at the BOTTOM ONLY",
        "MANDATORY CONCLUSION",
        "THE OPENING LINE NAMES THE DECIDING AXIS AND NO BRAND",
        "NEVER ROUTE THE PURCHASE AWAY",
        "NEVER SELF-DEPRECATE",
        "RE-FRAME, DO NOT TRANSCRIBE",
    ):
        assert s in p, (dm, s)


def test_the_conclusion_example_names_the_brand_rather_than_saying_our_lane():
    """The operator's rule, at the one spot the old draft still said 'that's our lane'."""
    p = _run()[1].calls[0]
    assert "that's what Jolly Search\n        is built for." in p
    assert 'Named, not "that\'s our lane"' in p


def test_the_anti_stuffing_guidance_rides_with_the_name_the_brand_rule():
    p = _run()[1].calls[0]
    assert "Anti-stuffing: name Jolly Search at the START of its own entry" in p
    assert "neutral third person" in p


def test_the_competitor_list_from_the_source_blog_is_injected():
    """The prompt rule and the deterministic check must agree on WHO the competitors are: the
    generator discards the brand block, so without this the model re-derives them from the body
    while the check reads the first table."""
    p = _run()[1].calls[0]
    assert "The options the source blog compares, besides Jolly Search:" in p
    # ';'-joined, because a cell like "Consultwebs, Inc." carries its own comma
    assert "Rankings.io; Consultwebs, Inc.; Juris Digital" in p
    assert "Treat exactly these as the competitors." in p


def test_a_first_table_that_is_not_the_comparison_injects_nothing():
    """A `| Metric | Value |` pricing table must never feed junk names into the prompt."""
    p = _run(article={"title": TITLE, "body_markdown": PRICING_BODY})[1].calls[0]
    assert "The options the source blog compares" not in p
    # the body itself still carries the table (it is the source), but nothing was promoted to a
    # competitor NAME — which is the only thing that could mislead the rule or the check.
    assert "Treat exactly these as the competitors." not in p


# =================================================================================================
# Change 2 — the three restatements point the same way (and the test-locked ones are untouched)
# =================================================================================================

def test_the_non_negotiable_restatement_points_at_the_shape_not_just_the_existence():
    p = _run(duration_min=2)[1].calls[0]
    assert "honest-tradeoffs segment IN THE SHAPE SPECIFIED ABOVE" in p
    assert "resolves the deciding axis to Jolly Search" in p


def test_the_short_form_beats_carry_the_new_shape():
    """At dm <= 3 the beat sheet IS the operative instruction, and it used to reproduce exactly the
    asymmetry Change 1 removes."""
    p = _run(duration_min=2)[1].calls[0]
    assert "HONEST TRADEOFFS" in p                       # FU203 test-locked literal, space form
    assert "open on the DECIDING AXIS with no brand named" in p
    assert "each win CONDITIONED on the buyer's situation" in p
    assert "Jolly Search LAST in the SAME frame" in p
    assert "ONE limit on a DIFFERENT dimension" in p
    assert "close the beat by resolving the deciding axis to Jolly Search" in p
    assert "Name Jolly Search in the third person, never 'we'." in p
    # beat 4
    assert "THE RECOMMENDATION — Jolly Search for the deciding axis" in p
    assert "the narrower case where another option fits better" in p


def test_the_density_clause_and_the_long_form_rule_are_untouched():
    """Both are asserted VERBATIM by tests/test_fu203.py — FU215 must not brush them."""
    p2 = _run(duration_min=2)[1].calls[0]
    assert "NEVER by dropping a named option, a figure, a price or a tradeoff" in p2
    assert "the runtime shrinks; the indexed text does not" in p2
    p10 = _run(duration_min=10)[1].calls[0]
    assert "tightest viable structure: answer" in p10
    assert "SHORT-FORM BEAT SHEET" not in p10


# =================================================================================================
# Change 3 — the deterministic backstop, against the two REAL failures
# =================================================================================================

def test_script_one_fires_a2_and_not_a1_because_its_brand_line_sits_after_the_routing():
    """Script 1's "Where we're built for…" line sits AFTER the Rankings.io/Consultwebs routing, so
    last_b > last_c and the ordering test is correctly silent. A single merged rule would miss it —
    this split is why (A) has two sub-tests."""
    n = _note(SCRIPT1)
    assert "ends on a shrug" in n                        # (A2)
    assert "closes on a competitor" not in n             # (A1) must NOT fire here


def test_script_two_fires_a1_and_not_a2_because_it_literally_ends_on_two_competitors():
    n = _note(SCRIPT2)
    assert "closes on a competitor" in n                 # (A1)
    assert "ends on a shrug" not in n                    # (A2) must NOT fire here


@pytest.mark.parametrize("script", [SCRIPT1, SCRIPT2])
def test_both_real_scripts_fire_the_deciding_axis_check(script):
    """The folded tokenizer is what makes this possible: the query says "agencies"/"firms" and the
    sentence says "law-firm-exclusive agency", so UNFOLDED the intersection is one generic token and
    no threshold >= 2 could ever fire on the sentence that motivated the feature."""
    n = _note(script)
    assert "the limit sits on the deciding axis" in n
    assert "agency" in n and "firm" in n


@pytest.mark.parametrize("script,phrase", [(SCRIPT1, "will serve that need better"),
                                           (SCRIPT2, "work together")])
def test_both_real_scripts_route_the_buyer_away(script, phrase):
    n = _note(script)
    assert "routes the buyer to a competitor" in n and phrase in n


@pytest.mark.parametrize("script", [SCRIPT1, SCRIPT2])
def test_both_real_scripts_fire_the_name_the_brand_check(script):
    """The operator's newest rule, and the one the model is most likely to drift on: the REST of the
    script is legitimately first-person, so the voice has to switch for exactly one segment."""
    assert 'instead of naming Jolly Search' in _note(script)


def test_the_note_carries_no_em_dash_or_arrow():
    """Unlike the generated fields, this note does NOT pass through the FU185 symbol scrub, so a
    stray symbol would ship raw into the export doc."""
    for s in (SCRIPT1, SCRIPT2):
        n = _note(s)
        assert n and "—" not in n and "–" not in n and "→" not in n


# =================================================================================================
# The no-cry-wolf set: a COMPLIANT segment must come back clean
# =================================================================================================

def test_the_worked_example_comes_back_completely_clean():
    assert _note(COMPLIANT) == ""


def test_why_each_check_is_silent_on_the_worked_example():
    sents = BlogGenerator._spoken_text(BlogGenerator._tradeoff_segment(COMPLIANT))
    # (A1): the last brand mention is after the last competitor mention (its entry + the conclusion).
    assert "Jolly Search" in sents[-1]
    # (A2): the final sentence names the brand.
    assert sents[-1].rstrip().endswith("that's the layer Jolly Search builds.")
    # (D): not one first-person marker anywhere in the segment.
    assert not G._TRADEOFF_FIRST_PERSON_RE.search(BlogGenerator._tradeoff_segment(COMPLIANT))


def test_the_buyer_profile_truncation_is_load_bearing_in_both_directions():
    """Change 1 REQUIRES the limit to name the buyer profile it is wrong for, and for THIS article
    that profile is built from the axis tokens themselves ("a firm that wants one agency"). Without
    the truncation the check fires on a rule-COMPLIANT line."""
    assert _note(COMPLIANT) == ""
    saved = G._TRADEOFF_PROFILE_RES
    try:
        G._TRADEOFF_PROFILE_RES = ()
        assert "the limit sits on the deciding axis" in _note(COMPLIANT)
    finally:
        G._TRADEOFF_PROFILE_RES = saved
    assert _note(COMPLIANT) == ""


def test_an_axis_concession_cannot_hide_behind_a_profile_clause():
    """The truncation cuts at the START of the profile clause, and a genuine axis concession has to
    state the axis BEFORE it — so this remains catchable."""
    s = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
         "Rankings.io is the better fit if you need legal every day.\n\n"
         "Jolly Search is the better fit if you need that citation layer. We are not a "
         "law-firm-exclusive agency, so a firm that needs one will be happier elsewhere." + _CLOSE)
    assert "the limit sits on the deciding axis" in _note(s)


def test_the_routing_check_is_second_person_scoped_in_both_directions():
    """Change 1 MANDATES "X is the better fit if/for …", so an unscoped `better fit for` would fire
    on the exact shape the rule requires."""
    base = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
            "{}\n\nJolly Search is the better fit if you need the citation layer." + _CLOSE)
    fires = base.format("Rankings.io would be the better fit for you if legal depth is the gap.")
    silent = base.format("Rankings.io is the better fit for firms that need day-to-day legal content.")
    assert "routes the buyer to a competitor" in _note(fires)
    assert _note(silent) == ""


def test_a_legitimate_price_concession_is_not_an_axis_concession():
    """"not the cheapest option for California law firms" scores THREE axis hits and is a perfectly
    good limit — the category-side gate is what separates it from the real defect."""
    s = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
         "Rankings.io is the better fit if you need legal every day.\n\n"
         "Jolly Search is the better fit if you need the citation layer. We are not the cheapest "
         "option for California law firms." + _CLOSE)
    assert "the limit sits on the deciding axis" not in _note(s)


@pytest.mark.parametrize("limit", [
    "We do not take on solo practitioners.",
    "We deliberately do not sell single-channel retainers.",
    "We're not a full-service web build shop.",
])
def test_other_honest_limits_stay_silent_on_the_axis_check(limit):
    s = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
         "Rankings.io is the better fit if you need legal every day.\n\n"
         "Jolly Search is the better fit if you need the citation layer. " + limit + _CLOSE)
    assert "the limit sits on the deciding axis" not in _note(s)


# =================================================================================================
# The coverage hole the NAME THE BRAND rule opened, and the two collision guards
# =================================================================================================

def test_a_pronoun_limit_still_fires_because_candidate_selection_is_block_based():
    """THE regression guard on the block floor: with "we" banned the natural limit reads "It isn't
    the fit for…", which names neither the brand nor a person — a subject-based selector would skip
    the very sentence the check exists to judge."""
    s = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
         "Rankings.io is the better fit if you need legal every day.\n\n"
         "Jolly Search is the better fit if you need that citation layer.\n\n"
         "It is not a law-firm-exclusive agency." + _CLOSE)
    assert "the limit sits on the deciding axis" in _note(s)


def test_a_competitors_own_conditioned_win_is_never_a_limit_candidate():
    """"Juris Digital … no long-term lock-in" carries a limit cue but is a competitor's WIN."""
    s = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
         "Juris Digital is the better fit if you want no long-term lock-in on a law firm agency.\n\n"
         "Jolly Search is the better fit if you need that citation layer." + _CLOSE)
    assert _note(s) == ""


def test_us_the_country_is_not_a_first_person_brand_close():
    """The marker set excludes `us` on purpose: the pattern is case-insensitive and THIS article's
    own query is "…in the US", so "the best option for US law firms" would read as a brand close and
    SILENCE a real (A2) failure."""
    s = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
         "Rankings.io is the better fit if you need legal every day.\n\n"
         "Jolly Search is the better fit if you need that citation layer.\n\n"
         "Either way that is the best option for US law firms.\n")
    n = _note(s)
    assert "ends on a shrug" in n                 # the real failure is NOT silenced
    assert "instead of naming" not in n           # and (D) does not false-positive on "US"


def test_second_person_is_required_and_never_flagged():
    """The conditioned-fit framing is built on "you / your" — flagging it would fight Change 1."""
    s = (_HEAD + "What your firm is short on decides who you hire.\n\n"
         "Rankings.io is the better fit if what you need is legal depth for your firm.\n\n"
         "Jolly Search is the better fit if what you need is the citation layer your firm lacks."
         + _CLOSE)
    assert _note(s) == ""


# =================================================================================================
# Inertness
# =================================================================================================

def test_a_script_with_no_tradeoffs_heading_is_silent_and_never_warns_about_a_missing_segment():
    """The prompt has always mandated the SEGMENT but only mandates its LABEL from FU215 on, so an
    older package must not be flagged for a heading it was never told to write."""
    s = ("## SEGMENT 4, THE COMPARISON\n\nWe are not a law-firm-exclusive agency and Rankings.io "
         "will serve you better.\n")
    assert _note(s) == ""


def test_a_segment_under_three_spoken_sentences_skips_the_conclusion_checks():
    s = _HEAD + "It is not a law-firm-exclusive agency.\n"
    n = _note(s)
    assert "ends on a shrug" not in n and "closes on a competitor" not in n


def test_without_a_competitor_list_the_name_dependent_checks_go_inert_and_b_stays_live():
    """No table at all, and a first table that is not the comparison, behave the same way."""
    for body in ("No table here at all.", PRICING_BODY):
        n = _note(SCRIPT2, body=body)
        assert "closes on a competitor" not in n          # (A1) inert
        assert "routes the buyer" not in n                # (C) inert
        assert "the limit sits on the deciding axis" in n  # (B) needs no competitor list


def test_an_empty_target_query_falls_back_to_the_article_title():
    assert "the limit sits on the deciding axis" in _note(SCRIPT2, tq="")


def test_no_axis_tokens_at_all_makes_the_axis_check_inert():
    assert "the limit sits on the deciding axis" not in _note(SCRIPT2, tq="", title="")


# =================================================================================================
# Wiring
# =================================================================================================

def test_the_warning_rides_youtube_meta_and_needs_no_schema_change():
    out, _ = _run(SCRIPT2)
    assert "tradeoff_warning" in out["meta"]
    assert "closes on a competitor" in out["meta"]["tradeoff_warning"]


def test_a_clean_segment_stores_no_warning_at_all():
    out, _ = _run(COMPLIANT)
    assert "tradeoff_warning" not in out["meta"]


def test_the_check_runs_on_the_default_path_where_no_duration_was_set():
    """The duration box is blank by default, so gating this on `dm` would silently disable it on the
    path almost every package takes. Length is meaningless without a target; balance is not."""
    out, _ = _run(SCRIPT2, duration_min=0)
    assert "length_warning" not in out["meta"]
    assert "duration_min" not in out["meta"]
    assert "tradeoff_warning" in out["meta"]


def test_the_check_reads_the_scrubbed_script_so_the_rewritten_em_dash_heading_still_matches():
    """`_AI_DASH_RE` turns the model's "SEGMENT 4 — HONEST TRADE-OFFS" into "SEGMENT 4, HONEST
    TRADE-OFFS"; a locator built against the raw JSON would drift off the real packages."""
    out, _ = _run(SCRIPT1.replace("SEGMENT 4, HONEST TRADE-OFFS",
                                  "SEGMENT 4 — HONEST TRADE-OFFS"))
    assert "—" not in out["script"]
    assert "tradeoff_warning" in out["meta"]


def test_the_checker_never_raises_and_a_failure_is_a_silent_no_op():
    assert BlogGenerator._tradeoff_balance_note(None, None, None) == ""
    assert BlogGenerator._tradeoff_balance_note("", BODY, NAME, TQ, TITLE) == ""


# =================================================================================================
# FU215b — the package that STILL shipped "Jolly Search is not a law-firm-exclusive agency"
# =================================================================================================
# The rule named that exact sentence as the defect and the model wrote it anyway: the source blog's
# only sourced limitation IS the banned one, CLAIMS DISCIPLINE forbids inventing another, and the
# model narrowed the deciding axis to "simultaneous Google + AI visibility" so it could argue the
# limit "is not a capability gap on the visibility axis". Three fixes, locked below.

SHIPPED_B = _HEAD + """Honest tradeoffs. The deciding axis here is simultaneous visibility across Google and AI answer engines, not one or the other, both at once.

Rankings.io is the better fit if what you need is a law-firm-exclusive agency with a nine-year track record.

Juris Digital is the better fit if what you need is no long-term contract lock-in.

Jolly Search is the better fit when the gap you are closing is Google and AI search visibility built simultaneously through one integrated system. The limit that is real and worth naming: Jolly Search is not a law-firm-exclusive agency. If your day-to-day content strategy requires a team whose entire practice is legal marketing, that is a genuine fit difference. It is a scope design choice, not a capability gap on the visibility axis.

If the gap is building cross-platform authority that gets your firm cited inside AI-generated answers, that is the layer Jolly Search is built for.
"""

REFRAMED = _HEAD + """What decides this is whether your gap is legal-marketing depth or getting named by the AI engines themselves.

Rankings.io is the better fit if what you need is a law-firm-exclusive agency with a nine-year track record.

Juris Digital is the better fit if what you need is no long-term contract lock-in.

Jolly Search is the better fit when the gap you are closing is Google and AI visibility built through one system. It is built as a citation layer rather than a full-service shop, so a firm that wants one agency writing its weekly blog posts and answering its intake calls will want a different partner.

If the gap you are closing is legal-vertical immersion, any of these will move it. If it is whether ChatGPT and Perplexity name your firm, that is the layer Jolly Search builds.
"""


# FU236 — REFRAMED is FU215's own worked example and stays exactly as it was, as the historical
# anchor. It does NOT satisfy FU236's publisher-lane rule: its two Jolly Search lines state a
# mechanism ("built through one system", "the layer Jolly Search builds") and never what that
# produces for the buyer. That is the defect FU236 exists for, and it was in the model answer.
# The loop tests below need a retry that is clean under EVERY check, so they use this instead.
REFRAMED_CLEAN = REFRAMED.replace(
    "that is the layer Jolly Search builds.",
    "that is the layer Jolly Search builds, and it is where the clients who ask an AI first come "
    "from.")


def test_the_shipped_sentence_fires_both_the_shape_check_and_the_axis_check():
    n = _note(SHIPPED_B)
    assert 'states a bare negative about Jolly Search ("Jolly Search is not...")' in n    # (E)
    assert "the limit sits on the deciding axis" in n                                     # (B)


def test_the_same_fact_re_framed_onto_scope_comes_back_clean():
    """The rule keeps the FACT and changes the dimension it is expressed on."""
    assert _note(REFRAMED) == ""


@pytest.mark.parametrize("line", [
    "Jolly Search does not do day-to-day content production.",
    "Jolly Search lacks a legal-only writing team.",
    "Jolly Search isn't a full-service shop.",
    "Jolly Search will never be the cheapest option.",
    "Where Jolly Search has limits: it is not legal-only.",
])
def test_a_bare_negative_about_the_brand_fires_in_any_wording(line):
    s = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
         "Rankings.io is the better fit if you need legal every day.\n\n"
         "Jolly Search is the better fit if you need the citation layer. " + line + _CLOSE)
    assert "states a bare negative about Jolly Search" in _note(s)


@pytest.mark.parametrize("line", [
    "Jolly Search is built as citation infrastructure, not a full-service shop.",
    "It was built as citation infrastructure, not a full-service shop.",
    "Jolly Search is the better fit if you need that citation layer.",
])
def test_the_positive_construction_is_never_mistaken_for_a_bare_negative(line):
    """The negation must follow the brand name IMMEDIATELY — "is built as X, not Y" is the shape
    the rule asks for, and "It …" is (B)'s territory, not (E)'s."""
    s = (_HEAD + "Legal depth or engine citation decides who you hire.\n\n"
         "Rankings.io is the better fit if you need legal every day.\n\n" + line + _CLOSE)
    assert "states a bare negative" not in _note(s)


def test_the_worked_example_is_still_clean_under_the_new_check():
    assert _note(COMPLIANT) == ""


# ---- the bounded self-correction -----------------------------------------------------------------

def _sequenced(*scripts):
    calls = {"n": 0}

    def handler(p):
        i = min(calls["n"], len(scripts) - 1)
        calls["n"] += 1
        return _reply(scripts[i]) if scripts[i] is not None else None
    return handler


def _run_seq(*scripts):
    stub = StubClaude(call_handler=_sequenced(*scripts))
    out = BlogGenerator(stub, db=None).generate_youtube_script(BRAND, ARTICLE, target_query=TQ)
    return out, stub


def test_a_clean_first_draft_costs_exactly_one_call():
    """The retry fires only when the check trips — a clean package costs what it costs today."""
    out, stub = _run_seq(COMPLIANT)
    assert len(stub.calls) == 1
    assert "tradeoff_warning" not in out["meta"]


def test_a_failing_draft_is_regenerated_once_and_the_clean_retry_ships():
    out, stub = _run_seq(SHIPPED_B, REFRAMED_CLEAN)
    assert len(stub.calls) == 2
    assert "is not a law-firm-exclusive agency" not in out["script"]
    assert "tradeoff_warning" not in out["meta"]


def test_the_retry_hands_the_model_its_own_failure():
    _, stub = _run_seq(SHIPPED_B, REFRAMED_CLEAN)
    retry = stub.calls[1]
    assert "CORRECTION" in retry
    assert "states a bare negative about Jolly Search" in retry
    assert "the deciding axis is the one the TITLE asks about and you may not narrow it" in retry
    assert retry.startswith(stub.calls[0])          # the full original prompt, plus the correction


def test_the_retry_budget_is_bounded():
    """FU215 allowed exactly one retry. FU236 raised it to `_YT_RETRIES`, because a flagged
    background segment shipped anyway after a single retry — but it is still BOUNDED, and a package
    that never converges still reaches the operator with its warning."""
    out, stub = _run_seq(*[SHIPPED_B] * (2 + _YT_RETRIES))
    assert len(stub.calls) == 1 + _YT_RETRIES
    assert "tradeoff_warning" in out["meta"]        # still failing: the operator still sees it


def test_a_still_failing_retry_ships_only_if_it_fails_fewer_checks():
    """SCRIPT1 carries more defects than SHIPPED_B, so the retry ships even though it also fails.
    FU236 scores by DEFECTS rather than by semicolons — each note joins its own sub-hits with "; ",
    so counting semicolons conflated "one check with three hits" with "three checks"."""
    out, _ = _run_seq(SCRIPT1, SHIPPED_B)
    assert "Here's where we have to be straight" not in out["script"]
    assert 'says "we"' not in out["meta"]["tradeoff_warning"]    # SCRIPT1's own defect is gone


def test_a_worse_retry_never_replaces_a_better_first_draft():
    out, _ = _run_seq(SHIPPED_B, SCRIPT1)
    assert "Here's where we have to be straight" not in out["script"]


def test_an_unusable_retry_keeps_the_first_draft_rather_than_returning_nothing():
    out, stub = _run_seq(SHIPPED_B, None)
    assert len(stub.calls) == 2
    assert out and "tradeoff_warning" in out["meta"]
