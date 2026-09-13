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


def test_the_live_leak_is_removed_including_the_inline_opener():
    """The first draft of this kept the SECOND "Certainly.", on the reasoning that it answers a yes/no
    question. Verifying twelve live rewrites showed that reading is wrong: it is the same tic bleeding
    into the content, and the declarative sentence after it is the liftable answer. Both go."""
    out = strip(LIVE)
    assert out.startswith("Online healthcare platforms")
    assert "Here is the rewritten section" not in out
    assert "Certainly" not in out
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


# --- the INLINE tic, found by verifying twelve live rewrites -------------------------------------

def test_an_inline_opener_at_the_head_of_real_content_is_trimmed():
    """Three shipped this way. The tic survives the line-level strip because the rest of the line is
    genuine text. Under a question heading it also reads as an answer, but "Certainly." is not a
    liftable answer and the declarative sentence after it is."""
    cases = [
        ("Certainly. Here is the rewritten section:\n\n---\n\nCertainly. Online healthcare platforms "
         "may lawfully assess patients [S2].",
         "Online healthcare platforms may lawfully assess patients [S2]."),
        ("Certainly! Here's the rewritten section:\n\n---\n\nCertainly, and this is among the key "
         "financial aspects to consider [S2].",
         "This is among the key financial aspects to consider [S2]."),
        ("Certainly, the 2025 findings are significant [S5].",
         "The 2025 findings are significant [S5]."),
    ]
    for src, want in cases:
        assert strip(src) == want, src


def test_every_interjection_form_is_covered():
    for word in ("Certainly", "Absolutely", "Sure", "Of course", "Indeed", "Definitely"):
        for punct in (".", ",", "!"):
            src = f"{word}{punct} The dose is 2.5 mg [S4]."
            assert strip(src) == "The dose is 2.5 mg [S4].", src


def test_yes_and_no_are_never_trimmed():
    """They ARE the direct answer the answer-first rules ask for, unlike a conversational tic."""
    for src in ["Yes, TRT suppresses sperm production in ~90% of men [S11].",
                "No, insurance rarely covers compounded tirzepatide [S4].",
                "Yes. The starting dose is 2.5 mg once weekly [S4]."]:
        assert strip(src) == src, src


def test_an_adverbial_certainly_is_left_alone():
    """No punctuation after it means it is modifying the sentence, not opening a chat reply."""
    for src in ["Certainly the most common option is tirzepatide [S1].",
                "Is it legal? Certainly. Providers may prescribe it [S2].",
                "Sure footing matters on a treadmill desk."]:
        assert strip(src) == src, src


def test_trimming_never_empties_a_line():
    assert strip("Certainly.") == "Certainly."
    assert strip("Absolutely!") == "Absolutely!"


# --- FU193: the prompt layer, so a NOVEL wording is discouraged as well as the known ones ---------

def test_both_rewrite_prompts_carry_the_output_discipline_rule():
    """The code strip is a list of known shapes. The prompt attacks the habit, which is what covers a
    form nobody has seen yet. The section prompt's generic version lost eight times across twelve live
    rewrites, so it is restated concretely; the whole-article branch had no such rule at all."""
    import inspect
    from generators import blog_gen
    src = inspect.getsource(blog_gen)
    assert src.count("OUTPUT DISCIPLINE") == 2          # section pass + whole-article attempt
    for phrase in ("PUBLISHED PROSE, not a chat reply", "Certainly", "Here is the rewritten section",
                   "NEVER close with an offer"):
        assert phrase in src, phrase


def test_the_rule_only_forbids_and_never_asks_for_more():
    """A forbid-only bullet cannot move prose quality in either direction, only narrow the band of
    allowed outputs — the same reasoning the earlier rewrite-prompt rules were accepted under."""
    import inspect, re
    from generators import blog_gen
    for m in re.finditer(r"OUTPUT DISCIPLINE.{0,900}", inspect.getsource(blog_gen), re.S):
        block = m.group(0).split("\\n\\n")[0]
        assert "NEVER" in block
        for asking in ("add ", "include ", "expand", "improve", "make it "):
            assert asking not in block.lower(), asking
