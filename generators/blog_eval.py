"""FU220 — score a FINISHED blog, so a change to the generator can be measured instead of guessed.

Three reports, kept SEPARATE and never merged into one number:

  A. grounding — every checkable claim is re-checked against the page its [S#] cites. This reuses the
     FU208 Verify machinery unchanged (extract → fetch each cited page once → judge each claim ONLY
     against its own page's text). No web search. The headline number is CONTRADICTED claims: facts
     the page it cites says are wrong.
  B. defects — deterministic, free. New detectors for the pipeline artifacts found in real exports,
     plus the checks the generator already runs.
  C. rubric — one call on a DIFFERENT model from the writer, so it is not grading its own work. It is
     an opinion, so it is used for trends only.

Nothing here writes to a blog. Every check is vertical-neutral: nothing keys on a brand, topic or site.
"""
import difflib
import os
import re
import time

# The reviewer model. A different model from the writer (Sonnet by default) so the rubric is not a
# model grading its own output. Overridable for an A/B.
EVAL_RUBRIC_MODEL = os.environ.get("BLOG_EVAL_RUBRIC_MODEL", "claude-opus-4-8")

_SOURCES_SPLIT_RE = re.compile(r"(?im)^[ \t]*#{2,3}[ \t]+Sources\b")
_HEAD_RE = re.compile(r"^(#{2,3})[ \t]+(.+?)[ \t]*$")
_FAQ_HEAD_RE = re.compile(r"^(?:faqs?|frequently\s+asked\s+questions?)\b", re.I)
_PLACEHOLDER_CELL_RE = re.compile(r"^(?:[-—–]+|n/?a|none|tbd|)$", re.I)
_CITE_RE = re.compile(r"\[S\d+\]")
# A bold label that introduces a DOWNSIDE of the option it sits under.
_DOWNSIDE_LABEL_RE = re.compile(
    r"\*\*[^*\n]{0,40}?\b(?:trade-?offs?|limitations?|downsides?|drawbacks?|cons|weakness(?:es)?|"
    r"caveats?|watch[- ]outs?|where\s+it\s+falls\s+short)\b[^*\n]{0,30}\*\*", re.I)
# The opening of the Quick answer — a bold label or its own heading.
_QA_START_RE = re.compile(
    r"^\s*(?:\*\*\s*(?:quick answer|short answer|the short answer|tl;?\s*dr)\s*:?\s*\*\*"
    r"|#{2,4}\s*(?:quick answer|short answer|the short answer|tl;?\s*dr)\b)", re.I)
# A disclaimer. The list spans verticals on purpose (legal, financial, medical, tax, investment,
# professional) — none of them is special-cased.
_DISCLAIMER_RE = re.compile(
    r"does\s+not\s+constitute|do(?:es)?\s+not\s+(?:replace|substitute\s+for)\s+(?:professional|expert)"
    r"|not\s+(?:intended\s+as\s+|to\s+be\s+(?:taken|construed)\s+as\s+)?(?:legal|financial|medical|tax|"
    r"investment|professional)\s+advice"
    r"|consult\s+(?:a|an|your)\s+(?:qualified|licensed|certified)"
    r"|for\s+(?:general\s+)?informational\s+purposes\s+only", re.I)


# ── helpers ──────────────────────────────────────────────────────────────────────────────────────────
def _prose(body):
    """The article without its code-written `## Sources` list."""
    return _SOURCES_SPLIT_RE.split(body or "", maxsplit=1)[0]


def _headings(text):
    """[(line_index, level, heading_text)] for every ## / ### heading."""
    out = []
    for i, ln in enumerate((text or "").split("\n")):
        m = _HEAD_RE.match(ln)
        if m:
            out.append((i, len(m.group(1)), m.group(2).strip().strip("#").strip()))
    return out


def _norm_heading(s):
    s = re.sub(r"[*_`\[\]]", "", s or "").lower()
    s = re.sub(r"[^a-z0-9 ]+", " ", s)
    s = re.sub(r"^the\s+", "", " ".join(s.split()))
    return s


# A generic label before a colon ("Step 1: …", "Option A: …") names a position, not the section.
_GENERIC_PREFIX_RE = re.compile(
    r"^(?:step|phase|stage|part|option|tip|mistake|reason|method|way|pros?|cons?|faq|q|a|note|bonus|"
    r"example|case|rule|question|answer|tier|level|round|day|week|month|year|chapter|section)\b"
    r"|^\d+\b|^[a-z]$")


def _heading_forms(text):
    """The comparable forms of one heading: the text without its parentheticals, each parenthetical
    on its own ("Seige Media (Siege Media)" → "Siege Media"), and the name before a colon or dash
    ("S&W Kitchens: Kitchen-Focused Remodeling" → "S&W Kitchens") — a profile's tagline is what a
    rewrite changes, its name is not."""
    forms = set()
    base = _norm_heading(re.sub(r"\([^)]*\)", " ", text or ""))
    if base:
        forms.add(base)
    for inner in re.findall(r"\(([^)]*)\)", text or ""):
        f = _norm_heading(inner)
        if len(f) >= 4:
            forms.add(f)
    m = re.match(r"^\s*([^:\u2014\u2013|]+?)\s*(?::|\s[\u2014\u2013|-]\s)", text or "")
    if m:
        head = _norm_heading(re.sub(r"\([^)]*\)", " ", m.group(1)))
        if len(head) >= 4 and not _GENERIC_PREFIX_RE.match(head) and not (text or "").rstrip().endswith("?"):
            forms.add(head)
    return forms


def _forms_match(x, y):
    """Same words (in any order), or the same words with a typo in one or two of them. A different
    word is a different section: "…a Kitchen Renovation?" and "…a Bathroom Renovation?" share every
    word but one, and that one word is the whole point."""
    tx, ty = x.split(), y.split()
    sx, sy = set(tx), set(ty)
    if sx == sy:
        return True
    ox, oy = sx - sy, sy - sx
    if len(ox) != len(oy) or len(ox) > 2 or len(tx) != len(ty):
        return False

    def near(a, b):
        return min(len(a), len(b)) >= 4 and difflib.SequenceMatcher(None, a, b).ratio() >= 0.75
    return all(any(near(a, b) for b in oy) for a in ox) and all(any(near(b, a) for a in ox) for b in oy)


def headings_match(a, b):
    """True when two headings name the same section: a shared form (incl. a parenthetical alias),
    the same words reordered, or a typo'd copy."""
    fa, fb = _heading_forms(a), _heading_forms(b)
    if not fa or not fb:
        return False
    if fa & fb:
        return True
    return any(_forms_match(x, y) for x in fa for y in fb)


def _tables(text):
    """[(header_cells, [(row_cells, raw_line)])] for every Markdown pipe table."""
    lines = (text or "").split("\n")
    out, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if s.startswith("|") and re.fullmatch(r"\|?[\s:|-]+\|?", nxt or "x") and "-" in nxt:
            hdr = [c.strip() for c in s.strip("|").split("|")]
            rows, j = [], i + 2
            while j < len(lines) and lines[j].strip().startswith("|"):
                rows.append(([c.strip() for c in lines[j].strip().strip("|").split("|")], lines[j]))
                j += 1
            out.append((hdr, rows))
            i = j
            continue
        i += 1
    return out


def _cell_text(c):
    return re.sub(r"[*_`]", "", c or "").strip()


# ── B. deterministic defect detectors ────────────────────────────────────────────────────────────────
def detect_duplicate_sections(body):
    """Two sections that are the same section — a renamed or misspelled heading re-added from the
    draft (the `_restore_dropped_sections` exact-match guard), or the model writing a profile twice.
    FAQ questions are skipped: they legitimately restate body topics as questions."""
    heads = _headings(_prose(body))
    faq_line = next((ln for ln, lvl, t in heads if _FAQ_HEAD_RE.match(t)), None)
    cand = []
    for ln, lvl, t in heads:
        if _FAQ_HEAD_RE.match(t) or t.lower().startswith("sources"):
            continue
        if faq_line is not None and ln > faq_line and t.rstrip().endswith("?"):
            continue   # an FAQ entry
        cand.append((ln, t))
    hits, seen = [], set()
    for i in range(len(cand)):
        for j in range(i + 1, len(cand)):
            a, b = cand[i][1], cand[j][1]
            if (a, b) in seen:
                continue
            if headings_match(a, b):
                seen.add((a, b))
                hits.append({"check": "duplicate-section",
                             "detail": f'"{b}" repeats the section "{a}"'})
    return hits


def detect_sections_after_faq(body):
    """The FAQ closes the article (before Sources). A section after it is a leftover — usually a
    draft section re-appended after the rewrite, which also breaks the FAQPage extraction."""
    heads = _headings(_prose(body))
    faq = next(((ln, lvl) for ln, lvl, t in heads if _FAQ_HEAD_RE.match(t)), None)
    if not faq:
        return []
    hits = []
    for ln, lvl, t in heads:
        if ln <= faq[0]:
            continue
        if lvl <= faq[1] or not t.rstrip().endswith("?"):
            hits.append({"check": "section-after-faq",
                         "detail": f'"{t}" sits after the FAQ — the FAQ should close the article'})
    return hits


def detect_uncited_table_cells(body, cap=6):
    """A cell with no [S#] in a column where the other cells are cited. A column that is cited
    everywhere else is a sourced dimension — an uncited cell in it is a fact with nothing behind it
    (usually written to fill the cell). The first (name) column and any Source column are skipped."""
    hits = []
    for hdr, rows in _tables(_prose(body)):
        if len(rows) < 2:
            continue
        ncol = len(hdr)
        for col in range(1, ncol):
            name = _cell_text(hdr[col])
            if re.search(r"\bsources?\b", name, re.I):
                continue
            cells = []
            for cells_row, _raw in rows:
                if col < len(cells_row):
                    cells.append((_cell_text(cells_row[0]), cells_row[col]))
            filled = [(r, c) for r, c in cells if not _PLACEHOLDER_CELL_RE.match(_cell_text(c))]
            cited = [x for x in filled if _CITE_RE.search(x[1])]
            if len(cited) < 2 or len(cited) * 2 < len(filled):
                continue
            for r, c in filled:
                if not _CITE_RE.search(c):
                    hits.append({"check": "uncited-cell",
                                 "detail": f'{r or "?"} · {name or "column " + str(col + 1)}: '
                                           f'"{_cell_text(c)[:90]}" has no source while the rest of '
                                           f'the column is cited'})
                    if len(hits) >= cap:
                        return hits
    return hits


def detect_publisher_only_downside(body, brand_name):
    """Only the publisher's own profile carries a bold-labelled downside ("Honest trade-off:",
    "Limitations:") while none of the other options it is compared with does. The page then argues
    against the brand that published it, in the one place a reader compares options side by side."""
    name = _norm_heading(brand_name)
    if not name:
        return []
    text = _prose(body)
    lines = text.split("\n")
    heads = _headings(text)
    sections = []   # (parent_h2_line, heading, block)
    parent = None
    for k, (ln, lvl, t) in enumerate(heads):
        end = heads[k + 1][0] if k + 1 < len(heads) else len(lines)
        if lvl == 2:
            parent = ln
            continue
        if t.rstrip().endswith("?"):
            continue   # a question (FAQ entry), not an option's profile
        sections.append((parent, t, "\n".join(lines[ln + 1:end])))
    mine = [s for s in sections if name in _norm_heading(s[1])]
    if not mine:
        return []
    own = mine[0]
    if not _DOWNSIDE_LABEL_RE.search(own[2]):
        return []
    siblings = [s for s in sections if s[0] == own[0] and s is not own and name not in _norm_heading(s[1])]
    if len(siblings) < 2 or any(_DOWNSIDE_LABEL_RE.search(s[2]) for s in siblings):
        return []
    label = _DOWNSIDE_LABEL_RE.search(own[2]).group(0).strip("*").strip()
    return [{"check": "publisher-only-downside",
             "detail": f'only {brand_name}\'s profile has a "{label}" — none of the {len(siblings)} other '
                       "options it is compared with does, so the page argues against its own publisher"}]


def detect_quick_answer_disclaimer(body):
    """A disclaimer inside the Quick answer — the chunk an answer engine lifts first. A disclaimer
    there dilutes the answer; it belongs further down."""
    lines = _prose(body).split("\n")
    for i, ln in enumerate(lines):
        if not _QA_START_RE.match(ln):
            continue
        region = [ln]
        for nxt in lines[i + 1:]:
            s = nxt.strip()
            if s.startswith("#") or re.fullmatch(r"(?:-{3,}|\*{3,}|_{3,})", s):
                break
            region.append(nxt)
        m = _DISCLAIMER_RE.search("\n".join(region))
        if m:
            txt = " ".join("\n".join(region).split())
            at = txt.lower().find(m.group(0).lower())
            return [{"check": "quick-answer-disclaimer",
                     "detail": "the Quick answer carries a disclaimer (…"
                               + txt[max(0, at - 30):at + 60].strip() + "…) — "
                               "move it below the answer"}]
        return []
    return []


def detect_repeated_citations(body, cap=4):
    """The same [S#] twice in a row ("…answers [S2][S2]") — a merge artifact of the rewrites that
    reads as broken to an editor and adds nothing for an engine."""
    hits = []
    for m in re.finditer(r"\[S(\d+)\](?:\s*\[S\d+\])*?\s*\[S\1\]", _prose(body)):
        start = max(0, m.start() - 60)
        hits.append({"check": "repeated-citation",
                     "detail": f'[S{m.group(1)}] is cited twice in a row: "\u2026{_prose(body)[start:m.end()].strip()}"'})
        if len(hits) >= cap:
            break
    return hits


def existing_checks(gen, brand, body, meta=""):
    """The checks the generator already runs on every blog, re-run on the stored body. Each is
    guarded: a helper that is missing or fails reports nothing rather than breaking the scoreboard."""
    out = []
    article = {"body_markdown": body or "", "meta_description": meta or ""}
    try:
        for it in (gen._verify_consistency(brand, article) or []):
            out.append({"check": "consistency-" + str(it.get("kind") or "other"),
                        "detail": (str(it.get("problem") or "") + (
                            f" ({it.get('detail')})" if it.get("detail") else "")).strip()})
    except Exception as e:
        print(f"[blog_eval] consistency check failed: {e}", flush=True)
    for fn in ("_answer_first_check", "_byline_present_check"):
        try:
            note = getattr(gen, fn)(body or "")
            if note:
                out.append({"check": note.split(":", 1)[0].strip() or fn, "detail": note})
        except Exception as e:
            print(f"[blog_eval] {fn} failed: {e}", flush=True)
    try:
        blocks = gen._blocks_from_sources(body or "", [])
        tools = _compared_names(gen, body)
        note = gen._citation_attribution_check(body or "", blocks, brand, tools)
        if note:
            out.append({"check": "citation-check", "detail": note})
        note = gen._source_class_check(body or "", blocks)
        if note:
            out.append({"check": "source-class", "detail": note})
    except Exception as e:
        print(f"[blog_eval] citation checks failed: {e}", flush=True)
    return out


def _compared_names(gen, body):
    try:
        from generators.blog_gen import _first_table_entities
        return [n for n in _first_table_entities(body or "") if n]
    except Exception:
        return []


def defects_report(gen, brand, body, meta=""):
    name = ((brand or {}).get("name") or "").strip()
    items = []
    items += detect_duplicate_sections(body)
    items += detect_sections_after_faq(body)
    items += detect_uncited_table_cells(body)
    items += detect_publisher_only_downside(body, name)
    items += detect_quick_answer_disclaimer(body)
    items += detect_repeated_citations(body)
    if gen is not None:
        items += existing_checks(gen, brand, body, meta)
    by = {}
    for it in items:
        by[it["check"]] = by.get(it["check"], 0) + 1
    return {"count": len(items), "by_check": by, "items": items}


# ── A. grounding: each cited claim against the page it cites ─────────────────────────────────────────
def grounding_report(gen, brand, body, meta="", max_pages=None):
    """Reuses FU208 unchanged: `_vx_extract` finds the checkable claims and their [S#];
    `_blocks_from_sources` maps S# → URL; `_vx_fetch_pages` reads each cited page once;
    `_vx_judge_pages` judges each claim ONLY against its own cited page and throws away any quote
    that is not really on that page. No web search."""
    # The Verify flow caps pages at 15 to bound a click's cost; a scoreboard that skips pages reports
    # "not checked" for claims it could have checked, so it reads more (pages are cheap to fetch).
    max_pages = max_pages or int(os.environ.get("BLOG_EVAL_MAX_PAGES", "40"))
    claims, _notes = gen._vx_extract(brand or {}, body or "", meta or "", [])
    blocks = gen._blocks_from_sources(body or "", [])

    def _urls(cited):
        out = []
        for i in cited or []:
            if 1 <= i <= len(blocks):
                u = (blocks[i - 1].get("url") or "").strip()
                if u and u not in out:
                    out.append(u)
        return out

    cands = [{"ref": c["id"], "text": c["claim"], "kind": c["kind"], "entity": c["entity"],
              "value": c["value"], "subject": c["subject"], "cited": c["cited"],
              "urls": _urls(c["cited"]), "quote": (c["occurrences"][0]["quote"] if c["occurrences"] else "")}
             for c in claims]
    want = []
    for c in cands:
        for u in c["urls"]:
            if u not in want:
                want.append(u)
    capped = want[max_pages:]
    want = want[:max_pages]
    labels = {b.get("url"): b.get("label") or "" for b in blocks if b.get("url")}
    pages = {}
    stats = {"pages_fetched": 0, "pages_reused": 0, "pages_unreadable": 0}
    gen._vx_fetch_pages(want, pages, stats, labels)
    judged = [dict(c, urls=[u for u in c["urls"] if u in pages]) for c in cands]
    verdicts = gen._vx_judge_pages([c for c in judged if c["urls"]], pages)

    counts = {"claims": len(cands), "checked": 0, "confirmed": 0, "contradicted": 0,
              "not_on_page": 0, "unreadable": 0, "uncited": 0, "not_checked": 0}
    items = []
    for c in judged:
        if not c["urls"]:
            status = "uncited" if not _urls(c["cited"]) else "not_checked"
        elif not any(pages.get(u, {}).get("ok") for u in c["urls"]):
            status = "unreadable"
        else:
            status = (verdicts.get(c["ref"]) or {}).get("status") or "not_on_page"
        counts[status] = counts.get(status, 0) + 1
        if status in ("confirmed", "contradicted", "not_on_page"):
            counts["checked"] += 1
        v = verdicts.get(c["ref"]) or {}
        items.append({"status": status, "claim": c["text"], "kind": c["kind"], "entity": c["entity"],
                      "subject": c["subject"], "value": c["value"],
                      "cited": [f"S{i}" for i in c["cited"]], "url": v.get("url") or (c["urls"][0] if c["urls"] else ""),
                      "page_says": v.get("page_says", ""), "page_value": v.get("page_value", ""),
                      "quote": c["quote"][:240]})
    counts["grounded_share"] = round(counts["confirmed"] / counts["checked"], 3) if counts["checked"] else None
    order = {"contradicted": 0, "not_on_page": 1, "unreadable": 2, "uncited": 3, "not_checked": 4, "confirmed": 5}
    items.sort(key=lambda x: order.get(x["status"], 9))
    return {"counts": counts, "items": items,
            "pages": {u: bool(p.get("ok")) for u, p in pages.items()},
            # FU221 (R6/R5): WHY each page could not be read — "blocked", "not-found", "error",
            # "blocked; web fetch url_not_accessible", … — and the cited links that are simply dead.
            "page_reasons": {u: (getattr(gen, "_fetch_reasons", {}) or {}).get(u, "ok" if p.get("ok") else "unknown")
                             for u, p in pages.items()},
            "dead_links": sorted(u for u, p in pages.items() if not p.get("ok") and str(
                (getattr(gen, "_fetch_reasons", {}) or {}).get(u, "")).startswith("not-found")),
            "pages_capped": len(capped)}


# ── C. reviewer rubric ────────────────────────────────────────────────────────────────────────────
RUBRIC_DIMENSIONS = ("answers_question", "sourcing", "publisher_placement", "fairness",
                     "structure", "polish")


def _rubric_prompt(brand_name, title, meta, body):
    return (
        "You are a senior editor reviewing a published article. The company that published it wrote it "
        "to answer a question its customers ask, hoping search engines and AI answer engines cite it. "
        "Review it the way a demanding independent reviewer would. Be specific and strict.\n\n"
        f"PUBLISHER: {brand_name or '(unknown)'}\nTITLE: {title}\nMETA DESCRIPTION: {meta}\n\n"
        "Score each dimension 0-10:\n"
        "- answers_question: answers the title's question directly and early, with specifics, and the "
        "rest of the page supports that answer.\n"
        "- sourcing: claims that need evidence cite it; the sources are authoritative and independent "
        "where it matters (not only the publisher's own pages or review/affiliate sites); a citation "
        "looks like it supports the sentence it is attached to.\n"
        "- publisher_placement: the publisher is presented credibly — its strengths stated with "
        "specifics, not crowned without evidence — and the page does not argue against its publisher "
        "(a downside only the publisher gets, or conceding the question's deciding point to others).\n"
        "- fairness: the other options are described accurately and even-handedly.\n"
        "- structure: easy to extract — a direct quick answer, question headings answered in their "
        "first sentence, a useful comparison, a clean FAQ; no duplicated or misplaced sections.\n"
        "- polish: reads like a careful human editor finished it — no repetition, filler, hedging, "
        "leftover notes or broken formatting.\n\n"
        "The line \"[Add author byline before publishing]\" is a deliberate placeholder the publisher "
        "replaces with a real byline at publishing time; do not count it as an issue.\n\n"
        "Then give an overall score 0-100 and the 5 most important issues, most important first. For "
        "each issue copy a SHORT verbatim quote (at most 25 words) from the article that shows it, "
        "exactly as written, and a one-line fix.\n"
        'Return JSON only: {"scores": {"answers_question": 0, "sourcing": 0, "publisher_placement": 0, '
        '"fairness": 0, "structure": 0, "polish": 0}, "overall": 0, '
        '"issues": [{"issue": "...", "quote": "...", "fix": "..."}]}\n\n'
        "ARTICLE (Markdown):\n" + (body or "")[:60000])


def _norm_ws(s):
    return " ".join(re.sub(r"[*_`]", "", s or "").split()).lower()


def rubric_report(client, brand_name, title, meta, body):
    """One call on the reviewer model. Scores are clamped; an issue quote that is not really in the
    article is blanked (an invented quote must not look like evidence)."""
    if client is None:
        return {"skipped": True}
    try:
        res = client.call(_rubric_prompt(brand_name, title, meta, body), max_tokens=3000)
    except Exception as e:
        return {"error": str(e)[:200]}
    if not isinstance(res, dict):
        return {"error": (getattr(client, "last_error", "") or "no JSON")[:200]}
    scores = {}
    raw = res.get("scores") if isinstance(res.get("scores"), dict) else {}
    for k in RUBRIC_DIMENSIONS:
        try:
            scores[k] = max(0.0, min(10.0, float(raw.get(k))))
        except (TypeError, ValueError):
            scores[k] = None
    try:
        overall = max(0.0, min(100.0, float(res.get("overall"))))
    except (TypeError, ValueError):
        overall = None
    flat = _norm_ws(body)
    issues = []
    for it in (res.get("issues") or [])[:5]:
        if not isinstance(it, dict):
            continue
        q = str(it.get("quote") or "").strip()
        ok = bool(q) and _norm_ws(q).strip(" .\"'…") in flat
        issues.append({"issue": str(it.get("issue") or "").strip()[:300],
                       "quote": q[:240] if ok else "", "quote_verified": ok,
                       "fix": str(it.get("fix") or "").strip()[:300]})
    return {"model": getattr(client, "model", ""), "scores": scores, "overall": overall, "issues": issues}


# ── Round 2 offline test: the existing Verify fact-check, applied automatically ──────────────────────
def factcheck_body(gen, brand, body, meta=""):
    """What an AUTOMATIC fact-check would do to one stored body, without saving anything: FU208
    `verify_analyze` with web search OFF (each claim judged only against the page it cites), then
    `verify_apply` with its default approvals (high-confidence items only), through the same `_vx_gate`
    an operator's click uses. Internal-defect items are not approved here, so the measured effect is
    the fact-check's alone. Returns (new_body, new_meta, stats)."""
    gen._vx_search = lambda *a, **k: {}                   # no web search: the cited page decides
    gen._verify_content = lambda *a, **k: ([], {})        # facts only — layout is measured separately
    session = gen.verify_analyze(brand or {}, body or "", meta or "")
    items = session.get("items") or []
    decisions = {it["id"]: {"approved": False} for it in items if not str(it.get("ref") or "").startswith("c")}
    out = gen.verify_apply(brand or {}, body or "", meta or "", session, decisions=decisions)
    rep = (out or {}).get("report") or {}
    stats = {"items": len(items),
             "approved": sum(1 for it in items if (it.get("decision") or {}).get("approved")
                             and it["id"] not in decisions),
             "applied": len(rep.get("applied") or []), "refused": len(rep.get("refused") or []),
             "not_approved": len(rep.get("not_approved") or []),
             "changes": [{"problem": (a.get("problem") if isinstance(a, dict) else str(a))}
                         for a in (rep.get("applied") or [])][:12]}
    return (out or {}).get("body") or body, (out or {}).get("meta_description") or meta, stats


# ── one blog ─────────────────────────────────────────────────────────────────────────────────────────
def evaluate_blog(gen, brand, blog, rubric_client=None, grounding=True, rubric=True):
    """Score one stored blog. `gen` is a BlogGenerator (its client pays for the grounding calls);
    `rubric_client` is a ClaudeClient on the reviewer model. Returns the report dict."""
    t0 = time.time()
    body = blog.get("body_markdown") or ""
    meta = blog.get("meta_description") or ""
    name = ((brand or {}).get("name") or "").strip()
    g_cost0 = gen.claude.usage_cost() if (gen is not None and gen.claude is not None) else 0.0
    r_cost0 = rubric_client.usage_cost() if rubric_client is not None else 0.0
    rep = {"blog_id": blog.get("id"), "brand": name, "brand_id": blog.get("brand_id"),
           "seed": blog.get("seed") or "", "title": blog.get("title") or "",
           "updated_at": blog.get("updated_at") or "",
           "words": len(re.findall(r"\w+", _prose(body))),
           "guide": bool(blog.get("guide"))}
    rep["defects"] = defects_report(gen, brand, body, meta)
    if grounding and gen is not None:
        try:
            rep["grounding"] = grounding_report(gen, brand, body, meta)
        except Exception as e:
            rep["grounding"] = {"error": str(e)[:200]}
    if rubric and rubric_client is not None:
        rep["rubric"] = rubric_report(rubric_client, name, blog.get("title") or "", meta, body)
    rep["cost"] = {
        "grounding": round((gen.claude.usage_cost() if (gen is not None and gen.claude is not None) else 0) - g_cost0, 4),
        "rubric": round((rubric_client.usage_cost() if rubric_client is not None else 0) - r_cost0, 4)}
    rep["secs"] = round(time.time() - t0, 1)
    return rep


def summary_row(rep):
    g = (rep.get("grounding") or {}).get("counts") or {}
    r = rep.get("rubric") or {}
    return {"id": rep.get("blog_id"), "brand": rep.get("brand", "")[:18], "words": rep.get("words"),
            "checked": g.get("checked"), "confirmed": g.get("confirmed"),
            "contradicted": g.get("contradicted"), "not_on_page": g.get("not_on_page"),
            "unreadable": g.get("unreadable"), "uncited": g.get("uncited"),
            "defects": (rep.get("defects") or {}).get("count"), "rubric": r.get("overall")}
