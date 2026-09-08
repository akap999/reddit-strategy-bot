"""FU160 — competitor-cache management: per-competitor SELECTIVE refresh, 45-day TTL, and the
delete + "clear all cached data" reset endpoints. $0, no network (StubClaude for sourcing; a Flask
test client on a temp DB for the endpoints)."""
import json
import os
import tempfile
import time

from generators.blog_gen import BlogGenerator, _COMPETITOR_FACTS_TTL_DAYS
from db import Database
from tests.stubs import StubClaude


def _iso(days_ago=0):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - days_ago * 86400))


def _cf(noom_days=0, ro_days=0):
    # the cached text carries the "pricing" dimension word so the orthogonal FU142 dim-rescue treats
    # the pricing column as satisfied and doesn't confound the cache assertions.
    return {
        "noom": {"domain": "noom.com", "verified_at": _iso(noom_days),
                 "blocks": [{"label": "Noom", "url": "https://noom.com/x", "text": "CACHED-NOOM pricing $99/mo"}]},
        "ro": {"domain": "ro.com", "verified_at": _iso(ro_days),
               "blocks": [{"label": "Ro", "url": "https://ro.com/x", "text": "CACHED-RO pricing $145/mo"}]},
    }


def _brand(cf):
    return {"name": "Acme", "domain_url": "https://acme.com", "category": "telehealth",
            "competitor_facts": json.dumps(cf)}


def _gen():
    def call_h(p):
        if '"peer_tools"' in p and '"dimensions"' in p:
            return {"tools": ["Noom", "Ro"], "peer_tools": ["Noom", "Ro"], "dimensions": ["pricing"],
                    "products": [], "claims": [], "core_topic": "telehealth"}
        if '"domains"' in p:
            return {"domains": {"Noom": "noom.com"}}
        return {}

    def search_h(brief, allowed, blocked):
        # a distinctive LIVE block only for a Noom OWN-DOMAIN sourcing search (Pass A tier1) → proves
        # Noom was actually re-sourced; broad corroboration/dim searches (allowed=None) return nothing.
        if allowed and any("noom.com" in str(a).lower() for a in allowed):
            return [{"url": "https://noom.com/pricing", "fact": "LIVE-NOOM $299/mo", "title": "Noom pricing"}]
        return []
    return BlogGenerator(StubClaude(call_handler=call_h, search_handler=search_h), db=None)


def _fresh_texts(sourcing):
    return " ".join((b.get("text") or "") for b in (sourcing.get("fresh") or []))


def _source(gen, brand, **kw):
    body = "| Clinic | Pricing |\n|---|---|\n| Noom | ? |\n| Ro | ? |\n"
    return gen._source_for_completion(brand, "best telehealth clinics", {"body_markdown": body},
                                      ymyl=None, **kw)


# --- Changes 1/2: selective per-slug refresh ------------------------------------------------------
def test_selective_refresh_only_bypasses_selected():
    t = _fresh_texts(_source(_gen(), _brand(_cf()), refresh_competitor_slugs=["noom"]))
    assert "LIVE-NOOM" in t          # Noom (selected) re-sourced live
    assert "CACHED-RO" in t          # Ro (not selected) kept from cache
    assert "CACHED-NOOM" not in t    # Noom's stale cache NOT used


def test_refresh_all_bypasses_every_cache():
    t = _fresh_texts(_source(_gen(), _brand(_cf()), refresh_competitor_facts=True))
    assert "CACHED-NOOM" not in t and "CACHED-RO" not in t   # both re-sourced
    assert "LIVE-NOOM" in t


def test_no_refresh_uses_all_cache():
    t = _fresh_texts(_source(_gen(), _brand(_cf())))
    assert "CACHED-NOOM" in t and "CACHED-RO" in t and "LIVE-NOOM" not in t


# --- Change 7: 45-day TTL -------------------------------------------------------------------------
def test_ttl_is_45_days_and_governs_freshness():
    assert _COMPETITOR_FACTS_TTL_DAYS == 45.0
    # 30 days old → FRESH under 45d (was stale at 21) → cache HIT
    assert "CACHED-NOOM" in _fresh_texts(_source(_gen(), _brand(_cf(noom_days=30, ro_days=30))))
    # 50 days old → STALE → re-sourced (Ro still fresh)
    t = _fresh_texts(_source(_gen(), _brand(_cf(noom_days=50, ro_days=0))))
    assert "CACHED-NOOM" not in t and "LIVE-NOOM" in t and "CACHED-RO" in t


# --- Changes 5/8: delete + reset endpoints (Flask test client on a temp DB) ----------------------
def _app_client(tmp_db):
    import app as appmod
    appmod.DB_PATH = tmp_db
    appmod._db_initialized = True   # skip cron/karma/init in get_db — we init the temp DB ourselves
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


def test_delete_endpoint_removes_one_slug():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        bid = _seed_brand(path, competitor_facts=json.dumps(_cf()))
        client = _app_client(path)
        r = client.post(f"/api/brands/{bid}/competitor-cache/delete", json={"slugs": ["noom"]})
        assert r.status_code == 200 and r.get_json()["removed"] == ["noom"]
        db = Database(path)
        db.connect()
        cf = json.loads(db.get_brand(bid)["competitor_facts"])
        db.close()
        assert "noom" not in cf and "ro" in cf   # only the selected entry removed
    finally:
        os.remove(path)


def test_reset_endpoint_clears_caches_keeps_operator_pricing_and_identity():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        kf = {"pricing": {"items": [
            {"product": "tirzepatide", "value": "$647 / 3 months", "operator_set": True},
            {"product": "", "value": "$79/mo", "source_url": "https://x.com/mens-trt/"}]}}  # auto-synced
        bid = _seed_brand(path, name="PeterMD", competitor_facts=json.dumps(_cf()),
                          competitor_domains=json.dumps({"Noom": "noom.com"}),
                          key_facts=json.dumps(kf), personas=json.dumps([{"label": "P"}]))
        client = _app_client(path)
        r = client.post(f"/api/brands/{bid}/reset-cached-data", json={})
        assert r.status_code == 200 and r.get_json()["cleared"]["auto_pricing_items_removed"] == 1
        db = Database(path)
        db.connect()
        brand = db.get_brand(bid)
        db.close()
        assert json.loads(brand["competitor_facts"]) == {}      # competitor cache wiped
        assert json.loads(brand["competitor_domains"]) == {}    # auto-resolved domains wiped
        items = json.loads(brand["key_facts"])["pricing"]["items"]
        assert len(items) == 1 and items[0].get("operator_set") and "647" in items[0]["value"]  # kept
        assert brand["name"] == "PeterMD" and json.loads(brand["personas"])   # identity untouched
    finally:
        os.remove(path)
