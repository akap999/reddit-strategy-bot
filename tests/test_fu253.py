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
@pytest.mark.parametrize("name,rows,want", [
    ("Ro", RO_ROWS, "$423-$498 (per month, membership plus the product, billed separately)"),
    ("Hims", HIMS_ROWS, "From $348 (per month, membership plus the product, billed separately)"),
])
def test_the_total_wins_over_the_part_price(gen, name, rows, want):
    """A part-price ranked against a rival's all-in price is not a comparison. The operator said
    which row is the total; the picker now reads it."""
    entry, _amb = gen._price_row_for(rows, name, _product_tokens("semaglutide cost per month"),
                                     subject="PeterMD")
    assert _format_price_value(entry) == want


def test_the_membership_alone_is_never_the_headline_price(gen):
    """A programme fee on its own is the least useful of the three rows — it is neither the product
    nor the total — so it must never be the one that reaches the comparison cell."""
    for rows in (RO_ROWS, HIMS_ROWS):
        entry, _ = gen._price_row_for(rows, "X", _product_tokens("anything at all"))
        assert "membership plus the product" in _format_price_value(entry)


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
