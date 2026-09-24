"""FU286 — BRAND MENTION mode for Live-Subreddit post generation.

Every post path in this file was built on one rule: the post never names the target brand. The
brand arrives later, in a seeded comment. The operator asked for the opposite as an opt-in toggle —
a post whose BODY names the brand once — needed above all for custom (typed-title) posts, with two
constraints of their own: the MODEL decides where and how the mention lands, and no OTHER company
may be named anywhere in the post, comparison intent included.

The hard part was not adding the block. The no-target-brand rule is stated SEVEN times across the
shared body guidance, the general-style overlay (twice), the brand-context header, and all three
intent tails. A block bolted on the end would have sat under four statements of its own opposite,
and a brief that contradicts itself is a coin toss — that is exactly the failure this project just
spent FU274-FU284 digging out of on the blog side. So every body-scoped ban is rendered from the
mode, and the tests below assert the contradictions are GONE, not merely that the block is present.

The title-scoped bans stay: the brand is never named in a title, in either mode.
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.post_gen import (  # noqa: E402
    PostGenerator,
    _body_guidance,
    _general_style_block,
    body_names_target,
)
from tests.stubs import StubClaude  # noqa: E402

BRAND = {
    "name": "Jolly SEO",
    "category": "GEO agency",
    "audience": "B2B SaaS marketers",
    "use_cases": ["reddit seeding"],
    "pain_points": ["no AI visibility"],
    "features": ["thread seeding"],
    "competitors": ["Profound", "Peec AI"],
    "context": "Jolly SEO runs GEO campaigns.",
    "keywords": ["geo"],
}
SUB = {"id": 1, "name": "SEO", "domain": "search marketing"}

# Any clause that tells the model not to name the target brand. The TITLE-scoped ones are excluded
# by the caller, because those hold in both modes.
_BAN_RE = re.compile(
    r"never (?:mention|name|write|output)[^.\n]*brand"
    r"|NEVER mention the target"
    r"|name a specific product/tool/brand"
    r"|never brand names",
    re.IGNORECASE)


def _body_scoped_bans(prompt):
    """Every no-target-brand clause in `prompt` that is NOT about the title.

    Scoped to the match's OWN line, deliberately. A ±120-char window looks safer and is worse:
    both legitimate title bans sit directly beside body text ("... in the title, and never" /
    "NEVER name the brand in the TITLE — the title rules above are unchanged"), so a window
    quietly excused every ban within two lines of them — which is where they all live.
    """
    out = []
    for m in _BAN_RE.finditer(prompt):
        line_start = prompt.rfind("\n", 0, m.start()) + 1
        line_end = prompt.find("\n", m.end())
        line = prompt[line_start:line_end if line_end != -1 else len(prompt)]
        if "title" in line.lower():
            continue
        out.append(m.group(0))
    return out


def _candidates_prompt(intent, brand_mention, general=False, entropy=False):
    claude = StubClaude(call_handler=lambda p: {"posts": []})
    PostGenerator(claude, None)._generate_candidates_for_intent(
        SUB, [BRAND], intent, ["question"], [], 3,
        general=general, entropy=entropy, brand_mention=brand_mention)
    return claude.calls[-1]


def _topic_prompt(brand_mention, general=False, entropy=False):
    claude = StubClaude(call_handler=lambda p: {
        "body": "x", "intent": "commercial", "storyline": "question"})
    PostGenerator(claude, None).generate_post_from_topic(
        SUB, BRAND, "best geo agency for b2b saas?", [],
        general=general, entropy=entropy, brand_mention=brand_mention)
    return claude.calls[0]   # call 1 is the separate scoring pass


def _regen_prompt(brand_mention):
    claude = StubClaude(call_handler=lambda p: {"body": "x"})
    PostGenerator(claude, None).regenerate_body(
        {"title": "t", "storyline": "question"}, [BRAND], brand_mention=brand_mention)
    return claude.calls[0]


# ── the mode is OFF by default, everywhere ────────────────────────────────────────────────────
def test_default_is_unchanged_on_every_path():
    """`all other controls should work as is` — with no flag, every prompt still bans the brand."""
    for intent in ("commercial", "comparison", "informational"):
        p = _candidates_prompt(intent, brand_mention=False)
        assert "BRAND MENTION MODE" not in p, intent
        assert _body_scoped_bans(p), f"{intent}: the default lost its no-brand rule"
    for p in (_topic_prompt(False), _regen_prompt(False)):
        assert "BRAND MENTION MODE" not in p
        assert _body_scoped_bans(p)


# ── the mode ON: no clause anywhere still says the opposite ───────────────────────────────────
def test_no_surviving_contradiction_in_any_intent():
    """The block is worthless if the brief also says 'never name the brand' four more times."""
    for intent in ("commercial", "comparison", "informational"):
        p = _candidates_prompt(intent, brand_mention=True, general=True, entropy=True)
        assert "BRAND MENTION MODE" in p, intent
        assert f"NAME the TARGET brand in the BODY, exactly once: {BRAND['name']}" in p, intent
        assert _body_scoped_bans(p) == [], f"{intent}: still contradicts itself: " \
                                           f"{_body_scoped_bans(p)}"


def test_title_ban_survives_in_mention_mode():
    """The brand is named in the BODY only — the title rule is untouched in both modes."""
    p = _candidates_prompt("commercial", brand_mention=True)
    assert "NEVER write the target brand name in the title" in p
    assert "NEVER name the brand in the TITLE" in p


def test_comparison_loses_its_competitor_licence():
    """Comparison normally names rivals. Naming ours beside three of theirs reads as an ad, and
    hands them the same retrievable mention — so the licence flips off with the mode."""
    off = _candidates_prompt("comparison", brand_mention=False)
    on = _candidates_prompt("comparison", brand_mention=True)
    assert "COMPETITORS: Profound, Peec AI" in off
    assert "You MAY (and should) name a competitor brand" in off
    assert "COMPETITORS: Profound, Peec AI" not in on
    assert "You MAY (and should) name a competitor brand" not in on
    assert "must NOT name a competitor" in on
    # and the title-angle suggestion stops recommending a named competitor
    assert "switching from a\nnamed competitor" in off
    assert "named competitor" not in on


def test_shared_body_guidance_drops_only_the_brand_ban():
    """One line of the shared body rules inverts; the other ~10 must be identical."""
    default, mention = _body_guidance(False), _body_guidance(True)
    assert "Never mention the target brand name in the body." in default
    assert "Never mention the target brand name in the body." not in mention
    assert default.replace(" Never mention the target brand name in the body.", "") == mention


def test_general_overlay_flips_both_of_its_brand_clauses():
    """General mode states the ban twice — as 'every rule above still applies', and as a body
    bullet. Either one left standing contradicts the mode."""
    default, mention = _general_style_block(False), _general_style_block(True)
    assert "still never names the target brand" in default
    assert "name a specific product/tool/brand" in default
    assert "never names the target brand" not in mention
    assert "name a specific product/tool/brand" not in mention
    assert "BRAND MENTION MODE" in mention
    # everything else about the overlay is untouched
    assert len(mention) - len(default) < 200
    assert "ANTI-FINGERPRINT" not in mention
    assert "SENTENCE-LENGTH VARIANCE" not in mention


def test_general_mode_uses_the_flipped_overlay_on_both_paths():
    """The overlay reaches the prompt through two call sites; a missed one reintroduces the ban."""
    for p in (_candidates_prompt("commercial", brand_mention=True, general=True),
              _topic_prompt(True, general=True)):
        assert "GENERAL MODE" in p
        assert _body_scoped_bans(p) == []


# ── the custom-title path, which is what the operator asked for specifically ───────────────────
def test_custom_title_path_is_body_only():
    p = _topic_prompt(True)
    assert "BRAND MENTION MODE — applies to the BODY ONLY" in p
    # the block ITSELF, not just the sentence introducing it
    assert f"Name {BRAND['name']} in the BODY, once." in p
    assert "NO OTHER COMPANY, PRODUCT, TOOL OR SERVICE MAY BE NAMED" in p
    assert f"NAME the TARGET brand in the BODY exactly once: {BRAND['name']}" in p
    assert _body_scoped_bans(p) == []
    # comparison's competitor licence is described inline on this path too
    assert "competitor names allowed" not in p


def test_regenerate_keeps_the_mode_it_is_given():
    on, off = _regen_prompt(True), _regen_prompt(False)
    assert "BRAND MENTION MODE" in on
    assert f"NAME the target brand once in the body: {BRAND['name']}" in on
    assert _body_scoped_bans(on) == []
    assert "BRAND MENTION MODE" not in off
    assert f"NEVER name the target brand(s): {BRAND['name']}" in off


# ── body_names_target: the record of which mode a stored post was written in ───────────────────
def test_body_names_target_matches_the_real_shapes():
    assert body_names_target("we ended up on Jolly SEO for this", [BRAND])
    assert body_names_target("tried jolly seo last year", [BRAND])          # case
    assert body_names_target("saw Jolly  SEO mentioned", [BRAND])           # double space
    assert body_names_target("someone said Jolly\nSEO was decent", [BRAND])  # wrapped
    assert body_names_target("Jolly SEO", BRAND)                            # bare dict
    assert body_names_target("we looked at Profound", ["Profound"])          # bare string


def test_body_names_target_rejects_near_misses():
    assert not body_names_target("no idea what to use here", [BRAND])
    assert not body_names_target("", [BRAND])
    assert not body_names_target(None, [BRAND])
    assert not body_names_target("we used Jollyfish SEO", [BRAND])   # not a word boundary
    assert not body_names_target("jollyseo.com", [BRAND])            # missing separator
    # single-word names are where a missing \b actually bites — a brand called "Jolly" must not
    # be found inside "Jollyfish", and one called "Peec" must not be found inside "Speec"
    one = [{"name": "Jolly"}]
    assert body_names_target("we went with Jolly in the end", one)
    assert not body_names_target("we went with Jollyfish in the end", one)
    assert not body_names_target("unjolly experience", one)
    assert not body_names_target("the speech was fine", [{"name": "Peec"}])
    assert not body_names_target("anything", [{"name": ""}])         # unusable brand name
    assert not body_names_target("anything", [])


def test_body_names_target_is_not_confused_by_regex_chars():
    """A brand name is user data — a '+' or '(' in it must not blow up or match wildly."""
    b = [{"name": "C++ Guru (US)"}]
    assert body_names_target("been reading C++ Guru (US) for years", b)
    assert not body_names_target("been reading CXX Guru US for years", b)


# ── the report pass: what came back, not a rewrite of it ──────────────────────────────────────
def test_mention_mode_reports_a_missing_brand_and_a_named_rival(capsys):
    posts = [{"title": "t1", "body": "no names at all in here"},
             {"title": "t2", "body": "we moved off Profound to Jolly SEO"},
             {"title": "t3", "body": "Jolly SEO has been fine"}]
    claude = StubClaude(call_handler=lambda p: {"posts": posts})
    got = PostGenerator(claude, None)._generate_candidates_for_intent(
        SUB, [BRAND], "commercial", ["question"], [], 3, brand_mention=True)
    out = capsys.readouterr().out
    assert "body does NOT name Jolly SEO" in out
    assert "names other companies Profound" in out
    assert out.count("body does NOT name") == 1      # only the first post
    assert out.count("names other companies") == 1    # only the second
    assert got == posts   # REPORTS — never rewrites the bodies


def test_report_pass_is_silent_when_the_mode_is_off():
    posts = [{"title": "t", "body": "no names at all in here"}]
    claude = StubClaude(call_handler=lambda p: {"posts": posts})
    PostGenerator(claude, None)._generate_candidates_for_intent(
        SUB, [BRAND], "commercial", ["question"], [], 3, brand_mention=False)


# ── plumbing: flag to prompt, UI to endpoint ──────────────────────────────────────────────────
def _read(rel):
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), rel),
                encoding="utf-8").read()


def test_batch_generation_threads_the_flag_to_the_prompt():
    """generate_posts -> _generate_candidates_for_intent is the batch path; a dropped kwarg there
    silently turns the toggle into a no-op with no error anywhere."""
    seen = []

    def _h(prompt):
        seen.append(prompt)
        return {"posts": []}

    class _DB:
        """Only the reads generate_posts makes before it builds the prompt."""

        def get_post_titles_for_brand_in_subreddit(self, *a, **k):
            return []

        def get_posts(self, *a, **k):
            return []

        def get_storyline_distribution(self, *a, **k):
            return {}

        def __getattr__(self, _n):
            return lambda *a, **k: []

    g = PostGenerator(StubClaude(call_handler=_h), _DB())
    g._select_best = lambda *a, **k: []
    brand = dict(BRAND, id=1)
    try:
        g.generate_posts(SUB, [brand], count=3, intent_counts={"commercial": 3},
                         brand_mention=True)
    except Exception:
        pass
    assert seen, "no prompt was built"
    assert any("BRAND MENTION MODE" in p for p in seen)


def test_every_candidates_call_site_threads_the_flag():
    """`_generate_candidates_for_intent` is reached from two places — the per-intent loop and the
    AI-Search gap-fill. The gap-fill one cannot be driven from a unit test cheaply, and a call
    site that forgets the kwarg fails SILENTLY: the toggle just does nothing for that path. So
    this reads the source and holds for any call site added later, too."""
    import ast as _ast

    src = _read(os.path.join("generators", "post_gen.py"))
    tree = _ast.parse(src)
    sites = [n for n in _ast.walk(tree)
             if isinstance(n, _ast.Call) and isinstance(n.func, _ast.Attribute)
             and n.func.attr == "_generate_candidates_for_intent"]
    assert len(sites) >= 2, f"expected the loop + the gap-fill, found {len(sites)}"
    for call in sites:
        kw = {k.arg for k in call.keywords}
        assert "brand_mention" in kw, (f"post_gen.py:{call.lineno} calls "
                                       f"_generate_candidates_for_intent without brand_mention")


def test_endpoints_read_the_flag():
    """Three endpoints, three distinct call shapes — asserted separately, because a single
    `"brand_mention=brand_mention)" in app` is satisfied by whichever one of them survives."""
    app = _read("app.py")
    assert app.count('brand_mention = bool(data.get("brand_mention", False))') == 2
    # batch: /api/live-posts/generate -> generate_posts
    assert ('persona=data.get("persona"), general=general, entropy=entropy,\n'
            "                brand_mention=brand_mention)") in app
    # custom title: /api/live-posts/custom -> generate_post_from_topic
    assert ("general=general, entropy=entropy,\n"
            "                                                      brand_mention=brand_mention)") in app
    # regenerate: reads the mode off the body it is replacing, then passes it on
    assert 'body_names_target(post.get("body"), brands)' in app
    assert "post_gen.regenerate_body(post, brands, brand_mention=brand_mention)" in app


def test_both_ui_toggles_post_the_flag():
    html = _read("templates/index.html")
    assert 'id="lsubs-brandmention"' in html
    assert 'id="lsubs-custom-brandmention"' in html
    assert "reqBody.brand_mention = true" in html
    # the custom modal sends it on BOTH the api() call and the 409/force retry fetch
    assert "body: { brand_id: bid, subreddit_name: sub, topic, force, general, entropy, brand_mention }" in html
    assert "JSON.stringify({ brand_id: bid, subreddit_name: sub, topic, force, general, entropy, brand_mention })" in html
