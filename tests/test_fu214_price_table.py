"""FU214 — hand the tool the prices you already have, in any shape, and compare exactly those brands.

Before this round a competitor price was ONE figure with no product attached: the save endpoint ran
the value through `_PRICE_FIG_RE` and kept `m.group(0)`, so "from $24.99" and "$19.99-$29.99" could
not be stored at all, and a brand selling a single AND a 3-pack could carry one number. The operator
already HAS the pricing — in a sheet, a doc, a few typed lines — so they paste it, ONE small call
parses it, and CODE decides what survives: every figure must appear verbatim in what was pasted.

$0 and network-free: StubClaude for every model call, temp SQLite for every write.
"""
import json
import os
import tempfile

from db import Database
from generators.blog_gen import (BlogGenerator, _format_price_value, _norm_price_kind,
                                 _priced_competitor_names, _is_priced, _PRICED_FIELD_MAX)
from tests.stubs import StubClaude

import app as _app

# FU265: a brand holds NAMED price sets; read them the way production does.
from generators.blog_gen import price_set_rows as _price_set_rows  # noqa: E402


BRAND = {"id": 1, "name": "Thyseed", "category": "baby bottles",
         "domain_url": "https://thyseed.com",
         "competitors": json.dumps(["Philips Avent", "Pigeon", "Comotomo", "Dr. Brown's"])}


def _tbl(*rows):
    """Build the stored price_table shape from (brand, product, kind, value, value_max, basis)."""
    out = {}
    for brand, product, kind, value, vmax, basis in rows:
        from generators.blog_gen import _kf_slug
        ent = out.setdefault(_kf_slug(brand), {"name": brand, "rows": []})
        ent["rows"].append({"product": product, "kind": kind, "value": value, "value_max": vmax,
                            "basis": basis, "url": "", "raw": f"{brand} {value}",
                            "updated_at": "2026-09-18T00:00:00Z"})
    return json.dumps(out)


# ─────────────────────────────────────────────────── Change 2: the parse, decided by CODE
def _parse(rows, text, names=None, subject=""):
    return _app._clean_price_rows(rows, known_names=names or [], subject=subject,
                                  verbatim_text=text)


TEXT = ("Philips Avent 3-pack $23.97\nPigeon from $12.99\n"
        "Comotomo $19.99-$24.99\nDr. Brown's doesn't publish a price\n")


def test_a_figure_the_model_returned_that_is_not_in_the_paste_is_dropped_with_a_reason():
    """The FU213 discipline applied to a paste: the model parses, CODE decides. A number no pasted
    line shows is exactly how a PMC study became a 'price' last round."""
    clean, dropped, _ = _parse(
        [{"brand": "Philips Avent", "kind": "exact", "value": "$23.97", "raw": "Philips Avent 3-pack $23.97"},
         {"brand": "Pigeon", "kind": "exact", "value": "$99.00", "raw": "Pigeon from $12.99"}], TEXT)
    assert list(clean) == ["philips-avent"]
    assert dropped and "$99.00" in dropped[0]["why"] and "not in the text" in dropped[0]["why"]


def test_kind_is_normalised_and_two_figures_typed_as_one_price_become_a_range():
    clean, dropped, _ = _parse(
        [{"brand": "Pigeon", "kind": "Starting at", "value": "$12.99", "raw": "Pigeon from $12.99"},
         {"brand": "Comotomo", "kind": "exact", "value": "$19.99", "value_max": "$24.99",
          "raw": "Comotomo $19.99-$24.99"}], TEXT)
    assert clean["pigeon"]["rows"][0]["kind"] == "from"
    r = clean["comotomo"]["rows"][0]
    assert r["kind"] == "range" and r["value"] == "$19.99" and r["value_max"] == "$24.99"
    assert dropped == []


def test_a_not_published_row_needs_no_figure_and_is_kept():
    clean, dropped, _ = _parse(
        [{"brand": "Dr. Brown's", "kind": "none", "value": "",
          "raw": "Dr. Brown's doesn't publish a price"}], TEXT)
    assert clean["dr-brown-s"]["rows"][0]["kind"] == "none" and dropped == []


def test_a_brand_matching_no_known_competitor_is_flagged_not_silently_added():
    """A typo must be VISIBLE — not a new brand that quietly joins the comparison."""
    clean, _d, flagged = _parse(
        [{"brand": "Philps Avnt", "kind": "exact", "value": "$23.97", "raw": "x $23.97"}],
        TEXT, names=["Philips Avent", "Pigeon"])
    assert flagged == ["Philps Avnt"] and "philps-avnt" in clean


def test_a_known_brand_is_matched_and_stored_under_its_real_name():
    clean, _d, flagged = _parse(
        [{"brand": "philips", "kind": "exact", "value": "$23.97", "raw": "x $23.97"}],
        TEXT, names=["Philips Avent"])
    assert flagged == [] and clean["philips-avent"]["name"] == "Philips Avent"


def test_a_non_http_link_is_dropped_and_the_row_survives():
    clean, _d, _f = _parse([{"brand": "Pigeon", "kind": "from", "value": "$12.99",
                            "url": "javascript:alert(1)", "raw": "Pigeon from $12.99"}], TEXT)
    assert clean["pigeon"]["rows"][0]["url"] == ""


def test_the_brand_cap_drops_the_extra_rather_than_the_rows_already_kept():
    rows, txt = [], ""
    for i in range(14):
        rows.append({"brand": f"Brand{i}", "kind": "exact", "value": f"${i + 10}.00",
                     "raw": f"Brand{i} ${i + 10}.00"})
        txt += f"Brand{i} ${i + 10}.00\n"
    clean, dropped, _ = _parse(rows, txt)
    assert len(clean) == _app._PRICE_TABLE_MAX_BRANDS and dropped
    assert all(v["rows"] for v in clean.values()), "a kept brand must never lose its rows to the cap"


# ─────────────────────────────────────────────────── Change 3: ONE formatter, everywhere
def test_every_kind_prints_the_way_the_article_will_state_it():
    assert _format_price_value({"value": "$23.97", "kind": "exact", "basis": "3-pack, 9 oz",
                                "per_unit": "$7.99 each"}) == "$23.97 (3-pack, 9 oz, $7.99 each)"
    assert _format_price_value({"value": "$12.99", "kind": "from"}) == "From $12.99"
    assert _format_price_value({"value": "$19.99", "value_max": "$24.99",
                                "kind": "range"}) == "$19.99-$24.99"
    assert _format_price_value({"value": "$29.99", "kind": "upto"}) == "Up to $29.99"
    assert _format_price_value({"value": "$1", "kind": "none"}) == ""
    # an entry stored before FU214 has no `kind` — it must keep printing as an exact price
    assert _format_price_value({"value": "$24.99", "basis": "single"}) == "$24.99 (single)"


def test_the_range_separator_survives_the_ai_symbol_scrub():
    """An en-dash between a digit and a `$` is rewritten to a COMMA by `_ai_fix_dashes`, which would
    silently turn one range into two prices — so the formatter uses an ASCII hyphen."""
    gen = BlogGenerator(StubClaude(), None)
    txt = _format_price_value({"value": "$19.99", "value_max": "$24.99", "kind": "range"})
    out, _n = gen._scrub_ai_symbols(f"It costs {txt} depending on size.\n")
    assert "$19.99-$24.99" in out


def test_the_cell_the_evidence_block_and_the_reconcile_all_say_the_same_thing():
    gen = BlogGenerator(StubClaude(), None)
    e = {"value": "$12.99", "kind": "from", "basis": "single 240ml", "per_unit": "",
         "url": "https://pigeon.example/p", "source": "yours", "checked_at": "2026-09-18T00:00:00Z"}
    shown = _format_price_value(e)
    gen._evidence_blocks = gen._price_ledger_blocks({"Pigeon": e})
    assert shown in gen._evidence_blocks[0]["text"]
    body = ("| Brand | Starting price |\n|---|---|\n| Pigeon | wrong |\n")
    out, n = gen._write_price_cells(body, {"Pigeon": e})
    assert n == 1 and shown in out and "wrong" not in out


def test_a_not_published_brand_writes_no_cell_and_no_evidence_block():
    gen = BlogGenerator(StubClaude(), None)
    e = {"value": "", "kind": "none", "source": "yours"}
    assert gen._price_ledger_blocks({"Dr. Brown's": e}) == []
    body = "| Brand | Starting price |\n|---|---|\n| Dr. Brown's | x |\n"
    out, n = gen._write_price_cells(body, {"Dr. Brown's": e})
    assert n == 0 and "| Dr. Brown's | x |" in out
    assert "Competitor prices are regular list prices" not in out


# ─────────────────────────────────────────── Change 3: precedence + product matching + cost
def _ledger(brand, tools, price_handler=None, topic=("bottle",), subject="baby bottle"):
    stub = StubClaude(price_handler=price_handler or (lambda **kw: {"candidates": [], "citations": []}))
    gen = BlogGenerator(stub, None)
    state = {t: {"dom": f"{t.lower().replace(' ', '')}.example"} for t in tools}
    led, missing, dirty = gen._ensure_price_ledger(brand, tools, state, {}, list(topic), subject)
    return stub, led, missing


def test_a_brand_you_priced_is_never_searched_and_never_link_fetched():
    """The cost win of the whole round: a pasted row is the operator speaking, so nothing is bought."""
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "single 240ml", "from", "$12.99", "", "")),
                 price_links=json.dumps({"pigeon": {"name": "Pigeon",
                                                    "urls": ["https://pigeon.example/p"]}}))
    stub, led, missing = _ledger(brand, ["Pigeon"])
    assert stub.price_calls == [] and stub.calls == [] and missing == []
    assert led["Pigeon"]["source"] == "yours" and _format_price_value(led["Pigeon"]) == "From $12.99"


def test_a_brand_you_did_not_price_is_still_searched():
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", "")))
    stub, led, missing = _ledger(brand, ["Pigeon", "Comotomo"])
    assert [c for c in stub.price_calls], "the unpriced brand must still be looked up"
    assert missing == ["Comotomo"] and "Pigeon" in led


def test_a_pasted_row_outranks_a_stale_cached_entry_and_never_expires():
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", "")))
    stub = StubClaude()
    gen = BlogGenerator(stub, None)
    old = {"value": "$44.44", "kind": "exact", "source": "retail",
           "checked_at": "2020-01-01T00:00:00Z"}
    led, missing, _d = gen._ensure_price_ledger(brand, ["Pigeon"], {"Pigeon": {"dom": "p.example"}},
                                                {"pigeon": {"price": old}}, ["bottle"], "bottle")
    assert led["Pigeon"]["value"] == "$12.99" and stub.price_calls == []


def test_several_rows_per_brand_pick_the_one_this_article_is_about():
    brand = dict(BRAND, price_table=_tbl(
        ("Philips Avent", "Anti-colic 9 oz single", "exact", "$9.99", "", "single 9 oz"),
        ("Philips Avent", "Anti-colic 3-pack", "exact", "$23.97", "", "3-pack, 9 oz")))
    _s, led, _m = _ledger(brand, ["Philips Avent"], topic=("bottle", "pack"), subject="3-pack bottles")
    assert led["Philips Avent"]["value"] == "$23.97"
    _s2, led2, _m2 = _ledger(brand, ["Philips Avent"], topic=("bottle", "single"),
                             subject="single 9 oz bottle")
    assert led2["Philips Avent"]["value"] == "$9.99"


def test_a_single_row_is_used_even_when_it_names_no_matching_product():
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "SofTouch", "exact", "$12.99", "", "")))
    _s, led, _m = _ledger(brand, ["Pigeon"], topic=("sippy",), subject="sippy cups")
    assert led["Pigeon"]["value"] == "$12.99"


def test_a_pack_row_still_gives_the_per_unit_price():
    brand = dict(BRAND, price_table=_tbl(
        ("Philips Avent", "3-pack", "exact", "$23.97", "", "3-pack, 9 oz")))
    _s, led, _m = _ledger(brand, ["Philips Avent"])
    assert "$7.99 each" in _format_price_value(led["Philips Avent"])


# ─────────────────────────────────────────── Change 4: "not published" drops the column
def test_a_not_published_brand_produces_no_pause_ask():
    brand = dict(BRAND, price_table=_tbl(("Dr. Brown's", "", "none", "", "", "")))
    stub, led, missing = _ledger(brand, ["Dr. Brown's"])
    assert missing == [] and stub.price_calls == []
    assert _norm_price_kind(led["Dr. Brown's"]["kind"]) == "none"


def test_the_price_column_is_dropped_for_everyone_and_every_brand_keeps_its_row():
    gen = BlogGenerator(StubClaude(), None)
    body = ("| Brand | Starting price | Material |\n|---|---|---|\n"
            "| Philips Avent | $23.97 | PP |\n| Dr. Brown's | — | Glass |\n")
    out, dropped = gen._strip_price_columns(body)
    assert dropped == ["Starting price"]
    assert "Philips Avent" in out and "Dr. Brown's" in out
    assert "$23.97" not in out and "| Brand | Material |" in out
    assert "not publicly listed" not in out.lower()


# ─────────────────────────────────────────── Change 5: compare exactly the brands you priced
def test_the_priced_set_reads_from_the_brand_and_excludes_the_subject():
    brand = dict(BRAND, price_table=_tbl(("Thyseed", "", "exact", "$29.99", "", ""),
                                         ("Pigeon", "", "from", "$12.99", "", ""),
                                         ("Philips Avent", "", "exact", "$23.97", "", "")))
    assert _priced_competitor_names(brand) == ["Pigeon", "Philips Avent"]
    assert _priced_competitor_names(BRAND) == [], "no table ⇒ inert"


def test_priced_matching_tolerates_how_a_brand_is_spelled():
    assert _is_priced("Philips Avent Natural", ["Philips Avent"])
    assert _is_priced("Pigeon", ["Pigeon"])
    assert _is_priced("Dr. Brown's", ["Dr Browns"]), "punctuation must not split a brand in two"
    assert not _is_priced("Comotomo", ["Pigeon", "Philips Avent"])
    assert not _is_priced("Rory", ["Ro"]), "matching on whole words, never a raw substring"


def test_the_brand_block_makes_the_priced_brands_the_field_and_is_inert_without_a_table():
    """FU253: three separate prompts carried the old "you may add more", and the model only has to
    believe one of them."""
    gen = BlogGenerator(StubClaude(), None)
    gen._priced_names = ["Pigeon", "Philips Avent"]
    _n, _u, block = gen._brand_block(BRAND)
    assert "THE COMPARISON FIELD" in block and "Pigeon, Philips Avent" in block
    assert "compare EXACTLY these and no other brand" in block
    assert "add none, even to reach a minimum" in block
    assert "at most" not in block
    gen._priced_names = []
    _n2, _u2, plain = gen._brand_block(BRAND)
    assert "THE COMPARISON FIELD" not in plain


def _article_prompt(brand, include_pricing=True):
    stub = StubClaude(call_handler=lambda p: {"title": "t", "body_markdown": "# t\n",
                                              "meta_description": "d"})
    gen = BlogGenerator(stub, None)
    gen.generate_article(brand, "best baby bottles", include_pricing=include_pricing)
    return next(p for p in stub.calls if "MINIMUM COMPETITORS" in p)


def test_the_writer_prompt_makes_the_priced_brands_the_whole_field():
    """FU253 (operator): "if I add prices for any competitors, only consider those competitors."
    The FU214 comment always said the price table IS the field and that padding it is what the
    table exists to stop — and the prompt then offered the model room for more."""
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", ""),
                                         ("Philips Avent", "", "exact", "$23.97", "", "")))
    p = _article_prompt(brand)
    assert "THE FIELD IS FIXED" in p and "Pigeon, Philips Avent" in p
    assert "Compare EXACTLY these and no others" in p
    assert "add NO other brand, even to reach a minimum" in p
    assert "never drop one of them to make room" in p
    assert "compared brands in total" not in p, "the prompt no longer offers room for more"


def test_the_writer_prompt_says_the_same_thing_however_many_you_priced():
    """There is no "full" case any more — one priced brand fixes the field as firmly as five."""
    for n in (1, _PRICED_FIELD_MAX):
        brand = dict(BRAND, price_table=_tbl(*[(f"Brand{i}", "", "exact", f"${i + 10}.00", "", "")
                                               for i in range(n)]))
        p = _article_prompt(brand)
        assert "Compare EXACTLY these and no others" in p and "add NO other brand" in p


def test_the_writer_prompt_is_byte_identical_without_a_table_and_with_pricing_off():
    plain = _article_prompt(BRAND)
    assert "HARD OVERRIDE" not in plain
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", "")))
    assert "HARD OVERRIDE" not in _article_prompt(brand, include_pricing=False)


def _sourcing(brand, extracted, include_pricing=True):
    def handler(p):
        if "core_topic" in p.lower():
            return {"tools": extracted, "peer_tools": extracted,
                    "dimensions": ["Starting price", "Material"], "claims": [],
                    "core_topic": "baby bottles", "products": [], "generic_options": []}
        return {}

    def search(brief, allowed, blocked):
        out = []
        for t in extracted:
            if t.lower().split()[0] in brief.lower():
                out.append({"title": t, "url": f"https://{t.lower().split()[0]}.example/p",
                            "fact": f"{t} bottles are BPA-free."})
        return out
    gen = BlogGenerator(StubClaude(call_handler=handler, search_handler=search,
                                   official_domain=lambda b, c: f"{b.lower().split()[0]}.example"),
                        None)
    gen._evidence_blocks = []
    return gen, gen._source_for_completion(brand, "best baby bottles",
                                           {"title": "t", "body_markdown": "# t\n"},
                                           include_pricing=include_pricing)


def test_the_priced_brands_ARE_the_field():
    """FU253 (operator): "if I add prices for any competitors, only consider those competitors."
    A field the operator defined must not grow names they did not choose — and every topped-up
    brand was one whose price then had to be found, which is the work the table exists to remove."""
    brand = dict(BRAND, price_table=_tbl(("Comotomo", "", "exact", "$19.99", "", ""),
                                         ("Pigeon", "", "from", "$12.99", "", "")))
    _g, s = _sourcing(brand, ["Pigeon", "Philips Avent", "Comotomo", "Dr. Brown's"])
    assert s["tools"] == ["Comotomo", "Pigeon"], "exactly what was priced, in the order pasted"
    assert s["priced_topups"] == []


def test_one_priced_brand_is_a_field_of_one():
    """It overrides the competitor floor too: padding a short field with an unpriced brand is
    exactly what the price table exists to stop."""
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", "")))
    _g, s = _sourcing(brand, ["Pigeon"] + [f"Other{i}" for i in range(8)])
    assert s["tools"] == ["Pigeon"]


def test_priced_brands_past_the_ceiling_are_cut_but_named():
    names = [f"Brand{i}" for i in range(_PRICED_FIELD_MAX + 2)]
    brand = dict(BRAND, price_table=_tbl(*[(n, "", "exact", f"${i + 10}.00", "", "")
                                           for i, n in enumerate(names)]))
    _g, s = _sourcing(brand, names)
    assert len(s["tools"]) == _PRICED_FIELD_MAX
    assert s["priced_over_cap"] == names[_PRICED_FIELD_MAX:]


def test_the_field_uses_the_name_YOU_priced_not_the_drafts_product_line():
    """FU262 — on a general question ("Best <category> for <use>") the BRAND is the unit of
    comparison, and pricing a brand is how the operator says so. This used to take the draft's name
    for the same brand instead, so a brand-level article came back comparing product lines the
    operator never named — and the pause then asked for a price PER PRODUCT LINE for brands whose
    price was already in the table."""
    brand = dict(BRAND, price_table=_tbl(("Dr. Brown's", "", "exact", "$9.99", "", ""),
                                         ("Philips Avent", "", "exact", "$12.95", "", "")))
    _g, s = _sourcing(brand, ["Dr. Brown's Anti-Colic Options+",
                              "Philips Avent Natural Response", "Comotomo"])
    assert s["tools"] == ["Dr. Brown's", "Philips Avent"], s["tools"]
    # the draft's variant is ABSORBED, not reported as a competitor that was left out
    assert "Dr. Brown's Anti-Colic Options+" not in (s.get("priced_excluded_mine") or [])


def test_an_exact_name_match_is_unchanged():
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", "")))
    _g, s = _sourcing(brand, ["Pigeon", "Philips Avent"])
    assert s["tools"] == ["Pigeon"]


def test_a_brand_you_priced_that_the_draft_never_named_is_still_compared():
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", ""),
                                         ("Comotomo", "", "exact", "$19.99", "", "")))
    _g, s = _sourcing(brand, ["Pigeon", "Philips Avent"])
    assert s["tools"] == ["Pigeon", "Comotomo"]
    assert "Philips Avent" not in s["tools"], "FU253: an unpriced brand the DRAFT named is dropped"


def test_your_own_competitor_is_excluded_by_a_price_table_but_NAMED():
    """Two operator instructions meet here: FU210 says your curated competitors are always compared,
    FU253 says a price table fixes the field. The price table is the more specific of the two, so it
    wins — and the one it leaves out is NAMED in the warnings rather than dropped quietly, which is
    the mechanism FU214 already built for a brand past the ceiling."""
    brand = dict(BRAND, manual_competitors=json.dumps(["Comotomo"]),
                 price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", "")))
    _g, s = _sourcing(brand, ["Pigeon", "Philips Avent", "Comotomo"])
    assert s["tools"] == ["Pigeon"]
    assert s["priced_excluded_mine"] == ["Comotomo"], "your competitor is named, not silently cut"


def test_a_priced_row_with_no_link_cites_the_brands_own_site():
    """Operator decision, and the rule FU158 already applies to an operator-set canonical price: a
    Sources entry that resolves to the brand beats one that resolves to nothing."""
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "from", "$12.99", "", "")),
                 competitor_domains=json.dumps({"Pigeon": "pigeon.com"}))
    _s, led, _m = _ledger(brand, ["Pigeon"])
    assert led["Pigeon"]["url"] == "https://pigeon.com"
    # no stored domain either → the domain the sourcing resolved for that tool
    brand2 = dict(BRAND, price_table=_tbl(("Comotomo", "", "exact", "$19.99", "", "")))
    _s2, led2, _m2 = _ledger(brand2, ["Comotomo"])
    assert led2["Comotomo"]["url"] == "https://comotomo.example"
    # a link you gave on the row always wins
    brand3 = dict(BRAND, competitor_domains=json.dumps({"Pigeon": "pigeon.com"}))
    brand3["price_table"] = json.dumps({"pigeon": {"name": "Pigeon", "rows": [
        {"product": "", "kind": "exact", "value": "$12.99", "value_max": "", "basis": "",
         "url": "https://pigeon.com/softouch", "raw": "", "updated_at": ""}]}})
    _s3, led3, _m3 = _ledger(brand3, ["Pigeon"])
    assert led3["Pigeon"]["url"] == "https://pigeon.com/softouch"


def test_the_subjects_canonical_price_falls_back_to_its_own_domain():
    db = _db()
    db.update_brand(1, domain_url="https://thyseed.com")
    _app._save_price_table(db, db.get_brand(1),
                           [{"brand": "Thyseed", "product": "9 oz", "kind": "exact",
                             "value": "$29.99"}])
    items = json.loads(db.get_brand(1)["key_facts"])["pricing"]["items"]
    assert items[0]["source_url"] == "https://thyseed.com"
    db.close()


def test_sourcing_is_unchanged_without_a_table_and_when_pricing_is_off():
    _g, a = _sourcing(BRAND, ["Pigeon", "Philips Avent", "Comotomo"])
    assert a["tools"] == ["Pigeon", "Philips Avent", "Comotomo"] and a["priced"] == []
    brand = dict(BRAND, price_table=_tbl(("Pigeon", "", "exact", "$12.99", "", "")))
    _g2, b = _sourcing(brand, ["Pigeon", "Philips Avent", "Comotomo"], include_pricing=False)
    assert b["tools"] == ["Pigeon", "Philips Avent", "Comotomo"] and b["priced"] == []


def _reconcile_prompt(sourcing):
    stub = StubClaude(call_handler=lambda p: {"body_markdown": "# t\n", "flagged": []})
    gen = BlogGenerator(stub, None)
    gen._evidence_blocks = []
    gen._reconcile_and_finish(BRAND, "best baby bottles",
                              {"title": "t", "body_markdown": "# t\n"},
                              {"fresh": [{"label": "Pigeon", "url": "https://p.example",
                                          "text": "x"}], **sourcing})
    return stub.calls[-1]


def test_the_reconcile_carries_the_priced_field_and_is_inert_without_it():
    p = _reconcile_prompt({"name": "Thyseed", "tools": ["Pigeon"], "dims": [],
                           "priced": ["Pigeon", "Philips Avent"]})
    assert "PRICED BY THE OPERATOR" in p and "PRICED FIELD" in p
    assert "never remove one" in p and f"at most {_PRICED_FIELD_MAX} compared" in p
    plain = _reconcile_prompt({"name": "Thyseed", "tools": ["Pigeon"], "dims": [], "priced": []})
    assert "PRICED BY THE OPERATOR" not in plain


def test_the_floor_yields_with_a_warning_that_names_your_excluded_competitor():
    gen = BlogGenerator(StubClaude(), None)
    gen._priced_names = ["Pigeon", "Philips Avent"]
    gen._priced_excluded_mine = ["Comotomo"]
    art = {"body_markdown": ("| Brand | Material |\n|---|---|\n| Thyseed | Glass |\n"
                             "| Pigeon | PP |\n| Philips Avent | PP |\n")}
    note = _floor_note(gen, art)
    assert "you priced 2 competitor(s)" in note and "below the floor of 3" in note
    assert "Comotomo" in note and "price" in note


def _floor_note(gen, art):
    """Drive only the FU105 competitor-count check inside `_finalize_article` (the rest of finalize
    needs a full pipeline)."""
    notes = []
    gen._warn = lambda a, n: notes.append(n)
    gen._finalize_article(BRAND, "best baby bottles", dict(art), art["body_markdown"])
    return next((n for n in notes if n.startswith("competitor-check:")), "")


# ─────────────────────────────────────────────────────────────── Change 1: storage semantics
def _db():
    d = tempfile.mkdtemp()
    db = Database(os.path.join(d, "t.db"))
    db.initialize()
    sid = db.create_subreddit("s", "d")
    sid2 = db.create_subreddit("s2", "d2")
    for s in (sid, sid2):
        db.conn.execute("INSERT INTO brands (subreddit_id,name,context) VALUES (?,?,?)",
                        (s, "Thyseed", "c"))
    db.conn.commit()
    return db


def test_the_column_round_trips_and_rows_save_to_every_copy_of_the_brand():
    db = _db()
    stored, dropped, flagged, canon = _app._save_price_table(
        db, db.get_brand(1),
        [{"brand": "Pigeon", "kind": "from", "value": "$12.99", "raw": "Pigeon from $12.99"}])
    assert "pigeon" in stored
    for bid in (1, 2):
        pt = _price_set_rows(db.get_brand(bid))
        assert pt["pigeon"]["rows"][0]["value"] == "$12.99"
    db.close()


def test_a_brand_sent_with_no_surviving_row_has_its_entry_removed():
    db = _db()
    _app._save_price_table(db, db.get_brand(1),
                           [{"brand": "Pigeon", "kind": "exact", "value": "$12.99"},
                            {"brand": "Comotomo", "kind": "exact", "value": "$19.99"}])
    _app._save_price_table(db, db.get_brand(1),
                           [{"brand": "Pigeon", "kind": "exact", "value": ""}])
    pt = _price_set_rows(db.get_brand(1))
    assert "pigeon" not in pt and "comotomo" in pt, "a brand you did not send is untouched"
    db.close()


def test_the_subjects_own_row_lands_in_key_facts_without_deleting_the_other_operator_prices():
    """The FU163 trap: the Edit Brand save treats what it is sent as the COMPLETE list and DELETES
    every operator price absent from it. The subject's rows go through the ADDITIVE upsert instead."""
    db = _db()
    db.update_brand(1, key_facts=json.dumps({"pricing": {"items": [
        {"product": "starter set", "value": "$49.99", "operator_set": True, "source_url": ""}]}}))
    _app._save_price_table(db, db.get_brand(1),
                           [{"brand": "Thyseed", "product": "9 oz bottle", "kind": "exact",
                             "value": "$29.99", "basis": "single"},
                            {"brand": "Pigeon", "kind": "exact", "value": "$12.99"}])
    kf = json.loads(db.get_brand(1)["key_facts"])
    items = {i["product"]: i for i in kf["pricing"]["items"]}
    assert "starter set" in items, "the operator's other price must survive"
    assert items["9 oz bottle"]["value"] == "$29.99 (single)"
    assert items["9 oz bottle"]["operator_set"] is True
    pt = _price_set_rows(db.get_brand(1))
    # FU260: the subject is KEPT in price_table as well as routed to key_facts. Popping it meant
    # everything built on that column skipped the publisher's own brand — its marked range could not
    # be stated, and its rows vanished from the price-table UI after saving.
    assert "thyseed" in pt and "pigeon" in pt
    # …and it is still not a COMPETITOR, which is what the pop was really protecting
    from generators.blog_gen import _priced_competitor_names
    assert _priced_competitor_names(db.get_brand(1)) == ["Pigeon"]
    db.close()


# ────────────────────────────────────────────────── the relaxed inline price editor (FU213 4f)
def _client():
    _app.app.config["TESTING"] = True
    return _app.app.test_client()


def test_the_inline_editor_stores_a_from_price_and_a_range():
    db = _db()
    orig = _app.get_db
    _app.get_db = lambda: db
    db.close_real = db.close
    db.close = lambda: None
    try:
        c = _client()
        r = c.put("/api/brands/1/competitor-prices",
                  json={"prices": [{"slug": "pigeon", "name": "Pigeon", "kind": "from",
                                    "value": "from $12.99"},
                                   {"slug": "comotomo", "name": "Comotomo", "kind": "exact",
                                    "value": "$19.99", "value_max": "$24.99"},
                                   {"slug": "drbrowns", "name": "Dr Browns", "kind": "none",
                                    "value": "no published price"}]})
        assert r.status_code == 200
        cf = json.loads(db.get_brand(1)["competitor_facts"])
        assert _format_price_value(cf["pigeon"]["price"]) == "From $12.99"
        assert _format_price_value(cf["comotomo"]["price"]) == "$19.99-$24.99"
        assert cf["drbrowns"]["price"]["kind"] == "none"
    finally:
        _app.get_db = orig
        db.close = db.close_real
        db.close()


def test_the_inline_editor_still_refuses_a_value_that_is_not_a_price():
    db = _db()
    orig = _app.get_db
    _app.get_db = lambda: db
    db.close_real = db.close
    db.close = lambda: None
    try:
        r = _client().put("/api/brands/1/competitor-prices",
                          json={"prices": [{"slug": "pigeon", "name": "Pigeon",
                                            "value": "call us"}]})
        assert r.status_code == 400 and "not a price figure" in r.get_json()["error"]
    finally:
        _app.get_db = orig
        db.close = db.close_real
        db.close()


# ─────────────────────────────────────────────────── Change 2: the endpoint, end to end ($0)
def _parse_endpoint(db, rows, text, names=None):
    """Drive POST /price-table/parse with a stubbed model, so the wiring (call → validation →
    response) is covered without a network call."""
    orig_db, orig_cc, orig_key = _app.get_db, _app.ClaudeClient, _app.ANTHROPIC_API_KEY
    _app.get_db = lambda: db
    _app.ClaudeClient = lambda *a, **k: StubClaude(call_handler=lambda p: {"rows": rows})
    _app.ANTHROPIC_API_KEY = "test-key"
    db.close_real, db.close = db.close, (lambda: None)
    try:
        return _client().post("/api/brands/1/price-table/parse",
                              json={"text": text, "names": names or [], "subject": "Thyseed"})
    finally:
        _app.get_db, _app.ClaudeClient, _app.ANTHROPIC_API_KEY = orig_db, orig_cc, orig_key
        db.close = db.close_real
        db.close()


def test_the_parse_endpoint_returns_reviewable_rows_and_the_reasons_it_dropped_the_rest():
    db = _db()
    r = _parse_endpoint(db, [
        {"brand": "Philips Avent", "product": "3-pack", "kind": "exact", "value": "$23.97",
         "basis": "3-pack, 9 oz", "raw": "Philips Avent 3-pack $23.97"},
        {"brand": "Pigeon", "kind": "from", "value": "$12.99", "raw": "Pigeon from $12.99"},
        {"brand": "Comotomo", "kind": "range", "value": "$19.99", "value_max": "$24.99",
         "raw": "Comotomo $19.99-$24.99"},
        {"brand": "Dr. Brown's", "kind": "none", "value": "", "raw": "Dr. Brown's doesn't publish a price"},
        {"brand": "Philips Avent", "kind": "exact", "value": "$99.00", "raw": "invented"},
    ], TEXT, names=["Philips Avent", "Pigeon", "Comotomo", "Dr. Brown's"])
    assert r.status_code == 200
    body = r.get_json()
    assert len(body["rows"]) == 4
    assert {x["brand"] for x in body["rows"]} == {"Philips Avent", "Pigeon", "Comotomo", "Dr. Brown's"}
    assert body["dropped"] and "$99.00" in body["dropped"][0]["why"]
    assert body["flagged"] == []
    kinds = {x["brand"]: x["kind"] for x in body["rows"]}
    assert kinds["Pigeon"] == "from" and kinds["Dr. Brown's"] == "none"


def test_the_parse_endpoint_refuses_an_empty_paste():
    db = _db()
    orig = _app.get_db
    _app.get_db = lambda: db
    db.close_real, db.close = db.close, (lambda: None)
    try:
        r = _client().post("/api/brands/1/price-table/parse", json={"text": "  "})
        assert r.status_code == 400
    finally:
        _app.get_db = orig
        db.close = db.close_real
        db.close()


def test_the_save_endpoint_stores_the_reviewed_rows_and_reports_what_it_dropped():
    db = _db()
    orig = _app.get_db
    _app.get_db = lambda: db
    db.close_real, db.close = db.close, (lambda: None)
    try:
        r = _client().put("/api/brands/1/price-table", json={"rows": [
            {"brand": "Pigeon", "kind": "from", "value": "$12.99"},
            {"brand": "Comotomo", "kind": "exact", "value": "call us"}]})
        assert r.status_code == 200
        body = r.get_json()
        assert body["rows"] == 1 and body["brands"] == 1
        assert body["dropped"] and "not a price figure" in body["dropped"][0]["why"]
        assert _price_set_rows(db.get_brand(1))["pigeon"]["rows"][0]["kind"] == "from"
    finally:
        _app.get_db = orig
        db.close = db.close_real
        db.close()


def test_one_not_published_brand_drops_the_column_even_after_the_other_cells_were_written():
    """The composition: code writes the verified cells, then a single "they publish no price" brand
    takes the whole column — a blank cell beside filled ones reads as "this product has none"."""
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    gen._price_ledger = {
        "Pigeon": {"value": "$12.99", "kind": "from", "basis": "single 240ml", "per_unit": "",
                   "url": "https://pigeon.example/p", "source": "yours",
                   "checked_at": "2026-09-18T00:00:00Z"},
        "Dr. Brown's": {"value": "", "kind": "none", "source": "yours",
                        "checked_at": "2026-09-18T00:00:00Z"}}
    body = ("# t\n\n| Brand | Starting price | Material |\n|---|---|---|\n"
            "| Pigeon | wrong | PP |\n| Dr. Brown's | $24.99 | Glass |\n")
    notes = []
    gen._warn = lambda a, n: notes.append(n)
    art = gen._finalize_article(BRAND, "best baby bottles",
                                {"title": "t", "body_markdown": body, "meta_description": ""}, body)
    out = art["body_markdown"]
    assert "Starting price" not in out and "$12.99" not in out and "$24.99" not in out
    assert "Pigeon" in out and "Dr. Brown's" in out, "every brand keeps its row"
    assert any("publishes no price" in n and "Dr. Brown's" in n for n in notes)
    assert "not publicly listed" not in out.lower()


# ────────────────── the two gaps the operator's real paste exposed
def test_a_range_typed_with_one_currency_symbol_survives_the_verbatim_gate():
    """"$28.99-29.99" is how a range is actually typed. The plain verbatim check only sees `$29.99`
    is absent and dropped the whole row; the pair-anchored check accepts it without loosening the
    rule — an upper bound that is NOT in the paste is still refused."""
    from generators.blog_gen import _range_in_text
    for tail in ("$28.99–$29.99", "$28.99–29.99", "$28.99 to 29.99"):
        clean, dropped, _f = _parse(
            [{"brand": "Comotomo", "kind": "range", "value": "$28.99", "value_max": "$29.99",
              "raw": f"Comotomo {tail}"}], f"Comotomo\t{tail}\tglass")
        assert not dropped, (tail, dropped)
        assert _format_price_value(clean["comotomo"]["rows"][0]) == "$28.99-$29.99"
    clean, dropped, _f = _parse(
        [{"brand": "Comotomo", "kind": "range", "value": "$28.99", "value_max": "$99.00",
          "raw": "x"}], "Comotomo\t$28.99–29.99\tglass")
    assert clean == {} and "$99.00" in dropped[0]["why"]
    assert not _range_in_text("$28.99", "$29.99", "$28.99 here and 29.99 somewhere else")


def test_the_note_under_the_table_says_where_the_prices_actually_came_from():
    """FU213's note claims each price came from the brand's own site or a named retailer. That is
    false for a price YOU supplied, and misdescribing a price's provenance is the exact failure this
    machinery exists to prevent."""
    gen = BlogGenerator(StubClaude(), None)
    gen._evidence_blocks = []
    yours = {"value": "$8.99", "kind": "exact", "source": "yours", "basis": "",
             "per_unit": "", "url": "", "checked_at": "2026-09-18T00:00:00Z"}
    found = {"value": "$12.99", "kind": "exact", "source": "retail", "basis": "",
             "per_unit": "", "url": "https://r.example/p", "checked_at": "2026-09-18T00:00:00Z"}
    tbl = ("| Brand | Starting price |\n|---|---|\n| Dr. Brown's | ? |\n| Pigeon | ? |\n")
    only_yours, _n = gen._write_price_cells(tbl, {"Dr. Brown's": yours})
    assert "supplied by the publisher" in only_yours
    assert "own site or a named retailer" not in only_yours
    mixed, _n2 = gen._write_price_cells(tbl, {"Dr. Brown's": yours, "Pigeon": found})
    assert "own site or a named retailer" in mixed and "supplied by the publisher" in mixed
    only_found, _n3 = gen._write_price_cells(tbl, {"Pigeon": found})
    assert "own site or a named retailer" in only_found
    assert "supplied by the publisher" not in only_found, "the FU213 wording is unchanged"
    # the note is still added exactly once
    twice, _n4 = gen._write_price_cells(only_yours, {"Dr. Brown's": yours})
    assert twice.count("supplied by the publisher") == 1


def test_a_price_the_basis_already_calls_per_unit_is_not_divided_again():
    """From the operator's real paste: a per-bottle column whose basis mentions the 2-pack. Dividing
    an already-per-unit figure states a price nobody charges."""
    from generators.blog_gen import _per_unit_price
    assert _per_unit_price("$23.97", "3-pack, 9 oz") == "$7.99 each"
    assert _per_unit_price("$17.50", "approx., per bottle in a 2-pack ($34.99)") == ""
    assert _per_unit_price("$12.00", "per seat, 5 seats billed annually") == ""
    assert _per_unit_price("$34.99", "2-pack wide-neck") == "$17.50 each"
