"""FU253 — the operator entered the right prices and almost nothing used them.

Correct prices were supplied for every brand and the article still shipped five defects. The headline
finding is not a missing check. It is that the data was there and the generator picked the wrong row:

    ENTERED                                        PRINTED
      Ro   Wegovy pen              $349  product     Ro    $349 (per month, the product only)
      Ro   membership         $74-$149  program     Hims  From $199 (per month, the product only)
      Ro   Wegovy pen + membership  $423-$498  plus   ← the total, never used
      Hims Wegovy pen         from $199  product
      Hims membership             $149  program
      Hims Wegovy pen + membership from $348  plus    ← the total, never used

`_price_row_for` matched on product-token intersection and took the FIRST match. None of those
product strings shares a token with a "semaglutide cost per month" article, so the match found
nothing at all and the first row won by default. A part-price was then ranked against the publisher's
own ALL-IN price, which is what made the article say "$149-$299/month … willing to pay a premium"
when $149 is not a premium over $270.

And the price-source check written in FU251 to stop a price coming from anywhere was INERT on this
article — 0 of its 8 price sentences reached it — because `_PRICE_CTX_RE` had `/mo\\b`, which does not
match `/month` (the `\\b` fails on the `n`), and `$270/month` is the commonest way anyone writes a
price. A list price cited to a magazine feature and a compounded price cited to a consumer blog both
walked past the gate built for exactly that.
"""
import os
import re

import pytest

from generators.blog_gen import (BlogGenerator, _MONEY_RE, _format_price_value, _product_tokens)

HERE = os.path.dirname(__file__)

# The rows the operator actually entered, verbatim from the production price table.
RO_ROWS = [
    {"product": "Wegovy pen", "kind": "exact", "value": "$349", "basis": "per month",
     "composition": "product"},
    {"product": "membership", "kind": "range", "value": "$74", "value_max": "$149",
     "basis": "per month", "composition": "program"},
    {"product": "Wegovy pen + membership", "kind": "range", "value": "$423", "value_max": "$498",
     "basis": "per month", "composition": "plus"},
]
HIMS_ROWS = [
    {"product": "Wegovy pen", "kind": "from", "value": "$199", "basis": "per month",
     "composition": "product"},
    {"product": "membership", "kind": "exact", "value": "$149", "basis": "per month",
     "composition": "program"},
    {"product": "Wegovy pen + membership", "kind": "from", "value": "$348", "basis": "per month",
     "composition": "plus"},
]


@pytest.fixture
def gen():
    return BlogGenerator.__new__(BlogGenerator)


@pytest.fixture(scope="module")
def shipped():
    """"How Much Does Semaglutide Cost Per Month in the US?" as published."""
    with open(os.path.join(HERE, "fixtures", "fu253_semaglutide_cost_body.md"), encoding="utf-8") as f:
        return f.read()


# ── 1. the price check has to be able to SEE a price ─────────────────────────────────────────────
@pytest.mark.parametrize("text", [
    "PeterMD offers a GLP-1 program at $270/month at all doses",
    "Brand-name Wegovy retails for nearly $1,400/month on average without insurance",
    "Compounded semaglutide: Can start as low as $129/month",
    "$500-$1,865/month (retail brand-name, no savings program)",
    "the plan is $99/mo",
    "billed at $1,200/year",
    "it costs $270 a month",
])
def test_a_price_is_recognised_as_one(gen, text):
    """`/mo\\b` does not match `/month` — the word boundary fails on the "n" — so the single
    commonest way to write a monthly price was invisible. Measured on the article that exposed it:
    0 of 8 price sentences reached the check."""
    assert gen._PRICE_CTX_RE.search(text), text


@pytest.mark.parametrize("text", [
    "The company raised $150 million in its Series C round",
    "the regulator issued a $2.3 million penalty",
    "the category reached $24 billion in 2024",
    "backed by a $1 million surety bond",
])
def test_widening_did_not_turn_every_dollar_into_a_price(gen, text):
    """The FU251 not-a-price guards still hold. A market size, a penalty, a funding round and an
    indemnity are not prices, and each of these was a live false positive once already."""
    assert not any(gen._is_price_figure(text, m) for m in _MONEY_RE.finditer(text))


def test_every_price_sentence_in_the_shipped_article_is_now_checked(gen, shipped):
    """The whole point: it is not that the check was wrong, it is that it never ran."""
    prose = shipped.split("## Sources")[0]
    seen = 0
    for line in prose.split("\n"):
        st = line.strip()
        if not st or st.startswith(("#", ">", "*[", "|")):
            continue
        for sent in gen._prose_sentences(line):
            if any(gen._is_price_figure(sent, m) for m in _MONEY_RE.finditer(sent)):
                seen += 1
                assert gen._PRICE_CTX_RE.search(sent), f"still invisible: {sent[:90]}"
    assert seen >= 6, f"expected the article's price sentences, found {seen}"


# ── 2. when the operator has entered the total, use the total ────────────────────────────────────
@pytest.mark.parametrize("name,rows,total", [
    ("Ro", RO_ROWS, "$423-$498"),
    ("Hims", HIMS_ROWS, "From $348"),
])
def test_the_total_wins_over_the_part_price(gen, name, rows, total):
    """A part-price ranked against a rival's all-in price is not a comparison. The operator said
    which row is the total; the picker now reads it. Asserted on the FIGURE, because how the total
    is worded is a separate decision (see the split tests below)."""
    entry, _amb = gen._price_row_for(rows, name, _product_tokens("semaglutide cost per month"),
                                     subject="PeterMD")
    assert _format_price_value(entry).startswith(total)
    assert "the product only" not in _format_price_value(entry)


def test_the_membership_alone_is_never_the_headline_price(gen):
    """A programme fee on its own is the least useful of the three rows — it is neither the product
    nor the total — so it must never be the one that reaches the comparison cell."""
    for rows in (RO_ROWS, HIMS_ROWS):
        cell = _format_price_value(gen._price_row_for(rows, "X", _product_tokens("anything at all"))[0])
        assert cell.startswith(("$423", "From $348")), cell


def test_a_brand_with_one_row_is_untouched(gen):
    one = [{"product": "GLP-1", "kind": "exact", "value": "$270", "basis": "per month",
            "composition": "all-in"}]
    entry, amb = gen._price_row_for(one, "PeterMD", _product_tokens("semaglutide cost"))
    assert _format_price_value(entry) == "$270 (per month, everything included)"
    assert amb is False


def test_rows_the_operator_never_graded_keep_the_old_behaviour(gen):
    """Every brand in the stored corpus whose rows carry no composition must pick exactly as before:
    the product match if there is one, otherwise the first row. Measured across 122 stored blogs and
    366 multi-row cells, only two (publisher, competitor) pairs move — and those are the two the
    operator entered a total for."""
    rows = [{"product": "9 oz single", "kind": "exact", "value": "$8.99"},
            {"product": "3-pack", "kind": "exact", "value": "$23.97"}]
    matched, _ = gen._price_row_for(rows, "X", _product_tokens("9 oz single bottle"))
    assert _format_price_value(matched) == "$8.99"          # the product match still wins
    first, amb = gen._price_row_for(rows, "X", _product_tokens("a totally unrelated topic"))
    assert _format_price_value(first) == "$8.99"            # …and otherwise the first row, as before
    assert amb is True                                      # with the same "I could not tell" flag


def test_ambiguity_is_only_reported_when_nothing_decided_it(gen):
    """The operator reads these warnings. Reporting a pick their own data settled is noise."""
    _e, amb = gen._price_row_for(RO_ROWS, "Ro", _product_tokens("semaglutide cost"), subject="PeterMD")
    assert amb is False


# ── 3. a constraint stated once is information; stated three times it is the article ─────────────
from generators.blog_eval import (detect_constraint_bloat, detect_repeated_constraint,  # noqa: E402
                                  _constraint_passages, _same_constraint, _constraint_tokens)


def test_the_compounding_rule_stated_three_times_is_detected(shipped):
    """The operator's words: "the three compounding legal paragraphs … with the legal text cut to
    one line, PeterMD looks strong". Three sections, 294 words, 14% of a page whose question is what
    something COSTS."""
    hits = detect_repeated_constraint(shipped)
    assert len(hits) == 1
    d = hits[0]["detail"]
    assert "3 sections" in d and "14% of the article" in d


def test_the_passages_are_paraphrases_not_copies(shipped):
    """Which is why nothing saw them. `detect_repeated_sentence` needs a verbatim repeat, and the one
    sentence that IS verbatim sits in the body and the FAQ, which FU251 exempts on purpose."""
    import difflib
    pars = [p for _h, p, _t in _constraint_passages(shipped) if "503" in p]
    assert len(pars) >= 3
    worst = max(difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio()
                for i, a in enumerate(pars) for b in pars[i + 1:])
    assert worst < 0.55, f"these are near-copies ({worst:.2f}); the cheap detector would have caught them"


def test_a_constraints_identity_is_what_is_SPECIFIC_to_it(shipped):
    """Grouping on raw tokens merged three unrelated rules in one article and a state-availability
    note with a pharmacy-law rule in another, because in a compounding article every rule mentions
    compounding. A token in the title, or in half the sections, names the topic, not the rule."""
    toks = [t for _h, _p, t in _constraint_passages(shipped)]
    assert all("semaglutide" not in t for t in toks), "a topic word still carries identity"


def test_the_volume_of_caveat_is_its_own_finding(shipped):
    """Separate from repetition: how much of the page is rule text at all. Measured across 217
    stored articles the median is 3%, p90 is 15%; this one is 24%."""
    hits = detect_constraint_bloat(shipped)
    assert len(hits) == 1 and "24% of the article" in hits[0]["detail"]


def test_an_article_whose_question_IS_the_rule_is_exempt(shipped):
    """"Is Compounded Tirzepatide Legit?" gives 22% of itself to regulatory text and that is the
    article working. The exemption is shape-based — the title asks about legality, eligibility or
    compliance — never a topic list."""
    assert detect_constraint_bloat(shipped, title="Is Compounded Semaglutide Legal?") == []
    assert detect_constraint_bloat(shipped, title="Who Is Eligible for Coverage?") == []
    assert detect_constraint_bloat(shipped)          # …and the price article is not exempt


def test_a_clause_length_reference_is_the_goal_not_a_repeat():
    """The fix the prompt asks for — one full statement, a clause everywhere else — must not itself
    read as a repetition."""
    body = ("# T\n\n## The rule\n\nUnder 503A rules a pharmacy may not compound a drug that is "
            "essentially a copy of a commercially available product, and the shortage was declared "
            "resolved in February 2025 with a wind-down deadline in April.\n\n"
            "## Price\n\nCompounded options, subject to the 503A rules above, start lower.\n\n"
            "## Choosing\n\nAsk any provider about the 503A rules above before you buy.\n")
    assert detect_repeated_constraint(body) == []


def test_the_writer_is_told_the_proportion_rule():
    golden = open(os.path.join(HERE, "fixtures", "prompts", "generate_article.ymyl.txt"),
                  encoding="utf-8").read()
    assert "SAY A RULE ONCE, WHERE IT BELONGS" in golden
    assert "REPETITION and PROPORTION only" in golden
    # and the boundary is stated to the model, not just to us
    assert "never soften or drop it because it is inconvenient" in golden


# ── 4. a price question with no price in its answer ──────────────────────────────────────────────
from generators.blog_eval import (detect_price_question_unanswered, editorial_findings,  # noqa: E402
                                  body_damage, _ASKS_PRICE_RE)


def test_the_unanswered_price_heading_is_detected(shipped):
    """"What Is the Retail Price of Brand-Name Wegovy Without Insurance?" over a section that talks
    about Medicare and gives no price. `_answer_first_check` cannot see it — that looks for nine
    stalling phrases in the first sentence, and this section opens fluently, confidently and
    entirely on the wrong subject."""
    hits = detect_price_question_unanswered(shipped)
    assert len(hits) == 1
    assert "Retail Price of Brand-Name Wegovy" in hits[0]["detail"]


@pytest.mark.parametrize("head,asks", [
    ("How much does semaglutide cost per month in the US?", True),
    ("What Is the Retail Price of Brand-Name Wegovy Without Insurance?", True),
    ("What does it cost?", True),
    # "how much" is not a price question on its own
    ("How much weight can a man expect to lose on semaglutide?", False),
    ("How much weight do people typically regain after stopping?", False),
    # asking what GOVERNS pricing is not asking what it costs
    ("US Regulatory Context: What Governs Semaglutide Pricing and Access?", False),
    ("What is the difference between a membership fee and the medication cost?", False),
    ("How long does it take to see results?", False),
])
def test_only_a_real_price_question_qualifies(head, asks):
    """Naive heading-to-body term overlap flagged 8 of 13 headings in this article, so the rule is
    narrow by design — and each of these was a live false positive while narrowing it."""
    assert bool(_ASKS_PRICE_RE.search(head)) is asks


def test_a_section_answered_by_its_TABLE_counts_as_answered():
    """`_sections` drops table rows so no detector reads one as prose — right everywhere else, wrong
    here, and it was 2 of the 3 false positives in the first cut."""
    body = ("# T\n\n## How much does it cost per month?\n\nThe table below compares the providers.\n\n"
            "| Provider | Monthly cost |\n| --- | --- |\n| Acme | $270/month |\n")
    assert detect_price_question_unanswered(body) == []


# ── 5. a price with no source at all ─────────────────────────────────────────────────────────────
def test_a_price_citing_nothing_is_reported(gen, shipped):
    """Both price checks open with "no citation in this unit → skip", so a price cited to the wrong
    kind of page is caught and a price cited to NOTHING is invisible. The reported article carries a
    "$500-$1,865/month (retail brand-name)" tier whose figures appear nowhere else in it."""
    note = gen._uncited_price_check(shipped)
    assert "$1,865" in note and "$500" in note


def test_a_price_restated_from_a_cited_one_is_not_uncited(gen):
    """The citation is elsewhere, not missing."""
    body = ("# T\n\n## Cost\n\nThe programme costs $270 per month [S1].\n\n"
            "## Summary\n\nAt $270 per month it is the flat option.\n")
    assert gen._uncited_price_check(body) == ""


def test_the_uncited_price_check_never_removes_anything(gen, shipped):
    """Warning only, deliberately: the shapes it finds are TIER LABELS — "Under $300/month", the
    bucket the article defines, sits beside "$500-$1,865/month", the invented one — and removing a
    tier label breaks the structure the section is built on, which is the damage class FU251 exists
    to stop."""
    before = shipped
    assert isinstance(gen._uncited_price_check(before), str)   # it returns a NOTE, not a body
    assert gen._uncited_price_check(before)                    # …and it still reports


def test_an_editorial_finding_never_reaches_the_removal_guard(shipped):
    """`body_damage` is what `_apply_removals_without_damage` consults: a removal that raises its
    count is widened or refused. Measured the hard way — with the price-question detector inside it,
    removing the only (unsourced) figure from a "What does it cost?" section raised the count, the
    removal was refused, and the unsourced figure would have shipped. `constraint-bloat` is worse: it
    is a SHARE, so ANY removal can push it up. A detector that switches off a removal protects the
    defect it was written to expose."""
    editorial = {h["check"] for h in editorial_findings(shipped)}
    damage = {h["check"] for h in body_damage(shipped)}
    assert editorial and not (editorial & damage)
    assert {"repeated-constraint", "constraint-bloat", "price-question-unanswered"} >= editorial


@pytest.mark.parametrize("section,findings", [
    # "free" is a complete answer to "what does it cost" and carries no digits at all —
    # 4 of the first 12 findings on the stored corpus were this
    ("## How much does it cost to use the platform?\n\nIt is free for buyers. There are no fees, "
     "no premium tiers and no upsells.\n", 0),
    # a contingency rate IS the price
    ("## How much does an agency charge?\n\nAgencies typically charge 25-40% of what is recovered, "
     "with no fee if nothing is collected.\n", 0),
    ("## How much does it cost per month?\n\nThe programme is $270 per month at every dose.\n", 0),
    # …and these evade the question they asked
    ("## How much does a kitchen remodel cost?\n\nCosts vary widely by scope — a cosmetic refresh "
     "runs lower than a full gut renovation.\n", 1),
])
def test_what_counts_as_answering_a_price_question(section, findings):
    assert len(detect_price_question_unanswered("# T\n\n" + section)) == findings


# ── 6. a rule with no number still goes out of date ──────────────────────────────────────────────
import time  # noqa: E402

from generators.blog_gen import _source_pub_date  # noqa: E402

NOW = time.strptime("2026-09-22", "%Y-%m-%d")
OLD_BLOCK = [{"label": "official · Obesity Playbook (April 2025) – Endocrine Society",
              "url": "https://www.endocrine.org/-/media/obesity-playbook-april-2025_updated2.pdf",
              "text": "official · Obesity Playbook (April 2025) – Endocrine Society Advocacy Resource"}]
RULE_BODY = ("# T\n\n## Coverage\n\nMedicare Part D does not cover anti-obesity medications, "
             "including semaglutide prescribed for weight loss [S1].\n")


def test_a_rule_cited_to_an_old_source_is_flagged(gen):
    """The claim appeared five times in the reported article, cited only to a document whose own
    title says April 2025 — seventeen months before publication, and false by then. Everything
    needed was already here: the current-state phrase matched on "cover", the date parser could read
    the source, the window is twelve months — and the sentence was thrown out one conjunct later for
    having no NUMBER in it. A rule is not less perishable than a price."""
    gen._claim_pages = {}
    note = gen._stale_source_check(RULE_BODY, OLD_BLOCK, now=NOW)
    assert note and "17 months" in note


def test_the_same_rule_cited_to_a_current_source_is_not(gen):
    gen._claim_pages = {}
    fresh = [dict(OLD_BLOCK[0], url="https://www.endocrine.org/playbook-august-2026.pdf",
                  text="official · Obesity Playbook (August 2026) – Endocrine Society")]
    assert gen._stale_source_check(RULE_BODY, fresh, now=NOW) == ""


def test_ordinary_qualitative_prose_is_still_out_of_scope(gen):
    """Narrow on purpose: the sentence must describe the PRESENT and state a RULE. Measured across
    217 stored articles, the new arm fires 4 times in 3 articles — three of them the reported
    claim."""
    gen._claim_pages = {}
    plain = "# T\n\n## About\n\nThe programme is delivered entirely online by its team [S1].\n"
    assert gen._stale_source_check(plain, OLD_BLOCK, now=NOW) == ""


@pytest.mark.parametrize("block,want", [
    # the date is in the URL slug as a month name…
    ({"url": "https://x/obesity-playbook-april-2025_updated2.pdf", "text": ""}, (2025, 4)),
    # …or in the title, with no day
    ({"url": "https://x/doc.pdf", "text": "Obesity Playbook (April 2025) – Endocrine Society"}, (2025, 4)),
    # the forms that already worked must keep working
    ({"url": "https://x/2024/12/post", "text": ""}, (2024, 12)),
    ({"url": "https://x/post", "text": "Published December 20, 2024"}, (2024, 12)),
    # …including the deliberate "a bare year looks as NEW as possible" rule
    ({"url": "https://x/report-2025.pdf", "text": ""}, (2025, 12)),
    ({"url": "https://x/p", "text": "no date here at all"}, None),
])
def test_a_month_and_year_with_no_day_is_a_date(block, want):
    """This is what actually hid the defect. "April 2025" in the title AND the URL slug, and neither
    form was read, so the bare-year fallback resolved it to December — making a seventeen-month-old
    document look nine months old and slip under a twelve-month window. Not a relaxation of the
    "look as new as possible" rule: April IS the date, and reading it as December was wrong."""
    assert _source_pub_date(block, NOW) == want


# ── 7. the total, with the split named ───────────────────────────────────────────────────────────
@pytest.mark.parametrize("name,rows,want", [
    ("Ro", RO_ROWS, "$423-$498 (per month, Wegovy pen $349 + membership $74-$149)"),
    ("Hims", HIMS_ROWS, "From $348 (per month, Wegovy pen $199 + membership $149)"),
])
def test_the_total_names_its_parts(gen, name, rows, want):
    """The operator's choice: comparable with an all-in rival AND the reader sees where the money
    goes. No new field and no extra typing — they already entered the parts as their own rows, so
    the components of the total are derived from its siblings."""
    entry, _ = gen._price_row_for(rows, name, _product_tokens("semaglutide cost per month"),
                                  subject="PeterMD")
    assert _format_price_value(entry) == want


def test_a_total_with_no_parts_still_describes_its_shape(gen):
    """Naming the split depends on the operator having entered the parts. Where they have not, the
    cell must still say a second charge exists."""
    rows = [{"product": "programme + product", "kind": "from", "value": "$300",
             "basis": "per month", "composition": "plus"},
            {"product": "something else", "kind": "exact", "value": "$10"}]
    entry, _ = gen._price_row_for(rows, "Z", _product_tokens("anything"))
    assert _format_price_value(entry) == "From $300 (per month, membership plus the product, billed separately)"


def test_an_all_in_price_is_unchanged(gen):
    """The publisher's own cell must read exactly as it did — this changes how a TOTAL is written,
    not how every price is."""
    rows = [{"product": "GLP-1", "kind": "exact", "value": "$270", "basis": "per month",
             "composition": "all-in"}]
    entry, _ = gen._price_row_for(rows, "PeterMD", _product_tokens("semaglutide cost"))
    assert _format_price_value(entry) == "$270 (per month, everything included)"
