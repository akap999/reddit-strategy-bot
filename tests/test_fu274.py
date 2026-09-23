"""FU274 — a third of the article was never rewritten, and nothing said so.

The rewrite pass returned worse copy on three agency articles. The first diagnosis blamed the guard
and proposed five more detectors; that was backwards. Every guard rejection reverts a block to
Claude's wording, which is the exact thing the pass exists to remove — more nets would have made the
watermark strip WORSE, not better.

The production logs say what actually happened:

    17/24 sections reworded   overlap 0.39   longest-run 232   not-confirmed
    15/25 sections reworded   overlap 0.45   longest-run 363   not-confirmed
    19/25 sections reworded   overlap 0.26   longest-run 227   not-confirmed

23 of 74 sections — 31% — shipped Claude's text VERBATIM. `_rewrite_sections._one` set `got = ""` on
any gate miss and fell straight back to the original section, silently. So the long verbatim runs
were not a weak rewording; they were sections where no rewrite happened at all, and `writer_overlap`
carried the blame for it.

Two changes here. A failed section is RE-ASKED with the reason named (the FU221 paragraph ladder,
applied one level up), and a section that still cannot be reworded is REPORTED rather than hidden.

Plus the one genuine spec gap: nothing anywhere required an FAQ answer to keep its direct opening
word, and the rewrite prompt actively said to rephrase FAQ answers. Across the three articles 6
direct openers became 2 — "Not reliably." became "The reliability is questionable.", which answers
nothing. The house position was already written down at blog_gen.py:1207 ("'yes'/'no' are
deliberately ABSENT: those ARE the direct answer"); it was simply never told to the rewriter.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from generators.blog_gen import BlogGenerator as B  # noqa: E402

SECTION = ("## What separates strong work\n"
           "The agency publishes a fixed monthly price of $4,997 for one target prompt [S1]. "
           "It reports 650+ brands scaled and 25,000+ backlinks built [S2]. " * 3)


def _gen(replies):
    """A generator whose writer hands back `replies` in order, recording the prompts it saw."""
    g = B.__new__(B)
    g.writer_mode = "rewrite"
    seen = []

    class _W:
        def call_text(self, prompt, **kw):
            seen.append(prompt)
            return replies[min(len(seen) - 1, len(replies) - 1)]

    g.writer = _W()
    g._page_terms = []
    return g, seen


# ── the gate now NAMES its reason ────────────────────────────────────────────────────────────────

def test_a_dropped_citation_is_named(monkeypatch):
    g, _ = _gen([""])
    why = g._section_gate("Text with [S1] and [S2].", "Reworded text with [S1] only.")
    assert "[S2]" in why and "citation" in why


def test_an_empty_reply_is_named():
    g, _ = _gen([""])
    assert g._section_gate("Some source text [S1].", "") == "returned nothing"


def test_a_stub_is_named():
    g, _ = _gen([""])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    why = g._section_gate("x" * 400, "y" * 100)
    assert "stub" in why


def test_a_clean_rewrite_has_no_reason():
    g, _ = _gen([""])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    src = "The agency lists a price of $4,997 for one prompt [S1]."
    assert g._section_gate(src, "A price of $4,997 for a single prompt is listed [S1], per the page.") == ""


# ── the retry: a failed section is asked AGAIN before its original stands ────────────────────────

def _run(g, body, name="Acme"):
    return g._rewrite_sections(body, name, temperature=1.0, timeout=5)


def test_a_failed_section_is_asked_again_with_the_reason():
    """The fix. A first attempt that drops [S1] used to end the section's life; now the model is
    told what it broke and gets one more go, and the good second answer is what ships."""
    bad = "Completely reworded prose that forgot its marker entirely, going on at length here."
    good = ("An agency publishing $4,997 monthly for a single target prompt [S1]; it also cites "
            "650+ brands scaled alongside 25,000+ backlinks constructed [S2]. " * 3)
    g, seen = _gen([bad, good])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    out = _run(g, SECTION)
    assert len(seen) == 2, f"expected a retry, got {len(seen)} call(s)"
    assert "REJECTED" in seen[1] and "[S1]" in seen[1], "the retry must name what failed"
    assert "650+ brands scaled alongside" in out, "the good second attempt should ship"
    assert g._section_pass_stats[0] == 1


def test_a_section_that_fails_twice_keeps_its_original_and_is_reported():
    """Safe degradation is preserved — the content is never lost. What changes is that the
    pass-through is now COUNTED and its reason kept, instead of vanishing into the overlap number.

    Two sections, because that is the shape production actually shows (17/24, 15/25, 19/25): one
    reworded, one surrendered. With a single failing section `_rewrite_sections` returns None by
    design and the whole-article path takes over, so a one-section fixture would test nothing."""
    body = ("## Kept section\nThe agency lists $4,997 for one target prompt [S1].\n"
            "## Second section\nA GOODSECTION paragraph about the same agency [S2].\n")
    g = B.__new__(B)
    g.writer_mode = "rewrite"
    seen = []

    class _W:
        def call_text(self, prompt, **kw):
            seen.append(prompt)
            # keyed on the CHUNK's content, not call order — sections run concurrently, and the
            # heading is re-emitted by the code rather than sent, so it never reaches the prompt
            if "GOODSECTION" in prompt:
                return "A wholly different paragraph covering that same agency [S2], reworded."
            return "Reworded prose that lost its marker, long enough to clear the stub check here."

    g.writer = _W()
    g._page_terms = []
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    out = _run(g, body)
    assert "$4,997 for one target prompt [S1]" in out, "the failed section keeps its ORIGINAL text"
    assert "wholly different paragraph" in out, "the good section still ships its rewrite"
    n_done, n_total, whys = g._section_pass_stats
    assert (n_done, n_total) == (1, 2)
    assert whys and "[S1]" in whys[0], f"the reason must be kept, got {whys}"
    assert sum(1 for p in seen if "GOODSECTION" not in p) == 2, "the failing section was retried once"


def test_a_section_that_passes_first_time_is_not_asked_twice():
    good = ("An agency publishing $4,997 monthly for one target prompt [S1], citing 650+ brands "
            "scaled and 25,000+ backlinks constructed [S2]. " * 3)
    g, seen = _gen([good])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    _run(g, SECTION)
    assert len(seen) == 1, "a clean section must cost exactly one call"


def test_the_sources_section_is_never_sent_to_the_writer():
    g, seen = _gen(["should never be used"])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    out = _run(g, "## Sources\n- [S1] Acme - https://acme.example/x\n")
    assert out is None or "acme.example" in out
    assert not seen, "the Sources list is rebuilt deterministically, never reworded"


# ── the FAQ opener, in BOTH prompts ──────────────────────────────────────────────────────────────

def test_the_section_prompt_protects_a_direct_opener():
    """`_rewrite_sections` is the PRIMARY path for any article >=4000 chars with >=3 headings —
    i.e. every one of the three reported articles. A rule added only to the whole-article prompt
    would not have reached them."""
    g, seen = _gen(["x"])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    _run(g, SECTION)
    p = seen[0]
    assert "KEEP THAT OPENING WORD" in p
    assert '"Not reliably."' in p, "the opener rule must name the actual shapes that were lost"


def test_the_whole_article_prompt_protects_a_direct_opener():
    import inspect
    src = inspect.getsource(B._build_prompt) if hasattr(B, "_build_prompt") else ""
    if not src:
        import generators.blog_gen as BG
        src = open(BG.__file__, encoding="utf-8").read()
    assert "KEEP THE DIRECT OPENING WORD" in src
