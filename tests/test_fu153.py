"""FU153 — self-hosted open-model FINAL writing pass (WRITER_MODE off/rewrite/compose) that strips
Claude's SynthID watermark. $0, no network (StubClaude + a local StubWriter).

Guarantees proven here:
  - `_ngram_overlap` (the watermark-removal proxy) is correct.
  - rewrite mode: a good rewrite ships (low overlap, [S#] preserved); a dropped citation FALLS BACK
    to the Claude body; a too-light rewrite (high overlap) SHIPS with a warning (never falls back).
  - compose mode: the prompt carries the evidence + key_facts; an out-of-range [S#] falls back.
  - writer=None / mode="off" → the writer pass is a NO-OP (today's flow byte-identical).
  - WriterClient POSTs the OpenAI-compatible shape and is graceful on failure.
"""
from generators.blog_gen import BlogGenerator
from generators.base import WriterClient
from tests.stubs import StubClaude


class StubWriter:
    def __init__(self, responder):
        self.responder = responder      # fn(prompt, attempt_idx) -> str|None
        self.prompts = []

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        i = len(self.prompts)
        self.prompts.append(prompt)
        return self.responder(prompt, i)


CLAUDE_BODY = (
    "# Best widgets for teams\n\n"
    "## Quick answer\n"
    "The Acme widget is the strongest pick for most teams because it ships quickly and costs "
    "noticeably less than the alternatives [S1], and it also connects to your stack without any "
    "custom glue code [S2].\n\n"
    "## Is the Acme widget reliable?\n"
    "Yes — independent benchmarking consistently reports high uptime across a wide range of "
    "workloads and traffic spikes [S1].\n"
)

REWRITE_BODY = (
    "# Best widgets for teams\n\n"
    "## Quick answer\n"
    "Acme leads the pack for the majority of squads: rollout is rapid and the price undercuts "
    "rival options [S1], while hooking it into an existing toolchain needs zero bespoke plumbing [S2].\n\n"
    "## Is the Acme widget reliable?\n"
    "Third-party stress tests keep showing dependable availability even when demand surges hard [S1].\n"
)

COMPOSED_BODY = (
    "# Best widgets\n\n## Quick answer\n"
    "For most teams the Acme widget is the standout option: it deploys fast and its pricing sits "
    "well below competing products [S1], while slotting into an existing toolchain with no custom "
    "integration work at all [S2].\n\n## Is it reliable?\n"
    "Outside stress testing repeatedly shows dependable uptime even during heavy traffic spikes [S1].\n"
)


def _gen(writer=None, mode="off", call_handler=None):
    return BlogGenerator(StubClaude(call_handler=call_handler), db=None,
                         writer=writer, writer_mode=mode)


# --- _ngram_overlap ------------------------------------------------------------------------------
def test_ngram_overlap():
    a = "the quick brown fox jumps over the lazy dog and runs away fast today"
    assert BlogGenerator._ngram_overlap(a, a, n=8) == 1.0                 # identical → full overlap
    b = "a totally distinct clause featuring none of those exact tokens whatsoever right here now"
    assert BlogGenerator._ngram_overlap(a, b, n=8) == 0.0                 # disjoint → zero
    assert BlogGenerator._ngram_overlap("too short", "x", n=8) == 0.0     # < n words → 0.0
    assert BlogGenerator._ngram_overlap(CLAUDE_BODY, REWRITE_BODY, n=8) < 0.15   # full reword → low
    assert BlogGenerator._ngram_overlap(CLAUDE_BODY, CLAUDE_BODY, n=8) == 1.0


# --- rewrite mode --------------------------------------------------------------------------------
def test_rewrite_success_keeps_citations_low_overlap():
    w = StubWriter(lambda p, i: REWRITE_BODY)
    gen = _gen(writer=w, mode="rewrite")
    art = {"body_markdown": CLAUDE_BODY}
    out = gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "best widgets for teams")
    assert out == REWRITE_BODY
    assert art["writer_mode_used"] == "rewrite"
    assert art["writer_overlap"] < 0.15
    assert "[S1]" in out and "[S2]" in out
    assert len(w.prompts) == 1                    # passed on the first attempt, no retry
    assert "PRESERVE EXACTLY" in w.prompts[0]     # rewrite-mode prompt


def test_rewrite_dropped_citation_falls_back():
    dropped = REWRITE_BODY.replace(" [S2]", "")   # loses a citation Claude had
    w = StubWriter(lambda p, i: dropped)
    gen = _gen(writer=w, mode="rewrite")
    art = {"body_markdown": CLAUDE_BODY}
    out = gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "seed")
    assert out == CLAUDE_BODY                      # fell back to Claude (today's output for this blog)
    assert art["writer_mode_used"] == "fallback"
    assert "fell back" in art["writer_warning"].lower()
    assert len(w.prompts) == 4                     # FU167: 4 escalating attempts before giving up


def test_rewrite_high_overlap_ships_with_warning():
    w = StubWriter(lambda p, i: CLAUDE_BODY)       # returns the SAME text → overlap 1.0
    gen = _gen(writer=w, mode="rewrite")
    art = {"body_markdown": CLAUDE_BODY}
    out = gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "seed")
    assert out == CLAUDE_BODY                      # shipped the open-model text — NOT a Claude fallback
    assert art["writer_mode_used"] == "rewrite"
    assert art["writer_overlap"] >= 0.15
    assert "not fully confirmed" in art["writer_warning"]
    assert art["writer_grade"] == "not-confirmed"   # FU167: graded removal level
    assert len(w.prompts) == 4                       # FU167: 4 escalating attempts (never reached "thorough")


# --- compose mode --------------------------------------------------------------------------------
def test_compose_prompt_has_evidence_and_validates_indices():
    w = StubWriter(lambda p, i: COMPOSED_BODY)
    gen = _gen(writer=w, mode="compose")
    gen._evidence_blocks = [
        {"label": "official · Acme pricing", "url": "https://acme.com/pricing", "text": "Pro is $9/mo."},
        {"label": "third-party · review", "url": "https://ex.com", "text": "Integrates via API."},
    ]
    art = {"body_markdown": CLAUDE_BODY}
    out = gen._apply_writer_pass(art, CLAUDE_BODY,
                                 {"name": "Acme", "key_facts": '{"pricing":"$9/mo"}'}, "best widgets")
    assert out == COMPOSED_BODY
    assert art["writer_mode_used"] == "compose"
    # FU165: with no captured Claude prompt (this test doesn't run generate_article) the compose falls
    # back to the minimal evidence prompt — which still carries the evidence.
    assert "EVIDENCE" in w.prompts[0]
    assert "Acme pricing" in w.prompts[0] and "$9/mo" in w.prompts[0]


def test_compose_out_of_range_citation_still_ships():
    # FU165: compose ALWAYS ships (never Claude's watermarked body). A stray/out-of-range [S#] is no
    # longer a fallback — the compose ships and the downstream `_rebuild_sources` renumbers/drops it.
    bad = COMPOSED_BODY.replace("[S2]", "[S9]")    # only 2 evidence blocks → [S9] is out of range
    w = StubWriter(lambda p, i: bad)
    gen = _gen(writer=w, mode="compose")
    gen._evidence_blocks = [
        {"label": "a", "url": "https://a", "text": "x"},
        {"label": "b", "url": "https://b", "text": "y"},
    ]
    art = {"body_markdown": CLAUDE_BODY}
    out = gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "seed")
    assert out == bad                              # SHIPPED the Qwen body (watermark stripped)
    assert art["writer_mode_used"] == "compose"


# --- inert when off (today's flow) --------------------------------------------------------------
def test_writer_none_is_off_and_noop():
    gen = BlogGenerator(StubClaude(), db=None)      # no writer args → defaults
    assert gen.writer is None and gen.writer_mode == "off"
    art = {"body_markdown": CLAUDE_BODY}
    assert gen._apply_writer_pass(art, CLAUDE_BODY, {"name": "Acme"}, "seed") == CLAUDE_BODY  # early return


def test_finalize_off_never_calls_writer():
    w = StubWriter(lambda p, i: "SHOULD NOT BE CALLED")
    gen = _gen(writer=w, mode="off")
    art = {"title": "seed", "meta_description": "d", "keywords": [],
           "body_markdown": "# Seed\n\n## Quick answer\nAcme is best.\n\n## FAQ\n### Does it work?\nYes.\n"}
    gen._evidence_blocks = []
    gen._finalize_article({"name": "Acme", "domain_url": "https://acme.com"}, "seed",
                          art, art["body_markdown"])
    assert w.prompts == []                          # off → the gate blocks the writer entirely


# --- WriterClient --------------------------------------------------------------------------------
def test_writer_client_no_config_returns_none():
    assert WriterClient("", "k", "m").call_text("hi") is None          # no endpoint
    assert WriterClient("https://x/v1", "k", "").call_text("hi") is None   # no model


def test_writer_client_posts_openai_shape(monkeypatch):
    import generators.base as base
    captured = {}

    class FakeResp:
        status_code = 200
        text = ""

        def json(self):
            return {"choices": [{"message": {"content": "  hello world  "}}]}

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(url=url, headers=headers, json=json)
        return FakeResp()

    monkeypatch.setattr(base.requests, "post", fake_post)
    out = WriterClient("https://x.modal.run/v1/", "secret", "qwen").call_text("write this")
    assert out == "hello world"
    assert captured["url"] == "https://x.modal.run/v1/chat/completions"   # trailing slash stripped
    assert captured["headers"]["Authorization"] == "Bearer secret"
    assert captured["json"]["model"] == "qwen"
    assert captured["json"]["messages"][-1]["content"] == "write this"


def test_writer_client_non_200_returns_none(monkeypatch):
    import generators.base as base

    class FakeResp:
        status_code = 500
        text = "boom"

        def json(self):
            return {}

    monkeypatch.setattr(base.requests, "post", lambda *a, **k: FakeResp())
    assert WriterClient("https://x/v1", "k", "m").call_text("hi") is None
