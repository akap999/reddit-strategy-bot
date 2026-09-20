"""FU225 — the condition words are checked against the page, not just the quote.

Two gaps the quote gate cannot see, because the quote IS on the page and only what we say ABOUT
the figure is wrong:

  1. A SCOPE WORD dropped. `kind` was read off the model's own answer only, so a page that says
     "plans start at $169/mo" and an answer that says "$169/mo" produced a cell claiming a fixed
     price where the source states a floor. The audit's rule 7.4: check the claim's scope words
     against the page.
  2. A CONDITION the page never states. The basis is meant to be built from the page's own words;
     a number in it that appears nowhere on the page was not read off it.

Both are deliberately conservative. The scope check only ever ADDS the qualifier the source has —
it can never drop a fact. The basis check judges NUMBERS only and drops the offending clause, never
the verified figure, because checking words against a page is where false rejections live: a
condition is routinely stated a sentence away, or as "/mo" where the basis says "per month".

$0 and network-free.
"""
import generators.research as R
from generators.blog_gen import _format_price_value
from tests.test_fu221_research import ResearchStub


# ───────────────────────────────────────────── the scope word the page attaches
def test_a_floor_price_is_recognised_as_a_floor():
    assert R.scope_on_page("$149", "Plans starting at $149 a month.") == "from"
    assert R.scope_on_page("$299", "Membership from $299/mo") == "from"
    assert R.scope_on_page("$169/mo", "cash-pay plans start at $169/mo and go up") == "from"
    assert R.scope_on_page("$449", "Pay up to $449 per month.") == "upto"
    # a fixed price stays fixed
    assert R.scope_on_page("$149", "Get the plan for $149 a month.") == ""
    assert R.scope_on_page("$39", "Pro $39 / month") == ""
    assert R.scope_on_page("", "starting at $149") == "" and R.scope_on_page("$1", "") == ""


def test_the_scope_word_must_be_in_front_of_THIS_figure():
    """"from" is a common word. Anchoring it to the characters directly before the figure is what
    stops "Save $50 from the list price of $499" reading as a floor — there, "of" precedes it."""
    assert R.scope_on_page("$499", "Save $50 from the list price of $499.") == ""
    # a different figure carrying the scope word does not lend it to ours
    assert R.scope_on_page("$149", "Plans from $99. The Pro tier is $149.") == ""
    # but any occurrence of OUR figure carrying it is enough
    assert R.scope_on_page("$149", "The plan is $149. New members: starting at $149.") == "from"


def test_the_pages_scope_word_reaches_the_cell():
    fact = {"answer": "$169/mo", "url": "https://f.com/p", "quote": "q", "basis": "cash pay",
            "scope": "from"}
    assert R.price_entry(fact)["kind"] == "from"
    assert _format_price_value(R.price_entry(fact)) == "From $169 (cash pay)"
    # an answer that already said it is unchanged, and no scope means no change
    assert R.price_entry({"answer": "starting at $169", "url": "u", "quote": "q"})["kind"] == "from"
    assert R.price_entry({"answer": "$169", "url": "u", "quote": "q"})["kind"] == "exact"
    assert R.price_entry({"answer": "$169", "url": "u", "quote": "q", "scope": "upto"})["kind"] == "upto"


def test_end_to_end_a_floor_on_the_page_is_not_shipped_as_a_fixed_price(monkeypatch):
    page = "Compounded semaglutide cash-pay plans start at $169/mo on a 12-month plan."
    url = "https://f.com/program"
    stub = ResearchStub(pages_handler=lambda b, d, n: [url],
                        facts_handler=lambda b, n, p: [
                            {"need": 1, "answer": "$169/mo", "url": url, "basis": "cash pay",
                             "quote": "cash-pay plans start at $169/mo", "product": ""}])
    monkeypatch.setattr(R, "_fetch_page", lambda u, retries=0, ignore_wall=False:
                        (f"<html>{page}</html>", "ok"))
    res = R.research_brand(stub, "Found", ["f.com"],
                           ["Price — the regular price with its unit"], rounds=1, log=lambda m: None)
    assert res["facts"][0]["scope"] == "from"
    assert _format_price_value(R.price_ladder(res["facts"])) == "From $169 (cash pay)"


# ───────────────────────────────────────────── a condition the page never states
def test_a_clause_split_never_breaks_a_thousands_separator():
    """Splitting the basis on every comma turned "$1,094 for 120mg" into "$1" and "094 for 120mg",
    and then flagged a real figure as an unsupported condition."""
    assert R.split_clauses("$1,094 for 120mg") == ["$1,094 for 120mg"]
    assert R.split_clauses("per month, 12-month plan, cash pay") == \
        ["per month", "12-month plan", "cash pay"]
    assert R.split_clauses("") == [] and R.split_clauses(None) == []


def test_a_number_that_is_nowhere_on_the_page_is_dropped_from_the_basis():
    assert R.unsupported_basis_clauses("per month, 12-month plan", "Billed per month.") \
        == ["12-month plan"]
    assert R.unsupported_basis_clauses("per month, 12-month plan", "A 12-month plan, per month.") == []
    assert R.unsupported_basis_clauses("3-pack, 9 oz", "Sold as a 3-pack.") == ["9 oz"]
    # a comma inside a figure is not a clause boundary, so a real price is never flagged
    assert R.unsupported_basis_clauses("$1,094 for 120mg", "Tirzepatide - 120mg $ 1,094.00") == []


def test_words_only_conditions_are_never_judged_here():
    """The honest limit, written down so it is not mistaken for coverage: checking WORDS against a
    page is where false rejections live. "billed quarterly" on a page that says "billed monthly" is
    a real defect this does NOT catch — it carries no number."""
    assert R.unsupported_basis_clauses("cash pay only, membership required", "anything") == []
    assert R.unsupported_basis_clauses("billed quarterly", "Billed monthly.") == []
    assert R.unsupported_basis_clauses("", "x") == [] and R.unsupported_basis_clauses(None, "x") == []


def test_the_figure_survives_when_only_its_condition_was_invented(monkeypatch):
    """A rejection must cost as little as possible: the figure passed the quote gate, so only the
    clause that was not on the page comes off."""
    page, url = "Our plan is $149 per month.", "https://b.com/p"
    stub = ResearchStub(pages_handler=lambda b, d, n: [url],
                        facts_handler=lambda b, n, p: [
                            {"need": 1, "answer": "$149 per month", "url": url, "product": "",
                             "quote": "Our plan is $149 per month", "basis": "per month, 12-month plan"}])
    monkeypatch.setattr(R, "_fetch_page", lambda u, retries=0, ignore_wall=False:
                        (f"<html>{page}</html>", "ok"))
    res = R.research_brand(stub, "B", ["b.com"], ["Price — the regular price"], rounds=1,
                           log=lambda m: None)
    assert len(res["facts"]) == 1, "the verified figure is kept"
    assert res["facts"][0]["basis"] == "per month", "only the invented condition came off"
    assert _format_price_value(R.price_ladder(res["facts"])) == "$149 (per month)"
