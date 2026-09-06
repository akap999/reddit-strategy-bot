"""FU152 — the blog byline is ALWAYS a generic "[Add author byline before publishing]" placeholder
(client deliverables → the client inserts their own; a specific/auto-guessed name never ships). $0.

(Change 2 — `_ensure_brand_byline_logo` no longer persisting a guessed author — is verified live, not
unit-tested here, because importing app.py starts the cron scheduler + touches the real DB.)
"""
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

PLACEHOLDER = "[Add author byline before publishing]"


def _gen(call_handler=None):
    return BlogGenerator(StubClaude(call_handler=call_handler), db=None)


def test_byline_is_always_the_placeholder():
    gen = _gen()
    # no author set → placeholder
    b0 = gen._byline_md({})
    assert PLACEHOLDER in b0
    # author SET → STILL the placeholder (never the specific name) — proving "always"
    b1 = gen._byline_md({"author_name": "Jane Doe", "author_title": "Founder"})
    assert PLACEHOLDER in b1
    assert "Jane Doe" not in b1 and "By Jane" not in b1
    # rendered as an italic line at the very top
    assert b1.startswith(f"*{PLACEHOLDER}") or f"*{PLACEHOLDER}*" in b1


def test_byline_keeps_reviewer_and_disclosure():
    gen = _gen()
    out = gen._byline_md({"reviewer_name": "Dr. Smith", "reviewer_title": "MD",
                          "disclosure": "This guide is published by Acme."})
    assert PLACEHOLDER in out                       # placeholder still present
    assert "Reviewed by Dr. Smith, MD" in out       # reviewer line preserved (when set)
    assert "This guide is published by Acme." in out  # disclosure line preserved


def test_generated_article_body_starts_with_placeholder_byline():
    gen = _gen(call_handler=lambda p: {"title": "T", "meta_description": "m", "keywords": [],
                                       "body_markdown": "## Quick answer\nx", "disclosure": "d"})
    art = gen.generate_article({"name": "Acme", "domain_url": "https://acme.com",
                                "author_name": "Jane Doe"}, "best acme")
    body = (art or {}).get("body_markdown") or ""
    assert body.lstrip().startswith(f"*{PLACEHOLDER}")   # byline prepended, placeholder not the name
    assert "Jane Doe" not in body
