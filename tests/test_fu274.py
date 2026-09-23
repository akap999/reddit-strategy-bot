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
    # FU275 replaced the bare rule with the REASON: the page exists to be quoted, and a direct
    # answer's first word is what gets lifted. Assert the property and its motivation, not the
    # sentence that carries it — rule #31 with no reason is exactly what the model ignored.
    assert "FIRST WORD" in p and '"Not reliably."' in p, "the shape that was lost must be named"
    assert "answer engines" in p, "the rule must arrive with the reason it exists"
    assert "published BY" in p, "the rewriter must know whose page this is"


def test_the_whole_article_prompt_protects_a_direct_opener():
    import inspect
    src = inspect.getsource(B._build_prompt) if hasattr(B, "_build_prompt") else ""
    if not src:
        import generators.blog_gen as BG
        src = open(BG.__file__, encoding="utf-8").read()
    assert "KEEP THE DIRECT OPENING WORD" in src


# ── the A/B probe (tools/rewrite_probe.py) ───────────────────────────────────────────────────────
# This is the instrument a model swap is judged on, so its own blind spots matter. Both classes
# below were found by hand against the real articles AFTER the first version reported them clean.

def _probe(orig, rew, brand="Jolly Search"):
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "tools"))
    import rewrite_probe
    return rewrite_probe.probe(orig, rew, brand)


def test_a_pronoun_subject_still_counts_as_doubt_on_the_publisher():
    """The reported defect was "It claims to have scaled 650+ brands" inside Jolly's own profile.
    A brand-name-only window missed it and the probe reported the article clean."""
    orig = "## Jolly Search\nJolly Search is an AI search agency.\nIt reports 650+ brands scaled.\n"
    rew = "## Jolly Search\nJolly Search is an AI search agency.\nIt claims to have scaled 650+ brands.\n"
    assert _probe(orig, rew)["doubt"] == (0, 1)


def test_the_noun_claims_is_not_a_doubt_verb():
    """"…understand which claims are valid" carries no doubt about anyone. Requiring a subject in
    front is what separates the verb from the noun."""
    body = "## Jolly Search\nBefore evaluating proposals, understand which claims are valid.\n"
    assert _probe(body, body)["doubt"] == (0, 0)


def test_a_competitors_doubt_verb_is_not_charged_to_the_publisher():
    """The pronoun match above is what makes paragraph scoping load-bearing: "It claims" inside a
    COMPETITOR's profile must not be charged to the publisher. Without the scope it would be."""
    orig = ("## NoGood\nNoGood is a growth agency.\nIt reports 27x growth.\n\n"
            "## Jolly Search\nJolly Search lists a fixed price.\n")
    rew = ("## NoGood\nNoGood is a growth agency.\nIt claims 27x growth.\n\n"
           "## Jolly Search\nJolly Search lists a fixed price.\n")
    assert _probe(orig, rew)["doubt"] == (0, 0), "scoped to paragraphs naming the publisher"


def test_a_lost_direct_opener_is_reported_with_its_question():
    orig = "## FAQ\n### Does ranking get me into ChatGPT?\nNot reliably. Ahrefs found 12%.\n"
    rew = "## FAQ\n### Does ranking get me into ChatGPT?\nThe reliability is questionable. Ahrefs found 12%.\n"
    p = _probe(orig, rew)
    assert p["faq_direct"] == (1, 0)
    assert p["faq_lost"] and "ranking" in p["faq_lost"][0]


def test_a_marker_the_original_already_used_is_not_charged_to_the_rewrite():
    """Only words the rewriter INTRODUCED count. An article that legitimately said "comprehensive"
    once must not be penalised for still saying it."""
    orig = "The agency offers a comprehensive service across every engine.\n"
    rew = ("The agency delivers a comprehensive offering, a comprehensive dashboard and "
           "comprehensive reporting.\n")
    assert _probe(orig, rew)["markers"] == {}, \
        "the count ROSE 1->3, but the word is the article's own — only words the rewriter " \
        "introduced from nothing are its doing"


# ── FU275: the brief, rewritten ──────────────────────────────────────────────────────────────────
# The operator's read, which the evidence bore out: the guard was heavy because the BRIEF was
# contradictory, not only because the model was weak. What the section prompt said, verbatim:
#
#   "share NO run of MORE THAN 4 consecutive words with the original — the single most important rule"
#   "PRESERVE EXACTLY: … every Markdown table (structure and cell values, VERBATIM); every heading
#    line … CHARACTER-FOR-CHARACTER"
#
# A table row kept verbatim IS a run of more than four words. Rule #1 is impossible on factual prose
# ("the Food and Drug Administration" is five words) and contradicted two lines down, with no
# priority stated. That explains both measured failure modes at once: the model either
# over-preserved (overlap 0.55) or broke a preserved item (the dropped facts that sent 31% of
# sections back to Claude's text).

def _section_prompt():
    g, seen = _gen(["x"])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    _run(g, SECTION)
    return seen[0]


def test_the_impossible_absolute_is_gone():
    """No rule may be both unobeyable and labelled the most important one — a model that finds rule
    #1 cannot be taken literally has been taught that none of them are."""
    p = _section_prompt()
    assert "single most important rule" not in p
    assert "NO run of MORE THAN 4 consecutive words" not in p


def test_preserved_items_are_stated_exempt_not_competing():
    """The contradiction's actual fix. Preserved items cost nothing against the rewording, so there
    is no longer anything to trade off — and no excuse to barely reword."""
    p = _section_prompt()
    assert "EXEMPT" in p and "reword around them" in p
    assert "NEVER TRADE MEANING FOR NOVELTY" in p


def test_nothing_may_be_added_or_dropped():
    """"Do not modify" left omission wide open — dropping is neither adding nor changing, which is
    how "fastest" went 2 -> 0 in the California article while breaking no rule."""
    p = _section_prompt()
    assert "nothing left out" in p and "nothing added" in p
    assert "Dropping a qualifier is as wrong as inventing one" in p
    assert "word of praise" in p, "the invented competitor praise class"


def test_the_output_must_read_as_english():
    """Nothing previously asked for this, which is how "within less than three weeks" shipped."""
    assert "natural, publishable English" in _section_prompt()


def test_every_bold_span_is_preserved_wherever_it_sits():
    """The operator's rule: bold is the control surface. Previously only a fully-bold LINE and a
    bold lead-in LABEL were protected, so a bold phrase mid-sentence was ordinary prose."""
    p = _section_prompt()
    assert "EVERY BOLD SPAN, wherever it appears" in p
    assert "middle of a sentence" in p


def test_a_preserved_bold_subject_must_keep_its_verb():
    """FU252 measured this exact damage: preserving a bold phrase and rewording around it strands a
    bold SUBJECT without its verb. Making bold preservation uniform makes that risk uniform too."""
    p = _section_prompt()
    assert "SUBJECT of its sentence" in p
    assert "without its verb is a broken sentence" in p


def test_the_publisher_is_named_as_the_publisher():
    """"a Jolly Search article" reads as an article ABOUT them. Nothing said it was BY them, which
    is how "Jolly Search claims a 250.54% increase" was a reasonable edit."""
    p = _section_prompt()
    assert "published BY" in p
    assert "as plainly as any competitor" in p, "the even-hand rule, derived rather than enumerated"


# ── FU276: bold is a contract, not a suggestion ──────────────────────────────────────────────────
# The operator marked up a real article with 112 bold spans — 3.5% of words frozen became 11.1% —
# to pin the exact claims a rewrite kept losing: "the fastest result documented here",
# "recommended by seven AI surfaces", "under three weeks", and the FAQ openers "Yes." /
# "Not necessarily.". Two things had to be true for that to work, and neither was.

def test_bold_spans_are_read_from_anywhere_in_the_text():
    """`_LABEL_RE` is anchored at a block's START, so mid-sentence bold was invisible to every code
    path in the guard. That is precisely where the lost claims lived."""
    body = ("Its fastest result was **recommended by seven AI surfaces** in **under three weeks**, "
            "and it reports **650+** brands.\n")
    got = B._bold_spans(body)
    assert "recommended by seven AI surfaces" in got
    assert "under three weeks" in got and "650+" in got


def test_longer_spans_are_checked_before_the_phrases_inside_them():
    got = B._bold_spans("**the fastest result documented here** and **fastest**")
    assert got[0] == "the fastest result documented here"


def test_a_dropped_bold_span_is_named_and_retried():
    g, _ = _gen([""])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    src = "It has **the fastest result documented here**: seven surfaces in three weeks. " * 4
    rew = "It achieved visibility across all seven surfaces within three weeks. " * 5
    why = g._section_gate(src, rew)
    assert "bolded text" in why and "fastest result documented here" in why


def test_re_marking_bold_is_allowed_but_losing_the_words_is_not():
    """The contract is on the WORDS. A rewrite may move or re-mark the span; it may not drop it."""
    g, _ = _gen([""])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    src = "Jolly reports **650+** brands scaled across every engine it tracks today. " * 4
    ok = "Across every engine tracked, Jolly cites 650+ brands it has scaled to date. " * 4
    assert g._section_gate(src, ok) == "", "the words survived unbolded — that is preservation"


# ── FU276: bolding more must not make the watermark grade look worse ─────────────────────────────

def test_forced_bold_text_does_not_count_against_the_strip():
    """`_prose_for_overlap` already excludes a fully-bold LINE, reasoning that it is "required
    verbatim by the rewrite prompt … counting them would inflate the overlap and falsely grade a
    SAFE strip 'not-confirmed'". That argument applies word-for-word to mid-sentence bold, which was
    NOT excluded — so the operator bolding more would strip better and grade worse."""
    frozen = "**recommended by seven AI surfaces in under three weeks**"
    a = f"The agency was {frozen} according to its own case study page here.\n"
    b = f"Per its published case study, the firm was {frozen} last quarter.\n"
    with_ex = B._ngram_overlap(B._prose_for_overlap(a, B._bold_spans(a)),
                               B._prose_for_overlap(b, B._bold_spans(a)), n=5)
    without = B._ngram_overlap(B._prose_for_overlap(a), B._prose_for_overlap(b), n=5)
    assert with_ex < without, "the forced span must not be charged to the rewrite"


def test_the_exemption_list_comes_from_the_input_only():
    """Otherwise a rewrite hides its own copying by bolding it. Exercised through
    `_watermark_removal_report`, because the source of the list is chosen THERE — asserting on
    `_prose_for_overlap` alone passes whichever body the caller happens to hand it.

    The two bodies must share ONLY the copied sentence, or shared filler carries the overlap and the
    fixture cannot see the exemption at all (the first version of this test could not)."""
    COPIED = ("The agency reported unusually strong results across every single engine that it "
              "actively tracks throughout this particular year")
    orig = COPIED + ". Separately the firm published a short note about its own methodology.\n"
    # a rewrite that copies that sentence VERBATIM and bolds its own copy; everything else differs
    cheat = ("**" + COPIED + "**. A different closing line entirely, sharing no wording with "
             "whatever the original happened to say here.\n")
    assert B._bold_spans(orig) == [], "the fixture's INPUT must carry no bold"
    rep = B._watermark_removal_report(orig, cheat)
    assert rep["n5_prose_overlap"] > 0.5, \
        f"self-bolded copying must stay visible to the measure, got {rep['n5_prose_overlap']}"


# ── FU277: a bolded bare figure carries its unit ─────────────────────────────────────────────────
# The operator's own markup, run live, FELL BACK ENTIRELY — no watermark stripped:
#
#   blog 253: applied 78/78 bold spans
#   [writer] section-chunked pass rejected: dropped facts ['4.8 stars']
#   [writer] attempt 1 quality gate failed:  dropped facts ['4.8 stars']
#   [blog_gen] writer: FALLBACK — watermark NOT stripped
#
# 30 of the 78 spans were a bare figure with the unit left outside: "ratings of **4.8** stars",
# "a minimum of **$6,000** a month", "lift over **19** months". The fact gate's atom is "4.8 stars"
# — number AND unit, because the unit carries the meaning — so the bold drew a boundary the gate
# does not recognise, the model reworded the unit, and the whole rewrite was rejected. Same lesson
# as FU176: a price's cadence is part of the fact.

ATOMS = ["4.8 stars", "$6,000 a month", "19 months", "650+ brands"]


def test_a_bolded_bare_figure_is_widened_to_its_fact():
    body = "It reports ratings of **4.8** stars on Google and Clutch alike.\n"
    assert "4.8 stars" in B._bold_with_units(body, ATOMS)
    assert "4.8" not in B._bold_with_units(body, ATOMS), "the bare figure is replaced, not kept beside it"


def test_a_span_that_already_has_its_unit_is_untouched():
    """The operator doing this by hand and the code doing it must converge, not fight."""
    by_hand = "It reports ratings of **4.8 stars** on Google and Clutch alike.\n"
    by_code = "It reports ratings of **4.8** stars on Google and Clutch alike.\n"
    assert B._bold_with_units(by_hand, ATOMS) == B._bold_with_units(by_code, ATOMS)


def test_widening_never_shrinks_a_span():
    """Where the operator's bold is LONGER than any atom — a range like "$2,000 to $10,000+" — the
    operator's span wins. Protection is the union; bold may only ever add."""
    body = "Pricing runs **$2,000 to $10,000+** a month depending on the engine mix.\n"
    assert B._bold_with_units(body, ["$2,000", "$10,000+"]) == ["$2,000 to $10,000+"]


def test_a_non_figure_span_is_left_alone():
    body = "It offers **a typical range** rather than a fixed fee for the work.\n"
    assert B._bold_with_units(body, ATOMS) == ["a typical range"]


def test_an_atom_that_is_not_in_this_body_cannot_widen_a_span():
    body = "The agency lists **19** offices worldwide across four continents today.\n"
    assert B._bold_with_units(body, ["19 months"]) == ["19"], \
        "19 months does not appear here — widening to it would protect text that is not present"


def test_the_gate_checks_the_widened_span():
    g, _ = _gen([""])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    g._section_atoms = ATOMS
    src = "It reports ratings of **4.8** stars on Google and on Clutch as well today. " * 3
    rew = "Google and Clutch both show it rated 4.8 overall, per its own page. " * 3
    why = g._section_gate(src, rew)
    assert "4.8 stars" in why, f"the unit must be part of what is checked, got {why!r}"


# ── FU277: the atom list reaches the section pass at all ─────────────────────────────────────────

def test_this_sections_facts_are_named_in_its_prompt():
    """The list went ONLY into the whole-article prompt. The section pass — the primary path for
    every article over 4,000 chars with 3+ headings — ran on generic shape categories with no
    article-specific list at all."""
    g, seen = _gen(["x"])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    body = "## Profile\nIt reports **650+** brands scaled and a fixed fee today.\n"
    g._rewrite_sections(body, "Acme", timeout=5, atoms=["650+ brands", "4.8 stars"])
    p = seen[0]
    assert "KEEP THESE EXACTLY" in p
    assert "650+ brands" in p, "the bolded figure arrives widened to its fact"
    assert "4.8 stars" not in p, "an atom from ANOTHER section must not be pasted into this one"


def test_only_a_bare_figure_is_widened():
    """Widening exists because a bare FIGURE draws a boundary the fact gate rejects. A phrase span
    has no such problem, so widening it would freeze more of the article than the operator asked
    for and buy nothing — the opposite of what FU275 was for."""
    body = "Ignite gives **a typical range** rather than a fixed fee for its work.\n"
    atoms = ["gives a typical range rather than a fixed fee"]
    assert B._bold_with_units(body, atoms) == ["a typical range"]


def test_the_shortest_containing_fact_wins():
    """Minimal widening. Two atoms can contain the same figure; taking the longest would freeze a
    whole clause off the back of one bolded number."""
    body = "It reports ratings of **4.8** stars on Google and Clutch alike today.\n"
    atoms = ["4.8 stars", "4.8 stars on Google and Clutch"]
    assert B._bold_with_units(body, atoms) == ["4.8 stars"]


def test_the_section_pass_is_actually_given_the_atoms():
    """`_rewrite_sections` accepting an `atoms` argument is worth nothing if the caller never
    passes one — which was the bug: the list existed and went only to the whole-article prompt."""
    import inspect
    src = inspect.getsource(B._apply_writer_pass)
    call = src[src.index("self._rewrite_sections("):][:400]
    assert "atoms=" in call, "the section pass must be handed the per-article fact list"
    assert "atoms=()" not in call, "and not an empty one"


def test_a_bolded_direct_opener_still_counts_as_direct():
    """The operator bolds "**Yes.**" precisely BECAUSE it is load-bearing. A probe that reads it as
    "not a direct answer" reports the article as having none — which is what it did, returning
    0 of 0 on the bolded version and hiding the only measurement that mattered."""
    orig = "## FAQ\n### Are AI summaries reducing clicks?\n**Yes.** Pew found 8% versus 15%.\n"
    rew = "## FAQ\n### Are AI summaries reducing clicks?\n**Yes.** Pew measured 8% against 15%.\n"
    assert _probe(orig, rew)["faq_direct"] == (1, 1)


# ── FU278: the fallback unit is the unit of failure ──────────────────────────────────────────────
# Operator: "reverting the entire section just because of one word/term does not make sense."
# Measured on blog 253: 5 of 25 sections reverted WHOLE, and the ones that fail are the big ones —
# "$25 million ... annual" sits in a 229-word section, "$6,000 a month" in a 155-word one. So two
# missing unit words cost roughly a quarter of the article, and every reverted word is Claude's
# wording coming back, which is the watermark this pass exists to remove.

def _g():
    g, _ = _gen([""])
    g._facts_preserved = lambda a, b, *x: (True, [])
    g._price_cadence_ok = lambda a, b: (True, [])
    g._section_atoms = []
    return g


GOOD_O = "Victorious calls itself a 5x SEO Agency of the Year and lists many awards won."
GOOD_N = "Describing itself as a 5x SEO Agency of the Year, the firm lists numerous awards."
BAD_O = "It recommends a minimum of $6,000 a month for SEO with a 12-month commitment."
BAD_N = "A floor of $6,000 is recommended for SEO work, on a 12-month commitment."


def test_only_the_failing_paragraph_goes_back():
    g = _g()
    g._price_cadence_ok = lambda a, b: (("a month" in a) <= ("a month" in b), ["$6,000 MONTH"])
    out, salvaged = g._salvage_section(f"{GOOD_O}\n\n{BAD_O}", f"{GOOD_N}\n\n{BAD_N}")
    assert salvaged
    assert GOOD_N in out, "the good paragraph keeps its rewrite"
    assert BAD_O in out, "only the failing paragraph reverts"
    assert BAD_N not in out


def test_a_section_whose_every_paragraph_fails_still_reverts_whole():
    g = _g()
    g._price_cadence_ok = lambda a, b: (False, ["x"])
    src = f"{GOOD_O}\n\n{BAD_O}"
    out, salvaged = g._salvage_section(src, f"{GOOD_N}\n\n{BAD_N}")
    assert out == src and not salvaged


def test_mismatched_shapes_are_not_paired():
    """Positional pairing across a different paragraph count would splice unrelated text into the
    article — the FU176 mis-pair failure. There is nothing safe to salvage, so it reverts."""
    g = _g()
    src = f"{GOOD_O}\n\n{BAD_O}"
    out, salvaged = g._salvage_section(src, f"{GOOD_N} {BAD_N}")   # merged into one paragraph
    assert out == src and not salvaged


def test_a_salvaged_section_counts_as_reworded():
    """It IS partly rewritten, so reporting it as a pass-through would understate the strip and
    send the operator chasing a problem that is no longer there."""
    g = _g()
    g._price_cadence_ok = lambda a, b: (("a month" in a) <= ("a month" in b), ["x"])
    _out, salvaged = g._salvage_section(f"{GOOD_O}\n\n{BAD_O}", f"{GOOD_N}\n\n{BAD_N}")
    assert salvaged is True


def test_a_single_paragraph_section_has_nothing_to_salvage():
    g = _g()
    g._price_cadence_ok = lambda a, b: (False, ["x"])
    out, salvaged = g._salvage_section(BAD_O, BAD_N)
    assert out == BAD_O and not salvaged


def test_a_failing_section_is_salvaged_rather_than_reverted_end_to_end():
    """Through `_rewrite_sections`, not the helper — the whole-section revert lived at the CALL
    SITE, so asserting on `_salvage_section` alone leaves it untouched."""
    body = f"## Victorious\n{GOOD_O}\n\n{BAD_O}\n"
    g, seen = _gen([f"{GOOD_N}\n\n{BAD_N}"])
    g._facts_preserved = lambda a, b, *x: (True, [])
    # the cadence gate fails wherever "a month" was dropped — i.e. only the second paragraph
    g._price_cadence_ok = lambda a, b: (("a month" in a) <= ("a month" in b), ["$6,000 MONTH"])
    out = g._rewrite_sections(body, "Victorious", timeout=5)
    assert out, "a section with one salvageable paragraph must not come back empty"
    assert GOOD_N in out, "the good paragraph keeps its rewrite instead of the section reverting"
    assert BAD_O in out and BAD_N not in out, "only the failing paragraph goes back"
    assert len(seen) == 2, "it still retried before salvaging"


# ── FU279: salvage aligns by fingerprint, so it actually fires ───────────────────────────────────
# Requiring an EQUAL paragraph count meant salvage almost never fired in production — 7 of 25
# sections still reverted whole. A rewrite that merges two paragraphs or splits one took the entire
# section with it, which is the blunt revert salvage exists to prevent. Paragraphs are now aligned
# the way `rewrite_guard` aligns sections: difflib over a fingerprint that survives rewording — the
# [S#] markers and figures, which are exactly what a rewrite may NOT change.

def test_a_split_paragraph_elsewhere_no_longer_sinks_the_section():
    g = _g()
    src = ("Victorious calls itself a 5x winner [S1].\n\n"
           "It recommends a minimum of $6,000 a month [S2].\n\n"
           "Its services cover answer engine optimization [S3].")
    # the rewrite SPLITS the middle paragraph in two — four paragraphs against three
    got = ("Describing itself as a 5x winner [S1].\n\n"
           "A floor of $6,000 a month is recommended [S2].\n\n"
           "That is its stated minimum.\n\n"
           "Answer engine optimization is among its services [S3].")
    out, salvaged = g._salvage_section(src, got)
    assert salvaged, "the unaffected paragraphs must still be salvageable"
    assert "Describing itself as a 5x winner" in out
    assert "Answer engine optimization is among its services" in out, \
        "the paragraph AFTER the split still pairs, by fingerprint rather than position"


def test_a_paragraph_with_no_safe_pair_keeps_the_input():
    g = _g()
    src = ("Victorious calls itself a 5x winner [S1].\n\n"
           "It recommends a minimum of $6,000 a month [S2].")
    got = ("Describing itself as a 5x winner [S1].\n\n"
           "An entirely unrelated sentence about something else [S9].")
    out, _salv = g._salvage_section(src, got)
    assert "It recommends a minimum of $6,000 a month [S2]." in out, \
        "no confident pair means the input stands — never splice unrelated text (FU176)"
    assert "unrelated sentence" not in out


def test_an_equivalent_rendering_is_not_a_dropped_span():
    """"seven" vs "7", a serial comma, a curly apostrophe — the contract is on the WORDS, not their
    typography. An exact-string check reverts a whole section over a difference no reader sees."""
    g = _g()
    g._section_atoms = []
    src = "It was **recommended by seven AI surfaces** in under three weeks after launch. " * 3
    got = "Recommended by 7 AI surfaces within three weeks of launching, it was. " * 3
    assert g._section_gate(src, got) == ""


def test_a_changed_value_is_still_a_dropped_span():
    g = _g()
    g._section_atoms = []
    src = "The agency reports **650+ brands** scaled across every engine it tracks. " * 3
    got = "Across every engine tracked, the agency reports 650 brands scaled. " * 3
    why = g._section_gate(src, got)
    assert "bolded text" in why, "650+ is not 650 — normalisation must not launder a value"
