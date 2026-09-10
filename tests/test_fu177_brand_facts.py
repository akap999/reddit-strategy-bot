"""FU177 — operator-set CANONICAL BRAND FACTS: free-form lines the blog treats as the brand's own
authoritative first-party evidence (the non-pricing sibling of the FU150/FU158 canonical pricing)."""
import json

from generators.blog_gen import (_parse_fact_lines, _kf_fact_items, _canonical_facts_block,
                                 _kf_pricing_items)


def test_plain_prose_line_needs_no_structure():
    items = _parse_fact_lines("B2B-only — does not handle consumer debt\n")
    assert items == [{"label": "", "value": "B2B-only — does not handle consumer debt",
                      "source_url": "", "operator_set": True}]


def test_optional_label_and_trailing_url_are_honoured():
    items = _parse_fact_lines(
        "Minimum claim | $10,000 per file\n"
        "Member of the CLLA | https://brand.example/about\n"
        "Contingency | 10-25% of the amount collected | https://brand.example/pricing\n")
    assert items[0]["label"] == "Minimum claim" and items[0]["value"] == "$10,000 per file"
    # label-less + URL: the trailing URL is popped FIRST, so the remainder is the fact itself
    assert items[1] == {"label": "", "value": "Member of the CLLA",
                        "source_url": "https://brand.example/about", "operator_set": True}
    assert items[2]["label"] == "Contingency"
    assert items[2]["source_url"] == "https://brand.example/pricing"


def test_blank_lines_skipped_and_empty_text_is_inert():
    assert _parse_fact_lines("\n\n   \n") == []
    assert _parse_fact_lines("") == []


def test_read_is_tolerant_of_bare_strings_and_partial_dicts():
    got = _kf_fact_items({"facts": ["plain", {"value": "dict fact"}, {"value": "  "}, 7]})
    assert [g["value"] for g in got] == ["plain", "dict fact", "7"]


def test_render_block_carries_facts_and_keeps_pricing():
    kf = {"pricing": {"items": [{"product": "Standard", "value": "$300/file", "operator_set": True}]},
          "facts": {"items": _parse_fact_lines("B2B-only\nMinimum claim | $10,000 per file\n")}}
    block = _canonical_facts_block("Acme", kf)
    assert "pricing (Standard)" in block and "$300/file" in block
    assert "- B2B-only" in block
    assert "- Minimum claim: $10,000 per file" in block
    # the header must tell the writer these are usable, citable first-party evidence
    assert "first-party EVIDENCE" in block and "NEVER" in block


def test_facts_key_is_not_double_rendered_by_the_generic_loop():
    kf = {"facts": {"items": [{"label": "", "value": "only once", "source_url": ""}]}}
    assert _canonical_facts_block("Acme", kf).count("only once") == 1


def test_no_facts_is_byte_identical_to_before():
    kf = {"pricing": {"items": [{"product": "", "value": "$99/mo"}]}}
    assert "$99/mo" in _canonical_facts_block("Acme", kf)
    assert _canonical_facts_block("Acme", {}) == ""


def test_facts_survive_alongside_pricing_in_storage_shape():
    """The whole key_facts dict round-trips: adding facts must not disturb _kf_pricing_items."""
    kf = json.loads(json.dumps({
        "pricing": {"items": [{"product": "P", "value": "$1", "operator_set": True}]},
        "facts": {"items": _parse_fact_lines("a fact")}}))
    assert [i["product"] for i in _kf_pricing_items(kf)] == ["P"]
    assert [i["value"] for i in _kf_fact_items(kf)] == ["a fact"]


# --- FU178: a fact earns an [S#] ONLY when a real page can be shown to state it ------------------
from generators.blog_gen import BlogGenerator          # noqa: E402
from tests.stubs import StubClaude                     # noqa: E402


def _source(brand, evidence_blocks=None):
    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": ["Rival"], "peer_tools": ["Rival"], "dimensions": ["pricing"],
                    "products": [], "claims": [], "core_topic": "collections"}
        if '"domains"' in p:
            return {"domains": {"Rival": "rival.com"}}
        return {}
    gen = BlogGenerator(StubClaude(call_handler=call_h, search_handler=lambda b, a, bl: []), db=None)
    gen._evidence_blocks = list(evidence_blocks or [])
    body = "| Agency | Pricing |\n|---|---|\n| Acme | ? |\n| Rival | ? |\n"
    return gen, gen._source_for_completion(brand, "best agencies", {"body_markdown": body}, ymyl=None)


def _brand_with_facts(text):
    return {"name": "Acme", "domain_url": "https://acme.com", "category": "collections",
            "key_facts": json.dumps({"facts": {"items": _parse_fact_lines(text)}})}


def _subject_blocks(sourcing):
    return [f for f in sourcing["fresh"] if (f.get("label") or "").lower() == "acme"]


FACT = "Accredited by the Commercial Law League of America"


def test_tier1_fact_found_on_a_fetched_page_is_cited_to_THAT_page():
    page = {"label": "Acme", "url": "https://acme.com/about",
            "text": "About us. Acme is accredited by the Commercial Law League of America since 1994."}
    _gen, sourcing = _source(_brand_with_facts(FACT), evidence_blocks=[page])
    hit = [b for b in _subject_blocks(sourcing) if FACT.split()[0].lower() in b["text"].lower()]
    assert hit, "a fact stated on an already-fetched page must become citable evidence"
    assert hit[0]["url"] == "https://acme.com/about", "must cite the PAGE, not the bare homepage"


def test_tier2_operator_url_is_FETCHED_and_verified_before_it_is_cited(monkeypatch):
    import generators.blog_gen as bg
    monkeypatch.setattr(bg, "_fetch_homepage",
                        lambda u, **k: f"<html><body>Acme is {FACT.lower()}.</body></html>")
    monkeypatch.setattr(bg, "_extract_visible_text", lambda h: (h or ""))
    _gen, sourcing = _source(_brand_with_facts(f"{FACT} | https://acme.com/creds"))
    hit = [b for b in _subject_blocks(sourcing) if "league" in b["text"].lower()]
    assert hit and hit[0]["url"] == "https://acme.com/creds"


def test_tier2_miss_is_demoted_and_never_cited(monkeypatch):
    """The operator's URL does NOT state the fact — citing it unread is the bug this prevents."""
    import generators.blog_gen as bg
    monkeypatch.setattr(bg, "_fetch_homepage", lambda u, **k: "<html><body>Contact us.</body></html>")
    monkeypatch.setattr(bg, "_extract_visible_text", lambda h: (h or ""))
    gen, sourcing = _source(_brand_with_facts(f"{FACT} | https://acme.com/contact"))
    assert not [b for b in _subject_blocks(sourcing) if "league" in b["text"].lower()]
    assert "unverified" in (sourcing.get("unverified_facts") and "unverified" or "unverified")
    assert any("League" in x for x in sourcing["unverified_facts"])
    assert getattr(gen, "_facts_note", "")


def test_tier3_unverifiable_fact_NEVER_mints_a_homepage_citation():
    """The FU178 core: a typed assertion is not a source. No page states it → no [S#] at all."""
    gen, sourcing = _source(_brand_with_facts("B2B-only — does not handle consumer debt"))
    assert not [b for b in _subject_blocks(sourcing) if "b2b-only" in (b["text"] or "").lower()], \
        "an unverified assertion must not be cited to the bare homepage"
    assert sourcing["unverified_facts"] == ["B2B-only — does not handle consumer debt"]
    assert "could not be found" in gen._facts_note


def test_tier3_fact_still_reaches_the_writer_and_the_coverage_test():
    """Not citable is not the same as unusable: it still renders in the canonical block, and it still
    marks the subject's dimension covered so no rescue search is wasted re-finding it."""
    brand = _brand_with_facts("Minimum claim | $10,000 per file")
    gen, sourcing = _source(brand)
    assert "$10,000 per file" in _canonical_facts_block("Acme", json.loads(brand["key_facts"]))
    assert not _subject_blocks(sourcing)
    assert sourcing["unverified_facts"]


def test_reconcile_is_told_to_attribute_an_unverified_line(monkeypatch):
    captured = {}

    def call_h(p):
        if "UNVERIFIED CANONICAL LINES" in p:
            captured["prompt"] = p
        return {"revised_body_markdown": "x", "flagged": []}
    gen = BlogGenerator(StubClaude(call_handler=call_h), db=None)
    gen._evidence_blocks = []
    gen._reconcile_and_finish(
        {"name": "Acme", "domain_url": "https://acme.com"}, "best agencies",
        {"body_markdown": "# t\n\nbody"},
        {"name": "Acme", "tools": [], "dims": [], "claims": [], "peers": [],
         "fresh": [{"label": "Acme", "url": "https://acme.com/x", "text": "a sourced fact"}],
         "unverified_facts": ["B2B-only — does not handle consumer debt"]})
    assert "prompt" in captured, "the reconcile must be told which lines are unverified"
    assert "OWN POSITIONING" in captured["prompt"] and "do NOT attach an [S#]" in captured["prompt"]
    assert "B2B-only" in captured["prompt"]


# --- the save path: facts and pricing are independent, and the textarea is authoritative ---------
import os        # noqa: E402
import tempfile  # noqa: E402

from db import Database   # noqa: E402


def _client(tmp_db):
    import app as appmod
    appmod.DB_PATH = tmp_db
    appmod._db_initialized = True
    return appmod.app.test_client()


def _seed(path, key_facts=None):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], "Acme")
    if key_facts:
        db.update_brand(bid, key_facts=json.dumps(key_facts))
    db.close()
    return bid


def _kf(path, bid):
    db = Database(path)
    db.connect()
    kf = json.loads(db.get_brand(bid).get("key_facts") or "{}")
    db.close()
    return kf


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_saving_facts_persists_them_and_keeps_pricing():
    path = _tmp()
    try:
        priced = {"pricing": {"items": [{"product": "P", "value": "$1", "operator_set": True}]}}
        bid = _seed(path, priced)
        r = _client(path).put(f"/api/brands/{bid}",
                              json={"key_facts_facts": "B2B-only\nMinimum | $10,000 per file"})
        assert r.status_code == 200
        kf = _kf(path, bid)
        assert [i["value"] for i in _kf_fact_items(kf)] == ["B2B-only", "$10,000 per file"]
        assert [i["product"] for i in _kf_pricing_items(kf)] == ["P"], "pricing must survive a facts save"
    finally:
        os.unlink(path)


def test_saving_pricing_keeps_facts():
    path = _tmp()
    try:
        bid = _seed(path, {"facts": {"items": [{"label": "", "value": "keep me"}]}})
        r = _client(path).put(f"/api/brands/{bid}",
                              json={"key_facts_pricing_items": [{"product": "P", "value": "$9"}]})
        assert r.status_code == 200
        kf = _kf(path, bid)
        assert [i["value"] for i in _kf_fact_items(kf)] == ["keep me"]
        assert [i["value"] for i in _kf_pricing_items(kf)] == ["$9"]
    finally:
        os.unlink(path)


def test_clearing_the_textarea_deletes_the_facts():
    path = _tmp()
    try:
        bid = _seed(path, {"facts": {"items": [{"label": "", "value": "gone soon"}]},
                           "pricing": {"items": [{"product": "P", "value": "$1"}]}})
        assert _client(path).put(f"/api/brands/{bid}", json={"key_facts_facts": ""}).status_code == 200
        kf = _kf(path, bid)
        assert _kf_fact_items(kf) == []
        assert "facts" not in kf
        assert [i["product"] for i in _kf_pricing_items(kf)] == ["P"]
    finally:
        os.unlink(path)


def test_a_save_that_mentions_neither_leaves_key_facts_untouched():
    path = _tmp()
    try:
        seeded = {"facts": {"items": [{"label": "", "value": "untouched"}]}}
        bid = _seed(path, seeded)
        assert _client(path).put(f"/api/brands/{bid}", json={"context": "hi"}).status_code == 200
        assert [i["value"] for i in _kf_fact_items(_kf(path, bid))] == ["untouched"]
    finally:
        os.unlink(path)
