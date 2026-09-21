"""FU251 — the fetcher calls a wall a missing page, and the removal passes mutilate what is left.

Two audited articles scored 58/100 and 45/100. Ten of the findings across them are one disease with
two heads.

THE FETCHER. `fda.gov` sits behind Akamai, which answers a datacenter IP with
`302 → /apology_objects/abuse-detection-apology.html`; that apology object then 404s with ten bytes
of "Not found". The ladder read the FINAL status and returned "not-found" — the one reason that
stops everything: no retry, no residential rung, no Anthropic web fetch. Three FDA sources in the
19 Sep scoreboard were recorded as pages that do not exist. They exist, and Anthropic's web fetch
reads them; we were never asking. NCBI is the same story told differently: it answers 203
Non-Authoritative with "Cookies must be enabled", and the ladder tested `status_code == 200`, so the
body was never even looked at and a cookie wall came out as a network error.

THE REMOVAL PASSES. A source nobody could read then triggers deletion, and no removal path has ever
looked at what the removed text was holding up. The audited article shows the whole catalogue:

  * four Source cells published as "—", from an UNCAPPED `[S#]` strip that ran over the tables and a
    Source column that the width rules deliberately never drop;
  * a section opening "Waist circumference and lipid markers ALSO showed…" and an FAQ answer opening
    "This strict protocol is necessary because…" — both continuing a sentence that is gone;
  * an FAQ question answered in 56 characters;
  * a 60-word paragraph spliced onto its own tail at "…without an in-person visit., which
    includes…", which the clause de-duplicator could not see because it only accepted `[,;:]`
    between the two copies and this one is separated by a full stop.

So every removal is now applied ON ITS OWN and the result inspected with the scoreboard's own
detectors: one that leaves damage is widened to the whole paragraph, and if that is damaging too it
is refused and reported. A pass can no longer create a defect the scoreboard would report.

The fixtures are the operator's audited articles, unedited.
"""
import os
import re

import pytest

import generators.brand_enrichment as BE
from generators.blog_eval import (body_damage, detect_blank_source_cells, detect_broken_join,
                                  detect_repeated_sentence, detect_stranded_reference,
                                  detect_stub_answer)
from generators.blog_gen import (_MONEY_RE, BlogGenerator, _composition_included,
                                 _format_price_value, _norm_price_composition,
                                 _strip_markers_safely)

HERE = os.path.dirname(__file__)


def _fixture(name):
    with open(os.path.join(HERE, "fixtures", name), encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def oral():
    """"Oral Semaglutide vs Injections" as published — the 45/100 article."""
    return _fixture("fu251_oral_vs_injection_body.md")


@pytest.fixture(scope="module")
def cost():
    """"How Much Does GLP-1 Treatment Cost Per Month in the US?" as published — the 58/100 one."""
    return _fixture("fu251_glp1_cost_body.md")


@pytest.fixture
def gen():
    return BlogGenerator.__new__(BlogGenerator)


class _Resp:
    """A `requests.Response` as far as the ladder is concerned."""

    def __init__(self, status, text="", url="https://x.test/a", history=(), ctype="text/html"):
        self.status_code = status
        self.text = text
        self.content = text.encode()
        self.url = url
        self.history = list(history)
        self.headers = {"Content-Type": ctype}


def _ladder(monkeypatch, resp, proxy=""):
    monkeypatch.setattr(BE.requests, "get", lambda *a, **k: resp)
    monkeypatch.setattr(BE.time, "sleep", lambda s: None)
    monkeypatch.setenv("REDDIT_HTTP_PROXY", proxy)
    BE.forget_walled_domains()
    return BE._fetch_page("https://x.test/a", retries=0)


# ── 1. the fetcher: a wall is a wall, whatever status it hides behind ────────────────────────────
def test_a_redirect_to_a_block_page_is_blocked_not_missing(monkeypatch):
    """The exact fda.gov shape: 302 to an abuse-detection apology object, which itself 404s.

    "not-found" is the ONE reason that ends the ladder, so this misreading cost three FDA sources
    their residential attempt AND their web fetch — the rung that can actually read them."""
    hop = _Resp(302, url="https://www.fda.gov/drugs/some-safety-communication")
    landed = _Resp(404, "Not found\n",
                   url="https://www.fda.gov/apology_objects/abuse-detection-apology.html",
                   history=[hop])
    assert _ladder(monkeypatch, landed) == ("", "blocked")


@pytest.mark.parametrize("path", [
    "/apology_objects/abuse-detection-apology.html",
    "/errors/access-denied",
    "/sorry/index",
    "/cdn-cgi/challenge-platform/h/b/orchestrate",
    "/bot-detection",
    "/blocked.html",
])
def test_every_common_block_landing_path_is_recognised(monkeypatch, path):
    """Akamai, Cloudflare, Imperva and Google each name the page differently. None is hard-coded to
    a host — the test is the PATH a refusal lands on."""
    landed = _Resp(403, "", url="https://vendor.example" + path,
                   history=[_Resp(302, url="https://vendor.example/real/page")])
    assert _ladder(monkeypatch, landed)[1] == "blocked"


def test_a_real_missing_page_is_still_missing(monkeypatch):
    """The widened rule must not turn every 404 into a wall: a missing page still skips the metered
    residential rung and the web fetch, which is why the reason exists at all."""
    assert _ladder(monkeypatch, _Resp(404, "", url="https://x.test/gone")) == ("", "not-found")


def test_a_block_path_in_a_url_that_SUCCEEDED_is_not_a_wall(monkeypatch):
    """A real article at /blog/the-challenge-of-scaling answers 200 and is returned as content. The
    path test is only ever consulted on a response that did not succeed."""
    html = "<html><body>" + ("real editorial prose about scaling teams. " * 30) + "</body></html>"
    body, reason = _ladder(monkeypatch, _Resp(200, html, url="https://x.test/blog/the-challenge-of-scaling"))
    assert reason == "ok" and "scaling teams" in body


def test_a_203_with_a_cookie_wall_is_blocked(monkeypatch):
    """NCBI's actual answer to a datacenter IP. The ladder tested `== 200`, so the body was never
    read and six PubMed/PMC citations came out as network errors instead of walls."""
    wall = ("<html><head><title>pubmed.ncbi.nlm.nih.gov</title></head><body>"
            "Cookies must be enabled. Enable cookies for pubmed.ncbi.nlm.nih.gov and reload this "
            "page to continue.</body></html>")
    assert _ladder(monkeypatch, _Resp(203, wall)) == ("", "blocked")


def test_a_203_that_carries_a_real_page_is_read(monkeypatch):
    """Widening the accepted statuses has to cut both ways — a 203 with a real document is content."""
    html = "<html><body>" + ("Measured outcomes for the cohort are reported below. " * 40) + "</body></html>"
    body, reason = _ladder(monkeypatch, _Resp(203, html))
    assert reason == "ok" and "Measured outcomes" in body


def test_an_empty_body_never_reaches_the_pdf_sniff(monkeypatch):
    """A 2xx with no bytes is not a file. Reading further used to be impossible because only 200
    got this far; now that more statuses do, emptiness is settled first."""
    assert _ladder(monkeypatch, _Resp(202, ""))[0] == ""


# ── 2. the audited article: every reported mutilation is detected ────────────────────────────────
def test_the_four_blanked_source_cells_are_detected(oral):
    """"the Source column appears blanked (—) for four rows" — the auditor's words."""
    hits = detect_blank_source_cells(oral)
    assert len(hits) == 4
    assert any("HbA1c reduction" in h["detail"] for h in hits)
    assert any("Waist circumference" in h["detail"] for h in hits)


def test_the_section_that_opens_mid_thought_is_detected(oral):
    hits = detect_stranded_reference(oral)
    details = " ".join(h["detail"] for h in hits)
    assert "Waist circumference and lipid markers also showed" in details
    assert "This strict protocol is necessary" in details


def test_the_one_sentence_faq_answer_is_detected(oral):
    hits = detect_stub_answer(oral)
    assert len(hits) == 1
    assert "Yes, when equivalent plasma concentrations are achieved." in hits[0]["detail"]


def test_the_spliced_paragraph_and_its_seam_are_detected(oral):
    assert any("in-person visit., which includes" in h["detail"] for h in detect_broken_join(oral))
    assert detect_repeated_sentence(oral)


def test_the_audited_article_reports_every_finding_at_once(oral):
    """The gate the project did not have: ten findings, free, deterministic, no model, no network."""
    by = {}
    for h in body_damage(oral):
        by[h["check"]] = by.get(h["check"], 0) + 1
    assert by == {"blank-source-cell": 4, "stranded-reference": 2, "broken-join": 1,
                  "duplicated-sentence": 2, "stub-answer": 1}


# ── 3. no false positives on articles the operator did not complain about ────────────────────────
@pytest.mark.parametrize("name", [
    "fu249_semaglutide_body.md",      # medical
    "fu220_jolly_body.md",            # SEO agencies
    "fu216_cmk_guide_body.md",        # a general guide
    "fu232_agency_table.md",          # a comparison table
    "fu233_glp1_body.md",
    "fu237_construction_script_v4.md",  # construction
])
def test_clean_articles_stay_clean(name):
    """Across four verticals. A detector that fires on ordinary prose would make the gate useless."""
    assert body_damage(_fixture(name)) == []


def test_an_abbreviation_is_not_a_broken_join():
    """"(e.g., type 2 diabetes…)" and "et al., 2024" end in a dot followed by a comma and are not
    seams. The lookbehind requires four letters, which "g", "al" and "etc" do not have."""
    for ok in ["Comorbidities (e.g., type 2 diabetes, hypertension) qualify a patient for therapy.",
               "Reported by Wilding et al., 2021, in the STEP 1 extension analysis of the cohort.",
               "The list covers devices, supplies, etc., for the first ninety days of the program."]:
        assert detect_broken_join("## H\n\n" + ok) == []


def test_a_demonstrative_followed_by_a_verb_is_ordinary_english():
    """"This is the clinical rationale" points at the heading just read. "This strict protocol"
    names a thing the reader was never shown. Only the second is damage."""
    assert detect_stranded_reference("## Does it help?\n\nThis is the primary reason clinicians "
                                     "prescribe it alongside a structured diet plan.\n") == []
    assert detect_stranded_reference("## Does it help?\n\nThis strict protocol is necessary because "
                                     "absorption is reduced by food and other drugs.\n")


def test_an_faq_answer_may_restate_a_body_sentence():
    """Restating the article in question form is what an FAQ is FOR — it must not read as a repeat."""
    body = ("## Cost\n\nWeight-loss drugs are not covered by Medicare and many commercial plans "
            "exclude them entirely.\n\n## FAQ\n\n### Does Medicare cover it?\n\nWeight-loss drugs "
            "are not covered by Medicare and many commercial plans exclude them entirely.\n")
    assert detect_repeated_sentence(body) == []


# ── 4. the passes can no longer CREATE the damage ────────────────────────────────────────────────
_TABLE_BODY = """# Which option works better?

| Measure | Option A | Option B | Source |
| --- | --- | --- | --- |
| Reduction at 6 months | 1.58% | 1.62% | [S4] |
| Weight change at 6 months | 3.77% | 3.48% | [S4] |
| Waist change | 1.78 cm | 1.80 cm | [S4] |

## Sources

1. [S4] https://walled.example/paper
"""


def test_a_source_cell_is_never_blanked_by_the_marker_strip():
    """The precise mechanism behind the four "—" cells: the strip ran over the whole body with no
    cap, a Source cell whose entire content was "[S4]" came out empty, and the width rules never
    drop a Source column, so the gap was written out and published."""
    out = _strip_markers_safely(_TABLE_BODY, {4})
    assert "[S4]" in out                      # the citation the cell exists to carry survives
    assert detect_blank_source_cells(out) == []


def test_the_marker_strip_still_clears_prose_markers():
    """It has a real job — a marker pointing at a page the article never opened must go from the
    prose, so `_rebuild_sources` drops it from the list instead of advertising it."""
    out = _strip_markers_safely("The cohort lost 4.1 kg over six months [S4].\n", {4})
    assert "[S4]" not in out


def test_a_removal_that_would_strand_the_next_sentence_takes_the_paragraph(gen):
    """The "Waist circumference … also" shape, reproduced. Removing the first sentence leaves the
    second opening a section mid-thought, so the pass must take both or neither — never just the
    first, which is what shipped."""
    body = ("# T\n\n## Are the outcomes equivalent?\n\n"
            "The trial reported a 12.4% reduction against placebo [S1]. "
            "Waist circumference and lipid markers also showed no significant difference [S1].\n")
    gen._claim_pages = {}
    out, note = gen._unsourced_figure_check(body, [{"label": "P", "url": "https://p.test/x",
                                                    "text": "A page that states nothing numeric."}])
    assert "12.4%" not in out
    assert detect_stranded_reference(out) == []
    assert "Waist circumference" not in out    # it went with the sentence it depended on


def test_a_removal_with_nothing_depending_on_it_is_still_made(gen):
    """The guard may not become a reason to keep an unsourced figure. A sentence nothing leans on
    is removed exactly as before."""
    body = ("# T\n\n## What does it cost?\n\n"
            "Independent audits put the figure at 12.4% of revenue [S1]. "
            "Pricing is published on each provider's own site and changes without notice.\n")
    gen._claim_pages = {}
    out, note = gen._unsourced_figure_check(body, [{"label": "P", "url": "https://p.test/x",
                                                    "text": "A page that states nothing numeric."}])
    assert "12.4%" not in out
    assert "Pricing is published" in out
    assert body_damage(out) == []


def test_the_deduplicator_sees_a_repeat_separated_by_a_full_stop(oral):
    """The published seam, fixed: the two copies are separated by ".," and the old pattern only
    accepted "[,;:]", so the 60-word repeat was never even a candidate."""
    out, n = BlogGenerator._dedupe_repeated_clauses(oral)
    assert n == 1
    assert "in-person visit., which includes" not in out
    assert detect_broken_join(out) == []
    assert detect_repeated_sentence(out) == []


def test_deduplication_leaves_a_sentence_that_reads(oral):
    line = next(ln for ln in BlogGenerator._dedupe_repeated_clauses(oral)[0].split("\n")
                if "in-person visit" in ln)
    assert line.rstrip().endswith("without an in-person visit.")
    assert line.count("in-person visit") == 1


def test_the_deduplicator_leaves_an_ordinary_article_alone(cost):
    """It rewrites sentences, so it has to be provably inert on prose that is not repeating."""
    out, n = BlogGenerator._dedupe_repeated_clauses(cost)
    assert (out, n) == (cost, 0)


def test_end_to_end_the_source_column_no_longer_publishes_em_dashes(gen):
    """The whole chain the reader saw, in one assertion: the strip empties the Source cell, and
    `_resolve_table_punts` — which is the one pass that never drops a Source column — writes "—"
    into the gap and publishes it. Four of these shipped."""
    tbl = ("# Which option works better?\n\n"
           "| Measure | Option A | Option B | Source |\n"
           "| --- | --- | --- | --- |\n"
           "| Reduction at 6 months | 1.58% | 1.62% | [S4] |\n"
           "| Weight change at 6 months | 3.77% | 3.48% | [S4] |\n"
           "| Waist change | 1.78 cm | 1.80 cm | [S4] |\n")
    published = gen._resolve_table_punts(_strip_markers_safely(tbl, {4}))
    assert "| — |" not in published
    assert published.count("[S4]") == 3
    # and the reverse, so the assertion is not vacuous: the unguarded strip did publish them
    was = gen._resolve_table_punts(tbl.replace("[S4]", ""))
    assert was.count("| — |") == 3
    assert detect_blank_source_cells(published) == []


# ── 5. a price may only rest on a page that SELLS or PUBLISHES it ────────────────────────────────
def _blocks_from_sources(body):
    """The article's own `## Sources` list, back into the evidence blocks the checks read."""
    tail = body.split("## Sources", 1)[-1]
    blocks = []
    for m in re.finditer(r"\[S(\d+)\]\s*(.*?)\s*-\s*\[(https?://[^\]]+)\]", tail):
        n, url = int(m.group(1)), m.group(3)
        while len(blocks) < n:
            blocks.append({"label": "", "url": "", "text": ""})
        blocks[n - 1] = {"label": m.group(2)[:60], "url": url, "text": "x" * 800}
    return blocks


PETERMD = {"name": "PeterMD", "domain_url": "https://getpetermd.com"}


def test_the_wegovy_list_price_came_from_four_consumer_blogs(gen, cost):
    """The audited defect. `_accept_price_candidate` already requires the brand's own site or a
    named retailer — but that gate only guards the LEDGER, and this figure was typed into prose with
    a "third-party ·" citation, so nothing it passed through had an opinion about it. wegovy.com was
    never consulted because nothing in the pipeline is capable of going there."""
    out, note = gen._price_source_check(cost, _blocks_from_sources(cost), PETERMD)
    assert "price-source" in note
    assert "$1,349" in note
    assert "$1,349" not in out.split("## Sources")[0]   # not restated elsewhere — gone
    # the now-uncited blogs leave the Sources list in `_rebuild_sources`, which runs after this
    assert "[S2]" not in out.split("## Sources")[0]


def test_removing_those_prices_leaves_the_article_intact(gen, cost):
    """Eight sentences out of a cost article, and the guard keeps every one of them clean."""
    out, _note = gen._price_source_check(cost, _blocks_from_sources(cost), PETERMD)
    assert body_damage(out) == []


def test_a_correctly_sourced_competitor_price_is_untouched(gen, cost):
    """Ro's $149, Calibrate's $199 and Noom's $129 each cite that company's own pricing page, and
    none of them is named in the sentence or the cell that carries the figure — the entity is in the
    row's name column or the section heading. Matching the sentence alone called all of them
    unsourced, which would have emptied the comparison table this article exists for."""
    out, _note = gen._price_source_check(cost, _blocks_from_sources(cost), PETERMD)
    for kept in ("$149", "$199", "$129", "$279", "$270"):
        assert kept in out, kept


def test_the_publishers_own_price_is_always_sourceable(gen):
    body = ("# T\n\n## What does it cost?\n\nOur programme is $270 per month, all in [S1].\n")
    blocks = [{"url": "https://getpetermd.com/product/glp1m2m/", "text": "x" * 800}]
    assert gen._price_source_check(body, blocks, PETERMD) == (body, "")


@pytest.mark.parametrize("url,unit", [
    ("https://getpetermd.com/product/x", "PeterMD charges $270 per month [S1]."),
    ("https://joincalibrate.com/pricing", "Calibrate's programme is $199 per month [S1]."),
    ("https://ro.co/weight-loss/pricing/", "Ro charges $149 per month [S1]."),
    ("https://www.noom.com/med/pricing/", "Noom Med is $279 per month [S1]."),
    ("https://www.amazon.com/dp/B0XYZ", "The kit lists at $49.99 [S1]."),
    ("https://www.cms.gov/newsroom/fact-sheets/x", "The negotiated price is $149 per month [S1]."),
])
def test_a_page_that_sets_the_price_is_accepted(gen, url, unit):
    """"get"/"join" prefixes, a two-letter brand, a possessive, a multi-word name, a retailer and a
    .gov — all vertical-neutral, none hard-coded."""
    body = "# T\n\n## Cost\n\n" + unit + "\n"
    assert gen._price_source_check(body, [{"url": url, "text": "x" * 800}],
                                   {"name": "Other", "domain_url": "https://other.test"})[1] == ""


@pytest.mark.parametrize("url", [
    "https://sesamecare.com/blog/wegovy-cost-without-insurance",
    "https://www.buzzrx.com/blog/how-much-is-wegovy-without-insurance",
    "https://glpchart.com/wegovy-cost/",
    "https://www.weightwatchers.com/us/blog/weight-loss/wegovy-cost",
    "https://www.forbes.com/health/weight-loss/wegovy-cost/",
])
def test_a_page_that_only_repeats_a_price_is_rejected(gen, url):
    """Reputable or not, a page that does not set the price is not a price source. Two of these
    belong to real health brands and one is Forbes; none of them sells Wegovy."""
    body = "# T\n\n## Cost\n\nWegovy lists at $1,349 per month at retail [S1].\n"
    assert "price-source" in gen._price_source_check(
        body, [{"url": url, "text": "x" * 800}], PETERMD)[1]


def test_a_money_figure_that_is_not_a_price_is_left_alone(gen):
    """The check must not become a general tax on every dollar sign: a market size, a fine, a salary
    and a funding round are not prices, and the pages that report them are the right sources."""
    for unit in ["The GLP-1 market reached $24 billion in 2024 [S1].",
                 "The agency issued a $2.3 million penalty against the compounder [S1].",
                 "The company raised $150 million in its Series C round [S1]."]:
        body = "# T\n\n## Background\n\n" + unit + "\n"
        assert gen._price_source_check(
            body, [{"url": "https://www.reuters.com/x", "text": "x" * 800}], PETERMD)[1] == ""


def test_a_systematically_unsourced_article_is_reported_not_gutted(gen):
    """More failures than the fabrication cap means the SOURCING failed, not the sentences. The
    article is left exactly as it was, with a note that names the figures and says where to put
    them — the same escape valve every other fabrication pass has."""
    rows = "".join(f"\n## Section {i}\n\nOption {i} charges ${100 + i} per month [S1].\n"
                   for i in range(12))
    body = "# T\n" + rows
    out, note = gen._price_source_check(body, [{"url": "https://blog.example/roundup",
                                                "text": "x" * 800}], PETERMD)
    assert out == body
    assert "too many to remove safely" in note and "price table" in note


def test_the_writer_is_told_the_rule_for_every_price_not_just_a_competitors(gen):
    """The existing bullet says COMPETITOR price, so it never reached a drug's list price — which is
    exactly how four consumer blogs became the source for $1,349."""
    golden = open(os.path.join(HERE, "fixtures", "prompts", "generate_article.rich.txt"),
                  encoding="utf-8").read()
    assert "ANY PRICE = WHOEVER SETS IT" in golden
    assert "REPEATING a price it does not set is NOT a price source" in golden


# ── 6. what a price COVERS, which the ledger could never say ─────────────────────────────────────
def test_a_price_states_what_it_covers(gen):
    """`kind` describes the SHAPE of one number and says nothing about whether a second charge is
    coming. "Membership separate from the medication" could only go in `basis` — 80 characters of
    free text that asserts nothing and is verified by nothing."""
    e = gen._price_row_entry({"value": "$149", "kind": "exact", "basis": "per month",
                              "composition": "plus"}, "Ro")
    assert _format_price_value(e) == "$149 (per month, membership plus the product, billed separately)"
    e2 = gen._price_row_entry({"value": "$270", "kind": "exact", "basis": "per month",
                               "composition": "all-in"}, "X")
    assert _format_price_value(e2) == "$270 (per month, everything included)"


def test_an_unstated_composition_is_never_guessed(gen):
    """Most prices do not say, and unstated is a real answer. Inventing one would be the same class
    of defect as inventing the figure."""
    e = gen._price_row_entry({"value": "$19.99", "kind": "exact", "basis": "3-pack"}, "X")
    assert _format_price_value(e) == "$19.99 (3-pack, $6.66 each)"
    assert _composition_included(e) == ""


@pytest.mark.parametrize("typed,key", [
    ("all-in", "all-in"), ("ALL_IN", "all-in"), ("inclusive", "all-in"),
    ("program", "program"), ("programme", "program"), ("membership", "plus"),
    ("bundle", "plus"), ("product-only", "product"), ("", ""), ("nonsense", ""),
])
def test_the_composition_vocabulary_is_a_closed_set(typed, key):
    """A closed set is what makes it renderable, comparable across rows and usable to fill a column.
    Anything outside it means unstated, never a guess."""
    assert _norm_price_composition(typed) == key


def test_the_included_column_is_filled_from_the_ledger_not_the_model(gen):
    """FU247 created the "Included?" column type and had to leave the cells 100% model-written,
    because no field in the ledger answered the question the column asks."""
    body = ("# T\n\n| Provider | Monthly cost | Medication included in fee? |\n"
            "| --- | --- | --- |\n| **PeterMD** | ? | maybe |\n| **Ro** | ? | maybe |\n")
    ledger = {"PeterMD": gen._price_row_entry({"value": "$270", "kind": "exact",
                                               "basis": "per month", "composition": "all-in"}, "PeterMD"),
              "Ro": gen._price_row_entry({"value": "$149", "kind": "exact",
                                          "basis": "per month", "composition": "plus"}, "Ro")}
    gen._evidence_blocks = []
    out, written = gen._write_price_cells(body, ledger)
    assert written
    assert "| Yes |" in out
    assert "| No — billed separately |" in out
    assert "maybe" not in out


def test_a_row_with_no_stated_composition_leaves_the_column_alone(gen):
    """Code fills what the operator said and nothing else — an empty composition is not a licence
    to overwrite whatever the column held."""
    body = ("# T\n\n| Provider | Monthly cost | Included? |\n| --- | --- | --- |\n"
            "| **X** | ? | see plan |\n")
    ledger = {"X": gen._price_row_entry({"value": "$99", "kind": "exact", "basis": "per month"}, "X")}
    gen._evidence_blocks = []
    assert "see plan" in gen._write_price_cells(body, ledger)[0]


# ── 7. the free-text pricing box ─────────────────────────────────────────────────────────────────
def test_free_text_pricing_becomes_first_party_evidence():
    """The structured rows carry a figure, a basis and a composition, which covers most prices and
    none of the awkward ones. These are the operator's own words, cited to their own site."""
    blocks = BlogGenerator._pricing_notes_block(
        {"name": "PeterMD", "domain_url": "https://getpetermd.com",
         "pricing_notes": "The $270 covers medication, supplies, shipping and consults. Labs are "
                          "billed separately at cost."})
    assert len(blocks) == 1
    assert blocks[0]["url"] == "https://getpetermd.com"
    assert "Labs are billed separately at cost." in blocks[0]["text"]
    assert "authoritative" in blocks[0]["text"]


def test_a_brand_with_no_pricing_notes_changes_nothing():
    """It appends LAST, so an empty one must append nothing — otherwise every [S#] in every article
    for every brand without notes would shift."""
    for b in ({}, {"name": "X"}, {"name": "X", "pricing_notes": "   "}):
        assert BlogGenerator._pricing_notes_block(b) == []


def test_operator_entered_prices_survive_the_price_source_check(gen):
    """The point of the whole feature: a price the operator entered, cited to the page they gave,
    is not a price the source check can take away."""
    body = "# T\n\n## Cost\n\nWegovy lists at $1,349 per month without insurance [S1].\n"
    ok = [{"url": "https://www.wegovy.com/coverage-and-savings/cost-and-coverage.html",
           "text": "x" * 800}]
    assert gen._price_source_check(body, ok, PETERMD)[1] == ""


# ── 8. a rung that cannot reach a host must not be paid for twice ────────────────────────────────
def test_the_proxy_refusing_a_tunnel_is_learned_not_repaid(monkeypatch):
    """Measured on Railway: IPRoyal answers `CONNECT www.fda.gov:443` with "403 Forbidden", and the
    same for cdc.gov, consumerfinance.gov, nih.gov and pubmed — while reddit.com and an ordinary
    brand site tunnel fine. Residential providers blocklist government destinations as an abuse
    control, so it is permanent. Every .gov source was paying a full connect timeout for a rung that
    CANNOT serve it. Learned from the refusal, never from a hard-coded TLD."""
    seen = []

    def fake_get(url, headers=None, timeout=None, allow_redirects=True, proxies=None):
        seen.append(bool(proxies))
        if proxies:
            raise BE.requests.exceptions.ProxyError(
                "Unable to connect to proxy", OSError("Tunnel connection failed: 403 Forbidden"))
        return _Resp(403, "", url=url)

    monkeypatch.setattr(BE.requests, "get", fake_get)
    monkeypatch.setattr(BE.time, "sleep", lambda s: None)
    monkeypatch.setenv("REDDIT_HTTP_PROXY", "http://user:pw@geo.example:12321")
    BE.forget_walled_domains()

    assert BE._fetch_page("https://www.agency.gov/a", retries=0)[1] == "blocked"
    assert any(seen), "the first page still tries the rung — that is how the refusal is learned"
    before = len(seen)
    assert BE._fetch_page("https://www.agency.gov/b", retries=0)[1] == "blocked"
    assert not any(seen[before:]), "a later page on that host must not pay the tunnel timeout again"
    # …and a DIFFERENT host is unaffected: the refusal is per host, not a global switch-off
    n = len(seen)
    BE._fetch_page("https://shop.example/a", retries=0)
    assert any(seen[n:]), "an unrelated host still gets the residential rung"


def test_a_proxy_refusal_is_not_evidence_that_the_PAGE_is_walled():
    """The proxy said no; the site said nothing. A refusal must not change the verdict the direct
    rung reached, or a readable page behind a refused tunnel would be recorded as blocked."""
    assert BE._PROXY_REFUSED_RE.search("Unable to connect to proxy")
    assert BE._PROXY_REFUSED_RE.search("Tunnel connection failed: 403 Forbidden")
    assert not BE._PROXY_REFUSED_RE.search("HTTPSConnectionPool: Read timed out")


# ── 9. what a dry run over 214 stored articles found wrong with section 5 ────────────────────────
# The price check was replayed against every article in the production DB before it was allowed near
# a live generation. It found real mis-sourcing — PeterMD's own $149 cited to vaccinealliance.org,
# LillyDirect's $299 cited to noom.com, Ro's membership to healthline.com — and two ways it fired on
# things that were fine. Both are fixed here, and neither could have been found by reading the code.

@pytest.mark.parametrize("url,unit", [
    # a SHORT company name inside a longer domain. Requiring an exact match under four characters
    # rejected these companies' OWN sites and called their correctly-cited prices unsourced.
    ("https://stacollect.com/rates", "STA International charges 25% on US debt [S1]."),
    ("https://psicollect.com/pricing", "PSI charges 35% for attorney-referred files [S1]."),
    ("https://getopt.com/membership", "Opt Health's plans start at $245/month [S1]."),
    ("https://myhspa.org/certification", "The HSPA exam costs $125 [S1]."),
])
def test_a_short_name_still_finds_its_own_domain(gen, url, unit):
    assert BlogGenerator._domain_names_entity(url, unit)


@pytest.mark.parametrize("url,unit", [
    ("https://rocket.com/x", "Ro charges $149 per month [S1]."),        # "ro" is not "rocket"
    ("https://tipalti.com/x", "you can tip the driver $5 [S1]."),       # lowercase word, not a name
    ("https://theweekly.com/x", "the plan costs $5 [S1]."),             # a stopword is not a name
    ("https://healthline.com/x", "Ro's membership is $145/month [S1]."),
    ("https://trakkr.ai/x", "Profound's pricing runs $399-$5,000/month [S1]."),
])
def test_a_short_name_does_not_reach_a_domain_it_merely_begins(gen, url, unit):
    """The loosening is bounded: a short token must be written as a NAME, and two characters still
    has to match exactly, or the check would accept any domain starting with a common word."""
    assert not BlogGenerator._domain_names_entity(url, unit)


@pytest.mark.parametrize("unit", [
    "backed by a $1 million surety bond [S15]",
    "charges 25% on the first $5,000 collected and 20% on the balance [S16]",
    "The company raised $150 million in its Series C round [S1]",
    "the category reached $24 billion in 2024 [S1]",
    "the regulator issued a $2.3 million penalty [S1]",
])
def test_a_money_figure_that_is_not_a_price_is_not_judged_as_one(unit):
    """Both real cases sit in sentences that ALSO contain the word "fee", so the context gate let
    them through and the check reported an indemnity amount and a commission bracket as unsourced
    prices. A magnitude word after the figure is the other tell: a unit price is not quoted in
    millions."""
    assert not any(BlogGenerator._is_price_figure(unit, m) for m in _MONEY_RE.finditer(unit))


@pytest.mark.parametrize("unit", [
    "PeterMD charges $270 per month [S1]",
    "Wegovy lists at $1,349 per month at retail [S2]",
    "with a $300 minimum per file [S7]",
    "The exam costs $125 [S10]",
    "$149 first month [S2]",
    "a $50 annual renewal fee [S13]",
])
def test_a_real_price_still_reads_as_one(unit):
    """The narrowing must not become a way for a price to escape the check."""
    assert any(BlogGenerator._is_price_figure(unit, m) for m in _MONEY_RE.finditer(unit))
