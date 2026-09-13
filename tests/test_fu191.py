"""FU191 — a thematic break is Markdown STRUCTURE, not a dash used as punctuation.

The FU185 scrub matched the "--" inside a "---" separator line and left a bare "-". On a live blog
that shipped as ELEVEN stray hyphen paragraphs instead of horizontal rules, and it also silently
disabled the FAQ parser's cut (which is keyed on "---"), so a dangling " -" leaked into a FAQPage
answer in the structured data. $0, no network."""
import markdown as _md

from generators.blog_gen import BlogGenerator, _parse_faq_pairs


def _scrub(text):
    return BlogGenerator.__new__(BlogGenerator)._scrub_ai_symbols(text)[0]


def test_a_thematic_break_survives_untouched():
    for brk in ("---", "----", "***", "___", "   ---   "):
        src = f"## A\n\ntext\n\n{brk}\n\n## B\n"
        assert _scrub(src) == src, brk


def test_it_renders_as_a_horizontal_rule_not_a_hyphen_paragraph():
    html = _md.markdown(_scrub("para one\n\n---\n\npara two\n"))
    assert "<hr" in html
    assert "<p>-</p>" not in html


def test_the_faq_answer_no_longer_picks_up_a_dangling_hyphen():
    """The live artefact: a FAQPage answer ending in ' -' because the cut lost its marker."""
    md = ("## FAQ\n\n### Does TRT affect fertility?\n\n"
          "Yes, TRT suppresses sperm production in ~90% of men [S11].\n\n"
          "---\n\n## Sources\n\n- [S11] x — <https://e.com>\n")
    ans = _parse_faq_pairs(_scrub(md))[0]["a"]
    assert not ans.rstrip().endswith("-"), ans
    assert ans.endswith("men.")


def test_real_dashes_elsewhere_are_still_fixed():
    out = _scrub("It works — but only in 12 states.\n\n---\n\nUse the widget--it is faster.\n")
    assert "It works, but only in 12 states." in out
    assert "Use the widget, it is faster." in out
    assert "\n---\n" in out                      # the break between them is untouched


def test_a_table_separator_row_is_still_left_alone():
    body = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    assert _scrub(body) == body
