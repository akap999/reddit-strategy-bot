"""FU166 — the COMPOSE writer was fed the STALE pre-sourcing prompt (captured at generate_article, before
_source_for_completion fetched the FDA/official/pricing sources), so Qwen wrote from ~half the evidence and
gutted the clinical depth. Fix: the writer pass SWAPS the early evidence chunk for the FULL post-sourcing
evidence (self._evidence_blocks), numbered [S#] to match so _rebuild_sources maps the citations. $0."""
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude


class _Writer:
    def __init__(self, out):
        self.out = out
        self.prompts = []

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        self.prompts.append(prompt)
        return self.out


QWEN = ("# TRT + GLP-1\n\n## Quick answer\n"
        + "Physician-guided combined care is possible under monitoring [S1]. " * 14
        + "\n\n## How is it monitored?\nHematocrit and estradiol are tracked [S2].\n")

# Claude's captured (EARLY) generate_article prompt: rules + ONLY the early evidence (S1), then more rules.
EARLY_EV = "\n[S1] PeterMD — https://getpetermd.com\nsemaglutide program overview\n"
ARTICLE_PROMPT = ("You are writing a FIRST-PARTY article for PeterMD ... build a comparison table with at "
                  "least 3 real competitors ..." + EARLY_EV
                  + "EVIDENCE RULE: cite every non-obvious fact [S#]. Return JSON only: "
                    "{\"body_markdown\": \"...\"}")


def _gen():
    gen = BlogGenerator(StubClaude(), db=None, writer=_Writer(QWEN), writer_mode="compose")
    gen._article_prompt = ARTICLE_PROMPT
    gen._article_prompt_ev = EARLY_EV
    # by writer time, _source_for_completion/reconcile appended the FDA label as [S2] — it was NOT in the
    # early prompt Claude was captured with, so pre-FU166 Qwen never saw it.
    gen._evidence_blocks = [
        {"label": "PeterMD", "url": "https://getpetermd.com", "text": "semaglutide program overview"},
        {"label": "official · ZEPBOUND (tirzepatide) FDA label", "url": "https://accessdata.fda.gov/z",
         "text": "hematocrit elevation compounded by dehydration; estradiol aromatization shifts with fat loss"},
    ]
    return gen


def test_compose_feeds_full_post_sourcing_evidence():
    gen = _gen()
    gen._apply_writer_pass({"body_markdown": "# claude body [S1]"}, "# claude body [S1]",
                           {"name": "PeterMD"}, "can you take TRT and a GLP-1 at the same time")
    p = gen.writer.prompts[0]
    # the LATE-sourced clinical evidence Qwen was missing is now IN the compose prompt:
    assert "hematocrit elevation compounded by dehydration" in p
    assert "estradiol aromatization shifts with fat loss" in p
    assert "ZEPBOUND (tirzepatide) FDA label" in p
    assert "[S2]" in p                                       # numbered to match self._evidence_blocks order
    # Claude's article RULES survived the evidence splice:
    assert "comparison table with at least 3 real competitors" in p
    # and the FU166 structure/depth instruction is present:
    assert "use ## (H2) for each MAIN section" in p.lower() or "use ## (H2) for each MAIN section" in p
    assert "do NOT" in p and "compress a well-sourced section" in p


def test_compose_without_captured_evidence_is_inert_splice():
    # a compose invoked with a captured prompt but NO _article_prompt_ev (e.g. no early evidence) must not
    # crash and must not blank the prompt — it just reuses the captured rules + the override.
    gen = BlogGenerator(StubClaude(), db=None, writer=_Writer(QWEN), writer_mode="compose")
    gen._article_prompt = "You are writing a FIRST-PARTY article ... comparison table ... Return JSON only: {}"
    gen._evidence_blocks = []
    gen._apply_writer_pass({"body_markdown": "# b [S1]"}, "# b [S1]", {"name": "PeterMD"}, "glp-1")
    p = gen.writer.prompts[0]
    assert "comparison table" in p
    assert "use ## (H2) for each MAIN section" in p
