"""FU186 — the three defects found by verifying the first post-FU185 live rewrite:

1. the FU185 full-stop rule punctuated a PHRASE as a sentence and shipped visible FRAGMENTS on the
   bolded-label bullet shape, so the default inverts to a COMMA and the full stop is EARNED;
2. a curly apostrophe survived inside a TABLE cell, because `|` rows were skipped wholesale;
3. a heading and its paragraph were swallowed INTO the comparison table, because the model emitted no
   blank line after the last row and python-markdown keeps consuming non-blank lines as rows.

$0, no network. The three fragments that actually shipped are locked as comma cases below."""
import markdown as _md

from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude


def _g():
    return BlogGenerator(StubClaude(), db=None)


def _scrub(text):
    return _g()._scrub_ai_symbols(text)[0]


def _dash(line):
    return BlogGenerator._ai_fix_dashes(line)[0]


# --- Change 1: the three fragments that SHIPPED, locked as comma cases --------------------------

def test_the_three_shipped_fragments_now_get_a_comma():
    """Verbatim from the live document. Each was a bolded label + an explanatory PHRASE; the length
    rule sent all three to a full stop and shipped a fragment."""
    a = _dash("- **Mandatory lab tests or health evaluations** — To establish your metabolic status "
              "and ensure safety")
    b = _dash("- **Continuous medical supervision** — Adjustment of doses and management of side "
              "effects by a healthcare provider")
    c = _dash("- **FDA-compliant procedures** — Medications obtained and distributed through "
              "approved channels")
    for out in (a, b, c):
        assert "**." not in out and "**. " not in out, out
        assert "**," in out, out
        assert "—" not in out


def test_the_clause_case_still_earns_a_full_stop():
    out = _dash("Coverage is limited — most plans will not reimburse a compounded product")
    assert out == "Coverage is limited. Most plans will not reimburse a compounded product"


# --- Change 1: non-clause openers -> comma ------------------------------------------------------

def test_infinitive_tail_is_a_comma():
    assert _dash("Bloodwork comes first — to confirm your metabolic status before dosing") == (
        "Bloodwork comes first, to confirm your metabolic status before dosing")


def test_ing_participle_tail_is_a_comma():
    out = _dash("He rebuilt the whole deck — working through the weekend to get it finished")
    assert out.startswith("He rebuilt the whole deck, working through")


def test_every_listed_preposition_tail_is_a_comma():
    for prep in ("for", "with", "without", "from", "in", "on", "at", "by", "of", "after",
                 "before", "during", "including", "such", "based", "plus"):
        src = f"The plan covers the basics — {prep} the members who enrolled before the cut off"
        assert _dash(src).startswith("The plan covers the basics, "), prep


def test_not_just_only_tails_are_commas():
    for w in ("not", "just", "only"):
        src = f"The fee is annual — {w} the first month is billed at the intro rate"
        assert _dash(src).startswith("The fee is annual, "), w


def test_the_bold_label_list_shape_forces_a_comma_even_on_a_clause_tail():
    """A bullet's bold label is followed by its explanation, never by a new sentence — so this shape
    is a forced comma regardless of how clause-like the tail looks."""
    out = _dash("- **Coverage** — most plans will not reimburse a compounded product")
    assert out == "- **Coverage**, most plans will not reimburse a compounded product"
    # the same tail OUTSIDE the list-label shape still earns its full stop
    assert _dash("Coverage varies — most plans will not reimburse it").endswith(
        ". Most plans will not reimburse it")


# --- Change 1: clause openers -> full stop ------------------------------------------------------

def test_every_subject_pronoun_tail_earns_a_full_stop():
    for pron in ("it", "they", "he", "she", "we", "you", "there", "this", "these", "those"):
        src = f"The price moved — {pron} settled a month later"
        out = _dash(src)
        assert out.startswith("The price moved. " + pron[:1].upper() + pron[1:]), pron


def test_determiner_noun_phrase_with_a_finite_verb_earns_a_full_stop():
    for det, verb in (("the", "is"), ("a", "was"), ("most", "will"), ("many", "have"),
                      ("each", "has"), ("every", "must"), ("their", "can")):
        src = f"Costs vary — {det} member {verb} charged the same base rate"
        assert _dash(src).startswith("Costs vary. " + det[:1].upper() + det[1:]), det


def test_a_determiner_without_a_finite_verb_stays_a_comma():
    out = _dash("Costs vary — the starting tier for a new member enrolling this quarter")
    assert out.startswith("Costs vary, the starting tier")


# --- Change 1: the comma's own fallout ----------------------------------------------------------

def test_a_stranded_function_word_capital_is_lowered_but_a_name_is_not():
    assert _dash("- **Labs** — To establish your status").endswith(", to establish your status")
    # a proper noun / acronym after the comma KEEPS its capital
    assert "FDA-approved" in _dash("- **Coverage** — FDA-approved options only")
    assert "Zepbound" in _dash("- **Brand** — Zepbound is covered by some plans")
    assert "I use" in _dash("- **Tooling** — I use both weekly")


# --- Change 2: quotes inside a table row --------------------------------------------------------

def test_a_table_cell_loses_its_curly_quote_but_keeps_its_dash_and_pipes():
    body = ("| Platform | Focus | Price |\n|---|---|---|\n"
            "| Ro | Focuses on men’s health… | — |\n")
    out, counts = _g()._scrub_ai_symbols(body)
    assert "men's health..." in out
    assert "| — |" in out                       # the FU138 punt placeholder survives
    assert out.count("|") == body.count("|")         # row structure untouched
    assert counts["quotes"] == 2 and counts["dashes"] == 0


def test_a_table_row_with_no_quotes_is_returned_byte_identical():
    body = "| A | B |\n|---|---|\n| Ro — the first | — |\n"
    assert _scrub(body) == body


# --- Change 3: a table block must end with a blank line -----------------------------------------

def test_a_table_followed_by_a_heading_gains_a_blank_line():
    md = ("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |\n"
          "## What makes a GLP-1 program genuinely \"doctor-led\"?\n"
          "Some prose about the answer.\n")
    out = _g()._resolve_table_punts(md)
    lines = out.split("\n")
    hi = next(i for i, l in enumerate(lines) if l.startswith("## What makes"))
    assert lines[hi - 1] == ""


def test_a_table_already_followed_by_a_blank_line_is_unchanged():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |\n\n## Next\n"
    assert _g()._resolve_table_punts(md) == md


def test_a_table_at_the_end_of_the_body_gains_nothing():
    md = "| A | B |\n| --- | --- |\n| 1 | 2 |\n"
    assert _g()._resolve_table_punts(md) == md


def test_the_live_defect_renders_as_a_heading_not_a_table_cell():
    """The whole point: the swallowed section comes back as a real <h2> with its paragraph."""
    md = ("| Platform | Model | Price |\n|---|---|---|\n"
          "| PeterMD | Doctor-led | $149 |\n| Ro | Doctor-led | $99 |\n"
          "## What makes a GLP-1 program genuinely \"doctor-led\"?\n"
          "A doctor-led program means a licensed clinician owns the decisions.\n")
    html = _md.markdown(_g()._resolve_table_punts(md), extensions=["tables"])
    assert "<h2>" in html and "doctor-led" in html
    assert "<td>##" not in html
    assert "<p>A doctor-led program means" in html
    # and the table still ends at its last platform row
    assert html.count("<tr>") == 3                   # header + two platform rows


def test_the_scrub_does_not_create_or_destroy_the_blank_line():
    """The scrub was verified NOT to be the cause of defect 3 — lock that."""
    md = ("| A | B |\n|---|---|\n| 1 | 2 |\n"
          "## Heading with a question?\nProse.\n")
    assert _scrub(md) == md
