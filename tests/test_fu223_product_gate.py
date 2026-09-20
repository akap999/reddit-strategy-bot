"""FU223 — a fact about one product can no longer answer a question about another.

The blog audit's biggest failure cluster is one bug wearing four hats: a brand with two products,
facts crossed between them. Its worked case is a telehealth brand selling a tirzepatide plan AND a
separate subscription; drafts repeatedly gave the tirzepatide plan the subscription's price
structure, its perks, and its eight-state exclusion list — telling readers in two of those states
they could not buy a product they can buy.

The quote gate cannot catch this. The page IS the brand's own, and the quote IS on it word for word.
Only the PRODUCT is wrong. So the research step needs its own predicate, and it has to be narrow:
a rejection throws away a real, sourced fact, so it fires only when the evidence points at a
DIFFERENT compared product AND not at this need's product.

$0 and network-free — the fetcher is monkeypatched and every model call is a stub.
"""
import generators.research as R
from tests.test_fu221_research import ResearchStub


PRODS = ["tirzepatide", "semaglutide"]


def _need(col="Pricing Structure", prods=PRODS):
    return R.build_needs([col], prods, include_pricing=True)[0]


# ───────────────────────────────────────────── reading the need's own product back
def test_a_need_carries_the_product_build_needs_wrote_into_it():
    need = _need()
    assert "tirzepatide" in need
    assert R.need_product(need, PRODS) == "tirzepatide"
    # the SECOND product's need resolves to the second product
    assert R.need_product(R.build_needs(["Pricing Structure"], PRODS, include_pricing=True)[1],
                          PRODS) == "semaglutide"
    # a dimension that names no product yields none, so the gate stays inert for it
    assert R.need_product("Delivery (for weight-loss providers)", PRODS) == ""
    # the longest match wins, so a dose-specific product is not mistaken for its plain form
    prods = ["semaglutide", "semaglutide 2.5 mg"]
    assert R.need_product("Price of semaglutide 2.5 mg — the regular price", prods) \
        == "semaglutide 2.5 mg"


# ───────────────────────────────────────────── the gate itself
def test_the_gate_fires_only_on_a_confident_cross_to_another_compared_product():
    mine = "tirzepatide"
    fires = [
        ("the extractor named the other product", {"product": "semaglutide subscription",
                                                   "url": "https://p.com/pricing"}),
        ("no product named, but the page IS the other product's",
         {"product": "", "url": "https://p.com/product/semaglutide/"}),
    ]
    for label, fact in fires:
        assert R.product_conflict(mine, fact, PRODS) == "semaglutide", label

    holds = [
        ("it names OUR product", {"product": "tirzepatide plan", "url": "https://p.com/pricing"}),
        ("nothing to go on — never reject on absence",
         {"product": "", "url": "https://p.com/pricing/"}),
        ("a brand name for the same thing, outside the compared set",
         {"product": "Zepbound", "url": "https://p.com/zepbound/"}),
        ("a page that covers both — ours is present",
         {"product": "tirzepatide and semaglutide", "url": "https://p.com/compare/"}),
    ]
    for label, fact in holds:
        assert R.product_conflict(mine, fact, PRODS) == "", label


def test_the_gate_is_inert_when_there_is_nothing_to_confuse_it_with():
    f = {"product": "semaglutide", "url": "https://p.com/semaglutide/"}
    assert R.product_conflict("tirzepatide", f, ["tirzepatide"]) == ""   # one compared product
    assert R.product_conflict("tirzepatide", f, []) == ""                # no product list
    assert R.product_conflict("", f, PRODS) == ""                        # need names no product
    assert R.product_conflict("tirzepatide", {}, PRODS) == ""            # no fact evidence at all


# ───────────────────────────────────────────── end to end, the audit's own failure
_PAGE = ("Our tirzepatide plan is $149 for the first month, then $249 a month billed quarterly. "
         "Our separate GLP-1 subscription is $270 a month, the same price at all doses, medicine "
         "included, cancel anytime.")


def _run(monkeypatch, facts, needs=None, prods=PRODS):
    url = "https://p.com/pricing"
    stub = ResearchStub(pages_handler=lambda b, d, n: [url],
                        facts_handler=lambda b, n, p: [dict(x, url=url) for x in facts])
    monkeypatch.setattr(R, "_fetch_page",
                        lambda u, retries=0, ignore_wall=False: (f"<html>{_PAGE}</html>", "ok"))
    return R.research_brand(stub, "PeterMD", ["p.com"], needs or [_need()], products=prods,
                            rounds=1, log=lambda m: None)


def test_another_compared_products_price_cannot_answer_this_products_question(monkeypatch):
    """Audit failure #1, in the shape the code gate can prove: the answer is a real quote from the
    brand's own page, but it is about the OTHER product this article compares."""
    res = _run(monkeypatch, [
        {"need": 1, "answer": "$270 a month, the same price at all doses",
         "quote": "separate GLP-1 subscription is $270 a month, the same price at all doses",
         "product": "semaglutide subscription", "basis": "per month"}])
    assert res["facts"] == [], "a real quote off the right site, about the WRONG product"
    assert res["unconfirmed"] == [_need()], "the need stays open for the next round"


def test_a_brands_own_second_product_is_refused_at_the_extraction_step():
    """The audit's actual case names the other offering "GLP-1 subscription" — a product the
    ARTICLE does not compare, so no token gate can recognise it. That discrimination needs meaning,
    not string overlap, so it is asked of the extractor, which is looking at both on one page."""
    from generators.base import ClaudeClient
    from tests.test_fu222_price_ladder import _FakeClient
    sink = []
    c = ClaudeClient("test-key-not-used")
    c.client = _FakeClient(sink, '{"facts": []}')
    c.extract_facts("PeterMD", [_need()], {"https://p.com/pricing": _PAGE})
    prompt = sink[0]["messages"][0]["content"]
    assert "answer ONLY from facts about" in prompt
    assert "a separate subscription, another tier, another size" in prompt
    assert "rather than crossing them" in prompt
    # and it must say WHY, so the model does not treat it as a style note
    assert "where it is available" in prompt


def test_the_right_products_price_still_gets_through(monkeypatch):
    res = _run(monkeypatch, [
        {"need": 1, "answer": "$149 for the first month",
         "quote": "tirzepatide plan is $149 for the first month",
         "product": "tirzepatide plan", "basis": "first month"}])
    assert [f["answer"] for f in res["facts"]] == ["$149 for the first month"]
    assert res["unconfirmed"] == []


def test_the_wrong_product_is_dropped_while_the_right_one_survives_the_same_page(monkeypatch):
    """The realistic shape: one page states both, and the extractor offers both for one need."""
    res = _run(monkeypatch, [
        {"need": 1, "answer": "$149 for the first month",
         "quote": "tirzepatide plan is $149 for the first month",
         "product": "tirzepatide plan", "basis": "first month"},
        {"need": 1, "answer": "$270 a month",
         "quote": "separate GLP-1 subscription is $270 a month",
         "product": "semaglutide subscription", "basis": "per month"}])
    assert [f["answer"] for f in res["facts"]] == ["$149 for the first month"]
    assert R.price_ladder(res["facts"])["value"] == "$149"


def test_a_fact_that_names_no_product_is_still_kept(monkeypatch):
    """The narrowness that matters: most pages do not repeat the product name in the sentence that
    carries the price. Rejecting on absence would throw away correct, sourced facts."""
    res = _run(monkeypatch, [
        {"need": 1, "answer": "$149 for the first month",
         "quote": "tirzepatide plan is $149 for the first month", "product": "", "basis": ""}])
    assert [f["answer"] for f in res["facts"]] == ["$149 for the first month"]


# ───────────────────────────────────────────── genericity
def test_the_gate_reads_the_articles_own_products_and_knows_no_vertical(monkeypatch):
    """Two SaaS plans and two retail sizes cross exactly the same way a telehealth plan does."""
    saas = ["the Pro plan", "the Business plan"]
    need = R.build_needs(["Pricing"], saas, include_pricing=True)[0]
    assert R.need_product(need, saas) == "the Pro plan"
    assert R.product_conflict("the Pro plan", {"product": "Business plan", "url": ""}, saas) \
        == "the Business plan"
    assert R.product_conflict("the Pro plan", {"product": "Pro", "url": ""}, saas) == ""

    retail = ["the 5.4 oz bottle", "the 8.1 oz bottle"]
    # sizes ARE the difference, so they survive tokenising; "oz bottle" is shared and says nothing
    assert R._discriminators(retail) == {"the 5.4 oz bottle": {"5", "4"},
                                         "the 8.1 oz bottle": {"8", "1"}}
    assert R.product_conflict("the 5.4 oz bottle", {"product": "8.1 oz bottle", "url": ""},
                              retail) == "the 8.1 oz bottle"
    assert R.product_conflict("the 5.4 oz bottle", {"product": "5.4 oz bottle", "url": ""},
                              retail) == ""


def test_a_noisy_url_cannot_reject_a_fact_on_a_stray_digit():
    """A URL carries dates and ids, so a one-character size token would collide constantly. Only a
    distinctive WORD in a URL counts; the product field is trusted at any length."""
    retail = ["the 5.4 oz bottle", "the 8.1 oz bottle"]
    assert R.product_conflict("the 5.4 oz bottle",
                              {"product": "", "url": "https://s.com/2024/08/1-thing"}, retail) == ""
    # a distinctive word in the URL still counts
    assert R.product_conflict("tirzepatide", {"product": "", "url": "https://p.com/semaglutide/"},
                              ["tirzepatide", "semaglutide"]) == "semaglutide"


# ───────────────────────────────────────────── availability is a PRODUCT fact, not a brand fact
def test_availability_is_asked_per_product_so_one_products_limits_cannot_travel():
    """Audit failure #2, the one their catalogue treats as a blocker: a tirzepatide plan listed as
    unavailable in eight states when its own page says two, because the OTHER product's exclusion
    list was carried across. It told readers in two of those states they could not buy something
    they can. Splitting the question is only safe because a fact about the other product is now
    refused, which is what the gate above does."""
    needs = R.build_needs(["Pricing Structure"], PRODS, include_pricing=True, geo="the US")
    avail = [n for n in needs if R.is_availability_need(n)]
    assert len(avail) == 2, "one availability question per compared product"
    assert [R.need_product(n, PRODS) for n in avail] == ["tirzepatide", "semaglutide"]
    # each one asks for the limits that apply to ITS product, not to the brand
    assert all("eligibility or coverage limit that applies to it" in n for n in avail)
    # and the gate is live on them, because the need names its product
    assert R.product_conflict("tirzepatide",
                              {"product": "semaglutide", "url": ""}, PRODS) == "semaglutide"


def test_a_brand_with_one_product_still_asks_once():
    one = R.build_needs(["Pricing"], ["tirzepatide"], include_pricing=True, geo="the US")
    assert len([n for n in one if R.is_availability_need(n)]) == 1
    none = R.build_needs(["Pricing"], [], include_pricing=True, geo="the US")
    assert len([n for n in none if R.is_availability_need(n)]) == 1
    assert R.build_needs(["Pricing"], PRODS, include_pricing=True) == \
        R.build_needs(["Pricing"], PRODS, include_pricing=True, geo=""), "no geo, no availability"


def test_an_availability_question_is_never_routed_into_the_price_search():
    """A product literally called "the Pro plan" puts the word "plan" into its availability need,
    which PRICE_NEED_RE matches. Left alone it would be searched with buyer PRICE queries and run
    through the sale-price refusal."""
    needs = R.build_needs(["Pricing"], ["the Pro plan"], include_pricing=True, geo="the US")
    avail = [n for n in needs if R.is_availability_need(n)]
    assert len(avail) == 1
    assert not R.is_price_need(avail[0]), "matches PRICE_NEED_RE, but it is an availability question"
    assert R.is_price_need([n for n in needs if "regular price" in n][0]), "the real price need still is"


def test_splitting_availability_does_not_evict_a_comparison_column():
    """Availability is added last, so making it per-product could silently push a column out of the
    needs list. There is room for both."""
    dims = ["Pricing Structure", "Delivery", "Support", "Integrations"]
    needs = R.build_needs(dims, PRODS, include_pricing=True, geo="the US")
    for d in dims[1:]:
        assert any(n.startswith(d) for n in needs), f"{d} was evicted"
    assert len([n for n in needs if R.is_price_need(n)]) == 2
    assert len([n for n in needs if R.is_availability_need(n)]) == 2


# ───────────────────────────────────────────── source-class exclusion
def test_a_recruitment_or_press_page_is_never_read_as_evidence():
    """Audit failure #13: a consumer claim cited to an affiliate-registration page. Those pages are
    written for recruiters and reporters; the fact a reader needs is on the product or policy page,
    and a source list handed to a client that cites "become an affiliate" reads as unserious."""
    for url, cls in [("https://b.com/affiliates/", "affiliates"),
                     ("https://b.com/affiliate-program", "affiliate-program"),
                     ("https://b.com/press", "press"),
                     ("https://b.com/press-releases/2024/launch", "press-releases"),
                     ("https://b.com/newsroom", "newsroom"),
                     ("https://b.com/media-kit", "media-kit"),
                     ("https://b.com/investors", "investors"),
                     ("https://b.com/investor-relations/", "investor-relations"),
                     ("https://b.com/careers", "careers"),
                     ("https://b.com/jobs/engineer", "jobs"),
                     ("https://b.com/become-a-partner", "become-a-partner"),
                     ("https://b.com/reseller/apply", "reseller")]:
        assert R.page_class_excluded(url) == cls, url


def test_the_pages_a_claim_should_actually_rest_on_are_untouched():
    for url in ["https://b.com/product/tirzepatide/", "https://b.com/pricing", "https://b.com/plans",
                "https://b.com/terms", "https://b.com/shipping-policy", "https://b.com/support/faq",
                "https://b.com/about", "https://b.com/", "https://b.com/weight-loss/pricing/"]:
        assert R.page_class_excluded(url) == "", url


def test_whole_segments_only_so_real_product_pages_are_not_swept_up():
    """The reason this matches whole path segments: a prefix match takes real pages with it. Two of
    these are live brands of ours."""
    for url in ["https://careerstep.com/careers-in-medical-coding",   # an education brand's content
                "https://b.com/pressure-washers",                      # "press" inside a product word
                "https://b.com/press-fit-bearings",
                "https://b.com/partners",                              # an integrations listing, not signup
                "https://b.com/jobsite-tools",
                "https://b.com/investor-guide-to-flooring"]:
        assert R.page_class_excluded(url) == "", url


def test_an_excluded_page_is_skipped_before_it_is_ever_fetched(monkeypatch):
    """It must cost nothing: the page is dropped at selection, so no fetch and no tokens are spent
    on it, and the need is answered from the page that should have carried it all along."""
    reads, url = [], "https://b.com/product/x"
    stub = ResearchStub(
        pages_handler=lambda b, d, n: ["https://b.com/affiliate-program", url],
        facts_handler=lambda b, n, p: [{"need": 1, "answer": "$149 for the first month", "url": url,
                                        "quote": "tirzepatide plan is $149 for the first month",
                                        "product": "tirzepatide plan", "basis": "first month"}])
    monkeypatch.setattr(R, "_fetch_page", lambda u, retries=0, ignore_wall=False:
                        (reads.append(u), (f"<html>{_PAGE}</html>", "ok"))[1])
    res = R.research_brand(stub, "PeterMD", ["b.com"], [_need()], products=PRODS, rounds=1,
                           log=lambda m: None)
    assert reads == [url], f"the affiliate page was fetched anyway: {reads}"
    assert [f["answer"] for f in res["facts"]] == ["$149 for the first month"]


def test_find_pages_is_told_not_to_return_them_either():
    from generators.base import ClaudeClient
    from tests.test_fu222_price_ladder import _FakeClient
    sink = []
    c = ClaudeClient("test-key-not-used")
    c.client = _FakeClient(sink, '{"pages": []}')
    c.find_pages("PeterMD", ["p.com"], ["Pricing — the regular price"])
    prompt = sink[0]["messages"][0]["content"]
    assert "NEVER return an affiliate, partner-signup, press, newsroom, investor or careers page" in prompt
    assert "the fact a reader needs is on the product or policy page" in prompt
