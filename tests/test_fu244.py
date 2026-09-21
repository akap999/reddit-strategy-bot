"""FU244 — the article publishes CURRENT, and no claim rests on a document that predates it.

The insurance article's remaining must-fix items were all one failure wearing four coats. It
published in September 2026 and described Medicare, Medicaid and cash prices as they stood in 2024;
it missed a Medicare programme that had started three months before it published; and it cited a
2020 position statement as the source for a 2025 approval. Every figure was sourced. Every source
said what the article said. They had simply stopped being true.

No verification pass can fix the first of those, and that is the point worth being honest about: a
check cannot surface a fact the sourcing never went looking for. The (c2) official search asks who
the authority on a topic IS, and FU242 bolted "state the current status" onto it — which returns the
standing policy page, because "who is the authority" and "what changed" are different questions. So
the fix that matters here is a SEARCH, not another warning.

The two checks that follow it read the SOURCES rather than the prose, which is what makes them see
what `_staleness_check` (FU242) cannot: that check needs the article to say "as of August 2024", and
an article that just quietly states a stale figure says nothing at all.

Every fixture is medical because that is the article that exposed this. Nothing in the code is.
"""
import time

import pytest

from generators.blog_gen import BlogGenerator, _source_pub_date
from tests.stubs import StubClaude

NOW = time.strptime("2026-09-15", "%Y-%m-%d")


def _gen(search_handler=None):
    return BlogGenerator(StubClaude(search_handler=search_handler), None)


# ══ the date helper — three places a date lives, and one place it must not be invented ═════════════
@pytest.mark.parametrize("block,want", [
    ({"url": "https://kff.org/2024/12/medicaid-glp1/", "text": ""}, (2024, 12)),
    ({"url": "https://kff.org/policy-watch/x/", "text": "Published January 14, 2026 — KFF"}, (2026, 1)),
    ({"url": "https://fda.gov/x", "text": "Last updated: March 2020. Position statement."}, (2020, 3)),
    ({"url": "https://ajmc.com/view/foo-2024", "text": "no stamp"}, (2024, 12)),
    ({"url": "https://cms.gov/policy", "text": "nothing datelike at all"}, None),
    ({"url": "https://x.example/2099/05/", "text": ""}, None),          # garbage year, not a date
])
def test_the_publication_date_is_read_or_left_unknown(block, want):
    assert _source_pub_date(block, NOW) == want


def test_a_bare_year_resolves_to_december():
    """Both callers use this to ask "is the source too OLD", so an unknown month must make the
    source look as NEW as it possibly could. Erring the other way lets a missing month manufacture
    a defect."""
    assert _source_pub_date({"url": "https://x.example/reports-2025/", "text": ""}, NOW) == (2025, 12)


# ══ a claim about a year its sources predate ═══════════════════════════════════════════════════════
_BLOCKS = [
    {"label": "official · CMS", "url": "https://cms.gov/2026/07/bridge",
     "text": "Published July 2026. The programme starts July 1, 2026."},
    {"label": "official · FDA statement", "url": "https://fda.gov/statement",
     "text": "Last updated: March 2020. Position statement."},
    {"label": "third-party · KFF", "url": "https://kff.org/2024/12/medicaid/",
     "text": "Published December 2024. 13 state Medicaid programs cover these drugs."},
]
_SOURCES = """
## Sources

- [S1] official · CMS — https://cms.gov/2026/07/bridge
- [S2] official · FDA statement — https://fda.gov/statement
- [S3] third-party · KFF — https://kff.org/2024/12/medicaid/
"""


def _body(prose):
    return "# Does insurance cover this?\n\n" + prose + "\n" + _SOURCES


def test_a_2025_claim_cited_to_a_2020_statement_is_flagged():
    note = _gen()._claim_date_check(
        _body("The agency approved the first treatment for this in 2025 [S2]."), _BLOCKS, now=NOW)
    assert note.startswith("claim-date:")
    assert "about 2025" in note and "stop at 2020" in note
    assert "[S1]" in note                     # and it names what in the evidence could carry it


def test_a_source_from_the_same_year_is_fine():
    note = _gen()._claim_date_check(
        _body("The programme began in 2026 [S1]."), _BLOCKS, now=NOW)
    assert note == ""


def test_a_future_dated_claim_is_never_flagged():
    """A programme running through 2027 is legitimately announced by a 2026 page. Flagging that
    would make the check noise, and noise is how a check gets ignored."""
    note = _gen()._claim_date_check(
        _body("The programme runs through 2027 and ends December 31, 2027 [S1]."), _BLOCKS, now=NOW)
    assert note == ""


def test_one_source_new_enough_is_enough():
    note = _gen()._claim_date_check(
        _body("The agency approved this in 2025 [S1][S2]."), _BLOCKS, now=NOW)
    assert note == ""


def test_an_undated_source_is_skipped_not_accused():
    blocks = _BLOCKS[:1] + [{"label": "official · X", "url": "https://x.example/policy", "text": "no date"}]
    body = ("# t\n\nThe agency approved this in 2025 [S2].\n\n## Sources\n\n"
            "- [S1] official · CMS — https://cms.gov/2026/07/bridge\n"
            "- [S2] official · X — https://x.example/policy\n")
    assert _gen()._claim_date_check(body, blocks, now=NOW) == ""


def test_a_body_with_no_citations_is_silent():
    assert _gen()._claim_date_check(_body("The agency approved this in 2025."), _BLOCKS, now=NOW) == ""


# ══ a current-state figure whose every source is a year old ════════════════════════════════════════
def test_a_current_coverage_figure_on_a_year_old_source_is_flagged():
    note = _gen()._stale_source_check(
        _body("Thirteen state Medicaid programs currently cover these drugs, and 13 states pay for "
              "them [S3]."), _BLOCKS, now=NOW)
    assert note.startswith("stale-source:")
    assert "21 months old" in note


def test_a_current_price_on_a_year_old_source_is_flagged():
    note = _gen()._stale_source_check(
        _body("The cash price is $149 per month [S3]."), _BLOCKS, now=NOW)
    assert note.startswith("stale-source:")


def test_a_dated_historical_fact_on_an_old_source_is_silent():
    """An old page is the RIGHT source for an old fact. Only a claim about the state of the world
    NOW can be made false by the calendar."""
    note = _gen()._stale_source_check(
        _body("In 2024, 13 state Medicaid programs added coverage [S3]."), _BLOCKS, now=NOW)
    assert note == ""


def test_a_qualitative_current_claim_with_no_figure_is_silent():
    note = _gen()._stale_source_check(
        _body("Medicaid coverage currently varies by state [S3]."), _BLOCKS, now=NOW)
    assert note == ""


def test_a_current_figure_on_a_recent_source_is_silent():
    note = _gen()._stale_source_check(
        _body("The copay is currently $50 per month [S1]."), _BLOCKS, now=NOW)
    assert note == ""


def test_the_source_text_is_put_back_before_the_date_is_read():
    """`_blocks_from_sources` rebuilds the [S#] map from the rendered Sources list, which carries a
    label and a URL and no text — and the FDA statement's only date is in its text. Without the
    back-fill both checks go quiet on exactly the source that motivated them."""
    g = _gen()
    mapped = g._dated_blocks(_body("x [S2]."), _BLOCKS)
    assert "March 2020" in (mapped[1].get("text") or "")


def test_a_page_the_probe_read_supplies_the_date():
    """Better still than the search summary: the page FU241 already fetched, where the stamp
    actually lives."""
    g = _gen()
    g._claim_pages = {"https://cms.gov/policy": ("Last updated: February 2019. Standing policy.", "direct")}
    blocks = [{"label": "official · CMS", "url": "https://cms.gov/policy", "text": "one-line summary"}]
    body = "# t\n\nThe rule changed in 2024 [S1].\n\n## Sources\n\n- [S1] official · CMS — https://cms.gov/policy\n"
    assert "stop at 2019" in g._claim_date_check(body, blocks, now=NOW)


# ══ the recency sweep — the only change that can find a programme nothing gathered mentions ════════
@pytest.mark.parametrize("core,seed,ymyl,pricing,want", [
    ("Medicare drug coverage", "does insurance cover this", "medical", False, True),
    ("baby bottles", "best baby bottles", None, True, True),
    ("contractor licensing rules", "do I need a permit", None, False, True),   # dated, no ymyl/price
    ("seasoning a cast iron pan", "how to season a pan", None, False, False),  # evergreen
])
def test_the_sweep_runs_only_where_the_answer_has_a_date(core, seed, ymyl, pricing, want):
    assert BlogGenerator._wants_recency(core, seed, ymyl, pricing) is want


def test_the_sweep_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("BLOG_RECENCY_SWEEP", "0")
    assert BlogGenerator._wants_recency("coverage rules", "x", "medical", True) is False


def _sweep(results, ymyl="medical"):
    seen = {}

    def handler(brief, allowed, blocked):
        seen["brief"] = brief
        return results
    g = _gen(handler)
    out = g._gather_recent_changes("Medicare GLP-1 coverage", "does insurance cover semaglutide",
                                   ymyl, {"name": "PeterMD", "domain_url": "https://getpetermd.com"},
                                   now=NOW)
    return out, seen


def test_the_sweep_asks_what_changed_not_who_the_authority_is():
    _out, seen = _sweep([])
    brief = seen["brief"]
    assert "what CHANGED" in brief and "took effect on or after 2025" in brief
    assert "REPLACED or superseded" in brief and "EFFECTIVE DATE" in brief
    # the distinction that makes it worth a second search at all
    assert "only restate the older position" in brief


def test_a_newer_official_programme_is_kept():
    out, _seen = _sweep([{"title": "Medicare GLP-1 Bridge", "url": "https://cms.gov/bridge",
                          "fact": "Effective July 1 2026, a $50 copay."}])
    assert len(out) == 1
    assert out[0]["label"].startswith("official ·") and out[0]["url"] == "https://cms.gov/bridge"


def test_being_recent_does_not_earn_an_official_badge_on_a_ymyl_page():
    out, _seen = _sweep([{"title": "Best GLP-1 deals 2026 review", "url": "https://dealsblog.example/x",
                          "fact": "cheap"}])
    assert out == []


def test_a_non_ymyl_page_keeps_a_recent_third_party_source():
    out, _seen = _sweep([{"title": "2026 pricing roundup", "url": "https://news.example/2026/pricing",
                          "fact": "prices rose"}], ymyl=None)
    assert len(out) == 1 and not out[0]["label"].startswith("official ·")


def test_the_sweep_is_capped():
    out, _seen = _sweep([{"title": f"Update {i}", "url": f"https://cms.gov/u{i}", "fact": "x"}
                         for i in range(8)])
    assert len(out) == BlogGenerator._RECENCY_MAX_BLOCKS


def test_the_sweep_never_raises_and_never_blocks_a_generation():
    def boom(brief, allowed, blocked):
        raise RuntimeError("search is down")
    g = _gen(boom)
    assert g._gather_recent_changes("x", "y", "medical", {"name": "B"}, now=NOW) == []


def test_the_sweep_is_wired_into_sourcing():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    assert "_gather_recent_changes(core_topic, seed, ymyl, brand" in src
    assert "self._wants_recency(core_topic, seed, ymyl, _px)" in src


# ══ the prompt rules the checks are the backstop for ═══════════════════════════════════════════════
def test_the_writer_and_reconcile_prompts_carry_the_recency_rules():
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    assert "STATE THE CURRENT POSITION, AND SAY WHEN IT IS FROM" in src
    assert "CURRENT OVER OLD" in src
    assert "cannot be the source for a 2025 approval" in src


@pytest.mark.parametrize("term", ["semaglutide", "medicare", "medicaid", "glp", "insurance",
                                  "drug", "wegovy", "pharmacy"])
def test_no_vertical_word_is_hard_coded_in_the_new_code(term):
    import re as _re
    src = open("generators/blog_gen.py", encoding="utf-8").read()
    start = src.index("def _gather_recent_changes")
    end = src.index("def _source_for_completion")
    block = _re.sub(r"(?m)#.*$", "", _re.sub(r'"""(?:.|\n)*?"""', "", src[start:end]))
    assert term not in block.lower()


def test_an_as_of_claim_still_fires_even_though_it_carries_a_date():
    """"As of August 2024" is a statement about NOW wearing a date, which is exactly the shape that
    goes stale — it must not be filtered out with the genuine history."""
    note = _gen()._stale_source_check(
        _body("As of August 2024, 13 state Medicaid programs cover these drugs [S3]."),
        _BLOCKS, now=NOW)
    assert note.startswith("stale-source:")


def test_being_recent_does_not_excuse_a_source_from_the_normal_filters():
    """The regression this caused on its way in: a "what changed" search returns whatever the index
    has, including last year's off-subject ranking table, and the sweep kept it as an official
    source because it had its own accept list. There is one accept policy, and the sweep uses it."""
    seen = {}

    def handler(brief, allowed, blocked):
        seen["brief"] = brief
        return [{"title": "Irwin Mitchell, Personal Injury: Mainly Claimant | Chambers UK Profile",
                 "url": "https://chambers.com/rankings/irwin-mitchell-personal-injury-uk-1:144",
                 "fact": "Ranked for personal injury."}]
    out = _gen(handler)._gather_recent_changes(
        "international family law", "which are the best UK law firms for international family law",
        "legal", {"name": "Osbornes Law", "domain_url": "https://osborneslaw.com"},
        now=NOW, subj_toks=["international", "family", "law"], biz_toks=["law", "solicitors"])
    assert out == []


# ══ the wrong-source half of the same review, already covered — locked so it stays that way ════════
def test_a_figure_cited_to_the_wrong_gathered_source_is_re_pointed():
    """The review's other citation defect: an FAQ citing the guideline for "13 state Medicaid
    programs" when a different gathered source is the one that states it. That needs no new check —
    `_claim_source_check` re-points it, and FU242's counted-noun atom is what lets it see "13 state
    programs" as a figure at all. This test exists so a later edit to either cannot quietly undo it."""
    long = "x " * 400
    blocks = [
        {"label": "official · guideline", "url": "https://endocrine.example/g",
         "text": "Pharmacological management of obesity. " + long},
        {"label": "third-party · policy brief", "url": "https://healthaffairs.example/f",
         "text": "As of August 2024, 13 state Medicaid programs cover these drugs. " + long},
    ]
    body = ("# t\n\nThirteen states cover it: 13 state Medicaid programs have chosen to cover "
            "them [S1].\n\n## Sources\n\n"
            "- [S1] official · guideline — https://endocrine.example/g\n"
            "- [S2] third-party · policy brief — https://healthaffairs.example/f\n")
    out, note = _gen()._claim_source_check(body, blocks, {"name": "PeterMD",
                                                          "domain_url": "https://getpetermd.com"})
    assert "re-pointed [S1]" in note
    assert "cover them [S2]." in out
