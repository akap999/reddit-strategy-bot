"""FU208 — verify a finished blog: a scrutiny analysis, the operator's approval (with their input), and a
SEPARATE verified version.

The old Verify could not do this job: it never checked a fact against its source, it auto-applied with
no approval, and its gate refused any fix that changed a number — which is exactly what "the price is
$74, not $145" is. These tests hold the new flow to the promises that make it safe to use on a live,
published blog:

  * the original `body_markdown` is byte-identical after an analysis AND after an apply;
  * every cited page is fetched once, and a search is spent only where the pages did not settle it;
  * an APPROVED value change passes the gate, and a different number changing in the same sentence
    is still refused;
  * every occurrence (table, prose, FAQ, meta) is carried and corrected;
  * the brand's canonical value is saved ADDITIVELY — every other operator-set price survives.

$0, no network: Claude is a scripted StubClaude, page fetches are stubbed.
"""
import json
import os
import tempfile

import pytest

import generators.blog_gen as bgmod
from db import Database
from generators.blog_gen import BlogGenerator as BG, upsert_canonical_value
from tests.stubs import StubClaude

SEED = "which telehealth platforms offer glp-1"
BODY = f"""# {SEED}

*[Add author byline before publishing]*

## Quick answer

Acme is a strong pick, and Ro charges $145/month for membership [S2].

## Comparison

| Platform | Monthly price | Notes |
| --- | --- | --- |
| Acme | $149/month [S1] | doctor-led |
| Ro | $145/month [S2] | ships in 14 days |

## FAQ

### How much does Ro cost?

Ro's membership costs $145/month [S2], with a $20 setup fee.

## Sources

- [S1] Acme — <https://acme.com/pricing>
- [S2] Ro — <https://ro.co/pricing>
"""
META = "Compare GLP-1 telehealth: Acme from $149/month, Ro $145/month."
PAGES = {"https://acme.com/pricing": "Acme plans. Acme membership is $149/month, doctor-led.",
         "https://ro.co/pricing": "Ro pricing. Membership: $74/month. Cancel anytime."}
TABLE_RO = "| Ro | $145/month [S2] | ships in 14 days |"
QUICK = "Acme is a strong pick, and Ro charges $145/month for membership [S2]."
FAQ = "Ro's membership costs $145/month [S2], with a $20 setup fee."


def _brand(**kw):
    b = {"id": 1, "name": "Acme", "domain_url": "https://acme.com",
         "competitor_domains": json.dumps({"Ro": "ro.co"})}
    b.update(kw)
    return b


def _claims(include_faq=False, extra=None):
    ro_occ = [{"where": "table", "quote": TABLE_RO}, {"where": "quick answer", "quote": QUICK}]
    if include_faq:
        ro_occ.append({"where": "faq", "quote": FAQ})
    claims = [
        {"claim": "Ro membership costs $145/month", "kind": "price", "entity": "Ro", "product": "membership",
         "value": "$145/month", "cited": ["S2"], "occurrences": ro_occ},
        {"claim": "Acme costs $149/month", "kind": "price", "entity": "Acme", "product": "",
         "value": "$149/month", "cited": ["S1"],
         "occurrences": [{"where": "table", "quote": "| Acme | $149/month [S1] | doctor-led |"}]},
    ]
    return claims + list(extra or [])


class Script:
    """Scripted Claude responses keyed on each prompt's opening words; records what was asked."""

    def __init__(self, claims=None, notes=None, verdicts=None, items=None, drafts=None, defects=None):
        self.claims = claims if claims is not None else _claims()
        self.notes = notes or []
        self.verdicts = verdicts if verdicts is not None else [
            {"ref": "c1", "status": "contradicted", "page": "P1", "page_says": "Membership: $74/month",
             "page_value": "$74/month"},
            {"ref": "c2", "status": "confirmed", "page": "P2", "page_says": "Acme membership is $149/month",
             "page_value": "$149/month"}]
        self.items = items if items is not None else [RO_ITEM]
        self.drafts = drafts or []
        self.defects = defects or []
        self.prompts = {}

    def __call__(self, p):
        for key in ("You are auditing a FINISHED", "Check each statement", "You are proof-checking",
                    "You are the fact-checker", "You are applying corrections"):
            if p.startswith(key):
                self.prompts.setdefault(key, []).append(p)
        if p.startswith("You are auditing a FINISHED"):
            return {"claims": self.claims, "notes": self.notes}
        if p.startswith("Check each statement"):
            return {"verdicts": self.verdicts}
        if p.startswith("You are proof-checking"):
            return {"issues": self.defects, "assessment": {"score": 80}}
        if p.startswith("You are the fact-checker"):
            return {"items": self.items}
        if p.startswith("You are applying corrections"):
            return {"items": self.drafts}
        return {}


RO_ITEM = {
    "ref": "c1", "problem": "Ro's membership price is out of date.", "action": "correct",
    "old_value": "$145/month", "new_value": "$74/month",
    "fixes": [{"quote": TABLE_RO, "fix": "| Ro | $74/month [S2] | ships in 14 days |"},
              {"quote": QUICK, "fix": "Acme is a strong pick, and Ro charges $74/month for membership [S2]."},
              {"quote": FAQ, "fix": "Ro's membership costs $74/month [S2], with a $20 setup fee."},
              {"quote": META, "fix": "Compare GLP-1 telehealth: Acme from $149/month, Ro $74/month."}],
    "basis": {"type": "cited page", "url": "https://ro.co/pricing", "excerpt": "Membership: $74/month"},
    "confidence": "high", "conflict": ""}


def _gen(script, fetched=None, pages=None):
    claude = StubClaude(call_handler=script)
    gen = BG(claude, None)
    pg = PAGES if pages is None else pages

    def _fetch(u):
        if fetched is not None:
            fetched.append(u)
        return pg.get(u, "")
    gen._fetch_url = _fetch
    return gen, claude


# ═══════════════════════════════════════ analysis ═══════════════════════════════════════════════
def test_each_cited_page_is_fetched_once_and_confirmed_claims_cost_no_search():
    fetched = []
    script = Script()
    gen, claude = _gen(script, fetched)
    s = gen.verify_analyze(_brand(), BODY, META)
    assert sorted(fetched) == sorted(PAGES), "every cited page read, and each exactly once"
    # c1 was contradicted by its page, c2 confirmed by its page: neither needs the web
    assert claude.searches == []
    assert s["stats"]["confirmed"] == 1 and s["stats"]["searches"] == 0
    assert [i["ref"] for i in s["items"]] == ["c1"]


def test_a_second_claim_citing_the_same_page_does_not_refetch_it():
    fetched = []
    extra = [{"claim": "Ro lets you cancel anytime", "kind": "capability", "entity": "Ro", "value": "",
              "cited": ["S2"], "occurrences": [{"where": "table", "quote": TABLE_RO}]}]
    gen, _ = _gen(Script(claims=_claims(extra=extra)), fetched)
    gen.verify_analyze(_brand(), BODY, META)
    assert fetched.count("https://ro.co/pricing") == 1


def test_a_recheck_reuses_the_pages_already_fetched():
    fetched = []
    gen, _ = _gen(Script(), fetched)
    first = gen.verify_analyze(_brand(), BODY, META)
    fetched.clear()
    second = gen.verify_analyze(_brand(), BODY, META, prior=first)
    assert fetched == [], "a re-check must not refetch pages it already read"
    assert second["stats"]["pages_reused"] == 2 and second["round"] == 2


def test_searches_go_only_to_unconfirmed_claims_and_notes_without_a_value():
    verdicts = [{"ref": "c1", "status": "not_on_page", "page": "P1", "page_says": "", "page_value": ""},
                {"ref": "c2", "status": "confirmed", "page": "P2", "page_says": "Acme membership is $149/month"}]
    notes = [{"note": 0, "entity": "Ro", "value": "$145/month", "cited": ["S2"],
              "occurrences": [{"where": "table", "quote": TABLE_RO}]},
             {"note": 1, "entity": "Acme", "value": "", "cited": [],
              "occurrences": [{"where": "table", "quote": "| Acme | $149/month [S1] | doctor-led |"}]}]
    script = Script(verdicts=verdicts, notes=notes, items=[])
    gen, claude = _gen(script)
    gen.verify_analyze(_brand(), BODY, META, notes=[
        {"text": "Ro's price is wrong", "value": "$74/month"},          # has a value → no search
        {"text": "Acme doesn't offer a doctor-led plan any more"}])     # no value → searched
    briefs = [x["brief"] for x in claude.searches]
    assert any("An editor says" in b and "doctor-led" in b for b in briefs), briefs
    assert not any("Ro's price is wrong" in b for b in briefs), "a note WITH a value must not be searched"
    assert any('Verify this claim about Ro' in b for b in briefs), "the unconfirmed claim is searched"
    assert not any('Acme costs $149' in b for b in briefs), "a confirmed claim must not be searched"


def test_a_subject_fact_is_searched_first_party_only():
    extra = [{"claim": "Acme is available in all 50 states", "kind": "capability", "entity": "Acme",
              "value": "", "cited": [], "occurrences": [{"where": "table", "quote": "| Acme | $149/month [S1] | doctor-led |"}]}]
    gen, claude = _gen(Script(claims=_claims(extra=extra), items=[]))
    gen.verify_analyze(_brand(), BODY, META)
    subj = [x for x in claude.searches if "all 50 states" in x["brief"]]
    assert subj and all(x["allowed"] == ["acme.com"] and x["first_party"] for x in subj)
    assert len(subj) == 1, "a subject fact never falls back to a broad search"


def test_a_competitor_price_prefers_the_vendors_own_site():
    verdicts = [{"ref": "c1", "status": "not_on_page", "page": "P1"},
                {"ref": "c2", "status": "confirmed", "page": "P2", "page_says": "Acme membership is $149/month"}]
    gen, claude = _gen(Script(verdicts=verdicts, items=[]))
    gen.verify_analyze(_brand(), BODY, META)
    ro = [x for x in claude.searches if "about Ro" in x["brief"]]
    assert ro[0]["allowed"] == ["ro.co"] and ro[0]["first_party"]


def test_the_search_limit_and_the_cost_ceiling_are_both_reported(monkeypatch):
    monkeypatch.setattr(bgmod, "_VX_MAX_SEARCHES", 1)
    extra = [{"claim": f"Ro fact {k}", "kind": "capability", "entity": "Ro", "value": "", "cited": [],
              "occurrences": [{"where": "table", "quote": TABLE_RO}]} for k in range(3)]
    verdicts = [{"ref": "c2", "status": "confirmed", "page": "P2", "page_says": "Acme membership is $149/month"},
                {"ref": "c1", "status": "contradicted", "page": "P1", "page_says": "Membership: $74/month"}]
    gen, claude = _gen(Script(claims=_claims(extra=extra), verdicts=verdicts, items=[]))
    claude._skipped = 2
    s = gen.verify_analyze(_brand(), BODY, META)
    assert s["stats"]["searches"] == 1 and s["stats"]["search_capped"] == 2
    assert s["stats"]["skipped_searches"] == 2


def test_an_item_carries_every_occurrence_table_prose_faq_and_meta():
    """The model listed only the table and the Quick answer; the deterministic backstop adds the FAQ
    sentence and the meta description, because both carry the same value about the same brand."""
    gen, _ = _gen(Script(items=[dict(RO_ITEM, fixes=RO_ITEM["fixes"][:2])]))
    s = gen.verify_analyze(_brand(), BODY, META)
    where = sorted(o["where"] for o in s["items"][0]["occurrences"])
    assert where == ["faq", "meta", "quick answer", "table"]
    assert sorted(s["items"][0]["uncovered"]) == sorted([FAQ, META])


def test_an_invented_page_quote_cannot_drive_a_correction():
    verdicts = [{"ref": "c1", "status": "contradicted", "page": "P1", "page_says": "Membership: $9/month"},
                {"ref": "c2", "status": "confirmed", "page": "P2", "page_says": "Acme membership is $149/month"}]
    gen, claude = _gen(Script(verdicts=verdicts, items=[]))
    gen.verify_analyze(_brand(), BODY, META)
    assert any("about Ro" in x["brief"] for x in claude.searches), \
        "a 'contradicted' verdict whose quote is not on the page must be treated as unsettled"


def test_scrutiny_both_ways_a_note_contradicted_by_its_source_shows_the_conflict():
    notes = [{"note": 0, "entity": "Ro", "value": "$145/month", "cited": ["S2"],
              "occurrences": [{"where": "table", "quote": TABLE_RO}]}]
    verdicts = [{"ref": "n0", "status": "contradicted", "page": "P1", "page_says": "Membership: $74/month"},
                {"ref": "c1", "status": "contradicted", "page": "P1", "page_says": "Membership: $74/month"},
                {"ref": "c2", "status": "confirmed", "page": "P2", "page_says": "Acme membership is $149/month"}]
    item = {"ref": "n0", "problem": "Ro's price", "action": "correct", "old_value": "$145/month",
            "new_value": "$99/month", "fixes": [{"quote": TABLE_RO, "fix": "| Ro | $99/month [S2] | ships in 14 days |"}],
            "basis": {"type": "your note", "url": "", "excerpt": ""}, "confidence": "medium",
            "conflict": "You say $99/month but ro.co/pricing says $74/month."}
    script = Script(notes=notes, verdicts=verdicts, items=[item])
    gen, _ = _gen(script)
    s = gen.verify_analyze(_brand(), BODY, META, notes=[{"text": "Ro's price is wrong", "value": "$99/month"}])
    synth = script.prompts["You are the fact-checker"][0]
    assert "SCRUTINY BOTH WAYS" in synth and "$99/month" in synth and "Membership: $74/month" in synth
    it = next(i for i in s["items"] if i["ref"] == "n0")
    assert it["conflict"] and it["origin"] == "your note"
    assert it["decision"]["approved"] is False, "a conflicted note must not be pre-approved"


def test_an_unconfirmed_claim_offers_keep_soften_and_remove():
    extra = [{"claim": "Ro ships in 14 days", "kind": "capability", "entity": "Ro", "value": "14 days",
              "cited": [], "occurrences": [{"where": "table", "quote": TABLE_RO}]}]
    soft = {"ref": "c3", "problem": "Shipping time could not be confirmed.", "action": "soften",
            "old_value": "14 days", "new_value": "",
            "fixes": [{"quote": TABLE_RO, "fix": "| Ro | $145/month [S2] | ships nationwide |"}],
            "alternatives": {"remove": [{"quote": TABLE_RO, "fix": "| Ro | $145/month [S2] | |"}]},
            "basis": {"type": "web source"}, "confidence": "low", "conflict": ""}
    gen, _ = _gen(Script(claims=_claims(extra=extra), items=[RO_ITEM, soft]))
    s = gen.verify_analyze(_brand(), BODY, META)
    it = next(i for i in s["items"] if i["ref"] == "c3")
    assert it["proposal"]["action"] == "soften" and "remove" in it["proposal"]["alternatives"]
    assert it["decision"]["approved"] is False


def test_a_model_quote_not_in_the_article_is_dropped():
    bad = dict(RO_ITEM, fixes=[{"quote": "Ro charges $145 per month.", "fix": "Ro charges $74 per month."}]
               + RO_ITEM["fixes"][:1])
    gen, _ = _gen(Script(items=[bad]))
    s = gen.verify_analyze(_brand(), BODY, META)
    assert [f["quote"] for f in s["items"][0]["proposal"]["fixes"]] == [TABLE_RO]


# ═══════════════════════════════════════ apply ═════════════════════════════════════════════════
def _session(items=None, **kw):
    gen, _ = _gen(Script(items=items if items is not None else [RO_ITEM], **kw))
    return gen.verify_analyze(_brand(), BODY, META)


def test_an_approved_price_change_is_applied_everywhere_and_the_source_is_not_mutated():
    s = _session()
    body_in, meta_in = str(BODY), str(META)
    gen, _ = _gen(Script())
    out = gen.verify_apply(_brand(), body_in, meta_in, s, seed=SEED)
    assert body_in == BODY and meta_in == META
    assert "$145" not in out["body"].split("## Sources")[0]
    assert out["body"].count("$74/month") == 3 and "Ro $74/month" in out["meta_description"]
    assert out["report"]["n_applied"] == 4 and out["report"]["n_refused"] == 0
    assert {a["where"] for a in out["report"]["applied"]} == {"table", "quick answer", "faq", "meta"}


def test_an_unapproved_number_change_in_the_same_sentence_is_still_refused():
    sneaky = dict(RO_ITEM, fixes=[{"quote": FAQ, "fix": "Ro's membership costs $74/month [S2], with a $5 setup fee."}])
    gen, _ = _gen(Script())
    s = _session(items=[sneaky])
    out = gen.verify_apply(_brand(), BODY, META, s, seed=SEED)
    assert out["body"] == gen._force_h1(bgmod.scrub_markdown_formatting(gen._sa(BODY))[0], SEED)
    reasons = [r["reason"] for r in out["report"]["refused"]]
    assert any("20" in r and "not part of the approved change" in r for r in reasons), reasons


def test_the_gate_directly_allows_the_approved_value_and_refuses_another():
    gen = BG(None, None)
    ok = gen._vx_gate(BODY, FAQ, "Ro's membership costs $74/month [S2], with a $20 setup fee.",
                      _brand(), "$145/month", "correct")
    assert ok == ""
    bad = gen._vx_gate(BODY, FAQ, "Ro's membership costs $74/month [S2], with a $25 setup fee.",
                       _brand(), "$145/month", "correct")
    assert "not part of the approved change" in bad
    # the OLD verify gate refuses the approved change outright — the reason this round exists
    assert gen._verify_repair_gate(BODY, FAQ, "Ro's membership costs $74/month [S2], with a $20 setup fee.",
                                   _brand())


def test_citations_are_preserved_and_the_protected_lines_are_refused():
    gen = BG(None, None)
    assert "citations" in gen._vx_gate(BODY, FAQ, "Ro's membership costs $74/month, with a $20 setup fee.",
                                       _brand(), "$145/month", "correct")
    assert "H1" in gen._vx_gate(BODY, f"# {SEED}", "# something else", _brand(), "", "correct")
    assert "byline" in gen._vx_gate(BODY, "*[Add author byline before publishing]*", "*By Jane*",
                                    _brand(), "", "correct")
    assert "Sources" in gen._vx_gate(BODY, "- [S2] Ro — <https://ro.co/pricing>", "- [S2] Ro",
                                     _brand(), "", "correct")
    assert "table cells" in gen._vx_gate(BODY, TABLE_RO, "| Ro | $74/month [S2] |", _brand(),
                                         "$145/month", "correct")
    assert "em-dash" in gen._vx_gate(BODY, QUICK, "Acme is a strong pick — Ro charges $74/month for membership [S2].",
                                     _brand(), "$145/month", "correct")
    assert "banned" in gen._vx_gate(BODY, QUICK, "Ro's pricing is not disclosed in public sources [S2].",
                                    _brand(), "$145/month", "soften")


def test_a_web_sourced_correction_appends_and_cites_a_new_source():
    verdicts = [{"ref": "c1", "status": "not_on_page", "page": "P1"},
                {"ref": "c2", "status": "confirmed", "page": "P2", "page_says": "Acme membership is $149/month"}]
    item = dict(RO_ITEM, fixes=[{"quote": TABLE_RO, "fix": "| Ro | $74/month [S2][NEW] | ships in 14 days |"}],
                basis={"type": "web source", "url": "https://news.example.com/ro-pricing", "excerpt": "Ro now charges $74"})
    script = Script(verdicts=verdicts, items=[item])
    script_search = lambda brief, allowed, blocked: ([] if allowed else [
        {"title": "Ro cuts its price", "url": "https://news.example.com/ro-pricing", "fact": "Ro now charges $74 a month."}])
    claude = StubClaude(call_handler=script, search_handler=script_search)
    gen = BG(claude, None)
    gen._fetch_url = lambda u: PAGES.get(u, "")
    s = gen.verify_analyze(_brand(), BODY, META)
    it = s["items"][0]
    assert it["new_source"] == {"label": "Ro cuts its price", "url": "https://news.example.com/ro-pricing"}
    out = gen.verify_apply(_brand(), BODY, META, s, seed=SEED)
    assert "| Ro | $74/month [S2][S3] | ships in 14 days |" in out["body"]
    assert "- [S3] Ro cuts its price — <https://news.example.com/ro-pricing>" in out["body"]
    assert out["report"]["new_sources"][0]["cite"] == "[S3]"


def test_an_edited_fix_is_used_as_written_and_still_gated():
    s = _session()
    gen, _ = _gen(Script())
    mine = "| Ro | $74/month (billed monthly) [S2] | ships in 14 days |"
    out = gen.verify_apply(_brand(), BODY, META, s, seed=SEED, decisions={"i1": {
        "approved": True, "fixes": [{"quote": TABLE_RO, "fix": mine, "edited": True}]}})
    assert mine in out["body"]
    broken = "| Ro | $74/month | ships in 14 days |"   # the operator's edit dropped [S2]
    out2 = gen.verify_apply(_brand(), BODY, META, s, seed=SEED, decisions={"i1": {
        "approved": True, "fixes": [{"quote": TABLE_RO, "fix": broken, "edited": True}]}})
    assert TABLE_RO in out2["body"]
    assert any(r["quote"] == TABLE_RO and "citations" in r["reason"] for r in out2["report"]["refused"])


def test_a_comment_reaches_the_drafting_call_and_limits_the_change():
    s = _session()
    script = Script(drafts=[{"id": "i1", "fixes": [{"quote": TABLE_RO, "fix": "| Ro | $74/month [S2] | ships in 14 days |"}]}])
    gen, _ = _gen(script)
    out = gen.verify_apply(_brand(), BODY, META, s, seed=SEED, decisions={"i1": {
        "approved": True, "comment": "only change the table"}})
    prompt = script.prompts["You are applying corrections"][0]
    assert "only change the table" in prompt
    assert "| Ro | $74/month [S2] | ships in 14 days |" in out["body"] and QUICK in out["body"]
    assert {u["where"] for u in out["report"]["unchanged"]} == {"quick answer", "faq", "meta"}


def test_an_unapproved_item_changes_nothing():
    s = _session()
    gen, _ = _gen(Script())
    out = gen.verify_apply(_brand(), BODY, META, s, seed=SEED, decisions={"i1": {"approved": False}})
    assert out["report"]["n_applied"] == 0 and out["report"]["not_approved"] == ["i1"]
    assert TABLE_RO in out["body"]


def test_text_changed_since_the_analysis_is_refused_not_guessed():
    s = _session()
    edited = BODY.replace(TABLE_RO, "| Ro | $150/month [S2] | ships in 14 days |")
    gen, _ = _gen(Script())
    out = gen.verify_apply(_brand(), edited, META, s, seed=SEED)
    assert out["report"]["text_changed"] is True
    assert any("changed since the analysis" in r["reason"] for r in out["report"]["refused"])


def test_upsert_canonical_value_is_additive():
    kf = {"pricing": {"items": [
        {"product": "tirzepatide", "value": "$149", "operator_set": True},
        {"product": "semaglutide", "value": "$99", "operator_set": True}]},
        "facts": {"items": [{"label": "", "value": "Ships to all 50 states", "operator_set": True}]}}
    out, saved = upsert_canonical_value(json.dumps(kf), "price", "tirzepatide", "$249/month", "https://acme.com/p")
    items = {i["product"]: i["value"] for i in out["pricing"]["items"]}
    assert saved and items == {"tirzepatide": "$249/month", "semaglutide": "$99"}
    assert out["facts"] == kf["facts"]
    out2, saved2 = upsert_canonical_value(out, "price", "", "$10")
    assert not saved2, "a nameless price on a per-product brand is reported, not silently kept"
    out3, _ = upsert_canonical_value(out, "fact", "", "Ships to 49 states")
    assert len(out3["facts"]["items"]) == 2


# ═══════════════════════════════════════ endpoints ═════════════════════════════════════════════
def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def _seed(path, key_facts=None):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    db.update_brand(bid, domain_url="https://acme.com", competitor_domains=json.dumps({"Ro": "ro.co"}),
                    **({"key_facts": json.dumps(key_facts)} if key_facts else {}))
    blog_id = db.save_blog(bid, SEED, title="Which telehealth platforms?", meta_description=META,
                           body_markdown=BODY)
    row = db.get_blog(blog_id)
    db.close()
    return blog_id, bid, row["body_markdown"], row["meta_description"]


def _row(path, blog_id):
    db = Database(path)
    db.connect()
    try:
        return db.get_blog(blog_id)
    finally:
        db.close()


@pytest.fixture
def api(monkeypatch):
    import app as appmod
    box = {"fetched": []}

    def _inline(_name, fn, **kw):
        box.pop("error", None)
        try:   # a real task failure marks the task as errored; record it the same way
            box["result"] = fn(_task_id="t") if kw.get("pass_task_id") else fn()
        except Exception as e:
            box["error"] = str(e)
        return "t"
    monkeypatch.setattr(appmod, "start_task", _inline)

    def _fetch(self, u):
        box["fetched"].append(u)
        return PAGES.get(u, "")
    monkeypatch.setattr(BG, "_fetch_url", _fetch)

    def _make(path, script):
        claude = StubClaude(call_handler=script)
        box["claude"] = claude
        monkeypatch.setattr(appmod, "ClaudeClient", lambda *a, **k: claude)
        appmod.DB_PATH = path
        appmod._db_initialized = True
        return appmod.app.test_client()
    return _make, box


def test_endpoints_analysis_then_apply_never_touch_the_original(api):
    make, box = api
    path = _tmp()
    try:
        blog_id, _, body0, meta0 = _seed(path)
        cli = make(path, Script())
        assert cli.post(f"/api/blogs/{blog_id}/verify-analyze", json={"notes": []}).status_code == 200
        row = _row(path, blog_id)
        assert row["body_markdown"] == body0 and row["meta_description"] == meta0
        assert row["verify_session"]["status"] == "ready" and row["verify_session"]["source"] == "original"
        assert box["result"]["items"] == 1
        assert cli.post(f"/api/blogs/{blog_id}/verify-apply", json={"decisions": {}}).status_code == 200
        row = _row(path, blog_id)
        assert row["body_markdown"] == body0 and row["meta_description"] == meta0, "apply wrote the original"
        assert "$74/month" in row["verified_body"] and "Ro $74/month" in row["verified_meta_description"]
        assert row["verified_report"]["n_applied"] == 4 and row["verified_at"]
        assert row["verify_session"]["status"] == "applied"
        assert row["verify_session"]["applied_from"]["body"] == body0
    finally:
        os.unlink(path)


def test_endpoint_canonical_checkbox_upserts_one_item_and_keeps_the_rest(api):
    make, box = api
    path = _tmp()
    kf = {"pricing": {"items": [{"product": "tirzepatide", "value": "$149", "operator_set": True},
                                {"product": "semaglutide", "value": "$99", "operator_set": True}]}}
    try:
        blog_id, bid, _, _ = _seed(path, key_facts=kf)
        acme_claim = [{"claim": "Acme tirzepatide costs $149/month", "kind": "price", "entity": "Acme",
                       "product": "tirzepatide", "value": "$149/month", "cited": ["S1"],
                       "occurrences": [{"where": "table", "quote": "| Acme | $149/month [S1] | doctor-led |"}]}]
        item = {"ref": "c1", "problem": "Acme's price changed.", "action": "correct", "old_value": "$149/month",
                "new_value": "$249/month",
                "fixes": [{"quote": "| Acme | $149/month [S1] | doctor-led |", "fix": "| Acme | $249/month [S1] | doctor-led |"}],
                "basis": {"type": "cited page", "url": "https://acme.com/pricing", "excerpt": ""},
                "confidence": "high", "conflict": ""}
        verdicts = [{"ref": "c1", "status": "contradicted", "page": "P1", "page_says": "Acme membership is $149/month"}]
        cli = make(path, Script(claims=acme_claim, verdicts=verdicts, items=[item]))
        cli.post(f"/api/blogs/{blog_id}/verify-analyze", json={})
        cli.post(f"/api/blogs/{blog_id}/verify-apply",
                 json={"decisions": {"i1": {"approved": True, "save_canonical": True}}})
        db = Database(path)
        db.connect()
        stored = json.loads(db.get_brand(bid)["key_facts"])
        db.close()
        items = {i["product"]: i["value"] for i in stored["pricing"]["items"]}
        assert items == {"tirzepatide": "$249/month", "semaglutide": "$99"}
        assert _row(path, blog_id)["verified_report"]["canonical"][0]["saved"] is True
    finally:
        os.unlink(path)


def test_endpoint_redo_makes_no_fetch_and_no_search_and_keeps_the_approvals(api):
    make, box = api
    path = _tmp()
    try:
        blog_id, _, body0, _ = _seed(path)
        script = Script(drafts=[{"id": "i1", "fixes": [
            {"quote": TABLE_RO, "fix": "| Ro | $74/month [S2] | ships in 14 days |"},
            {"quote": QUICK, "fix": "Acme is a strong pick; Ro's membership is $74/month [S2]."}]}])
        cli = make(path, script)
        cli.post(f"/api/blogs/{blog_id}/verify-analyze", json={})
        cli.post(f"/api/blogs/{blog_id}/verify-apply", json={"decisions": {}})
        box["fetched"].clear()
        n_search = len(box["claude"].searches)
        assert cli.post(f"/api/blogs/{blog_id}/verify-redo").status_code == 200
        assert box["fetched"] == [] and len(box["claude"].searches) == n_search
        assert "FRESH wording" in script.prompts["You are applying corrections"][-1]
        row = _row(path, blog_id)
        assert "Ro's membership is $74/month [S2]" in row["verified_body"]
        assert row["verified_report"]["redo"] is True and row["body_markdown"] == body0
    finally:
        os.unlink(path)


def test_endpoint_verify_again_reads_the_verified_version(api):
    make, box = api
    path = _tmp()
    try:
        blog_id, _, _, _ = _seed(path)
        db = Database(path)
        db.connect()
        db.update_blog(blog_id, verified_body=BODY.replace("doctor-led", "VERIFIED-ONLY-MARKER"))
        db.close()
        script = Script(items=[])
        cli = make(path, script)
        cli.post(f"/api/blogs/{blog_id}/verify-analyze", json={"source": "verified"})
        assert "VERIFIED-ONLY-MARKER" in script.prompts["You are auditing a FINISHED"][0]
        assert _row(path, blog_id)["verify_session"]["source"] == "verified"
    finally:
        os.unlink(path)


def test_endpoint_verify_again_without_a_verified_version_errors(api):
    make, box = api
    path = _tmp()
    try:
        blog_id, _, _, _ = _seed(path)
        cli = make(path, Script())
        cli.post(f"/api/blogs/{blog_id}/verify-analyze", json={"source": "verified"})
        assert "no verified version yet" in box.get("error", "")
        assert not _row(path, blog_id)["verify_session"]
    finally:
        os.unlink(path)


def test_endpoint_discard_restores_an_applied_session(api):
    make, box = api
    path = _tmp()
    try:
        blog_id, _, _, _ = _seed(path)
        cli = make(path, Script())
        cli.post(f"/api/blogs/{blog_id}/verify-analyze", json={})
        cli.post(f"/api/blogs/{blog_id}/verify-apply", json={"decisions": {}})
        cli.post(f"/api/blogs/{blog_id}/verify-analyze", json={"source": "verified"})
        assert _row(path, blog_id)["verify_session"]["status"] == "ready"
        r = cli.post(f"/api/blogs/{blog_id}/verify-discard").get_json()
        assert r["restored"] is True
        assert _row(path, blog_id)["verify_session"]["status"] == "applied"
    finally:
        os.unlink(path)


def test_exports_serve_the_verified_version_and_fall_back_to_the_original(api):
    make, box = api
    path = _tmp()
    try:
        blog_id, _, _, _ = _seed(path)
        cli = make(path, Script())
        md = cli.get(f"/api/blogs/{blog_id}/export?format=md&use=verified").get_data(as_text=True)
        assert "$145/month" in md, "no verified version yet → the original"
        db = Database(path)
        db.connect()
        db.update_blog(blog_id, verified_body=BODY.replace("$145/month", "$74/month"),
                       verified_meta_description="Verified meta: Ro $74/month.")
        db.close()
        md = cli.get(f"/api/blogs/{blog_id}/export?format=md&use=verified").get_data(as_text=True)
        assert "$74/month" in md and "$145/month" not in md
        html = cli.get(f"/api/blogs/{blog_id}/export?format=html&use=verified").get_data(as_text=True)
        assert "$74/month" in html and "Verified meta: Ro $74/month." in html
        plain = cli.get(f"/api/blogs/{blog_id}/export?format=md").get_data(as_text=True)
        assert "$145/month" in plain, "without use=verified the original still exports"
    finally:
        os.unlink(path)


def test_drive_upload_sends_the_verified_version(api, monkeypatch):
    make, box = api
    path = _tmp()
    try:
        blog_id, _, _, _ = _seed(path)
        db = Database(path)
        db.connect()
        db.update_blog(blog_id, verified_body=BODY.replace("$145/month", "$74/month"),
                       verified_meta_description=META.replace("$145/month", "$74/month"))
        db.meta_set("gdoc_script_url", "https://script.example/exec")
        db.meta_set("gdoc_secret", "s")
        db.close()
        cli = make(path, Script())
        sent = {}

        class _R:
            status_code = 200
            text = "{}"

            def json(self):
                return {"ok": True, "url": "https://docs.google.com/document/d/x"}
        import requests
        monkeypatch.setattr(requests, "post", lambda url, json=None, **k: (sent.update(json or {}), _R())[1])
        cli.post(f"/api/blogs/{blog_id}/upload-gdoc", json={"use": "verified"})
        assert "$74/month" in sent["html"] and "$145/month" not in sent["html"]
        cli.post(f"/api/blogs/{blog_id}/upload-gdoc", json={})
        assert "$145/month" in sent["html"]
    finally:
        os.unlink(path)


def test_a_hand_edited_verified_version_is_sanitised_on_save(api):
    make, box = api
    path = _tmp()
    try:
        blog_id, _, _, _ = _seed(path)
        cli = make(path, Script())
        r = cli.patch(f"/api/blogs/{blog_id}", json={
            "verified_body": BODY.replace("Acme is a strong pick, and", "Acme is a strong pick —"),
            "verified_meta_description": "Verified — meta"})
        assert r.status_code == 200
        row = _row(path, blog_id)
        assert "—" not in row["verified_body"].split("## Sources")[0]
        assert "—" not in row["verified_meta_description"]
    finally:
        os.unlink(path)
