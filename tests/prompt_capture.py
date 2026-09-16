"""FU205 — THE INERT PROOF.

R8 (consolidating the 52-rule article prompt) is deferred by operator decision, so this round has
to DEMONSTRATE that generation is untouched rather than assert it. This module builds the three
writer prompts — `generate_article`, `verify_claims`, `_reconcile_and_finish` — from FIXED inputs
so they can be captured byte-for-byte and compared against committed goldens.

Two variants are captured for the article prompt on purpose:
  * MINIMAL — every optional fragment inert (no geo, no qualifier, no ymyl, no evidence, no
    internal links, no siblings, no canonical facts). Locks the BASE template.
  * RICH    — every optional fragment ON. Locks the CONDITIONAL fragments, which is where an
    accidental edit is most likely to hide.

The ONLY normalisation applied is the FU140 current-year line (`datetime.utcnow().year`), which is
replaced with `{{YEAR}}` so the goldens survive a new year. Everything else is compared raw.
"""
import datetime as _dt
import re

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

_YEAR_RE = re.compile(re.escape(str(_dt.datetime.utcnow().year)))


def _norm(text):
    """Replace the only time-dependent token in the prompts (the FU140 CURRENT YEAR line)."""
    return _YEAR_RE.sub("{{YEAR}}", text or "")


# ── fixed inputs ──────────────────────────────────────────────────────────────────────────────
BRAND_MIN = {
    "name": "Acme Tools",
    "domain_url": "https://acmetools.example",
}

BRAND_RICH = {
    "name": "Acme Tools",
    "domain_url": "https://acmetools.example",
    "category": "concrete finishing equipment retailer",
    "audience": "independent finishing contractors in the US",
    "use_cases": ["buying a power trowel", "financing a mixer"],
    "pain_points": ["long lead times", "opaque financing terms"],
    "features": ["nationwide shipping", "in-house financing"],
    "competitors": ["Bravo Supply", "Delta Equipment"],
    "context": "Acme Tools sells concrete finishing equipment direct to contractors.",
    "learned_context": "Ships to all 50 states; financing handled in-house.",
}

SEED_MIN = "what is a power trowel"
SEED_RICH = "where to buy concrete finishing equipment online in the US with financing"

EVIDENCE = (
    "[S1] Acme Tools — https://acmetools.example/pricing\n"
    "Power trowels start at $1,300; financing available in-house.\n\n"
    "[S2] third-party · Contractor Review — https://example-review.test/acme\n"
    "Acme ships nationwide within five business days.\n"
)

KEY_FACTS = {"pricing": {"items": [
    {"product": "power trowel", "value": "$1,300 starting", "operator_set": True,
     "source_url": "https://acmetools.example/pricing"},
]}}

LINK_TARGETS = [
    {"url": "https://acmetools.example/pricing", "label": "pricing"},
    {"url": "https://acmetools.example/financing", "label": "financing"},
]

SIBLINGS = [
    {"title": "How to choose a power trowel", "meta_description": "A buyer's guide.",
     "url": "https://acmetools.example/blog/power-trowel-guide"},
]

ARTICLE = {
    "title": SEED_RICH,
    "meta_description": "Where US contractors buy concrete finishing equipment with financing.",
    "body_markdown": (
        "*[Add author byline before publishing]*\n\n"
        "# where to buy concrete finishing equipment online in the US with financing\n\n"
        "## Quick answer\n\nAcme Tools ships nationwide and finances in-house [S1].\n\n"
        "## Which retailers finance equipment?\n\nAcme Tools finances in-house [S1].\n\n"
        "| Retailer | Financing | Shipping |\n| --- | --- | --- |\n"
        "| Acme Tools | in-house | nationwide |\n| Bravo Supply | third-party | regional |\n"
    ),
}

SOURCING = {
    "name": "Acme Tools",
    "cat": "concrete finishing equipment retailer",
    "tools": ["Bravo Supply", "Delta Equipment"],
    "peers": ["Bravo Supply", "Delta Equipment"],
    "options": [],
    "dims": ["Financing", "Shipping"],
    "claims": [{"brand": "Bravo Supply", "claim": "offers third-party financing"}],
    "fresh": [
        {"label": "Bravo Supply", "url": "https://bravosupply.example/financing",
         "text": "Bravo Supply offers third-party financing through a lender partner."},
        {"label": "third-party · Equipment Weekly",
         "url": "https://example-news.test/delta",
         "text": "Delta Equipment ships within the continental US only."},
    ],
    "geo": "the US",
    "qualifier": "financing",
    "ymyl": None,
    "unverified_facts": [],
}


def _gen():
    """A BlogGenerator whose LLM is a stub — every prompt is recorded, none is sent."""
    stub = StubClaude(call_handler=lambda p: {
        "title": SEED_RICH,
        "meta_description": "x",
        "keywords": ["a"],
        "body_markdown": "# x\n",
        "disclosure": "d",
        "revised_body_markdown": "# x\n",
        "flagged": [],
    })
    return BlogGenerator(stub, db=None), stub


def capture():
    """Return {variant_name: prompt_text} for the three writer prompts, year-normalised."""
    out = {}

    # ── generate_article, all optional fragments INERT ────────────────────────────────────────
    gen, stub = _gen()
    gen.generate_article(BRAND_MIN, SEED_MIN)
    out["generate_article.minimal"] = _norm(gen._article_prompt)

    # ── generate_article, every optional fragment ON ──────────────────────────────────────────
    gen, stub = _gen()
    gen.generate_article(
        BRAND_RICH, SEED_RICH,
        extra_keywords=["concrete equipment financing", "power trowel financing"],
        evidence=EVIDENCE, geo="the US", sibling_titles=SIBLINGS, qualifier="financing",
        internal_links=True, link_targets=LINK_TARGETS, ymyl=None,
        key_facts=KEY_FACTS, key_facts_products=["power trowel"],
    )
    out["generate_article.rich"] = _norm(gen._article_prompt)

    # ── generate_article, YMYL on (the one big fragment the rich variant leaves off) ──────────
    gen, stub = _gen()
    gen.generate_article(BRAND_RICH, SEED_RICH, evidence=EVIDENCE, ymyl="medical")
    out["generate_article.ymyl"] = _norm(gen._article_prompt)

    # ── verify_claims ─────────────────────────────────────────────────────────────────────────
    gen, stub = _gen()
    gen.verify_claims(BRAND_RICH, ARTICLE, evidence=EVIDENCE)
    out["verify_claims"] = _norm(stub.calls[-1])

    # ── _reconcile_and_finish ─────────────────────────────────────────────────────────────────
    gen, stub = _gen()
    gen._evidence_blocks = [
        {"label": "Acme Tools", "url": "https://acmetools.example/pricing", "text": "prices"},
    ]
    gen._reconcile_and_finish(BRAND_RICH, SEED_RICH, dict(ARTICLE), SOURCING)
    out["reconcile_and_finish"] = _norm(stub.calls[-1])

    return out
