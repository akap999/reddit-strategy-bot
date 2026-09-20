"""FU222 — the price question gets its OWN pinned search, and the whole price LADDER survives.

Two separate faults, both proven against a real generation (blog #182, Ro):

  A. FINDING. Every column's question went into ONE page-finding call sharing four page slots, so
     the price need competed with "Men's Health Integration" and "Delivery" and lost. It was also
     phrased as a comparison COLUMN HEADING ("Pricing Structure for tirzepatide — the regular price
     with its unit, term or pack and any condition"), which a search engine does not match to a page
     titled "Pricing". Ro's `/weight-loss/pricing/` was never read; a product page that happened to
     show one figure answered the need instead.

  B. KEEPING. The extraction step was already told to keep every condition the page attaches, and
     every figure still has to carry its own on-page quote — but `price_entry` took `figs[0]` and
     `research_brand` took the FIRST fact per need, so a page stating "$39 for the first month, then
     $149 a month" reached the cell as "$39".

The verbatim gate is UNCHANGED by this round: more rungs reach the cell, and each one still stands
or falls on a quote that is on the page. $0 and network-free — the fetcher is monkeypatched and
every model call is a stub.
"""
import generators.research as R
from generators.base import ClaudeClient
from generators.blog_gen import BlogGenerator, _format_price_value
from tests.stubs import StubClaude
from tests.test_fu221_research import ResearchStub


# ───────────────────────────────────────────── A. the price question gets its own search
def test_price_queries_are_what_a_buyer_types_not_a_column_heading():
    qs = R.price_queries("Ro", ["tirzepatide", "semaglutide"])
    assert qs == ["Ro tirzepatide price", "Ro semaglutide price", "Ro pricing"]
    # no product named -> still a real search, never an empty one
    assert R.price_queries("Hims", []) == ["Hims pricing"]
    assert R.price_queries("", ["x"]) == []
    # the cap holds, and a brand whose product repeats does not get the same search twice
    assert len(R.price_queries("Ro", ["a", "b", "c", "d"])) == 3
    assert R.price_queries("Ro", ["Pricing"]) == ["Ro Pricing price", "Ro pricing"]


def test_the_price_need_is_searched_on_its_own_and_not_against_the_other_columns(monkeypatch):
    """The reported case: 4 needs, one call, 4 page slots — the price need got ~one page. Now the
    price need has its own call with its own slots, and the other columns keep theirs."""
    needs = ["Tirzepatide Offering (for tirzepatide providers)",
             "Pricing Structure for tirzepatide — the regular price with its unit, term or pack",
             "Men's Health Integration (for tirzepatide providers)",
             "Delivery (for tirzepatide providers)"]
    stub = ResearchStub(pages_handler=lambda b, d, n: ["https://ro.co/weight-loss/pricing/"],
                        facts_handler=lambda b, n, p: [])
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0, ignore_wall=False: ("", "blocked"))
    R.research_brand(stub, "Ro", ["ro.co"], needs, products=["tirzepatide"], rounds=1,
                     log=lambda m: None)

    price_calls = [c for c in stub.find_calls if any(R.is_price_need(n) for n in c["needs"])]
    other_calls = [c for c in stub.find_calls if not any(R.is_price_need(n) for n in c["needs"])]
    assert len(price_calls) == 1 and len(other_calls) == 1, "one call each, not one call for all four"
    assert len(price_calls[0]["needs"]) == 1, "the price need does not share its search"
    assert price_calls[0]["queries"] == ["Ro tirzepatide price", "Ro pricing"]
    assert other_calls[0]["queries"] == [], "only the price search is re-phrased as a buyer query"
    assert set(other_calls[0]["needs"]) == {n for n in needs if not R.is_price_need(n)}
    assert all(c["domains"] == ["ro.co"] for c in stub.find_calls), "still pinned first-party"


def test_pricing_off_means_no_price_search_at_all(monkeypatch):
    stub = ResearchStub(pages_handler=lambda b, d, n: ["https://ro.co/x"], facts_handler=lambda b, n, p: [])
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0, ignore_wall=False: ("", "blocked"))
    R.research_brand(stub, "Ro", ["ro.co"], ["Delivery"], products=["tirzepatide"], rounds=1,
                     log=lambda m: None)
    assert len(stub.find_calls) == 1 and stub.find_calls[0]["queries"] == []


class _FakeUsage:
    input_tokens, output_tokens, server_tool_use = 10, 5, None


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeMsg:
    def __init__(self, text):
        self.content, self.usage = [_FakeBlock(text)], _FakeUsage()


class _FakeClient:
    """Captures the real `find_pages` request without a network call."""

    def __init__(self, sink, reply):
        self.sink, self.reply = sink, reply
        self.messages = self

    def create(self, **kw):
        self.sink.append(kw)
        return _FakeMsg(self.reply)


def test_find_pages_puts_the_buyer_queries_in_the_prompt_and_keeps_the_domain_pin():
    """The real client, so the wiring is proven end to end — not just the stub's signature."""
    sink = []
    c = ClaudeClient("test-key-not-used")
    c.client = _FakeClient(sink, '{"pages": ["https://ro.co/weight-loss/pricing/"]}')
    urls = c.find_pages("Ro", ["ro.co"], ["Pricing Structure for tirzepatide — the regular price"],
                        queries=["Ro tirzepatide price", "Ro pricing"], max_pages=3)
    assert urls == ["https://ro.co/weight-loss/pricing/"]
    kw = sink[0]
    prompt = kw["messages"][0]["content"]
    assert "Ro tirzepatide price" in prompt and "Ro pricing" in prompt
    assert "what a buyer would type" in prompt
    assert kw["tools"][0]["allowed_domains"] == ["ro.co"], "the first-party pin is untouched"

    # No queries -> the prompt is what it has always been (the non-price search is unchanged)
    sink2 = []
    c2 = ClaudeClient("test-key-not-used")
    c2.client = _FakeClient(sink2, '{"pages": []}')
    c2.find_pages("Ro", ["ro.co"], ["Delivery"])
    assert "what a buyer would type" not in sink2[0]["messages"][0]["content"]


# ───────────────────────────────────────────── B. the ladder survives the verifier
_PRICING_PAGE = ("Ro Body membership is $39 for the first month, then $149 a month. "
                 "Medication is billed separately.")


def _ladder_stub(answers):
    """A stub whose extraction offers SEVERAL figures for the ONE price need, as a real pricing page
    does — each with its own quote, which is what every rung must survive on."""
    facts = [{"need": 1, "answer": a, "url": "https://ro.co/pricing", "quote": q, "basis": b}
             for a, q, b in answers]
    return ResearchStub(pages_handler=lambda b, d, n: ["https://ro.co/pricing"],
                        facts_handler=lambda b, n, p: facts)


def test_every_rung_with_its_own_quote_is_kept_not_just_the_first(monkeypatch):
    stub = _ladder_stub([("$39", "is $39 for the first month", "first month"),
                         ("$149", "then $149 a month", "per month")])
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0, ignore_wall=False: (_PRICING_PAGE, "ok"))
    res = R.research_brand(stub, "Ro", ["ro.co"], ["Price — the regular price"],
                           products=["tirzepatide"], log=lambda m: None)
    assert [f["answer"] for f in res["facts"]] == ["$39", "$149"], "the whole ladder, in page order"
    assert res["unconfirmed"] == []


def test_a_rung_whose_quote_is_not_on_the_page_is_still_dropped(monkeypatch):
    """The point of the round is MORE rungs, not a weaker gate."""
    stub = _ladder_stub([("$39", "is $39 for the first month", "first month"),
                         ("$99", "only $99 a month forever", "per month")])
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0, ignore_wall=False: (_PRICING_PAGE, "ok"))
    res = R.research_brand(stub, "Ro", ["ro.co"], ["Price — the regular price"], log=lambda m: None)
    assert [f["answer"] for f in res["facts"]] == ["$39"], "the invented rung never reaches a cell"


def test_a_sale_rung_is_refused_while_the_regular_rungs_are_kept(monkeypatch):
    page = "Sale price $34.99 Regular price $39.99 for the 2-pack. Single bottle $12.99."
    stub = _ladder_stub([("$34.99", "Sale price $34.99", "2-pack"),
                         ("$39.99", "Regular price $39.99 for the 2-pack", "2-pack"),
                         ("$12.99", "Single bottle $12.99", "single")])
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0, ignore_wall=False: (page, "ok"))
    res = R.research_brand(stub, "Pigeon", ["p.com"], ["Price — the regular price"], log=lambda m: None)
    assert [f["answer"] for f in res["facts"]] == ["$39.99", "$12.99"]


def test_the_same_figure_stated_twice_is_kept_once_and_the_cap_holds(monkeypatch):
    page = "$39 for the first month. Again: $39 for the first month. Then $149 a month. Also $199 a month."
    stub = _ladder_stub([("$39", "$39 for the first month", "first month"),
                         ("$39", "$39 for the first month", "first month"),
                         ("$149", "Then $149 a month", "per month"),
                         ("$199", "Also $199 a month", "per month")])
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0, ignore_wall=False: (page, "ok"))
    res = R.research_brand(stub, "Ro", ["ro.co"], ["Price — the regular price"], log=lambda m: None)
    assert [f["answer"] for f in res["facts"]] == ["$39", "$149", "$199"]
    assert len(res["facts"]) <= R._MAX_FACTS_PER_NEED


# ───────────────────────────────────────────── the ledger entry and the printed cell
def _facts(*rows):
    return [{"answer": a, "url": "https://ro.co/pricing", "quote": f"q {a}", "basis": b} for a, b in rows]


def test_price_ladder_keeps_the_old_entry_shape_so_nothing_downstream_changes():
    e = R.price_ladder(_facts(("$39", "first month"), ("$149", "per month")), checked_at="T")
    # every existing consumer reads value/basis/url/source — they must still read the first figure
    assert e["value"] == "$39" and e["basis"] == "first month"
    assert e["url"] == "https://ro.co/pricing" and e["source"] == "own" and e["checked_at"] == "T"
    assert [t["value"] for t in e["tiers"]] == ["$39", "$149"]
    assert [t["basis"] for t in e["tiers"]] == ["first month", "per month"]


def test_one_figure_still_produces_a_plain_entry_with_no_tiers():
    e = R.price_ladder(_facts(("$299", "per month")))
    assert e["value"] == "$299" and "tiers" not in e
    assert R.price_ladder([]) is None and R.price_ladder(None) is None
    # a single fact (not a list) is accepted, as the old call site passed
    assert R.price_ladder(_facts(("$299", "per month"))[0])["value"] == "$299"


def test_a_ladder_prints_every_rung_and_a_single_price_prints_exactly_as_before():
    ladder = R.price_ladder(_facts(("$39", "first month"), ("$149", "per month, medication separate")))
    assert _format_price_value(ladder) == "$39 (first month); $149 (per month, medication separate)"
    # FU213/FU214 rendering is untouched for every entry that has no ladder
    assert _format_price_value({"value": "$24.99", "kind": "from", "basis": "3-pack",
                                "per_unit": "$8.33 each"}) == "From $24.99 (3-pack, $8.33 each)"
    assert _format_price_value({"value": "$19.99", "value_max": "$29.99", "kind": "range"}) \
        == "$19.99-$29.99"
    assert _format_price_value({"value": "$5", "kind": "none"}) == ""
    assert _format_price_value({}) == "" and _format_price_value(None) == ""


def test_a_from_rung_keeps_its_from_wording_inside_the_ladder():
    e = R.price_ladder(_facts(("Starts at $199/mo", "per month"), ("$449", "maintenance dose")))
    assert _format_price_value(e) == "From $199 (per month); $449 (maintenance dose)"


def test_the_cell_the_evidence_block_and_the_log_all_carry_the_whole_ladder():
    """The reported symptom: the table cell showed one figure although the page stated the ladder."""
    gen = BlogGenerator(StubClaude(), None)
    e = R.price_ladder(_facts(("$299", "first month"), ("$449", "per month")),
                       checked_at="2026-09-20T00:00:00Z")
    shown = _format_price_value(e)
    gen._evidence_blocks = gen._price_ledger_blocks({"Ro": e})
    assert shown in gen._evidence_blocks[0]["text"]
    body = "| Brand | Starting price |\n|---|---|\n| Ro | $299 |\n"
    out, n = gen._write_price_cells(body, {"Ro": e})
    assert n == 1 and "$299 (first month); $449 (per month)" in out


# ───────────────────────────────────────────── the reported failure, end to end
_RO_PRICING = ("<html><body>Ro Body membership is $39 for the first month, then $149 a month. "
               "Medication is billed separately.</body></html>")
_RO_PRODUCT = ("<html><body>Zepbound through Ro. Get started from $299 a month for your first "
               "month supply.</body></html>")


def test_the_pricing_page_is_reached_and_its_whole_ladder_reaches_the_cell(monkeypatch):
    """Blog #182, reproduced: four column questions shared one search and four page slots, so the
    price need was answered by the PRODUCT page ($299) and `/weight-loss/pricing/` was never read.
    Now the price question has its own search, and every rung its own quote proves reaches the cell."""
    needs = ["Tirzepatide Offering (for tirzepatide providers)",
             "Pricing Structure for tirzepatide — the regular price with its unit, term or pack",
             "Delivery (for tirzepatide providers)"]
    pricing, product = "https://ro.co/weight-loss/pricing/", "https://ro.co/weight-loss/zepbound/"

    def pages_for(brand, domains, n):
        # the price search finds the PRICING page; the other columns find the product page
        return [pricing] if any(R.is_price_need(x) for x in n) else [product]

    def facts_for(brand, n, p):
        out = []
        for i, need in enumerate(n, start=1):
            if R.is_price_need(need):
                out += [{"need": i, "answer": "$39", "url": pricing, "basis": "first month",
                         "quote": "membership is $39 for the first month"},
                        {"need": i, "answer": "$149", "url": pricing, "basis": "per month",
                         "quote": "then $149 a month"}]
            else:
                out.append({"need": i, "answer": "yes", "url": product, "basis": "",
                            "quote": "Zepbound through Ro"})
        return out

    stub = ResearchStub(pages_handler=pages_for, facts_handler=facts_for)
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0, ignore_wall=False:
                        ((_RO_PRICING if url == pricing else _RO_PRODUCT), "ok"))
    res = R.research_brand(stub, "Ro", ["ro.co"], needs, products=["tirzepatide"], rounds=1,
                           log=lambda m: None)

    assert pricing in res["pages"], "the page titled Pricing is actually read now"
    prices = [f for f in res["facts"] if R.is_price_need(f["need"])]
    assert [f["answer"] for f in prices] == ["$39", "$149"]
    assert res["unconfirmed"] == []

    entry = R.price_ladder(prices, checked_at="2026-09-20T00:00:00Z")
    assert entry["url"] == pricing, "cited to the page the ladder was read from"
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = gen._price_ledger_blocks({"Ro": entry})
    body = "| Provider | Pricing Structure |\n|---|---|\n| Ro | $299 |\n"
    out, n = gen._write_price_cells(body, {"Ro": entry})
    assert n == 1 and "$39 (first month); $149 (per month)" in out
    assert "| Ro | $299 |" not in out, "the single flattened figure is gone"


def test_a_page_both_searches_find_is_read_once(monkeypatch):
    """Splitting the search must not double the fetch bill when both halves point at one page."""
    url, reads = "https://ro.co/pricing#plans", []
    stub = ResearchStub(pages_handler=lambda b, d, n: [url, url.replace("#plans", "")],
                        facts_handler=lambda b, n, p: [])
    monkeypatch.setattr(R, "_fetch_page",
                        lambda u, retries=0, ignore_wall=False: (reads.append(u), ("", "blocked"))[1])
    R.research_brand(stub, "Ro", ["ro.co"], ["Price — the regular price", "Delivery"],
                     products=["tirzepatide"], rounds=1, log=lambda m: None)
    assert len(reads) == 1, f"fetched {reads}"


def test_the_price_search_spends_its_own_small_budget(monkeypatch):
    """The split costs about one extra search per round — it does not double the search bill, and
    the non-price search keeps exactly the budget it has today."""
    seen = []
    stub = ResearchStub(pages_handler=lambda b, d, n: [], facts_handler=lambda b, n, p: [])
    real = stub.find_pages

    def spy(brand, domains, needs, context="", max_searches=3, max_pages=4, queries=None):
        seen.append({"price": any(R.is_price_need(n) for n in needs), "searches": max_searches,
                     "pages": max_pages})
        return real(brand, domains, needs, context=context, max_searches=max_searches,
                    max_pages=max_pages, queries=queries)

    stub.find_pages = spy
    R.research_brand(stub, "Ro", ["ro.co"], ["Price — the regular price", "Delivery"],
                     products=["tirzepatide"], rounds=2, log=lambda m: None)
    r1 = [c for c in seen[:2]]
    price, other = next(c for c in r1 if c["price"]), next(c for c in r1 if not c["price"])
    assert price["searches"] == 2 and price["pages"] == 3
    assert other["searches"] == 3 and other["pages"] == 4, "unchanged from before the split"
    assert [c["searches"] for c in seen[2:] if c["price"]] == [1], "round 2 spends less"


# ───────────────────────────────────────────── genericity: nothing here knows a vertical
def _vertical_case(monkeypatch, brand, column, product, dom, page, rungs):
    """Run the whole split-search + ladder path for one vertical. `rungs` are (value, basis, quote)."""
    need = f"{column} — the regular price with its unit, term or pack and any condition"
    assert R.is_price_need(need), f"{column} must read as a price column"
    url = f"https://{dom}/pricing"
    facts = [{"need": 1, "answer": v, "url": url, "quote": q, "basis": b} for v, b, q in rungs]
    stub = ResearchStub(pages_handler=lambda b, d, n: [url],
                        facts_handler=lambda b, n, p: facts)
    monkeypatch.setattr(R, "_fetch_page",
                        lambda u, retries=0, ignore_wall=False: (f"<html><body>{page}</body></html>", "ok"))
    res = R.research_brand(stub, brand, [dom], [need], products=[product], rounds=1,
                           log=lambda m: None)
    assert stub.find_calls[0]["queries"] == [f"{brand} {product} price", f"{brand} pricing"], brand
    return res


def test_the_split_search_and_the_ladder_work_the_same_for_every_vertical(monkeypatch):
    """Standing rule: a generator fix works across verticals. The price question is detected from
    the COLUMN's own wording and the ladder is built from the PAGE's own words, so a SaaS seat
    price and a trade's call-out fee behave exactly like a retail pack price or a care plan."""
    saas = _vertical_case(
        monkeypatch, "Acme HR", "Seat pricing", "the Growth plan", "acmehr.example",
        "Growth is $12 per seat a month, or $10 per seat billed annually.",
        [("$12", "per seat, billed monthly", "Growth is $12 per seat a month"),
         ("$10", "per seat, billed annually", "or $10 per seat billed annually")])
    assert _format_price_value(R.price_ladder(saas["facts"])) == \
        "$12 (billed monthly); $10 (billed annually); all per seat"

    trades = _vertical_case(
        monkeypatch, "Bright Roofing", "Cost", "a roof inspection", "brightroof.example",
        "Inspections are $149 flat, waived if you book the repair.",
        [("$149", "flat, waived if you book the repair", "Inspections are $149 flat")])
    assert _format_price_value(R.price_ladder(trades["facts"])) == \
        "$149 (flat, waived if you book the repair)"

    retail = _vertical_case(
        monkeypatch, "Pigeon", "Starting price", "the PPSU bottle", "pigeon.example",
        "Single bottle $12.99. 2-pack $23.99.",
        [("$12.99", "single", "Single bottle $12.99"), ("$23.99", "2-pack", "2-pack $23.99")])
    assert _format_price_value(R.price_ladder(retail["facts"])) == "$12.99 (single); $23.99 (2-pack)"
    assert _format_price_value(R.price_ladder(retail["facts"])).count("; all ") == 0


def test_a_percentage_priced_vertical_still_reaches_the_writer_though_not_the_written_cell(
        monkeypatch):
    """A KNOWN, pre-existing limit, pinned here so it is visible rather than surprising: the ledger's
    figure pattern (`research._FIG_RE`, unchanged by this round) matches CURRENCY only, so a rate or
    a percentage fee yields no ledger entry and `_write_price_cells` leaves that cell to the writer.
    The research half is unaffected — the rate is verified against the page and reaches the evidence
    with its quote, so the article can still state and cite it."""
    lending = _vertical_case(
        monkeypatch, "Northgate Lending", "Rate / fees", "the equipment loan", "northgate.example",
        "Equipment loans start at 6.9% APR, with a 1% origination fee.",
        [("6.9% APR", "on approved credit", "Equipment loans start at 6.9% APR"),
         ("1%", "origination fee", "with a 1% origination fee")])
    # the split search ran and the facts ARE verified on the brand's own page
    assert [f["answer"] for f in lending["facts"]] == ["6.9% APR", "1%"]
    assert all(f["verified_by"] == "quote" for f in lending["facts"])
    _blk = R.facts_to_blocks("Northgate Lending", lending["facts"])[0]["text"]
    assert "6.9% APR" in _blk and "1% origination fee" in _blk, "both rungs reach the writer, quoted"
    # ...but no currency figure means no ledger entry, today and before this round alike
    assert R.price_ladder(lending["facts"]) is None


# ───────────────────────────────────────────── the verified price stops being re-bought
def test_a_verified_price_survives_the_competitor_cache_write_instead_of_being_re_bought():
    """`_ensure_price_ledger` writes the verified price into `_cfacts[slug]["price"]`; the live
    write-back a few lines later REPLACED the whole entry, so the price was gone before the cache
    was persisted and the next generation paid to verify it again. The write-back now merges."""
    import json
    from tests.test_fu151 import (BRAND, SEED, ARTICLE, _gen, _FakeDB, _now_iso, _handler,
                                  _vendor_for_pinned)
    db = _FakeDB()
    price = {"value": "$10", "kind": "exact", "basis": "per month", "per_unit": "",
             "url": "https://compa.com/pricing", "source": "own", "quote": "Pro is $10 per month",
             "checked_at": _now_iso()}          # fresh price ...
    cf = {"compa": {"domain": "compa.com", "verified_at": "2020-01-01T00:00:00Z",
                    "blocks": [], "price": price}}      # ... beside STALE blocks, so CompA re-sources
    brand = dict(BRAND, competitor_facts=json.dumps(cf))
    gen = _gen(call_handler=_handler(["CompA", "CompB"]), search_handler=_vendor_for_pinned, db=db)
    gen._source_for_completion(brand, SEED, ARTICLE, ymyl=None)

    written = json.loads(db.updates[-1][1]["competitor_facts"])
    assert written["compa"]["price"]["value"] == "$10", "the verified price was wiped again"
    assert written["compa"]["blocks"], "the freshly sourced blocks still replace the stale ones"
    assert written["compa"]["verified_at"] > "2020", "and the entry is still re-stamped"


# ───────────────────────────────────────────── readability of a real ladder (found by the live run)
def test_a_long_basis_is_cut_at_a_clause_not_mid_word():
    long = ("per month, 7.5 mg, 10 mg, 12.5 mg, and 15 mg doses, with manufacturer offer, "
            "cash pay only, membership required, subject to eligibility")
    out = R._trim_basis(long)
    assert len(out) <= 120 and not out.endswith((",", ";", " "))
    assert out.split(", ")[-1] in long.split(", "), "the last clause is whole, not a fragment"
    assert R._trim_basis("per month") == "per month"        # short bases are untouched
    assert R._trim_basis("") == "" and R._trim_basis(None) == ""


def test_conditions_every_rung_shares_are_stated_once_not_three_times():
    """Live, Ro's three doses each repeated 'per month, cash pay only, membership required' — a
    230-character cell. The shared conditions move to the end; nothing the page said is lost."""
    facts = [{"answer": "$299/mo", "url": "https://ro.co/p", "quote": "q1",
              "basis": "per month, 2.5 mg dose, cash pay only, membership required"},
             {"answer": "$399/mo", "url": "https://ro.co/p", "quote": "q2",
              "basis": "per month, 5 mg dose, cash pay only, membership required"},
             {"answer": "$449/mo", "url": "https://ro.co/p", "quote": "q3",
              "basis": "per month, 7.5 mg to 15 mg doses, with offer, cash pay only, membership required"}]
    cell = _format_price_value(R.price_ladder(facts))
    assert cell == ("$299 (2.5 mg dose); $399 (5 mg dose); "
                    "$449 (7.5 mg to 15 mg doses, with offer); "
                    "all per month, cash pay only, membership required")
    assert len(cell) < 150, "the whole point is a cell a reader can scan"
    # every figure and every condition the page stated still appears exactly once
    # NB "5 mg dose" is a substring of "2.5 mg dose" — compare the whole rung, not the fragment
    for token in ("$299 (2.5 mg dose)", "$399 (5 mg dose)", "$449 (", "with offer",
                  "per month", "cash pay only", "membership required"):
        assert cell.count(token) == 1, token


def test_rungs_with_nothing_in_common_are_left_alone():
    none_shared = [{"answer": "$10", "url": "u", "quote": "q", "basis": "monthly"},
                   {"answer": "$99", "url": "u", "quote": "q", "basis": "lifetime"}]
    assert _format_price_value(R.price_ladder(none_shared)) == "$10 (monthly); $99 (lifetime)"
    # one shared clause is still lifted — repeating it on every rung is what made cells unreadable
    packs = [{"answer": "$39.99", "url": "u", "quote": "q", "basis": "2 Pack, 5.4 Oz"},
             {"answer": "$43.99", "url": "u", "quote": "q", "basis": "2 Pack, 8.1 Oz"}]
    assert _format_price_value(R.price_ladder(packs)) == "$39.99 (5.4 Oz); $43.99 (8.1 Oz); all 2 Pack"


def test_rungs_for_different_plans_are_named_so_the_cell_cannot_mix_tiers():
    """Live (Mubert): three rungs for three different plans printed as bare figures, so the cell
    silently mixed a consumer tier in with Pro and Business. A rung names its plan when the rungs
    disagree about which plan they are — and says nothing extra when they are all one product."""
    plans = [{"answer": "$39/mo", "url": "u", "quote": "q", "basis": "per month", "product": "Pro"},
             {"answer": "$199/mo", "url": "u", "quote": "q", "basis": "per month", "product": "Business"},
             {"answer": "$9.99/mo", "url": "u", "quote": "q", "basis": "per month", "product": "Premium"}]
    assert _format_price_value(R.price_ladder(plans)) == \
        "$39 (Pro); $199 (Business); $9.99 (Premium); all per month"
    one = [{"answer": "$299/mo", "url": "u", "quote": "q", "basis": "2.5 mg dose", "product": "tirzepatide"},
           {"answer": "$399/mo", "url": "u", "quote": "q", "basis": "5 mg dose", "product": "tirzepatide"}]
    assert _format_price_value(R.price_ladder(one)) == "$299 (2.5 mg dose); $399 (5 mg dose)"
    # a rung with no product at all never gets a half-labelled ladder
    part = [{"answer": "$39/mo", "url": "u", "quote": "q", "basis": "per month", "product": "Pro"},
            {"answer": "$199/mo", "url": "u", "quote": "q", "basis": "per month", "product": ""}]
    assert _format_price_value(R.price_ladder(part)) == "$39; $199; all per month"


def test_a_rung_with_no_basis_at_all_still_prints_its_figure():
    facts = [{"answer": "$10", "url": "u", "quote": "q", "basis": ""},
             {"answer": "$99", "url": "u", "quote": "q", "basis": "per year, billed up front"}]
    cell = _format_price_value(R.price_ladder(facts))
    assert cell == "$10; $99 (per year, billed up front)"


def test_one_figure_for_one_plan_is_one_rung_even_when_two_answers_describe_it():
    """Live (Mubert): the model returned the SAME $39 for Pro twice — once tagged 'billed annually'
    (which is the $32.49 rate, not $39) and once plain — so the cell showed $39 twice with
    contradictory conditions. The plainest basis wins; the figure appears once."""
    dup = [{"answer": "$39/mo", "url": "u", "quote": "q", "product": "Pro",
            "basis": "per month, billed annually"},
           {"answer": "$39/mo", "url": "u", "quote": "q", "product": "Pro", "basis": "per month"},
           {"answer": "$199/mo", "url": "u", "quote": "q", "product": "Business", "basis": "per month"}]
    cell = _format_price_value(R.price_ladder(dup))
    assert cell == "$39 (Pro); $199 (Business); all per month"
    assert cell.count("$39") == 1
    # order is unchanged: the plainer basis replaces the first rung in place, it does not jump
    assert cell.index("$39") < cell.index("$199")
    # the SAME figure for a DIFFERENT plan is two real rungs, not a duplicate
    two = [{"answer": "$10", "url": "u", "quote": "q", "product": "Starter", "basis": "per month"},
           {"answer": "$10", "url": "u", "quote": "q", "product": "Team", "basis": "per seat"}]
    assert _format_price_value(R.price_ladder(two)) == "$10 (Starter, per month); $10 (Team, per seat)"


def test_the_extractor_is_told_never_to_bundle_two_figures_in_one_answer():
    """The root cause of the duplicate: one `answer` carried two prices, and only its first figure
    survives into the ledger — so the second was lost and the first wore the wrong condition."""
    sink = []
    c = ClaudeClient("test-key-not-used")
    c.client = _FakeClient(sink, '{"facts": []}')
    c.extract_facts("Mubert", ["Pricing — the regular price"], {"https://m.com/p": "text"})
    prompt = sink[0]["messages"][0]["content"]
    assert "NEVER put two figures in one `answer`" in prompt
    assert "its own exact `quote`" in prompt and "do not leave any out" in prompt


def test_a_full_sku_title_does_not_become_a_rung_label():
    """Live (Pigeon): the model returned the whole product title as `product`, so each rung led
    with 'PPSU Wide Neck Baby Bottle for Newborns 2 Packs, 5.4 Oz' and then repeated '5.4 Oz' from
    the basis — 170 characters of noise. A short PLAN name labels a rung; a SKU title does not."""
    sku = [{"answer": "$39.99", "url": "u", "quote": "q", "basis": "2 Pack, 5.4 Oz",
            "product": "PPSU Wide Neck Baby Bottle for Newborns 2 Packs, 5.4 Oz"},
           {"answer": "$43.99", "url": "u", "quote": "q", "basis": "2 Pack, 8.1 Oz",
            "product": "PPSU Wide Neck Baby Bottle 2 Packs, 8.1 Oz (3+ months)"}]
    assert _format_price_value(R.price_ladder(sku)) == "$39.99 (5.4 Oz); $43.99 (8.1 Oz); all 2 Pack"

    # a short plan name still labels its rung
    plans = [{"answer": "$39", "url": "u", "quote": "q", "basis": "per month", "product": "Pro"},
             {"answer": "$199", "url": "u", "quote": "q", "basis": "per month", "product": "Business"}]
    assert _format_price_value(R.price_ladder(plans)) == "$39 (Pro); $199 (Business); all per month"

    # a product that merely restates its own basis adds nothing, so it is not used as a label
    echo = [{"answer": "$10", "url": "u", "quote": "q", "basis": "5.4 Oz", "product": "5.4 Oz"},
            {"answer": "$20", "url": "u", "quote": "q", "basis": "8.1 Oz", "product": "8.1 Oz"}]
    assert _format_price_value(R.price_ladder(echo)) == "$10 (5.4 Oz); $20 (8.1 Oz)"
