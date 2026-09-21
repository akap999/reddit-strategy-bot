"""FU221 — the rewrite guard: the rewording step may change WORDS, never structure, sources or terms.

Why this exists. The watermark-free rewrite (a second model rewords Claude's finished body) was shipping
its own defects, found in the PeterMD "TRT and a GLP-1" blog (#158) against the operator's corrected copy:
  - a comparison table lost its "Pricing (Tirzepatide)" column (our own empty-cell table rule re-ran on
    the reworded text, so the rewrite path must hand back the input's tables unchanged);
  - a literal "[S#]" placeholder was appended to an answer that had no citation;
  - section labels were swapped ("Quick answer:" became something else);
  - domain terms were swapped for near-synonyms that change the meaning ("lean mass" became "muscle
    mass", "narrative review" became "review article").
The existing gates only checked that every number and every ORIGINAL citation survived somewhere in the
body, and that the headings matched. Nothing compared tables, per-paragraph citations, labels or terms.

What it does. The input and the rewrite are split into the same blocks (headings, tables, list items,
paragraphs, code) and paired section by section. Then, for each pair:
  - a table, a code block, a heading, the byline lines and the Sources list come back exactly as they
    were in the input;
  - a changed bold label is put back (the reworded text after it is kept);
  - a paragraph or list item whose citations differ from the input's (added, dropped, piled together, or
    a placeholder like "[S#]"), whose figures differ, or which lost one of the article's key terms, is
    replaced by the input's version of that block.
A section whose block layout changed is kept only if the whole section still passes those checks and has
the same tables and list items; otherwise it comes back from the input whole. Everything else keeps the
rewrite, so the watermark strip is only given up where accuracy requires it.

Vertical-neutral: the key terms come from the article itself (the extraction call plus the article's own
acronyms, capitalised names and study-design phrases); nothing here names a vertical or a site.
"""

import difflib
import re

# FU252 — the meaning-aware figure/claim primitives live with the scoreboard detectors, so the guard
# and the offline replay can never disagree about what counts as a change. `blog_eval` imports only
# the standard library, so this adds no cycle.
from generators.blog_eval import (_acronym_expansions, _bold_subjects, _figure_bounds,
                                  _same_expansion, _MD_LINK_RE, strength_terms)

_CITE_RE = re.compile(r"\[S(\d+)\]")
_CITE_RUN_RE = re.compile(r"(?:\[S\d+\]\s*)+")
# A citation-shaped token that is not a real one: [S#], [S?], [Sx], [Sn], [S...], [S], [citation needed].
# Never a Markdown link ("[Sonilo](https://…)" is link text, not a placeholder).
_BAD_CITE_RE = re.compile(
    r"\[\s*S\s*(?:#\d*|\?+|[xn]|\.{1,3}|_+|)\s*\](?!\()"
    r"|\[\s*(?:citation needed|source|sources|cite|ref)\s*\](?!\()", re.I)
_LABEL_RE = re.compile(r"^(\s*(?:[-*+]|\d+[.)])?\s*)\*\*([^*\n]{1,90}?)\*\*")
# a figure — never a digit that belongs to a code ("GLP-1", "B2B", "T3", "HbA1c"; those are terms)
_NUM_RE = re.compile(r"(?<![A-Za-z])(?<![A-Za-z]-)\d[\d,]*(?:\.\d+)?")
_HEAD_RE = re.compile(r"#{1,6}\s")
_ITEM_RE = re.compile(r"(?:[-*+]|\d+[.)])\s")
_FENCE_RE = re.compile(r"(```|~~~)")
_SOURCES_HEAD_RE = re.compile(r"^\s*#{1,6}\s+Sources\s*$", re.I)
# a line that is ONE emphasis run — the byline / reviewer / disclosure lines prepended to every article
_EMPH_LINE_RE = re.compile(r"^\s*(?:\*[^*\s].*[^*\s]\*|_[^_\s].*[^_\s]_)\s*$")
# Study / evidence phrases whose first word carries the meaning ("narrative review" is not a "systematic
# review"; an "audited report" is not a "report"). The first word must come from this METHODOLOGY
# vocabulary, which is shared by every field that cites evidence (clinical, financial, legal, market
# research), so "this study", "scientific study programs" and "doctors will review" are never terms.
_DESIGN_ADJ = (
    "narrative|systematic|scoping|umbrella|randomi[sz]ed|controlled|placebo-controlled|double-blind|"
    "single-blind|open-label|observational|prospective|retrospective|longitudinal|cross-sectional|"
    "case-control|pilot|feasibility|clinical|preclinical|pivotal|peer-reviewed|independent|third-party|"
    "audited|unaudited|consolidated|interim|annual|quarterly|qualitative|quantitative|real-world|"
    "post-hoc|population-based|registry|mendelian|network"
)
_DESIGN_RE = re.compile(r"\b(" + _DESIGN_ADJ + r")\s+(review|reviews|study|studies|trial|trials|analysis|"
                        r"meta-analysis|survey|cohort|guideline|guidelines|audit|report|data)\b(?!-)", re.I)
_META_RE = re.compile(r"\bmeta-analys[ie]s\b", re.I)


# ordinary words written in capitals for emphasis ("low testosterone AND a high BMI") — not codes
_CAPS_WORDS = {"AND", "OR", "NOT", "NO", "YES", "ALL", "ANY", "ONLY", "MUST", "NEVER", "ALWAYS", "THE",
               "IF", "BUT", "IS", "ARE", "DO", "NOTE", "NOW", "NEW", "FREE", "BEFORE", "AFTER", "WITH",
               "WITHOUT", "EVERY", "NONE", "BOTH", "IMPORTANT", "WARNING", "TIP", "OK"}


# How many paragraphs may go back for a WORDING swap alone (no code, name or figure in it): one in ten.
# Measured on real rewrites: the must-fix reverts (a changed citation, figure, label or table) already
# cost most of the strip, and lifting this further buys little — so the rest are reported instead.
_BUDGET_DIV = 10


def _parse(lines):
    """Blocks over `lines` as (kind, start, end), end exclusive. kind: h heading, t table, i list item
    (with its continuation lines), c fenced code, p paragraph, x divider / punctuation-only line."""
    blocks, cur, fence = [], None, None

    def close(end):
        nonlocal cur
        if cur is not None:
            blocks.append((cur[0], cur[1], end))
            cur = None

    for n, ln in enumerate(lines):
        st = ln.strip()
        if fence is not None:
            if st.startswith(fence):
                blocks.append(("c", cur[1], n + 1))
                cur, fence = None, None
            continue
        m = _FENCE_RE.match(st)
        if m:
            close(n)
            cur, fence = ("c", n), m.group(1)
            continue
        if not st:
            close(n)
            continue
        if _HEAD_RE.match(st):
            close(n)
            blocks.append(("h", n, n + 1))
            continue
        if re.fullmatch(r"[-*_=~\s]+", st):
            # a divider or a leftover lone "-": layout, not content — kept from the input, never paired
            close(n)
            blocks.append(("x", n, n + 1))
            continue
        if st.startswith("|"):
            if cur is None or cur[0] != "t":
                close(n)
                cur = ("t", n)
            continue
        if _ITEM_RE.match(st):
            close(n)
            cur = ("i", n)
            continue
        if cur is not None and cur[0] in ("p", "i"):
            continue            # continuation line of the paragraph / item above
        close(n)
        cur = ("p", n)
    close(len(lines))
    return blocks


def _sections(lines, blocks):
    """Sections, each running from its heading line to the line before the next heading."""
    starts = [b[1] for b in blocks if b[0] == "h"]
    edges = [0] + starts + [len(lines)]
    heads = {b[1]: b for b in blocks if b[0] == "h"}
    secs = []
    for k in range(len(edges) - 1):
        a, z = edges[k], edges[k + 1]
        head = heads.get(a) if k > 0 else None
        body = [b for b in blocks if b[0] != "h" and a <= b[1] < z]
        secs.append({"head": head, "blocks": body, "start": a, "end": z})
    return secs


def _head_key(lines, sec):
    if not sec["head"]:
        return ""
    return re.sub(r"[^a-z0-9]+", " ", lines[sec["head"][1]].lower().lstrip("#")).strip()


def _text(lines, b):
    return "\n".join(lines[b[1]:b[2]])


def _norm_num(n):
    # compared on the bare value: "10–20%" and "10% to 20%" state the same two figures, and "$1,300" is
    # "$1300" (the writer's own fact gate already guards currency and units across the whole body)
    return n.replace(",", "").rstrip(".")


# shorthand that states no fact ("100% online", "1:1 coaching", "24/7 support") — rewording it away is
# fine, and the writer's own fact gate ignores it for the same reason (FU174)
_RHETORICAL_RE = re.compile(r"(?<![\d.])(?:100\s?%|1\s?:\s?1|24\s?/\s?7|24\s?[x×]\s?7)(?![\d])")


def _nums(s):
    return {_norm_num(n) for n in _NUM_RE.findall(_RHETORICAL_RE.sub(" ", _CITE_RE.sub(" ", s)))}


def _longest_cite_run(s):
    return max((len(_CITE_RE.findall(m.group(0))) for m in _CITE_RUN_RE.finditer(s)), default=0)


def _case_sensitive(term):
    # a word with two or more capitals (TRT, GLP-1, HbA1c, PeterMD, US) must match exactly, or "US" would
    # match the pronoun "us"; Title Case names and lowercase phrases match case-insensitively.
    return any(sum(1 for c in w if c.isupper()) >= 2 for w in term.split())


def _phrase(term, text):
    """Is `term` in `text` as a phrase? Spaces and hyphens are interchangeable ("fixed-rate" finds
    "fixed rate") and the last word may be plural or possessive, but nothing may sit between the words:
    "lean muscle mass" and "lean body mass" do NOT carry "lean mass" — that insertion is exactly the
    loss of precision the guard exists to catch."""
    words = [w for w in re.split(r"[\s-]+", term.strip()) if w]
    if not words:
        return False
    flags = 0 if _case_sensitive(term) else re.I
    pat = r"[\s-]+".join(re.escape(w) for w in words)
    return re.search(r"(?<![A-Za-z0-9])" + pat + r"(?:s|es|'s)?(?![A-Za-z0-9])", text, flags) is not None


def _acronym_defs(text):
    """{ACRONYM: [spelled-out forms]} from the article's own definitions — "erectile dysfunction (ED)"
    and "TRT (testosterone replacement therapy)" — so a rewrite that spells an abbreviation out, or
    abbreviates a spelled-out term, is not a lost term."""
    defs = {}

    def _add(acr, words):
        letters = acr.upper()
        k = len(letters)
        if 2 <= k <= 8 and len(words) >= k and \
                "".join(w[0] for w in words[-k:]).upper() == letters:
            defs.setdefault(acr, []).append(" ".join(words[-k:]))
        elif words and words[-1][:1].upper() == letters[:1] and len(words[-1]) > k:
            defs.setdefault(acr, []).append(words[-1])     # "gastrointestinal (GI)"

    for m in re.finditer(r"((?:[A-Za-z][A-Za-z'-]*\s+){0,7}[A-Za-z][A-Za-z'-]*)\s*\(\s*([A-Z]{2,8})s?\s*\)", text):
        _add(m.group(2), m.group(1).split())
    for m in re.finditer(r"\b([A-Z]{2,8})s?\s*\(\s*([A-Za-z][A-Za-z'-]*(?:\s+[A-Za-z][A-Za-z'-]*){0,7})\s*\)",
                         text):
        words = m.group(2).split()
        if "".join(w[0] for w in words).upper() == m.group(1):
            defs.setdefault(m.group(1), []).append(" ".join(words))
    return defs


def _has(term, text, defs=None, lenient=False):
    """Is `term` in `text`? An abbreviation and its spelled-out form count as the same term: "US",
    "U.S." and "United States"; "ED" and "erectile dysfunction"; "TX" and "Texas".

    STRICT (lenient=False) is used to decide what the INPUT says: the term itself, a dotted form, or a
    spelling the article defines. LENIENT (lenient=True) is used on the REWRITE and also accepts any
    spelling whose initials match — generous on purpose, and only on that side, so "body weight" can
    never make the input look like it mentioned a "Boxed Warning"."""
    if _phrase(term, text):
        return True
    t = term.strip()
    if re.fullmatch(r"[A-Z]{2,8}", t):
        dotted = r"\.?".join(t) + r"\.?"
        if re.search(r"(?<![A-Za-z0-9])" + dotted + r"(?![A-Za-z0-9])", text):
            return True
        if any(_phrase(x, text) for x in (defs or {}).get(t, [])):
            return True
        if not lenient:
            return False
        # a Title Case spelling whose initials are the abbreviation ("United States")
        if re.search(r"(?<![A-Za-z0-9])" + r"\s+".join(c + r"[a-z]+" for c in t) + r"(?![A-Za-z0-9])", text):
            return True
        # spelled out in ordinary words: "erectile dysfunction" for ED, "nurse practitioner" for NP
        # (content words only, so "each day" never passes for ED)
        if re.search(r"(?<![A-Za-z0-9])" + r"[\s-]+".join(f"[{c}{c.lower()}][a-z]{{3,}}" for c in t)
                     + r"(?![A-Za-z0-9])", text):
            return True
        # a two-letter code written as the name it stands for: "Texas" for TX, "Hawaii" for HI — a
        # capitalised word the text never writes in lowercase (so "Also" never passes for AL)
        if len(t) == 2:
            lower = {w.lower() for w in re.findall(r"\b[a-z][a-z'-]*\b", text)}
            for w in re.findall(r"(?<![A-Za-z])" + t[0] + r"[a-z]{3,}", text):
                if t[1].lower() in w[1:] and w.lower() not in lower:
                    return True
        return False
    words = t.split()
    if len(words) >= 2 and all(re.fullmatch(r"[A-Z][a-z]+", w) for w in words):
        return _has("".join(w[0] for w in words), text, None, False)   # "United States" ← "US"
    for acr, forms in (defs or {}).items():
        if any(f.lower() == t.lower() for f in forms):
            return _has(acr, text, None, False)                          # "erectile dysfunction" ← "ED"
    return False


def auto_key_terms(text):
    """Terms the article itself marks as precise: acronyms and codes (TRT, GLP-1, HbA1c, SURMOUNT-1,
    503B), capitalised names used mid-sentence (Zepbound, United States, Noom Med) and study-design
    phrases ("narrative review", "randomized trial"). Headings, tables, the Sources list, bold labels,
    links and code are ignored — title-case headings would otherwise turn every word into a "term"."""
    lines = (text or "").split("\n")
    keep = []
    for kind, a, z in _parse(lines):
        if kind in ("p", "i"):
            keep.append("\n".join(lines[a:z]))
        elif kind == "h" and _SOURCES_HEAD_RE.match(lines[a]):
            break
    t = "\n".join(keep)
    t = re.sub(r"\]\([^)]*\)", "]", t)
    t = re.sub(r"https?://\S+", " ", t)
    t = re.sub(r"\*\*[^*\n]*\*\*", " ", t)
    t = _CITE_RE.sub(" ", t)
    terms = set()
    for m in re.finditer(r"(?<![A-Za-z0-9])(?=[A-Za-z0-9-]*[A-Z])(?=[A-Za-z0-9-]*(?:\d|[A-Z]{2}))"
                         r"[A-Za-z][A-Za-z0-9-]*[A-Za-z0-9]", t):
        # keep only the code-like parts of a hyphenated word: "GLP-1-assisted" → "GLP-1",
        # "FDA-regulated" → "FDA" (the rewrite may say "assisted by GLP-1" — that is not a swap)
        segs = [x for x in m.group(0).split("-") if re.search(r"[A-Z0-9]", x)]
        tok = re.sub(r"(?<=[A-Z0-9])s$", "", "-".join(segs))   # "GLP-1s" → "GLP-1", "TRTs" → "TRT"
        if len(tok) >= 2 and re.search(r"[A-Za-z]", tok) and tok not in _CAPS_WORDS:
            terms.add(tok)
    lower_words = {w.lower() for w in re.findall(r"\b[a-z][a-z'-]*\b", t)}
    for sent in re.split(r"(?<=[.!?:;])\s+|\n+", t):
        found = list(re.finditer(r"[A-Za-z][A-Za-z'-]*", sent))
        run, prev_end = [], None
        for m in found[1:] + [None]:                     # never the sentence-initial word
            w = re.sub(r"'s$", "", m.group(0)) if m else ""
            # a name is consecutive capitalised words with only spaces between them — a comma, "&" or
            # bracket ends it, so "California, Texas" is two names, not "California Texas"
            joined = m is not None and prev_end is not None and sent[prev_end:m.start()].strip() == ""
            if w and re.fullmatch(r"[A-Z][a-z]+", w) and (not run or joined):
                run.append(w)
                prev_end = m.end()
                continue
            # a run of capitalised words is a name ("United States", "Noom Med", "Eli Lilly") when at
            # least one of its words is never written in lowercase in the article — so "Multiple" alone
            # (the article also says "multiple") is not a term, but "Multiple Endocrine Neoplasia" is
            if run and any(x.lower() not in lower_words for x in run) and \
                    (len(run) > 1 or len(run[0]) >= 4):
                terms.add(" ".join(run))
            run, prev_end = [], None
            if w and re.fullmatch(r"[A-Z][a-z]+", w):        # a new run starts at this word
                run, prev_end = [w], m.end()
    _sing = {"studies": "study", "trials": "trial", "reviews": "review", "guidelines": "guideline"}
    for m in _DESIGN_RE.finditer(t):
        noun = m.group(2).lower()
        terms.add(f"{m.group(1).lower()} {_sing.get(noun, noun)}")   # singular: "trial" also finds "trials"
    if _META_RE.search(t):
        terms.add("meta-analysis")
    return sorted(terms)


def _precise(term):
    """A term worth reverting a paragraph over: a code, a name, a figure-bearing token, or a short
    phrase. A long all-lowercase phrase ("user-reported pros and cons", "team collaboration tools") is
    category wording the rewrite is SUPPOSED to reword — locking it would give the rewrite nothing to
    do, which is how over-protection quietly cancels the whole pass."""
    t = term.strip()
    return bool(re.search(r"[A-Z0-9]", t)) or len(t.split()) <= 2


def _bound_problems(orig, new):
    """FU252 — a figure that was a BOUND in the input and is a bare value in the output.

    `_nums` compares VALUES, and no regex in this file or in blog_gen ever captured the "+", so
    "DR 70+" and "DR 70" were the same figure to every gate — a floor published as an average. The
    comparison is on MEANING, not characters: "25,000+" may become "over 25,000" or "at least
    25,000" freely, and may not become nothing."""
    a, b = _figure_bounds(orig), _figure_bounds(new)
    out = []
    for num, kinds in a.items():
        was = sum(v for k, v in kinds.items() if k != "exact")
        if not was:
            continue
        after = b.get(num) or {}
        now = sum(v for k, v in after.items() if k != "exact")
        # only when the figure itself survived — a merged repetition is tighter prose, not a changed
        # claim (the same rule the offline detector uses)
        if now < was and sum(after.values()) >= sum(kinds.values()):
            out.append(num)
    return out


# Set once per document by `_guard`: the expansions the INPUT authorised, for the whole article.
# Acronym consistency is a document property, not a block one — the input may define TRT in its first
# paragraph and the rewrite may legitimately repeat that expansion in its seventh. Judged per block,
# eight blocks of the FU221 fixture reverted for spelling out TRT, CBC and BMI exactly as the input
# had already spelled them out.
_DOC_ACRONYMS = {}


def _unit_problems(orig, new, terms, defs=None):
    """Why a reworded block (or section) cannot be kept; an empty list means it can.

    FU252 added the last four. Each is a class that reached a client: a figure that lost the modifier
    carrying its meaning, a claim reworded harder than the input made it, a link to the publisher's
    own evidence dropped, and an acronym given an expansion the input never authorised."""
    probs = []
    if _BAD_CITE_RE.search(new):
        probs.append("placeholder citation")
    if sorted(_CITE_RE.findall(orig)) != sorted(_CITE_RE.findall(new)):
        probs.append("citations changed")
    else:
        lo, ln = _longest_cite_run(orig), _longest_cite_run(new)
        if ln >= 3 and ln > lo:
            probs.append("citations piled together")
    if _nums(orig) != _nums(new):
        probs.append("figures changed")
    lost = [k for k in terms if _has(k, orig, defs) and not _has(k, new, defs, lenient=True)]
    if lost:
        probs.append("term lost: " + ", ".join(lost[:3]))
    _b = _bound_problems(orig, new)
    if _b:
        probs.append("figure lost its bound: " + ", ".join(_b[:3]))
    # A strength word the input block did not use. Deliberately NOT a count comparison: a full
    # rewording moves wording between sentences, and counting reverted three blocks of the FU221
    # fixture for ordinary paraphrase. The named words are the ones that change what is promised.
    _sa, _sn = strength_terms(orig), strength_terms(new)
    if _sn - _sa:
        probs.append("claim stated more strongly: " + ", ".join(sorted(_sn - _sa)[:3]))
    # A bold phrase with NO colon is the sentence's SUBJECT, not a label. The label rule restores it
    # verbatim, so if the rewrite replaced what followed with a new sentence the subject is left
    # without a verb — "**California's environmental permit requirements** Construction companies
    # must address…". Three of these shipped in one article.
    _ba, _bb = _bold_subjects(orig), _bold_subjects(new)
    for _lab, _rest in _bb.items():
        _was = _ba.get(_lab)
        if not _was:
            continue
        _fa = (_was.split() or [""])[0].strip("*_`")
        _fb = (_rest.split() or [""])[0].strip("*_`")
        if _fa and _fb and (_fa[:1].islower() or _fa[:1] in ",;:—–-") and _fb[:1].isupper():
            probs.append(f"bold lead-in left without its sentence: **{_lab[:40]}**")
            break
    _la = {u for _t, u in _MD_LINK_RE.findall(orig)}
    _lb = {u for _t, u in _MD_LINK_RE.findall(new)}
    if _la - _lb:
        probs.append("link dropped: " + ", ".join(sorted(_la - _lb)[:2]))
    _ea, _eb = _acronym_expansions(orig), _acronym_expansions(new)
    for acro, forms in _eb.items():
        # the block's own definition, plus every definition the INPUT gave anywhere in the article
        was = (_ea.get(acro) or set()) | set(_DOC_ACRONYMS.get(acro) or ())
        for f in sorted(forms):
            if not any(_same_expansion(f, w) for w in was):
                probs.append(f"{acro} spelled out as \"{f[:40]}\""
                             + (f', input said "{sorted(was)[0][:40]}"' if was else " (input never did)"))
                break
    return probs


def _labels(text):
    out = []
    for blk in re.split(r"\n\s*\n", text or ""):
        m = _LABEL_RE.match(blk)
        if m:
            out.append(m.group(2).strip())
    return out


def _set_doc_acronyms(original):
    """Record the expansions the INPUT authorised, for the whole article, before any block is judged."""
    global _DOC_ACRONYMS
    try:
        _DOC_ACRONYMS = _acronym_expansions(original or "")
    except Exception:
        _DOC_ACRONYMS = {}


def guard_rewrite(original, rewritten, key_terms=None, log=print, repairs=None):
    """Return (body, report). `body` is the rewrite with every block that changed its structure,
    sources, figures, label or key terms put back to the input's version, laid out with the input's
    paragraph breaks. Never raises; on any internal error the rewrite is returned unchanged with
    report["error"] set."""
    rep = {"tables": 0, "labels": 0, "blocks": 0, "units": 0, "sections": 0, "restructured": 0,
           "relaid": 0, "sources": 0, "fixed_lines": 0, "soft": 0, "soft_left": 0, "budget": 0,
           "repaired": 0, "retryable": [], "_repairs": None,
           "reasons": {}, "examples": [], "changed": False, "summary": ""}
    rep["_repairs"] = dict(repairs or {})
    try:
        _set_doc_acronyms(original or "")
        return _guard(original or "", rewritten or "", key_terms, log, rep)
    except Exception as e:                                   # a guard must never take the rewrite down
        rep["error"] = str(e)
        log(f"[rewrite-guard] skipped ({e})")
        return rewritten, rep


def _reason(rep, probs, where):
    for p in probs:
        key = p.split(":")[0]
        rep["reasons"][key] = rep["reasons"].get(key, 0) + 1
    if len(rep["examples"]) < 12:
        rep["examples"].append(f"{'; '.join(probs)}: {where.strip()[:80]}")


def _guard(original, rewritten, key_terms, log, rep):
    if not original.strip() or not rewritten.strip():
        return rewritten, rep
    ol, rl = original.split("\n"), rewritten.split("\n")
    ob, rb = _parse(ol), _parse(rl)
    so, sr = _sections(ol, ob), _sections(rl, rb)
    terms = sorted({t.strip() for t in list(key_terms or []) + auto_key_terms(original)
                    if t and len(t.strip()) >= 2 and _precise(t)}, key=lambda s: -len(s))
    defs = _acronym_defs(original)
    # how many paragraphs may go back for a WORDING swap alone: a tenth of the article
    rep["budget"] = max(2, sum(1 for b in ob if b[0] in ("p", "i")) // _BUDGET_DIV)
    ok_keys = [_head_key(ol, s) for s in so]
    rk_keys = [_head_key(rl, s) for s in sr]
    out = []

    def orig_section(s):
        out.extend(ol[s["start"]:s["end"]])

    ops = difflib.SequenceMatcher(None, ok_keys, rk_keys, autojunk=False).get_opcodes()
    for tag, i1, i2, j1, j2 in ops:
        if tag != "equal":
            # headings the rewrite added, dropped or renamed: the input's sections stand
            for s in so[i1:i2]:
                orig_section(s)
                if s["blocks"] or s["head"]:
                    rep["sections"] += 1
                    rep["examples"].append(
                        f"section '{ol[s['head'][1]].strip()[:60] if s['head'] else '(intro)'}' does not "
                        f"line up with the rewrite — restored")
            continue
        for k in range(i2 - i1):
            _pair(so[i1 + k], sr[j1 + k], ol, rl, terms, defs, out, rep, orig_section)

    body = "\n".join(out)
    rep["changed"] = body != rewritten
    rs = ", ".join(f"{v} {k}" for k, v in sorted(rep["reasons"].items()))
    bits = []
    if rep["tables"]:
        bits.append(f"{rep['tables']} table(s)")
    if rep["labels"]:
        bits.append(f"{rep['labels']} label(s)")
    if rep["blocks"]:
        bits.append(f"{rep['blocks']} of {rep['units']} paragraph(s)" + (f" ({rs})" if rs else ""))
    if rep["repaired"]:
        bits.append(f"(and had {rep['repaired']} reworded a second time instead)")
    if rep["sections"]:
        bits.append(f"{rep['sections']} whole section(s)")
    if rep["sources"]:
        bits.append("the Sources list")
    if rep["relaid"]:
        bits.append(f"the paragraph breaks in {rep['relaid']} section(s)")
    if rep["fixed_lines"]:
        bits.append(f"{rep['fixed_lines']} heading/byline line(s)")
    if rep["soft_left"]:
        bits.append(f"(and reworded a key term in {rep['soft_left']} more — left as the rewrite wrote "
                    f"them, review before publishing)")
    if bits:
        rep["summary"] = "rewrite guard put back " + ", ".join(bits) + " from the input"
        log("[rewrite-guard] " + rep["summary"])
        for ex in rep["examples"][:8]:
            log("[rewrite-guard]   " + ex)
    return body, rep


def _split_paragraph_lines(blocks):
    """Treat every line of a multi-line paragraph as its own paragraph. A rewrite that dropped the
    blank lines between paragraphs glues them into one (Markdown renders them as one paragraph too)."""
    out = []
    for b in blocks:
        if b[0] == "p" and b[2] - b[1] > 1:
            out.extend(("p", n, n + 1) for n in range(b[1], b[2]))
        else:
            out.append(b)
    return out


def _pair(s_o, s_r, ol, rl, terms, defs, out, rep, orig_section):
    """Merge one pair of matched sections into `out`."""
    head_line = ol[s_o["head"][1]] if s_o["head"] else None
    if head_line is not None and _SOURCES_HEAD_RE.match(head_line):
        if rl[s_r["start"]:s_r["end"]] != ol[s_o["start"]:s_o["end"]]:
            rep["sources"] += 1
        orig_section(s_o)
        return
    if s_r["head"] and head_line is not None and rl[s_r["head"][1]].strip() != head_line.strip():
        rep["fixed_lines"] += 1
    bo = [b for b in s_o["blocks"] if b[0] != "x"]
    br = [b for b in s_r["blocks"] if b[0] != "x"]
    ko = [b[0] for b in bo]
    if ko != [b[0] for b in br]:
        br2 = _split_paragraph_lines(br)
        if ko == [b[0] for b in br2]:
            br = br2
            rep["relaid"] += 1
    if ko == [b[0] for b in br]:
        chosen = []
        for b_o, b_r in zip(bo, br):
            chosen.append(_choose(b_o, b_r, ol, rl, terms, defs, rep))
        # emit with the INPUT's layout: its heading, its spacing, the chosen text of each block
        pos = s_o["start"]
        if head_line is not None:
            out.append(head_line)
            pos = s_o["head"][2]
        for b, lines in zip(bo, chosen):
            out.extend(ol[pos:b[1]])
            out.extend(lines)
            pos = b[2]
        out.extend(ol[pos:s_o["end"]])
        return
    # the rewrite re-laid the section out (merged or split paragraphs, a list turned into prose). Keep it
    # only if nothing that matters moved: same tables, list items and code, same labels, and the section
    # as a whole passes the citation / figure / term checks.
    bo_all = "\n\n".join(_text(ol, b) for b in bo if b[0] in ("p", "i"))
    br_all = "\n\n".join(_text(rl, b) for b in br if b[0] in ("p", "i"))
    probs = []
    for k, name in (("t", "tables"), ("i", "list items"), ("c", "code")):
        if sum(1 for b in bo if b[0] == k) != sum(1 for b in br if b[0] == k):
            probs.append(f"{name} changed")
    if _labels(bo_all) != _labels(br_all):
        probs.append("labels changed")
    probs += _unit_problems(bo_all, br_all, terms, defs)
    if probs:
        rep["sections"] += 1
        _reason(rep, probs, head_line or "(intro)")
        orig_section(s_o)
        return
    rep["restructured"] += 1
    repl = {}
    fixed_o = [b for b in bo if b[0] in ("t", "c")]
    fixed_r = [b for b in br if b[0] in ("t", "c")]
    for b_o, b_r in zip(fixed_o, fixed_r):
        if _text(ol, b_o) != _text(rl, b_r) and b_o[0] == "t":
            rep["tables"] += 1
        repl[b_r[1]] = (b_r[2], ol[b_o[1]:b_o[2]])
    i = s_r["start"]
    if s_r["head"]:
        out.append(head_line if head_line is not None else rl[i])
        i = s_r["head"][2]
    elif head_line is not None:
        out.append(head_line)
    while i < s_r["end"]:
        if i in repl:
            end, new_lines = repl[i]
            out.extend(new_lines)
            i = end
        else:
            out.append(rl[i])
            i += 1


def _choose(b_o, b_r, ol, rl, terms, defs, rep):
    """The lines to ship for one paired block: the rewrite's, the rewrite's with its label repaired, or
    the input's."""
    ot, nt = _text(ol, b_o), _text(rl, b_r)
    kind = b_o[0]
    if kind in ("t", "c"):
        if ot != nt:
            if kind == "t":
                rep["tables"] += 1
                rep["examples"].append("table restored: " + ot.split("\n")[0][:70])
            else:
                rep["fixed_lines"] += 1
        return ol[b_o[1]:b_o[2]]
    rep["units"] += 1
    if _EMPH_LINE_RE.match(ot) and "\n" not in ot:          # byline / reviewer / disclosure line
        if nt != ot:
            rep["fixed_lines"] += 1
        return ol[b_o[1]:b_o[2]]
    probs = []
    lo, ln = _LABEL_RE.match(ot), _LABEL_RE.match(nt)
    if lo:
        if not ln:
            probs.append("label lost")
        elif ln.group(2).strip() != lo.group(2).strip():
            nt = nt[:ln.start(2)] + lo.group(2) + nt[ln.end(2):]
            rep["labels"] += 1
    elif ln:
        # the labels are the INPUT's: the rewrite may not invent one either. This matters most on an
        # imported article that never used them — the rewrite must not quietly restyle it.
        probs.append("label added")
    probs += _unit_problems(ot, nt, terms, defs)
    if probs and all(p.startswith("term lost") and not re.search(r"[A-Z0-9]", p[11:]) for p in probs):
        # a swapped WORDING (no code, name or figure in it) — real, but the whole point of the pass is
        # to reword, so a few go back and the rest are reported instead of reverting half the article
        if rep["soft"] >= max(2, rep["budget"]):
            rep["soft_left"] += 1
            _reason(rep, [p + " (kept — over the wording budget)" for p in probs], ot)
            return nt.split("\n")
        rep["soft"] += 1
    if probs:
        # A REPAIR the rewrite model was asked to make after its first attempt broke a rule: use it when
        # it passes the same checks, so a paragraph is reworded twice rather than handed back unreworded.
        fixed = (rep["_repairs"] or {}).get(ot)
        if fixed:
            cand = fixed
            f_lo = _LABEL_RE.match(cand)
            if lo and f_lo and f_lo.group(2).strip() != lo.group(2).strip():
                cand = cand[:f_lo.start(2)] + lo.group(2) + cand[f_lo.end(2):]
            still = ([] if lo or not _LABEL_RE.match(cand) else ["label added"]) \
                + (["label lost"] if lo and not _LABEL_RE.match(cand) else []) \
                + _unit_problems(ot, cand, terms, defs)
            if not still:
                rep["repaired"] += 1
                return cand.split("\n")
            # Still broken after a full re-reword. Hand the model's OWN attempt back for a SURGICAL
            # edit rather than giving up on it — reverting means shipping the input's wording, which
            # is the one thing this whole pass exists to replace.
            rep["retryable"].append({"orig": ot, "rewrite": cand, "problems": list(still), "round": 2})
            _reason(rep, [p + " (retry failed too)" for p in still], ot)
        else:
            rep["retryable"].append({"orig": ot, "rewrite": nt, "problems": list(probs)})
            _reason(rep, probs, ot)
        rep["blocks"] += 1
        return ol[b_o[1]:b_o[2]]
    return nt.split("\n")


def restore_tables(original, body):
    """Put the input's tables back, in order, over the tables in `body`. The rewrite path re-runs table
    rules that must not reshape a finished article's tables (they dropped #158's pricing column). Returns
    (body, n_restored); `body` is unchanged when the two carry a different number of tables."""
    try:
        ol, bl = (original or "").split("\n"), (body or "").split("\n")
        ot = [b for b in _parse(ol) if b[0] == "t"]
        bt = [b for b in _parse(bl) if b[0] == "t"]
        if not ot or len(ot) != len(bt):
            return body, 0
        out, i, k, n = [], 0, 0, 0
        starts = {b[1]: (b[2], ot[j]) for j, b in enumerate(bt)}
        while i < len(bl):
            if i in starts:
                end, src = starts[i]
                new = ol[src[1]:src[2]]
                if new != bl[i:end]:
                    n += 1
                out.extend(new)
                i = end
            else:
                out.append(bl[i])
                i += 1
        return "\n".join(out), n
    except Exception:
        return body, 0
