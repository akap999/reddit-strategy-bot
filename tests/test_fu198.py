"""FU198 — competitor facts were fetched for the brand's whole CATEGORY, not the article's SUBJECT.

An Osbornes Law blog on international family law shipped a comparison saying Irwin Mitchell does Court
of Protection and personal injury, Farrer & Co does M&A on £10m-£100m deals, and Taylor Wessing does
venture capital in Germany: 14 of its 31 sources were rankings in unrelated practice areas, and two
were the Chambers *Student* Guide, a graduate-recruitment resource. The page never established that
three of its five firms practise the subject at all.

Cause: the brand's stored category is "full-service London solicitors for individuals and businesses",
every competitor brief was anchored on it, and the reconcile then demanded every cell be filled and
dropped any row that could not be. A model holding an off-topic ranking had no legal move but to use
it. This round scopes the briefs to the subject, refuses an off-subject credential as evidence, lets a
cell say plainly that nothing was found, and requires the page to be led by the subject's mechanics.

$0, no network (StubClaude).
"""
import re

import pytest

from generators.blog_gen import (BlogGenerator, _is_non_capability_source,
                                 _is_offtopic_credential, _product_tokens)
from tests.stubs import StubClaude

SUBJECT = "international family law"
BROAD = {"name": "Osbornes Law", "domain_url": "https://osborneslaw.com",
         "category": "full-service London solicitors for individuals and businesses"}
NARROW = dict(BROAD, category="international family law solicitors")
SEED = "which are the best UK law firms for international family law"

OFFTOPIC = {"title": "Irwin Mitchell, Personal Injury: Mainly Claimant | Chambers UK Profile",
            "url": "https://chambers.com/department/irwin-mitchell-personal-injury-uk-1:144",
            "fact": "Ranked Band 1 for personal injury."}
ONTOPIC = {"title": "Irwin Mitchell - Family: Divorce and Financial Remedy | Legal 500",
           "url": "https://www.legal500.com/rankings/c-london/family-divorce/1708-irwin-mitchell",
           "fact": "Great strength in depth across the broad spectrum of family law work."}
STUDENT = {"title": "Irwin Mitchell - True Picture | Chambers Student Guide",
           "url": "https://www.chambersstudent.co.uk/irwin-mitchell/true-picture/209/1",
           "fact": "What trainees say about the seat."}
WIKI = {"title": "Farrer & Co - Wikipedia", "url": "https://en.wikipedia.org/wiki/Farrer_%26_Co",
        "fact": "A London law firm."}


def _source(brand, subject=SUBJECT, tools=("Irwin Mitchell",), results=(), mechanics=()):
    """Drive the real competitor-sourcing pass; returns (gen, sourcing). Mirrors tests/test_fu161."""
    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": list(tools), "peer_tools": list(tools),
                    "dimensions": ["key rankings"], "products": [], "claims": [],
                    "core_topic": "", "subject": subject, "core_mechanics": list(mechanics)}
        if '"domains"' in p:
            return {"domains": {t: t.lower().replace(" ", "") + ".com" for t in tools}}
        return {}
    gen = BlogGenerator(StubClaude(call_handler=call_h,
                                   search_handler=lambda b, a, bl: list(results)), db=None)
    body = "| Firm | Key Rankings |\n|---|---|\n" + "".join(f"| {t} | ? |\n" for t in tools)
    return gen, gen._source_for_completion(brand, SEED, {"body_markdown": body}, ymyl=None)


def _briefs(gen):
    return [s["brief"] for s in gen.claude.searches] + \
           [c["brief"] for c in gen.claude.site_fact_calls]


# ── INERT PROOF first: this is what protects the fourteen existing brief-assertion suites ─────
def test_a_brand_whose_category_covers_the_subject_gets_no_new_clause():
    gen, _ = _source(NARROW)
    assert not any("specifically their" in b for b in _briefs(gen))


def test_no_subject_extracted_means_no_new_clause():
    gen, _ = _source(BROAD, subject="")
    assert not any("specifically their" in b for b in _briefs(gen))


# ── the reported case, locked ────────────────────────────────────────────────────────────────
def test_a_broad_category_brand_scopes_its_competitor_briefs_to_the_subject():
    gen, _ = _source(BROAD)
    scoped = [b for b in _briefs(gen) if SUBJECT in b]
    assert scoped, "no competitor brief mentioned the article's subject"
    assert any("specifically their" in b for b in scoped)


def test_an_offsubject_credential_is_refused_and_the_ontopic_one_is_kept():
    gen, sourcing = _source(BROAD, results=[OFFTOPIC, ONTOPIC])
    urls = " ".join(b.get("url", "") for b in sourcing["fresh"])
    assert "family-divorce" in urls, "the family-law ranking should have been kept"
    assert "personal-injury" not in urls, "a personal-injury ranking is not evidence here"


def test_recruitment_and_encyclopedia_sources_never_become_evidence():
    gen, sourcing = _source(BROAD, results=[STUDENT, WIKI, ONTOPIC])
    urls = " ".join(b.get("url", "") for b in sourcing["fresh"])
    assert "chambersstudent" not in urls and "wikipedia" not in urls
    assert "family-divorce" in urls


# ── the filters in isolation ─────────────────────────────────────────────────────────────────
def test_non_capability_source_helper():
    assert _is_non_capability_source(STUDENT)
    assert _is_non_capability_source(WIKI)
    assert not _is_non_capability_source(ONTOPIC)
    assert not _is_non_capability_source({})


def test_offtopic_credential_helper_is_inert_without_a_subject():
    toks = _product_tokens(SUBJECT)
    assert _is_offtopic_credential(OFFTOPIC, toks)
    assert not _is_offtopic_credential(ONTOPIC, toks)
    assert not _is_offtopic_credential(OFFTOPIC, []), "must be inert when no subject was derived"


def test_a_non_credential_page_is_never_dropped_as_offtopic():
    """Only RANKING-shaped pages are judged on subject match — ordinary pages pass through."""
    toks = _product_tokens(SUBJECT)
    assert not _is_offtopic_credential(
        {"title": "About our London office", "url": "https://x.com/about", "fact": "We opened in 1988."},
        toks)


# ── CROSS-VERTICAL: the standing genericity gate — nothing may key on law ─────────────────────
@pytest.mark.parametrize("subject,cred", [
    ("payroll integration",
     {"title": "Acme HR — Recruiting Suite, Tier 1 | G2 Grid", "url": "https://g2.com/acme/recruiting",
      "fact": "Ranked tier 1 for recruiting."}),
    ("bathroom remodeling",
     {"title": "BuildCo — Roofing Contractor of the Year Award 2026",
      "url": "https://trades.example/awards/roofing", "fact": "Award winner."}),
])
def test_an_offline_product_line_credential_is_refused_in_any_vertical(subject, cred):
    assert _is_offtopic_credential(cred, _product_tokens(subject))


def test_the_new_helpers_carry_no_vertical_term():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("_CREDENTIAL_SHAPE_RE")
    block = src[i:src.index("def _product_tokens", i)]
    for term in ("law", "legal", "solicitor", "medical", "clinic", "drug", "saas"):
        assert not re.search(r"\b%s\b" % term, block, re.I), f"'{term}' hardcoded in the FU198 helpers"


# ── Change 3: the honest cell survives the punt machinery (regression guard on those regexes) ─
SENT = "No international family law ranking found in public sources"


def _tbl(cell):
    return ("| Firm | Key Rankings | Scope |\n|---|---|---|\n"
            "| **Osbornes** | Chambers, Legal 500 [S1] | Europe [S2] |\n"
            f"| Farrer | {cell} | London [S3] |\n"
            f"| Taylor Wessing | {cell} | 16 offices [S4] |\n")


def test_the_honest_cell_survives_and_keeps_its_column():
    gen = BlogGenerator(StubClaude(), db=None)
    out = gen._resolve_table_punts(_tbl(SENT))
    assert "Key Rankings" in out, "the column must not be dropped"
    assert out.count(SENT) == 2
    assert gen._scrub_punts(out).count(SENT) == 2
    assert "no subject-specific fact" in (gen._table_punt_note or "")


def test_the_banned_phrasing_is_still_stripped_and_still_drops_the_column():
    """The carve-out must not become a way to smuggle an ordinary punt back in."""
    gen = BlogGenerator(StubClaude(), db=None)
    out = gen._resolve_table_punts(_tbl("Not specified in sourced facts"))
    assert "Not specified" not in out
    assert "Key Rankings" not in out


def test_the_reconcile_tells_the_model_to_use_the_honest_cell():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    i = src.index("def _reconcile_and_finish")
    block = src[i:src.index("\n    def ", i + 10)]
    assert "RELEVANCE BEFORE COMPLETENESS" in block
    assert "found in public sources" in block
    assert "COUNTS AS FILLED" in block


# ── Change 4: the page must be led by the subject's mechanics ─────────────────────────────────
def test_the_article_prompt_demands_mechanics_over_credentials():
    gen = BlogGenerator(StubClaude(call_handler=lambda p: {
        "title": SEED, "meta_description": "m", "keywords": [],
        "body_markdown": "## Quick answer\nx", "disclosure": "d"}), db=None)
    gen.generate_article(BROAD, SEED)
    p = gen.claude.calls[0]
    assert "SUBSTANCE BEFORE CREDENTIALS" in p
    assert "COMMODITY" in p and "never stand in PLACE of a mechanic" in p.replace("NEVER", "never")
    # vertical-neutral: the block must not name a single industry's mechanics as the rule
    i = p.index("SUBSTANCE BEFORE CREDENTIALS")
    block = p[i:p.index("GEOGRAPHY / QUALIFIER DIFFERENTIATION", i)]
    for term in ("family law", "solicitor", "tirzepatide", "HRIS"):
        assert term.lower() not in block.lower()


def test_the_extraction_asks_for_the_subject_and_its_mechanics():
    gen, _ = _source(BROAD)
    claim = next(p for p in gen.claude.calls if '"peer_tools"' in p)
    assert "SUBJECT (FU198)" in claim and "CORE_MECHANICS" in claim
    assert '"subject": ""' in claim and '"core_mechanics"' in claim


def _finalize(body, mechanics):
    gen = BlogGenerator(StubClaude(), db=None)
    gen._core_mechanics = list(mechanics)
    art = {"title": "T", "meta_description": "m", "keywords": [], "body_markdown": body}
    return gen._finalize_article(BROAD, SEED, art, body)


MECHS = ["forum shopping and the race to issue", "child relocation applications",
         "enforcement of foreign orders"]


def test_a_credential_led_page_is_flagged():
    body = ("# T\n\n## Rankings\n\nRanked by Chambers and Legal 500, with offices in 20 locations "
            "and over 200 countries of reach.\n")
    assert "mechanics-check" in (_finalize(body, MECHS).get("geo_warning") or "")


def test_a_page_that_covers_the_mechanics_is_silent():
    body = ("# T\n\n## Jurisdiction\n\nForum shopping decides the outcome: the race to issue means "
            "filing first can fix the forum. Relocation applications turn on the child's welfare. "
            "Enforcement of foreign orders is not automatic.\n")
    assert "mechanics-check" not in (_finalize(body, MECHS).get("geo_warning") or "")


def test_no_extracted_mechanics_means_no_check():
    assert "mechanics-check" not in (_finalize("# T\n\nAnything.\n", []).get("geo_warning") or "")
