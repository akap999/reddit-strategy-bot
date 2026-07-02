"""FU54 Tier-1 test — the later blog pipeline end-to-end with a stubbed LLM ($0, no network).

Reproduces the v8 failure: the reconcile drops the core-topic (labeling) section. Asserts that after
the substance guard + deterministic ## Sources rebuild (exactly as generate_blog runs them), the section
is BACK and the authoritative `official ·` source survives — even though the reconciled body never cited
it inline. This exercises core_topic extraction → targeted official search → reconcile → guard → rebuild.
"""
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude


DRAFT_BODY = """## Quick answer
AI music tools that produce royalty-free audio are the safest choice.

## TikTok's AI labeling rules
Creators must disclose AI-generated content. Failing to label risks removal or demonetization.

## Comparison
| Tool | Price |
| --- | --- |
| Suno | ? |

## FAQ
### Is AI music copyright-safe?
Often, on paid plans.
"""

# The reconcile RETURNS a body that DROPPED the labeling section and does NOT cite the official source.
RECONCILED_DROPPED = """## Quick answer
AI music tools that produce royalty-free audio are the safest choice. [S1]

## Comparison
| Tool | Price |
| --- | --- |
| Suno | $10/mo |

## FAQ
### Is AI music copyright-safe?
Often, on paid plans.
"""


def _call_handler(prompt):
    if "extract for verification" in prompt:
        return {"tools": ["Suno"], "dimensions": ["Price"],
                "core_topic": "TikTok AI-content labeling / monetization policy",
                "claims": [{"brand": "Suno", "dimension": "Price", "claim": "price", "value": "?"}]}
    if "VERIFY + COMPLETE agent" in prompt:
        return {"revised_body_markdown": RECONCILED_DROPPED,
                "flagged": [{"claim": "Suno price", "action": "filled", "reason": "vendor page"}]}
    return {}


def _search_handler(brief, allowed, blocked):
    if "OFFICIAL primary source documenting" in brief:
        return [{"title": "TikTok AI-content policy",
                 "url": "https://www.tiktok.com/legal/ai-content",
                 "fact": "Creators must label realistic AI-generated content."}]
    if allowed:   # Tier-1 domain-pinned vendor search
        return [{"title": "Suno Pricing", "url": "https://suno.com/pricing",
                 "fact": "Pro is $10/mo with commercial rights."}]
    return []     # corroboration / broad tier-3


def test_pipeline_preserves_section_and_official_source():
    stub = StubClaude(call_handler=_call_handler, search_handler=_search_handler,
                      official_domain=lambda b, ctx: {"suno": "suno.com"}.get(b.lower(), ""))
    gen = BlogGenerator(stub, None)
    gen._evidence_blocks = []   # fresh, as _gather_evidence would leave it
    brand = {"name": "AI Inspo", "domain_url": "ai-inspo.com", "category": "AI music generator"}
    article = {"title": "t", "body_markdown": DRAFT_BODY, "keywords": []}

    vc = gen.verify_and_complete(brand, "safe AI music for TikTok monetization", article)
    assert vc, "verify_and_complete returned None"

    # Mirror generate_blog's tail: substance guard, then deterministic ## Sources rebuild.
    body = gen._restore_dropped_sections(DRAFT_BODY, vc["body_markdown"])
    body = gen._rebuild_sources(body)

    # 1) the dropped core-topic (labeling) section is restored
    assert "AI labeling rules" in body
    assert "Failing to label" in body
    # 2) the authoritative TikTok policy survived into ## Sources despite not being cited inline
    assert "tiktok.com/legal/ai-content" in body
    # 3) the competitor was sourced from its OWN site (vendor page), not only a review
    assert "suno.com/pricing" in body


def test_official_source_targeted_search_used_core_topic():
    """The (c2) official-source search brief is built from the extracted core_topic, not a generic string."""
    stub = StubClaude(call_handler=_call_handler, search_handler=_search_handler,
                      official_domain=lambda b, ctx: "suno.com")
    gen = BlogGenerator(stub, None)
    gen._evidence_blocks = []
    gen.verify_and_complete({"name": "AI Inspo", "category": "AI music generator"},
                            "safe AI music for TikTok", {"body_markdown": DRAFT_BODY})
    official_briefs = [s["brief"] for s in stub.searches if "OFFICIAL primary source documenting" in s["brief"]]
    assert official_briefs, "no official-source search was issued"
    assert "TikTok AI-content labeling" in official_briefs[0]   # from core_topic, not generic
