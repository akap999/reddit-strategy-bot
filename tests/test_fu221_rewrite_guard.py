"""FU221 — the rewrite guard: the rewording step may change words, never structure, sources or terms.

Driven by the REAL blog #158 ("Can You Take TRT and a GLP-1 at the Same Time?") and the rewrite that
shipped: a dropped "Pricing (Tirzepatide)" column, a literal "[S#]", seven renamed bold labels and
"lean mass" / "narrative review" swapped for near-synonyms. All $0, no network.
"""
import os
import tempfile
from pathlib import Path

from generators.rewrite_guard import auto_key_terms, guard_rewrite, restore_tables

FIX = Path(__file__).parent / "fixtures"
ORIG = (FIX / "fu221_158_original.md").read_text()
REW = (FIX / "fu221_158_rewrite.md").read_text()
KEY_TERMS = ["lean mass", "narrative review"]      # what the extraction call returns for #158


def _g(orig, new, terms=None):
    return guard_rewrite(orig, new, key_terms=terms or [], log=lambda *a: None)


def _labels(text):
    import re
    return re.findall(r"(?m)^\s*(?:[-*+]|\d+[.)])?\s*\*\*([^*\n]{1,90}?)\*\*", text)


# ── the reported blog ─────────────────────────────────────────────────────────────────────────────
def test_158_the_pricing_column_comes_back():
    assert "Pricing (Tirzepatide)" not in REW
    body, rep = _g(ORIG, REW, KEY_TERMS)
    assert "| Platform | TRT Offered | GLP-1 / Tirzepatide | Pricing (Tirzepatide) |" in body
    assert "Starts at $149/month, then $249/month billed quarterly for the 60 mg plan [S15]" in body
    assert rep["tables"] == 1


def test_158_the_placeholder_citation_is_gone():
    assert "[S#]" in REW
    body, rep = _g(ORIG, REW, KEY_TERMS)
    assert "[S#]" not in body
    assert rep["reasons"].get("placeholder citation") == 1


def test_158_every_renamed_label_is_put_back_and_the_reworded_text_after_it_is_kept():
    body, rep = _g(ORIG, REW, KEY_TERMS)
    assert _labels(body) == _labels(ORIG)
    assert rep["labels"] == 7
    # the Quick answer keeps the REWORDED sentence — only the label went back
    assert "**Quick answer:** Absolutely, testosterone replacement therapy (TRT) alongside" in body


def test_158_swapped_terms_are_reverted_to_the_input_wording():
    body, _ = _g(ORIG, REW, KEY_TERMS)
    assert body.lower().count("lean mass") == ORIG.lower().count("lean mass")
    assert "narrative review" in body


def test_158_the_strip_survives_everywhere_else():
    """Only the blocks that broke a rule go back; the rest keep the rewrite (and its watermark strip)."""
    body, rep = _g(ORIG, REW, KEY_TERMS)
    assert rep["units"] == 61 and rep["blocks"] <= 8
    rew_paras = [p for p in REW.split("\n\n") if p.strip() and not p.lstrip().startswith(("#", "|"))]
    kept = sum(1 for p in rew_paras if p in body)
    assert kept >= 40, f"only {kept} reworded paragraphs survived"


def test_158_headings_and_sources_are_the_inputs():
    body, _ = _g(ORIG, REW, KEY_TERMS)
    heads = lambda t: [l for l in t.split("\n") if l.startswith("#")]   # noqa: E731
    assert heads(body) == heads(ORIG)
    assert body.split("## Sources")[1] == ORIG.split("## Sources")[1]


# ── each rule on its own, so a later edit can't quietly drop one ────────────────────────────────
BASE = """# Title

**Quick answer:** Acme costs $49 a month [S1] and ships in 3 days [S2].

## How does it work?

Acme uses a narrative review of HbA1c data [S3]. It covers lean mass too.

- **Price:** $49 per seat [S1]
- **Speed:** 3 days [S2]

## Sources
- [S1] Acme — <https://acme.com>
"""


def _swap(old, new, text=BASE):
    assert old in text
    return text.replace(old, new)


def test_a_clean_rewrite_passes_untouched():
    new = _swap("Acme uses a narrative review", "Acme draws on a narrative review")
    body, rep = _g(BASE, new, KEY_TERMS)
    assert body == new and not rep["changed"]


def test_a_citation_moved_to_another_paragraph_reverts_both():
    new = _swap(" [S2].\n\n## How", ".\n\n## How")
    new = new.replace("lean mass too.", "lean mass too [S2].")
    body, rep = _g(BASE, new, KEY_TERMS)
    assert body == BASE
    assert rep["reasons"]["citations changed"] == 2


def test_citations_piled_at_the_end_are_reverted():
    base = "# T\n\nOne fact [S1]. Two fact [S2]. Three fact [S3].\n"
    new = "# T\n\nOne fact. Two fact. Three fact [S1][S2][S3].\n"
    body, rep = _g(base, new)
    assert body == base and rep["reasons"]["citations piled together"] == 1


def test_placeholder_shapes_are_caught_but_link_text_is_not():
    for bad in ("[S#]", "[S?]", "[S]", "[Sx]", "[citation needed]"):
        new = _swap("lean mass too.", f"lean mass too {bad}.")
        body, rep = _g(BASE, new, KEY_TERMS)
        assert bad not in body, bad
    ok_base = "# T\n\nSee [Sonilo](https://sonilo.com) for more.\n"
    ok_new = "# T\n\nRead [Sonilo](https://sonilo.com) for more.\n"
    body, rep = _g(ok_base, ok_new)
    assert body == ok_new and not rep["changed"]


def test_a_changed_figure_reverts_the_block():
    new = _swap("ships in 3 days [S2]", "ships in 2 days [S2]")
    body, rep = _g(BASE, new, KEY_TERMS)
    assert "3 days [S2]" in body and rep["reasons"]["figures changed"] == 1


def test_a_label_that_disappears_reverts_the_block():
    new = _swap("- **Speed:** 3 days [S2]", "- Delivery takes 3 days [S2]")
    body, rep = _g(BASE, new, KEY_TERMS)
    assert "- **Speed:** 3 days [S2]" in body and rep["reasons"]["label lost"] == 1


def test_a_renamed_label_is_repaired_in_place():
    new = _swap("- **Price:** $49 per seat [S1]", "- **Cost:** just $49 per seat [S1]")
    body, rep = _g(BASE, new, KEY_TERMS)
    assert "- **Price:** just $49 per seat [S1]" in body and rep["labels"] == 1


def test_a_swapped_term_reverts_even_when_everything_else_is_intact():
    new = _swap("It covers lean mass too.", "It covers muscle mass too.")
    body, rep = _g(BASE, new, KEY_TERMS)
    assert "lean mass" in body and rep["reasons"]["term lost"] == 1


def test_an_acronym_the_article_uses_is_protected_without_being_listed():
    new = _swap("narrative review of HbA1c data", "narrative review of blood sugar data")
    body, rep = _g(BASE, new, KEY_TERMS)
    assert "HbA1c" in body


def test_a_reworded_table_comes_back_verbatim():
    base = "# T\n\n| A | B |\n| --- | --- |\n| x | - |\n"
    new = "# T\n\n| A |\n| --- |\n| x |\n"
    body, rep = _g(base, new)
    assert body == base and rep["tables"] == 1


def test_a_merged_section_is_kept_when_nothing_that_matters_moved():
    base = "# T\n\n## H\n\nFirst point [S1].\n\nSecond point [S2].\n"
    new = "# T\n\n## H\n\nFirst point [S1], and a second one [S2].\n"
    body, rep = _g(base, new)
    assert body == new and rep["restructured"] == 1


def test_a_merged_section_that_lost_a_citation_comes_back_whole():
    base = "# T\n\n## H\n\nFirst point [S1].\n\nSecond point [S2].\n"
    new = "# T\n\n## H\n\nFirst point and a second one [S1].\n"
    body, rep = _g(base, new)
    assert body == base and rep["sections"] == 1


def test_a_heading_the_rewrite_dropped_brings_its_section_back():
    base = "# T\n\n## A\n\nAlpha [S1].\n\n## B\n\nBeta [S2].\n"
    new = "# T\n\n## A\n\nAlpha, reworded [S1].\n"
    body, rep = _g(base, new)
    assert "## B\n\nBeta [S2]." in body and "Alpha, reworded [S1]." in body


def test_the_byline_and_the_sources_list_are_never_reworded():
    base = "*[Add author byline before publishing]*\n\n# T\n\nText [S1].\n\n## Sources\n- [S1] A — <https://a.com>\n"
    new = "*[Insert the author byline]*\n\n# T\n\nWords [S1].\n\n## Sources\n- [S1] A site — <https://a.com>\n"
    body, rep = _g(base, new)
    assert body.startswith("*[Add author byline before publishing]*")
    assert body.endswith("- [S1] A — <https://a.com>\n") and rep["sources"] == 1
    assert "Words [S1]." in body


def test_the_guard_never_raises():
    body, rep = guard_rewrite("# T\n\nA\n", None, log=lambda *a: None)
    assert body is None or body == ""
    body, rep = guard_rewrite("", "# T\n\nB\n", log=lambda *a: None)
    assert body == "# T\n\nB\n"


# ── the article's own terms: precise, but not every capitalised word ─────────────────────────────
def test_auto_terms_ignore_headings_and_ordinary_words():
    t = auto_key_terms("# Why Men Are Combining TRT\n\nThis study on TRT and a recent trial. A narrative "
                       "review found GLP-1-assisted loss. Multiple doses; multiple visits. Doctors "
                       "will review the Zepbound label.\n")
    assert "TRT" in t and "GLP-1" in t and "Zepbound" in t and "narrative review" in t
    for junk in ("Combining", "Men", "this study", "recent trial", "will review", "Multiple",
                 "GLP-1-assisted"):
        assert junk not in t, junk


def test_an_acronym_is_matched_case_sensitively():
    base = "# T\n\nShips across the US in 3 days. Call us.\n"
    new = "# T\n\nShips across the country in 3 days. Call us.\n"
    body, _ = _g(base, new)
    assert "the US" in body, "'us' the pronoun must not stand in for 'US'"


def test_a_word_slipped_inside_a_term_counts_as_a_swap():
    """#158 shipped "lean muscle mass" and "lean body mass" for "lean mass" — not the same measure."""
    for swapped in ("lean muscle mass", "lean body mass", "muscle mass"):
        new = _swap("It covers lean mass too.", f"It covers {swapped} too.")
        body, _ = _g(BASE, new, KEY_TERMS)
        assert "It covers lean mass too." in body, swapped


def test_hyphen_and_space_forms_of_a_term_are_the_same_term():
    base = "# T\n\nA fixed-rate loan at 3.9% [S1].\n"
    new = "# T\n\nA loan with a fixed rate of 3.9% [S1].\n"
    body, rep = _g(base, new, ["fixed-rate"])
    assert body == new and not rep["changed"]


# ── cross-vertical: nothing here knows it is a medical blog ──────────────────────────────────────
def test_lending_terms_are_protected():
    base = "# T\n\nThe 3.9% APR applies to a fixed-rate term loan [S2].\n"
    new = "# T\n\nThe 3.9% rate applies to a term loan [S2].\n"
    body, rep = _g(base, new, ["fixed-rate"])
    assert body == base and rep["reasons"]["term lost"] == 1


def test_software_terms_are_protected():
    base = "# T\n\nAcme holds SOC 2 Type II and bills per seat [S1].\n"
    new = "# T\n\nAcme is SOC 2 certified and bills per seat [S1].\n"
    body, _ = _g(base, new, ["SOC 2 Type II"])
    assert body == base


# ── the endpoint: the table cleanup must not reshape the input's tables ──────────────────────────
def test_restore_tables_puts_a_dropped_column_back():
    reshaped = REW
    body, n = restore_tables(ORIG, reshaped)
    assert n == 1 and "Pricing (Tirzepatide)" in body
    assert body.replace(
        [ln for ln in body.split("\n") if ln.startswith("| **Ro")][0], "") != ""


def test_restore_tables_leaves_a_body_with_a_different_table_count_alone():
    body, n = restore_tables("# T\n\n| a |\n| --- |\n| 1 |\n", "# T\n\nno table\n")
    assert n == 0 and body == "# T\n\nno table\n"


def _tmp():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    return path


def test_the_rewrite_endpoint_stores_the_inputs_table(monkeypatch):
    """The stored rewritten_body keeps #158's pricing column even though the cleanup after the
    rewrite (`_rebuild_sources` → the table-punt rule) drops a column with a '-' cell."""
    import app as appmod
    from db import Database
    from generators.blog_gen import BlogGenerator as BG

    path = _tmp()
    try:
        db = Database(path)
        db.connect()
        db.initialize()
        sub = db.ensure_live_subreddit("t")
        bid = db.add_brand(sub["id"], "PeterMD")
        blog_id = db.save_blog(bid, "Can You Take TRT and a GLP-1 at the Same Time?",
                               title="t", body_markdown=ORIG)
        db.meta_set("writer_endpoint_url", "https://writer.example")
        db.meta_set("writer_api_key", "k")
        db.close()
        appmod.DB_PATH = path
        appmod._db_initialized = True
        box = {}

        def _inline(_name, fn, *a, **kw):
            box["result"] = fn(_task_id="t") if kw.get("pass_task_id") else fn()
            return "t"
        monkeypatch.setattr(appmod, "start_task", _inline)

        def _fake_pass(self, article, draft_body, brand, seed, surface="blog"):
            article["writer_mode_used"] = "rewrite"
            body, _ = guard_rewrite(draft_body, REW, key_terms=KEY_TERMS, log=lambda *a: None)
            return body
        monkeypatch.setattr(BG, "_apply_writer_pass", _fake_pass)

        r = appmod.app.test_client().post(f"/api/blogs/{blog_id}/rewrite", json={"surface": "blog"})
        assert r.status_code == 200, r.get_data(as_text=True)
        db = Database(path)
        db.connect()
        stored = db.get_blog(blog_id)["rewritten_body"]
        db.close()
        assert "Pricing (Tirzepatide)" in stored
        assert "[S#]" not in stored
    finally:
        os.unlink(path)


# ── the writer pass runs the guard before it grades and ships ────────────────────────────────────
class _Writer:
    def __init__(self, out):
        self.out = out

    def probe(self, timeout=8):
        return {"state": "ok"}

    def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
        return self.out


def test_the_writer_pass_ships_the_guarded_body(monkeypatch):
    from generators.blog_gen import BlogGenerator as BG
    from tests.stubs import StubClaude

    monkeypatch.setenv("WRITER_FACT_VERIFY", "0")
    orig = ("# Which platform?\n\n**Quick answer:** Acme suits claims of $10,000 or more [S1], "
            "reviewed in a narrative review [S2].\n\n## What does Acme charge?\n\nAcme charges "
            "10-25% of the amount collected, billed monthly [S2].\n\n## Sources\n"
            "- [S1] Acme — https://acme.com/pricing\n")
    rew = ("# Which platform?\n\n**Rapid response:** For claims of $10,000 or more [S1], Acme fits, "
           "per a review article [S2].\n\n## What does Acme charge?\n\nThe fee is 10-25% of what is "
           "recovered, invoiced monthly [S2]. [S#]\n\n## Sources\n"
           "- [S1] Acme — https://acme.com/pricing\n")
    gen = BG(StubClaude(), db=None, writer=_Writer(rew), writer_mode="rewrite")
    gen._evidence_blocks = []
    art = {"body_markdown": orig}
    out = gen._apply_writer_pass(art, orig, {"name": "Acme", "domain_url": "https://acme.com"},
                                 "which platform")
    assert "[S#]" not in out
    assert "**Quick answer:**" in out and "narrative review" in out
    assert "rewrite guard put back" in (art.get("writer_warning") or "")
    assert art["writer_guard"]["changed"]


# ── found by running the guard over every rewrite stored in production ───────────────────────────
def test_an_abbreviation_and_its_spelled_out_form_are_the_same_term():
    base = "# T\n\nEvery US state licenses its own physicians [S1].\n"
    for ok in ("Every United States state", "Every U.S. state"):
        new = base.replace("Every US state", ok)
        body, rep = _g(base, new)
        assert body == new and not rep["changed"], ok
    body, _ = _g(base, base.replace("Every US state", "Every state"))
    assert body == base, "dropping the geography entirely is still a lost term"


def test_an_abbreviation_the_article_defines_may_be_spelled_out():
    base = ("# T\n\nMany men have erectile dysfunction (ED) [S1].\n\n"
            "Treating ED early helps [S2].\n")
    new = base.replace("Treating ED early helps", "Treating erectile dysfunction early helps")
    body, rep = _g(base, new)
    assert body == new and not rep["changed"]


def test_a_name_is_kept_as_a_whole_run_of_capitalised_words():
    t = auto_key_terms("# T\n\nProducts ship across the United States from Eli Lilly and Noom Med. "
                       "Multiple doses, multiple visits.\n")
    assert "United States" in t and "Noom Med" in t and "Multiple" not in t


def test_glued_paragraphs_are_paired_line_by_line_and_get_their_breaks_back():
    """Older rewrites dropped the blank lines between paragraphs, which Markdown then renders as ONE
    paragraph. They are paired line by line, and the output keeps the input's paragraph breaks."""
    base = "# T\n\n## H\n\nFirst point [S1].\n\nSecond point is 3 days [S2].\n"
    new = "# T\n## H\nFirst point, reworded [S1].\nSecond point takes 3 days [S2].\n"
    body, rep = _g(base, new)
    assert body == "# T\n\n## H\n\nFirst point, reworded [S1].\n\nSecond point takes 3 days [S2].\n"
    assert rep["relaid"] == 1 and rep["blocks"] == 0


def test_a_glued_paragraph_that_broke_a_rule_is_reverted_alone():
    base = "# T\n\n## H\n\nFirst point [S1].\n\nSecond point is 3 days [S2].\n"
    new = "# T\n## H\nFirst point, reworded [S1].\nSecond point takes 2 days [S2].\n"
    body, rep = _g(base, new)
    assert "First point, reworded [S1]." in body and "Second point is 3 days [S2]." in body
    assert rep["sections"] == 0 and rep["blocks"] == 1


def test_a_leftover_divider_line_does_not_unpair_a_section():
    """Older stored articles end sections with a lone "-" (a divider a scrub reduced). The rewrite
    drops it; that must not make the whole section look re-laid-out."""
    base = "# T\n\n## H\n\n**Quick answer:** Acme is 3 days [S1].\n\n-\n\n## I\n\nMore [S2].\n"
    new = "# T\n## H\n**Rapid response:** Acme takes 3 days [S1].\n## I\nMore words [S2].\n"
    body, rep = _g(base, new)
    assert "**Quick answer:** Acme takes 3 days [S1]." in body and "\n-\n" in body
    assert rep["sections"] == 0 and rep["labels"] == 1


def test_a_range_written_two_ways_is_the_same_figures():
    base = "# T\n\nRates run 10–20% depending on claim size [S1].\n"
    new = "# T\n\nThe rates vary from 10% to 20%, based on claim size [S1].\n"
    body, rep = _g(base, new)
    assert body == new and not rep["changed"]


def test_digits_inside_a_code_are_not_figures():
    base = "# T\n\nThey handle B2B invoices and GLP-1 claims of $5,000 [S1].\n"
    new = "# T\n\nThey take on B2B invoices and GLP-1 claims worth $5,000 [S1].\n"
    body, rep = _g(base, new)
    assert body == new and not rep["changed"]
    body, _ = _g(base, base.replace("$5,000", "$500"))
    assert body == base, "a real figure change is still caught"


def test_shorthand_numbers_may_be_reworded():
    base = "# T\n\nA 100% online clinic with 1:1 coaching and 24/7 support [S1].\n"
    new = "# T\n\nA fully online clinic with personal coaching and round-the-clock support [S1].\n"
    body, rep = _g(base, new)
    assert body == new and not rep["changed"]


def test_codes_spelled_out_by_the_rewrite_are_not_losses():
    base = ("# T\n\n- Not available in AL, CA, NC, or TX [S1]\n- Covers ED treatment and an NP visit [S2]\n")
    new = ("# T\n\n- Not offered in Alabama, California, North Carolina, or Texas [S1]\n"
           "- Covers erectile dysfunction treatment and a nurse practitioner visit [S2]\n")
    body, rep = _g(base, new)
    assert body == new and not rep["changed"]
    dropped = new.replace("Alabama, California, North Carolina, or Texas", "several states")
    body, _ = _g(base, dropped)
    assert "AL, CA, NC, or TX" in body, "dropping the states is still a loss"


def test_emphasis_capitals_are_not_terms():
    assert "AND" not in auto_key_terms("# T\n\nMen with low testosterone AND a high BMI.\n")


def test_a_loose_spelling_never_makes_the_input_look_like_it_had_a_term():
    """'body weight' must not count as the input mentioning a 'Boxed Warning' (initials BW)."""
    base = "# T\n\nThe label carries a Boxed Warning [S1].\n\nIt lowers body weight by 15% [S2].\n"
    new = "# T\n\nThe label carries a Boxed Warning [S1].\n\nIt reduces weight by 15% [S2].\n"
    body, rep = _g(base, new)
    assert body == new and not rep["changed"]


def test_names_stop_at_punctuation():
    t = auto_key_terms("# T\n\nIt is sold in Arizona, California, and Texas by Caine & Weiner.\n")
    assert "Arizona California" not in " | ".join(t) and "California" in t and "Caine" in t


def test_long_lowercase_phrases_from_the_extraction_are_not_enforced():
    """The extraction call sometimes lists category wording ("team collaboration tools"). Reverting
    those would leave the rewrite nothing to reword."""
    base = "# T\n\nIt has team collaboration tools and user-reported pros and cons [S1].\n"
    new = "# T\n\nIt offers tools for working together and reviews from users [S1].\n"
    body, rep = _g(base, new, ["team collaboration tools", "user-reported pros and cons"])
    assert body == new and not rep["changed"]


def test_wording_swaps_are_budgeted_so_the_rewrite_is_not_gutted():
    paras = "\n\n".join(f"Paragraph {i} covers lean mass in detail [S1]." for i in range(20))
    base = f"# T\n\n{paras}\n"
    new = base.replace("lean mass", "muscle mass")
    body, rep = _g(base, new, ["lean mass"])
    assert rep["blocks"] == 2 and rep["soft_left"] == 18, "a couple go back, the rest are reported"
    assert "review before publishing" in rep["summary"]
    # a swap that carries a code or a name is NEVER budgeted away
    base2 = "\n\n".join(f"Paragraph {i} uses GLP-1 therapy [S1]." for i in range(20))
    new2 = base2.replace("GLP-1 therapy", "weight-loss therapy")
    body2, rep2 = _g("# T\n\n" + base2 + "\n", "# T\n\n" + new2 + "\n")
    assert rep2["blocks"] == 20 and rep2["soft_left"] == 0


# ── the rewrite model is now TOLD to leave a bold lead-in label alone (the guard stays the backstop) ──
def test_every_rewrite_prompt_tells_the_model_to_keep_a_bold_lead_in_label():
    import inspect
    from generators.blog_gen import BlogGenerator as BG
    src = inspect.getsource(BG._apply_writer_pass) + inspect.getsource(BG._rewrite_sections) \
        + inspect.getsource(BG._residual_polish) + inspect.getsource(BG._verify_facts_semantic)
    assert src.count("BOLD LEAD-IN LABEL") >= 4, "the whole-article, section, polish and repair prompts"
    assert "**Quick answer:**" in src


def test_the_blog_writer_prompt_is_not_touched_by_this():
    """Claude's own article prompt must be unchanged — only the REWRITE side gained the rule."""
    import inspect
    from generators.blog_gen import BlogGenerator as BG
    assert "BOLD LEAD-IN LABEL" not in inspect.getsource(BG.generate_article)


# ── imported articles: the label CONTENT is there, the bold usually isn't ─────────────────────────
def test_an_imported_lead_in_is_bolded_without_changing_a_word():
    from generators.blog_gen import promote_bold_labels
    body = ("# T\n\n- Hematocrit elevation: TRT can raise red blood cell production and needs "
            "monitoring.\n2. Binding estimate: a quote that cannot change after the truck loads.\n")
    out, n = promote_bold_labels(body)
    assert n == 2
    assert "- **Hematocrit elevation:** TRT can raise" in out
    assert "2. **Binding estimate:** a quote that cannot" in out
    assert out.replace("**", "") == body, "only asterisks were added — no word changed"
    assert promote_bold_labels(out)[1] == 0, "idempotent"


def test_a_colon_alone_is_not_a_label():
    """"In short:", "She said:" and "The result:" open ordinary prose — and a label lives in a LIST."""
    from generators.blog_gen import promote_bold_labels
    for line in ("In short: the cheapest quote is rarely the one you should book here.",
                 "She said: the crew arrived two hours late and the paperwork was wrong.",
                 "- The result: a 30% drop in damage claims across the board last year.",
                 "- She said: the crew arrived two hours late and the paperwork was wrong.",
                 "Quick answer: yes, most national movers cross state lines for customers."):
        assert promote_bold_labels(line)[0] == line, line


def test_the_label_pass_leaves_everything_else_alone():
    from generators.blog_gen import promote_bold_labels
    for line in ("- **Already bold:** untouched text that carries on for a while here.",
                 "See https://acme.com/pricing: the plan page lists every tier it includes.",
                 "The meeting starts at 9:00 and runs until the afternoon session begins.",
                 "| Platform | Pricing: notes | more |",
                 "This is a long ordinary sentence with a colon in it: and it keeps going.",
                 "```\ncode: not prose at all in here\n```"):
        assert promote_bold_labels(line)[0] == line, line
    src = "# T\n\nBody [S1].\n\n## Sources\n- [S1] PeterMD: https://getpetermd.com/\n"
    assert promote_bold_labels(src)[0] == src, "the Sources list is never relabelled"


def test_the_rewrite_may_not_invent_a_label_the_input_never_had():
    base = "# T\n\nShipping runs to all 50 states within three business days of order [S1].\n"
    new = "# T\n\n**Shipping:** Orders reach all 50 states within three business days [S1].\n"
    body, rep = _g(base, new)
    assert body == base and rep["reasons"]["label added"] == 1


# ── the label pass: Claude MARKS, code applies ───────────────────────────────────────────────────
class _LabelClaude:
    """Returns scripted label picks; records the prompt it was given."""

    def __init__(self, labels):
        self.labels, self.prompts = labels, []

    def call(self, prompt, max_tokens=1024, max_retries=3, temperature=None, system_prompt=None):
        self.prompts.append(prompt)
        return {"labels": self.labels}


def _mark(body, labels, seed="which movers ship nationwide"):
    from generators.blog_gen import BlogGenerator as BG
    c = _LabelClaude(labels)
    gen = BG(c, db=None)
    out, n = gen.mark_bold_labels(body, seed, {"name": "Northwind"})
    return out, n, c


BODY = ("# T\n\n## What should you check?\n\n"
        "- Interstate licence: a mover crossing state lines needs a USDOT number [S1].\n"
        "- Binding estimate — a quote that cannot change once the truck is loaded [S2].\n"
        "- The result: a 30% drop in damage claims across the board last year [S3].\n\n"
        "| Feature | Coverage: notes |\n| --- | --- |\n| Licence | yes |\n\n"
        "## Sources\n- [S1] Northwind: https://northwind.example\n")


def test_the_articles_own_labels_are_shown_to_the_model_as_its_convention():
    body = ("# T\n\n- **Interstate licence:** a mover crossing state lines needs a USDOT number.\n"
            "- Binding estimate: a quote that cannot change once the truck has been loaded.\n")
    _, _, c = _mark(body, [])
    assert "ALREADY LABELS ITS POINTS LIKE THIS" in c.prompts[0]
    assert "Interstate licence" in c.prompts[0].split("LINES:")[0]


def test_the_model_marks_and_the_code_bolds_including_a_dash_lead_in():
    lines = BODY.split("\n")
    picks = [{"line": lines.index("- Interstate licence: a mover crossing state lines needs a USDOT number [S1]."),
              "phrase": "Interstate licence"},
             {"line": lines.index("- Binding estimate — a quote that cannot change once the truck is loaded [S2]."),
              "phrase": "Binding estimate"}]
    out, n, _ = _mark(BODY, picks)
    assert n == 2
    assert "- **Interstate licence:** a mover crossing state lines" in out
    assert "- **Binding estimate:** a quote that cannot change" in out   # a dash lead-in too
    assert out.replace("**", "").replace("Binding estimate:", "Binding estimate —") == BODY
    assert _mark(out, picks)[1] == 0, "idempotent"


def test_a_phrase_that_is_not_the_start_of_that_line_is_dropped():
    lines = BODY.split("\n")
    i = lines.index("- The result: a 30% drop in damage claims across the board last year [S3].")
    for bad in ("a 30% drop", "Damage claims", "Interstate licence", "The invented label"):
        out, n, _ = _mark(BODY, [{"line": i, "phrase": bad}])
        assert n == 0 and out == BODY, bad


def test_headings_tables_and_sources_are_never_offered_to_the_model():
    _, _, c = _mark(BODY, [])
    sent = c.prompts[0]
    assert "What should you check?" not in sent and "| Feature |" not in sent
    assert "https://northwind.example" not in sent


def test_the_label_pass_is_off_by_env_and_never_raises(monkeypatch):
    monkeypatch.setenv("BLOG_LABEL_PASS", "0")
    assert _mark(BODY, [{"line": 4, "phrase": "Interstate licence"}]) [1] == 0

    class _Boom:
        def call(self, *a, **k):
            raise RuntimeError("no api")
    from generators.blog_gen import BlogGenerator as BG
    monkeypatch.delenv("BLOG_LABEL_PASS")
    out, n = BG(_Boom(), db=None).mark_bold_labels(BODY, "seed")
    assert n == 0 and out == BODY


def test_a_list_that_already_labels_its_points_labels_the_stragglers_too():
    from generators.blog_gen import BlogGenerator as BG
    lines = ["- **Interstate licence:** a mover crossing state lines needs a USDOT number.",
             "- Binding estimate: a quote that cannot change once the truck has been loaded.",
             "- Storage in transit — a warehouse hold between pickup and delivery for weeks.",
             "- She said: the crew turned up two hours late and the paperwork was incomplete.",
             "",
             "A plain paragraph: this is prose and must never be touched by any of this."]
    before = list(lines)
    n = BG._label_siblings(lines)
    assert n == 2
    assert lines[1].startswith("- **Binding estimate:**")
    assert lines[2].startswith("- **Storage in transit:**")      # a dash lead-in counts
    assert lines[3] == before[3], "a sentence opener is still not a label"
    assert lines[5] == before[5], "prose outside the list is untouched"
    assert BG._label_siblings(lines) == 0, "idempotent"


def test_a_list_with_no_labels_at_all_is_left_alone():
    from generators.blog_gen import BlogGenerator as BG
    lines = ["- Binding estimate: a quote that cannot change once the truck has been loaded.",
             "- Storage in transit: a warehouse hold between pickup and delivery for weeks."]
    assert BG._label_siblings(lines) == 0 and lines[0].startswith("- Binding estimate:")


# ── retry before reverting ────────────────────────────────────────────────────────────────────────
RETRY_BASE = "# T\n\nAcme ships to all 50 states in 3 days [S1].\n\nIt costs $49 a month [S2].\n"
RETRY_BAD = "# T\n\nAcme delivers everywhere within 3 days [S1].\n\nThe cost is $49 monthly [S2].\n"


def test_a_rejected_paragraph_is_listed_for_a_retry():
    body, rep = _g(RETRY_BASE, RETRY_BAD)
    assert rep["blocks"] == 1 and len(rep["retryable"]) == 1
    assert rep["retryable"][0]["problems"] == ["figures changed"]
    assert rep["retryable"][0]["orig"].startswith("Acme ships to all 50 states")


def test_a_good_retry_is_used_instead_of_reverting():
    first = _g(RETRY_BASE, RETRY_BAD)[1]
    src = first["retryable"][0]["orig"]
    body, rep = guard_rewrite(RETRY_BASE, RETRY_BAD, key_terms=[], log=lambda *a: None,
                              repairs={src: "Acme reaches all 50 states inside 3 days [S1]."})
    assert "Acme reaches all 50 states inside 3 days [S1]." in body
    assert rep["repaired"] == 1 and rep["blocks"] == 0
    assert "reworded a second time" in rep["summary"]


def test_a_retry_that_breaks_the_rule_again_falls_back_to_the_input():
    src = _g(RETRY_BASE, RETRY_BAD)[1]["retryable"][0]["orig"]
    for still_bad in ("Acme reaches every state inside 3 days [S1].",     # still lost "50"
                      "Acme reaches all 50 states inside 3 days [S1][S2].",   # added a citation
                      "**Coverage:** Acme reaches all 50 states inside 3 days [S1]."):  # invented a label
        body, rep = guard_rewrite(RETRY_BASE, RETRY_BAD, key_terms=[], log=lambda *a: None,
                                  repairs={src: still_bad})
        assert src in body and rep["repaired"] == 0 and rep["blocks"] == 1, still_bad
        assert any("retry failed too" in e for e in rep["examples"])


def test_the_retry_asks_the_rewrite_model_and_names_what_to_keep():
    from generators.blog_gen import BlogGenerator as BG
    from tests.stubs import StubClaude

    class _W:
        def __init__(self):
            self.prompts = []

        def probe(self, timeout=8):
            return {"state": "ok"}

        def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
            self.prompts.append(prompt)
            return "Acme reaches all 50 states inside 3 days [S1]."

    w = _W()
    gen = BG(StubClaude(), db=None, writer=w, writer_mode="rewrite")
    todo = [{"orig": "Acme ships to all 50 states in 3 days [S1].",
             "rewrite": "Acme delivers everywhere within 3 days [S1].",
             "problems": ["figures changed"]}]
    fixed = gen._guard_retry(todo)
    assert fixed[todo[0]["orig"]].startswith("Acme reaches all 50 states")
    p = w.prompts[0]
    assert "[S1]" in p and "50" in p and "no placeholder like [S#]" in p
    assert "previous attempt was rejected because it figures changed" in p


def test_the_fallback_rule_also_finishes_a_half_labelled_list():
    """No API key / pass switched off: the deterministic path still makes a list consistent."""
    from generators.blog_gen import promote_bold_labels
    body = ("# T\n\n- **Interstate licence:** a mover crossing state lines needs a USDOT number.\n"
            "- Binding estimate — a quote that cannot change once the truck has been loaded.\n")
    out, n = promote_bold_labels(body)
    assert n == 1 and "- **Binding estimate:** a quote that cannot" in out
    assert promote_bold_labels(out)[1] == 0


def test_a_fault_in_the_guard_costs_the_guard_not_the_rewrite(monkeypatch):
    """Found live: an error in the guard step threw away the whole rewrite and shipped Claude's body."""
    from generators.blog_gen import BlogGenerator as BG
    from tests.stubs import StubClaude
    import generators.rewrite_guard as RG

    monkeypatch.setattr(RG, "guard_rewrite", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    orig = ("# Which platform?\n\n**Quick answer:** Acme suits claims of $10,000 or more [S1].\n\n"
            "## What does Acme charge?\n\nAcme charges 10-25% of the amount collected [S2].\n\n"
            "## Sources\n- [S1] Acme — https://acme.com/pricing\n")
    rew = ("# Which platform?\n\n**Quick answer:** For claims of $10,000 or more [S1], Acme fits.\n\n"
           "## What does Acme charge?\n\nThe fee is 10-25% of what is recovered [S2].\n\n"
           "## Sources\n- [S1] Acme — https://acme.com/pricing\n")

    class _W:
        def probe(self, timeout=8):
            return {"state": "ok"}

        def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
            return rew

    art = {"body_markdown": orig}
    gen = BG(StubClaude(), db=None, writer=_W(), writer_mode="rewrite")
    gen._evidence_blocks = []
    out = gen._apply_writer_pass(art, orig, {"name": "Acme"}, "which platform")
    assert "The fee is 10-25%" in out, "the rewrite still shipped"
    assert art.get("writer_mode_used") == "rewrite", "not a fallback to Claude's body"


# ── the surgical round: never hand a paragraph back unreworded without trying a minimal edit ───────
def test_a_failed_retry_is_offered_for_a_surgical_edit_not_just_reverted():
    """The whole point of the pass is to replace the input's wording, so a paragraph whose re-reword
    also failed is queued for a patch of the MODEL'S OWN attempt before it can be handed back."""
    src = _g(RETRY_BASE, RETRY_BAD)[1]["retryable"][0]["orig"]
    still_bad = "Acme reaches every state inside 3 days [S1]."            # still lost "50"
    _body, rep = guard_rewrite(RETRY_BASE, RETRY_BAD, key_terms=[], log=lambda *a: None,
                               repairs={src: still_bad})
    todo2 = [x for x in rep["retryable"] if x.get("round") == 2]
    assert len(todo2) == 1
    assert todo2[0]["orig"] == src
    assert todo2[0]["rewrite"] == still_bad, "the surgical round patches the model's own text"
    assert todo2[0]["problems"] == ["figures changed"]


def test_a_good_surgical_fix_ships_the_models_words_not_the_input():
    src = _g(RETRY_BASE, RETRY_BAD)[1]["retryable"][0]["orig"]
    patched = "Acme reaches all 50 states inside 3 days [S1]."
    body, rep = guard_rewrite(RETRY_BASE, RETRY_BAD, key_terms=[], log=lambda *a: None,
                              repairs={src: patched})
    assert patched in body and src not in body
    assert rep["repaired"] == 1 and rep["blocks"] == 0


def test_the_surgical_prompt_patches_its_own_paragraph_and_names_the_missing_words():
    from generators.blog_gen import BlogGenerator as BG
    from tests.stubs import StubClaude

    class _W:
        def __init__(self):
            self.prompts = []

        def probe(self, timeout=8):
            return {"state": "ok"}

        def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
            self.prompts.append(prompt)
            return "OutSail is a vendor-agnostic HR tech broker."

    w = _W()
    gen = BG(StubClaude(), db=None, writer=w, writer_mode="rewrite")
    todo = [{"orig": "OutSail is a vendor-agnostic HR tech broker.",
             "rewrite": "OutSail is a vendor-neutral HR technology broker.",
             "problems": ["term lost: HR tech, vendor-agnostic"], "round": 2}]
    gen._guard_surgical(todo)
    p = w.prompts[0]
    assert "YOUR PARAGRAPH:\nOutSail is a vendor-neutral HR technology broker." in p, \
        "it must patch the model's own attempt, not re-reword the input"
    assert '"HR tech"' in p and '"vendor-agnostic"' in p
    assert "SMALLEST possible" in p and "Do NOT rewrite the paragraph again" in p


def test_the_surgical_round_restores_a_changed_figure_too():
    from generators.blog_gen import BlogGenerator as BG
    from tests.stubs import StubClaude

    class _W:
        def __init__(self):
            self.prompts = []

        def probe(self, timeout=8):
            return {"state": "ok"}

        def call_text(self, prompt, system_prompt=None, max_tokens=6000, temperature=0.7, timeout=300):
            self.prompts.append(prompt)
            return "Acme reaches all 50 states inside 3 days [S1]."

    w = _W()
    gen = BG(StubClaude(), db=None, writer=w, writer_mode="rewrite")
    todo = [{"orig": "Acme ships to all 50 states in 3 days [S1].",
             "rewrite": "Acme reaches every state inside 3 days [S1].",
             "problems": ["figures changed"], "round": 2}]
    fixed = gen._guard_surgical(todo)
    assert fixed[todo[0]["orig"]].startswith("Acme reaches all 50 states")
    assert "50" in w.prompts[0] and "restore every figure" in w.prompts[0]


def test_the_safety_net_still_holds_when_even_the_surgical_edit_fails():
    """Correct beats stripped: if the patch is still wrong the input paragraph ships, as before."""
    src = _g(RETRY_BASE, RETRY_BAD)[1]["retryable"][0]["orig"]
    body, rep = guard_rewrite(RETRY_BASE, RETRY_BAD, key_terms=[], log=lambda *a: None,
                              repairs={src: "Acme reaches every state inside 3 days [S1]."})
    assert src in body and rep["repaired"] == 0 and rep["blocks"] == 1
