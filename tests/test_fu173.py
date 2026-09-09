"""FU173 — the rewrite was ~31 SEQUENTIAL GPU round-trips (16 of them the section pass), not mainly cold
start. Parallelize while preserving ORDER and every safety gate, cut the redundant whole-article attempt,
and make "is the model loaded?" answerable. All $0, deterministic, no network."""
import re
import threading
import time
from generators.blog_gen import BlogGenerator as B
from tests.stubs import StubClaude


class _CountingWriter:
    """Records concurrency and can stagger/fail per section."""
    def __init__(self, transform=None, delay=0.03, fail_on=None):
        self.transform, self.delay, self.fail_on = transform, delay, (fail_on or set())
        self.prompts, self.lock = [], threading.Lock()
        self.inflight, self.max_inflight = 0, 0

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        with self.lock:
            self.prompts.append(prompt)
            self.inflight += 1
            self.max_inflight = max(self.max_inflight, self.inflight)
        try:
            time.sleep(self.delay)
            if "SECTION TEXT:" in prompt:
                src = prompt.split("SECTION TEXT:\n")[-1]
            elif "ARTICLE:\n" in prompt:
                src = prompt.split("ARTICLE:\n")[-1]        # whole-article attempt
            else:
                src = prompt
            for bad in self.fail_on:
                if bad in src:
                    raise RuntimeError("section call failed")
            return self.transform(src) if self.transform else src
        finally:
            with self.lock:
                self.inflight -= 1


def _gen(writer, claude=None, mode="rewrite"):
    return B(claude or StubClaude(), db=None, writer=writer, writer_mode=mode)


BODY = "".join(
    f"## Section {i}\n\nThe committee reviewed budget item {i} carefully during this quarter [S1].\n\n"
    for i in range(6)) + "## Sources\n- [S1] x — <https://x>\n"


# ── Change 1: sections run CONCURRENTLY but reassemble in ORIGINAL order ─────────────────────────
def test_fu173_sections_reassemble_in_original_order():
    """The risk parallelism introduces is reordering. Each section gets a DISTINCT marker and a staggered
    finish; the output must still be in source order."""
    w = _CountingWriter(transform=lambda src: "REWORDED " + src.strip())
    out = _gen(w)._rewrite_sections(BODY, "X")
    heads = [l for l in out.split("\n") if l.startswith("## ")]
    assert heads == [f"## Section {i}" for i in range(6)] + ["## Sources"]
    bodies = re.findall(r"REWORDED The committee reviewed budget item (\d)", out)
    assert bodies == [str(i) for i in range(6)]          # section text stayed with its own heading


def test_fu173_sections_actually_run_concurrently():
    w = _CountingWriter(transform=lambda src: "REWORDED " + src.strip(), delay=0.05)
    _gen(w)._rewrite_sections(BODY, "X")
    assert w.max_inflight > 1, "section pass is still serial"


def test_fu173_parallel_output_matches_serial_output():
    """Byte-identical to the serial path for the same model replies."""
    t = lambda src: "REWORDED " + src.strip()
    par = _gen(_CountingWriter(transform=t))._rewrite_sections(BODY, "X")
    import generators.blog_gen as bg
    old = bg._WRITER_WORKERS
    try:
        bg._WRITER_WORKERS = 1                            # force one-at-a-time
        ser = _gen(_CountingWriter(transform=t))._rewrite_sections(BODY, "X")
    finally:
        bg._WRITER_WORKERS = old
    assert par == ser


# ── Safe degradation must survive concurrency ────────────────────────────────────────────────────
def test_fu173_failing_section_keeps_its_original_and_others_still_rewrite():
    w = _CountingWriter(transform=lambda src: "REWORDED " + src.strip(), fail_on={"item 2"})
    out = _gen(w)._rewrite_sections(BODY, "X")
    assert "REWORDED The committee reviewed budget item 2" not in out
    assert "The committee reviewed budget item 2 carefully during this quarter [S1]." in out   # original kept
    assert "REWORDED The committee reviewed budget item 3" in out                              # neighbours fine


def test_fu173_section_dropping_a_citation_or_fact_keeps_the_original():
    body = ("## A\n\nThe starting dose is 2.5 mg once weekly for adults beginning therapy here [S1].\n\n"
            "## Sources\n- [S1] x — <https://x>\n")
    # drops the [S1] marker
    out1 = _gen(_CountingWriter(transform=lambda s: "Clinicians start adults on 2.5 mg each week."))\
        ._rewrite_sections(body, "X")
    assert out1 is None or "[S1]" in out1
    # keeps [S1] but drops the dose
    out2 = _gen(_CountingWriter(transform=lambda s: "Clinicians begin weekly dosing for adults now [S1]."))\
        ._rewrite_sections(body, "X")
    assert out2 is None or "2.5 mg" in out2


def test_fu173_sources_section_is_never_reworded():
    w = _CountingWriter(transform=lambda src: "REWORDED " + src.strip())
    out = _gen(w)._rewrite_sections(BODY, "X")
    assert "- [S1] x — <https://x>" in out
    assert not any("[S1] x —" in p for p in w.prompts)   # never even sent to the model


# ── Change 3: one whole-article attempt on a long article (the section pass supersedes it) ───────
def test_fu173_long_article_runs_one_whole_article_attempt():
    long_body = ("# T\n\n" + "".join(
        f"## S{i}\n\n" + ("The committee reviewed the quarterly budget in detail. " * 12) + "[S1]\n\n"
        for i in range(6)) + "## Sources\n- [S1] x — <https://x>\n")
    w = _CountingWriter(transform=lambda src: src)      # returns the source → never grades thorough
    _gen(w)._apply_writer_pass({"body_markdown": long_body}, long_body, {"name": "X"}, "t")
    whole = sum(1 for p in w.prompts if "Re-compose the following finished blog article" in p)
    assert whole == 1, f"expected 1 whole-article attempt on a long article, got {whole}"
    assert any("SECTION TEXT:" in p for p in w.prompts)  # the section pass still ran


# ── Changes 4/5: where did the time go, and was the model loaded? ────────────────────────────────
class _ProbeWriter(_CountingWriter):
    def __init__(self, state, **kw):
        super().__init__(**kw)
        self.state = state

    def probe(self, timeout=8):
        return {"state": self.state}


def test_fu173_stage_timings_and_cold_flag_recorded():
    w = _ProbeWriter("ok", transform=lambda src: "REWORDED " + src.strip())
    art = {"body_markdown": BODY}
    _gen(w)._apply_writer_pass(art, BODY, {"name": "X"}, "t")
    st = art.get("writer_stage_secs") or {}
    assert set(st) >= {"extract", "attempts"}            # the breakdown is populated
    assert art.get("writer_was_cold") is False
    assert "first_call" in st                            # a cold model's load lands in this call


def test_fu173_cold_model_is_flagged():
    w = _ProbeWriter("warming", transform=lambda src: "REWORDED " + src.strip())
    art = {"body_markdown": BODY}
    _gen(w)._apply_writer_pass(art, BODY, {"name": "X"}, "t")
    assert art.get("writer_was_cold") is True            # "the GPU was loading" ≠ "generation is slow"


def test_fu173_missing_probe_does_not_break_the_pass():
    """A writer without probe() (older client / stubs) must behave exactly as before."""
    w = _CountingWriter(transform=lambda src: "REWORDED " + src.strip())
    art = {"body_markdown": BODY}
    out = _gen(w)._apply_writer_pass(art, BODY, {"name": "X"}, "t")
    assert out and art.get("writer_was_cold") is False
