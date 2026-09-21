"""FU205 (R1) — ONE guard composition, reached by EVERY write path.

The audit's first structural finding: `_finalize_article` is the only place the deterministic guards
are composed, and only 2 of 11 write paths reach it. `PATCH /api/blogs/<id>` had ZERO guards, so a
hand-edit could reintroduce every defect 204 rounds removed; blog import, the FU79 paused body and the
`part=article` / `part=verify` regenerate branches each hand-rolled a DIFFERENT subset.

This module is the fix's foundation: the CONTEXT-FREE guards — the ones that need nothing but the text
itself — behind one function, dispatched PER FIELD, called at the two DB write choke points
(`db.save_blog`, `db.update_blog`). Every current and future write path is then covered by construction.

Deliberately NOT included, and the distinction is load-bearing:

  * the `[S#]` renumber and the authoritative `## Sources` rebuild — they need `self._evidence_blocks`,
    which exists only during a generation. Running them here with no evidence map would DELETE
    citations rather than fix them.
  * `_resolve_table_punts` — it DROPS a comparison column (and, under the FU204 collapse guard, the
    whole table) when cells are unfilled. That is a SOURCING judgement made at generation time about
    evidence that could not be found. Re-applying it to an arbitrary stored body would silently delete
    a table an operator deliberately hand-wrote. The table SPACING half of FU186 is not lost:
    `scrub_markdown_formatting` (FU202 1B) inserts the blank lines around a table independently.

The rule the inclusions follow: this module REPAIRS text, it never DECIDES what content deserves to
exist. Anything that needs to know what the sourcing found stays in `_finalize_article`.

Every guard here is idempotent, so double-application (generation → finalize → save) is a no-op.
Nothing in this module can raise: on ANY failure the original text is returned unchanged. A blog save
must never fail because a scrub did.
"""

# ── field classes ─────────────────────────────────────────────────────────────────────────────────
# MARKDOWN bodies: the full context-free pipeline. These are the fields a reader sees as an article.
MARKDOWN_FIELDS = frozenset({
    "body_markdown",      # the blog itself
    "rewritten_body",     # FU154 watermark-free version — shipped to clients, same guarantees
    "body_pre_verify",    # FU202 snapshot; scrubbed so the "before" view isn't worse than the after
    "verified_body",      # FU208 operator-approved verified version — hand edits get the same guards
    # FU250: an imported version and its rewrite are whole articles that get handed to a client, so
    # they get exactly what the generated body gets. An outside file is the likeliest source of a
    # curly quote or an em-dash in the whole system.
    "imported_body", "imported_rewritten",
})

# PROSE surfaces: plain text or light markdown that must NOT be restructured. A LinkedIn post has no
# headings, YouTube captions are a transcript — running the markdown fixer or the table/punt resolvers
# over them would be a change with no defect to justify it. They get only the two guarantees that are
# genuinely universal: no AI symbols, no invisible characters.
PROSE_FIELDS = frozenset({
    "linkedin_text", "linkedin_rewritten",
    "linkedin_article", "linkedin_article_rewritten",
    "youtube_script", "youtube_description", "youtube_captions",
})

# META fields: published text, so the symbol scrub applies — exactly as `_finalize_article` already
# does for these two. `title` is deliberately ABSENT: FU88 pins it to the operator's seed verbatim,
# and the H1 is pinned to the same string, so scrubbing it would break that equality.
META_FIELDS = frozenset({"meta_description", "meta_title",
                         "verified_meta_description"})   # FU208

SANITIZED_FIELDS = MARKDOWN_FIELDS | PROSE_FIELDS | META_FIELDS


def _generator():
    """A throwaway BlogGenerator used purely as a bag of deterministic text guards.

    Imported lazily so `db.py` keeps importing no generator at module load (it never has), and so an
    import failure degrades to a no-op instead of breaking the app's DB layer.
    """
    from generators.blog_gen import BlogGenerator, scrub_markdown_formatting
    gen = BlogGenerator(None, None)
    gen._evidence_blocks = []   # explicit: the renumber/Sources-rebuild half must stay switched off
    return gen, scrub_markdown_formatting


def sanitize_stored_body(field, text):
    """Return (clean_text, changed) for one blog column's value.

    `field` selects the guard set; an unknown field is returned untouched. Non-string values (ints,
    JSON already encoded by the caller, None) pass straight through.
    """
    if field not in SANITIZED_FIELDS or not isinstance(text, str) or not text.strip():
        return text, False
    try:
        gen, _md_fix = _generator()
        out = text
        if field in MARKDOWN_FIELDS:
            # FU201/FU202: spacing, list markers/indent, table blank lines, unclosed emphasis.
            out = _md_fix(out)[0]
            # FU185: em/en-dashes, arrows, decorative bullets, curly quotes, one-char ellipsis.
            # Runs BEFORE the punt/table pass for the same reason `_finalize_article` orders it that
            # way — the " — <url>" separators and "—" placeholder cells written downstream are exempt.
            out = gen._scrub_ai_symbols(out)[0]
            # FU47: a reader-directed "go verify it yourself" punt never ships — in a cell it becomes
            # "—", as a standalone sentence it is dropped. FU55: the model's leaked edit-narration
            # ("… row removed per sourcing rules") is dropped. FU167: `[S1, S2]` → `[S1][S2]` so a
            # later renumber can see the markers. None of the three needs the evidence map.
            out = gen._scrub_punts(out)
            out = gen._scrub_meta(out)
            out = gen._split_grouped_citations(out)
            # FU167: zero-width / bidi carriers, from any source (paste, fetch, another model).
            out = gen._strip_invisible_chars(out)[0]
        else:
            # PROSE + META: the two universal guarantees only — no restructuring.
            out = gen._scrub_ai_symbols(out)[0]
            out = gen._strip_invisible_chars(out)[0]
        return out, (out != text)
    except Exception as exc:   # never break a save because a scrub failed
        print(f"[body_sanitize] skipped {field}: {exc}", flush=True)
        return text, False


def sanitize_fields(fields):
    """Sanitize every recognised key of a {column: value} dict. Returns (new_dict, [changed_fields])."""
    if not isinstance(fields, dict):
        return fields, []
    out, changed = dict(fields), []
    for key in list(out):
        val, did = sanitize_stored_body(key, out[key])
        if did:
            out[key] = val
            changed.append(key)
    return out, changed
