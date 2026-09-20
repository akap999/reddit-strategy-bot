"""FU221 (Step 0) — research a brand's facts on its own site with every quote checked; read walled pages
through web fetch; stop rejecting normal pages as "blocked"; say WHY a fetch failed.

$0, no network: the page fetcher is monkeypatched and the Claude calls are a StubClaude subclass. The
real-world numbers behind the design (Railway, 19 Sep): the one-call design verified 2 of 4 quotes at
$0.75/brand; the split design here verified 4 of 4 at $0.27, and caught the Thyseed blog's sale-price
and phantom-price cells."""
import generators.research as R
from generators import brand_enrichment as BE
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude


class ResearchStub(StubClaude):
    """StubClaude + the three FU221 calls, each scripted and recorded."""

    def __init__(self, *, pages_handler=None, fetch_handler=None, facts_handler=None, **kw):
        super().__init__(**kw)
        self._pages_handler, self._fetch_handler, self._facts_handler = pages_handler, fetch_handler, facts_handler
        self.find_calls, self.fetch_calls, self.extract_calls = [], [], []

    def find_pages(self, brand, domains, needs, context="", max_searches=3, max_pages=4,
                   queries=None):
        self.find_calls.append({"brand": brand, "domains": list(domains), "needs": list(needs),
                                "queries": list(queries or [])})
        return list((self._pages_handler or (lambda b, d, n: []))(brand, domains, needs))[:max_pages]

    def web_fetch_text(self, url, max_content_tokens=8000):
        self.fetch_calls.append(url)
        return (self._fetch_handler or (lambda u: ("", "error")))(url)

    def extract_facts(self, brand, needs, pages, context="", guidance="", page_chars=12000):
        self.extract_calls.append({"brand": brand, "needs": list(needs), "pages": dict(pages)})
        return list((self._facts_handler or (lambda b, n, p: []))(brand, needs, pages))


# --------------------------------------------------------------------------- D. the quote gate
def test_quote_gate_tolerates_formatting_but_not_paraphrase():
    page = ("## Plans\n\nYou’ll pay a **$39 membership fee** at checkout for your first month. After that, "
            "your membership auto-renews at $149 per month (medication cost not included).")
    assert R.quote_on_page("You'll pay a $39 membership fee at checkout for your first month.", page)
    assert R.quote_on_page("your membership   auto-renews at $149 per month", page)
    assert R.quote_on_page("You'll pay a $39 membership fee … auto-renews at $149 per month", page)
    assert not R.quote_on_page("The membership is $39, then $149 monthly.", page)       # paraphrase
    assert not R.quote_on_page("$149", page)                                           # too short to prove
    assert not R.quote_on_page("auto-renews at $149 per month … You'll pay a $39", page)   # out of order


def test_price_on_page_takes_regular_never_sale_and_never_a_derived_figure():
    page = ("Quick add PPSU Wide Neck Baby Bottle for Newborns 2 Packs,5.4 Oz 4.9 Sale price $34.99 "
            "Regular price $39.99 4.9 + Quick add Glass Wide Neck Baby Bottle")
    prod = "PPSU Wide Neck Baby Bottle 2 Packs, 5.4 Oz"
    assert R.price_on_page("Regular price $39.99 for the 2-pack", prod, page)
    assert not R.price_on_page("$34.99 for the 2-pack", prod, page)                    # that is the SALE price
    assert not R.price_on_page("≈ $20.00 per bottle ($39.99 for 2)", prod, page)       # first figure derived
    assert not R.price_on_page("$39.99", "Titanium Water Flask", page)                 # not near that product


def test_product_data_is_appended_so_an_unshown_price_can_be_found():
    html = ('<html><head><title>Avent Anti-colic bottle</title><script type="application/ld+json">'
            '{"@type":"Product","name":"Anti-colic baby bottle 4oz","offers":{"@type":"Offer",'
            '"price":"8.99","priceCurrency":"USD"}}</script></head><body>'
            + "<p>" + "Anti-colic valve keeps air out of baby's tummy. " * 20 + "</p></body></html>")
    txt = R.page_text_from_html(html)
    assert "PRODUCT DATA: Anti-colic baby bottle 4oz — price 8.99 USD" in txt
    assert R.price_on_page("$8.99 single 4oz bottle", "Anti-colic baby bottle 4oz", txt)
    bare = '<html><title>Bottle</title><script>var p={"price":"19.99"}</script><p>x</p></html>'
    assert "price 19.99" in R.page_text_from_html(bare)


# --------------------------------------------------------------------------- needs (vertical-neutral)
def test_build_needs_follows_the_articles_columns_and_the_pricing_switch():
    dims = ["Starting Price", "Body Material", "Nipple Design"]
    on = R.build_needs(dims, ["anti-colic bottle"], include_pricing=True, subject="newborn bottles")
    assert on[0].startswith("Starting Price for anti-colic bottle") and "condition" in on[0]
    assert any(n.startswith("Body Material") for n in on) and any(n.startswith("Nipple Design") for n in on)
    off = R.build_needs(dims, ["anti-colic bottle"], include_pricing=False)
    assert not any(R.is_price_need(n) for n in off) and len(off) == 2                 # FU162: no price asked
    no_col = R.build_needs(["Contract length", "Coverage area"], [], include_pricing=True)
    assert any(n.startswith("Price of the comparable offering") for n in no_col)
    # FU223: availability is asked PER PRODUCT; with no products named it stays brand-level
    assert R.build_needs(["Coverage area"], [], include_pricing=False, geo="Florida")[-1] \
        .startswith("Availability or coverage in Florida")


# --------------------------------------------------------------------------- A–D end to end
def test_research_brand_verifies_quotes_reads_walled_pages_and_retries_the_rest(monkeypatch):
    pages = {
        "https://acme.com/pricing": ("<html><title>Pricing</title><p>" + "Plans for every team. " * 30
                                     + "The Pro plan costs $49 per user per month, billed annually.</p>"
                                       "</html>", "ok"),
        "https://acme.com/security": ("", "blocked"),
        "https://acme.com/about": ("", "not-found"),
        "https://acme.com/compliance": ("<html><title>Compliance</title><p>" + "We take security "
                                        "seriously. " * 30 + "Acme is SOC 2 Type II certified.</p></html>",
                                        "ok"),
    }
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0: pages.get(url, ("", "not-found")))
    rounds = [["https://acme.com/pricing", "https://acme.com/security", "https://acme.com/about"],
              ["https://acme.com/compliance"]]

    def facts(brand, needs, pgs):
        out = []
        for i, n in enumerate(needs, start=1):
            if "Price" in n and "https://acme.com/pricing" in pgs:
                out.append({"need": i, "answer": "$49 per user per month, billed annually",
                            "url": "https://acme.com/pricing", "product": "Pro plan",
                            "quote": "The Pro plan costs $49 per user per month, billed annually."})
            if "Security" in n and "https://acme.com/security" in pgs:     # a paraphrase — must NOT pass
                out.append({"need": i, "answer": "SOC 2 certified", "url": "https://acme.com/security",
                            "quote": "Acme holds a SOC 2 report for its platform."})
            if "Security" in n and "https://acme.com/compliance" in pgs:
                out.append({"need": i, "answer": "SOC 2 Type II", "url": "https://acme.com/compliance",
                            "quote": "Acme is SOC 2 Type II certified."})
        return out

    stub = ResearchStub(pages_handler=lambda b, d, n: rounds.pop(0) if rounds else [],
                        fetch_handler=lambda u: ("# Security\nWe encrypt data at rest.", "ok"),
                        facts_handler=facts)
    res = R.research_brand(stub, "Acme", ["acme.com"], ["Price of Pro — the regular price", "Security certification"],
                           log=lambda m: None)
    by_need = {f["need"]: f for f in res["facts"]}
    assert by_need["Price of Pro — the regular price"]["verified_by"] == "quote"
    assert by_need["Security certification"]["url"] == "https://acme.com/compliance"   # round 2 found it
    assert res["unconfirmed"] == []
    assert stub.fetch_calls == ["https://acme.com/security"]          # walled → web fetch; missing → not
    assert res["pages"]["https://acme.com/about"].startswith("unread (not-found")
    assert len(stub.find_calls) == 2 and stub.find_calls[1]["needs"] == ["Security certification"]
    blocks = R.facts_to_blocks("Acme", res["facts"])
    assert {b["url"] for b in blocks} == {"https://acme.com/pricing", "https://acme.com/compliance"}
    assert all(b["label"] == "Acme" for b in blocks)
    assert 'the page says: "The Pro plan costs $49 per user per month, billed annually."' in blocks[0]["text"]


def test_price_entry_carries_basis_and_kind():
    e = R.price_entry({"answer": "Starts at $199/mo, billed monthly", "url": "https://h.com/p",
                       "basis": "per month, membership required", "quote": "Starts at $199/mo"},
                      checked_at="2026-09-19T00:00:00Z")
    assert e["value"] == "$199" and e["kind"] == "from" and e["source"] == "own"
    assert e["basis"] == "per month, membership required" and e["via"] == "research"
    assert R.price_entry({"answer": "not stated", "url": "x"}) is None


# --------------------------------------------------------------------------- R3 / reasons
def test_looks_blocked_no_longer_rejects_a_normal_page_that_loads_a_captcha_script():
    normal = ("<html><head><title>Atradius</title><script>grecaptcha.render('captcha')</script></head><body>"
              + "<p>" + "Atradius is a global provider of trade credit insurance. " * 40 + "</p></body></html>")
    assert BE._looks_blocked(normal) == ""
    cf = "<html><head><title>Just a moment...</title></head><body><p>Checking your browser</p></body></html>"
    assert BE._looks_blocked(cf) == "challenge-page"
    akamai = "<html><title>Error</title><body><h1>Access Denied</h1><p>You don't have permission.</p></body></html>"
    assert BE._looks_blocked(akamai) == "challenge-page"
    footer = ("<html><title>Contact</title><body><p>" + "Call our office any weekday for a quote. " * 60
              + "</p><small>This site is protected by reCAPTCHA.</small></body></html>")
    assert BE._looks_blocked(footer) == ""                     # long real page, captcha only in the footer
    sitemap = '<?xml version="1.0"?><urlset><url><loc>https://a.com/</loc></url></urlset>'
    assert BE._looks_blocked(sitemap) == ""                    # a small sitemap is not "thin content"


def test_fetch_page_says_why_and_a_404_spends_no_residential_gb(monkeypatch):
    calls = []

    class _R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    def fake_get(url, headers=None, timeout=None, allow_redirects=True, proxies=None):
        calls.append(bool(proxies))
        return _R(404) if "missing" in url else _R(403)
    monkeypatch.setattr(BE.requests, "get", fake_get)
    monkeypatch.setattr(BE.time, "sleep", lambda s: None)
    monkeypatch.setenv("REDDIT_HTTP_PROXY", "http://proxy:1")
    assert BE._fetch_page("https://a.com/missing") == ("", "not-found")
    assert calls == [False]                                   # no retry, no residential shot
    calls.clear()
    assert BE._fetch_page("https://a.com/walled") == ("", "blocked")
    assert calls[-1] is True                                  # a walled page does get the residential shot


def test_fetch_url_reads_a_walled_page_through_web_fetch_but_not_a_missing_one(monkeypatch):
    import generators.blog_gen as BG
    monkeypatch.setattr(BG, "_fetch_page",
                        lambda url: ("", "not-found") if "missing" in url else ("", "blocked"))
    stub = ResearchStub(fetch_handler=lambda u: ("# Pricing\nPro is $49 per user per month.", "ok"))
    gen = BlogGenerator(stub, db=None)
    assert "Pro is $49" in gen._fetch_url("https://walled.com/pricing")
    assert gen._fetch_url("https://walled.com/missing") == ""
    assert stub.fetch_calls == ["https://walled.com/pricing"]
    assert gen._fetch_reasons["https://walled.com/pricing"] == "blocked; read via web fetch"
    assert gen._fetch_reasons["https://walled.com/missing"] == "not-found"
    for p in ("/a", "/b", "/c", "/d"):                        # per-domain cap for guessed paths
        gen._fetch_url("https://other.com" + p)
    assert sum(u.startswith("https://other.com") for u in stub.fetch_calls) == BG._WEB_FETCH_PER_DOMAIN
    gen._fetch_url("https://other.com/chosen", force_web_fetch=True)
    assert stub.fetch_calls[-1] == "https://other.com/chosen"


# --------------------------------------------------------------------------- wired into the pipeline
def _source_with_research(monkeypatch, pages, facts, tools=("Ro",), dims=("Pricing", "Membership terms"),
                          products=("semaglutide",), include_pricing=True):
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0: pages.get(url, ("", "not-found")))

    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": list(tools), "peer_tools": list(tools), "dimensions": list(dims),
                    "products": list(products), "claims": [], "core_topic": "glp-1"}
        if '"domains"' in p:
            return {"domains": {t: t.lower() + ".com" for t in tools}}
        return {}
    stub = ResearchStub(call_handler=call_h,
                        pages_handler=lambda b, d, n: [u for u in pages if d and u.split("/")[2] == d[0]],
                        facts_handler=facts)
    gen = BlogGenerator(stub, db=None)
    body = "| Clinic | Pricing |\n|---|---|\n" + "".join(f"| {t} | ? |\n" for t in tools)
    src = gen._source_for_completion({"name": "Acme", "domain_url": "https://acme.com", "category": "telehealth"},
                                     "best glp-1 clinics", {"body_markdown": body}, ymyl=None,
                                     include_pricing=include_pricing)
    return gen, stub, src


def test_competitor_research_replaces_the_old_first_party_search(monkeypatch):
    pages = {"https://ro.com/weight-loss/pricing": (
        "<html><title>Ro pricing</title><p>" + "Ro Body is a weight-loss program. " * 30
        + "The Ro Body membership costs $39 for the first month. If you stay on a monthly plan, the "
          "ongoing cost is $149/month.</p></html>", "ok")}

    def facts(brand, needs, pgs):
        return [{"need": i, "answer": "$39 first month, then $149/month", "url": "https://ro.com/weight-loss/pricing",
                 "quote": "The Ro Body membership costs $39 for the first month.", "product": "Ro Body membership",
                 "basis": "first month, then $149/month on a monthly plan"}
                for i, n in enumerate(needs, start=1) if R.is_price_need(n)] if brand == "Ro" else []
    gen, stub, src = _source_with_research(monkeypatch, pages, facts)
    ro_blocks = [b for b in src["fresh"] if b["label"] == "Ro"]
    assert any("The Ro Body membership costs $39 for the first month." in b["text"] for b in ro_blocks)
    # the old Tier-1 own-site search for Ro never ran — research answered it
    assert not any((s.get("allowed") or []) == ["ro.com"] for s in stub.searches)
    assert any(c["brand"] == "Ro" for c in stub.find_calls)
    assert any(c["brand"] == "Acme" for c in stub.find_calls)          # the subject is researched too
    assert all(c["domains"] == ["acme.com"] for c in stub.find_calls if c["brand"] == "Acme")   # own site only


def test_research_asks_no_price_when_pricing_is_off_and_can_be_switched_off(monkeypatch):
    gen, stub, _ = _source_with_research(monkeypatch, {}, lambda b, n, p: [], include_pricing=False,
                                         dims=("Pricing", "Coverage area"))
    asked = [n for c in stub.find_calls for n in c["needs"]]
    assert asked and not any(R.is_price_need(n) for n in asked)
    monkeypatch.setattr("generators.blog_gen._RESEARCH_ON", False)
    gen2, stub2, _ = _source_with_research(monkeypatch, {}, lambda b, n, p: [])
    assert stub2.find_calls == []                                        # BLOG_RESEARCH=0 → old tiers only


def test_a_quoted_sale_price_is_rejected_even_though_the_quote_is_on_the_page(monkeypatch):
    # The live Pigeon run (19 Sep): the model quoted "…2Packs, 8.1 Oz(240 ml) for 3+ months 4.8 Sale price
    # $35.99" — word for word on the page, and a SALE price. It must not become the price.
    page = ("<html><title>Baby bottles</title><p>" + "Wide neck bottles for every stage. " * 30
            + "Glass Wide Neck Baby Bottle, 2Packs, 8.1 Oz(240 ml) for 3+ months 4.8 Sale price $35.99 4.8 "
              "Save $5.00 PPSU Wide Neck Baby Bottle 2 Packs, 5.4 Oz Sale price $34.99 Regular price $39.99</p></html>")
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0: (page, "ok"))
    answers = iter(["$35.99 for the glass 2-pack", "$39.99 for the PPSU 2-pack"])
    quotes = iter(["Glass Wide Neck Baby Bottle, 2Packs, 8.1 Oz(240 ml) for 3+ months 4.8 Sale price $35.99",
                   "PPSU Wide Neck Baby Bottle 2 Packs, 5.4 Oz Sale price $34.99 Regular price $39.99"])
    stub = ResearchStub(pages_handler=lambda b, d, n: ["https://p.com/bottles"],
                        facts_handler=lambda b, n, p: [{"need": 1, "answer": next(answers),
                                                         "url": "https://p.com/bottles", "quote": next(quotes)}])
    stub.find_pages = (lambda brand, domains, needs, context="", max_searches=3, max_pages=4,
                       queries=None:
                       ["https://p.com/bottles"] if not stub.find_calls.append(1) and len(stub.find_calls) == 1
                       else ["https://p.com/bottles-2"])
    monkeypatch.setattr(R, "_fetch_page", lambda url, retries=0: (page, "ok"))
    res = R.research_brand(stub, "Pigeon", ["p.com"], ["Starting price — the regular price"], log=lambda m: None)
    assert [f["answer"] for f in res["facts"]] == ["$39.99 for the PPSU 2-pack"]   # the sale figure was refused
    assert R.figure_is_sale("$35.99", page) and not R.figure_is_sale("$39.99", page)


# ------------------------------------------------- a host that walls us is only discovered once
import pytest                                                   # noqa: E402


@pytest.fixture(autouse=True)
def _forget_walls():
    """The wall note is process-wide by design, so each test starts with no assumptions."""
    BE.forget_walled_domains()
    yield
    BE.forget_walled_domains()


def _recording_get(monkeypatch, handler):
    calls = []

    class _R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    def fake_get(url, headers=None, timeout=None, allow_redirects=True, proxies=None):
        calls.append((url, bool(proxies)))
        return _R(*handler(url))
    monkeypatch.setattr(BE.requests, "get", fake_get)
    monkeypatch.setattr(BE.time, "sleep", lambda s: None)
    monkeypatch.setenv("REDDIT_HTTP_PROXY", "http://proxy:1")
    return calls


def test_a_walled_host_costs_one_ladder_not_one_per_page(monkeypatch):
    """jollysearch.com had 7 cited pages in the 19 Sep run; each paid a certain-to-403 residential
    fetch. The wall is learned once and the rest of its pages go straight to the caller's fallback."""
    calls = _recording_get(monkeypatch, lambda u: (403,))
    assert BE._fetch_page("https://walled.com/a") == ("", "blocked")
    first = len(calls)
    assert any(proxied for _u, proxied in calls), "the first page still earns a residential attempt"
    for p in ("/b", "/c", "/d", "/e", "/f", "/g"):
        assert BE._fetch_page("https://walled.com" + p) == ("", "blocked")
    assert len(calls) == first, "no further page on that host touched the network at all"


def test_the_wall_note_expires_so_a_transient_block_does_not_stick(monkeypatch):
    calls = _recording_get(monkeypatch, lambda u: (403,))
    BE._fetch_page("https://flaky.com/a")
    n = len(calls)
    monkeypatch.setattr(BE, "_WALLED_TTL", 0)                   # as if the note had aged out
    BE._fetch_page("https://flaky.com/b")
    assert len(calls) > n, "an expired note is re-probed rather than trusted forever"


def test_only_a_wall_is_remembered_not_a_missing_page_or_a_network_error(monkeypatch):
    calls = _recording_get(monkeypatch, lambda u: (404,) if "missing" in u else (500,))
    BE._fetch_page("https://a.com/missing")
    BE._fetch_page("https://b.com/broken")
    n = len(calls)
    BE._fetch_page("https://a.com/other")
    BE._fetch_page("https://b.com/other")
    assert len(calls) > n, "404 and 500 are not walls — those hosts are still probed"


def test_a_caller_can_insist_on_probing_a_known_wall(monkeypatch):
    calls = _recording_get(monkeypatch, lambda u: (403,))
    BE._fetch_page("https://walled.com/a")
    n = len(calls)
    BE._fetch_page("https://walled.com/b", ignore_wall=True)
    assert len(calls) > n


def test_the_second_page_of_a_walled_host_still_gets_read_through_web_fetch(monkeypatch):
    """The saving must not cost us the page: the caller sees "blocked" and falls back as before."""
    _recording_get(monkeypatch, lambda u: (403,))
    stub = ResearchStub(fetch_handler=lambda u: ("# Pricing\nPro is $49 per user per month.", "ok"))
    gen = BlogGenerator(stub, db=None)
    gen._must_read_urls = {"https://walled.com/a", "https://walled.com/b"}
    assert "Pro is $49" in gen._fetch_url("https://walled.com/a")
    assert "Pro is $49" in gen._fetch_url("https://walled.com/b")
    assert stub.fetch_calls == ["https://walled.com/a", "https://walled.com/b"]
    assert gen._fetch_reasons["https://walled.com/b"] == "blocked; read via web fetch"


def test_a_403_from_the_residential_rung_is_a_wall_not_a_network_error(monkeypatch):
    """jollysearch.com answers 202 to the datacenter IP and 403 to the residential one. The reason
    used to come out "error" — so the scoreboard blamed the network and the wall was never learned."""
    def handler(url):
        return (403,) if "proxy" in url else (202, "")
    calls = []

    class _R:
        def __init__(self, code, text=""):
            self.status_code, self.text = code, text

    def fake_get(url, headers=None, timeout=None, allow_redirects=True, proxies=None):
        calls.append(bool(proxies))
        return _R(403) if proxies else _R(202, "")
    monkeypatch.setattr(BE.requests, "get", fake_get)
    monkeypatch.setattr(BE.time, "sleep", lambda s: None)
    monkeypatch.setenv("REDDIT_HTTP_PROXY", "http://proxy:1")
    assert BE._fetch_page("https://odd.com/a", retries=0) == ("", "blocked")
    assert BE._walled("https://odd.com/b"), "and the host is remembered as walled"
