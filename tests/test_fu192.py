"""FU192 — a chat model answers a rewrite request CONVERSATIONALLY, and the preamble shipped.

A live blog carried this inside an FAQ answer:

    Is it legal to get tirzepatide prescribed online in the US?
    Certainly. Here is the rewritten section:
    ---
    Certainly. Online healthcare platforms utilizing licensed US doctors can lawfully...

The section prompt already says "Do NOT add a heading, a preamble, or any commentary". The HEADING
half of that same instruction is enforced in code; the preamble half was left to the prompt, and the
prompt was ignored. $0, no network."""
from generators.blog_gen import _strip_model_preamble as strip

LIVE = ("Certainly. Here is the rewritten section:\n\n---\n\n"
        "Certainly. Online healthcare platforms utilizing licensed US doctors can lawfully "
        "assess patients and prescribe tirzepatide when clinically indicated [S2].")


def test_the_live_leak_is_removed_and_the_real_answer_survives():
    """The nuance that matters: the SECOND "Certainly." is the genuine answer to a yes/no question
    and must be kept. Only the lead-in line and its separator go."""
    out = strip(LIVE)
    assert out.startswith("Certainly. Online healthcare platforms")
    assert "Here is the rewritten section" not in out
    assert "---" not in out
    assert "[S2]" in out


def test_every_preamble_shape_is_dropped():
    for src, want in [
        ("Sure.\n\nThe program costs $149 a month [S1].", "The program costs $149 a month [S1]."),
        ("Here's the rewritten version:\n\nThe dose is 2.5 mg [S4].", "The dose is 2.5 mg [S4]."),
        ("Below is the revised text:\n\nCoverage varies.", "Coverage varies."),
        ("I have rewritten the section below:\n\nCoverage varies.", "Coverage varies."),
        ("As requested:\n\nCoverage varies.", "Coverage varies."),
        ("Of course. Here is the rewritten section:\n\nCoverage varies.", "Coverage varies."),
    ]:
        assert strip(src) == want, src


def test_a_trailing_sign_off_is_dropped():
    assert strip("The dose is 2.5 mg [S4].\n\nLet me know if you want a different tone.") \
        == "The dose is 2.5 mg [S4]."
    assert strip("Coverage varies.\n\nI hope this helps!") == "Coverage varies."


def test_ordinary_prose_is_never_eaten():
    """Head-anchored, whole-line only. These must all come back byte-identical."""
    for src in [
        "Certainly the most common option is tirzepatide [S1].",      # adverb, not an interjection
        "Is it legal? Certainly. Providers may prescribe it [S2].",   # mid-sentence
        "Here is what the label says about the starting dose [S4].",  # no trailing colon
        "The program costs $149 a month [S1].",
        "---",                                                        # a break-only reply
        "Certainly.",                                                 # stripping would empty it
    ]:
        assert strip(src) == src, src


def test_it_never_returns_empty_and_never_raises():
    assert strip("") == ""
    assert strip(None) == ""
    assert strip("Sure.\n\n---\n\n") == "Sure.\n\n---\n\n"   # nothing left → keep the original


def test_it_is_applied_at_every_writer_reply_site():
    import inspect
    from generators import blog_gen
    src = inspect.getsource(blog_gen)
    # the helper definition plus one call per writer reply: sections, polish, repair, whole-article
    assert src.count("_strip_model_preamble") >= 5


def test_a_clean_multiline_body_keeps_its_blank_lines_and_trailing_newline():
    """The bug the first draft of this helper had: it consumed a clean reply's own leading and
    trailing blank lines while scanning, silently editing text that had nothing wrong with it."""
    body = "# Guide\n\nThe dose is 2.5 mg [S4].\n\n## Sources\n\n- [S4] x — <https://x>\n"
    assert strip(body) == body
    assert strip("\n\nLeading blank lines are content spacing.\n\n") \
        == "\n\nLeading blank lines are content spacing.\n\n"
