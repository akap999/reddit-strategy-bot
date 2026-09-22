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
import collections
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


# ── FU251: damage OUR OWN removal passes leave behind ────────────────────────────────────────────
# Every check below describes a shape no writer produces and no reader forgives — the trace of a
# sentence, a cell or a citation marker taken out of finished prose by the fabrication passes without
# anyone looking at what it was holding up. They are detectors for the scoreboard AND the guard the
# removal passes consult before they commit (`body_damage`), so a pass can no longer create a defect
# this file would report.

# A sentence that cannot stand at the top of a section because it points BACK at one that is gone.
# "This/These/Such <noun>" needs an antecedent; the self-referential nouns below have one (the page
# itself), so they are excluded. A bare "It is/There are" is a dummy subject, not a reference.
_SELF_REF_NOUNS = (r"guide|article|page|post|piece|section|table|comparison|list|chart|breakdown|"
                   r"answer|faq|report")
# FU254 — a HEADING is text the reader has just read, so it can BE the antecedent. Judged on the
# opening sentence alone this detector produced 51 findings across the stored corpus and 45 of them
# were ordinary English whose referent the heading had already supplied ("<section> Can men take X
# alongside Y?" -> "This clinical question must be assessed by a licensed doctor."). It feeds
# body_damage, which is the REMOVAL guard, so every false positive REFUSED a real removal and
# protected the defect underneath it. That asymmetry is why the excuses below are deliberately
# generous: a miss costs one unflagged sentence, a false positive costs a fix.
#
# A noun that names the HEADING ITSELF rather than a thing in the world -- the same idea as the
# self-referential nouns above, for the shape "This <adj> <noun>" where the noun IS the question.
_QUESTION_NOUNS = (r"question|consideration|distinction|difference|issue|matter|topic|point|"
                   r"decision|choice|segment|part|overview|checklist|summary|explainer|scenario|"
                   r"situation|tradeoff|trade-off|contrast|caveat|requirement")
# A CATEGORY noun stands in for the subject the title already named, which the reader always knows.
_CATEGORY_NOUNS = (r"medication|drug|medicine|product|tool|platform|service|provider|company|"
                   r"companies|firm|brand|program|programme|option|approach|method|material|"
                   r"treatment|therapy|therapies|device|plan|package|model|system|solution|"
                   r"vendor|supplier|business|panel|test|screen|metric|measure")
# A heading that names two or more things, or asks for a comparison, is the antecedent of a plural
# opener: "<section> Botric vs Profound: How Do They Compare?" -> "Both platforms address...".
_HEAD_MULTI_RE = re.compile(
    r"\b(?:and|or|vs\.?|versus|between|both|either|neither|compares?|compared?|comparison|"
    r"differs?|different(?:ly)?|difference|differences|same|alongside|against|combining|"
    r"combine[ds]?)\b", re.I)
# A heading that POINTS ("...is this checklist most relevant for?") has already introduced the thing.
_HEAD_DEMO_RE = re.compile(r"\b(?:this|these|that|those)\b", re.I)
_PLURAL_OPEN_RE = re.compile(
    r"^\s*(?:They|Both|Each of them|Neither|Either|These|Those|The same)\b", re.I)
_DEMO_OPEN_RE = re.compile(r"^\s*(?:This|That|These|Those|Such)\s+(.{0,70})", re.I | re.S)
# The word that ends a noun phrase, so "This regulatory distinction IS crucial" yields
# "regulatory distinction" and the head noun can be read out of it.
_NP_STOP_RE = re.compile(
    r"\b(?:is|are|was|were|be|been|being|can|could|will|would|may|might|must|should|shall|does|"
    r"do|did|has|have|had|of|for|in|on|to|that|which|when|from|with|by|as|like|means|matters|"
    r"applies|holds|carries|requires|comes|remains)\b", re.I)
# "X and Y ALSO showed no difference" -- an additional RESULT, which only means something beside the
# result before it. "is also used off-label", "may also include" and "(also known as)" are ordinary
# English, and they were 15 of the 16 findings this arm produced.
_REPORTING_VERB_RE = re.compile(
    r"\b(?:showed?|found|reported|demonstrated|revealed|confirmed|suggested|indicated|"
    r"improved|worsened|increased|decreased|declined|rose|fell|dropped|gained|"
    r"averaged|reached|measured|scored|registered|outperformed|lagged|matched)\b", re.I)
_CONTENT_WORD_RE = re.compile(r"[A-Za-z][A-Za-z-]{4,}")


def _heading_supplies(head, first):
    """FU254 -- True when the SECTION HEADING already gave the reader what the opening sentence
    points back at. Shape-based and field-neutral: a comparison heading answers a plural opener, a
    heading noun repeated in the opener answers a demonstrative one."""
    if not head:
        return False
    if _PLURAL_OPEN_RE.match(first):
        if _HEAD_MULTI_RE.search(head):
            return True
        # or the sentence names them itself: "Both Botric and Profound offer citation tracking."
        lead = " ".join(first.split()[:12])
        if re.search(r"\b(?:and|or)\b", lead, re.I) and len(_CONTENT_WORD_RE.findall(lead)) >= 2:
            return True
    m = _DEMO_OPEN_RE.match(first)
    if m:
        np = _NP_STOP_RE.split(m.group(1), 1)[0]
        toks = [w.lower() for w in _CONTENT_WORD_RE.findall(np)][:4]
        for t in toks:
            if re.fullmatch(r"(?:%s)s?" % _QUESTION_NOUNS, t) or \
                    re.fullmatch(r"(?:%s)(?:e?s)?" % _CATEGORY_NOUNS, t):
                return True
        hw = {w.lower()[:6] for w in _CONTENT_WORD_RE.findall(head)}
        if any(t[:6] in hw for t in toks):
            return True
        if _HEAD_DEMO_RE.search(head):
            return True
    return False
# "This IS the rationale" points at the heading just read and is ordinary English; "This PROTOCOL is
# necessary" names a thing the reader was never shown. Only the second shape is damage, so a
# demonstrative followed by a verb is let through.
_DEMONSTRATIVE_VERBS = (
    r"is|are|was|were|be|been|being|can|could|will|would|may|might|must|should|shall|"
    r"does|do|did|has|have|had|means|matters|happens|makes|allows|requires|reflects|explains|"
    r"varies|depends|applies|works|helps|changes|comes|includes|leaves|gives|remains|tends|"
    r"differs|holds|says|shows|puts|takes|creates|becomes|"
    # FU254 -- every one of these was missing and turned an ordinary sentence into a "finding"
    r"measures|covers|checks|tests|looks|tells|assesses|evaluates|captures|detects|indicates|"
    r"affects|determines|influences|drives|reduces|raises|lowers|improves|prevents|avoids|adds|"
    r"costs|saves|starts|ends|runs|lasts|ranges|sits|carries|reflects|signals|flags")
_STRANDED_OPEN_RE = re.compile(
    r"^\s*(?:"
    r"(?:This|That|These|Those|Such)\s+(?!(?:%s)\b)(?!(?:%s)\b)[a-z]"
    % (_SELF_REF_NOUNS, _DEMONSTRATIVE_VERBS)
    + r"|(?:Also|Additionally|In addition|Similarly|Likewise|However|But|And|Moreover|Furthermore|"
      r"Instead|Meanwhile|Conversely|Either way|By contrast|On the other hand|The same)\b"
    r"|(?:They|Both|Each of them|Neither|Either)\s+[a-z]"
    r")", re.I)
# "X and Y ALSO showed …" — the "also" that only makes sense after a sentence that is gone. Only in
# the opening clause of a section's first sentence, so an "also" deep in a paragraph is untouched.
_STRANDED_ALSO_RE = re.compile(r"^[^.;:]{0,90}?\balso\b", re.I)
# A splice: text rejoined where something was cut out, leaving the punctuation of both halves.
# The lookbehind is what keeps "e.g.," and "et al.," out of it: an abbreviation's dot is preceded by
# a short token or another dot, a real sentence end by four or more letters ("…in-person visit.,").
_BROKEN_JOIN_RE = re.compile(
    r"(?<=[a-z]{4})[.!?]\s*[,;:]"        # a sentence ended, then a clause was joined onto it
    r"|,\s*[.!?](?![.!?])"               # "…, ." — the other half of the same cut
    r"|\s[,;:](?=\s)"                   # "…the same , since…" — a noun removed from between them
    r"|\(\s*\)|\[\s*\]"               # an emptied parenthetical
    r"|[,;:]\s*[,;:]")                  # two separators with nothing between them
_BACKREF_MIN_WORDS = 4          # a 3-word opener is a label, not a stranded sentence
_STUB_ANSWER_CHARS = 60         # an FAQ answer shorter than this is a fragment, not an answer


def _first_sentence(par):
    m = re.match(r"\s*(.+?[.!?])(?:\s|$)", par or "", re.S)
    return (m.group(1) if m else (par or "")).strip()


def _sections(body):
    """[(heading_text, level, [paragraph, ...])] — the article's sections, tables excluded from the
    paragraph list so a table row is never read as prose."""
    lines = _prose(body).split("\n")
    out, cur = [], ("", 0, [])
    buf = []

    def _close():
        par = " ".join(x.strip() for x in buf).strip()
        if par:
            cur[2].append(par)
        buf.clear()

    for ln in lines:
        m = _HEAD_RE.match(ln)
        if m:
            _close()
            out.append(cur)
            cur = (m.group(2).strip().strip("#").strip(), len(m.group(1)), [])
            continue
        st = ln.strip()
        if not st or st.startswith("|") or st.startswith(">") or st.startswith("---") \
                or st.startswith("*[") or st.startswith("#"):
            _close()
            continue
        if st.startswith(("-", "*", "+")) or re.match(r"^\d+[.)]\s", st):
            _close()
            cur[2].append(re.sub(r"^(?:[-*+]|\d+[.)])\s*", "", st))
            continue
        buf.append(st)
    _close()
    out.append(cur)
    return [x for x in out if x[0] or x[2]]


def detect_blank_source_cells(body, cap=8):
    """A Source column cell with nothing in it. The Source column is the one column the width rules
    never drop, so when a pass strips the [S#] that was the cell's whole content the gap is written
    out as "—" and published. A reader sees a comparison table that cites every row but one."""
    hits = []
    for hdr, rows in _tables(_prose(body)):
        for col in range(len(hdr)):
            if not re.search(r"\bsources?\b|\bcitations?\b|\bevidence\b", _cell_text(hdr[col]), re.I):
                continue
            for cells, _raw in rows:
                if col >= len(cells):
                    continue
                if _PLACEHOLDER_CELL_RE.match(_cell_text(cells[col])):
                    hits.append({"check": "blank-source-cell",
                                 "detail": f'{_cell_text(cells[0])[:60] or "?"}: the '
                                           f'"{_cell_text(hdr[col])}" cell is empty'})
                    if len(hits) >= cap:
                        return hits
    return hits


def detect_stranded_reference(body, cap=8):
    """A section or FAQ answer that OPENS by pointing back at something the reader has not been
    told — "This strict protocol is necessary because…", "Waist circumference and lipid markers
    also showed…". Nobody writes a section that way; it is what is left when the sentence in front
    of it was removed and nothing checked what depended on it."""
    hits = []
    for head, _lvl, pars in _sections(body):
        if not pars or not head:
            continue
        first = _first_sentence(pars[0])
        if len(first.split()) < _BACKREF_MIN_WORDS:
            continue
        why = ""
        if _STRANDED_OPEN_RE.match(first):
            # FU254: not stranded when the heading the reader just read IS the antecedent
            if _heading_supplies(head, first):
                continue
            why = "opens with a back-reference"
        elif _STRANDED_ALSO_RE.match(first):
            # FU254: an additional RESULT is stranded ("…also SHOWED no difference"); an ordinary
            # "is also used off-label" or "(also known as)" is not, and nor is an "also" whose
            # subject the heading already named.
            if not _REPORTING_VERB_RE.search(first):
                continue
            _subj = re.split(r"\balso\b", first, 1, re.I)[0]
            _hw = {w.lower()[:6] for w in _CONTENT_WORD_RE.findall(head or "")}
            if any(w.lower()[:6] in _hw for w in _CONTENT_WORD_RE.findall(_subj)):
                continue
            why = 'opens with "also"'
        if why:
            hits.append({"check": "stranded-reference",
                         "detail": f'"{head[:60]}" {why}: "{first[:90]}"'})
            if len(hits) >= cap:
                return hits
    return hits


def detect_broken_join(body, cap=8):
    """Punctuation from both halves of a cut, left side by side — "…without an in-person visit.,
    which includes…". The de-duplication and removal passes splice text back together and none of
    them repairs the seam."""
    hits = []
    for ln in _prose(body).split("\n"):
        st = ln.strip()
        if not st or st.startswith("#") or st.startswith("---"):
            continue
        m = _BROKEN_JOIN_RE.search(st)
        if m and not re.match(r"^\|?[\s:|-]+\|?$", st):
            hits.append({"check": "broken-join",
                         "detail": f'"{st[max(0, m.start() - 45):m.end() + 45]}"'})
            if len(hits) >= cap:
                return hits
    return hits


def detect_duplicated_paragraph(body, min_words=25, cap=6):
    """The same paragraph printed twice. A clause-level de-duplicator cannot see it, and the
    restore-dropped-sections pass can create it by putting a stale copy back beside the corrected
    one. Also catches a paragraph that repeats a long span of itself."""
    hits, seen = [], {}
    for head, _lvl, pars in _sections(body):
        for par in pars:
            norm = re.sub(r"\W+", " ", par.lower()).strip()
            if len(norm.split()) < min_words:
                continue
            if norm in seen:
                hits.append({"check": "duplicated-paragraph",
                             "detail": f'"{par[:80]}…" appears twice ({seen[norm]} and {head or "top"})'})
            else:
                seen[norm] = head or "top"
            if len(hits) >= cap:
                return hits
    return hits


def detect_repeated_sentence(body, min_words=12, cap=6):
    """The same sentence printed twice in the prose. This is the shape a paragraph spliced onto its
    own tail takes — the clause-level de-duplicator only looks inside ONE line at a separator, so a
    whole repeated sentence passes it untouched. A writer does not repeat a 12-word sentence."""
    hits, seen, in_faq = [], set(), False
    for _head, _lvl, pars in _sections(body):
        if _FAQ_HEAD_RE.match(_head or ""):
            in_faq = True
        if in_faq:
            continue        # an FAQ answer restating a body sentence is what an FAQ is for
        for par in pars:
            for sent in re.findall(r"[^.!?]+[.!?]", par):
                norm = re.sub(r"\W+", " ", sent.lower()).strip()
                if len(norm.split()) < min_words:
                    continue
                if norm in seen:
                    hits.append({"check": "duplicated-sentence",
                                 "detail": f'"{sent.strip()[:100]}…" appears twice'})
                    if len(hits) >= cap:
                        return hits
                seen.add(norm)
    return hits


def detect_stub_answer(body, cap=6):
    """An FAQ question answered by a fragment. "Yes, when equivalent plasma concentrations are
    achieved." is what is left of an answer, not an answer — and it is what the schema extractor
    publishes as the page's answer to that question."""
    hits, in_faq = [], False
    for head, lvl, pars in _sections(body):
        if _FAQ_HEAD_RE.match(head or ""):
            in_faq = True
            continue
        if not in_faq or not (head or "").rstrip().endswith("?"):
            continue
        ans = " ".join(pars).strip()
        if ans and len(ans) < _STUB_ANSWER_CHARS:
            hits.append({"check": "stub-answer",
                         "detail": f'"{head[:70]}" is answered in {len(ans)} characters: "{ans}"'})
            if len(hits) >= cap:
                return hits
    return hits


# A heading-shaped fragment: title case or a known heading, no terminal punctuation, short.
_ORPHAN_TAIL_RE = re.compile(r"(?<=[.!?])\s+((?:[A-Z][A-Za-z0-9&/'’-]*)(?:\s+(?:[A-Z][A-Za-z0-9&/'’-]*|and|or|of|for|in|the)){0,5})\s*$")


def detect_trailing_orphan(body, cap=6):
    """A paragraph or FAQ answer that ends in a heading-shaped fragment.

    "Simply having a well-designed website is not enough to ensure a strong online presence for AI
    citations. Community Mentions" — a phrase stuck on the end with no sentence around it. Every
    other detector here is anchored to the START of a unit or needs a punctuation seam, and this
    shape has neither: it is longer than the stub floor, and its full stop is in the right place."""
    hits = []
    for _head, _lvl, pars in _sections(body):
        for par in pars:
            par = par.rstrip()
            # A finished paragraph closes with punctuation. A list item may not — but a list item is
            # a single fragment by design, with no sentence boundary inside it, and the regex below
            # requires one. That pair of conditions is what separates an orphan from ordinary prose:
            # measured across every fixture in the repo, it fires once, on the reported case.
            if not par or par[-1] in ".!?:)]\"'”’*|":
                continue
            m = _ORPHAN_TAIL_RE.search(par)
            if not m:
                continue
            frag = m.group(1).strip()
            if len(frag.split()) > 6:
                continue
            hits.append({"check": "trailing-orphan",
                         "detail": f'"…{par[-70:]}" ends on the fragment "{frag}"'})
            if len(hits) >= cap:
                return hits
    return hits


# ── FU253: a constraint stated once is information; stated five times it is the article ──────────
# Measured across 216 stored articles: 41 restate a rule or caveat across three or more sections, 20
# across five, and the worst gives a third of the page to it. In the article that prompted this, the
# same compounding rule fills three sections and 14% of a page whose question is what something
# costs — and the publisher SELLS the class the rule constrains.
#
# Nothing saw it. The three passages are PARAPHRASES of each other (0.29-0.45 similarity) so the
# duplicate detectors, which need near-exact matches, are silent; the one sentence that is verbatim
# sits in the body and the FAQ, which `detect_repeated_sentence` exempts on purpose. And FU243's
# `_CATEGORY_RULE_RE` matches the rule verb in all three — it has always seen the rule, three times,
# and only ever asked whether the brand is pitched fairly beside it, never how much of the page it is.
#
# The boundary this is built around: it is about REPETITION AND PROPORTION, never about dropping an
# inconvenient truth. The article must still state the constraint, once, in full — `detect_*` here
# only reports that it was stated again, and again. An article whose QUESTION is the rule keeps its
# depth, because the rule then belongs in every section and the share test is what separates the two.
_CONSTRAINT_RE = re.compile(
    r"\b(?:may|must|can|cannot|can't|could|shall)\s+not\s+\w+"
    r"|\bcannot\b|\bcan't\b"
    r"|\b(?:is|are|was|were)\s+not\s+(?:permitted|allowed|eligible|approved|covered|authori[sz]ed|"
    r"licen[sc]ed|available|reimbursed)\b"
    r"|\b(?:is|are)\s+(?:prohibited|banned|barred|restricted|excluded|ineligible|unapproved|illegal)\b"
    r"|\b(?:do|does|did)\s+not\s+(?:qualify|cover|reimburse|permit|allow|apply)\b"
    r"|\bnot\s+(?:federally|legally|nationally|officially|formally)\s+approved\b"
    r"|\bineligible\b|\bexcluded\s+from\b|\bwind-?down\b|\bdeadline\b", re.I)
_CAVEAT_HEDGE_RE = re.compile(
    r"\b(?:confirm(?:\s+the)?\s+(?:current\s+)?status|check\s+with\s+(?:your|a)\s+\w+"
    r"|consult\s+a\s+licen[sc]ed|status\s+(?:can|may)\s+change|subject\s+to\s+change"
    r"|verify\s+(?:the\s+)?(?:current|eligibility|status))\b", re.I)
# Words that carry no identity — two passages are the same constraint when their DISTINCTIVE tokens
# overlap, not when they share "the", "patients" or "should".
_CONSTRAINT_STOP = frozenset("""about above after again against under below between both during each
further more most other some such only very will just than then once here there when where which
while with without would could should must their there these those they this that have been being
what your yours because before after through during above below from into over under again
patients patient provider providers prescriber prescribers current currently confirm please should
program programs programme programmes product products service services company companies""".split())
_CONSTRAINT_SECTIONS = int(os.environ.get("BLOG_CONSTRAINT_SECTIONS", "3"))


def _constraint_tokens(par):
    return {w for w in re.findall(r"[a-z0-9][a-z0-9-]{4,}", (par or "").lower())
            if w not in _CONSTRAINT_STOP}


def _constraint_passages(body):
    """[(section heading, paragraph, distinctive tokens)] for every passage stating a rule or a
    caveat. Tables and headings are not passages — a rule belongs in prose.

    "Distinctive" is relative to THIS ARTICLE. A token in the title, or one that turns up in half the
    sections, identifies the topic and not the constraint: in a compounding article every rule
    mentions compounding, so grouping on it merges three unrelated rules into one. Measured, that
    false-grouped the FDA shortage status, the "essentially a copy" standard and a clinical-necessity
    requirement in one article, and a state-availability note with a pharmacy-law rule in another."""
    secs = _sections(body)
    common, nsec = collections.Counter(), 0
    for _h, _l, pars in secs:
        if not pars:
            continue
        nsec += 1
        for t in {t for p in pars for t in _constraint_tokens(p)}:
            common[t] += 1
    heads = {t for _h, _l, _p in secs for t in _constraint_tokens(_h)}
    everywhere = {t for t, n in common.items() if nsec and n > max(1, nsec // 2)} | heads
    out = []
    for head, _lvl, pars in secs:
        for par in pars:
            if len(par.split()) < 12:
                continue                       # a clause-length reference is the GOAL, not a repeat
            if _CONSTRAINT_RE.search(par) or _CAVEAT_HEDGE_RE.search(par):
                out.append((head or "(top)", par, _constraint_tokens(par) - everywhere))
    return out


def _same_constraint(a, b, floor=0.30):
    """Two passages state the same constraint when their distinctive tokens overlap.

    Jaccard, not a min-length ratio: with the smaller set as the denominator a short caveat matched
    almost any long passage it shared three words with. Paraphrase-tolerant on purpose — the
    passages that prompted this sit at 0.29-0.45 whole-text similarity, so anything keyed on exact
    text sees nothing at all."""
    if len(a) < 3 or len(b) < 3:
        return False
    return len(a & b) / float(len(a | b)) >= floor


def detect_repeated_constraint(body, cap=4):
    """One rule or caveat restated across section after section.

    Reports the sections and the share of the article it occupies. It does NOT say the constraint is
    wrong or that it should go — only that it has been stated more times than a reader needs."""
    passages = _constraint_passages(body)
    if len(passages) < _CONSTRAINT_SECTIONS:
        return []
    total = len(re.sub(r"\s+", " ", _prose(body)).split()) or 1
    groups = []
    for head, par, toks in passages:
        for grp in groups:
            if _same_constraint(toks, grp["toks"]):
                grp["items"].append((head, par))
                grp["toks"] = grp["toks"] | toks
                break
        else:
            groups.append({"toks": set(toks), "items": [(head, par)]})
    hits = []
    for grp in groups:
        heads = list(dict.fromkeys(h for h, _p in grp["items"]))
        words = sum(len(p.split()) for _h, p in grp["items"])
        share = words / float(total)
        # REPETITION is the defect, so the section count is the trigger and the share is reported
        # alongside it. Firing on share alone flagged a clinical-efficacy comparison and an
        # insurance-concierge description in two sections apiece — neither a constraint, and both a
        # body-and-FAQ pair, which is what an FAQ is for. A constraint that is long in ONE place is
        # verbosity, a different complaint with a different fix.
        if len(heads) < _CONSTRAINT_SECTIONS:
            continue
        hits.append({"check": "repeated-constraint",
                     "detail": f"the same constraint is stated in {len(heads)} sections "
                               f"({words} words, {share * 100:.0f}% of the article): "
                               + " · ".join(h[:34] for h in heads[:4])})
        if len(hits) >= cap:
            break
    return hits


# An article whose QUESTION is the rule may spend the page on it. Shape-based: the title asks about
# legality, eligibility or compliance. #130 "Is Compounded Tirzepatide Legit?" gives 22% of itself to
# regulatory text and that is the article working, not failing.
_RULE_TOPIC_TITLE_RE = re.compile(
    r"\b(?:legal|legit|legitimate|lawful|allowed|permitted|prohibited|banned|compliance|compliant|"
    r"regulat\w+|rules?|law|laws|eligib\w+|qualif\w+|approved|approval|covered|coverage|"
    r"licen[sc]\w+|permit\w*|safe\b|risks?)\b", re.I)
# Measured over 217 stored articles: median 3% of the page is rule-or-caveat text, p75 9%, p90 15%,
# p95 18%. Past a fifth of the article it has stopped being context and become the subject.
_CONSTRAINT_BLOAT_SHARE = float(os.environ.get("BLOG_CONSTRAINT_BLOAT", "0.20"))


def detect_constraint_bloat(body, title=""):
    """A page that is mostly caveat.

    Separate from `detect_repeated_constraint`: that one is about saying ONE thing repeatedly, this
    one about how much of the page is rule text at all. A price article that spends a fifth of itself
    on what is and is not permitted has answered a question nobody asked — and the reader who came
    for the price has to wade through it."""
    passages = _constraint_passages(body)
    if not passages:
        return []
    head = title or next((t for _ln, _lvl, t in _headings(body)), "")
    if _RULE_TOPIC_TITLE_RE.search(head or ""):
        return []
    total = len(re.sub(r"\s+", " ", _prose(body)).split()) or 1
    words = sum(len(p.split()) for _h, p, _t in passages)
    share = words / float(total)
    if share < _CONSTRAINT_BLOAT_SHARE:
        return []
    return [{"check": "constraint-bloat",
             "detail": f"{share * 100:.0f}% of the article is rule or caveat text "
                       f"({words} of {total} words, {len(passages)} passages) — the median article "
                       f"gives it 3%"}]


# A heading that ASKS what something costs. Narrow on purpose: naive heading-to-body term overlap
# flagged 8 of 13 headings in the article that prompted this, because a section can answer "What
# Drives the Price Difference" perfectly without repeating the word "drives".
# "How much" alone is not a price question — "how much weight can a man expect to lose" is the same
# shape and wants a number of a different kind. Every branch needs a MONEY word.
_ASKS_PRICE_RE = re.compile(
    r"\bhow much\b[^?]{0,44}\b(?:cost|costs|price|priced|charge|charges|pay|spend|fee|fees)\b"
    r"|\bwhat (?:is|are|does)\b[^?]{0,30}\b(?:price|cost|charge|fee)\b"
    r"|\b(?:price|cost) of\b", re.I)
# What COUNTS as answering "what does it cost": a money figure, a rate expressed as a percentage (a
# contingency fee IS the price), or saying it is free. "OutSail is free for buyers. There are no
# fees" is a complete answer and carries no digits at all — 4 of the first 12 findings were that.
_ANY_MONEY_RE = re.compile(
    r"(?:\$|€|£)\s?\d"
    r"|\d[\d,.]*\s?%"
    r"|\b(?:free|no (?:cost|fee|fees|charge|charges)|at no (?:cost|charge)|zero (?:cost|fees?))\b",
    re.I)


def _sections_with_tables(body):
    """[(heading, section text)] — like `_sections`, but the text keeps TABLE ROWS.

    `_sections` drops them so a table row is never read as prose, which is right for every other
    detector here and wrong for this one: a section whose answer IS the comparison table looks empty
    to it. Measured, that was 2 of the 3 false positives in the first cut."""
    out, head, cur = [], "", []
    for ln in _prose(body).split("\n"):
        if _HEAD_RE.match(ln):
            out.append((head, "\n".join(cur)))
            head, cur = ln.lstrip("#").strip(), []
        else:
            cur.append(ln)
    out.append((head, "\n".join(cur)))
    return out


def detect_price_question_unanswered(body, cap=4):
    """A heading that asks what something costs, above a section that never says.

    The reported case: "What Is the Retail Price of Brand-Name Wegovy Without Insurance?" over a
    section that talks about Medicare and gives no price at all. `_answer_first_check` cannot see it
    — that one looks for nine stalling phrases in the first sentence, and this section opens with a
    fluent, confident, entirely on-topic-sounding sentence about something else."""
    hits = []
    for head, text in _sections_with_tables(body):
        if not head.rstrip().endswith("?") or not _ASKS_PRICE_RE.search(head):
            continue
        if not text.strip() or _ANY_MONEY_RE.search(text):
            continue
        hits.append({"check": "price-question-unanswered",
                     "detail": f'"{head[:74]}" is asked and the section under it states no price'})
        if len(hits) >= cap:
            break
    return hits


def body_damage(body):
    """Every mutilation detector at once. The removal passes call this BEFORE and AFTER a removal:
    a removal that raises the count is widened to the whole paragraph, or refused. Cheap, no
    network, no model — it is a handful of regexes over one string."""
    return (detect_blank_source_cells(body) + detect_stranded_reference(body)
            + detect_broken_join(body) + detect_duplicated_paragraph(body)
            + detect_repeated_sentence(body) + detect_stub_answer(body)
            + detect_trailing_orphan(body))


# A column that DESCRIBES what a price covers. `_YESNO_DIM_RE` in blog_gen needs a trailing "?", so
# a column headed "What's Included" is owned by nobody: code writes the price cell and the model
# writes the one beside it, and no function in the repo reads two cells of the same row.
_INCLUSION_COL_RE = re.compile(r"^\s*(?:what'?s?\s+)?(?:included|includes|inclusions|covers?|"
                               r"coverage|what\s+you\s+get)\b", re.I)
# What the rendered composition says is NOT in the price (FU251 wording, via `_format_price_value`).
_EXCLUDES_RE = re.compile(r"the product only|product billed separately|billed separately", re.I)
# The component a "product only" price leaves out.
_SECOND_CHARGE_RE = re.compile(
    r"\b(?:membership|subscription|programme|program|plan|service|consult|consultation|"
    r"coaching|platform)\s*(?:fee|fees|cost|charge)?\b", re.I)
# …and the words that make naming it honest rather than contradictory.
_EXTRA_QUALIFIER_RE = re.compile(
    r"\b(?:required|extra|additional|separate|separately|not included|excluded|on top|"
    r"billed|charged|add-?on|plus)\b", re.I)


def detect_row_contradiction(body, cap=4):
    """A comparison row whose price cell and inclusion cell disagree.

    The reported case: the price cell reads "$349 (per month, the product only)" and the cell beside
    it reads "Ro Body membership; insurance check offered", which a reader takes to mean the
    membership is covered. It is $74-$149 a month on top. Code wrote the first cell and the model
    wrote the second, and nothing in the repo has ever read two cells of one row."""
    hits = []
    for hdr, rows in _tables(_prose(body)):
        pcols = [i for i, h in enumerate(hdr) if re.search(r"pric|cost|fee", _cell_text(h), re.I)]
        icols = [i for i, h in enumerate(hdr) if _INCLUSION_COL_RE.search(_cell_text(h))]
        if not pcols or not icols:
            continue
        for cells, _raw in rows:
            price = " ".join(cells[i] for i in pcols if i < len(cells))
            if not _EXCLUDES_RE.search(price):
                continue
            for i in icols:
                if i >= len(cells):
                    continue
                inc = cells[i]
                m = _SECOND_CHARGE_RE.search(inc)
                if not m or _EXTRA_QUALIFIER_RE.search(inc):
                    continue
                hits.append({"check": "row-contradiction",
                             "detail": f'{_cell_text(cells[0])[:34] or "?"}: the price is '
                                       f'"{_cell_text(price)[:40]}" but the "{_cell_text(hdr[i])[:22]}" '
                                       f'cell lists "{m.group(0)}" as if it were covered'})
                if len(hits) >= cap:
                    return hits
    return hits


def editorial_findings(body, title=""):
    """FU253 — findings about what the article SAYS, as distinct from damage our own removal passes
    did to it.

    They are kept apart because `body_damage` is the guard `_apply_removals_without_damage` consults:
    a removal that raises its count is widened or refused. An editorial finding must never reach it.
    Measured the hard way — with `price-question-unanswered` inside `body_damage`, removing the only
    (unsourced) figure from a "What does it cost?" section raised the count, so the removal was
    refused and the unsourced figure would have shipped. `constraint-bloat` is worse: it is a SHARE,
    so any removal at all can push it up. A detector that switches off a removal is a detector that
    protects the defect."""
    return (detect_repeated_constraint(body) + detect_constraint_bloat(body, title)
            + detect_price_question_unanswered(body) + detect_row_contradiction(body))


# ── FU252: what the REWORDING changed about what the article ASSERTS ─────────────────────────────
# The rewrite pass is a model transformation with three gates — a length band, [S#] set containment
# and a numeric fact gate — plus the FU221 guard, which restores blocks whose citations, figures or
# key terms changed. Everything else is unwatched, and that is where the reported defects live: a
# wrong acronym expansion, a guarantee, a dropped link, a figure that lost its "+", a bold subject
# that lost its verb.
#
# These are PAIR detectors: they take the input and the output and report what changed, which is a
# different question from `body_damage`'s "is this body damaged". A rewording is allowed to change
# every word; it is not allowed to change what the article claims.
#
# Each finding carries a severity. "blocking" changes what the article asserts and is never
# acceptable at any count; "budgeted" is a quality defect a reader survives, tolerated up to a limit
# that scales with the article. Measured across 68 stored pairs before any of this existed:
# strength-added 43, plus-dropped 16, links-lost 8, bold-lead-broken 6.

# A figure's MODIFIER — the part that turns a value into a bound. No regex in the codebase captured
# any of these, so "DR 70+" and "DR 70" were the same figure to every gate.
_FIG_MOD_RE = re.compile(
    r"(?P<pre>\b(?:at least|no less than|more than|over|under|fewer than|less than|up to|"
    r"as many as|around|about|approximately|roughly|nearly|almost|minimum(?: of)?|maximum(?: of)?)\s+)?"
    r"(?P<sym>[~≈<>≥≤]\s*)?"
    # (?<![A-Za-z])(?<![A-Za-z]-) — a digit hyphenated or run onto letters belongs to a product CODE,
    # not to a figure. `rewrite_guard._NUM_RE` already carries this exact guard; without it here,
    # every paragraph naming a hyphenated product looked like it contained the figure "1".
    r"(?P<num>(?<![A-Za-z])(?<![A-Za-z]-)\d[\d,]*(?:\.\d+)?)"
    r"(?P<plus>\s*\+)?", re.I)
# The wordings a "+" may legitimately become. "DR 70+" → "a minimum DR of 70" keeps the meaning;
# "DR 70+" → "an average DR of 70" does not.
_LOWER_BOUND_WORDS = re.compile(
    r"\b(?:at least|no less than|more than|over|minimum|minimums?|or more|plus|upward|upwards|"
    r"exceed(?:s|ing)?|north of|starting (?:at|from)|from)\b", re.I)
# "over the past 10 years" is a time span, not a floor of ten. Measured: without this the rewrite's
# "over the past 10 years" was read as a lower bound, so the input's genuine "10+ years" looked
# preserved and the one defect this detector exists for went unreported.
_TEMPORAL_SPAN_RE = re.compile(
    r"\b(?:the\s+|an?\s+)?(?:past|last|next|previous|coming|"
    r"period of|span of|course of|stretch of|window of)\s*$", re.I)
_UPPER_BOUND_WORDS = re.compile(r"\b(?:up to|under|fewer than|less than|maximum|at most|no more than)\b", re.I)
_APPROX_WORDS = re.compile(r"\b(?:about|around|approximately|roughly|nearly|almost|circa|some)\b", re.I)
# A bound stated AFTER the figure: "a BMI of 30 or higher", "25,000 and above". An optional unit or
# percent sign may sit between ("30% or higher").
# The trailing lookahead matters: in "an average DR of 70 and over 25,000 monthly visitors" the
# "and over" belongs to 25,000, not to 70. Without it the rewrite that flattened "DR 70+" to a bare
# "DR 70" looked like it had kept the bound.
_TRAIL_LOWER_RE = re.compile(
    r"^\s*(?:%|[a-zA-Z/²³]{1,12})?\s*(?:or\s+(?:more|higher|above|greater|over)|"
    r"and\s+(?:above|over|up)|plus\b|\+)(?!\s*[\d$€£])", re.I)
_TRAIL_UPPER_RE = re.compile(
    r"^\s*(?:%|[a-zA-Z/²³]{1,12})?\s*(?:or\s+(?:less|lower|fewer|below|under)|"
    r"and\s+(?:below|under))(?!\s*[\d$€£])", re.I)
# A citation marker is not a figure. `rewrite_guard._nums` already strips these before comparing
# numbers; without the same strip here, "[S1]" and "[S14]" were read as the values 1 and 14.
_CITE_STRIP_RE = re.compile(r"\[S\d+\]")
# "…visible over 3-6 months" spans the range; it does not mean "more than three". A bound word in
# front of the FIRST half of a range is spanning it.
_RANGE_TAIL_RE = re.compile(r"^\s*(?:-|\u2013|\u2014|to|and)\s*\d", re.I)


def _figure_bounds(text):
    """{normalised value: Counter of bound kinds} for every figure in `text`.

    A bound is "lower" (70+, at least 70, 30 or higher), "upper" (up to 70), "approx" (~70, about 70)
    or "exact". COUNTED, not set-valued: the article that motivated this says "DR 70+" three times
    and the rewrite kept the one inside a table — which the guard restores wholesale — while both
    prose mentions became a bare "DR 70". A set comparison sees the surviving cell and reports
    nothing.

    A bound word binds the NEAREST number. Measured on 68 stored pairs, getting that wrong is what
    produced almost all the noise: "reached at least 15% against 56.2%" was reading "at least" onto
    56.2, and "over 20,000 reviews" was read as exact because the regex had already consumed "over"
    so the lookback landed in front of it."""
    out = {}
    text = _CITE_STRIP_RE.sub(" ", text or "")
    for m in _FIG_MOD_RE.finditer(text):
        num = (m.group("num") or "").replace(",", "")
        if not num:
            continue
        pre = (m.group("pre") or "").strip().lower()
        sym = (m.group("sym") or "").strip()
        tail = (text or "")[m.end():m.end() + 30]
        # the lookback starts at the NUMBER, not at the match — `pre` may already have eaten the word
        back = (text or "")[max(0, m.start("num") - 20):m.start("num")]
        kind = "exact"
        if m.group("plus") or sym in (">", "\u2265") or _TRAIL_LOWER_RE.match(tail):
            kind = "lower"
        elif sym in ("<", "\u2264") or _TRAIL_UPPER_RE.match(tail):
            kind = "upper"
        elif sym in ("~", "\u2248"):
            kind = "approx"
        elif _RANGE_TAIL_RE.match(tail):
            kind = "exact"                          # the first half of a range, not a bound
        elif pre:                                   # adjacent by construction
            kind = ("approx" if _APPROX_WORDS.search(pre)
                    else "upper" if _UPPER_BOUND_WORDS.search(pre) else "lower")
        elif not re.search(r"\d", back) and not _TEMPORAL_SPAN_RE.search(back):
            # a short lookback with NO other number in it — "a minimum DR of 50" binds to 50
            if _APPROX_WORDS.search(back):
                kind = "approx"
            elif _UPPER_BOUND_WORDS.search(back):
                kind = "upper"
            elif _LOWER_BOUND_WORDS.search(back):
                kind = "lower"
        if kind != "exact" and _TEMPORAL_SPAN_RE.search(back):
            kind = "exact"                          # "over the past 10 years" is a span, not a floor
        out.setdefault(num, collections.Counter())[kind] += 1
    return out


def detect_figure_modifier_lost(original, rewritten, cap=8):
    """A figure that was a BOUND in the input and is a bare value in the output.

    "keyword backlinks averaging DR 70+" became "an average placement of DR 70" — the value survived,
    which is all any gate looked at, and the claim changed from a floor to an average. The check is
    on MEANING, not characters: "25,000+" may become "over 25,000" or "at least 25,000" freely."""
    hits = []
    a, b = _figure_bounds(_prose(original)), _figure_bounds(_prose(rewritten))
    for num, kinds in a.items():
        was_bounded = sum(v for k, v in kinds.items() if k != "exact")
        if not was_bounded:
            continue                       # it was never a bound
        after = b.get(num) or collections.Counter()
        now_bounded = sum(v for k, v in after.items() if k != "exact")
        if now_bounded >= was_bounded:
            continue
        # Only when the FIGURE itself survived: a rewrite that merges two sentences and drops a
        # repetition has not changed the claim, it has tightened the prose.
        if sum(after.values()) < sum(kinds.values()):
            continue
        lost = ", ".join(sorted(k for k in kinds if k != "exact"))
        hits.append({"check": "figure-modifier-lost", "severity": "blocking",
                     "detail": f'"{num}" was a {lost} bound {was_bounded}× and is now one '
                               f'{now_bounded}× — the value survived, the bound did not'})
        if len(hits) >= cap:
            return hits
    return hits


# A bold lead-in. `**Quick answer:**` is a LABEL — a thing a reader scans, which the guard restores
# verbatim. `**California's environmental permit requirements**` with no colon is the SENTENCE'S
# SUBJECT, and restoring it in front of an independently reworded clause leaves it without a verb.
_BOLD_LEAD_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])?\s*\*\*([^*\n]{4,90}?)\*\*(\s*[:—–-])?\s*(.*)$")


def _bold_subjects(text):
    """{label text: continuation} for every bold lead-in that is NOT a colon-label."""
    out = {}
    for ln in _prose(text).split("\n"):
        m = _BOLD_LEAD_RE.match(ln)
        if not m or m.group(2):
            continue                       # no lead-in, or a proper label — both fine
        lab, rest = m.group(1).strip(), (m.group(3) or "").strip()
        # A bold phrase that closes with its own punctuation is a LABEL ("**Multi-city search
        # footprints.**"), not the subject of the sentence that follows it.
        if lab.endswith((":", ".", "?", "!")) or not rest:
            continue
        out[lab] = rest
    return out


def detect_bold_lead_broken(original, rewritten, cap=8):
    """A bold SUBJECT whose continuation no longer continues it.

    The input reads "**California's environmental permit requirements** affect how construction
    companies describe their services online" — a subject and its verb. The output reads
    "**California's environmental permit requirements** Construction companies must address CEQA…",
    which is a dangling phrase followed by an unrelated sentence. The tell is that the continuation
    now starts a new independent clause: a capitalised word the input's own continuation did not
    start with."""
    hits = []
    a, b = _bold_subjects(original), _bold_subjects(rewritten)
    for lab, rest in b.items():
        was = a.get(lab)
        if was is None:
            continue
        first_b = (rest.split() or [""])[0].strip("*_`")
        first_a = (was.split() or [""])[0].strip("*_`")
        # The input CONTINUED the phrase — a verb, a preposition, or a comma opening a clause — and
        # the output starts a new sentence instead. A leading comma is a continuation too: the
        # reported article reads "**Reddit brand mention campaigns**, Reddit threads are heavily
        # indexed…" and came back as "**Reddit brand mention campaigns** Given that…".
        cont_a = bool(first_a) and (first_a[:1].islower() or first_a[:1] in ",;:—–-")
        if cont_a and first_b and first_b[:1].isupper():
            hits.append({"check": "bold-lead-broken", "severity": "budgeted",
                         "detail": f'"**{lab[:48]}**" continued "{first_a}…" and now starts a new '
                                   f'sentence "{first_b}…"'})
            if len(hits) >= cap:
                return hits
    return hits


_MD_LINK_RE = re.compile(r"\[([^\]\n]{2,120})\]\((https?://[^)\s]+)\)")


def detect_link_lost(original, rewritten, cap=8):
    """A markdown link the input carried and the output does not.

    The URL gate that would catch this exists but is switched off for the blog surface, the rewrite
    prompt never mentions links, and the guard strips link targets before it harvests terms. The
    reported case dropped the publisher's own case-studies link out of the one sentence that pointed
    at its evidence."""
    hits = []
    a = {u: t for t, u in _MD_LINK_RE.findall(_prose(original))}
    b_urls = {u for _t, u in _MD_LINK_RE.findall(_prose(rewritten))}
    rw = _prose(rewritten)
    for url, text in a.items():
        if url in b_urls or url in rw:
            continue
        flat = text.strip() and text.strip().lower() in rw.lower()
        hits.append({"check": "link-flattened" if flat else "link-lost",
                     "severity": "budgeted" if flat else "blocking",
                     "detail": (f'"{text[:46]}" → {url[:60]} '
                                + ("survives as plain text" if flat else "is gone"))})
        if len(hits) >= cap:
            return hits
    return hits


# Wording that raises the temperature of a claim. Shape-based and vertical-neutral: it says nothing
# about what an article MAY claim, only that a rewording may not claim it harder.
_STRENGTH_RE = re.compile(
    r"\b(?:ensур\w*|ensure[sd]?|ensuring|guarantee[sd]?|guaranteeing|"
    r"will (?:be|get|have|result|appear|rank|receive)|proven to|shown to|"
    r"resulting in|results in|makes? it (?:suitable|ideal|perfect)|"
    r"always|every time|invariably|certainly|undoubtedly|definitely)\b", re.I)
_HEDGE_RE = re.compile(
    r"\b(?:can be|could be|may be|might be|tends? to|is designed to|are designed to|"
    r"aims? to|helps? to|can help|may help|is intended to|typically|generally|often|"
    r"can be relevant|where applicable|in many cases)\b", re.I)


# "This is not always the case" is a HEDGE. Counting "always" without looking left reads it as an
# absolute and reverts a block for softening a claim, which is the opposite of the point.
_NEGATED_RE = re.compile(r"\b(?:not|never|n't|rarely|hardly|seldom|isn't|aren't|won't|doesn't|don't)"
                         r"\W+(?:\w+\W+){0,2}$", re.I)


def strength_terms(text):
    """The strength wording in `text`, with negated absolutes left out."""
    out = set()
    for m in _STRENGTH_RE.finditer(text or ""):
        if _NEGATED_RE.search((text or "")[max(0, m.start() - 30):m.start()]):
            continue
        out.add(m.group(0).lower())
    return out


def _strength_count(text):
    n = 0
    for m in _STRENGTH_RE.finditer(text or ""):
        if not _NEGATED_RE.search((text or "")[max(0, m.start() - 30):m.start()]):
            n += 1
    return n


def detect_claim_strengthened(original, rewritten, cap=8):
    """The rewording claims harder than the input did.

    "this is the layer that determines whether your brand appears" became "This ensures that your
    brand is mentioned when a property developer asks an AI assistant" — a capability turned into a
    guarantee nobody can make. Counted, not matched sentence to sentence, because a full rewording
    moves every sentence; an INCREASE in strength wording is the signal."""
    a, b = _strength_count(_prose(original)), _strength_count(_prose(rewritten))
    if b <= a:
        return []
    added = sorted(strength_terms(_prose(rewritten)) - strength_terms(_prose(original)))
    return [{"check": "claim-strengthened", "severity": "blocking",
             "detail": f"strength wording went {a} → {b}"
                       + (f' (new: {", ".join(added[:4])})' if added else "")}][:cap]


def detect_hedge_lost(original, rewritten, cap=8):
    """A hedge the input carried that the output does not. "can be relevant for larger multi-practice
    firms" became "make it suitable for large, multi-practice law firms" — the same sentence, one
    grade more certain."""
    fa = [m.group(0).lower() for m in _HEDGE_RE.finditer(_prose(original))]
    fb = [m.group(0).lower() for m in _HEDGE_RE.finditer(_prose(rewritten))]
    if len(fb) >= len(fa):
        return []
    gone = sorted(set(fa) - set(fb))
    return [{"check": "hedge-lost", "severity": "budgeted",
             "detail": f"hedged wording went {len(fa)} → {len(fb)}"
                       + (f' (gone: {", ".join(gone[:4])})' if gone else "")}][:cap]


# "Generative Engine Optimization (GEO)" / "GEO (Generative Engine Optimization)".
_ACRONYM_DEF_RE = re.compile(r"([A-Za-z][A-Za-z0-9'’\-]*(?:\s+[A-Za-z][A-Za-z0-9'’\-]*){1,5})\s*\(([A-Z]{2,6})\)")


def _acronym_expansions(text):
    out = {}
    for phrase, acro in _ACRONYM_DEF_RE.findall(_prose(text)):
        words = phrase.split()
        # the expansion is the trailing words whose initials spell the acronym
        for n in range(len(acro), min(len(words), len(acro) + 2) + 1):
            cand = words[-n:]
            if "".join(w[0] for w in cand if w).upper()[:len(acro)] == acro:
                out.setdefault(acro, set()).add(" ".join(cand).lower())
                break
    return out


def _same_expansion(x, y):
    """Two spellings of the same expansion — case and punctuation aside."""
    n = lambda t: re.sub(r"[^a-z0-9 ]+", "", (t or "").lower()).strip()
    return n(x) == n(y)


def detect_acronym_expansion_changed(original, rewritten, cap=6):
    """An acronym that means something different after the rewording.

    "Generative Engine Optimization (GEO)" came back as "global engagement optimization (GEO)" — in
    an article whose publisher SELLS GEO. The guard could not see it: it treats an acronym and its
    spelled-out form as the same term on purpose, so "GEO" being present satisfied it while the words
    around it were rewritten freely. Two rules, both anchored to the INPUT: an expansion the input
    gave may not change, and an acronym the input never expanded may not gain one."""
    hits = []
    a, b = _acronym_expansions(original), _acronym_expansions(rewritten)
    for acro, forms in b.items():
        was = a.get(acro) or set()
        # EVERY expansion is judged on its own. The reported article kept the correct expansion in
        # four places and added a wrong one in a fifth, so any test that asks "does at least one
        # expansion still match" reports nothing — which is what a set intersection does.
        for f in sorted(forms):
            if any(_same_expansion(f, w) for w in was):
                continue
            if was:
                hits.append({"check": "acronym-expansion-changed", "severity": "blocking",
                             "detail": f'{acro} is spelled out as "{f[:50]}"; the input said '
                                       f'"{sorted(was)[0][:50]}"'})
            elif re.search(r"\b%s\b" % re.escape(acro), _prose(original)):
                # Blocking, at the operator's instruction. Sampled across 68 stored rewrites, 26 of
                # these are a model helpfully spelling out a standard abbreviation — BMI, FSA, HSA —
                # which is not a defect in itself. But there is no way to tell a right new expansion
                # from a wrong one without world knowledge, and the one that reached a client
                # ("global engagement optimization") was exactly this shape in an article whose
                # publisher sells the thing. An expansion the input did not authorise does not ship.
                hits.append({"check": "acronym-expansion-invented", "severity": "blocking",
                             "detail": f'{acro} was never spelled out and is now "{f[:56]}"'})
            if len(hits) >= cap:
                return hits
    return hits


def detect_citation_invented(original, rewritten, cap=6):
    """A [S#] the input never used. The `_valid` gate tests set CONTAINMENT — dropped markers only —
    so an added one passes, and the reported article moved a claim from [S2] to a [S15] that the
    input never cited, which re-attributed the claim to a different source."""
    a = {int(x) for x in _CITE_RE.findall(_prose(original))} if False else \
        {int(x) for x in re.findall(r"\[S(\d+)\]", _prose(original))}
    b = {int(x) for x in re.findall(r"\[S(\d+)\]", _prose(rewritten))}
    new_ = sorted(b - a)
    if not new_:
        return []
    return [{"check": "citation-invented", "severity": "blocking",
             "detail": f"the rewrite cites {', '.join('[S%d]' % n for n in new_[:6])}, "
                       f"which the input never cited"}][:cap]


def rewrite_findings(original, rewritten):
    """Everything the rewording changed about what the article asserts. Deterministic, no model, no
    network. Each finding carries a severity — see the module note above."""
    if not (original or "").strip() or not (rewritten or "").strip():
        return []
    out = []
    for fn in (detect_figure_modifier_lost, detect_bold_lead_broken, detect_link_lost,
               detect_claim_strengthened, detect_hedge_lost, detect_acronym_expansion_changed,
               detect_citation_invented):
        try:
            out += fn(original, rewritten)
        except Exception as e:                       # a detector must never take the rewrite down
            out.append({"check": "detector-error", "severity": "budgeted",
                        "detail": f"{fn.__name__}: {e}"})
    return out


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
    items += body_damage(body)             # FU251 — damage our own removal passes leave behind
    items += editorial_findings(body)      # FU253 — what the article says, judged separately
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
