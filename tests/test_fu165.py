"""FU165 — the COMPOSE writer reuses the EXACT article-writing prompt Claude used (same rules +
evidence, NO Claude-OUTPUT reference), so Qwen writes its OWN full article; and compose ALWAYS ships
(never Claude's watermarked body) except on an empty output. $0, no network."""
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude


class _Writer:
    def __init__(self, out):
        self.out = out
        self.prompts = []

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        self.prompts.append(prompt)
        return self.out


CLAUDE_BODY = ("# GLP-1 programs\n\n## Quick answer\n"
               + "PeterMD, Ro and Noom Med run doctor-supervised GLP-1 weight-loss programs [S1]. " * 40
               + "\n\n## How do they differ?\nThey differ on price, model and monitoring [S2].\n")
# a genuinely-shorter Qwen article (~0.4x) that has headings + citations
QWEN_ARTICLE = ("# GLP-1 programs\n\n## Quick answer\n"
                + "Several telehealth clinics offer physician-guided GLP-1 plans to eligible patients [S1]. " * 14
                + "\n\n## How do they differ?\nThe clinics vary in cost structure and check-in cadence [S2].\n")


def _gen(writer, mode="compose"):
    gen = BlogGenerator(StubClaude(), db=None, writer=writer, writer_mode=mode)
    gen._evidence_blocks = [{"label": "PeterMD", "url": "https://getpetermd.com", "text": "semaglutide $149/mo"}]
    return gen


# --- generate_article captures its EXACT prompt for the writer to reuse -----------------------
def test_generate_article_captures_its_prompt():
    art = {"title": "T", "meta_description": "m", "keywords": [],
           "body_markdown": "## Quick answer\nx", "disclosure": ""}
    gen = BlogGenerator(StubClaude(call_handler=lambda p: art), db=None)
    gen.generate_article({"name": "Acme", "domain_url": "https://acme.com"}, "best widgets")
    assert getattr(gen, "_article_prompt", "")                # captured
    assert "FIRST-PARTY article" in gen._article_prompt       # it IS the article-writing prompt Claude got


# --- the compose writer REUSES Claude's prompt (not a simplified one, not Claude's OUTPUT) -----
def test_compose_reuses_claude_prompt_not_reference():
    w = _Writer(QWEN_ARTICLE)
    gen = _gen(w, "compose")
    gen._article_prompt = ("You are writing a FIRST-PARTY article ... build a comparison table with at "
                           "least 3 real competitors ... Return JSON only: {\"body_markdown\": \"...\"}")
    gen._apply_writer_pass({"body_markdown": CLAUDE_BODY}, CLAUDE_BODY, {"name": "PeterMD"}, "glp-1")
    p = w.prompts[0]
    assert "comparison table with at least 3 real competitors" in p     # Claude's REAL rules reused verbatim
    assert "return only the finished markdown article body" in p.lower()  # JSON→Markdown output override
    assert "REFERENCE ARTICLE" not in p                                  # the reverted reference is GONE
    assert CLAUDE_BODY not in p                                          # Claude's OUTPUT is NOT fed to Qwen


def test_compose_ships_qwen_article():
    w = _Writer(QWEN_ARTICLE)
    art = {"body_markdown": CLAUDE_BODY}
    out = _gen(w, "compose")._apply_writer_pass(art, CLAUDE_BODY, {"name": "PeterMD"}, "glp-1")
    assert art.get("writer_mode_used") == "compose"          # SHIPPED Qwen's body (watermark stripped)
    assert out == QWEN_ARTICLE                                # NOT Claude's watermarked body
    assert len(QWEN_ARTICLE) < 0.6 * len(CLAUDE_BODY)        # even a leaner article ships (old gate rejected it)


def test_compose_falls_back_only_on_empty():
    w = _Writer("")                                          # endpoint returned nothing → nothing to ship
    art = {"body_markdown": CLAUDE_BODY}
    out = _gen(w, "compose")._apply_writer_pass(art, CLAUDE_BODY, {"name": "PeterMD"}, "glp-1")
    assert art.get("writer_mode_used") == "fallback"
    assert out == CLAUDE_BODY


def test_rewrite_still_gates_on_length():
    w = _Writer(QWEN_ARTICLE)                               # ~0.4x — outside rewrite's 0.6-1.4 band
    art = {"body_markdown": CLAUDE_BODY}
    out = _gen(w, "rewrite")._apply_writer_pass(art, CLAUDE_BODY, {"name": "PeterMD"}, "glp-1")
    assert art.get("writer_mode_used") == "fallback"        # rewrite unchanged — length band hard-gates
    assert out == CLAUDE_BODY
