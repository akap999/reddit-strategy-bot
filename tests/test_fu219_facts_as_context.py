"""FU219 — verified facts are CONTEXT, not content (the operator's rule: "facts should be used as
context; if I want something mandatory I'll add it in Content instructions").

The case that started it: CMK Construction's "best home renovation companies in Hillsborough County"
blog shipped 17 uncited statements — every competitor fact the operator had verified, pasted into the
prose as a dossier (qualifier names, owners, a street address), because FU217 told the writer to "use
these EXACT values wherever this article names these brands" and stated a no-link fact uncited. And
the operator's "never call CBC a General Building Contractor" correction was lost at Analyse, so the
wrong phrase shipped anyway.

Everything is $0 (StubClaude). The FU217 suite covers the rest of the machinery.
"""
import json
import os

from generators.blog_gen import BlogGenerator as BG, _vfact_clean, _vfact_prompt_block, build_blog_jsonld
from tests.stubs import StubClaude

PASTE = open(os.path.join(os.path.dirname(__file__), "fixtures", "fu217_cmk_verified_paste.txt"),
             encoding="utf-8").read()
_CORR = next(l for l in PASTE.splitlines() if l.startswith("Correction needed"))


def _store():
    parsed = [
        {"name": "CMK Construction", "sources": [{"url": "cmkconstructioninc.com/awards-licenses", "name": ""}],
         "rows": [{"fact": "CGC 1516665 — State Certified General Contractor", "kind": "license"}]},
        {"name": "Revive Design and Renovation", "trade_name": "Revive Kitchen & Bath",
         "legal_name": "Revive Design and Renovation",
         "rows": [{"fact": "CBC1264856 — Certified Building Contractor, Current/Active, licensed 07/22/2021",
                   "kind": "license"},
                  {"fact": "Qualifier: Justin Caballero. 3102 W Kennedy Blvd, Tampa. BuildZoom 111, top 4%.",
                   "kind": "note"}]},
        {"name": "S&W Kitchens, Inc.",
         "rows": [{"fact": "CBC1262059 is a Certified Building Contractor licence, not a General Building "
                           "Contractor licence", "kind": "correction", "wrong": "General Building Contractor",
                   "raw": _CORR}]},
    ]
    return _vfact_clean(parsed, known_names=["Revive Kitchen & Bath", "S&W Kitchens"],
                        subject="CMK Construction", verbatim_text=PASTE)


BRAND = {"id": 1, "name": "CMK Construction", "domain_url": "https://cmkconstructioninc.com",
         "competitors": json.dumps(["Revive Kitchen & Bath", "S&W Kitchens"])}


def _brand():
    return dict(BRAND, verified_facts=json.dumps(_store()[0]))


def test_the_writer_is_told_the_facts_are_background_not_a_list_to_include():
    p = _vfact_prompt_block(_brand(), "article", [])
    assert "BACKGROUND CONTEXT, not content" in p
    assert "never add a fact, a sentence or a section just because it is listed here" in p
    assert "never run through a brand's facts as a list" in p
    assert "Use these EXACT values wherever" not in p and "FIRST mention" not in p
    # a line with no link is still stated where relevant — just without a citation
    assert "(no source — state it without a citation)" in p
    assert "may be stated wherever it is relevant, without a citation" in p
    # accuracy still holds wherever the article does state one
    assert "must match the line here exactly" in p


def test_a_paraphrased_correction_keeps_the_operators_own_line_and_its_never_write_phrase():
    store, dropped, _f, _o = _store()
    rows = store["s-w-kitchens"]["rows"]
    assert rows and rows[0]["kind"] == "correction"
    assert rows[0]["fact"] == _CORR, "the model's paraphrase is replaced by the pasted line"
    assert rows[0]["wrong"] == "General Building Contractor"
    assert not [d for d in dropped if "General Building" in d["raw"]]
    p = _vfact_prompt_block(_brand(), "article", [])
    assert 'never write "General Building Contractor" about S&W Kitchens' in p


def test_a_non_correction_paraphrase_is_still_dropped():
    _s, dropped, _f, _o = _vfact_clean(
        [{"name": "S&W Kitchens", "rows": [{"fact": "A leading kitchen firm", "kind": "note",
                                            "raw": "S&W Kitchens, Inc."}]}],
        known_names=["S&W Kitchens"], subject="CMK Construction", verbatim_text=PASTE)
    assert any("reworded" in d["why"] for d in dropped)


def test_the_finished_body_gets_no_fact_inserted_and_the_wrong_phrase_is_still_caught():
    g = BG(StubClaude(), db=None)
    body = "# t\n\nRevive Kitchen & Bath remodels baths.\n\nS&W Kitchens is a General Building Contractor.\n"
    out, n = g._vfact_enforce(body, _brand())
    assert (out, n) == (body, 0)
    notes = g._vfact_checks(body, _brand())
    assert any('"General Building Contractor" appears with S&W Kitchens' in x for x in notes)


def test_structured_data_names_the_company_not_its_parenthetical():
    body = ("# t\n\n| Company | Licence |\n|---|---|\n"
            "| **Revive Kitchen & Bath** (licensed as Revive Design and Renovation) | x |\n"
            "| Bath World (also known as Bath World and More) | y |\n")
    ld = build_blog_jsonld({"title": "t", "body_markdown": body}, {"name": "CMK Construction"})
    graph = json.loads(ld)["@graph"] if isinstance(ld, str) else ld["@graph"]
    items = next(x for x in graph if x["@type"] == "ItemList")["itemListElement"]
    assert [i["name"] for i in items] == ["Revive Kitchen & Bath", "Bath World"]
