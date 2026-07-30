"""FU105 — minimum-3-competitors floor ($0, no network).

Covers the four FU105 seams:
  1. generate_article prompt carries the AT-LEAST-3 floor (ENTITY-TYPE + the new MINIMUM
     COMPETITORS bullet + its inert no-comparison clause) with the FU98 wording intact.
  2. The FU98 peer-discovery brief now asks for AT LEAST 3 competitors.
  3. The reconcile prompt carries the COMPETITOR FLOOR rule + the protected PEERS line.
  4. The deterministic _finalize_article competitor-check: <3 competitor rows in the FINAL
     body's table -> warning appended to geo_warning (never clobbering an existing note);
     >=3 rows or no table -> silent; subject-row matching is word-boundary.
"""
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude


BRAND = {"id": 1, "name": "Acme", "domain_url": "https://acme.com", "category": "AI SEO platform",
         "competitors": '["CompA", "CompB", "CompC"]'}
SEED = "best AI SEO platforms for small teams"


def _gen(handler=None, search_handler=None):
    return BlogGenerator(StubClaude(call_handler=handler, search_handler=search_handler), db=None)


# --- 1. writer prompt floor -------------------------------------------------------------------

def test_generate_article_prompt_has_min3_floor():
    gen = _gen(handler=lambda p: {"title": SEED, "meta_description": "m", "keywords": [],
                                  "body_markdown": "## Quick answer\nx", "disclosure": "d"})
    gen.generate_article(BRAND, SEED)
    prompt = next(p for p in gen.claude.calls if "ENTITY-TYPE MATCH" in p)
    flat = " ".join(prompt.split())   # collapse the prompt's line-wrapping
    # the FU98 rule now carries the numeric floor
    assert "AT LEAST 3 real entities of THAT type besides" in flat
    # the new FU105 bullet + its inert clause
    assert "MINIMUM COMPETITORS (FU105" in flat
    assert "AT LEAST 3 REAL competitors" in flat
    assert "Skip this rule ONLY when the article genuinely contains no comparison" in flat
    # FU98 regression wording intact
    assert "self-crowning comparison" in flat
    assert "clearly-labeled supplementary category" in flat


# --- 2. peer-discovery brief asks for a count -------------------------------------------------

def test_peer_discovery_brief_asks_for_at_least_3():
    gen = _gen(search_handler=lambda brief, allowed, blocked: [])
    gen._gather_independent_sources("Acme", ["CompA"], SEED, "AI SEO platform", ["acme.com"])
    peer_briefs = [s["brief"] for s in gen.claude.searches if "DIRECT COMPETITORS" in s["brief"]]
    assert peer_briefs, "peer-discovery brief never issued"
    assert "AT LEAST 3" in peer_briefs[0]


# --- 3. reconcile prompt: floor rule + protected PEERS ----------------------------------------

def test_reconcile_prompt_has_competitor_floor_and_peers():
    gen = _gen(handler=lambda p: {"revised_body_markdown": "## x\nbody", "flagged": []})
    sourcing = {"name": "Acme", "cat": "AI SEO platform",
                "tools": ["CompA", "CompB", "CompC"], "peers": ["CompA", "CompB"],
                "dims": ["Price"], "claims": [], "core_topic": "",
                "fresh": [{"label": "CompA", "url": "https://compa.com", "text": "fact"}],
                "unsourced": [], "geo": "", "qualifier": ""}
    art = {"body_markdown": "## Quick answer\nx\n\n| Tool | Price |\n| --- | --- |\n| Acme | $1 |"}
    gen._reconcile_and_finish(BRAND, SEED, art, sourcing)
    prompt = next(p for p in gen.claude.calls if "COMPETITOR FLOOR" in p)
    flat = " ".join(prompt.split())   # collapse the prompt's line-wrapping
    assert "COMPETITOR FLOOR (FU105)" in flat
    assert "KEEP AT LEAST 3 non-Acme competitors" in flat
    assert "fewer than 3 same-type competitors remain" in flat            # amended FU98 tail
    assert 'PEERS (same-type competitors — protected' in flat
    assert '"CompA"' in flat and '"CompB"' in flat                        # the peers JSON rides in


def test_source_for_completion_returns_peers():
    def handler(p):
        if "PEER_TOOLS" in p:   # the extraction call
            return {"tools": ["CompA", "CompB"], "peer_tools": ["CompA", "Acme"],
                    "dimensions": ["Price"], "core_topic": "", "claims": []}
        return {}
    gen = _gen(handler=handler)
    sourcing = gen._source_for_completion(BRAND, SEED,
                                          {"body_markdown": "## x\n| T | P |\n| - | - |\n| CompA | 1 |"},
                                          deep=False)
    assert sourcing is not None
    # subject filtered out of the protected set; the real peer kept
    assert sourcing.get("peers") == ["CompA"]


# --- 4. deterministic competitor-count check --------------------------------------------------

def _finalize(body, geo_warning=None):
    gen = _gen(handler=lambda p: {})
    art = {"body_markdown": body, "meta_description": "m"}
    if geo_warning:
        art["geo_warning"] = geo_warning
    # draft == final: substance guard is a no-op; seed has no geo/qualifier -> those checks inert
    return gen._finalize_article(BRAND, "how to compare platforms", art, body)


TABLE_2_COMP = """# how to compare platforms

## Comparison
| Tool | Price |
| --- | --- |
| Acme | $10 |
| CompA | $12 |
| CompB | $15 |

## FAQ
### Q?
A.
"""

TABLE_3_COMP = TABLE_2_COMP.replace("| CompB | $15 |", "| CompB | $15 |\n| CompC | $20 |")


def test_competitor_check_warns_under_3():
    art = _finalize(TABLE_2_COMP)
    assert "competitor-check" in (art.get("geo_warning") or "")
    assert "only 2 competitor row(s)" in art["geo_warning"]


def test_competitor_check_joins_after_existing_note():
    art = _finalize(TABLE_2_COMP, geo_warning="geo-check: existing note")
    assert art["geo_warning"].startswith("geo-check: existing note; ")
    assert "competitor-check" in art["geo_warning"]


def test_competitor_check_silent_at_3():
    art = _finalize(TABLE_3_COMP)
    assert "competitor-check" not in (art.get("geo_warning") or "")


def test_competitor_check_silent_without_table():
    art = _finalize("# how to compare platforms\n\n## Quick answer\nNo table here.\n")
    assert "competitor-check" not in (art.get("geo_warning") or "")


def test_competitor_check_subject_match_is_word_boundary():
    # "Acmeify" must NOT be mistaken for the subject "Acme" -> counts as a competitor row
    body = TABLE_2_COMP.replace("| CompB | $15 |", "| CompB | $15 |\n| Acmeify | $9 |")
    art = _finalize(body)   # CompA + CompB + Acmeify = 3 competitors -> silent
    assert "competitor-check" not in (art.get("geo_warning") or "")
