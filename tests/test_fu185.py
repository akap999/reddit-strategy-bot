"""FU185 — the obvious AI SYMBOLS are stripped MECHANICALLY from a finished body, on Claude's output
AND on Qwen's rewrite, while GENERATION IS LEFT COMPLETELY ALONE. $0, no network (StubClaude).

The design constraint being proven here: no prompt instructs any model to write differently, so there
is no mechanism by which content or retrieval quality can move. The only prompt change in the round is
Change 6's four forbid-only bullets in the rewrite branch, asserted explicitly below."""
from generators.blog_gen import BlogGenerator
from tests.stubs import StubClaude

BRAND = {"name": "Acme", "domain_url": "https://acme.com"}


def _g(call_handler=None, writer=None, mode="off"):
    return BlogGenerator(StubClaude(call_handler=call_handler), db=None,
                         writer=writer, writer_mode=mode)


def _scrub(text):
    return _g()._scrub_ai_symbols(text)[0]


# --- dashes: one test per rule in the replacement table -----------------------------------------

def test_numeric_range_becomes_a_hyphen_not_a_comma():
    """The rule that matters most: "5, 10 business days" would be a factual mangle, not a style nit."""
    assert _scrub("Support runs 5–10 business days.") == "Support runs 5-10 business days."
    assert _scrub("Open 9am – 5pm on weekdays.") == "Open 9am-5pm on weekdays."          # spaced range
    assert _scrub("Claims between $1,300 — $10,000.") == "Claims between $1,300-$10,000."


def test_compound_proper_noun_becomes_a_hyphen():
    assert _scrub("Available Monday–Friday only.") == "Available Monday-Friday only."


def test_conjunction_tail_becomes_a_comma():
    assert _scrub("It works — but only in 12 states.") == "It works, but only in 12 states."


def test_short_appositive_becomes_commas():
    assert (_scrub("The starting dose — 2.5 mg — is taken weekly.")
            == "The starting dose, 2.5 mg, is taken weekly.")


def test_long_tail_becomes_a_full_stop_and_capitalises():
    src = ("Coverage is limited — most plans will not reimburse a compounded product without a prior "
           "authorisation on file.")
    out = _scrub(src)
    assert out.startswith("Coverage is limited. Most plans will not reimburse")
    assert "—" not in out


def test_dangling_dash_is_dropped():
    assert _scrub("That was the plan —") == "That was the plan"


def test_double_hyphen_is_caught():
    assert _scrub("Use the widget--it is faster.") == "Use the widget, it is faster."


def test_no_comma_artefacts_are_left():
    out = _scrub("The plan, — and the price, — both changed this quarter for every single customer.")
    assert ",," not in out and ", ," not in out and ",." not in out and "  " not in out


# --- what it must NEVER touch --------------------------------------------------------------------

def test_table_row_keeps_its_punt_placeholder_cell():
    """FU138 writes a bare "—" into an unsourceable cell and its resolver looks for exactly that."""
    row = "| Ro | — |"
    assert _scrub(row) == row
    assert _scrub("| Tool | Price |\n|---|---|\n| Ro | — |\n") == "| Tool | Price |\n|---|---|\n| Ro | — |\n"


def test_sources_section_separator_survives():
    """`_rebuild_sources` writes "- [S1] label — <url>" itself; that dash is code, not a model tell."""
    body = "## Sources\n- [S1] Acme — <https://acme.com>\n- [S2] Ro — <https://ro.com>\n"
    assert _scrub(body) == body


def test_a_source_entry_is_skipped_even_without_its_heading():
    line = "- [S1] Acme — <https://acme.com>"
    assert _scrub(line) == line


def test_a_heading_after_sources_resumes_scrubbing():
    body = "## Sources\n- [S1] x — <https://x.com>\n\n## Notes\nIt works — but only sometimes.\n"
    out = _scrub(body)
    assert "- [S1] x — <https://x.com>" in out          # the source line untouched
    assert "It works, but only sometimes." in out       # prose after the next heading scrubbed


def test_code_fence_is_untouched():
    body = "text — here\n```\na — b\n```\nmore — text\n"
    out = _scrub(body)
    assert "a — b" in out                                # inside the fence
    assert "text, here" in out and "more, text" in out   # outside it


def test_nested_list_indentation_survives():
    body = "- top\n  - nested item — with a dash\n"
    out = _scrub(body)
    assert out.splitlines()[1].startswith("  - nested item")


# --- decoration + quotes -------------------------------------------------------------------------

def test_leading_decoration_becomes_a_real_list_marker():
    assert _scrub("• First item") == "- First item"
    assert _scrub("→ Step one") == "- Step one"
    assert _scrub("✓ Included") == "- Included"


def test_inline_arrow_is_said_in_words_and_ornament_is_dropped():
    assert _scrub("Input → output is the flow.") == "Input to output is the flow."
    assert _scrub("It ships ✓ every time.") == "It ships every time."


def test_curly_quotes_and_ellipsis_are_straightened():
    assert _scrub("Claude’s “best” pick… and more") == "Claude's \"best\" pick... and more"


def test_the_evidence_label_middle_dot_survives():
    """U+00B7 is the code-written separator in "third-party · <title>" — never decoration."""
    body = "## Sources\n- [S1] third-party · Forbes review — <https://forbes.com/x>\n"
    assert "third-party · Forbes review" in _scrub(body)


# --- the fact gates must remain valid over the scrubbed body ------------------------------------

def test_scrub_is_a_no_op_on_every_protected_atom_shape():
    body = ("Claims between $1,300 and $10,000 are handled in-house. The dose is 2.5 mg weekly for a "
            "503B product. Zepbound® was approved on March 19, 2025 [S7]. Uptime is 99.9% at 3.9% APR.")
    assert _scrub(body) == body                          # byte-identical: nothing to strip


def test_fact_gates_return_the_same_verdict_before_and_after():
    gen = _g()
    src = "Plans start at $149/month, then $249/month billed quarterly for the 60mg dose [S1]."
    out = "Pricing opens at $149/month and then $249/month billed quarterly for 60mg [S1]."
    before = (gen._facts_preserved(src, out, BRAND)[0], gen._price_cadence_ok(src, out)[0])
    after = (gen._facts_preserved(_scrub(src), _scrub(out), BRAND)[0],
             gen._price_cadence_ok(_scrub(src), _scrub(out))[0])
    assert before == after


def test_a_clean_body_is_returned_byte_identical_with_zero_counts():
    body = "# Title\n\n## Quick answer\nAcme is a fit for small teams [S1].\n\n## FAQ\n### Does it work?\nYes [S2].\n"
    out, counts = _g()._scrub_ai_symbols(body)
    assert out == body
    assert not any(counts.values())


def test_idempotent():
    body = "It works — but only in 12 states. Claude’s “pick” → done. 5–10 days.\n"
    once = _scrub(body)
    assert _scrub(once) == once


# --- call-site coverage --------------------------------------------------------------------------

# NOTE the table shape: TWO data rows with one real value, so FU138's punt resolver KEEPS the column
# (a column blank in >50% of rows is correctly dropped by that resolver, which is pre-FU185 behaviour).
DIRTY = ("# Seed\n\n## Quick answer\nAcme ships fast — and costs less [S1].\n\n"
         "| Tool | Price |\n|---|---|\n| Acme | $149/mo |\n| Ro | — |\n\n"
         "## FAQ\n### Does it work?\nYes… “reliably” [S1].\n")


def test_finalize_article_scrubs_the_body_and_the_meta_fields():
    gen = _g(call_handler=lambda p: {"linkedin_text": "post"})
    gen._evidence_blocks = [{"label": "Acme", "url": "https://acme.com", "text": "t"}]
    art = {"title": "seed", "meta_description": "Acme ships fast — and costs less",
           "meta_title": "Acme — the fit", "keywords": [], "body_markdown": DIRTY}
    out = gen._finalize_article(BRAND, "seed", art, DIRTY)
    body = out["body_markdown"]
    assert "—" not in body.split("## Sources")[0].replace("| Ro | — |", "")   # prose clean
    assert "| Ro | — |" in body                       # the punt-placeholder CELL survives
    assert "Acme ships fast, and costs less [S1]." in body
    assert "..." in body and "“" not in body and "”" not in body
    assert "—" not in out["meta_description"] and "—" not in out["meta_title"]


def test_generate_linkedin_output_is_scrubbed():
    gen = _g(call_handler=lambda p: {"linkedin_text": "Which tool — and why? Acme’s pick…"})
    out = gen.generate_linkedin(BRAND, "seed", {"body_markdown": "b", "title": "t"})
    assert "—" not in out and "’" not in out and "…" not in out


def test_generate_linkedin_article_scrubs_the_body_and_a_generated_title():
    gen = _g(call_handler=lambda p: {"title": "The Real Pick — 2026",
                                     "body_markdown": "Acme wins — but only sometimes."})
    out = gen.generate_linkedin_article(BRAND, {"title": "t", "body_markdown": "b"})
    assert "—" not in out["title"] and "—" not in out["body_markdown"]


def test_a_manual_linkedin_headline_keeps_its_dash_the_lock_outranks_the_strip():
    """FU83 locks an operator-typed headline verbatim; FU185 must not override that."""
    gen = _g(call_handler=lambda p: {"title": "ignored", "body_markdown": "body — here"})
    out = gen.generate_linkedin_article(BRAND, {"title": "t", "body_markdown": "b"},
                                        manual_title="My Exact — Headline")
    assert out["title"] == "My Exact — Headline"
    assert "—" not in out["body_markdown"]


def test_youtube_package_fields_are_scrubbed():
    def call_h(p):
        return {"title": "Which tool — and why?", "demo_title": "", "mini_answer": "Acme — the fit.",
                "script_markdown": "Acme ships fast — and costs less.",
                "chapters": [{"question": "What is it — really?", "ts": "00:00"}],
                "captions_transcript": "Acme ships fast — and costs less.",
                "shot_list": ["a"], "thumbnail_text": "Best — 2026", "cta": "Go — now",
                "pinned_comment": "Details — here", "tags": ["x"], "category": "Education"}
    out = _g(call_handler=call_h).generate_youtube_script(BRAND, {"title": "t", "body_markdown": "b"})
    for k in ("title", "script", "description", "captions"):
        assert "—" not in out[k], k
    for k in ("cta", "thumbnail_text", "mini_answer", "pinned_comment"):
        assert "—" not in out["meta"][k], k
    assert "—" not in out["meta"]["chapters"][0]["question"]


# --- the INERT PROOF: generation prompts carry NO FU185 rule -------------------------------------

def test_no_generation_prompt_instructs_the_model_about_symbols_or_style():
    """The premise of the whole round: generation is untouched. The scrub is code, not a prompt rule,
    so no Claude prompt may mention dashes, banned phrases, or any rhythm/structure rule."""
    gen = _g(call_handler=lambda p: {"title": "t", "meta_description": "m", "keywords": [],
                                     "body_markdown": "## Quick answer\nx", "disclosure": "d"})
    gen.generate_article(BRAND, "best widgets for teams")
    gen.generate_linkedin(BRAND, "seed", {"body_markdown": "b", "title": "t"})
    # NOTE: a bare "em dash" is NOT banned here — the pre-existing FU47 punt rule legitimately tells
    # the writer to put "—" in an unsourceable cell. What must be absent is any INSTRUCTION to avoid
    # symbols, or any phrase/rhythm rule, i.e. anything FU185 could have injected.
    banned = ["never an em", "never use an em", "no em-dash", "avoid em", "banned phrase",
              "delve", "tapestry", "balanced three-item", "sentence-length variance", "bold-first",
              "transition-word", "in conclusion", "it's worth noting"]
    for prompt in gen.claude.calls:
        low = prompt.lower()
        for b in banned:
            assert b not in low, f"{b!r} leaked into a generation prompt"


def test_the_rewrite_prompt_carries_change_6_and_nothing_about_symbols():
    """Change 6 is the round's ONLY prompt change, and every bullet only FORBIDS a regression."""
    import generators.blog_gen as bg
    src = bg.__file__
    with open(src, encoding="utf-8") as fh:
        text = fh.read()
    for rule in ["THE PUNT BAN IS ON MEANING", "NO SUPERLATIVE OR PROMOTIONAL ESCALATION",
                 "KEEP THE BALANCE", "DESCRIBE SOURCES HONESTLY"]:
        assert rule in text, rule
    # and the rewrite prompt still says nothing about punctuation or style
    assert "never an em-dash" not in text.lower()
