"""FU163 — a NAMELESS general price must not coexist with NAMED products (the stuck "$79 flexible
plans" item), canonical-pricing deletion actually deletes, and a generic .org no longer earns the
`official ·` badge (telehealth.org). $0, no network (StubClaude + a Flask test client on a temp DB)."""
import json
import os
import tempfile

from generators.blog_gen import (BlogGenerator, build_blog_jsonld, _canonical_facts_block,
                                  _drop_nameless_when_named, _official_source_ok,
                                  _YMYL_OFFICIAL_DOMAINS)
from db import Database
from tests.stubs import StubClaude


BRAND = {"id": 1, "name": "Acme", "domain_url": "https://acme.com", "category": "telehealth"}


def _gen(call_handler=None, search_handler=None, db=None):
    return BlogGenerator(StubClaude(call_handler=call_handler, search_handler=search_handler), db=db)


class _FakeDB:
    def __init__(self):
        self.updates = []

    def update_brand(self, brand_id, **kw):
        self.updates.append((brand_id, kw))


def _stored_items(db):
    return json.loads(db.updates[-1][1]["key_facts"])["pricing"]["items"]


# --- pure helper -----------------------------------------------------------------------------
def test_drop_nameless_when_named():
    items = [{"product": "", "value": "$79"}, {"product": "tirzepatide", "value": "$149"}]
    kept, dropped = _drop_nameless_when_named(items)
    assert dropped and [i["product"] for i in kept] == ["tirzepatide"]
    only = [{"product": "", "value": "$29"}]                 # general-only brand → untouched
    kept2, dropped2 = _drop_nameless_when_named(only)
    assert not dropped2 and kept2 == only


# --- A1: the sync self-heals the nameless general item away when named products exist ----------
def test_sync_drops_general_when_named_exists():
    db = _FakeDB()
    gen = _gen(db=db)   # evidence="" → early-return path (no claude call), but the choke-point drop runs
    brand = dict(BRAND, key_facts=json.dumps({"pricing": {"items": [
        {"product": "", "value": "PeterMD flexible plans, as low as $79/mo", "operator_set": True},
        {"product": "tirzepatide", "value": "$149/mo", "operator_set": True}]}}))
    gen._resolve_and_sync_key_facts(brand, evidence="", seed="tirzepatide clinics")
    items = _stored_items(db)                                 # persisted because a general item dropped
    assert all(str(i.get("product") or "").strip() for i in items)   # no nameless item survives
    assert any(i["product"] == "tirzepatide" for i in items)
    assert not any("$79" in (i.get("value") or "") for i in items)


def test_sync_keeps_general_when_only_general():
    db = _FakeDB()
    gen = _gen(db=db)
    brand = dict(BRAND, key_facts=json.dumps({"pricing": {"items": [
        {"product": "", "value": "$29/mo flat", "operator_set": True}]}}))
    gen._resolve_and_sync_key_facts(brand, evidence="", seed="x")
    assert db.updates == []                                   # nothing dropped → no persist (general kept)


def test_sync_skips_general_search_when_named_exists():
    searches = []

    def search_handler(brief, allowed, blocked):
        searches.append(brief)
        return []
    gen = _gen(call_handler=lambda p: {"items": [], "seed_product": ""},   # generic seed, nothing fresh
               search_handler=search_handler, db=_FakeDB())
    brand = dict(BRAND, key_facts=json.dumps({"pricing": {"items": [
        {"product": "tirzepatide", "value": "$149/mo", "operator_set": True}]}}))
    gen._resolve_and_sync_key_facts(brand, evidence="Acme homepage text", seed="best clinics")
    # target=="" (general) AND a named item exists → the "{name} pricing" general search must NOT fire
    assert searches == []


# --- A3: JSON-LD + writer prompt never surface a nameless general price beside named ones -------
def test_jsonld_no_general_offer_beside_named():
    kf = {"pricing": {"items": [
        {"product": "", "value": "flexible plans as low as $79/mo", "operator_set": True,
         "source_url": "https://acme.com/"},
        {"product": "tirzepatide", "value": "$149/mo", "operator_set": True,
         "source_url": "https://acme.com/"}]}}
    brand = {"name": "Acme", "domain_url": "https://acme.com", "key_facts": json.dumps(kf)}
    blog = {"title": "GLP-1 programs compared", "seed": "which platforms offer glp-1 programs",
            "meta_description": "d", "created_at": "2026-01-01T00:00:00", "updated_at": "2026-01-02T00:00:00",
            "body_markdown": "# T\n\n## Quick answer\nx\n"}
    graph = build_blog_jsonld(blog, brand)["@graph"]
    prod = next((n for n in graph if n.get("@type") == "Product"), None)
    assert prod is not None
    offers = prod["offers"] if isinstance(prod["offers"], list) else [prod["offers"]]
    assert "79" not in [o.get("price") for o in offers]           # nameless general $79 dropped
    assert any(o.get("name") == "tirzepatide" for o in offers)    # the named product Offer kept


def test_canonical_block_omits_general_when_named():
    kf = {"pricing": {"items": [
        {"product": "", "value": "flexible plans as low as $79/mo"},
        {"product": "tirzepatide", "value": "$149/mo"}]}}
    blk = _canonical_facts_block("Acme", kf, seed_products=["tirzepatide"])
    assert "$79" not in blk
    assert "tirzepatide" in blk and "$149/mo" in blk


# --- B: `official ·` requires a recognized authority; a generic .org does not qualify ----------
def test_official_source_authority_allowlist():
    pins = _YMYL_OFFICIAL_DOMAINS["medical"]
    # telehealth.org — an industry/education .org — is NOT official
    assert not _official_source_ok(
        "https://telehealth.org/news/fda-glp-1-compounding-crackdown/",
        "FDA GLP-1 Compounding Crackdown | Telehealth.org", "PeterMD", "getpetermd.com", pins)
    # recognized authority societies (www stripped) ARE official
    assert _official_source_ok("https://www.ama-assn.org/x",
                               "Doctors are fighting to boost access — AMA", "PeterMD", "getpetermd.com", pins)
    assert _official_source_ok("https://www.auanet.org/guideline",
                               "Testosterone Deficiency Guideline", "PeterMD", "getpetermd.com", pins)
    # strong tier unchanged
    assert _official_source_ok("https://www.fda.gov/x", "FDA label", "PeterMD", "getpetermd.com", pins)
    assert _official_source_ok("https://dailymed.nlm.nih.gov/x", "DailyMed", "PeterMD", "getpetermd.com", pins)
    # a review-shaped .org affiliate about the client is still NOT official
    assert not _official_source_ok("https://paigesimple.org/petermd-review",
                                   "PeterMD Review — Is It Worth It?", "PeterMD", "getpetermd.com", pins)


# --- A2: the canonical-pricing save is AUTHORITATIVE for operator items (real deletion) --------
def _app_client(tmp_db):
    import app as appmod
    appmod.DB_PATH = tmp_db
    appmod._db_initialized = True
    return appmod.app.test_client()


def _seed_brand(path, **cols):
    db = Database(path)
    db.connect()
    db.initialize()
    sub = db.ensure_live_subreddit("t")
    bid = db.add_brand(sub["id"], cols.pop("name", "Acme"))
    if cols:
        db.update_brand(bid, **cols)
    db.close()
    return bid


def _brand_items(path, bid):
    db = Database(path)
    db.connect()
    kf = json.loads(db.get_brand(bid).get("key_facts") or "{}")
    db.close()
    return (kf.get("pricing") or {}).get("items") or []


def test_pricing_save_deletes_removed_operator_item():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        kf = {"pricing": {"items": [
            {"product": "tirzepatide", "value": "$149/mo", "operator_set": True},
            {"product": "GLP1", "value": "$99/mo", "operator_set": True}]}}
        bid = _seed_brand(path, name="Acme", domain_url="https://acme.com", key_facts=json.dumps(kf))
        client = _app_client(path)
        # save the textarea with ONLY tirzepatide (operator removed the GLP1 line)
        r = client.put(f"/api/brands/{bid}",
                       json={"key_facts_pricing_items": [{"product": "tirzepatide", "value": "$149/mo"}]})
        assert r.status_code == 200
        prods = [i.get("product") for i in _brand_items(path, bid)]
        assert "tirzepatide" in prods and "GLP1" not in prods   # the removed operator line is DELETED
    finally:
        os.remove(path)


def test_pricing_save_keeps_non_operator_item_and_drops_nameless_general():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        kf = {"pricing": {"items": [
            {"product": "tirzepatide", "value": "$149/mo", "operator_set": True},
            {"product": "", "value": "flexible plans $79/mo", "operator_set": True},   # nameless general
            {"product": "semaglutide", "value": "$199/mo"}]}}                            # non-operator (auto)
        bid = _seed_brand(path, name="Acme", domain_url="https://acme.com", key_facts=json.dumps(kf))
        client = _app_client(path)
        r = client.put(f"/api/brands/{bid}",
                       json={"key_facts_pricing_items": [{"product": "tirzepatide", "value": "$149/mo"}]})
        assert r.status_code == 200
        prods = [i.get("product") for i in _brand_items(path, bid)]
        assert "tirzepatide" in prods
        assert "semaglutide" in prods          # a NON-operator (auto-synced) item is preserved
        assert "" not in prods                 # the nameless general $79 item is dropped (named exist)
    finally:
        os.remove(path)
