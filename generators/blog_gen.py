"""GEO blog generator — first-party articles + LinkedIn adaptations from a seed.

PRIMARY GOAL (the only one that matters): get the article CITED / SOURCED by AI
answer engines (ChatGPT / Perplexity / Gemini / Google AI Overviews) for the seed
query, AND have it name the brand. The article structure is optimized for EXTRACTION
& CITATION (Quick answer, question-shaped headings, concise factual answers, FAQ),
not for conversion. SEO (title/meta/keywords) is the means to get indexed, not the goal.

Reuses the brand's stored enrichment and — for keyword sourcing — the same AI-Search
cluster fan-out we target on Reddit (see `suggest_keywords`). Unlike the Reddit
generators, blogs are FIRST-PARTY: they name and recommend the brand (owned media).
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor   # FU151 (B): parallelize independent network work
from difflib import SequenceMatcher as _SequenceMatcher   # FU167: longest-shared-run watermark metric

from generators.post_gen import PostGenerator
from generators.brand_enrichment import _fetch_homepage, _extract_visible_text

PROMPT_VERSION = "blog-v2-evidence"

# FU115 — discovery of the brand site's EXISTING live blog posts (sitemap → /blog fallback),
# used as internal-link candidates when the 🔗 checkbox is on.
_SITE_POST_PREFIXES = ("/blog/", "/insights/", "/articles/", "/news/", "/resources/",
                       "/post/", "/guides/")
_SITE_POST_EXCLUDE = ("/tag/", "/category/", "/author/", "/page/")
_site_posts_cache = {}    # domain -> (ts, [{url, label}])
_SITE_POSTS_TTL = 3600


def _norm_site_domain(domain_url):
    d = re.sub(r"^https?://", "", (domain_url or "").strip()).split("/")[0].lower()
    return d[4:] if d.startswith("www.") else d


def _looks_like_site_post(path):
    """FU115 — is this URL path plausibly a LIVE BLOG POST (not an index/tag/asset)?"""
    low = (path or "").lower()
    if any(x in low for x in _SITE_POST_EXCLUDE):
        return False
    if "?" in low or low.endswith((".jpg", ".jpeg", ".png", ".gif", ".webp", ".pdf",
                                   ".xml", ".css", ".js")):
        return False
    for pref in _SITE_POST_PREFIXES:
        if low.startswith(pref) and len(low.rstrip("/")) > len(pref):
            return True
    return False

# Common pages worth fetching beyond a brand's homepage, for real feature/pricing facts
# and on-site testimonials / case studies (curated, first-party — citable customer quotes).
_EVIDENCE_PATHS = ("", "/pricing", "/features", "/about",
                   "/testimonials", "/customers", "/case-studies", "/reviews")
_MAX_EVIDENCE_BRANDS = 3          # subject + up to 2 competitors
_EVIDENCE_TEXT_CAP = 2500         # chars of page text kept per source

# Reputable INDEPENDENT domains for the optional web-search tier (Follow-up 7). Passed as
# allowed_domains so discovered sources are third-party (these inherently exclude the
# brands' own sites). Review sites + mainstream tech press.
_THIRD_PARTY_DOMAINS = [
    "g2.com", "capterra.com", "getapp.com", "trustradius.com",
    "softwareadvice.com", "producthunt.com", "gartner.com", "forrester.com",
    "techcrunch.com", "theverge.com", "forbes.com", "businessinsider.com",
    "reuters.com", "crunchbase.com", "wikipedia.org",
    "usnews.com", "health.usnews.com",   # FU161: U.S. News — a reputable review source (preferred over affiliates)
]
# FU204 — this list was assembled for SaaS (G2 / Capterra / Gartner), where a listing carries editorial
# research. `trustpilot.com` rode along and, because membership here EXEMPTS a domain from
# `_is_affiliate_review`, a Trustpilot RATING page became "reputable" evidence — on a shipped
# baby-bottle blog two of them were the sole source for "borosilicate glass, heat and thermal
# shock-resistant". A star rating is aggregated customer sentiment: real, but never evidence for a
# material, a safety certification or an efficacy claim. Same for a marketplace listing, which is the
# seller's own marketing copy: fine for a PRICE or availability, not for a spec.
_RATING_AGGREGATORS = {
    "trustpilot.com", "sitejabber.com", "reviews.io", "reviewcentre.com", "resellerratings.com",
}
_RETAIL_LISTINGS = {
    "amazon.com", "amazon.co.uk", "walmart.com", "target.com", "ebay.com", "etsy.com",
    "babylist.com", "chewy.com", "wayfair.com", "bestbuy.com", "costco.com", "samsclub.com",
}


def _source_class(url):
    """FU204 — the class of a third-party page, so the writer and the checks can both SEE what a
    citation actually rests on. "" for an ordinary/reputable page."""
    d = _norm_domain(url or "")
    if not d:
        return ""
    for dom in _RATING_AGGREGATORS:
        if d == dom or d.endswith("." + dom):
            return "review"
    for dom in _RETAIL_LISTINGS:
        if d == dom or d.endswith("." + dom):
            return "retail"
    return ""
_MAX_WEB_SOURCES = 5             # FU56: cap independent third-party sources folded in (was 8 — cost)
_VERIFY_MAX_SEARCHES = 5         # FU56: cap on deep independent re-check web searches (was 8 — cost)
_VERIFY_MAX_BRANDS = 4           # FU56: cap on competitor tools sourced per article (was 6 — cost)
_MAX_TOOL_PAGES = 2              # FU52: cap on distinct per-tool source PAGES (deep-linked citations)
# FU179 — the watermark rewrite runs on the BLOG and on the two derived LinkedIn surfaces. Everything
# that matters (fact extraction, the fact + price-cadence gates, the attempt loop, the section pass,
# residual polish, semantic verification, the grade) is surface-agnostic; only the PRESERVE list and the
# structural half of `_valid` are shaped by the surface. `blog` MUST reproduce the pre-FU179 prompt and
# gates byte-for-byte — tests/test_fu179.py asserts it against a captured baseline.
#   label     — what to call the text in the prompt
#   band      — (lo, hi) multipliers on the original LENGTH. A LinkedIn post has a hard fold/length
#               contract, so the blog's 0.6-1.4 would wave through a gutted post.
#   preserve  — surface-specific PRESERVE bullets, replacing the blog's [S#]/tables/## Sources ones
#   plain     — plain text only (no Markdown headings may be introduced)
#   urls/tags — hard-gate that every URL / hashtag in the original survives
_WRITER_SURFACES = {
    "blog": {"label": "article", "band": (0.6, 1.4), "preserve": (), "plain": False,
             "urls": False, "tags": False},
    "linkedin_post": {
        "label": "LinkedIn post", "band": (0.85, 1.15), "plain": True, "urls": True, "tags": True,
        "preserve": (
            "- every URL exactly as written (the call-to-action link);",
            "- every #hashtag, unchanged and in the same order;",
            "- the OPENING SHAPE, which is what makes this post citable: LINE 1 stays a QUESTION, and "
            "LINE 2 still gives the direct answer with the same options NAMED. Reword them, never "
            "restructure them;",
            "- the AGAINST-INTEREST sentence (the one naming where an alternative wins) — reworded, "
            "never dropped;",
            "- PLAIN TEXT only: never introduce a Markdown heading, table, or horizontal rule, and keep "
            "the line and paragraph breaks where they are;")},
    "linkedin_article": {
        "label": "LinkedIn article", "band": (0.7, 1.3), "plain": False, "urls": True, "tags": True,
        "preserve": (
            "- every URL exactly as written (the call-to-action link);",
            "- every #hashtag, unchanged and in the same order;",
            "- never introduce a Markdown TABLE or a horizontal rule (---/***/___) — LinkedIn's editor "
            "renders them as literal characters;")},
}

_VERIFY_MAX_OPTIONS = 3          # FU189: generic OPTIONS are capped SEPARATELY from providers —
                                 # one costs ~1 reference search, a provider ~13, so an option must
                                 # never displace a provider from the _VERIFY_MAX_BRANDS budget.


def _opt_forms(s):
    """FU189 — every reading of an entity string that carries a parenthetical expansion:
    "TRT (Testosterone Replacement Therapy)" -> the whole string, "TRT", and the expansion. Matching
    on all three is what lets a short form and its spelled-out form recognise each other."""
    s = (s or "").strip()
    if not s:
        return []
    out = [s]
    m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", s)
    if m:
        out += [m.group(1).strip(), m.group(2).strip()]
    return [x for x in out if x]


def _named_as_option(tool, options):
    """FU189 — did the extraction name this comparison entity as a generic OPTION (a treatment,
    method, material, plan type, approach, technology, standard or product class) rather than a
    PROVIDER with its own website? Exact or slug match across both parenthetical readings."""
    forms = {f.lower() for f in _opt_forms(tool)}
    slugs = {_kf_slug(f) for f in _opt_forms(tool) if _kf_slug(f)}
    for o in (options or []):
        for of in _opt_forms(o):
            if of.lower() in forms or (_kf_slug(of) and _kf_slug(of) in slugs):
                return True
    return False


def _matches_a_product(tool, products):
    """FU189 BACKSTOP — the entity is one of the article's own PRODUCTS ("the things an official
    label, standard or specification document would exist FOR"), which is this file's existing and
    already vertical-neutral name for exactly this kind of thing. Token-subset either way, so
    "TRT (Testosterone Replacement Therapy)" recognises the product "testosterone".

    Used ONLY together with "no domain resolved" at the call site: on its own a token overlap could
    misroute a real provider whose name shares a token with a product, and the extra condition costs
    nothing because Pass 0 has already run. NOTE the inverse is deliberately NOT a classifier — a
    small REAL vendor the model has never heard of also fails to resolve, so kind is a question about
    the ENTITY, never about our lookup luck."""
    for p in (products or []):
        pt = set(_product_tokens(p))
        if not pt:
            continue
        for f in _opt_forms(tool):
            ft = set(_product_tokens(f))
            if ft and (ft <= pt or pt <= ft):
                return True
    return False


_FACT_RESCUE_TRIES = 3
_FACT_VERIFY_FETCHES = int(os.environ.get("BRAND_FACT_VERIFY_FETCHES", "6"))   # FU178: cap the
                                # operator-URL reads used to verify canonical brand facts (tier 2)
# FU200 — a comparison table has to be readable. Without a cap the writer turned the article's own
# selection criteria into columns and added more, shipping an 11-column matrix whose rows ran to ~1,400
# characters. Excludes the first (name) column.
_VF_LIST_RE = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)")
_VF_RULE_RE = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
_VF_SOURCES_RE = re.compile(r"(?im)^[ \t]*#{2,3}[ \t]+Sources\b")
_VF_TABLE_RE = re.compile(r"^[ \t]*\|")
_VF_HEAD_RE = re.compile(r"^[ \t]*#{1,6}[ \t]+\S")   # a REAL heading — the space rules out a #hashtag line
# FU204 — a line that is a COMPLETE emphasis run (`*…*`), not a list item. FU152 prepends
# `*[Add author byline before publishing]*` to EVERY article and FU84 adds `*Reviewed by …*` /
# `*Disclosure: …*`, so the FU202 marker-space rule was rewriting the first line of every blog into
# a bullet (`- [Add author byline before publishing]*`). `[` is not in that rule's excluded set, so
# the OPENING asterisk of an italic line read as a bullet missing its space.
_VF_EMPH_LINE_RE = re.compile(r"^\s*\*(?!\*).*\*\s*$")
# FU202 — a sentence that PROMISES a page. With no link in the same sentence it is a dead end for the
# reader and a dangling reference for an answer engine.
_VF_LINKPROMISE_RE = re.compile(
    r"(?i)\b(?:click here|read more|learn more|linked below|linked above|more here|see below for|"
    r"(?:see|read|check out|explore)\s+(?:our|the|my|this)\s+"
    r"(?:guide|article|post|page|resource|write-?up|breakdown|comparison))\b")
# NBSP + the exotic spaces `_strip_invisible_chars` deliberately leaves alone (it removes ZERO-WIDTH
# characters; these are visible-width spaces). They survive into the Google Doc and break wrapping.
_VF_NBSP_RE = re.compile("[\u00a0\u1680\u2000-\u200a\u202f\u205f\u3000]")
# FU203 — a YouTube chapter timestamp: MM:SS, M:SS or H:MM:SS. Nothing validated these
# before, so a model timing a 90-second script out to 5:00 shipped broken chapters.
_YT_TS_RE = re.compile(r"^(?:(\d{1,2}):)?(\d{1,3}):(\d{2})$")

def scrub_markdown_formatting(body):
    """Deterministic FORMATTING repair of the FINISHED body. Every rule below was confirmed against
        python-markdown before being included — a defect that renders correctly is NOT 'fixed', and is
        applied (if at all) only as a labelled cosmetic NORMALISATION.

        RENDER DEFECTS — the markdown is wrong on the page without these:
          1. a list directly under a paragraph collapses into one <p> with literal '- item'
             (`_normalize_md_lists` fixed this at EXPORT time only; the STORED markdown kept the defect,
             so the .md export / gdoc upload / CMS paste all shipped it). Fixed at the source now.
          2. '---' directly under a paragraph is parsed as a setext H2 — it EATS the paragraph into a
             heading and the divider disappears.
          3. an odd number of '**' on a line renders the asterisks literally.
          6. a TABLE directly under a paragraph is swallowed into that paragraph (verified: the whole
             table renders as literal '| A | B |' text), and a block directly AFTER the last row is
             eaten as another table ROW (a following '## Heading' becomes a <td> — the FU186 defect).
          7. a bullet / number with NO space after the marker ('-item', '1.item') renders as a plain
             paragraph, not a list.
          8. a nested list indented 1-3 spaces renders as a SIBLING, not a child; python-markdown needs
             a multiple of 4.

        COSMETIC NORMALISATION — render-identical, applied so the stored markdown is clean for the .md
        export, the Google Doc and a CMS paste:
          4. whitespace-only lines, and runs of 3+ blank lines.
          5. mixed bullet markers normalised to the dominant one.
          9. a blank line before a heading / blockquote / code fence that follows a paragraph.
         10. trailing whitespace: 1 space stripped, 2+ normalised to exactly 2 (markdown's hard line
             break, which must SURVIVE), and exactly one newline at the end of the body.
         11. non-breaking and other exotic spaces → a normal space (`_strip_invisible_chars` removes
             zero-width characters but leaves these, and they break line wrapping in Google Docs);
             runs of 3+ mid-line spaces collapsed; a space before , . ; : ! ? removed.

        NOT touched, deliberately: a heading directly under a paragraph RENDERS correctly (so 9 is
        cosmetic, never sold as a fix); '##Heading' with no space also renders correctly, and "fixing"
        it would turn a '#hashtag' line into an H1 — the exact FU197 defect — so it is never touched.
        SKIPPED entirely: fenced code blocks, and the `## Sources` section (its ' — <url>' separators are
        deliberate em-dashes written by `_rebuild_sources`, and a source title containing '*' would
        false-positive rule 3).

        Returns (body, fixes) — fixes is a list of {kind, detail}. Idempotent."""

    text = body or ""
    if not text.strip():
        return text, []
    lines, out, fixes = text.split("\n"), [], []
    in_fence = in_sources = False
    # bullet marker census (outside fences) so we normalise TO the dominant style
    marks = {}
    _f = _s = False
    for ln in lines:
        st = ln.lstrip()
        if _VF_SOURCES_RE.match(ln):
            _s = True
        if _s:
            continue
        if st.startswith("```") or st.startswith("~~~"):
            _f = not _f
            continue
        if _f:
            continue
        m = re.match(r"^\s*([-*+])\s+", ln)
        if m:
            marks[m.group(1)] = marks.get(m.group(1), 0) + 1
    dominant = max(marks, key=lambda k: (marks[k], k == "-")) if marks else "-"

    def _seen(kind, detail):
        if not any(f["kind"] == kind for f in fixes):
            fixes.append({"kind": kind, "detail": detail})

    for ln in lines:
        st = ln.lstrip()
        # Once the code-generated `## Sources` section starts, everything below is passthrough.
        if _VF_SOURCES_RE.match(ln):
            in_sources = True
        if in_sources:
            out.append(ln)
            continue
        if st.startswith("```") or st.startswith("~~~"):
            if not in_fence and out and out[-1].strip() and not _VF_TABLE_RE.match(out[-1]):
                out.append("")               # 9: blank line before an opening fence
                _seen("block-spacing", "blank line inserted before a code fence that followed a paragraph")
            in_fence = not in_fence
            out.append(ln)
            continue
        if in_fence:
            out.append(ln)
            continue
        if ln.strip() == "" and ln != "":
            ln = ""                                     # 4: whitespace-only line
        prev = out[-1] if out else ""
        if ln.strip():
            # 9: cosmetic — a paragraph glued directly under a heading (renders correctly either way)
            if prev.strip() and _VF_HEAD_RE.match(prev):
                out.append("")
                prev = ""
                _seen("block-spacing", "blank line inserted after a heading (cosmetic — it already "
                                       "rendered correctly)")
            # 7: a marker with no space after it is not a list at all
            _ns = re.match(r"^(\s*)([-*+])(?=[^\s*_-])", ln) or re.match(r"^(\s*)(\d+[.)])(?=\S)", ln)
            # FU204: an ITALIC line is not a bullet. A `*` marker whose line is a complete emphasis
            # run with an EVEN number of asterisks opened emphasis, not a list — skip the rule. That
            # protects the FU152 byline placeholder and the FU84 reviewer/disclosure lines, which are
            # prepended to every article, while a genuine `*item` (odd count, no closing `*`) is still
            # fixed. `-`/`+`/`1.` markers are untouched.
            _emph = (_ns and _ns.group(2) == "*" and _VF_EMPH_LINE_RE.match(ln)
                     and ln.count("*") % 2 == 0)
            if _ns and not _VF_RULE_RE.match(ln) and not _emph:
                ln = _ns.group(1) + _ns.group(2) + " " + ln[_ns.end():]
                _seen("marker-space", f"a missing space after the list marker '{_ns.group(2)}' was "
                                      "inserted (the line was rendering as a paragraph)")
            # 6: a table needs a blank line when the line above is prose
            if _VF_TABLE_RE.match(ln) and prev.strip() and not _VF_TABLE_RE.match(prev):
                out.append("")
                _seen("table-spacing", "blank line inserted before a table that followed a paragraph "
                                       "(the whole table was rendering as literal text)")
            # 6 (the FU186 shape): a block right after the last table row is eaten as another row
            elif not _VF_TABLE_RE.match(ln) and _VF_TABLE_RE.match(prev):
                out.append("")
                _seen("table-spacing", "blank line inserted after a table (the line below it was "
                                       "rendering as another table row)")
            # 1: a list needs a blank line when the line above is prose (not blank, not a list item)
            elif _VF_LIST_RE.match(ln) and prev.strip() and not _VF_LIST_RE.match(prev):
                out.append("")
                _seen("list-spacing", "blank line inserted before a list that followed a paragraph "
                                      "(it was rendering as one run-on paragraph)")
            # 2: a rule under prose becomes a setext H2 and swallows the paragraph
            elif _VF_RULE_RE.match(ln) and prev.strip() and not _VF_RULE_RE.match(prev):
                out.append("")
                _seen("thematic-break", "blank line inserted before '---' (it was turning the line "
                                        "above into a heading and losing the divider)")
            # 9: cosmetic — a heading or blockquote glued to the paragraph above it
            elif (re.match(r"^#{1,6}\s", st) or st.startswith(">")) and prev.strip() \
                    and not prev.lstrip().startswith(">"):
                out.append("")
                _seen("block-spacing", "blank line inserted before a heading or quote that followed a "
                                       "paragraph (cosmetic — it already rendered correctly)")
            # 8: a nested list item needs an indent that is a multiple of 4 to nest at all
            _ind = re.match(r"^( +)(?:[-*+]|\d+[.)])\s", ln)
            if _ind and _VF_LIST_RE.match(prev or ""):
                n = len(_ind.group(1))
                want = max(1, int(n / 4 + 0.5)) * 4
                if n != want:
                    ln = " " * want + ln[n:]
                    _seen("list-indent", f"a nested list item indented {n} space(s) was re-indented to "
                                         f"{want} (it was rendering as a sibling, not a child)")
            # 5: bullet marker consistency
            mm = re.match(r"^(\s*)([-*+])(\s+)", ln)
            if mm and mm.group(2) != dominant:
                ln = mm.group(1) + dominant + mm.group(3) + ln[mm.end():]
                _seen("bullet-marker", f"list marker '{mm.group(2)}' normalised to '{dominant}'")
            # 3: unclosed bold
            if ln.count("**") % 2:
                # FU204: an odd count is NOT always an unclosed run. A line carrying a balanced pair
                # PLUS a stray trailing marker ("Some **bold** text**") was given another one, making
                # "****" — four literal asterisks on the page. A trailing marker opened nothing, so
                # drop it; only a marker with content after it is genuinely unclosed.
                if ln.rstrip().endswith("**"):
                    ln = ln.rstrip()[:-2].rstrip()
                    _seen("unclosed-bold", "a stray trailing '**' that opened nothing was removed "
                                           "(closing it would have rendered '****' literally)")
                else:
                    ln = ln + "**"
                    _seen("unclosed-bold", "an unclosed '**' was closed (it was rendering literally)")
            # 11: exotic spaces / mid-line space runs / a space before punctuation
            _pre = ln
            ln = _VF_NBSP_RE.sub(" ", ln)
            if ln != _pre:
                _seen("exotic-space", "a non-breaking or other exotic space was converted to a normal "
                                      "space (it breaks line wrapping in the Google Doc)")
            if not _VF_TABLE_RE.match(ln):
                _lead = len(ln) - len(ln.lstrip(" "))
                _head, _body = ln[:_lead], ln[_lead:]
                _b2 = re.sub(r"(?<=\S)   +(?=\S)", " ", _body)
                _b2 = re.sub(r"(?<=\w) +([,.;:!?])(?= |$)", r"\1", _b2)
                if _b2 != _body:
                    ln = _head + _b2
                    _seen("spacing", "stray spacing inside a line was tidied (a run of spaces, or a "
                                     "space before punctuation)")
            # 10: trailing whitespace — 2+ is a hard line break and must survive as exactly 2
            _t = len(ln) - len(ln.rstrip(" \t"))
            if _t:
                _nl = ln.rstrip(" \t") + ("  " if _t >= 2 else "")
                if _nl != ln:
                    ln = _nl
                    _seen("trailing-space", "trailing whitespace normalised (a markdown hard line break "
                                            "is kept as exactly two spaces)")
        out.append(ln)

    # 4: collapse a run of 2+ blank lines to ONE separator. Applied to the text BEFORE the Sources
    # heading only, and via a regex so the head/tail boundary is never disturbed (an earlier
    # line-based version ate the blank line that precedes `## Sources`).
    joined = "\n".join(out)
    _cut = _VF_SOURCES_RE.search(joined)
    _h, _t = (joined[:_cut.start()], joined[_cut.start():]) if _cut else (joined, "")
    _h2, _n = re.subn(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", _h)
    if _n:
        fixes.append({"kind": "blank-lines",
                      "detail": f"collapsed {_n} run(s) of repeated blank lines"})
    res = _h2 + _t
    # 10: exactly one trailing newline
    _r2 = res.rstrip("\n") + "\n" if res.strip() else res
    if _r2 != res and not any(f["kind"] == "trailing-space" for f in fixes):
        fixes.append({"kind": "trailing-space", "detail": "the body now ends with a single newline"})
    return _r2, fixes


_DIM_CAP = int(os.environ.get("BLOG_MAX_DIMENSIONS", "5"))
# FU105's comparison floor, as a named constant: a comparison needs at least this many non-subject
# competitors to be a comparison at all. FU206 reads it to decide whether REMOVING a thin competitor
# is even offerable — shrinking the field below the floor is the self-crowning failure the floor
# exists to prevent, so the option is simply not offered there.
_MIN_COMPARISON_BRANDS = int(os.environ.get("BLOG_MIN_COMPETITORS", "3"))
_DIM_RESCUE_BUDGET = 6          # FU139: targeted (tool × dimension) rescue searches per generation —
_SUBJ_RESCUE_BUDGET = 4         # FU142: RESERVED rescue searches for the SUBJECT's own missing cells
                                # (its own row previously had no rescue path at all) — separate pool so
                                # publisher-gap filling never starves the FU139 tool rescues
                                # "try harder" before the FU138 resolver may drop a starved column          # FU78: broad-search retries to fetch a vendor's MISSING key facts (pricing,
                                # commercial-use/license) when its own (often JS-rendered) pages don't yield
                                # them, BEFORE dropping the cell/row
# FU78: signals that a given KEY comparison fact is actually PRESENT in the fetched text. A vendor page found
# WITHOUT one of these means the fact wasn't captured (a JS page, a thin snippet) → escalate with broad
# searches TARGETING that fact instead of shipping a one-try "see their site" punt. This generalizes beyond
# price to the columns that most often go "not confirmed": pricing AND commercial-use / license / royalty-free.
_PRICE_SIGNAL_RE = re.compile(
    r"(\$\s?\d|[€£]\s?\d|\b\d+(?:\.\d+)?\s?(?:usd|eur|gbp)\b|"
    r"\b\d+(?:\.\d+)?\s?(?:/|per\s+)mo(?:nth)?\b|\bper\s+month\b|\bfree\s+(?:tier|plan|version|forever)\b)",
    re.IGNORECASE)
# FU169: which numbers are genuinely LOAD-BEARING for the fact-integrity gate (_facts_preserved) — a
# decimal (dose/threshold: 2.5, 6.5), a currency amount (a price: $149, $1,000), or a number carrying a
# physical/clinical UNIT (15 mg, 100 mg, 500 units, 2.5 mg/mL). BARE integers and rhetorical percents
# (100%, top 100, 3 steps, the year 2026) are NOT hard-gated — they caused false-positive fallbacks (a
# reworded "100% → all/completely" dropped an incidental "100" and killed an otherwise-perfect rewrite
# → the rewrite NEVER shipped). The rewrite PROMPT + the human review-before-publish still cover those;
# the deterministic gate protects only the numbers whose silent alteration is a real clinical/commercial error.
# FU187 — a writer CAPITALISING AN ORDINARY WORD FOR EMPHASIS ("low testosterone AND a BMI over 30")
# is not stating a fact, but the acronym harvest below reads any >=3-letter ALLCAPS token as a code
# (FDA / MTC / MEN2 / BMI). "AND" then had to survive the rewrite CHARACTER-FOR-CHARACTER, so Qwen
# writing a natural lowercase "and" failed every attempt and shipped Claude's WATERMARKED body with
# `dropped facts ['AND']`. This is the same class as FU169 / FU174 / FU181: the atom layer false-failing
# a rewrite the regex floor would pass, with the worst possible failure mode.
#
# The list holds ONLY function words with no plausible acronym meaning in any vertical. Deliberately
# ABSENT and therefore still protected: MEN (Multiple Endocrine Neoplasia), ALL (acute lymphoblastic
# leukaemia), WHO (World Health Organization), ACT, AID, CARE, HOPE — real codes in some domain.
_EMPHASIS_CAPS = {
    "AND", "BUT", "NOT", "THE", "FOR", "YOU", "YOUR", "ARE", "WAS", "WERE", "HAS", "HAVE", "HAD",
    "CAN", "WILL", "WOULD", "SHOULD", "MUST", "ONLY", "EVERY", "NEVER", "ALWAYS", "BOTH", "EACH",
    "MORE", "LESS", "MOST", "MANY", "SOME", "ANY", "WHEN", "WHAT", "HOW", "WHY", "NOW", "ALSO",
    "JUST", "VERY", "SAME", "THAN", "THEN", "THIS", "THAT", "THESE", "THOSE", "WITH", "FROM",
    "INTO", "THEY", "THEIR", "ONE", "TWO", "YES", "DOES", "BEFORE", "AFTER", "UNTIL", "WHILE",
}


def _is_emphasis_caps(token, body=""):
    """True when an ALLCAPS token is an ordinary word shouted for emphasis rather than an acronym.
    Two conditions, so a genuine code can never be unprotected by accident: the word is on the curated
    function-word list AND the SAME word also appears in LOWERCASE in the author's own body. A real
    acronym ("FDA", "MEN") fails the first test; a domain code that happens to be on the list but is
    used only in caps fails the second."""
    t = (token or "").strip()
    if t.upper() not in _EMPHASIS_CAPS:
        return False
    if not body:
        return True
    return bool(re.search(r"\b%s\b" % re.escape(t.lower()), body))


_LOADBEARING_NUM_RE = re.compile(
    # FU181: `\d(?:[\d,]*\d)?` — a thousands separator must be FOLLOWED BY A DIGIT. With the older
    # greedy `\d[\d,]*` (and the decimal group optional) the match could END on a comma, so
    # "$10,000, and" matched `$10,000,` and the SENTENCE COMMA became part of the "fact". That token
    # was then required character-for-character, so a rewrite that correctly moved the comma failed
    # the gate and shipped Claude's watermarked body. This is the only alternative that can terminate
    # on the digit class — every other one below ends in a unit.
    r"(?:\$|€|£|USD|EUR|GBP)\s?\d(?:[\d,]*\d)?(?:\.\d+)?"                        # currency: $149, $1,000, €2.50
    r"|\d[\d,]*\.\d+\s?%?"                                                       # decimal:  2.5, 6.5, 99.9%, 3.9% (dose/threshold/APR/SLA)
    # NB (FU174): a BARE-INTEGER percent ("100%", "27%") is deliberately NOT load-bearing — it is usually
    # rhetorical ("100% online", "100% of patients") and Qwen validly rewords it, which the FU169 fix
    # established must never hard-fail a rewrite. Decimal percents above stay gated.
    # FU172 GENERALITY: units must not be medical-only — a "5 seats" / "99.9% uptime" / "14-day term" /
    # "3.9% APR" fact is exactly as load-bearing for a SaaS or lending brand as a dose is for a clinic.
    r"|\d[\d,]*\s?(?:mg|mcg|µg|ug|ng|mL|ml|kg|g|units?|iu|mmol|meq)\b"            # clinical: 15 mg, 500 units
    r"|\d[\d,]*\s?(?:seat|seats|user|users|licen[sc]e|licen[sc]es|member|members)\b"   # SaaS: 5 seats
    r"|\d[\d,]*\s?(?:GB|TB|MB|requests?|calls?|queries)\b"                        # quota: 100 GB, 10k requests
    r"|\d[\d,]*[- ]?(?:day|week|month|year|mo|yr)s?\b"                             # term: 14-day, 12 months
    r"|\d[\d,]*\s?(?:bps|APR|x|×)\b"                                              # finance/multiplier: 250 bps, 3x
    r"|\d[\d,]*\s?(?:mg|mcg|g|mL|ml)?/\s?(?:mL|ml|day|wk|week|mo|month|dose|kg|hr|hour|seat|user)\b",  # rate
    re.IGNORECASE)
# FU170: a SENTENCE we DELIBERATELY keep verbatim for YMYL safety — a contraindication, a boxed warning,
# a safety negation ("not a controlled substance", "not FDA-approved"), or a dosing-escalation directive.
# The rewrite prompt's SAFETY FALLBACK preserves these word-for-word (rewording risks flipping a negation's
# scope, which the fact-integrity gate can't catch), so — exactly like Markdown tables and the ## Sources
# list — they are low-entropy, intentionally identical, and carry ~no SynthID watermark. _prose_for_overlap
# drops them so a SAFE strip of the discretionary prose isn't graded "not-confirmed" by a residual the
# operator could only remove by risking a clinical fact (and regenerating never clears it).
# FU172 GENERALITY: the FU170 rule was MEDICAL-ONLY, so for a SaaS / lending / legal brand it matched
# NOTHING and the "keep this sentence whole" protection silently did not exist for them. Generalized to
# any sentence whose meaning is safety-, legal-, money- or eligibility-critical and whose scope cannot be
# preserved while rewording. Every original medical pattern is retained → YMYL behavior cannot regress.
_CRITICAL_DIRECTIVE_RE = re.compile(
    # clinical (FU170, unchanged)
    r"contraindicat|boxed warning|medullary thyroid|multiple endocrine neoplasia|\bMEN\s?2\b"
    r"|not a controlled substance|not FDA-approved|not approved as a|\bmust not\b"
    r"|\bmg\b.{0,70}?(?:increment|maintenance|escalat|titrat|starting dose|once weekly)"
    r"|(?:increment|maintenance|escalat|titrat|starting dose|once weekly).{0,70}?\bmg\b"
    # regulatory / legal obligation (any vertical)
    r"|\b(?:required by law|prohibited|not permitted|may not be|are not permitted|is unlawful)\b"
    r"|\b(?:must (?:be|not|comply|retain|disclose))\b"
    # financial terms
    r"|\b(?:APR|interest rate|late fee|penalty|early repayment|minimum term|non[- ]refundable)\b"
    # licence / contract restrictions
    r"|\b(?:commercial use|non[- ]transferable|royalty[- ]free|per[- ]seat licen[sc]e|terms of service)\b"
    # eligibility rules
    r"|\b(?:only if|not eligible|eligibility requires|do(?:es)? not qualify)\b",
    re.IGNORECASE)
_CLINICAL_DIRECTIVE_RE = _CRITICAL_DIRECTIVE_RE   # back-compat alias for existing call sites

# FU176: a PRICE's cadence is part of the fact. "$249/month billed quarterly" (≈$747 a quarter) and
# "$249 quarterly" differ ~3x, yet BOTH contain "$249" — so the presence-based fact gate passes them
# equally, and the LLM semantic verifier missed exactly this in a real shipped rewrite (in the Quick
# answer, the most-extracted position on the page). This deterministic check pairs every money token with
# the cadence terms attached to it and fails a DROPPED cadence. Vertical-neutral: an APR losing its term
# or a per-seat price becoming per-account is the same failure.
# Each pattern accepts the natural PARAPHRASES of its cadence, not just the original wording — the whole
# point of the rewrite is to reword freely, so "billed quarterly" → "charged every quarter" must PASS.
# Only an outright DROPPED cadence is a failure. A false positive here would needlessly revert a good
# sentence; a false negative would ship a wrong price — hence paraphrases in, synonyms folded together.
_CADENCE_PATTERNS = [
    (re.compile(r"per\s+month|/\s*month\b|/\s*mo\b|\bmonthly\b|(?:every|each|a)\s+month\b", re.I), "MONTH"),
    (re.compile(r"per\s+week|/\s*week\b|\bweekly\b|(?:every|each|a)\s+week\b", re.I), "WEEK"),
    (re.compile(r"per\s+quarter|\bquarterly\b|(?:every|each)\s+quarter\b"
                r"|(?:every|each)\s+(?:three|3)\s+months", re.I), "QUARTER"),
    (re.compile(r"per\s+year|/\s*year\b|\bannual(?:ly)?\b|\byearly\b|per\s+annum"
                r"|(?:every|each|a)\s+year\b|(?:every|each)\s+(?:twelve|12)\s+months", re.I), "YEAR"),
    (re.compile(r"first\s+month|initial\s+month|opening\s+month", re.I), "FIRST_MONTH"),
    (re.compile(r"one[-\s]time|single\s+payment|up[-\s]front", re.I), "ONE_TIME"),
    (re.compile(r"per\s+seat|/\s*seat\b|per\s+user|/\s*user\b|(?:each|a)\s+seat\b", re.I), "PER_SEAT"),
    # FU194 — these two patterns are the SAME CONCEPT: a stated multi-unit SPAN that the amount covers.
    # They were separate tags ("N_WEEK" vs "TERM"), so merely RESPELLING one as the other read as a lost
    # cadence. A live rewrite failed with `$897 lost its cadence (TERM → ['N_WEEK'])` on exactly that:
    # Claude's own body wrote it BOTH ways — "$897 per 12-week recurring subscription" (N_WEEK, x3) and
    # "$897 for 12 weeks recurring" (TERM, x1) — so the rewrite only had to phrase the odd one out like
    # the other three, and the watermarked body shipped. Nothing is lost by merging: neither tag ever
    # encoded the DURATION, so "for 12 weeks" and "for 12 months" were already indistinguishable here;
    # the digits are the number gate's job. Fifth instance of the class where a spelling difference
    # false-fails a rewrite the meaning gate would pass (cf. FU169 / FU174 / FU181 / FU187).
    (re.compile(r"\b\d+[-\s]week\b", re.I), "TERM"),
    # a contract TERM is part of the offer too — "$1,000 per month over 12 months" losing "over 12 months"
    # drops the total commitment (lending, SaaS annual plans, financed equipment).
    (re.compile(r"(?:over|for|across)\s+(?:\d+|twelve|six|three|two)\s+(?:month|year|week)s?\b"
                r"|\b\d+[-\s](?:month|year)\s+term\b", re.I), "TERM"),
]
# FU181: `\d(?:[\d,]*\d)?` — see _LOADBEARING_NUM_RE. The old `[\d,]+` could both START and END on a
# comma, so "$10,000, billed quarterly" produced the amount KEY "$10,000," while the rewrite's
# "$10,000" keyed differently — the cadence multiset then reported a cadence the rewrite had actually
# kept as LOST, failing _valid and shipping the watermarked body.
_MONEY_RE = re.compile(r"(?:\$|€|£)\s?\d(?:[\d,]*\d)?(?:\.\d+)?")

# FU172: function words carry no real choice in context (near-deterministic), so they are not evidence of a
# surviving watermark. Vertical-neutral.
_FUNCTION_WORDS = frozenset("""a an the and or of to in on for with as is are was were be been being that
this these those it its by at from not no can could may might must should would will do does did have has
had than then so such also more most other another each any all both which who whom whose their them they
he she you your our we us if when while about into over under between within per via but""".split())
# FU158: a comparison DIMENSION that is a pricing/cost column (vertical-neutral) — used to skip the
# subject's own-domain pricing re-search when an authoritative canonical price already exists.
_PRICE_DIM_RE = re.compile(r"pric|cost|\bfee\b|\bfees\b|\$|/mo|month|subscription|billing|plan\b",
                           re.IGNORECASE)
_LICENSE_SIGNAL_RE = re.compile(
    r"\b(commercial(?:ly|[- ]use)?|licen[sc]e[ds]?|royalty[- ]free|copyright|monetiz\w*|own\s+the\s+(?:output|rights))\b",
    re.IGNORECASE)
_FACT_SIGNALS = {"price": _PRICE_SIGNAL_RE, "license": _LICENSE_SIGNAL_RE}
# FU150 (Change 9): a comparison DIMENSION that is about license/commercial/terms — used to decide
# whether the `license` key-fact rescue is relevant for THIS article's vertical (creative/SaaS) vs
# a vertical with no such column (loans, HR software) where chasing it would just waste searches.
_LICENSE_DIM_RE = re.compile(
    r"licen[sc]e|commercial|royalty|copyright|usage\s+rights|monetiz|\bterms\b|ownership",
    re.IGNORECASE)

# FU56/FU78: hard per-generation cost ceiling ($). Once the running cost hits this, further web searches are
# skipped (search result tokens are ~90% of a blog's cost). Bumped 1.5→2.0 (FU78) to leave headroom for the
# price-rescue searches so public pricing gets fetched rather than punted. Env-overridable.
_COMPETITOR_FACTS_TTL_DAYS = float(os.environ.get("COMPETITOR_FACTS_TTL_DAYS", "45"))   # FU151 (A); FU160: 21→45d
_COMPETITOR_FACTS_CAP = 40   # FU151 (A): max competitors kept in the per-brand fact cache (prune oldest)
_BLOG_FETCH_WORKERS = int(os.environ.get("BLOG_FETCH_WORKERS", "5"))   # FU151 (B): parallel fetch pool
# FU173: a SEPARATE pool for self-hosted-writer calls. Unlike the fetch pool (many web hosts) these all hit
# ONE GPU container (deploy.py max_containers=1), so the cap is about that GPU's batch/KV-cache headroom
# for ~3k-token generations, not about politeness to a remote host.
_WRITER_WORKERS = int(os.environ.get("WRITER_WORKERS", "6"))
_BLOG_COST_CEILING = float(os.environ.get("BLOG_COST_CEILING", "3.0"))   # FU150: 2.0→3.0 — the $2 ceiling
# FU56: the LOW-priority independent-source sweep runs in _gather_evidence FIRST. Cap that stage to a
# FRACTION of the budget so it can't starve the higher-priority official/vendor searches that come later —
# i.e. plan + prioritize instead of a first-come cutoff. The remainder is reserved for verify_and_complete.
_CEIL_EVIDENCE = round(_BLOG_COST_CEILING * 0.3, 2)   # FU150: 0.4→0.3 — the subject is no longer
# web-swept in _gather_evidence, so this stage spends less; hand the reserve to competitor sourcing.

# FU54: stale SaaS-listing / aggregator domains whose pricing lags the vendor. Downranked BELOW the
# vendor's OWN site and reputable reviews for a price/license cell (a competitor sourced only from one of
# these read as stale in v8, e.g. SaaSworthy's Beatoven price).
_STALE_AGGREGATORS = {
    "saasworthy.com", "softwarefinder.com", "eesel.ai", "softwaresuggest.com",
    "goodfirms.co", "sourceforge.net", "slashdot.org", "toolify.ai", "futurepedia.io",
    # FU161: low-quality affiliate / SEO-review domains (vertical-neutral) — never cite a competitor
    # price from these; the vendor's own site or a reputable review (_THIRD_PARTY_DOMAINS) wins.
    "manytreatments.com", "choosingtherapy.com", "nutritionnc.com", "healthrx.com",
    "consumerrating.org", "weightrxguide.com", "bariatricreports.org", "plexusdx.com",
    "businessmodelcanvastemplate.com", "ai-health-apps.com", "xcode.life",
}

# FU133 — YMYL (Your Money / Your Life) verticals: pages whose value is CLINICAL / regulatory
# authority must cite OFFICIAL sources (regulator labels, professional-association guidelines),
# never rest on vendor/affiliate/review pages. Detection is a deterministic lexicon over the
# brand's own enrichment text; the operator can override via the generate-form checkbox.
_YMYL_LEXICON = {
    "medical": ("medical", "health", "telehealth", "clinic", "treatment", "therapy", "hormone",
                "trt", "testosterone", "glp-1", "glp1", "medication", "pharma", "prescription",
                "weight loss", "mental health", "rehab", "detox", "peptide", "semaglutide",
                "tirzepatide", "physician", "doctor", "patient", "wellness"),
    "finance": ("loan", "credit", "lending", "insurance", "invest", "mortgage", "banking",
                "fintech", "financial services", "tax "),
    "legal": ("law firm", "attorney", "legal services", "lawyer"),
}
_YMYL_OFFICIAL_DOMAINS = {
    "medical": ["fda.gov", "accessdata.fda.gov", "dailymed.nlm.nih.gov", "nih.gov", "cdc.gov"],
    "finance": ["sec.gov", "irs.gov", "consumerfinance.gov", "ftc.gov"],
    "legal": [],
}

# FU163: RECOGNIZED-AUTHORITY .org/.int bodies — professional societies + international organizations
# that legitimately earn the `official ·` badge on a non-.gov TLD. A GENERIC .org/.int (industry /
# education / advocacy site — e.g. telehealth.org) is NOT a credential and must NOT be labeled official.
# Extensible per vertical; seeded to cover every society the pages/tests already expect (auanet, ama-assn).
_AUTHORITY_ORGS = {
    # medical / clinical professional societies + guideline bodies
    "ama-assn.org", "auanet.org", "endocrine.org", "aace.com", "obesitymedicine.org",
    "diabetes.org", "heart.org", "acc.org", "cancer.org", "aad.org", "aap.org", "acog.org",
    "apa.org", "psychiatry.org", "aafp.org", "acponline.org", "gastro.org", "thyroid.org",
    "kidney.org", "lung.org", "rheumatology.org", "uspreventiveservicestaskforce.org",
    "cochrane.org", "ada.org",
    # finance / legal professional bodies
    "finra.org", "sipc.org", "aicpa.org", "cfainstitute.org", "nfcc.org", "americanbar.org",
    # international organizations
    "who.int", "oecd.org", "un.org", "worldbank.org", "imf.org",
}


def _is_ymyl_brand(brand):
    """FU133: deterministic YMYL vertical ('medical'|'finance'|'legal'|None) from the brand's
    own enrichment text. Word-boundary matched for short tokens so 'trt' can't hit 'attrition'."""
    text = " ".join(str((brand or {}).get(k) or "") for k in
                    ("category", "context", "use_cases", "pain_points", "audience")).lower()
    if not text.strip():
        return None
    for vertical, terms in _YMYL_LEXICON.items():
        for t in terms:
            if len(t) <= 4:
                if re.search(r"\b" + re.escape(t.strip()) + r"\b", text):
                    return vertical
            elif t in text:
                return vertical
    return None


# FU93 (P2) — source-selection hygiene: a hit-piece/"products to avoid" roundup is opposition
# research, not comparison sourcing — citing one turns the page into a takedown. Matched on the
# source TITLE at every evidence accept-point; a drop is logged, never silent.
_HIT_PIECE_RE = re.compile(
    r"steer\s+clear|stay\s+away|\bavoid\b|\bworst\b|(?:don'?t|do\s+not|should\s+not|never)\s+buy|"
    r"\bterrible\b|\bscam\b|rip-?off|\bbeware\b",
    re.IGNORECASE)


def _is_hit_piece(src):
    """True when a search result's title reads as a hit-piece/negative roundup (FU93 P2)."""
    return bool(_HIT_PIECE_RE.search((src or {}).get("title") or ""))


# FU140 — NON-EVIDENCE sources: pages that say nothing about a company's service capabilities —
# job listings / recruitment ads (a Coface credit-analyst posting is not proof of anything),
# careers pages, salary pages. Filtered at every evidence accept-point alongside hit-pieces.
_NON_EVIDENCE_RE = re.compile(
    r"job\s+(?:listing|posting|opening|description)|(?:jobs?|careers?)\s+(?:at|in|with)\b|"
    r"\bhiring\b|we'?re\s+hiring|apply\s+(?:now|today)|\bvacanc(?:y|ies)\b|"
    r"\bsalar(?:y|ies)\s+(?:at|for)\b|\brecruit(?:ing|ment)\b|\binternship\b",
    re.IGNORECASE)
_JOB_BOARD_DOMAINS = {
    "simplyhired.com", "indeed.com", "glassdoor.com", "ziprecruiter.com", "monster.com",
    "lever.co", "greenhouse.io", "workable.com", "jobvite.com", "wellfound.com",
}


# FU192 — a chat model answers a rewrite request CONVERSATIONALLY. The section prompt already says
# "Do NOT add a heading, a preamble, or any commentary", and a live blog still shipped
# "Certainly. Here is the rewritten section:" INSIDE an FAQ answer. A prompt rule is not a guarantee —
# the heading half of that same instruction is already enforced in code, and this is its missing twin.
# Head-anchored and whole-line only, so ordinary prose can never be eaten: "Certainly." is stripped as
# the FIRST line of a reply and kept everywhere else (it is a legitimate answer to a yes/no question,
# which is exactly how it read in the live leak).
_PREAMBLE_LEAD = re.compile(
    r"^\s*(?:"
    r"(?:certainly|sure|of course|absolutely|got it|understood|okay|ok)\s*[.!,:]?\s*$"
    r"|(?:(?:certainly|sure|of course|absolutely)\s*[.!,]\s*)?"
    r"(?:here(?:'s| is)|below is|this is|the following is)\b[^\n]{0,90}:\s*$"
    r"|(?:i(?:'ve| have)\s+(?:rewritten|reworded|revised|rephrased)|as (?:requested|instructed))"
    r"\b[^\n]{0,90}:?\s*$"
    r")", re.IGNORECASE)
_PREAMBLE_TAIL = re.compile(
    r"^\s*(?:let me know\b|i hope (?:this|that) helps\b|hope (?:this|that) helps\b|"
    r"feel free to\b)[^\n]{0,120}$", re.IGNORECASE)
_BREAK_ONLY = re.compile(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$")
# The same conversational tic, but INLINE at the head of the real content rather than on its own line
# ("Certainly, and this is among the key financial aspects to consider."). It survives the line-level
# strip because the rest of the line is genuine text. Under a question heading it also reads as an
# ANSWER, which is why it was left alone at first — but "Certainly." is not a liftable answer, and the
# declarative sentence after it is, so trimming makes the chunk stronger, not weaker. A leftover "and"
# or "but" goes with it. NOTE "yes"/"no" are deliberately ABSENT: those ARE the direct answer the
# answer-first rules ask for.
_INTERJECTION = re.compile(
    r"^(?:certainly|absolutely|sure|of course|indeed|definitely)\s*[.,!]\s+(?:(?:and|but|so)\s+)?"
    r"(?=\S)", re.IGNORECASE)


def _strip_model_preamble(text):
    """Drop a conversational lead-in (and its trailing sign-off) from a writer reply, plus the
    separator a model often puts between the preamble and the content. Returns the text unchanged when
    nothing matches, and never returns empty — if stripping would consume the whole reply, the original
    is kept, because a damaged reply is better caught by the existing validation than silently blanked."""
    if not (text or "").strip():
        return text or ""
    lines = text.split("\n")
    i, n = 0, len(lines)
    # Both scans are SPECULATIVE: blank lines are only consumed once the pattern after them actually
    # matches. Committing them eagerly would trim a clean reply's own leading/trailing blank lines,
    # which is a silent edit to text that has nothing wrong with it.
    for _ in range(6):                                   # bounded; a real preamble is 1-2 lines
        k = i
        while k < n and not lines[k].strip():
            k += 1
        if k < n and _PREAMBLE_LEAD.match(lines[k]):
            k += 1
            while k < n and (not lines[k].strip() or _BREAK_ONLY.match(lines[k])):
                k += 1
            i = k
            continue
        break
    j = n
    for _ in range(3):
        k = j
        while k > i and not lines[k - 1].strip():
            k -= 1
        if k > i and _PREAMBLE_TAIL.match(lines[k - 1]):
            j = k - 1
            continue
        break
    # the inline tic at the head of the real content, once any preamble LINE is out of the way
    trimmed = False
    if i < j:
        _cut = _INTERJECTION.sub("", lines[i], count=1)
        if _cut != lines[i] and _cut.strip():
            lines = list(lines)
            lines[i] = _cut[:1].upper() + _cut[1:]
            trimmed = True
    if i == 0 and j == n and not trimmed:
        return text                                      # nothing matched → BYTE-IDENTICAL
    out = "\n".join(lines[i:j]).strip()
    if not out:
        return text
    print(f"[blog_gen] writer-preamble: dropped {i} lead line(s) and {n - j} trailing line(s)"
          + (" + an inline opener" if trimmed else ""), flush=True)
    return out


def _is_non_evidence(src):
    """FU140: True when a search result can never serve as evidence — a hit-piece (FU93) OR a
    job listing / recruitment ad / careers page (by title pattern, job-board domain, or a
    linkedin.com/*/jobs URL)."""
    if _is_hit_piece(src):
        return True
    title = (src or {}).get("title") or ""
    url = ((src or {}).get("url") or "").lower()
    if _NON_EVIDENCE_RE.search(title):
        return True
    d = _norm_domain(url)
    if d in _JOB_BOARD_DOMAINS:
        return True
    if "linkedin.com" in url and "/jobs" in url:
        return True
    return False


# FU150 — a blog must NEVER cite/source anything NEGATIVE about the SUBJECT brand. Beyond the
# hit-piece TITLE filter, this catches a source whose title/fact carries a negativity marker AND
# names the subject (a "{name} complaints / lawsuit / stay away" page). Applied at every accept
# point that can ingest a subject-mentioning source. Heuristic — the rule is absolute, so it errs
# toward dropping.
_NEGATIVE_RE = re.compile(
    r"\b(?:complaints?|lawsuits?|sued|class[- ]action|scam|rip-?off|fraud|"
    r"disappoint(?:ed|ing|ment)|terrible|horrible|awful|worst|sucks?|"
    r"stay\s+away|steer\s+clear|beware|red\s+flags?|not\s+worth|waste\s+of\s+money|"
    r"regret|nightmare|warning|avoid|problems?\s+with|issues?\s+with|downsides?|"
    r"bad\s+reviews?|negative\s+reviews?|do\s+not\s+recommend|don'?t\s+recommend)\b",
    re.IGNORECASE)


def _is_negative_about(src, subject_name):
    """FU150 (#2): True when a search result speaks NEGATIVELY about the SUBJECT brand — a
    negativity marker in its title/fact AND the subject's name present. Never cited."""
    nm = (subject_name or "").strip().lower()
    if not nm:
        return False
    blob = ((src or {}).get("title") or "") + " " + ((src or {}).get("fact") or "")
    if nm not in blob.lower():
        return False
    return bool(_NEGATIVE_RE.search(blob))


def _kf_slug(s):
    """Lowercase hyphen-slug for matching product names / URL paths (vertical-neutral)."""
    return re.sub(r"[^a-z0-9]+", "-", (s or "").strip().lower()).strip("-")


def _kf_pricing_items(key_facts):
    """FU150 (#4): normalize key_facts['pricing'] to a PER-PRODUCT list of items
    [{product, value, source_url, verified_at, previous?}]. Migrate-on-read: the old single
    {value, source_url, verified_at} shape becomes one general item (product='')."""
    kf = key_facts if isinstance(key_facts, dict) else {}
    p = kf.get("pricing")
    if isinstance(p, dict):
        items = p.get("items")
        if isinstance(items, list):
            return [dict(it) for it in items
                    if isinstance(it, dict) and str(it.get("value") or "").strip()]
        if str(p.get("value") or "").strip():   # old single-value shape
            return [{"product": "", "value": str(p.get("value")).strip(),
                     "source_url": p.get("source_url") or "", "verified_at": p.get("verified_at") or ""}]
    return []


def _kf_fact_items(key_facts):
    """FU177: the operator's CANONICAL BRAND FACTS — free-form lines the blog must treat as {name}'s
    OWN authoritative first-party facts (the non-pricing sibling of _kf_pricing_items). Stored under
    key_facts['facts']['items'] as [{label, value, source_url, operator_set}]; a plain sentence has an
    empty label. Tolerant on read: a bare list of strings and a legacy {key: "value"} scalar both
    normalize, so nothing an operator (or an older save) wrote is silently dropped."""
    kf = key_facts if isinstance(key_facts, dict) else {}
    node = kf.get("facts")
    raw = node.get("items") if isinstance(node, dict) else (node if isinstance(node, list) else [])
    out = []
    for it in (raw or []):
        if isinstance(it, dict):
            val = str(it.get("value") or "").strip()
            if val:
                out.append({"label": str(it.get("label") or "").strip(), "value": val,
                            "source_url": str(it.get("source_url") or "").strip(),
                            "operator_set": bool(it.get("operator_set", True))})
        elif str(it or "").strip():
            out.append({"label": "", "value": str(it).strip(), "source_url": "", "operator_set": True})
    return out


def _parse_fact_lines(text):
    """FU177: parse the operator's "Brand facts" textarea into _kf_fact_items shape. A line is
    FREE-FORM PROSE by default — no structure is required. Two OPTIONAL conveniences are honoured when
    present: a trailing `| https://…` becomes that fact's source URL, and a remaining `Label | Value`
    split labels the fact. Everything else (including a sentence that happens to contain a pipe) is
    kept as the fact's text. Blank lines are skipped."""
    items = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        url = parts.pop() if (len(parts) > 1 and re.match(r"^https?://", parts[-1], re.I)) else ""
        if len(parts) >= 2 and parts[0] and parts[1]:
            label, value = parts[0], " | ".join(parts[1:]).strip()
        else:
            label, value = "", " | ".join(p for p in parts if p).strip()
        if value:
            items.append({"label": label, "value": value, "source_url": url, "operator_set": True})
    return items


_FACT_FILLER = {"the", "and", "for", "with", "from", "that", "this", "are", "was", "its", "our",
                "all", "any", "per", "not", "does", "has", "have", "you", "your", "their", "than",
                "only", "also", "but", "can", "will", "into", "over", "out", "off", "via"}


def _fact_anchor_tokens(value):
    """FU178: the tokens a page MUST contain for us to accept it as stating this fact. Deliberately
    strict — citing the wrong page is worse than not citing at all. Anchors = tokens carrying a digit,
    tokens that were Capitalised/UPPER in the operator's line (names, codes, acronyms), and the
    distinctive long words (≥6 chars). Generic filler is dropped. Capped so a long sentence doesn't
    become impossible to match."""
    raw = re.findall(r"[A-Za-z0-9][A-Za-z0-9./%$£€-]*", value or "")
    anchors = []
    for w in raw:
        lw = w.lower().strip(".,;:")
        if len(lw) < 3 or lw in _FACT_FILLER:
            continue
        if re.search(r"\d", w) or w[:1].isupper() or len(lw) >= 6:
            if lw not in anchors:
                anchors.append(lw)
    return anchors[:6]


def _fact_stated_in(value, text):
    """FU178: True when `text` (a fetched first-party page) plausibly STATES the fact — every anchor
    token present. With no anchors at all (a very short generic line) fall back to requiring the whole
    normalized value as a substring, which is stricter still."""
    hay = re.sub(r"\s+", " ", (text or "").lower())
    if not hay:
        return False
    anchors = _fact_anchor_tokens(value)
    if not anchors:
        return re.sub(r"\s+", " ", (value or "").lower()).strip() in hay
    return all(a in hay for a in anchors)


def _drop_nameless_when_named(items):
    """FU163: a NAMELESS general (empty-product) pricing item must never coexist with NAMED-product
    items — a catch-all price (e.g. a homepage "flexible plans … $79/mo") contradicts the specific
    per-product prices. Drops every empty-product item when ≥1 named-product item exists; a
    single-product / general-only brand (no named items) is left untouched. Returns (items, dropped)."""
    items = list(items or [])
    if any(str(it.get("product") or "").strip() for it in items):
        kept = [it for it in items if str(it.get("product") or "").strip()]
        return kept, (len(kept) != len(items))
    return items, False


# FU156: generic filler dropped so a category/price word doesn't match every page.
_PRODUCT_FILLER = {"the", "and", "for", "with", "its", "their", "together", "combined", "use",
                   "therapy", "treatment", "medication", "medications", "drug", "drugs", "injection",
                   "tablets", "oral", "weekly", "online", "clinic", "clinics", "program", "programs",
                   "plan", "plans", "price", "pricing", "cost", "costs", "buy", "get", "best", "shop"}


# FU198 — a credential page that is not about the ARTICLE'S subject is not evidence for its
# comparison. When a brand's category is broader than one article (a full-service firm, a multi-line
# agency, a hospital system, a contractor with several trades), competitor searches anchored on the
# CATEGORY return standing in some other part of the business; the comparison then fills up with it,
# which pads the source list AND hides that a competitor may not do the article's subject at all.
_CREDENTIAL_SHAPE_RE = re.compile(
    r"\brank(?:ing|ings|ed)?\b|\btier\s*\d|\bband\s*\d|\baward(?:s|ed)?\b|\btop\s+\d"
    r"|\bbest\s+\d|\bleague\s+table\b|\bprofile\b|\bdirectory\b|\brated\b", re.I)

# Page classes that evidence no practice at ALL, however reputable the host: graduate / trainee
# recruitment material ("what it's like to train here" is not a practice ranking) and general
# encyclopedias. Wikipedia sits in _THIRD_PARTY_DOMAINS, so without this it counts as reputable
# corroboration for a capability claim.
_NON_CAPABILITY_PATH_RE = re.compile(
    r"\b(?:student|students|graduate|graduates|trainee|trainees|careers?|recruitment|internship)\b",
    re.I)
_NON_CAPABILITY_HOST_RE = re.compile(
    r"student|graduate|trainee|careers|recruitment|internship", re.I)


def _split_url(u):
    """(host, space-separated path) for the shape tests below."""
    u = (u or "").strip()
    return _norm_domain(u), re.sub(r"[-_/]+", " ", re.sub(r"[?#].*$", "", re.sub(r"^https?://[^/]*", "", u)))


def _is_non_capability_source(src):
    """FU198: True for a recruitment/encyclopedia page — never evidence that an option DOES a thing."""
    if not isinstance(src, dict):
        return False
    host, path = _split_url(src.get("url"))
    if host.endswith("wikipedia.org") or host.endswith("wikimedia.org"):
        return True
    # the host is matched as a SUBSTRING (a concatenated host like "chambersstudent.co.uk" has no
    # word boundary), the path with word boundaries so an ordinary /careers-page slug is not over-read
    return bool(_NON_CAPABILITY_HOST_RE.search(host) or _NON_CAPABILITY_PATH_RE.search(path))


def _is_offtopic_credential(src, subject_tokens):
    """FU198: True when a result is CREDENTIAL-shaped (ranking / tier / award / directory profile)
    but carries NONE of the article subject's distinctive tokens — standing in a different practice
    area, product line or service line. Vertical-neutral: the shape is generic and the tokens are
    derived per article. INERT when no subject tokens were derived, so it can never fire by default."""
    if not subject_tokens or not isinstance(src, dict):
        return False
    title = src.get("title") or ""
    _host, path = _split_url(src.get("url"))
    if not _CREDENTIAL_SHAPE_RE.search(title + " " + path):
        return False
    blob = (title + " " + (src.get("url") or "") + " " + (src.get("fact") or "")).lower()
    return not any(t in blob for t in subject_tokens)


def _product_tokens(s):
    """FU156: a product name's distinctive tokens for matching a URL path / title / fact — generic
    filler dropped (so 'tirzepatide program' doesn't match every /program/ page). Mirrors the FU142
    subject tokenizer."""
    return [w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower()) if w not in _PRODUCT_FILLER]


def _best_product_price(results, product, own_domain):
    """FU156: PURE selection (issues NO search) — from a list of {url,title,fact} results a search
    ALREADY returned, return the best {url, fact} that is (a) on `own_domain`, (b) carries a price
    signal, and (c) product-MATCHES `product` (a distinctive product token in the URL PATH, title, or
    fact). Prefers a URL-PATH match (the dedicated /product/<product>/ page) over a text-only mention.
    Returns None when nothing matches. `product` empty (single-product/general brand) → the best
    own-domain priced result (any). This is the shared logic used for the subject AND every competitor;
    it adds zero cost — it just picks smartly among results already paid for."""
    toks = _product_tokens(product)
    best, best_score = None, -1
    for s in (results or []):
        if not isinstance(s, dict):
            continue
        url = (s.get("url") or "").strip()
        fct = (s.get("fact") or s.get("title") or "").strip()
        if not url or not fct:
            continue
        dom = _norm_domain(url)
        if not dom or not (dom == own_domain or dom.endswith("." + own_domain)):
            continue
        if not _PRICE_SIGNAL_RE.search(fct):
            continue
        if toks:   # a specific product → require a token match; prefer the product-page URL
            path = re.sub(r"^[a-z]+://[^/]+", "", url).lower()   # URL path (scheme+host stripped)
            blob = (fct + " " + str(s.get("title") or "")).lower()
            if any(t in path for t in toks):
                score = 2
            elif any(t in blob for t in toks):
                score = 1
            else:
                continue   # priced own-domain page but NOT about this product → reject (the TRT case)
        else:
            score = 0
        if score > best_score:
            best, best_score = {"url": url, "fact": fct}, score
    return best


def _canonical_price_item(key_facts, product):
    """FU158: from stored key_facts pricing items, return the single AUTHORITATIVE item for `product` —
    an OPERATOR-SET product match first, then any product-token match, then (only for a general/empty
    `product`) an operator-set item else the first item. Returns None for a SPECIFIC product with no
    matching item (never substitute a different product's / general price as this product's price).
    Used to make an operator-set / canonical price the single source of truth for the SUBJECT's pricing
    cell (Change 1) AND the JSON-LD Offer (Change 3)."""
    items = _kf_pricing_items(key_facts)
    if not items:
        return None
    ptoks = set(_product_tokens(product))

    def _match(it):
        return bool(ptoks and (ptoks & set(_product_tokens(it.get("product") or ""))))

    return (next((it for it in items if it.get("operator_set") and _match(it)), None)
            or next((it for it in items if _match(it)), None)
            or (None if ptoks
                else (next((it for it in items if it.get("operator_set")), None) or items[0])))


def _canonical_facts_block(name, key_facts, seed_products=None):
    """FU150 (#4): render {name}'s CANONICAL PER-PRODUCT first-party facts (pricing) into a writer-
    prompt block so EVERY blog states the SAME values (cluster sync). The blog's seed-product item is
    listed FIRST (this blog's product), the rest as consistency context. Empty when nothing stored."""
    items, _ = _drop_nameless_when_named(_kf_pricing_items(key_facts))   # FU163: no nameless general beside named
    sp = [_kf_slug(p) for p in (seed_products or []) if str(p).strip()]

    def _rank(it):
        ps = _kf_slug(it.get("product") or "")
        if sp and ps and any(ps == s or (len(ps) >= 4 and (ps in s or s in ps)) for s in sp):
            return 0
        return 1
    items = sorted(items, key=_rank)
    lines = []
    for it in items:
        prod = str(it.get("product") or "").strip()
        val = str(it.get("value") or "").strip()
        # FU158: mark an operator-set/locked line so the writer knows it OUTRANKS any priced evidence.
        tag = " [operator-set, authoritative — locked]" if it.get("operator_set") else ""
        if val:
            lines.append((f"  - pricing ({prod}){tag}: {val}" if prod else f"  - pricing{tag}: {val}"))
    kf = key_facts if isinstance(key_facts, dict) else {}
    # FU177: the operator's free-form CANONICAL BRAND FACTS — same authority as a locked price. No
    # per-line [operator-set] tag here (unlike pricing, where it separates operator from auto-synced
    # items): every line in this list is operator-supplied, and the header already says so.
    for it in _kf_fact_items(kf):
        lab = it.get("label") or ""
        lines.append(f"  - {lab}: {it['value']}" if lab else f"  - {it['value']}")
    for k, v in kf.items():
        if k in ("pricing", "facts"):
            continue
        val = (v.get("value") if isinstance(v, dict) else v) or ""
        val = str(val).strip()
        if val:
            lines.append(f"  - {k}: {val}")
    if not lines:
        return ""
    # FU158: these values are AUTHORITATIVE first-party EVIDENCE — they must fill {name}'s COMPARISON-
    # TABLE pricing cell and every {name} price sentence (not only the meta/prose), and a general
    # plan/consult/membership/base fee must NEVER be substituted as {name}'s product price.
    return (f"CANONICAL {name} FACTS (first-party — {name}'s OWN authoritative values; use these EXACT "
            f"values VERBATIM everywhere {name}'s own facts appear, and cite {name}'s own site; NEVER a "
            f"third-party number. Each pricing line is for the named product/service. These ARE "
            f"first-party EVIDENCE: for {name}'s comparison-table pricing cell AND any {name} price "
            f"sentence, use the canonical value for THIS article's product VERBATIM — do NOT substitute a "
            f"general plan / consult / membership / base fee (e.g. a base '$X/mo plans' or a processing "
            f"fee) as {name}'s product price; an [operator-set] line is locked and overrides any priced "
            f"page you find. A NON-pricing line is a fact about {name} the operator supplied: use it "
            f"where it is relevant to this article, keep every value in it EXACT, and NEVER contradict "
            f"it or hedge it as unconfirmed. CITE it ONLY when the EVIDENCE contains a page that "
            f"actually states it — otherwise state it as {name}'s OWN POSITIONING and attribute it "
            f"(\"{name} says it …\"); never attach a citation to a line you cannot find in the "
            f"EVIDENCE):\n"
            + "\n".join(lines) + "\n")


# FU141 — review-shaped titles ("PeterMD Review… Worth It?", "Is It Safe/Legit", "X vs Y",
# "Top 7 …"). NOTE the best-(?!practice) exemption: official bodies publish "Best Practice
# Statements" — only listicle-best is review-shaped.
_REVIEWISH_RE = re.compile(
    r"\breview(?:s|ed)?\b|worth\s+it|is\s+it\s+(?:safe|legit|worth)|\bvs\.?\b|"
    r"\btop\s+\d|\bbest\s+(?!practice)|\bpromo\b|coupon|discount\s+code",
    re.IGNORECASE)


def _is_subject_review(src_or_title, brand_name):
    """FU141: True when a source is a REVIEW OF THE SUBJECT BRAND (title names the brand AND is
    review-shaped). Such sources are tagged `review ·` and capped — they may add program-facts
    color but are never authoritative and never clinical support."""
    title = src_or_title.get("title") if isinstance(src_or_title, dict) else src_or_title
    title = (title or "")
    bn = re.sub(r"\s+", "", (brand_name or "")).lower()
    if not bn or bn not in re.sub(r"\s+", "", title).lower():
        return False
    return bool(_REVIEWISH_RE.search(title))


def _is_affiliate_review(src, own_domain=""):
    """FU161: True when a source is a LOW-QUALITY AFFILIATE / SEO review that must be DROPPED for a
    competitor (a positive review of a COMPETITOR is caught by none of the hit-piece / subject-review
    filters). Affiliate = the domain is a known aggregator/affiliate (_STALE_AGGREGATORS) OR its title
    is review-shaped AND the domain is NOT reputable (_THIRD_PARTY_DOMAINS), NOT official/.gov/.nih, and
    NOT the competitor's OWN site. Reputable reviews (Forbes / G2 / U.S. News) and own-site pages are
    NEVER affiliate. Vertical-neutral (shape + domain heuristic)."""
    if not isinstance(src, dict):
        return False
    d = _norm_domain(src.get("url") or "")
    if not d:
        return False
    if own_domain and (d == own_domain or d.endswith("." + own_domain)):
        return False   # the competitor's own site is never 'affiliate'
    if d in _STALE_AGGREGATORS:
        return True
    # FU204: a star-RATING page is aggregated customer sentiment, not evidence for a product fact.
    # It used to be exempt here purely because trustpilot.com sat in the SaaS-era reputable list.
    if _source_class(src.get("url") or "") == "review":
        return True
    if d in _THIRD_PARTY_DOMAINS or d.endswith(".gov") or "nlm.nih" in d or "ncbi.nlm" in d:
        return False   # reputable / official — keep
    # FU184: test the review SHAPE against the TITLE **and the URL PATH**. A review-shaped slug
    # ("/calibrate-weight-loss-program-review/") on a non-reputable domain is an affiliate review even
    # when the page TITLE happens to omit a review word — that gap is how one shipped as a competitor's
    # sole price source. Safe in both directions: the competitor's OWN site and reputable/official
    # domains are exempted ABOVE, so only a non-reputable third party is newly caught.
    _path = re.sub(r"[?#].*$", "", re.sub(r"^https?://[^/]*", "", (src.get("url") or "").strip()))
    _path = re.sub(r"[-_/]+", " ", _path)
    return bool(_REVIEWISH_RE.search(((src.get("title") or "") + " " + _path)))


def _official_source_ok(url, title, brand_name, own_domain, pins):
    """FU141: THE one validator for granting the `official ·` badge, everywhere. Generic —
    brand name, own domain and vertical pins are all parameters; every rule is shape-based.
    A page about the CLIENT is never official; a review-shaped title is never official;
    .org alone is NOT a credential (affiliates squat .org TLDs) — it passes only after
    surviving every rejection."""
    d = _norm_domain(url or "")
    if not d:
        return False
    if own_domain and (d == own_domain or d.endswith("." + own_domain)):
        return False
    if d in _THIRD_PARTY_DOMAINS or d in _STALE_AGGREGATORS or d in _JOB_BOARD_DOMAINS:
        return False
    if _is_non_evidence({"title": title, "url": url}):
        return False
    # STRONG domain credentials — the domain IS the authority; a title shape can't demote a
    # .gov / NIH / vertical-pinned page (real rules are titled "Regulation Best Interest",
    # "Best Practice Statement", …).
    if d.endswith(".gov") or "nlm.nih" in d or "ncbi.nlm" in d:
        return True
    for p in (pins or []):
        if d == p or d.endswith("." + p):
            return True
    # FU163: RECOGNIZED-AUTHORITY .org/.int only — professional societies / international bodies (the
    # domain IS the authority, like a pin). A GENERIC .org/.int (industry / education / advocacy site —
    # e.g. telehealth.org) is NOT a credential and no longer earns "official ·" for the TLD alone; it
    # flows to normal "third-party ·" labeling, and a leg that keeps 0 officials retries for a .gov source.
    if d in _AUTHORITY_ORGS or any(d.endswith("." + a) for a in _AUTHORITY_ORGS):
        return True
    return False


def _evidence_tier(block, brand_name, own_domain):
    """FU141: authority tier for evidence ordering — official → provided → subject-own →
    competitor/vendor → community/third-party → review."""
    lab = str((block or {}).get("label") or "")
    low = lab.lower()
    d = _norm_domain((block or {}).get("url") or "")
    if low.startswith("official ·"):
        return 0
    if low.startswith("provided"):
        return 1
    if (brand_name and lab.strip().lower() == brand_name.strip().lower()) or \
       (own_domain and d and (d == own_domain or d.endswith("." + own_domain))):
        return 2
    if low.startswith("review ·"):
        return 5
    if low.startswith("community") or low.startswith("third-party") or "reddit.com" in (d or ""):
        return 4
    return 3   # competitor/vendor own-page blocks (labeled with the tool's name)


def _as_list(raw):
    """Best-effort list of non-empty strings from a JSON string / delimited string /
    list / dict / None. Used for both stored enrichment fields and LLM outputs."""
    if isinstance(raw, str):
        s = raw.strip()
        try:
            raw = json.loads(s)
        except (json.JSONDecodeError, TypeError):
            raw = re.split(r"[\n;,]", s)
    if isinstance(raw, dict):
        raw = list(raw.values())
    out = []
    for x in (raw or []):
        v = str(x).strip()
        if v:
            out.append(v)
    return out


_GEO_LEXICON = [
    # (canonical term, word-boundary alternation) — common countries/regions only; states/cities/any
    # other region come from the EXPLICIT geo input (FU90), which always wins. ACRONYMS are matched
    # case-sensitively ("US" must not hit the pronoun "us"); full names case-insensitively via (?i:…).
    ("the US", r"US|USA|U\.S\.A?\.?|(?i:united states|america|american)"),
    ("the UK", r"UK|U\.K\.|(?i:united kingdom|britain|british|england)"),
    ("Australia", r"AUS|(?i:australia|australian)"),
    ("Canada", r"(?i:canada|canadian)"),
    ("Europe", r"EU|(?i:europe|european)"),
    ("India", r"(?i:india|indian)"),
    ("Germany", r"(?i:germany|german)"),
    ("France", r"(?i:france|french)"),
    ("Singapore", r"(?i:singapore)"),
    ("the UAE", r"UAE|(?i:dubai)"),
    ("New Zealand", r"NZ|(?i:new zealand)"),
]


def _seed_geo(seed):
    """FU90 — deterministically detect a COMMON geography named in the seed; returns the canonical
    term or "". Word-boundary so "usage"/"aus…" can't false-hit. The operator's explicit geo input
    always wins over this fallback (it covers states, cities, and any region the lexicon can't)."""
    s = (seed or "").strip()
    if not s:
        return ""
    for canon, alt in _GEO_LEXICON:
        if re.search(rf"\b(?:{alt})\b", s):
            return canon
    return ""


def _seed_qualifier(seed):
    """FU93 (P1) — deterministically detect a trailing purchase/variant QUALIFIER in the seed
    ("with financing", "without a contract", "under $5000"); returns the phrase or "".
    HIGH-PRECISION on purpose: ambiguous forms ("for beginners", "for small teams") are NOT
    auto-captured — they come from the EXPLICIT qualifier input, which always wins."""
    s = (seed or "").strip().rstrip("?!. \t")
    if not s:
        return ""

    def _strip_geo_tail(q):
        # "financing in the US" -> "financing" (the geo axis is handled separately by _seed_geo)
        for _canon, alt in _GEO_LEXICON:
            q = re.sub(rf"[\s,]+(?:in|across|for|throughout)\s+(?:the\s+)?(?:{alt})\s*$", "", q)
        return q.strip(" ,")

    m = re.search(r"\bwith\s+([\w$][\w$%/&' -]{1,40})$", s, re.IGNORECASE)
    if m:
        q = _strip_geo_tail(m.group(1))
        if q:
            return q
    m = re.search(r"\b(without\s+[\w$][\w$%/&' -]{1,40})$", s, re.IGNORECASE)
    if m:
        q = _strip_geo_tail(m.group(1))
        if q and q.lower() != "without":
            return q
    m = re.search(r"\b((?:under|over|below|above)\s+\$?\d[\d,.]*[km]?)\b", s, re.IGNORECASE)
    if m:
        return m.group(1)
    # FU135 — AUDIENCE qualifiers, small demographic lexicon only (high precision): "for men",
    # "where can men …", "should seniors …". An audience page with a swapped noun cannibalizes
    # the generic page — detection routes it through the FU93 substance machinery.
    _AUD = r"(?:men|women|seniors|teens|teenagers|athletes|veterans|kids|children)"
    m = re.search(rf"\bfor\s+({_AUD})\b", s, re.IGNORECASE)
    if m:
        return f"for {m.group(1).lower()}"
    m = re.search(rf"^(?:where\s+|how\s+)?(?:can|do|does|should|will)\s+({_AUD})\b", s, re.IGNORECASE)
    if m:
        return f"for {m.group(1).lower()}"
    return ""


def _brand_in_comparison(body, name):
    """FU84 — True when the brand appears in a Markdown TABLE ROW of the body, i.e. the brand is one
    of the options being COMPARED. Drives the factually-safe disclosure fallback wording."""
    if not body or not (name or "").strip():
        return False
    return bool(re.search(r"^\|[^\n]*" + re.escape(name.strip()), body, re.MULTILINE | re.IGNORECASE))


def _norm_domain(u):
    """Registrable-ish domain from a url/bare domain: strip scheme + path + a leading 'www.' so a
    www / non-www variant compares equal (FU54). Module-level so it's unit-testable."""
    u = re.sub(r"^https?://", "", (u or "").strip()).rstrip("/")
    d = u.split("/")[0].lower()
    return d[4:] if d.startswith("www.") else d


class BlogGenerator:
    def __init__(self, claude, db, writer=None, writer_mode="off"):
        self.claude = claude
        self.db = db
        # FU153: optional self-hosted open-model writer for the final content-writing pass
        # (watermark strip). writer=None / writer_mode="off" → NO writer pass → today's flow
        # byte-identical. Set (a WriterClient, "rewrite"|"compose") to enable. app.py resolves
        # the mode/endpoint/key from app_meta→env and injects them.
        self.writer = writer
        self.writer_mode = (writer_mode or "off")
        self._evidence_blocks = []   # set by _gather_evidence; read by _rebuild_sources
        # FU205 (R4) — the deterministic checks' notes, initialised EXPLICITLY. Every one is read
        # with `getattr(self, ..., default)` in `_finalize_article`, so an absent attribute silently
        # evaluates to "clean" instead of failing loudly. On the FU79 resume path — a fresh instance
        # where sourcing does NOT re-run — that meant peer-check, official-source gap,
        # mechanics-check, the subject-price warning and brand-facts reported clean on every paused
        # blog, while `self-reference` read an empty `_sibling_urls` and so false-positived on every
        # matching sentence. They are carried through the pause checkpoint now (see
        # `_check_notes` / `finish_pending_blog`); declaring them here is what makes that state
        # visible rather than implied.
        self._peer_note = ""          # FU98: "best/top <type>" page with <2 same-type competitors
        self._auth_note = ""          # FU142: a non-primary product with no official source
        self._facts_note = ""         # FU178: the canonical-fact verification tier tally
        self._price_warn = ""         # FU161: the subject's price could not be confirmed
        self._invented_note = ""      # FU184: a competitor the model named itself
        self._table_punt_note = ""    # FU138: the unsourced-table resolution outcome
        self._core_mechanics = []     # FU198: the subject's defining mechanics
        self._sibling_urls = set()    # FU197: the brand's PUBLISHED pages, for the self-reference check
        self._article_tools = []      # FU204: the compared brand names, for the citation check
        self._budget_warn = ""        # FU205 (R6): a starvation note carried across a FU79 pause
        self._removed_brands = []     # FU206: brands the operator removed from the whole blog
        self._removed_refused = []    # FU206: removals refused because they would breach the floor
        # Reuse the embedding relevance helpers (graceful no-op without an OPENAI key)
        # to filter fan-out queries to the seed. Cheap to construct.
        self._pg = PostGenerator(claude, db)

    # ------------------------------------------------------------------ context
    def _brand_block(self, brand):
        """First-party brand context. Returns (name, domain_url, block_text). Unlike
        the Reddit block, this NAMES the brand — blogs are owned media."""
        b = brand or {}
        name = (b.get("name") or "the brand").strip()
        url = (b.get("domain_url") or "").strip()
        # FU84: the brand controls its casing (e.g. "OutSail") — the model must never normalize it.
        lines = [f"Brand name: {name}   (write the brand name with this EXACT spelling and casing everywhere)"]
        if url:
            lines.append(f"Website: {url}")
        if b.get("category"):
            lines.append(f"Category: {b['category']}")
        if b.get("audience"):
            lines.append(f"Audience: {b['audience']}")
        for label, key in (("Use cases", "use_cases"), ("Pain points", "pain_points"),
                           ("Features", "features")):
            vals = _as_list(b.get(key))
            if vals:
                lines.append(f"{label}: {', '.join(vals)}")
        # FU199 — the competitor line is the ONE piece of brand context that must not be the same on
        # every article. The operator's list is brand-level by nature; when this article has a NARROWER
        # subject, say so and offer the specialists found for it. Byte-identical when inert.
        _comp = _as_list(b.get("competitors"))
        _sphr = getattr(self, "_subject_phrase", "") or ""
        _speers = getattr(self, "_subject_peers", None) or {}
        if _sphr and (_comp or _speers):
            if _comp:
                lines.append(f"Competitors (the operator's brand-level list — name one ONLY if it "
                             f"genuinely does {_sphr}): {', '.join(_comp)}")
            if _speers:
                lines.append(f"Specialists in {_sphr} (found for THIS article): "
                             + ", ".join(f"{n} ({d})" for n, d in _speers.items()))
        elif _comp:
            lines.append(f"Competitors: {', '.join(_comp)}")
        if b.get("context"):
            lines.append(f"Context: {b['context']}")
        if b.get("learned_context"):
            lines.append(f"Learned context: {b['learned_context']}")
        return name, url, "\n".join(lines)

    def _byline_md(self, brand):
        """EEAT byline + disclosure block (Markdown). FU152: the AUTHOR line is ALWAYS a generic
        placeholder — blogs are client deliverables, so the client inserts their own byline before
        publishing; a specific/auto-guessed name must never ship. NEVER fabricated. The reviewer +
        disclosure lines still render only from brand-supplied fields."""
        b = brand or {}
        bits = ["[Add author byline before publishing]"]   # FU152: always a replace-me placeholder
        rv = (b.get("reviewer_name") or "").strip()
        if rv:
            rt = (b.get("reviewer_title") or "").strip()
            bits.append(f"Reviewed by {rv}" + (f", {rt}" if rt else ""))
        line = " · ".join(bits)
        disc = (b.get("disclosure") or "").strip()
        out = []
        if line:
            out.append(f"*{line}*")
        if disc:
            out.append(f"*{disc}*")
        return ("\n\n".join(out) + "\n\n") if out else ""

    # ------------------------------------------------------------ evidence sourcing
    def _resolve_brand_domains(self, names, seed=None, subject=None, subject_category=None,
                                want_subject=False):
        """Ask the model for the official homepage domain of each brand it's confident
        about. Resolves both (a) the supplied competitor `names` missing a cached domain
        AND (b) any OTHER brand/product named in the `seed`/title (e.g. "Botric vs
        Profound" -> Profound), excluding `subject`. `subject_category` disambiguates
        same-named companies (e.g. Profound the AI-search tool at tryprofound.com vs
        Profound the market-research firm at profound.com) by anchoring on the subject's
        space. Returns {name: bare-domain}; {} on failure."""
        names = [n for n in (names or []) if str(n).strip()]
        seed = (seed or "").strip()
        subject = (subject or "").strip()
        subject_category = (subject_category or "").strip()
        if not names and not seed:
            return {}
        body = ""
        if names:
            body += "Names:\n" + "\n".join(f"- {n}" for n in names)
        if seed:
            excl = f" Do NOT include {subject} itself (the article is about it)." if subject else ""
            body += (f"\n\nALSO extract every OTHER brand/product name mentioned in this article "
                     f"topic and include it with its domain:{excl}\nTopic: \"{seed}\"")
        ctx = ""
        if subject_category or subject:
            who = f"{subject} ({subject_category})" if subject_category else subject
            ctx = (f"\n\nIMPORTANT: these are competitors/alternatives to {who}. When a name is "
                   "shared by multiple companies, choose the one operating in THAT SAME space — "
                   "NOT a same-named company in an unrelated industry. Pick the domain whose "
                   "product is actually a peer of the subject.")
        # FU199 — WHICH competitors get compared was decided at BRAND level: the operator's list is
        # rendered identically on every article, and peer discovery asks for rivals of the FIRM. For a
        # brand broader than one article (a full-service firm, a multi-line agency, a hospital system,
        # a contractor with several trades) that hands the writer the wrong field entirely. This is the
        # ONLY unconditional, non-search LLM call in the whole pre-draft path and it already receives
        # the seed — so the subject and its specialists ride along here for free, no new call, no new
        # search. The post-draft call site passes nothing, so its prompt is unchanged.
        subj_ask, subj_schema = "", ""
        if want_subject:
            subj_ask = (
                "\n\nALSO return two things about the ARTICLE ITSELF:\n"
                '  "subject" — the specific offering / practice area / product line THIS article is '
                'about, as a SHORT noun phrase (2-6 words, no brand names, no "best"/"top"). It is '
                "usually NARROWER than the category above: a provider that does many things writes one "
                "article about ONE of them. Return the category itself when the provider genuinely "
                "does only that one thing.\n"
                '  "peers" — 3-5 REAL, currently-operating providers that SPECIALISE in that subject '
                "(not merely in the wider category), each with its bare domain. Exclude the subject "
                "itself. Omit any you are not confident still operates in that exact space — a shorter "
                "honest list beats a plausible-sounding wrong peer. Return {} when the subject IS the "
                "category and the names above already cover the field.")
            subj_schema = ', "subject": "", "peers": {"Name": "domain.com"}'
        prompt = ("For each brand/product below, give its official homepage domain (bare, "
                  "no https://, no path). Include ONLY ones you are confident about; omit "
                  "the rest.\n" + body + ctx + subj_ask +
                  '\n\nReturn JSON only: {"domains": {"Name": "domain.com"}' + subj_schema + '}')
        res = self.claude.call(prompt, max_tokens=(700 if want_subject else 400), temperature=0)
        dm = (res or {}).get("domains") if isinstance(res, dict) else None
        out = {}
        if isinstance(dm, dict):
            for n, d in dm.items():
                d = re.sub(r"^https?://", "", str(d or "").strip().lower()).rstrip("/").split("/")[0]
                if str(n).strip() and d:
                    out[str(n).strip()] = d
        if want_subject:
            # INERT GATE (the same test FU198 uses): keep the subject ONLY when it contributes a token
            # the category does not already carry. For a single-line brand it does not, so nothing
            # downstream activates and every prompt is byte-identical to before.
            _sp = re.sub(r"\s+", " ", str((res or {}).get("subject") or "").strip())[:80]
            _cat_toks = set(_product_tokens(subject_category))
            self._subject_phrase = _sp if (_sp and [t for t in _product_tokens(_sp)
                                                    if t not in _cat_toks]) else ""
            _peers, _pr = {}, ((res or {}).get("peers") if isinstance(res, dict) else None)
            if self._subject_phrase and isinstance(_pr, dict):
                for n, d in _pr.items():
                    n = str(n or "").strip()
                    d = re.sub(r"^https?://", "", str(d or "").strip().lower()).rstrip("/").split("/")[0]
                    if n and d and n.lower() != (subject or "").strip().lower():
                        _peers[n] = d
            self._subject_peers = _peers
            print(f"[blog_gen] subject-peers: subject={self._subject_phrase!r} "
                  f"specialists={list(_peers)}", flush=True)
        return out

    def _gather_independent_sources(self, subject, competitors, seed, category, own_domains):
        """Thorough multi-angle independent-source search (Follow-up 35). For the subject + the top
        comparison competitor, run a TARGETED search per ANGLE (reviews / news+funding / analyst+
        pricing), each as TWO passes — pass A on a reputable allowlist (guarantees an independent,
        high-authority source), pass B broad-web with the brands' own sites blocked (niche-brand
        fallback). Merge + dedup by url, capped at _MAX_WEB_SOURCES. Returns [{label,url,text}].
        Never raises (each failing brief is skipped)."""
        out, seen = [], set()
        cat = (category or "").strip()
        # FU150 (#2/#3): the SUBJECT is NOT third-party-swept — brand info is first-party only, and a
        # third-party subject page could be negative about the brand. Sweep only the top competitor.
        brands = [n for n in (list(competitors or [])[:1]) if (n or "").strip()]
        angles = [   # FU56: 2 angles (was 3) — fewer searches per brand
            "independent user REVIEWS and ratings (e.g. G2, Capterra, Trustpilot, TrustRadius)",
            "NEWS / funding / analyst coverage OR third-party PRICING & commercial-license / terms references "
            "(e.g. TechCrunch, Reuters, Forbes, Crunchbase, G2)",
        ]

        def _take(srcs):
            for s in (srcs or []):
                if _is_non_evidence(s):   # FU93/FU140: hit-pieces + job listings never become evidence
                    print(f"[blog_gen] source-hygiene: dropped hit-piece "
                          f"'{(s.get('title') or '')[:70]}'", flush=True)
                    continue
                if _is_negative_about(s, subject):   # FU150 (#2): never cite anything negative about {name}
                    print(f"[blog_gen] source-hygiene: dropped negative-about-brand "
                          f"'{(s.get('title') or '')[:70]}'", flush=True)
                    continue
                if _is_affiliate_review(s):   # FU161: drop low-quality affiliate/SEO reviews
                    print(f"[blog_gen] source-hygiene: dropped affiliate review "
                          f"'{(s.get('title') or '')[:70]}'", flush=True)
                    continue
                url = (s.get("url") or "").strip()
                fact = (s.get("fact") or s.get("title") or "").strip()
                if not url or not fact:
                    continue
                key = url.lower().split("?")[0].rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                out.append({"label": f"{_source_class(url) or 'third-party'} · {s.get('title') or url}",
                            "url": url, "text": fact[:_EVIDENCE_TEXT_CAP]})

        # FU98 — peer discovery, SUBJECT only: the writer needs REAL same-type competitors to
        # name (the model defaults to famous SaaS tools it already knows). One brief, ~2 searches.
        if (subject or "").strip():
            # FU199: `seed` was in scope here and unused, so this hunted rivals of the FIRM. Aim it at
            # the article's subject when there is one, or a multi-line brand gets the wrong field.
            _psub = getattr(self, "_subject_phrase", "") or ""
            pbrief = (f'Find the DIRECT COMPETITORS of "{subject}"'
                      + (f' for {_psub} specifically (providers that actually do {_psub}, not its '
                         f'wider {cat or "category"})' if _psub else (f' ({cat})' if cat else ""))
                      + '. Return AT LEAST 3 (ideally 3-5) distinct real competing providers of the '
                        "SAME TYPE serving the same market — each competitor's NAME, its OWN website "
                        "URL, and one concrete fact about it. NOT the brand's own site; no 'best of' "
                        "listicles from content farms; no negative roundups.")
            try:
                _take(self.claude.search_sources(pbrief, max_searches=2,
                                                 blocked_domains=own_domains))
            except Exception as e:
                print(f"[blog_gen] peer-discovery search skipped: {e}", flush=True)
        for nm in brands:
            for ang in angles:
                if len(out) >= _MAX_WEB_SOURCES:
                    break
                brief = (f'Find {ang} about "{nm}"' + (f' ({cat})' if cat else "")
                         + f'. Topic: {seed}. Prefer recent (2024-2025) coverage. It MUST be an '
                           "INDEPENDENT third-party page, NOT the brand's own website. Do NOT return "
                           "negative-review roundups or 'products/brands to avoid' listicles — seek "
                           "factual coverage (features, pricing, terms, scale).")
                try:
                    got = self.claude.search_sources(brief, max_searches=2,   # FU56: was 3
                                                     allowed_domains=_THIRD_PARTY_DOMAINS)
                    if not got:
                        # niche brand with no reputable page -> broad web, block the brands' own sites
                        got = self.claude.search_sources(brief, max_searches=2,   # FU56: was 3
                                                         blocked_domains=own_domains)
                    _take(got)
                except Exception as e:
                    print(f"[blog_gen] independent-source search ({nm} / {ang[:18]}) skipped: {e}", flush=True)
            if len(out) >= _MAX_WEB_SOURCES:
                break
        if out:
            print(f"[blog_gen] evidence: {len(out)} independent third-party source(s) found", flush=True)
        else:
            print("[blog_gen] evidence: NO independent third-party sources found", flush=True)
        return out[:_MAX_WEB_SOURCES]

    @staticmethod
    def _force_h1(body, seed):
        """FU88 — the on-page H1 IS the user's seed, verbatim (H1 = the exact target prompt is the
        core retrieval design). Replace the body's first H1 line with the seed; if the body has no
        H1, prepend one. Deterministic — the model can't drift the visible heading."""
        s = (seed or "").strip()
        if not s or not (body or "").strip():
            return body
        if re.search(r"(?m)^#\s+\S", body):
            return re.sub(r"(?m)^#\s+[^\n]*", lambda m: "# " + s, body, count=1)
        return f"# {s}\n\n{body}"

    def _fetch_url(self, url):
        """Fetch a URL and return its stripped visible text ("" on failure). Shared by
        `_gather_evidence`'s pasted-source path and FU79 `finish_pending_blog`'s manual-link path."""
        txt = _extract_visible_text(_fetch_homepage(url))
        return (txt or "").strip()

    def _gather_evidence(self, brand, seed, source_urls=None, research_notes="",
                         use_web_search=False, reddit_thread=None):
        """Fetch real, citable evidence for the article and return a formatted EVIDENCE
        block string (or "" when nothing usable). Sources:
          - the subject brand's own site (always),
          - competitor sites (cached `competitor_domains`, else model-resolved),
          - any user-pasted source_urls (verbatim),
          - research_notes (user-provided).
        Each brand domain → homepage + a few key pages (/pricing,/features,/about),
        graceful on failure. A competitor page is kept only if the competitor's name
        appears in it (validates we hit the right site)."""
        b = brand or {}
        subject = (b.get("name") or "").strip()
        blocks = []   # {label, url, text}

        def _fetch(url):
            return self._fetch_url(url)

        # ----- subject + competitor domains -----
        targets = []  # (label, domain, validate)
        web_resolved_names = set()   # competitor names whose domain came from find_official_domain
        if b.get("domain_url"):
            targets.append((subject, b["domain_url"].strip(), False))
        else:
            print(f"[blog_gen] evidence: subject {subject!r} has NO domain_url — "
                  "no first-party source can be fetched", flush=True)
        try:
            cached = json.loads(b.get("competitor_domains") or "{}")
        except (json.JSONDecodeError, TypeError):
            cached = {}
        cached = cached if isinstance(cached, dict) else {}
        stored_domains = dict(cached)          # original (before model-resolution) — to detect new
        comp_names = _as_list(b.get("competitors"))
        seed_low = (seed or "").lower()

        def _in_seed(nm):
            nm = (nm or "").strip().lower()
            return bool(nm) and nm in seed_low

        # Resolve domains for: competitors missing a cached domain, PLUS any competitor
        # named in the seed (re-resolve even if cached — a cached value for a seed-named
        # brand is the most likely to be a stale wrong same-name guess, e.g. profound.com
        # market-research cached for the AI-search Profound). The seed param also extracts
        # brands the seed mentions that aren't stored competitors yet. Category-anchored so
        # a same-named company in a different industry isn't picked.
        to_resolve = [c for c in comp_names if (c not in cached) or _in_seed(c)]
        resolved = self._resolve_brand_domains(
            to_resolve, seed=seed, subject=subject, subject_category=b.get("category"),
            want_subject=True)   # FU199: learn the article's subject + its specialists here
        # Web-search-backed resolution for seed-named comparison brands: the training-
        # knowledge resolver tends to pick the famous SAME-NAME domain (e.g. profound.com)
        # for a niche brand; a live search finds the actual peer site (tryprofound.com).
        # Override the resolved domain for any brand that's a comparison target in the seed.
        if use_web_search:
            seed_brands = [n for n in (list(resolved.keys()) + comp_names)
                           if _in_seed(n) and (n or "").strip().lower() != subject.lower()]
            for n in dict.fromkeys(seed_brands):
                try:
                    official = self.claude.find_official_domain(
                        n, f"{b.get('category') or ''} {seed or ''}".strip())
                except Exception:
                    official = ""
                if official:
                    web_resolved_names.add(n)
                if official and resolved.get(n) != official:
                    print(f"[blog_gen] evidence: web-search resolved {n!r} -> {official} "
                          f"(was {resolved.get(n) or 'unresolved'})", flush=True)
                    resolved[n] = official
        if resolved:
            print("[blog_gen] evidence: resolved competitor domains: "
                  + ", ".join(f"{n}={d}" for n, d in resolved.items()), flush=True)
        # Seed-named / newly-extracted brands: fresh resolution WINS over any (stale) cache.
        # Stored competitors not in the seed: cache wins; resolution only fills a gap.
        for n, d in resolved.items():
            if _in_seed(n) or n not in comp_names:
                cached[n] = d
            else:
                cached.setdefault(n, d)
        # Comparison/seed brands first (prioritized), then the rest — deduped.
        seed_first = [n for n in resolved if (_in_seed(n) or n not in comp_names)
                      and n.strip().lower() != subject.lower()]
        ordered = []
        for cn in seed_first + comp_names:
            if cn not in ordered:
                ordered.append(cn)
        for cn in ordered:
            dom = (cached.get(cn) or "").strip()
            if dom:
                targets.append((cn, dom, True))
        # subject first, then seed/comparison brands, then stored competitors — capped
        targets = targets[:_MAX_EVIDENCE_BRANDS]

        # Topic terms for relevance validation: a competitor page must share some topical
        # signal with the subject's space, not just contain the brand name — so a same-named
        # off-topic site (profound.com market-research) is rejected in an AI-search compare.
        _stop = {"with", "from", "that", "this", "your", "what", "best", "vs", "versus",
                 "review", "reviews", "alternative", "alternatives", "compare", "comparison",
                 "platform", "tool", "tools", "software", "company", "solution", "solutions",
                 "pricing", "plan", "plans", "features", "free", "online", "service", "services"}
        _topic_src = f"{b.get('category') or ''} {seed or ''}".lower()
        topic_terms = {w for w in re.findall(r"[a-z]{4,}", _topic_src) if w not in _stop}
        for _nm in [subject] + comp_names:           # drop brand-name tokens themselves
            for w in re.findall(r"[a-z]{4,}", (_nm or "").lower()):
                topic_terms.discard(w)

        validated = {}   # competitor label -> bare domain that fetched + validated this run
        produced = set()  # labels (subject/competitor) that yielded ≥1 first-party block this run
        target_dom = {}   # label -> bare domain used (for the web-search fallback below)
        # Competitor paths: include product/platform pages so FEATURE facts are captured,
        # not just pricing (the gap that left competitor feature rows "not confirmed").
        _comp_paths = ("", "/pricing", "/features", "/product", "/platform",
                       "/how-it-works", "/testimonials", "/terms", "/license")
        # FU151 (B): PARALLEL-fetch every (target, path) URL up front — these ~24 HTTP round-trips are
        # independent and dominate evidence latency. The validation + block-ordering loop below is
        # UNCHANGED (it just reads the pre-fetched text), so output + [S#] order stay byte-identical.
        _fetch_urls = []
        for _lbl, _dm, _val in targets:
            _dm2 = re.sub(r"^https?://", "", _dm).rstrip("/")
            for _p in (_EVIDENCE_PATHS if not _val else _comp_paths):
                u = f"https://{_dm2}{_p}"
                if u not in _fetch_urls:
                    _fetch_urls.append(u)
        _fetched = {}
        if _fetch_urls:
            with ThreadPoolExecutor(max_workers=min(_BLOG_FETCH_WORKERS, len(_fetch_urls))) as _ex:
                for _u, _t in zip(_fetch_urls, _ex.map(self._fetch_url, _fetch_urls)):
                    _fetched[_u] = _t or ""
        for label, dom, validate in targets:
            dom = re.sub(r"^https?://", "", dom).rstrip("/")
            target_dom[label] = dom
            # Subject brand gets the full path set; competitors get the product-heavy set. (FU150 Case 2 —
            # the product-page PRICE is fetched path-agnostically by the FREE own-domain web-search in
            # _resolve_and_sync_key_facts, NOT by guessing product routes here.)
            paths = _EVIDENCE_PATHS if not validate else _comp_paths
            kept = 0
            for path in paths:
                txt = _fetched.get(f"https://{dom}{path}", "")
                if not txt:
                    continue
                if validate:
                    low = txt.lower()
                    if label.lower() not in low:
                        if path == "":
                            print(f"[blog_gen] evidence: {label} -> {dom} homepage fetched but "
                                  f"brand name NOT on page (wrong/parked domain?)", flush=True)
                        continue   # wrong/parked domain — skip rather than mis-cite
                    if label not in validated:
                        # First page that names this competitor must also be on-topic —
                        # rejects a same-named company in another industry. Once a domain
                        # passes here, its other pages are accepted by name alone. Match on
                        # whole words (a set intersection) — NOT substrings, or "search"
                        # would spuriously match "re-search" on a market-research site.
                        page_words = set(re.findall(r"[a-z]{4,}", low))
                        # Require ≥2 distinct topic-word hits (or all, if fewer terms
                        # exist) — a single generic word like "search" appears on a
                        # market-research site too, so one match isn't enough to prove
                        # it's the right same-named company.
                        need = min(2, len(topic_terms))
                        if topic_terms and len(topic_terms & page_words) < need:
                            print(f"[blog_gen] evidence: {label} -> {dom}{path} skipped — off-topic "
                                  f"({len(topic_terms & page_words)}/{need} topic hits); likely the "
                                  f"WRONG same-name domain", flush=True)
                            continue
                        validated[label] = dom
                blocks.append({"label": label, "url": f"https://{dom}{path}",
                               "text": txt[:_EVIDENCE_TEXT_CAP]})
                kept += 1
            if kept:
                produced.add(label)
            if validate:
                if label in validated:
                    print(f"[blog_gen] evidence: {label} -> {dom} OK ({kept} page(s) cited)", flush=True)
                else:
                    print(f"[blog_gen] evidence: {label} -> {dom} produced NO first-party evidence "
                          f"(name/topic validation failed) — will be name-only ('not confirmed')", flush=True)
            else:  # subject (validate=False) — log its outcome (was previously silent)
                if kept:
                    print(f"[blog_gen] evidence: subject {label} -> {dom} OK ({kept} page(s))", flush=True)
                else:
                    print(f"[blog_gen] evidence: subject {label} -> {dom} fetched NOTHING "
                          f"(blocked/empty) — web fallback {'on' if use_web_search else 'OFF'}", flush=True)

        # FIRST-PARTY web-search fallback (Follow-up 36): when a direct HTTP fetch returned
        # nothing (cloud-IP Cloudflare block), pull the brand's OWN facts from its OWN domain
        # via the server-side web_search tool (runs on Anthropic's infra, not the blocked IP).
        # Gated by use_web_search (paid). Scoped to the SUBJECT (its domain_url is trusted) and
        # to competitors whose domain came from find_official_domain (topic-anchored) — never a
        # cached/guessed competitor domain that failed validation (avoids citing a wrong site).
        if use_web_search:
            for label, dom, validate in targets:
                if label in produced:
                    continue
                if validate and label not in web_resolved_names:
                    continue
                dom2 = target_dom.get(label) or re.sub(r"^https?://", "", dom).rstrip("/")
                try:
                    facts = self.claude.fetch_site_facts(dom2, label, seed)
                except Exception as e:
                    print(f"[blog_gen] evidence: {label} -> site-facts fallback error: {e}", flush=True)
                    facts = ""
                if facts:
                    blocks.append({"label": label, "url": f"https://{dom2}",
                                   "text": facts[:_EVIDENCE_TEXT_CAP]})
                    produced.add(label)
                    if validate:           # web-resolved competitor that produced facts → persist its domain
                        validated.setdefault(label, dom2)
                    print(f"[blog_gen] evidence: {label} -> {dom2} site-facts via web search "
                          f"({len(facts)} chars) [direct fetch was blocked]", flush=True)
                else:
                    print(f"[blog_gen] evidence: {label} -> {dom2} site-facts fallback found nothing", flush=True)

        # Accumulate: persist competitor domains that were newly resolved AND validated this
        # run back onto the brand, so its competitor set grows (idempotent; validated only,
        # never a wrong guess). Best-effort — write-back must never break generation.
        new_doms = {k: v for k, v in validated.items() if stored_domains.get(k) != v}
        if new_doms and b.get("id") is not None:
            try:
                merged = {**stored_domains, **new_doms}
                names = _as_list(b.get("competitors"))
                for k in new_doms:
                    if k not in names:
                        names.append(k)
                self.db.update_brand(b["id"], competitor_domains=json.dumps(merged),
                                     competitors=json.dumps(names))
            except Exception as e:
                print(f"[blog_gen] competitor write-back skipped: {e}")

        # ----- user-pasted source URLs (verbatim, no validation) -----
        for u in (source_urls or []):
            u = str(u).strip()
            if not u:
                continue
            txt = _fetch(u)
            if txt:
                blocks.append({"label": f"provided · {_norm_domain(u) or u[:50]}", "url": u,
                               "text": txt[:_EVIDENCE_TEXT_CAP]})   # FU141: named, never a bare "provided source"

        notes = (research_notes or "").strip()
        if notes:
            blocks.append({"label": "research notes (user-provided)", "url": "",
                           "text": notes[:_EVIDENCE_TEXT_CAP]})

        # ----- live Reddit thread (community discussion: the brand's own live post +
        #       comments, incl. the brand comment) — cited with a community angle -----
        if isinstance(reddit_thread, dict) and (reddit_thread.get("text") or "").strip():
            sub = (reddit_thread.get("subreddit") or "").strip()
            blocks.append({
                "label": f"community discussion · Reddit thread{f' in r/{sub}' if sub else ''}",
                "url": (reddit_thread.get("url") or "").strip(),
                "text": reddit_thread["text"][:_EVIDENCE_TEXT_CAP],
            })

        # ----- optional: independent third-party sources via web search -----
        # Search the whole web but BLOCK the brands' own domains (subject + known competitor
        # domains) so results are genuinely independent (reviews/news/forums), not the brands'
        # own marketing. The brand's OWN testimonials still come from the first-party fetch
        # above, so nothing is lost when web search returns little.
        if use_web_search:
            comp_names = _as_list(b.get("competitors"))
            own = []
            if b.get("domain_url"):
                own.append(re.sub(r"^https?://", "", b["domain_url"].strip()).rstrip("/").split("/")[0])
            for dom in cached.values():
                d = re.sub(r"^https?://", "", str(dom or "").strip()).rstrip("/").split("/")[0]
                if d:
                    own.append(d)
            own = sorted(set(d for d in own if d))
            blocks.extend(self._gather_independent_sources(
                subject, comp_names, seed, b.get("category") or "", own))

        # FU141 — tag reviews OF the subject (`review ·`, machine-readable so the rules can key
        # on them) and CAP them at 2 per generation: review pileup (5 affiliate reviews of one
        # brand on a single page) was the recurring reviewer complaint. Generic — the brand name
        # is this generation's subject, the shape check is brand-agnostic.
        _subj_name = subject
        _own_dom = _norm_domain(b.get("domain_url") or "")
        _review_kept = 0
        _tagged = []
        for bl in blocks:
            lab = str(bl.get("label") or "")
            ttl = lab.split("·", 1)[-1].strip() if "·" in lab else lab
            if lab.lower().startswith(("third-party", "review")) and _is_subject_review(ttl, _subj_name):
                if _review_kept >= 2:
                    print(f"[blog_gen] review-cap: dropped excess subject review '{ttl[:60]}'", flush=True)
                    continue
                _review_kept += 1
                bl = dict(bl, label=f"review · {ttl}")
            _tagged.append(bl)
        blocks = _tagged
        # FU141 — AUTHORITY ORDER: number the evidence best-source-first (official → provided →
        # subject's own site → vendor pages → community/third-party → reviews) so the writer's
        # habit of citing the earliest support lands on the strongest source. Stable sort keeps
        # each tier's internal order.
        blocks.sort(key=lambda _bl: _evidence_tier(_bl, _subj_name, _own_dom))
        # Stash the structured blocks (in [S#] order) so _rebuild_sources can rebuild the
        # article's ## Sources authoritatively. Always set (even when empty) so a stale value
        # from a prior call on this instance can't leak in.
        self._evidence_blocks = list(blocks)
        if not blocks:
            return ""
        parts = ["EVIDENCE (the ONLY admissible support for factual claims — cite by [S#] and URL. "
                 "AUTHORITY-ORDERED: earlier sources are more authoritative — for any claim, cite the "
                 "EARLIEST source that supports it. Sources labeled 'review ·' are third-party reviews "
                 "OF the subject brand: never authoritative, never clinical/safety support, at most "
                 "color for program facts):"]
        for i, bl in enumerate(blocks, 1):
            src = f"{bl['label']}" + (f" — {bl['url']}" if bl["url"] else "")
            parts.append(f"[S{i}] {src}\n{bl['text']}")
        return "\n\n".join(parts)

    # FU138 — the punt ban is on MEANING, not wording: after the literal bans, the model
    # evaded with fresh phrasing ("Not specified in sourced facts", "data not present"). This
    # clause-level detector catches the data-unavailable FAMILY; _resolve_table_punts resolves
    # matches STRUCTURALLY (strip the clause, drop mostly-unsourced columns) instead of playing
    # phrase whack-a-mole.
    _PUNT_MEANING_RE = re.compile(
        r"\bnot\s+(?:specified|disclosed|stated|provided|listed|available|published|detailed|"
        r"mentioned|documented|confirmed|found)\b|"
        r"\bno\s+(?:data|information|details?|figures?)\b|"
        r"\bdata\s+not\s+(?:present|available|found)\b|"
        r"\binformation\s+not\s+(?:found|available)\b|"
        r"\bunclear\s+from\b|\bunknown\b|\bn/?a\b|\btbd\b|"
        r"\bnot\s+publicly\s+documented\b|\bvaries?\s+by\s+plan\b|"
        # FU200: the "No <x> found in public sources" line FU198 briefly permitted. It was my own
        # exception to this ban and it filled a third of a shipped table, so it is stripped like any
        # other punt — a regenerate cleans an existing body without a migration.
        r"\bno\s+[^|\n]{1,70}?\s+found\s+in\s+public\s+sources\b|"
        # FU204: the two phrasings a shipped blog used under its comparison table. "not retrievable"
        # is a RESEARCH-PROCESS word — it never appears in legitimate consumer prose, only when the
        # writer is narrating its own failed lookup.
        r"\bnot\s+retrievable\b|"
        r"\bcould\s+not\s+(?:be\s+)?(?:confirm|verif|retriev|sourc)(?:e[ds]?|ied|y)?\b",
        re.IGNORECASE)

    # FU204 — the PROSE half. `_PUNT_MEANING_RE` above governs table CELLS and already matched
    # "prices are not confirmed from a first-party source in the available evidence"; the sentence
    # still shipped because `_PUNT_SENT_RE` (the prose dropper) requires `not confirmed` IMMEDIATELY
    # followed by "in (the) sourced/available …" and the real sentence wedges five words in between.
    # This is deliberately NOT a blanket reuse of `_PUNT_MEANING_RE`: that pattern contains `unknown`
    # and `n/a`, which are legitimate consumer prose ("the cause of colic is unknown") — fine to strip
    # from a cell, wrong to delete a whole sentence for. So a generic "not <verb>" only counts as a
    # punt when a SOURCING-context word sits within ~70 chars of it; the research-process phrases
    # stand alone.
    _PUNT_PROSE_RE = re.compile(
        r"\bnot\s+(?:specified|disclosed|stated|provided|listed|available|published|documented|"
        r"confirmed|verified|found)\b[^.\n]{0,70}?\b(?:evidence|sources?|sourced|cited|public|"
        r"publicly|first[- ]party|available)\b|"
        r"\b(?:evidence|sources?|public|publicly|first[- ]party)\b[^.\n]{0,70}?\bnot\s+"
        r"(?:specified|disclosed|stated|provided|listed|available|published|documented|confirmed|"
        r"verified|found)\b|"
        r"\bnot\s+retrievable\b|"
        r"\bcould\s+not\s+(?:be\s+)?(?:confirm|verif|retriev|sourc)(?:e[ds]?|ied|y)?\b",
        re.IGNORECASE)

    _PUNT_CELL_RE = re.compile(
        r"^\s*(?:verify\b|check\b|consult\b|confirm\b|refer\s+to\b|visit\b|"
        # FU78: "See <site>/pricing/terms/license …" punt (covers pricing AND commercial-use/license cells)
        r"see\s+[^|]*?(?:pricing|plans?|terms|licen[sc]e|commercial|\.(?:com|io|ai|co|net|org))|"
        r"varies?\s+by\s+plan|not\s+publicly\s+documented)",
        re.IGNORECASE)
    _PUNT_SENT_RE = re.compile(
        r"[^.\n]*\b(?:(?:always\s+|be\s+sure\s+to\s+|please\s+)?(?:verify|confirm|check)\b[^.\n]*"
        r"\bbefore\s+(?:publishing|monetiz|you\s+publish)|always\s+verify|varies?\s+by\s+plan|"
        r"depends?\s+on\s+the\s+(?:specific\s+)?plan|not\s+publicly\s+documented|"
        # FU138: the "not specified/disclosed in (the) sourced/available facts/sources" prose family
        r"not\s+(?:specified|disclosed|stated|provided|confirmed)\s+in\s+(?:the\s+)?"
        r"(?:sourced|available|cited|provided)\s+(?:facts|sources|evidence|information|data)|"
        r"data\s+not\s+(?:present|available)|"
        # FU78: "See/refer to/visit/check <site> for (current) pricing / plans / terms / license / commercial"
        r"(?:see|refer\s+to|visit|check)\s+[^.\n]*?\bfor\b[^.\n]*?(?:pricing|prices?|plans?|current\s+plan|terms|licen[sc]e|commercial\s+use)|"
        r"(?:see|visit|check)\s+(?:the\s+)?[^.\n]*?(?:pricing|plans?|terms|licen[sc]e)\s+page)\b[^.\n]*\.",
        re.IGNORECASE)
    # FU78: same "see/visit/check … pricing/plans/terms/license …" pointer but URL-tolerant — a URL's internal
    # dots break the [^.\n] sentence class above, so this variant allows dots and uses a period-then-whitespace
    # (or EOL) as the real sentence boundary. Catches "See mubert.com/render/pricing for current plan pricing."
    _PUNT_URL_SENT_RE = re.compile(
        r"(?:^|(?<=[.\n]))\s*(?:see|visit|refer\s+to|check)\b[^\n]*?"
        r"\b(?:pricing|prices?|plans?|current\s+plan|terms|licen[sc]e|commercial\s+use)\b[^\n]*?(?:\.(?=\s)|\.$|(?=\n)|$)",
        re.IGNORECASE)

    # FU54 substance guard: section splitter (## / ### headings) + concrete-stat detector.
    _SECTION_RE = re.compile(r"(?im)^(#{2,3})[ \t]+(.+?)[ \t]*$")
    _STAT_RE = re.compile(r"\$?\d(?:[\d,]*\d)?(?:\.\d+)?%?")   # FU181: never end on a comma

    # FU55: the model narrating its OWN sourcing/editing decisions into the article — a broken comparison
    # row or an "addressed elsewhere" note. Never real content; scrubbed from the final body.
    _META_RE = re.compile(
        r"deduplicated\s+(?:above|below)|no\s+tool-specific\s+fresh\s+fact|per\s+sourcing\s+rules|"
        r"row(?:'?s)?\s+(?:is|are)\s+removed|removed\s+per\s+sourcing|not\s+a\s+direct\s+comparison\s+row|"
        r"addressed\s+in\s+the\s+.{0,40}?section\b.{0,30}?rather\s+than",
        re.IGNORECASE)

    # ── FU204 Change 6 — ONE price predicate, used by BOTH the ask and the re-check ───────────────
    # The FU161 flag-and-ask queued "current price" for a competitor with no confirmed price, and
    # FU189's re-check then deleted it, because that re-check asks "is this ENTITY sourced?" — always
    # true for a price-only item, which is by definition about a tool we DID source and are missing one
    # FACT from. Reproduced on the shipped blog: tool "Tommee Tippee", tokens ['tommee','tippee'], its
    # own cited block in the evidence → item dropped → the operator was never asked. Sharing the
    # predicate is what keeps the queue site and the re-check site from drifting apart again.
    @staticmethod
    def _blocks_naming(tool, blocks):
        """The evidence blocks that belong to `tool` — its own labelled blocks, plus any block whose
        text/url names it (the dim-rescue keeps those under a `third-party ·` label)."""
        _t = (tool or "").strip().lower()
        _toks = [x for x in _product_tokens(tool or "") if len(x) >= 3]
        out = []
        for b in (blocks or []):
            lbl = (b.get("label") or "").lower()
            blob = (lbl + " " + (b.get("text") or "") + " " + (b.get("url") or "")).lower()
            if (_t and _t in lbl) or (_toks and all(x in blob for x in _toks)):
                out.append(b)
        return out

    @staticmethod
    def _has_confirmed_price(blocks, dom):
        """True when one of `blocks` carries a price AND comes from the tool's OWN site or a
        reputable third-party domain — an affiliate/retail price does not count as confirmed."""
        dd = _norm_domain(dom or "")
        for b in (blocks or []):
            if not _PRICE_SIGNAL_RE.search(b.get("text") or ""):
                continue
            du = _norm_domain(b.get("url") or "")
            if (dd and du and (du == dd or du.endswith("." + dd))) or du in _THIRD_PARTY_DOMAINS:
                return True
        return False

    def _resolve_table_punts(self, body):
        """FU138 — STRUCTURAL resolution of data-unavailable table cells, in any phrasing:
        1. per cell, strip punt CLAUSES (";"/" — " separated) and keep any real remainder
           ("Not specified in sourced facts; billed separately [S7]" → "billed separately [S7]");
        2. DROP a whole column (never the first/name column, never a Source column) when ANY of its
           data cells is empty — FU204, the operator's rule: a comparison column answers for EVERY
           option or it does not exist. A blank cell does not read as "not found", it reads as "this
           product has none", which is worse than omitting the dimension. Measured on the two real
           tables in the repo: the Thyseed bottles table comes back 4x6 and the FU200 Osbornes table
           4x5, both 100% filled — the rule is strict but does not collapse a real comparison. It is
           also the LAST resort: the FU161 price ask (FU204 Change 6) asks the operator for the
           missing value FIRST, and this drop is what happens when they skip;
        2b. COLLAPSE GUARD — if that leaves ZERO comparison dimensions, drop the table BLOCK entirely
           and say so in the note. A name-only one-column table is broken output; the per-option prose
           carries the comparison instead;
        3. any leftover empty cell → "—". After (2) this is unreachable for a data column; it stays
           live for the one deliberate exception, an exempt Source column (provenance is not a
           comparison dimension, so it is never traded away). Counted into self._table_punt_note so
           the operator sees a toast warning instead of a silently thin table;
        4. FU186 — guarantee a BLANK LINE after the last row whenever the next line is not a table
           row. python-markdown's table extension keeps consuming NON-BLANK lines as rows, so a
           heading (and its paragraph) emitted right after the last row is swallowed INTO the table as
           junk cells — on a live blog that corrupted the comparison table AND hid a whole section.
           This method already parses every table, so it knows exactly where each one ends. Fixing it
           HERE rather than in the HTML render means the STORED markdown is correct for every consumer
           (the Markdown export, the Drive upload, the LinkedIn surfaces), not just one renderer."""
        if not body:
            return body
        self._table_punt_note = ""
        lines = body.split("\n")
        # locate contiguous table blocks
        out, i, dropped_cols, leftover, collapsed = [], 0, 0, 0, 0
        while i < len(lines):
            if not (lines[i].strip().startswith("|") and lines[i].count("|") >= 2):
                out.append(lines[i]); i += 1
                continue
            tbl = []
            while i < len(lines) and lines[i].strip().startswith("|") and lines[i].count("|") >= 2:
                tbl.append(lines[i]); i += 1
            rows = [[c.strip() for c in ln.strip().strip("|").split("|")] for ln in tbl]
            if len(rows) < 3:
                out.extend(tbl)
                if i < len(lines) and lines[i].strip():
                    out.append("")                 # FU186: never let the next line become a row
                continue
            header, sep, data = rows[0], rows[1], rows[2:]
            ncols = len(header)
            # 1. strip punt clauses per data cell
            for r in data:
                for ci in range(1, min(len(r), ncols)):
                    parts = re.split(r"\s*(?:;|—|--)\s*", r[ci])
                    kept = [p for p in parts if p and not self._PUNT_MEANING_RE.search(p)
                            and not self._PUNT_CELL_RE.match(p)]
                    r[ci] = "; ".join(kept).strip(" ;")
            # 2. drop ANY column with an empty cell (skip col 0 and any Source column)
            drop = set()
            for ci in range(1, ncols):
                if re.search(r"source", header[ci], re.I):
                    continue
                vals = [r[ci] if ci < len(r) else "" for r in data]
                empty = sum(1 for v in vals if not v.strip() or v.strip() in ("—", "-"))
                if data and empty:          # FU204: one gap is enough — see the docstring
                    drop.add(ci)
            # FU200: cap the width, keeping the best-evidenced dimensions. A Source column is never
            # dropped (it is exempt above and re-added here), and the first column is never touched.
            _keep = [ci for ci in range(1, ncols) if ci not in drop]
            if len(_keep) > _DIM_CAP:
                def _filled(ci):
                    return sum(1 for r in data
                               if ci < len(r) and r[ci].strip() and r[ci].strip() not in ("—", "-"))
                _src = [ci for ci in _keep if re.search(r"source", header[ci], re.I)]
                _rank = sorted((ci for ci in _keep if ci not in _src),
                               key=lambda ci: (-_filled(ci), ci))
                _survive = set(_src) | set(_rank[:max(0, _DIM_CAP - len(_src))])
                drop |= {ci for ci in _keep if ci not in _survive}
            # FU204 (2b) — COLLAPSE GUARD. With the any-empty rule a table whose every dimension has
            # a gap would come back as a name column alone (plus an exempt Source column), which is
            # broken output, not a comparison. Drop the whole block and say so; the per-option prose
            # carries it. Neither real fixture reaches this — it is a floor, not a path.
            _dims_left = [ci for ci in range(1, ncols)
                          if ci not in drop and not re.search(r"source", header[ci], re.I)]
            if not _dims_left:
                collapsed += 1
                if i < len(lines) and lines[i].strip():
                    out.append("")             # FU186: the next block must not glue to what is above
                continue                       # emit no rows at all for this table
            if drop:
                dropped_cols += len(drop)
                header = [c for ci, c in enumerate(header) if ci not in drop]
                sep = [c for ci, c in enumerate(sep) if ci not in drop]
                data = [[c for ci, c in enumerate(r) if ci not in drop] for r in data]
            # 3. leftover empties → "—" + count
            for r in data:
                for ci in range(1, len(r)):
                    if not r[ci].strip():
                        r[ci] = "—"; leftover += 1
            for row in [header, sep] + data:
                out.append("| " + " | ".join(row) + " |")
            if i < len(lines) and lines[i].strip():
                out.append("")                     # FU186: never let the next line become a row
        if dropped_cols or leftover or collapsed:
            bits = []
            if dropped_cols:
                bits.append(f"dropped {dropped_cols} unsourced column(s)")
            if collapsed:
                bits.append(f"removed {collapsed} table(s) with no dimension the whole field could answer")
            if leftover:
                bits.append(f"{leftover} cell(s) still unsourced")
            self._table_punt_note = ("comparison table: " + ", ".join(bits)
                                     + " — provide sources or regenerate")
            print(f"[blog_gen] table-punts: {self._table_punt_note}", flush=True)
        return "\n".join(out)

    def _scrub_punts(self, body):
        """FU47 guarantee: never SHIP a reader-directed 'go verify it yourself' cop-out (the fallback
        the model reaches for when it can't source a value). In a Markdown TABLE row, a cell that STARTS
        with a punt ("Verify on X's site", "Varies by plan", "Not publicly documented") → "—"; a
        standalone punt sentence in prose is dropped. This never invents a value (the anti-fabrication
        gate is intact) — it just stops surfacing the gap, as a backstop to the prompt + verify_claims
        rules. Belt-and-braces; runs at the top of _rebuild_sources so every path is covered."""
        if not body:
            return body
        lines = []
        for line in body.split("\n"):
            s = line.strip()
            if s.startswith("|") and s.count("|") >= 2:   # markdown table row
                cells = line.split("|")
                for i, c in enumerate(cells):
                    if self._PUNT_CELL_RE.match(c.strip() or ""):
                        cells[i] = " — "
                line = "|".join(cells)
            lines.append(line)
        body = "\n".join(lines)
        body = self._PUNT_SENT_RE.sub(" ", body)          # drop pure go-verify-yourself sentences
        body = self._PUNT_URL_SENT_RE.sub(" ", body)      # FU78: URL-bearing "see <site> for pricing" pointers
        # FU204: drop a whole SENTENCE that narrates our own failed lookup ("prices are not confirmed
        # from a first-party source in the available evidence", "… was not retrievable"). Same
        # sentence-shaped sub `_scrub_meta` already uses for edit-narration.
        body = re.sub(r"[^.\n]*(?:" + self._PUNT_PROSE_RE.pattern + r")[^.\n]*\.", " ", body,
                      flags=re.IGNORECASE)
        body = re.sub(r"[ \t]{2,}", " ", body)
        return body

    @staticmethod
    def _split_grouped_citations(body):
        r"""FU167: an open model sometimes writes GROUPED citations `[S1, S2, S3]`, which the single-marker
        `\[S\d+\]` renumber/drop pass in _rebuild_sources cannot see → the orphan markers survive un-renumbered
        (the broken `[S21-S29]` seen in a live compose). Split any grouped bracket into individual `[S#]`
        markers first. Matches `[S1, S2]`, `[S1,S2,S3]`, `[S1, 2, 3]`; leaves single `[S1]` untouched."""
        return re.sub(r"\[S\d+(?:\s*,\s*S?\d+)+\]",
                      lambda m: "".join(f"[S{n}]" for n in re.findall(r"\d+", m.group(0))), body or "")

    def _scrub_meta(self, body):
        """FU55: drop the model's edit-narration that leaked into the article — a comparison-table ROW
        or a blockquote/prose SENTENCE that explains a sourcing/editing decision ("… deduplicated above",
        "… row is removed per sourcing rules", "… addressed in the risk section … rather than a direct
        comparison row"). Never real content. Matches ONLY edit-narration phrasing, so real rows/sentences
        are untouched. Runs alongside _scrub_punts at the top of _rebuild_sources."""
        if not body:
            return body
        kept = []
        for line in body.split("\n"):
            s = line.strip()
            # a table row or blockquote line that is pure edit-narration → drop the whole line
            if (s.startswith("|") or s.startswith(">")) and self._META_RE.search(s):
                continue
            kept.append(line)
        body = "\n".join(kept)
        # a standalone prose sentence that narrates an edit → drop just that sentence
        body = re.sub(r"[^.\n]*(?:" + self._META_RE.pattern + r")[^.\n]*\.", " ", body,
                      flags=re.IGNORECASE)
        body = re.sub(r"[ \t]{2,}", " ", body)
        return body

    def _split_sections(self, body):
        """[(heading_text_lower, full_block)] for each ## / ### section (heading line + content up to
        the next ## / ### heading). Content before the first heading (e.g. the byline) is not a section."""
        body = body or ""
        out = []
        matches = list(self._SECTION_RE.finditer(body))
        for i, m in enumerate(matches):
            title = m.group(2).strip().strip("#").strip().lower()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            out.append((title, body[m.start():end].rstrip()))
        return out

    def _restore_dropped_sections(self, draft, revised):
        """FU54 substance guard: if the verify/reconcile rewrite DROPPED a whole ## / ### section that was
        in the draft, re-append that section's ORIGINAL content (before ## Sources) so substantive prose —
        a policy section, a checklist — can't silently vanish. Restores SECTIONS only; it never re-adds an
        individual dropped table-cell value (those stay dropped — anti-fabrication intact). Heading match is
        exact/lowercased, so it targets outright DELETION (a renamed heading is treated as dropped and the
        original restored — content-preservation over cosmetic dedup). Skips Sources/FAQ (handled elsewhere).
        No-op when nothing was dropped."""
        draft = draft or ""
        revised = revised or ""
        if not draft.strip() or not revised.strip():
            return revised or draft
        rev_titles = {t for t, _ in self._split_sections(revised)}
        restored = [block for title, block in self._split_sections(draft)
                    if title and title not in ("sources", "faq") and title not in rev_titles]
        if not restored:
            return revised
        for block in restored:
            head = block.splitlines()[0].strip() if block.splitlines() else "?"
            print(f"[blog_gen] substance-guard: restored dropped section {head!r}", flush=True)
        add = "\n\n" + "\n\n".join(restored).strip() + "\n"
        m = re.search(r"(?im)^[ \t]*#{2,3}[ \t]+Sources\b", revised)
        if m:
            return revised[:m.start()].rstrip() + add + "\n" + revised[m.start():]
        return revised.rstrip() + add

    def _dropped_stats(self, draft, revised):
        """Concrete stats (multi-digit numbers / percentages / counts) present in the draft but MISSING
        from the revised body — a log/test SIGNAL (not auto-restored; mid-paragraph insertion is fragile)."""
        def stats(t):
            out = set()
            for s in self._STAT_RE.findall(t or ""):
                if len(re.sub(r"\D", "", s)) >= 3:   # keep the big citation-magnet numbers (8,600 / 51,000)
                    out.add(s)
            return out
        return sorted(stats(draft) - stats(revised))

    def _rebuild_sources(self, body):
        """Deterministically rebuild the article's ## Sources from the evidence map captured by
        the last `_gather_evidence` call. Renumbers the [S#] markers the model actually used to a
        contiguous [S1..Sn] (in order of first appearance), rewrites them inline, drops any
        out-of-range / hallucinated index, and replaces the model's ## Sources with an
        AUTHORITATIVE list (label — URL straight from the evidence, not model-typed). This is
        what guarantees every cited source — brand site, competitor site, Reddit, third-party —
        appears with the right URL and no numbering gaps. No-op when there's no evidence or
        nothing was cited."""
        body = self._resolve_table_punts(body or "")   # FU138: structural table punt resolution
        body = self._scrub_punts(body)         # FU47: kill reader-directed punts on every path
        body = self._scrub_meta(body)          # FU55: drop leaked edit-narration (broken table rows/notes)
        body = self._split_grouped_citations(body)   # FU167: [S1, S2, S3] → [S1][S2][S3] so the renumber sees them
        blocks = getattr(self, "_evidence_blocks", None) or []
        if not body or not blocks:
            return body
        # Drop the model's own ## / ### Sources section (we rebuild it).
        prose = re.split(r"(?im)^[ \t]*#{2,3}[ \t]+Sources\b.*", body, maxsplit=1)[0].rstrip()
        used = []   # cited indices in order of first appearance, in range only
        for m in re.finditer(r"\[S(\d+)\]", prose):
            idx = int(m.group(1))
            if 1 <= idx <= len(blocks) and idx not in used:
                used.append(idx)
        # FU46/FU54 backstop: a deliberately-attached community/Reddit thread OR an authoritative
        # "official ·" primary source must ALWAYS be listed in ## Sources, even if the model didn't cite it
        # inline — the highest-authority source can never be silently dropped. Force such blocks into the
        # render set so they get a Sources entry. Other uncited blocks are still dropped as before.
        def _is_forced(bl):
            lab = (bl.get("label") or "").lower()
            url = (bl.get("url") or "").lower()
            return (lab.startswith("community discussion") or lab.startswith("official ·")
                    or "reddit.com" in url)
        forced = [i + 1 for i, bl in enumerate(blocks) if _is_forced(bl) and (i + 1) not in used]
        render = used + forced
        if not render:
            return body   # nothing valid cited and no community block — leave the body untouched
        # FU200 — two evidence blocks can hold the SAME page (a Chambers profile reached by two
        # different briefs), and the article then cites it as two numbers. Collapse on the normalised
        # URL: the later index re-points at the earlier number and gets no Sources line of its own.
        remap, render_out, _byurl = {}, [], {}
        for old in render:
            _u = (blocks[old - 1].get("url") or "").strip().split("?")[0].rstrip("/").lower()
            if _u and _u in _byurl:
                remap[old] = _byurl[_u]
                continue
            render_out.append(old)
            remap[old] = len(render_out)
            if _u:
                _byurl[_u] = len(render_out)
        render = render_out
        prose = re.sub(r"\[S(\d+)\]",
                       lambda m: (f"[S{remap[int(m.group(1))]}]" if int(m.group(1)) in remap else ""),
                       prose)
        lines = ["", "## Sources", ""]
        for old in render:
            bl = blocks[old - 1]
            label = (bl.get("label") or "source").strip()
            url = (bl.get("url") or "").strip()
            # <url> autolink → renders as a clickable <a> in the HTML/`.md` export (bare URLs don't).
            lines.append(f"- [S{remap[old]}] {label}" + (f" — <{url}>" if url else ""))
        return prose.rstrip() + "\n" + "\n".join(lines) + "\n"

    # ----------------------------------------------------------- keyword sourcing
    def _filter_relevant(self, seed, pairs, threshold=0.30, top=12):
        """Keep only fan-out (query, region) pairs relevant to the seed, ranked by
        cosine similarity. No-op (returns the input, capped) when embeddings are
        unavailable so it never blocks keyword suggestion."""
        if not pairs:
            return []
        sv = self._pg._embed_texts([seed])
        qv = self._pg._embed_texts([q for q, _ in pairs])
        if not sv or not qv:
            return pairs[:top]
        s = sv[0]
        scored = [(self._pg._cosine(s, v), p) for v, p in zip(qv, pairs)]
        scored = [x for x in scored if x[0] >= threshold]
        scored.sort(key=lambda x: x[0], reverse=True)
        return [p for _, p in scored[:top]]

    def _expand(self, name, seed, block, existing=None, n=8):
        """LLM expansion: AI-style query-variants around the seed the brand can answer."""
        ex = ""
        if existing:
            ex = ("\nAlready have these (don't repeat):\n"
                  + "\n".join(f"- {e}" for e in existing[:30]))
        prompt = f"""A person is researching: "{seed}"
We are writing ONE article for {name} that should be CITED by AI answer engines
(ChatGPT / Perplexity / Gemini) for this topic. List the distinct QUESTION-style search
queries a real person would ask an AI around this topic that {name} could credibly be the
answer to.

BRAND:
{block}
{ex}
Rules: natural question phrasings; vary the angle (informational, "best X for Y",
comparison, constraint/qualifier, long-tail); {n}-{n + 4} items; deduped; do NOT put the
brand name in the query text.

Return JSON only: {{"queries": ["...", "..."]}}"""
        res = self.claude.call(prompt, max_tokens=800, temperature=0.7)
        return _as_list((res or {}).get("queries"))

    def suggest_keywords(self, brand, seed, manual=None):
        """Ranked, editable query-variant set that will drive the article. Merges, in
        priority order: manual keywords → reused AI-Search fan-out (filtered to the seed)
        → fresh LLM expansion. Deduped. Returns [{query, source, region}]."""
        seed = (seed or "").strip()
        if not seed:
            return []
        name, _url, block = self._brand_block(brand)
        out, seen = [], set()

        def _add(q, source, region=""):
            q = (q or "").strip()
            k = q.lower()
            if q and k not in seen:
                seen.add(k)
                out.append({"query": q, "source": source, "region": region})

        # 1) manual keywords first (always kept, priority)
        for m in _as_list(manual):
            _add(m, "manual")

        # 2) reused fan-out: the brand's existing AI-Search cluster rewrites + variants
        #    (the same prompts we target on Reddit), filtered to this seed.
        fan = []
        try:
            bid = (brand or {}).get("id")
            for cl in self.db.get_ai_search_clusters_for_brand(bid):
                for rw in self.db.normalize_rewrites(cl.get("rewrites_json")):
                    region = rw.get("region") or ""
                    if rw.get("query"):
                        fan.append((rw["query"], region))
                    for v in (rw.get("variants") or []):
                        fan.append((v, region))
        except Exception as e:
            print(f"[blog_gen] fan-out read failed: {e}")
        for q, region in self._filter_relevant(seed, [(q, r) for q, r in fan if str(q).strip()]):
            _add(q, "fanout", region)

        # 3) fresh LLM expansion for the remaining space
        for q in self._expand(name, seed, block, existing=[o["query"] for o in out]):
            _add(q, "expanded")

        return out

    # ------------------------------------------------------------------- article
    def generate_article(self, brand, seed, extra_keywords=None, evidence="", geo="",
                         sibling_titles=None, qualifier="", internal_links=False,
                         link_targets=None, ymyl=None, key_facts=None, key_facts_products=None):
        """GEO-first first-party article. `extra_keywords` (the reviewed query set) are
        the target queries the article MUST answer (each becomes a question heading + FAQ
        entry) and are merged into the returned keywords. `evidence` is the formatted
        EVIDENCE block from `_gather_evidence` — when present, factual claims must cite it.
        `geo` (FU90): the operator's explicit geography (wins over seed auto-detect) — makes the
        FU89 differentiation block concrete. `sibling_titles` (FU90): the brand's OTHER blog titles,
        so geo variants differentiate instead of converging into near-duplicates.
        Returns {title, meta_description, keywords, body_markdown} (body carries the
        brand byline when supplied) or None on failure. Does NOT run the claims pass."""
        seed = (seed or "").strip()
        if not seed:
            return None
        name, url, block = self._brand_block(brand)
        kws = _as_list(extra_keywords)
        kw_block = ""
        if kws:
            kw_block = ("\nTARGET QUERIES — the article MUST answer EACH of these. Make each a "
                        "question-shaped H2/H3 with a concise answer, and an FAQ entry:\n"
                        + "\n".join(f"- {k}" for k in kws) + "\n")
        evidence_block = f"\n{evidence}\n" if (evidence or "").strip() else ""
        kf_block = _canonical_facts_block(name, key_facts, key_facts_products)   # FU150 (#4): cluster-synced
        link = f" Link to {url} where it reads naturally." if url else ""
        # FU114 — opt-in internal linking + meta title. OFF → both strings empty → the
        # prompt is BYTE-IDENTICAL to today (the user's hard requirement).
        il_block, il_schema = "", ""
        if internal_links:
            _targets = [t for t in (link_targets or []) if (t.get("url") or "").strip()]
            _tlist = "\n".join(
                f'  - {t["url"].strip()}' + (f'   ({t["label"]})' if (t.get("label") or "").strip() else "")
                for t in _targets[:10])
            il_block = f"""
INTERNAL LINKS + META TITLE (opt-in for this article):
Weave 3-6 Markdown links into the body, ONLY from this VERIFIED list of {name}'s own pages:
{_tlist or "  (none available — include NO internal links)"}
LINKING RULES (quality/extractability must stay intact):
  1. NEVER in the FIRST SENTENCE under ANY heading — not the Quick answer, not any H2/H3's
     answer sentence, not an FAQ answer's first sentence. The liftable answer-chunks stay
     clean and self-contained.
  2. Anchor = the natural DESCRIPTIVE words already in the sentence ("US-specific HRIS
     compliance criteria"), never "click here" / "read more", and never a bare brand name
     as the anchor — the anchor text is the routing signal.
  3. 3-6 links total, each placed where that page GENUINELY serves the reader — use fewer
     if nothing fits naturally; never stack links in one paragraph.
  4. Link only genuinely RELATED pages: the sibling article covering a subtopic you touch,
     the case-studies page a result claim comes from, the pricing page a price claim cites.
     A link that doesn't serve the reader hurts.
  5. CLAIM ATTRIBUTION: when the body states a first-party claim sourced from one of these
     pages (a price, a case-study result, a feature), make THAT claim's descriptive words
     the anchor — internal link and attribution in one.
  6. Links must NEVER alter, strengthen, or reword a claim to justify their placement.
  7. LINK HONESTY: never substitute the homepage or an /about page for a promised SPECIFIC
     page ("our muscle-preservation guide" must link that guide, not the homepage). If the
     exact page is not in the VERIFIED list, do not link that phrase at all.
ALSO return "meta_title": an SEO <title> tag under 60 characters that carries the target
query's core phrasing (add {name} only where natural), never clickbait, and never diverging
in meaning from the H1. Make "meta_description" LEAD with the direct-answer phrasing (the
extractable answer), still under 160 chars.
"""
            il_schema = '\n  "meta_title": "SEO title tag, under 60 chars",'
        # FU90 — resolved geography: explicit operator input WINS; else deterministic seed detection.
        rgeo = (geo or "").strip() or _seed_geo(seed)
        geo_line = (f"  - GEOGRAPHY FOR THIS PAGE: {rgeo} (operator-specified or detected from the "
                    f"seed) — apply everything in this section to it.\n" if rgeo else "")
        # FU93 — resolved qualifier (same rule: explicit input wins, else trailing-pattern detection).
        rqual = (qualifier or "").strip() or _seed_qualifier(seed)
        qual_line = (f"  - QUALIFIER FOR THIS PAGE: {rqual} (operator-specified or detected from the "
                     f"seed) — this is the page's reason to exist; apply everything in this section "
                     f"to it.\n" if rqual else "")
        # FU133 — YMYL AUTHORITY: a clinical/regulatory page's value IS its authority; claims of
        # that class must cite the official sources in the EVIDENCE (labeled "official ·"), never
        # vendor/affiliate/review pages. Inert ("") for non-YMYL blogs.
        ymyl_line = ""
        if ymyl:
            ymyl_line = (
                "  - YMYL AUTHORITY (this is a " + str(ymyl) + " page — non-negotiable): every "
                "CLINICAL/REGULATORY claim — contraindications, boxed warnings, diagnostic "
                "thresholds, dosing or monitoring standards, eligibility/coverage rules — MUST "
                "cite an \"official ·\" EVIDENCE source [S#] (regulator label / prescribing "
                "information / professional-association guideline). Vendor, affiliate, and review "
                "sources may ONLY support program logistics (pricing, what's bundled, delivery) — "
                "never a clinical fact. Aim for at least 2 DISTINCT official citations. If a "
                "clinical specific has NO official source in the EVIDENCE, state it generally and "
                "attribute it (\"per the drug's prescribing information\") — never invent the "
                "specific and never cite a marketing page for it. EXTRACT ZONE = CLINICAL "
                "SUBSTANCE: logistics perks (free/discreet shipping, discounts, bundles) must NOT "
                "appear in the Quick answer, the opening paragraph, or the meta_description — on a "
                "prescription-product page they read as gray-market signals there; keep them in a "
                "logistics/pricing section. QUICK-ANSWER CITATIONS: every [S#] cited in the "
                "Quick answer must be an \"official ·\" source or " + name + "'s own page — never "
                "a review/affiliate/third-party source for the lead claim. PRODUCT MATCH: a "
                "clinical claim about a specific product/drug must cite a source documenting THAT "
                "product — never a different molecule's/product's label, however closely related; "
                "in a comparison sentence, cite each side's own source.\n")
        # FU140 — the model's training prior lags the calendar: pages shipped titled "(2025)"
        # in 2026. State the current year explicitly; the deterministic bump in
        # _finalize_article backstops the meta fields.
        import datetime as _dt140
        year_line = (f"  - CURRENT YEAR: {_dt140.datetime.utcnow().year}. Any year in the meta "
                     f"title, headings, or forward-looking copy MUST be the current year — never "
                     f"date the page with an earlier year unless the sentence is explicitly about "
                     f"a past event (a founding date, a past ruling).\n")
        # FU199 — subject fit decides WHO is compared, not position on the operator's list. Empty
        # (and so byte-identical) unless this article's subject is narrower than the brand's category.
        _sfit_p = getattr(self, "_subject_phrase", "") or ""
        _sfit = ""
        if _sfit_p:
            _sfit = (f"    SUBJECT FIT OVERRIDES LIST POSITION: this article is about {_sfit_p}, so at "
                     f"EVERY step above a name qualifies ONLY if it genuinely does {_sfit_p}. Do NOT "
                     f"name a curated competitor that has no standing in {_sfit_p} merely because it "
                     f"is on the operator's list — a real specialist in {_sfit_p} (including one under "
                     f"\"Specialists in …\" in the brand context) outranks it. A comparison of "
                     f"providers that do not do {_sfit_p} answers nobody's question.\n")
        # FU90/FU135 — sibling context: differentiation AND cluster-consistent positioning.
        # Accepts bare title strings (legacy) or {title, meta_description} digests.
        # FU197: the caller now supplies PUBLISHED siblings only, each carrying the live URL, so a
        # reference can always be checked against a page that really exists.
        sibs, _sib_urls = [], set()
        for t in (sibling_titles or [])[:10]:
            if isinstance(t, dict):
                _ttl = str(t.get("title") or "").strip()
                _md = str(t.get("meta_description") or "").strip()[:160]
                _su = str(t.get("url") or "").strip()
                if _ttl:
                    sibs.append(f"{_ttl}" + (f" — {_md}" if _md else "")
                                + (f"\n      URL: {_su}" if _su else ""))
                    if _su:
                        _sib_urls.add(_su.split("?")[0].rstrip("/").lower())
            else:
                _ttl = str(t or "").strip()
                if _ttl:
                    sibs.append(_ttl)
        self._sibling_urls = _sib_urls   # read by _finalize_article's self-reference check
        sibling_block = ""
        if sibs:
            sibling_block = (f"\nOTHER PAGES OF THIS BRAND THAT ARE ALREADY LIVE (each shown with the "
                             f"URL a reader can actually open) — keep the brand's POSITIONING, claims "
                             f"and pricing CONSISTENT with them (never contradict them), and do NOT "
                             f"duplicate their content: THIS page must add its own "
                             f"geography/qualifier/audience substance.\n"
                             + "\n".join(f"  - {t}" for t in sibs) + "\n"
                             f"  REFERENCE RULE (hard): you may point the reader at one of the pages "
                             f"LISTED ABOVE, and only by the exact URL shown. NEVER state or imply that "
                             f"{name} has published a guide, article, resource or breakdown that is not "
                             f"in that list — no \"our guide on X\", no \"{name} has published a "
                             f"dedicated guide on ...\", no \"refer to {name}'s published guidance "
                             f"on ...\". Any such page does not exist, and promising one sends the "
                             f"reader and the answer engine nowhere.\n")
        prompt = f"""You are writing a FIRST-PARTY article published on {name}'s own site. The ONLY
goal is for AI answer engines (ChatGPT, Perplexity, Gemini, Google AI Overviews) to RETRIEVE
and CITE this page when someone asks about the seed topic, AND for that answer to name {name}.
Optimize for EXTRACTION & CITATION, not for sales copy.

SEED TOPIC (what the reader is asking): {seed}
{kw_block}{sibling_block}
BRAND (first-party — you MAY name and recommend {name}):
{block}
{kf_block}{evidence_block}
EVIDENCE RULE (intent-agnostic — applies to EVERY sentence, comparison blog or not):
  - You may NAME any brand freely (listing it as an option / alternative needs no source).
  - But any SPECIFIC factual claim about a named brand — features, pricing, numbers, "does / does
    NOT do X", superiority ("stronger / better / more complete") — MUST be grounded in the EVIDENCE
    above and cite it inline as [S#]. This applies to {name}'s OWN claims too.
  - {name}'s OWN FACTS ARE FIRST-PARTY ONLY. Any fact about {name} — pricing, plans, features, terms,
    policies, financing, shipping, return/warranty, locations, products carried, contact details, any
    claim about what {name} does — may be sourced ONLY from {name}'s OWN website or content {name}
    itself published (its own site/blog/docs/press releases; the EVIDENCE source labeled "{name}").
    NEVER cite a third-party / independent / review / analyst source for a fact ABOUT {name} — even if
    one is in the EVIDENCE. If a {name} fact isn't on {name}'s own site, OMIT it or state it only as
    {name}'s own positioning ("on our site, we …") — never source a {name} fact to a third party. Every
    {name} specific you DO state MUST cite {name}'s own-site [S#], and {name}'s site MUST appear in
    "## Sources" (don't let {name}'s own specifics ride uncited just because it's a first-party article).
  - A BRAND-SPECIFIC CLAIM MUST CITE THAT BRAND'S OWN SOURCE. A specific factual claim about a NAMED
    brand must cite a source that is that brand's OWN page, or a page that explicitly names that brand
    and states that fact about it. NEVER cite another brand's page, a listing for a DIFFERENT product,
    or a general authority/guideline page as the source for a brand-specific fact. If no source in the
    EVIDENCE supports the claim FOR THAT BRAND, drop the specific and say only what is sourced.
  - WHAT A SOURCE IS GOOD FOR. A source labeled "retail · …" (a marketplace listing) or "review · …"
    (a star-rating page) may support a PRICE or AVAILABILITY and nothing else — it is the seller's own
    copy or aggregated customer sentiment. A MATERIAL, safety, certification, temperature/performance
    or efficacy claim ("clinically proven", "BPA-free", "heat-resistant to 180C", "FDA-registered")
    must cite the brand's OWN page or an official / authority source. Never let a rating page be the
    source for what a product is MADE OF or what it DOES.
  - NEVER CITE ANYTHING NEGATIVE ABOUT {name}. Do not cite, quote, link, or reference any source that
    says anything negative or critical about {name} (complaints, lawsuits, "problems with", bad
    reviews, "stay away", etc.). If a gathered source contains a negative statement about {name}, do
    not use it at all — omit it entirely.
  - If the evidence does NOT support a specific claim about some brand, DO NOT assert it and DO NOT
    hedge with "not publicly documented" — either omit it, or state it only as {name}'s own
    positioning ("on our site, we …"). Never assert an unsourced fact about a competitor.
  - JOB LISTINGS ARE NOT EVIDENCE: never cite a job posting, recruitment ad, careers page or
    salary listing as support for ANY claim — a company hiring a credit analyst proves nothing
    about its services. If such a page is in the EVIDENCE, ignore it entirely.
  - DESCRIBE SOURCES HONESTLY (no authority laundering): NEVER describe a "third-party ·" source
    with authority-inflating framing — "an independent audit found", "an independent pricing
    audit/analysis/report", "independently verified" — reviews and affiliate write-ups are NOT
    audits. Attribute them plainly by name ("a review by <site> lists …", "per <site>'s review")
    or cite the vendor's OWN page for the number instead. Only a source labeled "official ·" may
    be framed as authoritative/official.
  - THE PUNT BAN IS ON MEANING, NOT WORDING: any cell, clause or sentence whose meaning is "this
    data is not available/specified/disclosed/found" is a punt IN ANY PHRASING ("Not specified in
    sourced facts", "data not present", "no data", …). The resolution is STRUCTURAL, never verbal:
    state the sourced value, or drop the COLUMN (most options lack the data) or the ROW (one option
    lacks everything). Re-wording a data-unavailable phrase is still a violation.
  - NEVER PUNT TO THE READER. Do NOT write "verify on X's site", "check their site", "consult the
    terms", "varies by plan — always verify", "always verify … before publishing", "not publicly
    documented", or ANY go-check-it-yourself instruction — that is a cop-out, not content, and it
    names the exact thing you couldn't source. Instead, when you lack a sourced value:
      • COMPARISON TABLE cell → put "—" (em dash), or a DIFFERENT attribute you DO have sourced for
        that tool. Never fill a cell with a "verify/check their site" sentence.
      • A whole COLUMN you can't source for most tools → DROP the column and compare on dimensions you
        CAN source. Prefer to build the table from tools + dimensions you actually have evidence for;
        do not add a column you'll only be able to punt on.
      • In PROSE → just leave the unsourced point out and say something substantive you CAN support
        instead. Never surface the gap or tell the reader to go find it.
  - END the body with a "## Sources" section listing every [S#] you cited (label + URL). Omit the
    section only if you cited nothing.
  - If no EVIDENCE is provided, keep claims to {name}'s own brand context and name competitors
    without asserting specifics about them.
  - TESTIMONIALS: you may include at most ONE short customer quote ONLY if it appears in the
    EVIDENCE — attribute it and cite [S#]. Never fabricate a testimonial and don't paste long blocks.
  - COMMUNITY SOURCE: if the EVIDENCE includes a "community discussion" (a real Reddit thread with the
    post + its comments), you MUST cite it AT LEAST ONCE as real-world SOCIAL PROOF with a NATURAL
    community framing — e.g. "in a r/<sub> thread, contractors weighing nationwide options pointed to …",
    "pros on Reddit discussing this flagged …" — and cite it [S#]. It was deliberately attached, so the
    article has to reference it. Include a short VERBATIM quote ONLY if the thread contains a SUBSTANTIVE,
    on-topic remark; if it does not, reference the discussion generally (paraphrase the sentiment) WITHOUT
    quoting — NEVER quote a vacuous or off-topic throwaway line (e.g. "so music is good sometimes") just to
    have a quote. At most ONE short quoted line, attributed; NEVER invent comments beyond what's in the
    thread. Frame it as community discussion, not a raw link.
  - PREFER INDEPENDENT SOURCES — for COMPETITOR and TOPIC facts ONLY, never for {name}: the EVIDENCE may
    include "third-party ·" sources (independent reviews, news/funding, analyst/pricing — NOT the brands'
    own sites). For facts about COMPETITORS or the general TOPIC/category, LEAD with them and aim to cite
    at least 2 DISTINCT independent sources where available (a review + a news/funding item + an analyst/
    pricing reference). This does NOT apply to facts about {name} — those stay FIRST-PARTY ONLY per the
    rule above; never move a {name} fact onto a third-party source to look "independent". (Still cite ONLY
    what is actually in the EVIDENCE — never invent a source.)

EXTRACTABILITY IS THE CORE OBJECTIVE — it OVERRIDES every other choice below. If any format or
title decision would make the page harder for an AI to extract a direct answer from, drop it and
keep the extractable structure. When in doubt, choose the more extractable option.

FIRST, classify the seed's INTENT, then LEAD the body with the dominant block that matches it (the
backbone below is still mandatory in every case):
  - comparison ("best X", "X vs Y", "alternative to Y") → lead with a COMPARISON TABLE + a short
    per-option verdict.
  - how-to ("how do I", "how to", "steps to") → lead with NUMBERED STEPS.
  - definitional / does-it-work ("what is", "does X work") → lead with a crisp DEFINITION sentence
    then concept Q&A (cite a primary source for any efficacy claim).
  - evaluation ("is X worth it", "is X legit / safe") → lead with a CRITERIA CHECKLIST + evidence.
  - otherwise → the default question-and-answer structure below.
Pick exactly ONE dominant block. The dominant block MUST itself be extractable — a real Markdown
numbered list / table / checklist / definition sentence, never narrative prose that buries the answer.

SUBSTANCE BEFORE CREDENTIALS (FU198 — what makes the page worth citing):
  - FIRST identify the defining MECHANICS of this article's subject: the decisions, procedures, rules,
    constraints and trade-offs that actually DETERMINE the outcome for someone dealing with it. Then
    make those mechanics the MAJORITY of the article's substance.
  - Rankings, awards, accreditations, office counts, team size and years in business are COMMODITY
    content — every competitor can list them, and an answer engine gains nothing by citing them. They
    may support a recommendation; they must NEVER stand in PLACE of a mechanic, and a section that is
    only credentials is a wasted section.
  - This is vertical-neutral — the mechanics are whatever genuinely governs THIS subject. Across
    industries they look like: the procedural rule that decides which forum or authority hears a
    matter and what happens if you act late; the migration, integration or data constraint that
    decides which platform actually fits; the eligibility, monitoring or aftercare rule that decides
    who qualifies and what happens next; the permitting, sequencing or lead-time constraint that
    decides what a job really costs. Work out the equivalent for this subject and cover it concretely.
  - A mechanic is covered when the page says what it IS, what it CHANGES about the reader's decision,
    and what the reader should DO about it — not merely that it exists.

GEOGRAPHY / QUALIFIER DIFFERENTIATION (FU89 — a variant page must EARN its existence):
{geo_line}{qual_line}{ymyl_line}{year_line}  - DETECT whether the seed names a GEOGRAPHY or jurisdiction ("US", "set up in the US", "UK",
    "Australia", a state or city) or another strong qualifier (a purchasing mechanism like "with
    financing", a company size, an industry). If it does NOT (and no geography or qualifier is stated
    above), SKIP this whole section — it is inert for a generic seed.
  - When one IS present, this page is that VARIANT of the topic, and it competes with the generic page:
    it must contain substance the generic page cannot have, or it is a duplicate.
  - ANTI-DOORWAY (hard rule): NEVER produce the generic answer with the geography/qualifier inserted
    into the headings and topic sentences — that is a doorway page (search engines devalue it, and it
    cannibalizes the generic page instead of adding coverage). Every heading that PROMISES the
    geography/qualifier must DELIVER genuinely specific substance directly under it.
  - REQUIRED SUBSTANCE — at least 2-3 sections (or major section-parts) that are ONLY true for this
    geography/qualifier, i.e. what is actually DIFFERENT about the topic THERE:
      • the applicable REGULATORY / COMPLIANCE regime, NAMED concretely — MULTIPLE named
        provisions/rules, EACH with its ACTUAL stated implication for the topic (what it CHANGES
        about how the reader should act — e.g. for US HR software: multi-state payroll tax,
        W-2/1099 handling, ACA reporting, I-9 verification, state leave-law variance as evaluation
        criteria; the equivalents for whatever the topic and geography actually are). NEVER a single
        one-sentence nod. The geography/qualifier sections together should carry roughly 250+ words
        of substance that is ONLY true for this variant;
      • local standards / certifications buyers there expect;
      • which of the compared options are native / strong / available in that geography — ONLY from the
        EVIDENCE, never an invented vendor claim. When the EVIDENCE has nothing geo-specific for an
        option, use and cite its GENERAL sourced facts as usual and keep the WRITING geo-focused (apply
        the local criteria/framing to them) — NEVER write hedge language like "unverified", "coverage
        unknown", or "not confirmed for this region" in the body;
      • geography-specific EVALUATION CRITERIA a local buyer should apply;
      • when the variant qualifier is a PURCHASING/COMMERCIAL mechanism (financing, leasing, free
        shipping, warranty, tax treatment, …), the page's core substance must EXPLAIN THE MECHANISM
        ITSELF for this product category: the main options/structures (e.g. lease vs. loan for
        equipment), what providers/lenders actually evaluate, typical terms/ranges, and the governing
        tax/regulatory angle (e.g. the relevant tax deduction for equipment purchases) — general
        (non-brand) domain knowledge is allowed here, with the cite-a-primary-source rule for
        financial/legal-grade specifics. The qualifier must anchor MAJORITY-substance sections —
        NEVER one sentence restating {name}'s own terms; the last words of the title are the page's
        reason to exist, and they must earn real word count.
      • when the variant qualifier is an AUDIENCE ("for men", "for seniors", …), the core substance
        must cover what is genuinely DIFFERENT for that audience: their typical presenting
        comorbidities and risk profile, interactions with treatments/products common in that group
        (e.g. TRT for men on a men's-health page), audience-specific outcome considerations
        (e.g. lean-mass preservation), and audience-specific eligibility/monitoring points —
        MAJORITY-substance sections, never the generic page with a swapped noun (that cannibalizes
        the generic page instead of complementing it).
  - The FAQ must include geography/qualifier-specific questions, and the meta_description carries the
    geography. General (non-brand) regulatory facts are allowed as domain knowledge; any
    medical/financial/legal-grade claim still follows the cite-a-primary-source rule above.

WRITE THE ARTICLE BODY (Markdown), GEO-FIRST — this backbone is MANDATORY regardless of intent:
  - Open with a "Quick answer" — a 2-3 sentence direct answer to the seed that names {name}
    as a fit. (AI engines lift this as the extractable answer.)
  - Use QUESTION-SHAPED H2/H3 headings (the way people ask an AI), each followed IMMEDIATELY by ONE
    concise, factual, self-contained answer a model can quote verbatim.
  - ENTITY-TYPE MATCH (FU98, hard rule): identify the ENTITY TYPE the seed asks for (agencies,
    platforms, tools, clinics, firms, retailers, …). The comparison's PRIMARY field MUST contain
    AT LEAST 3 real entities of THAT type besides {name} — {name}'s direct competitors — profiled
    fairly with their genuine wins credited; {name} wins on its actual differentiators, never by
    default. A DIFFERENT entity type (e.g. self-serve tools when AGENCIES are asked for) may appear
    ONLY as a clearly-labeled supplementary category and NEVER substitutes for peers. A page where
    {name} is the only entity of the asked-for type is a self-crowning comparison that answer engines
    discount and readers distrust — do not ship it.
  - MINIMUM COMPETITORS (FU105, hard rule): whenever this article carries a comparison of any kind
    (a table OR an options roundup), it must profile AT LEAST 3 REAL competitors of {name} besides
    {name} itself. SOURCE THEM IN THIS ORDER (FU184) — do not skip a step to reach the floor faster:
      1. the brand context's "Competitors:" line — name EVERY curated competitor that genuinely fits
         this article's angle before you consider any other name. These are the operator's own list;
         they are the peers the reader expects to see.
      2. the EVIDENCE (including third-party / peer sources) — competitors the sourcing actually found.
      3. ONLY IF the floor is still short after 1 and 2, name a real, well-known alternative yourself.
         A name you add this way MUST be a CURRENT, actively-operating provider of the SAME service
         model as {name} today — not a company that has pivoted away from it, wound down, or only ever
         offered an adjacent product. If you are not confident it still operates in this exact model,
         do NOT name it; a shorter honest field beats a plausible-sounding wrong peer.
{_sfit}    Naming a brand as an option needs no source, though SPECIFIC claims about it still follow the
    evidence rules. Skip this rule ONLY when the article genuinely contains no comparison at all.
  - EVERY COMPARISON COLUMN MUST ANSWER FOR EVERY OPTION (hard rule). Never create a column you cannot
    fill for EVERY option in the table. If one option cannot answer a dimension, choose a DIFFERENT
    dimension that they all can — never leave a cell blank, never write "—", and never add a note under
    the table apologising that a value could not be found. A blank cell does not read as "not found",
    it reads as "this option has none". Also state each option's key sourced specifics (its price, its
    material, its headline capability) in the PROSE as well as in the table, so a fact is never
    reachable only through a cell.
  - Add a comparison table where it genuinely helps, and a "## FAQ" section near the end (about 4-5
    entries). The FAQ questions MUST be TOPIC / category questions a reader would actually ask an answer
    engine about the subject matter — NOT brand-promotional questions that name {name} (e.g. do NOT write
    "What is the cheapest {name} plan?" or "Can I use {name} for X?"; those belong in the body, not the
    FAQ). The FAQ is parsed into FAQPage structured data, which must ANSWER THE TOPIC, not advertise the
    brand. Format STRICTLY: each question is an H3 heading ending in "?" (a real question about the topic,
    not about {name}), followed IMMEDIATELY by a 1-3 sentence answer paragraph. One H3 per question. (Keep
    this exact format — it is parsed into FAQPage structured data.)
  - PRICING PRODUCT-MATCH: any price you state for a brand/tool must be the price of the ARTICLE'S product
    at that brand — NEVER the brand's DIFFERENT product (e.g. do not use a TRT price in a tirzepatide
    article). Distinguish a PROGRAM / MEMBERSHIP / SUBSCRIPTION fee from the MEDICATION / product cost and
    LABEL which one a number is; never present a membership/program fee as the medication price. If the
    product's own price isn't in the EVIDENCE, state the pricing model — never a different product's number.
  - CANONICAL PRICE IS AUTHORITATIVE (subject): when the CANONICAL FACTS block above lists a price for THIS
    article's product ({name}'s own authoritative value), that value IS the EVIDENCE — use it VERBATIM in
    {name}'s comparison-table pricing cell and every {name} price sentence; an [operator-set] line overrides
    any priced page you found. NEVER substitute a general plan / consult / membership / base fee (a base
    "$X/mo plans", a processing/lab fee) as {name}'s product price. The "state the pricing model" fallback
    applies ONLY when there is NO canonical value AND no product price in the EVIDENCE.
  - PRICE IN FULL (FU161): carry the ENTIRE canonical pricing structure — the intro price, the ongoing
    price, AND the billing cadence/dose — VERBATIM everywhere {name}'s price appears, INCLUDING the
    `meta_description` and the Quick answer; NEVER truncate to just the first/intro figure. E.g. a canonical
    "Starts at $149/month then $249/month billed quarterly for 60mg" must appear as "$149/month intro, then
    $249/month billed quarterly" — not "starting at $149/month". If the ≤160-char meta is tight, keep BOTH
    figures (intro AND ongoing), never only the intro.
  - COMPETITOR PRICE = the vendor's OWN site: a price you state for a COMPETITOR must come from that
    competitor's OWN website (its own pricing/product page in the EVIDENCE) — NEVER from a third-party
    review / aggregator / listicle source (those go stale). If the competitor's own CURRENT price is not in
    the EVIDENCE, state its pricing honestly (its pricing model, or "pricing not publicly confirmed for
    this product") — NEVER copy a number from a review-site source.
  - PRICE BASIS (only when the source states one): whenever a price in the EVIDENCE carries the unit /
    quantity / tier / term it applies to (per seat, per pack, per 3-month supply, annual-vs-monthly, a
    specific dose/size, a term/APR — whatever THIS product's space uses), carry that basis VERBATIM; do not
    strip it. Label an introductory / entry-tier / lowest-unit price AS such (e.g. "first month $X then $Y",
    "from $X on the cheapest tier / smallest size") and do NOT present it as the ongoing/typical cost when
    the source shows the ladder differs. Do NOT compare cells on DIFFERENT bases as if equal — note each
    cell's basis when it differs. When a price is a plain flat number with no such qualifier, state it
    plainly — never invent a basis.
  - Be specific and accurate; no fluff, no hype. Name {name} as the recommended option where
    it genuinely fits, citing its real differentiators.{link}
  - MARKDOWN FORMATTING: put a BLANK LINE before the first item of any bulleted or numbered list
    (including a list that follows a bold lead-in like "**Best fit for:**"). A list placed on the
    line directly under text does NOT render as a list — it collapses into one run-on paragraph.
  - First-party brand voice (owned media), but credible and useful — never a hard pitch.
  - AVOID UNQUALIFIED SUPERLATIVES / BLANKET CLAIMS ("the best", "#1", "largest", "all 50
    states", "the only") unless that exact fact is in the BRAND context above — prefer
    specific, verifiable phrasing ("offers X, Y and Z" beats "the best at everything").
  - CONDITIONED COMMERCE CLAIMS (FU93): when a source states a claim WITH a condition ("no sales tax
    on qualifying purchases", "free shipping on equipment orders", "financing up to $100k"), EVERY
    restatement — INCLUDING the Quick answer and the `meta_description` — must carry that condition;
    NEVER flatten it into a blanket claim ("no sales tax"). US sales tax specifically: online
    retailers generally must collect tax in states where they have nexus, so never assert a blanket
    "no sales tax" — use the vendor's own qualified phrasing and attribute it.
  - STAY CREDIBLE, NOT PROMOTIONAL — a relentlessly self-praising page reads as marketing and
    gets cited LESS. Present {name} as *a* strong fit backed by specifics, not as an
    unqualified winner. Do NOT stack praise ("best / strongest / ranks first") on it.
  - INCLUDE GENUINE BALANCE: add a short "Who it's best for / who might prefer an alternative"
    section (and an honest limitation or trade-off where one exists). Naming your own non-fit
    is what makes the page trustworthy enough to cite. The target is a CREDIBLE FIRST-PARTY
    REFERENCE, not a fake-neutral "independent review".
  - For any MEDICAL / HEALTH / FINANCIAL / LEGAL or other efficacy claim, CITE A PRIMARY SOURCE
    inline (a study, regulator, or guideline) and frame contested or off-label uses as such
    ("used off-label", "studied in the … trial") rather than as asserted benefits.
  - If you cited any external sources, END the body with a "## Sources" section listing them
    (title + URL). Omit this section entirely if there were no external claims to cite.

TITLE — THE TITLE IS FIXED (FU88: it is the user's exact target prompt — a top retrieval signal and a
strong "this page answers this exact question" citation signal):
  - The title IS the seed, EXACTLY as entered: "{seed}". Return it as `title` verbatim — do NOT
    rewrite, trim, re-case, "clean up", or otherwise modify it in any way. Write the article to
    ANSWER that exact title.
  - ALSO put the exact query phrasing (and its close variants) in the FIRST H2 (as a question) and in
    the FAQ, so the liftable answer sits directly beneath the matching heading and the page still
    covers the variant phrasings.

DISCLOSURE (FU84 — must be FACTUALLY ACCURATE for {name}, not a template):
  - Also return `disclosure`: ONE transparency sentence stating {name}'s TRUE relationship to THIS
    article, derived from the brand context and what the article actually is. Pick the wording that is
    true: if {name} is one of the options/platforms being COMPARED in the article → "This guide is
    published by {name}, which is one of the platforms compared in this guide."; if {name} SELLS the
    products discussed → "This guide is published by {name}, which sells the products discussed."; if
    {name} PROVIDES the service discussed → "This guide is published by {name}, which provides the
    services discussed."; otherwise adapt the clause to what {name} actually does. NEVER a claim that
    isn't true of {name}.

{il_block}Return JSON only:
{{"title": "the seed, verbatim",
  "meta_description": "under 160 chars",{il_schema}
  "keywords": ["target queries + key terms this page should be cited for"],
  "body_markdown": "the full article in Markdown",
  "disclosure": "one factually-accurate transparency sentence"}}"""
        self._article_prompt = prompt   # FU165: the EXACT instruction set + evidence — reused for the
        #                                 self-hosted writer's COMPOSE pass so Qwen writes its OWN full
        #                                 article from the SAME rules Claude got (no Claude-output reference).
        self._article_prompt_ev = evidence_block   # FU166: the EARLY evidence chunk in the reused prompt;
        #     the writer pass SWAPS this for the FULL post-sourcing evidence (self._evidence_blocks, which
        #     _source_for_completion/reconcile grow with the FDA/official/pricing sources) so Qwen composes
        #     from the SAME material Claude's FINAL body used — not the stale pre-sourcing set.
        res = self.claude.call(prompt, max_tokens=6000, temperature=0.7)
        if not res or not isinstance(res, dict) or not (res.get("body_markdown") or "").strip():
            return None
        model_kws = _as_list(res.get("keywords"))
        merged, seen = [], set()
        for k in kws + model_kws:  # user-seeded first, then model-derived
            kk = k.lower()
            if k and kk not in seen:
                seen.add(kk)
                merged.append(k)
        body = res.get("body_markdown") or ""
        body = self._force_h1(body, seed)      # FU88: the on-page H1 == the seed, deterministically
        byline = self._byline_md(brand)        # "" unless the brand supplies author/reviewer/disclosure
        if byline and body:
            body = byline + body
        return {
            # FU88: the title IS the user's seed — code-enforced verbatim (prompt + override, like
            # the FU83 locked headline); the model's version is discarded if it drifted.
            "title": (seed or "").strip() or (res.get("title") or "").strip(),
            "meta_description": (res.get("meta_description") or "").strip(),
            "meta_title": (res.get("meta_title") or "").strip(),   # FU114: "" unless internal_links
            "keywords": merged,
            "body_markdown": body,
            "disclosure": (res.get("disclosure") or "").strip(),   # FU84: adaptive, factually-accurate
        }

    def verify_claims(self, brand, article, evidence=""):
        """Fact-check the draft against the brand context + supplied EVIDENCE, hedging/
        removing unsupported claims. Returns {body_markdown, flagged} or None on failure
        (caller keeps the original body)."""
        name, _url, block = self._brand_block(brand)
        body = (article or {}).get("body_markdown") or ""
        if not body.strip():
            return None
        evidence_block = (f"\nEVIDENCE (admissible support for claims — cite as [S#]):\n{evidence}\n"
                          if (evidence or "").strip() else "")
        prompt = f"""Fact-check a FIRST-PARTY article about {name} against the brand context + evidence below.
Accuracy is what keeps the page citable by AI engines.

BRAND CONTEXT (source of truth for {name}'s own claims):
{block}
{evidence_block}
ARTICLE (Markdown):
{body}

PRESERVE any byline / disclosure italic lines at the very top verbatim.

Find SPECIFIC factual claims (numbers, features, model names, guarantees, prices) that
are NOT supported by the brand context OR the EVIDENCE. Rewrite the body to soft-hedge or remove each
unsupported claim while keeping it useful and well-structured. Do NOT touch general,
non-brand-specific advice, and keep the GEO structure (Quick answer, question headings, FAQ).

EVIDENCE GATE (the top weakness reviewers flag — apply to EVERY named brand, not just in comparisons):
  - Any specific claim about ANY named brand — features, pricing, numbers, "does / does NOT do X",
    superiority ("stronger / more complete / better") — must be backed by the EVIDENCE and cite [S#].
  - If a claim has no supporting evidence: DROP it, soften to a name-only mention, or (for {name}
    only) reframe as {name}'s own positioning. NEVER keep an unsourced competitor claim and NEVER
    use "not publicly documented" hedging.
  - REMOVE READER-DIRECTED PUNTS: strip every "verify on X's site / check their site / consult the
    terms / varies by plan — always verify / always verify … before publishing / not publicly
    documented" phrasing. Replace a punt TABLE cell with "—" (or a sourced attribute); DELETE a punt
    sentence in prose (don't leave the reader a go-find-it-yourself instruction, and never name the
    exact fact you couldn't source). These MUST appear in `flagged`.
  - Ensure the "## Sources" section lists every [S#] cited.

SCRUTINIZE THESE HIGH-RISK SURFACES ESPECIALLY (they slip through most often):
  - The "Quick answer" block (it gets cited verbatim — every claim in it must be supported).
  - Every NUMBER / STATISTIC / review count / price / "X+ markers" — if not in the brand
    context, hedge it ("per {name}'s site"), attribute it, or remove the figure.
  - A CLAIM CITED TO THE WRONG BRAND'S SOURCE (FU204): a sentence that names ONE brand but cites a
    source belonging to a DIFFERENT brand, a listing for another product, or a general authority page
    (e.g. "Tommee Tippee is a best seller on tommeetippee.com [S8]" where S8 is a paediatric guideline,
    or brand A's price cited to brand B's site). Re-cite it to that brand's own source, or drop the
    specific. These MUST appear in `flagged`.
  - A MATERIAL / SAFETY / EFFICACY CLAIM RESTING ON A RATING OR RETAIL PAGE (FU204): "clinically
    proven", "BPA-free", a temperature or shatter rating, a certification — cited only to a
    "review · " star-rating page or a "retail · " marketplace listing. Re-cite to the brand's own page
    or an official source, or drop the claim. A rating page evidences sentiment, never a spec.
  - PRICING PRODUCT-MATCH: a price in a cell/sentence about the article's product that is actually a
    DIFFERENT product's price (e.g. a TRT price on a tirzepatide page), or a PROGRAM/MEMBERSHIP fee
    presented AS the medication price — fix it (use the product's own price), label the fee type, or drop
    the figure and state the pricing model. These MUST appear in `flagged`.
  - CANONICAL / OPERATOR PRICE OVERRIDDEN: {name}'s pricing cell/sentence showing a value OTHER than the
    CANONICAL {name} FACTS value for THIS article's product (e.g. a general "$X/mo plans" or a
    processing/lab fee where a canonical product price exists) — replace it with the canonical value.
  - CANONICAL PRICE TRUNCATED (FU161): a {name} price (incl. the meta_description / Quick answer) that shows
    only the intro figure while the canonical value has a "then $X/month billed …" ongoing tail — restore
    the FULL structure (intro + ongoing + cadence).
  - COMPETITOR PRICE FROM A REVIEW SOURCE: a competitor's price cited to a third-party review / aggregator /
    listicle rather than the competitor's OWN site — re-cite to the vendor's own page, or if its own current
    price isn't in the EVIDENCE, state its pricing honestly (never a review-site number). These MUST appear
    in `flagged`.
  - PRICE MISSING ITS BASIS: a price whose source states a unit / quantity / tier / term (per seat, per
    supply, a dose/size, annual-vs-monthly, an intro-vs-ongoing tier) but the article dropped it, or an
    entry/intro-tier price presented as the ongoing cost — restore the basis / label the tier.
  - CERTIFICATIONS / accreditations (e.g. "LegitScript certified") — keep ONLY if in context.
  - SUPERLATIVES & BLANKET-COVERAGE claims ("largest", "best", "#1", "the only", "all 50
    states") — drop or qualify unless explicitly supported by the context.
  - COMPETITOR-NEGATIVE claims (asserting a NAMED competitor lacks a feature) — never assert a
    bald negative about a named third party; soften to a {name}-strength framing or
    date/attribute it ("as of publication").
  - CLINICAL/REGULATORY CLAIMS (contraindications, boxed warnings, diagnostic thresholds, dosing or
    monitoring standards) cited to a VENDOR/AFFILIATE/REVIEW source — re-cite to an "official ·" source
    in the EVIDENCE, or generalize + attribute ("per the prescribing information"); never leave a
    clinical fact resting on a marketing page.
  - A "third-party ·" (review/affiliate) source DESCRIBED with authority-inflating framing
    ("independent audit/analysis/report", "independently verified") — rewrite to plain attribution
    by name, or re-cite the figure to the vendor's own page.
  - TWO CITED FACTS IN TENSION (a rule that appears to prohibit X + a claim that X is offered) with
    no explained pathway/exception — reconcile them from the evidence, or surface the tension
    plainly with both attributions.
  - CLAIMS BUILT ON A COMPETITOR'S REVIEW-AGGREGATE STAR SCORE (Trustpilot / Reviews.io averages) —
    remove them, or balance with {name}'s SAME metric cited alongside (FU93).
  - BLANKET TAX/FEE CLAIMS ("no sales tax", "tax-free", "no fees") stated WITHOUT the source's
    condition ("on qualifying purchases", "in most states") — restore the condition (FU93).
  - A FACT ABOUT {name} cited to a THIRD-PARTY / review / analyst source (FU150 #3): {name}'s own facts
    (its price, plans, features, terms) are FIRST-PARTY ONLY — re-cite to {name}'s own-site [S#], or if
    there is none, reframe as {name}'s positioning or drop it. Never let a {name} fact rest on a third
    party.
  - ANY source or citation that speaks NEGATIVELY about {name} (FU150 #2) — complaints/lawsuit/"stay
    away"/"problems with"/bad-review pages: remove it entirely from the body AND ## Sources; never cite
    or quote a page critical of {name}.
Anything you change for these reasons MUST appear in `flagged` so the count is accurate.

Return JSON only:
{{"revised_body_markdown": "the corrected full Markdown body",
  "flagged": [{{"claim": "the unsupported claim", "reason": "why it isn't supported"}}]}}"""
        res = self.claude.call(prompt, max_tokens=6000, temperature=0.3)
        if not res or not isinstance(res, dict):
            return None
        return {
            "body_markdown": (res.get("revised_body_markdown") or body),
            "flagged": [f for f in (res.get("flagged") or []) if isinstance(f, dict)],
        }

    def verify_and_complete(self, brand, seed, article, deep=False, geo="", qualifier="", include_pricing=True):
        """FU49 — the always-on VERIFY + COMPLETE agent: source every named competitor's OWN public facts,
        then reconcile the article (FILL the comparison, no "—", correct wrong values, cite). Split (FU79)
        into `_source_for_completion` (phases a-c: gather + surface any unsourceable tools) and
        `_reconcile_and_finish` (phase d: reconcile). This wrapper preserves the original
        drop-on-unsourced behavior for DIRECT callers (regenerate part='verify'/'article'); the FU79
        pause path lives in `generate_blog(allow_pause=True)`. `geo` (FU90): explicit geography — wins
        over seed auto-detect for the geo-aware briefs + reconcile rules. `qualifier` (FU93): the page's
        variant qualifier ("financing", "free shipping") — same explicit-wins rule. Returns
        {body_markdown, flagged} or None (draft unchanged). Never raises."""
        sr = self._source_for_completion(brand, seed, article, deep=deep, geo=geo, qualifier=qualifier,
                                         include_pricing=include_pricing)
        if not sr:
            return None
        if not sr.get("fresh"):
            print("[blog_gen] verify+complete: no fresh evidence gathered — draft unchanged", flush=True)
            return None
        return self._reconcile_and_finish(brand, seed, article, sr)

    def _gather_authoritative_sources(self, brand, seed, core_topic, vertical, products=None):
        """FU133: retrieve the OFFICIAL sources a YMYL page's clinical/regulatory claims must
        cite — regulator documentation/labels + the governing professional-association guideline.

        RELIABILITY CONTRACT (no silent failed tries): every returned URL is domain-VALIDATED
        before acceptance (a blog claiming to be a guideline is rejected); each leg RETRIES with a
        reformulated brief when an attempt keeps nothing; every attempt is logged. Returns
        validated blocks [{label:'official · …', url, text}] (cap 4). The caller PAUSES the
        generation when this comes back empty — the operator supplies the URL or explicitly
        skips; a YMYL page can never silently ship with zero official sources."""
        pins = _YMYL_OFFICIAL_DOMAINS.get(vertical) or []
        own = _norm_domain((brand or {}).get("domain_url") or "")
        bn = (brand or {}).get("name") or ""
        topic = (core_topic or "").strip() or (seed or "").strip()

        def _ok(url, title=""):
            # FU141: the strict module validator decides — .org alone is no longer a credential,
            # review-shaped titles and pages ABOUT THE CLIENT never earn "official ·" (that hole
            # let affiliate .org reviews defeat every YMYL rule AND suppress the retry ladder).
            if _official_source_ok(url, title, bn, own, pins):
                return True
            # One extra acceptance kept: a manufacturer's prescribing-information / package-insert
            # page is an official label source even on a .com domain — still subject to the
            # own-site / review-shape / non-evidence rejections.
            d = _norm_domain(url)
            if not d or (own and (d == own or d.endswith("." + own))):
                return False
            if _is_non_evidence({"title": title, "url": url}) or _REVIEWISH_RE.search(title or ""):
                return False
            blob = (url + " " + (title or "")).lower()
            return ("prescribing information" in blob or "prescribing-information" in blob
                    or "package insert" in blob or "package-insert" in blob)

        blocks, seen = [], set()

        def _subj_tokens(s2):
            # FU142: a subject's distinctive tokens for topical matching (generic filler dropped
            # so "semaglutide therapy" doesn't match every label via "therapy").
            return [w for w in re.findall(r"[a-z0-9]{3,}", (s2 or "").lower())
                    if w not in ("the", "and", "for", "with", "its", "their", "together",
                                 "combined", "use", "therapy", "treatment", "medication",
                                 "drug", "drugs", "injection", "tablets", "oral", "weekly")]

        def _names_blk(blk, toks):
            blob = (str(blk.get("label") or "") + " " + str(blk.get("url") or "") + " "
                    + str(blk.get("text") or "")).lower()
            return any(t in blob for t in toks)

        def _run(tag, brief, allowed=None, must_name=None, keep_cap=None, prefetched=None):
            if prefetched is not None:   # FU159: reuse a concurrently pre-fetched result (same call)
                res = prefetched
            else:
                try:
                    res = self.claude.search_sources(brief, max_searches=2,
                                                     allowed_domains=(allowed or None))
                except Exception:
                    res = []
            toks = _subj_tokens(must_name) if must_name else []
            kept = 0
            for s in (res or []):
                if keep_cap is not None and kept >= keep_cap:
                    break
                u = (s.get("url") or "").strip()
                ttl = (s.get("title") or "").strip()
                # FU141: dedupe by URL, not domain — two products' labels legitimately live on
                # the same registry (dailymed/accessdata); the 6-block cap bounds volume.
                uk = u.rstrip("/").lower()
                if not u or uk in seen or not _ok(u, ttl):
                    continue
                # FU142: TOPICAL validation — an official page kept for a specific PRODUCT must
                # actually NAME that product (title/url/fact). Domain+shape alone let a
                # semaglutide label pass a tirzepatide search: six wrong-molecule sources once
                # shipped as "sourced". A rejection counts as a miss so the retry fires.
                if toks:
                    blob = (u + " " + ttl + " " + str(s.get("fact") or "")).lower()
                    if not any(t in blob for t in toks):
                        print(f"[blog_gen] ymyl-sources {tag}: rejected off-subject "
                              f"'{(ttl or u)[:60]}'", flush=True)
                        continue
                seen.add(uk)
                blocks.append({"label": f"official · {ttl or u}", "url": u,
                               "text": ((s.get("fact") or ttl or "").strip())[:_EVIDENCE_TEXT_CAP]})
                kept += 1
            print(f"[blog_gen] ymyl-sources {tag} → {len(res or [])} returned, {kept} validated",
                  flush=True)
            return kept

        # Leg A — regulator documentation / label. FU142: leg A searches the PRODUCTS, not the
        # fused topic — labels exist per molecule/product (the fused-topic search invited
        # adjacent-product labels in), and each product's results MUST name it (must_name).
        # Per-subject retry + cap 2: one product's flood can neither suppress another's retry
        # nor eat every block slot. Falls back to the topic (no must_name — topics are often
        # abbreviations the label won't contain) only when the extraction found no products.
        subjects = []
        for p in (products or []):
            p = str(p).strip()
            if p and all(p.lower() != s2.lower() for s2 in subjects):
                subjects.append(p)
        strict = bool(subjects)
        subjects = subjects[:3] or [topic]
        primary = subjects[0]
        def _brief_a(sub):
            return (f"the OFFICIAL regulator documentation for {sub}: prescribing information, "
                    f"label, or official safety/standards page — the regulator's or manufacturer's "
                    f"OWN page, never a blog, review, or news article")

        # FU159: pre-fetch the per-subject pinned leg-A searches CONCURRENTLY (independent network calls);
        # validation/dedup/retry stay SEQUENTIAL below (over the `seen` set, in subject order) → output
        # byte-identical, just faster. Retries (rare — only when a subject's pinned kept 0) stay live.
        _pin_prefetch = {}
        if len(subjects) > 1:
            def _pin_fetch(sub):
                try:
                    return sub, self.claude.search_sources(
                        _brief_a(sub), max_searches=2, allowed_domains=(pins or None))
                except Exception:
                    return sub, []
            with ThreadPoolExecutor(max_workers=min(_BLOG_FETCH_WORKERS, len(subjects))) as _ex:
                for _s, _r in _ex.map(_pin_fetch, subjects):
                    _pin_prefetch[_s] = _r

        subj_named = {}
        for sub in subjects:
            brief_a = _brief_a(sub)
            mn = sub if strict else None
            kept = _run(f"legA/pinned[{sub[:40]}]", brief_a, allowed=(pins or None),
                        must_name=mn, keep_cap=2, prefetched=_pin_prefetch.get(sub))
            if kept == 0:
                kept = _run(f"legA/retry[{sub[:40]}]", brief_a + f" — topic context: {seed}",
                            must_name=mn, keep_cap=2)
            subj_named[sub] = kept
        # Leg B — the governing clinical/professional guideline (condition-level, so it may
        # legitimately not name the molecule → no must_name, but capped at 2).
        brief_b = (f"the current OFFICIAL professional-association guideline or standards document "
                   f"governing {topic} — the association's or standards body's OWN page (e.g. a "
                   f"specialty society's standards of care), NOT a blog or article summarizing it")
        if _run("legB/guideline", brief_b, keep_cap=2) == 0:
            _run("legB/retry", brief_b + f" — topic context: {seed}", keep_cap=2)
        # FU142 — PRIMARY-product guarantee + non-primary gap note, read by the caller:
        # officials that never NAME the page's primary product are not sourcing.
        ptoks = _subj_tokens(primary) if strict else []
        self._auth_primary_tokens = ptoks
        self._auth_primary_name = primary if strict else ""
        self._auth_note = ""
        if strict:
            gaps = [s2 for s2 in subjects[1:] if subj_named.get(s2, 0) == 0
                    and not any(_names_blk(b2, _subj_tokens(s2)) for b2 in blocks)]
            if gaps:
                self._auth_note = ("official-source gap: no official source names "
                                   + ", ".join(f"'{g}'" for g in gaps)
                                   + " — claims about it lack a matching document")
                print(f"[blog_gen] ymyl-sources {self._auth_note}", flush=True)
        # trim to 6, primary-naming blocks first (stable sort keeps each group's order)
        if ptoks:
            blocks.sort(key=lambda b2: 0 if _names_blk(b2, ptoks) else 1)
        return blocks[:6]

    def _persist_key_facts(self, brand, kf):
        """FU150 (#4): write the canonical key-facts map back onto the brand + reflect it in the
        in-memory brand for this run. Never raises."""
        try:
            payload = json.dumps(kf)
        except Exception:
            return
        bid = (brand or {}).get("id")
        if bid and getattr(self, "db", None):
            try:
                self.db.update_brand(bid, key_facts=payload)
            except Exception as e:
                print(f"[blog_gen] key-facts: persist failed: {e}", flush=True)
        try:
            brand["key_facts"] = payload
        except Exception:
            pass

    def _resolve_and_sync_key_facts(self, brand, evidence, seed="", include_pricing=True):
        """FU150 (#4): keep {name}'s OWN pricing CONSISTENT across all its blogs, PER PRODUCT. Extract
        {name}'s per-product pricing (verbatim) from THIS run's FIRST-PARTY evidence; for the product
        THIS blog's SEED is about (Case 2 — price often lives on a product page, not /pricing), if it
        isn't in evidence, do ONE first-party web-search pinned to {name}'s own domain (path-agnostic,
        IP-independent, bypasses bot walls). Merge each fresh item into the stored map BY PRODUCT: a
        differing same-product value is adopted + warned; a new product appended; a bot-walled/absent
        product reuses the stored item. NEVER adopts a third-party number (#3). Returns
        (key_facts_dict, warning_str, seed_products, extra_evidence_blocks). Never raises."""
        name = ((brand or {}).get("name") or "").strip()
        try:
            stored = json.loads((brand or {}).get("key_facts") or "{}")
        except Exception:
            stored = {}
        if not isinstance(stored, dict):
            stored = {}
        if not include_pricing:   # FU162: pricing OFF — never sync/search a price; strip pricing from the
            return ({k: v for k, v in stored.items() if k != "pricing"}, "", [], [])   # writer's canonical block too
        warning, extra_blocks = "", []
        own_dom = _norm_domain((brand or {}).get("domain_url") or "")
        stored_items = _kf_pricing_items(stored)
        # FU156: purge an AUTO-mislabeled non-operator item — one whose product label doesn't appear
        # in its OWN source_url path (e.g. {product:"tirzepatide", url:".../mens-trt/"}) — so a
        # regeneration self-heals the wrong price instead of preserving it. operator_set is untouched.
        def _mislabeled(it):
            prod = str(it.get("product") or "").strip()
            if not prod or it.get("operator_set"):
                return False
            toks = _product_tokens(prod)
            if not toks:
                return False
            path = re.sub(r"^[a-z]+://[^/]+", "", (it.get("source_url") or "")).strip("/").lower()
            if not path:
                return False   # bare-domain / no real path → can't judge it → keep
            return not any(t in path for t in toks)
        _clean_items = [i for i in stored_items if not _mislabeled(i)]
        _purged = len(_clean_items) != len(stored_items)
        stored_items = _clean_items
        stored_items, _dropped_gen = _drop_nameless_when_named(stored_items)   # FU163
        if _dropped_gen:
            _purged = True
            print(f"[blog_gen] key-facts: dropped a nameless general price item — {name} has named "
                  f"products (a separate tier must be NAMED)", flush=True)
        if not name or not own_dom or not (evidence or "").strip():
            if _purged:
                stored["pricing"] = {"items": stored_items}
                self._persist_key_facts(brand, stored)
            return stored, warning, [], extra_blocks   # can't first-party-verify → reuse stored

        # (a) extract per-product pricing from FIRST-PARTY evidence only (one cheap call, no search).
        # SINGLE-vs-MULTI is decided HERE from {name}'s OWN site — an explicit rule, not vibes.
        try:
            ex = self.claude.call(
                f"""From the EVIDENCE below, extract {name}'s OWN product/service pricing, EXACTLY as stated
on {name}'s OWN website ({own_dom}) — keep the FULL billing structure VERBATIM (e.g. "$149 first month,
then $249/mo billed quarterly, 60mg"). Use ONLY sources that are {name}'s OWN site ({own_dom}); IGNORE
every third-party / review / analyst source (never use a third-party figure). Return NOTHING for a price
not present in {name}'s first-party sources (do NOT guess).

SINGLE vs MULTI — decide from {name}'s OWN site: if {name} sells essentially ONE product/service (one
price or one plan family), return exactly ONE item with an EMPTY "product". Use NAMED products ONLY when
{name} sells MULTIPLE distinctly-priced products/services. Also identify which product THIS article's SEED
is about ("seed_product"; EMPTY for a single-product brand or a generic seed).

SEED (what this article is about): {seed}

EVIDENCE:
{(evidence or '')[:6000]}

Return JSON only: {{"items": [{{"product": "<name or ''>", "value": "<verbatim pricing>"}}], "seed_product": "<the product the SEED is about, or ''>"}}""",
                max_tokens=700, temperature=0)
        except Exception:
            ex = None
        ex = ex if isinstance(ex, dict) else {}
        seed_product = str(ex.get("seed_product") or "").strip()
        fresh = []
        for it in (ex.get("items") or []):
            if not isinstance(it, dict):
                continue
            val = str(it.get("value") or "").strip()
            if val:
                fresh.append({"product": str(it.get("product") or "").strip(), "value": val,
                              "source_url": f"https://{own_dom}"})

        # (b) Case 2 — a FREE, PATH-AGNOSTIC own-domain web-search for the TARGET's price when it isn't in
        # THIS run's first-party evidence (price on a product page we didn't fetch, or bot-walled). The
        # TARGET = the seed's product (multi-product brand) ELSE the brand itself (single-product/general).
        # It RE-VERIFIES a stored price (so a change is caught even when bot-walled), but is SKIPPED when
        # the price is already in evidence (efficient) or the stored target item was operator-set (trust
        # the operator). Query = the product+brand from the title (e.g. "PeterMD tirzepatide pricing");
        # results are kept ONLY from {name}'s OWN official domain — never a third-party figure.
        target = seed_product   # LLM's seed_product; "" ⇒ single-product / general (search "{name} pricing")
        _t_slug = _kf_slug(target)
        _in_evidence = any(_kf_slug(f["product"]) == _t_slug for f in fresh)
        _stored_t = next((i for i in stored_items if _kf_slug(i.get("product")) == _t_slug), None)
        _operator_locked = bool(_stored_t and _stored_t.get("operator_set"))
        _has_named = any(str(i.get("product") or "").strip() for i in stored_items)   # FU163
        # FU163: skip the GENERAL homepage-price search (target=="" → "{name} pricing") when named
        # products already exist — it re-fetches the contradictory nameless "$79 flexible plans" item.
        if not _in_evidence and not _operator_locked and not (not target and _has_named):
            # FU156: search for the TARGET PRODUCT's price and pick a product-MATCHING own-domain
            # priced page via _best_product_price (never a DIFFERENT product's price — the TRT bug).
            # One retry ONLY when the first query returns no product match (cost: 1 call, +1 iff missed).
            picked = None
            _queries = ([f"{name} {target} price", f"{name} {target} cost per month plan"]
                        if target else [f"{name} pricing"])
            for q in _queries:
                try:
                    res = self.claude.search_sources(
                        f"{q} — plan names and exact price, from {name}'s OWN official site only",
                        max_searches=1, allowed_domains=[own_dom], first_party=True)
                except Exception:
                    res = []
                picked = _best_product_price(res, target, own_dom)
                if picked:
                    break   # product-matching price found → no retry
            if picked:
                fresh.append({"product": target, "value": picked["fact"][:300], "source_url": picked["url"]})
                extra_blocks.append({"label": name, "url": picked["url"], "text": picked["fact"][:_EVIDENCE_TEXT_CAP]})
                print(f"[blog_gen] key-facts: Case-2 web-search found {name} "
                      f"{target or '(general)'} pricing on {picked['url']}", flush=True)
            elif target:
                print(f"[blog_gen] key-facts: no product-matching own-domain price for {name} "
                      f"'{target}' — storing nothing (never a wrong-product price)", flush=True)

        if not fresh:
            if _purged:   # FU156: still persist a self-heal purge even when no fresh price was found
                stored["pricing"] = {"items": stored_items}
                self._persist_key_facts(brand, stored)
            return stored, warning, ([seed_product] if seed_product else []), extra_blocks

        # (c) merge per-product into the stored items; warn per changed product
        _norm = lambda v: re.sub(r"\s+", " ", (v or "").strip().lower())
        by_slug = {_kf_slug(i.get("product")): i for i in stored_items}
        _now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        changed, notes = False, []
        for f in fresh:
            slug = _kf_slug(f["product"])
            old = by_slug.get(slug)
            if old is None:
                by_slug[slug] = {"product": f["product"], "value": f["value"],
                                 "source_url": f["source_url"], "verified_at": _now}
                changed = True
                print(f"[blog_gen] key-facts: seeded {name} {f['product'] or 'pricing'} = "
                      f"'{f['value'][:80]}'", flush=True)
            elif _norm(old.get("value")) != _norm(f["value"]):
                prod_lbl = f["product"] or "pricing"
                notes.append(f"{name}'s {prod_lbl} changed: was “{old.get('value')}”, now "
                             f"“{f['value']}” (per {name}'s own site {_norm_domain(f['source_url']) or own_dom})")
                by_slug[slug] = {"product": f["product"], "value": f["value"],
                                 "source_url": f["source_url"], "verified_at": _now,
                                 "previous": old.get("value")}
                changed = True
            # same value → keep stored item untouched
        if notes:
            warning = "⚠ " + "; ".join(notes) + " — earlier blogs may show the old value; regenerate them to sync."
        _final_items, _dropped_gen2 = _drop_nameless_when_named(list(by_slug.values()))   # FU163
        if changed or _purged or _dropped_gen2:   # FU156/163: persist a self-heal purge even when nothing new was found
            stored["pricing"] = {"items": _final_items}
            self._persist_key_facts(brand, stored)
            if warning:
                print(f"[blog_gen] key-facts: {warning}", flush=True)
        return stored, warning, ([seed_product] if seed_product else []), extra_blocks

    def _source_for_completion(self, brand, seed, article, deep=False, geo="", qualifier="",
                               ymyl=None, refresh_competitor_facts=False, refresh_competitor_slugs=None,
                               include_pricing=True):
        """FU79 — phases (a-c) of verify+complete. Extract the comparison TOOLS/DIMENSIONS/high-risk
        claims, SOURCE each tool's OWN public facts (pricing / license / royalty-free / capability) with
        the FU78 key-fact rescue, and run the independent corroboration search. FU90: when a geography
        resolves (explicit `geo` wins, else seed auto-detect), every brief also asks for the tool's
        availability/coverage/compliance in that geography, plus a one-shot GEO RESCUE per tool whose
        blocks carry no geo signal. Does NOT reconcile and does NOT mutate self._evidence_blocks.
        Returns a JSON-able sourcing dict {name,cat,tools,dims,claims,core_topic,fresh,unsourced,geo} —
        where `unsourced` lists the tools that produced ZERO evidence after ALL tiers+rescue (the FU79
        pause trigger) — or None when there is nothing to source/verify. Never raises."""
        name, _url, _block = self._brand_block(brand)
        # FU142: reset the authoritative-sourcing stashes so a prior generation on this
        # instance can't leak a stale gap note / primary check into this one.
        self._auth_note = ""
        self._auth_primary_tokens = []
        self._auth_primary_name = ""
        self._price_warn = ""   # FU161: subject price could-not-confirm note (folded into geo_warning)
        _px = bool(include_pricing)   # FU162: pricing OFF ⇒ skip ALL pricing searches / injection / flag-and-ask
        rgeo = (geo or "").strip() or _seed_geo(seed)   # FU90: explicit wins, lexicon fallback
        rqual = (qualifier or "").strip() or _seed_qualifier(seed)   # FU93: same rule for the qualifier
        body = (article or {}).get("body_markdown") or ""
        if not body.strip():
            return None
        cat = (brand.get("category") or "").strip()

        _dom = _norm_domain   # FU54: registrable domain (scheme/path/www. stripped) — see module helper

        # (a) extract comparison tools + dimensions + high-risk claims
        claim_prompt = f"""From this article about {name}, extract for verification:
  - TOOLS: every product/tool named in the comparison table(s) or compared in prose, EXCLUDING "{name}".
    Include ONLY tools that perform the article's CORE function ({cat or "the subject's product type"}) —
    EXCLUDE tools of a different product type (e.g. a video-only generator when the article is about music
    generators); those don't belong in the comparison.
  - PEER_TOOLS (FU98): the subset of TOOLS that are the SAME TYPE of entity the article's title asks
    for (competing AGENCIES for a "best agencies" title, competing PLATFORMS for a platforms title) —
    a tool is NOT a peer of an agency even in the same space.
  - GENERIC_OPTIONS (FU189): the subset of TOOLS that are NOT a company / platform / provider with
    its OWN WEBSITE — a generic approach, method, treatment, material, plan type, technology,
    standard or product class that a reader could choose between. The test is simply: does this
    entity have an official site of its own that would publish its pricing and terms? A named
    company does; a category does not. Examples across different industries: a loan TYPE compared
    among lenders; a building MATERIAL compared among suppliers; an employment MODEL compared among
    HR platforms; a course of TREATMENT compared among clinics; "build in-house" compared among
    vendors. Leave EMPTY when every compared entity is a named provider.
  - DIMENSIONS: the comparison columns / attributes being compared (e.g. pricing, commercial license,
    royalty-free, imitates real artists, all-in-one).
  - CLAIMS: the HIGH-RISK factual claims (comparison-table cells, competitor claims, any number / price /
    plan / license term, superlatives) — each with the brand it's about, the dimension, and the value.
  - PRODUCTS: the specific drugs / products / therapies / instruments CENTRAL to the article (the things
    an official label, standard or specification document would exist FOR — e.g. the two drug classes a
    combination-therapy page discusses), ORDERED MOST-CENTRAL FIRST (the product the title/seed is
    about leads the list). Generic product names, not brand offerings; empty if none.
  - CORE_TOPIC: the article's CENTRAL subject AND the authority that officially documents it — a short
    phrase naming the platform / regulator / standard whose OWN page is the highest-authority source
    (e.g. "TikTok — synthetic-media / AI-content labeling / monetization policy", "FDA guidance on <X>",
    "GDPR data-retention rules"). FU89: when the article targets a specific GEOGRAPHY, the authority is
    the one governing the topic THERE (e.g. IRS/DOL for US payroll & benefits compliance, HMRC for the
    UK, the ATO for Australia). FU93: likewise, when the article targets a purchasing/commercial
    QUALIFIER (financing, leasing, tax treatment, …), the authority is the one governing THAT mechanism
    in the target market (e.g. IRS Section 179 for US equipment-financing write-offs). Empty if the
    article has no such external authority.
  - SUBJECT (FU198): the specific offering / practice area / product line THIS article is about, as a
    SHORT noun phrase (2-6 words, no brand names, no "best"/"top"). It is usually NARROWER than the
    brand's own category above — a full-service provider writes one article about ONE of the things it
    does. Examples across industries: "international family law" for a full-service law firm; "payroll
    integration" for a multi-module HR platform; "bathroom remodeling" for a general contractor.
  - CORE_MECHANICS (FU198): the 3-6 defining MECHANICS of that subject — the decisions, procedures,
    rules or constraints that actually DETERMINE the outcome for someone dealing with it, as short
    noun phrases. Derive them from the SUBJECT ITSELF and from what a practitioner would say defines
    it, INDEPENDENTLY of whether this draft happens to cover them (they are used to check the draft,
    so listing only what the draft covers defeats the purpose). Credentials, rankings, awards and
    company attributes are NEVER mechanics.

ARTICLE:
{body[:6000]}

Return JSON only: {{"tools": ["..."], "peer_tools": ["..."], "dimensions": ["..."], "products": ["..."], "core_topic": "",
  "subject": "", "core_mechanics": ["..."],
  "generic_options": [],
  "claims": [{{"brand": "", "dimension": "", "claim": "", "value": ""}}]}}"""
        cres = self.claude.call(claim_prompt, max_tokens=1500, temperature=0.2)
        cres = cres if isinstance(cres, dict) else {}
        core_topic = str(cres.get("core_topic") or "").strip()
        products = [str(p).strip() for p in (cres.get("products") or []) if str(p).strip()]
        # FU198 — the article's SUBJECT (narrower than the brand category) + the mechanics that
        # define it. Both ride the SAME extraction call, so this costs nothing.
        _subject_x = str(cres.get("subject") or "").strip()
        self._core_mechanics = [str(m).strip() for m in (cres.get("core_mechanics") or [])
                                if str(m).strip()][:6]
        # FU198 — the SUBJECT anchor. Every competitor brief below was scoped to `cat` (a BRAND-level
        # field, identical for every article) or to nothing but the tool name, so for a brand broader
        # than one article the searches asked the wrong question and the comparison filled up with
        # off-topic credentials. INERT BY CONSTRUCTION: when the subject contributes no token that
        # `cat` does not already carry, `_subj_brief` is "" and every brief is byte-identical.
        _subject = _subject_x or re.sub(r"\s*[—–-]\s*.*$", "", core_topic).strip()
        _subject = re.sub(r"\s+", " ", _subject)[:80].strip()
        _cat_toks = set(_product_tokens(cat))
        _subj_toks = [t for t in _product_tokens(_subject) if t not in _cat_toks]
        _subj_brief = (f"; specifically their {_subject} work — any ranking, credential or documented "
                       f"capability IN {_subject}, NOT standing in their other practice/product areas"
                       if (_subj_toks and _subject) else "")
        if _subj_brief:
            print(f"[blog_gen] subject-scope: competitor briefs anchored on '{_subject}'", flush=True)
        # FU98 — peer-field check (warning only, rides the geo_warning toast): a "best/top <type>"
        # title where the comparison has <2 same-type competitors is the self-crowning pattern.
        self._peer_note = ""
        self._invented_note = ""   # FU184: competitors the MODEL named (not curated, not evidenced)
        # FU105: the same-type peer list rides into the reconcile (so its COMPETITOR FLOOR rule
        # knows WHICH tools are the protected peers), computed for EVERY seed, not just best/top.
        peers = [str(t).strip() for t in (cres.get("peer_tools") or [])
                 if str(t).strip() and str(t).strip().lower() != name.lower()]
        _m_bt = re.search(r"\b(?:best|top)\s+[\w /&-]*?(agencies|platforms|tools|companies|"
                          r"providers|services|firms|clinics|apps|software|retailers|vendors)\b",
                          seed or "", re.I)
        if _m_bt:
            _peers = peers
            if len(_peers) < 2:
                self._peer_note = (f"peer-check: 'best {_m_bt.group(1)}' title but only "
                                   f"{len(_peers)} same-type competitor(s) in the comparison — "
                                   f"possible self-crowning field; name real competing "
                                   f"{_m_bt.group(1)}")
                print(f"[blog_gen] {self._peer_note}", flush=True)
        tools = [str(t).strip() for t in (cres.get("tools") or [])
                 if str(t).strip() and str(t).strip().lower() != name.lower()]
        dims = [str(d).strip() for d in (cres.get("dimensions") or []) if str(d).strip()]
        claims = [c for c in (cres.get("claims") or []) if isinstance(c, dict)]
        # FU189: which compared entities are generic OPTIONS (no website) rather than PROVIDERS.
        _opt_names = [str(o).strip() for o in (cres.get("generic_options") or []) if str(o).strip()]
        # de-dupe tools (case-insensitive), then cap EACH KIND separately. A provider costs ~13
        # searches (domain hunt + tiers + key-fact rescue), an option costs ONE reference search, so
        # sharing a single cap let a website-less entity evict a real competitor from sourcing
        # entirely. Recombine in the ORIGINAL order — the finalize loop and the [S#] numbering both
        # walk `tools` in order.
        seen_t, tools_u = set(), []
        for t in tools:
            if t.lower() not in seen_t:
                seen_t.add(t.lower()); tools_u.append(t)
        _prov_q = [t for t in tools_u if not _named_as_option(t, _opt_names)][:_VERIFY_MAX_BRANDS]
        _opt_q = [t for t in tools_u if _named_as_option(t, _opt_names)][:_VERIFY_MAX_OPTIONS]
        _keep = {t.lower() for t in _prov_q} | {t.lower() for t in _opt_q}
        tools = [t for t in tools_u if t.lower() in _keep]
        _options = {t.lower() for t in _opt_q}   # grows in the loop via the products backstop
        if _options:
            print(f"[blog_gen] entity-kind: {len(_prov_q)} provider(s), {len(_opt_q)} generic "
                  f"option(s) ({', '.join(_opt_q)}) — options skip the vendor-site hunt", flush=True)

        # FU184 — flag a competitor the MODEL INVENTED to satisfy the >=3 floor. Phase (a) extracts
        # `tools` from the DRAFT BODY, so a freely-named brand becomes a sourcing target and earns
        # citations exactly like a curated one — nothing downstream can tell them apart, which is how
        # a peer that belongs to no list and no cache shipped in a live comparison. Classify each tool:
        #   curated        — slug-matches a name on the brand's stored competitor list
        #   evidence-backed — its distinctive token appears in a gathered evidence block
        #   invented       — neither  => surfaced to the operator on the geo_warning toast.
        # Warning ONLY: never drops a tool, never blocks a generation (the floor still stands).
        # CRITICAL: brand["competitors"] is a JSON STRING (db.get_brand returns dict(row) with no JSON
        # parsing) — read it with _as_list; list() would iterate CHARACTERS and mark every tool invented.
        _curated_slugs = {_kf_slug(c) for c in _as_list(brand.get("competitors")) if _kf_slug(c)}
        _ev_blob = " ".join(
            str((b or {}).get("text") or "") + " " + str((b or {}).get("label") or "")
            for b in (getattr(self, "_evidence_blocks", None) or [])).lower()
        def _is_curated(slug):
            # Exact slug, or the same vendor carrying a product/line suffix in one of the two names
            # ("Noom" on the operator's list vs "Noom Med" in the draft). The match must land on a
            # hyphen boundary, so a curated "Ro" never swallows an unrelated "Rory".
            return bool(slug) and any(
                slug == c or slug.startswith(c + "-") or c.startswith(slug + "-")
                for c in _curated_slugs)

        # FU199: a specialist the system itself found for this article's subject came from a system
        # call, not from thin air — flagging it as invented would be noise.
        _peer_slugs = {_kf_slug(n) for n in (getattr(self, "_subject_peers", None) or {}) if _kf_slug(n)}
        _invented = []
        for _t in tools:
            if _is_curated(_kf_slug(_t)) or _kf_slug(_t) in _peer_slugs:
                continue                                   # curated, or a found subject specialist
            _tok = (_t.lower().split() or [""])[0]
            if _tok and _tok in _ev_blob:
                continue                                   # evidence-backed
            _invented.append(_t)
        if _invented:
            _one = len(_invented) == 1
            self._invented_note = (
                "competitor-check: " + ", ".join(_invented) + (" was" if _one else " were") +
                f" named by the model, not from {name}'s competitor list or the gathered evidence — "
                f"verify {'it belongs' if _one else 'they belong'} in this comparison, or add/remove "
                f"in Edit Brand")
            print(f"[blog_gen] {self._invented_note}", flush=True)

        if not tools and not claims:
            return None  # nothing to source or check

        fresh = []   # each: {"label","url","text"} — appended to _evidence_blocks in [S#] order
        unsourced = []   # FU79: tools with ZERO evidence after all tiers+rescue — the pause trigger

        # PRIORITY ORDER (FU56): under a cost ceiling, secure the highest-VALUE sources FIRST so a budget
        # cutoff only ever drops the least-important searches. Rank: (c2) authoritative core-topic source →
        # (b) each competitor's OWN vendor page → (c) independent corroboration (least critical, cut first).

        # (c2) PRIMARY / OFFICIAL source for the article's CENTRAL factual claim — the single
        # highest-authority citation, so fetch it BEFORE spending budget on competitors. Labeled "official ·".
        core_q = core_topic or (seed or name)
        try:
            prim = self.claude.search_sources(
                f"the OFFICIAL primary source documenting {core_q} — the platform's / regulator's / "
                f"standard-body's OWN policy, documentation or help page (NOT a third-party blog or "
                f"review). Return the official page URL + the exact rule / requirement it states.",
                max_searches=2)
        except Exception:
            prim = []
        # FU141: this legacy search used to grant "official ·" to ANYTHING it returned — that is
        # how the client's own site and affiliate reviews got the badge. Validate before labeling:
        # pass → official; the authority NAMED in core_topic hosting the page on its own domain
        # (a platform's policy page on its .com) also counts; anything else → third-party ·
        # demotion (still usable), or dropped entirely on YMYL pages (clinical pages must not
        # carry affiliate noise under any label).
        _own_d = _norm_domain((brand or {}).get("domain_url") or "")
        _pins_c2 = (_YMYL_OFFICIAL_DOMAINS.get(ymyl) or []) if ymyl else []
        for c in (prim or [])[:2]:
            u = (c.get("url") or "").strip()
            ttl = (c.get("title") or "").strip()
            fct = (c.get("fact") or ttl or "").strip()
            if not (u and fct):
                continue
            ok_off = _official_source_ok(u, ttl, name, _own_d, _pins_c2)
            if not ok_off:
                d = _norm_domain(u)
                stem = d.split(".")[0] if d else ""
                if (len(stem) >= 4 and stem in (core_q or "").lower()
                        and not (_own_d and (d == _own_d or d.endswith("." + _own_d)))
                        and not _is_non_evidence({"title": ttl, "url": u})
                        and not _REVIEWISH_RE.search(ttl or "")):
                    ok_off = True
            if ok_off:
                fresh.append({"label": f"official · {ttl or u}", "url": u,
                              "text": fct[:_EVIDENCE_TEXT_CAP]})
            elif _is_subject_review({"title": ttl, "fact": fct}, name) \
                    or _is_negative_about({"title": ttl, "fact": fct}, name) \
                    or _is_affiliate_review({"title": ttl, "url": u}) \
                    or _is_non_capability_source({"title": ttl, "url": u}) \
                    or _is_offtopic_credential({"title": ttl, "url": u, "fact": fct}, _subj_toks):
                # FU150 (#2/#3): a demoted core-topic source that is a REVIEW OF or NEGATIVE about the
                # subject — or a low-quality affiliate review (FU161) — never becomes a third-party block.
                # FU198: nor a recruitment/encyclopedia page, nor a credential from a different
                # practice/product area — this DEMOTION path was the last way an off-subject ranking
                # could still reach the evidence after the competitor tiers had rejected it.
                print(f"[blog_gen] c2: '{(ttl or u)[:70]}' is a review-of / negative-about {name}, "
                      f"affiliate, non-capability or off-subject — dropped", flush=True)
            elif not ymyl:
                fresh.append({"label": f"{_source_class(u) or 'third-party'} · {ttl or u}", "url": u,
                              "text": fct[:_EVIDENCE_TEXT_CAP]})
            else:
                print(f"[blog_gen] c2: '{(ttl or u)[:70]}' failed official validation — "
                      f"dropped (ymyl page)", flush=True)

        # FU133 (c2b): YMYL pages need REAL authoritative grounding — regulator labels +
        # professional guidelines — retrieved with validation + retries. If nothing validated
        # comes back, add a PAUSE item so the operator supplies the official URL (or skips)
        # rather than shipping a clinical page cited to marketing sources.
        if ymyl:
            auth = self._gather_authoritative_sources(brand, seed, core_topic, ymyl,
                                                        products=products)
            fresh.extend(auth)
            # FU142: the pause now also fires when officials exist but NONE names the PRIMARY
            # product — six adjacent-molecule labels must never again count as "sourced".
            # Checked across ALL official blocks in fresh (the legacy c2 result included).
            _offs = [b for b in fresh if (b.get("label") or "").startswith("official ·")]
            _ptoks = getattr(self, "_auth_primary_tokens", None) or []
            _prim_ok = (not _ptoks) or any(
                any(t in ((b.get("label") or "") + " " + (b.get("url") or "") + " "
                          + (b.get("text") or "")).lower() for t in _ptoks)
                for b in _offs)
            if not _offs or not _prim_ok:
                _pname = getattr(self, "_auth_primary_name", "") or core_topic or seed
                unsourced.append({
                    "tool": f"official · {_pname}",
                    "facts": [f"regulator prescribing information / official documentation "
                              f"naming {_pname}",
                              "governing clinical or professional guideline"],
                    "ymyl_official": True,
                })
                _why = ("ZERO validated official sources" if not _offs else
                        f"no official source NAMES the primary product '{_pname}'")
                print(f"[blog_gen] ymyl-sources: {_why} after all retries — pause item added",
                      flush=True)

        # (b) SOURCE each named competitor tool from its OWN public pages (vendor domains ALLOWED)
        ctx = f"{cat} {seed}".strip()
        # FU90: for a geo blog the evidence must CARRY local-coverage facts, or the claims-gate strips
        # the geo points the article needs — so every brief also asks about the geography.
        geo_brief = (f"; availability, local coverage and compliance support in {rgeo} (local tax/"
                     f"payroll/regulatory support or market presence, as applicable)" if rgeo else "")
        # FU93: a qualifier page needs per-tool facts about the qualifier's mechanism too.
        qual_brief = (f"; {rqual} — terms, options and conditions offered" if rqual else "")
        # FU150 (Change 9) — VERTICAL-NEUTRAL, category-anchored (was music-specific): works for SaaS,
        # e-commerce, finance, legal, medical … not just creative tools.
        # FU156 — the article's PRIMARY product (a specific drug/product); anchor competitor pricing on
        # it so a competitor's price cell shows the MEDICATION/product cost, not a generic membership fee.
        _product = (products[0] if products else (core_topic or "")).strip()
        # FU161: the FULL list of the article's compared products (cap 3), so competitor + subject price
        # retrieval runs PER product — a multi-drug comparison (semaglutide + tirzepatide) fetches EACH
        # product's own-site price, not just products[0]. GENERAL — products are extracted per article.
        _products = [str(p).strip() for p in (products[:3] if products else []) if str(p).strip()] \
            or ([_product] if _product else [])
        # FU158/FU161: anchor competitor pricing on the vendor's OWN CURRENT price for EACH product, with
        # the basis it applies to (unit/tier/dose/supply) — generic across verticals.
        _prod_price_brief = (f"the CURRENT price/cost of {' and '.join(_products)} as listed on the "
                             f"vendor's OWN site (state EACH product's price), the plan / unit / tier / "
                             f"dose / supply it applies to and the billing basis; " if (_products and _px) else "")
        fetch_brief = (_prod_price_brief
                       + (f"pricing and plans; " if _px else "")   # FU162: no price ask when pricing is OFF
                       + f"commercial / license / eligibility / contract terms as "
                       f"applicable; the key capabilities and differentiators for "
                       f"{cat or 'this product/service'}; who it's best for"
                       + _subj_brief + geo_brief + qual_brief)   # FU198: subject-scoped
        # FU150 — TWO-PASS competitor sourcing so NO competitor is starved to zero purely by loop
        # position (the mass-pause root cause: the first 1-2 competitors used to burn the whole shared
        # web-search budget on Tier3+rescue, leaving later competitors with `_over_budget()`=True on
        # every call → zero blocks → the FU79 pause on facts that were actually public). Shared closures
        # (were per-iteration), then Pass 0 (cheap batched domain resolve) → Pass A (cheap first-party
        # baseline for EVERY tool) → Pass B (rescue, zero-block tools FIRST) → finalize in original order.

        def _same_site(u, dom):
            """True when url u is the tool's OWN site (its registrable domain or a subdomain)."""
            dd = _dom(dom)
            du = _dom(u)
            return bool(dd) and du and (du == dd or du.endswith("." + dd))

        def _blocks_from(srcs, tool, dom, cap=_MAX_TOOL_PAGES):
            """Per-page blocks with HONEST labels (FU53): the tool's OWN domain → first-party (tool
            name); any other domain → 'third-party · <title>'. Deduped by url. FU150: drops any source
            that is NEGATIVE about the SUBJECT brand (#2)."""
            out_, seen_ = [], set()
            for s in (srcs or []):
                if _is_non_evidence(s):   # FU93/FU140: hit-pieces + job listings never get cited
                    print(f"[blog_gen] source-hygiene: dropped hit-piece "
                          f"'{(s.get('title') or '')[:70]}'", flush=True)
                    continue
                if _is_negative_about(s, name):   # FU150 (#2): never cite anything negative about {name}
                    print(f"[blog_gen] source-hygiene: dropped negative-about-brand "
                          f"'{(s.get('title') or '')[:70]}'", flush=True)
                    continue
                # FU161: drop a low-quality AFFILIATE / SEO review of a competitor (own-site + reputable
                # + official always survive) so the vendor's own price / reputable coverage is used.
                if not _same_site(s.get("url") or "", dom) and _is_affiliate_review(s, _dom(dom)):
                    print(f"[blog_gen] source-hygiene: dropped affiliate review "
                          f"'{(s.get('title') or '')[:70]}'", flush=True)
                    continue
                # FU198: a recruitment/encyclopedia page evidences no capability, and a credential
                # from a DIFFERENT practice/product area is not evidence for THIS comparison.
                if _is_non_capability_source(s):
                    print(f"[blog_gen] source-hygiene: dropped non-capability source "
                          f"'{(s.get('title') or '')[:70]}'", flush=True)
                    continue
                if _is_offtopic_credential(s, _subj_toks):
                    print(f"[blog_gen] source-hygiene: dropped off-subject credential "
                          f"'{(s.get('title') or '')[:70]}'", flush=True)
                    continue
                u = (s.get("url") or "").strip()
                fc = (s.get("fact") or s.get("title") or "").strip()
                key = u.lower().split("?")[0].rstrip("/")
                if not (u and fc) or key in seen_:
                    continue
                seen_.add(key)
                label = tool if _same_site(u, dom) else \
                    f"{_source_class(u) or 'third-party'} · {(s.get('title') or _dom(u) or 'review')}"
                out_.append({"label": label, "url": u, "text": fc[:_EVIDENCE_TEXT_CAP]})
                if len(out_) >= cap:
                    break
            return out_

        def _has_vendor(blocks, dom):
            return any(_same_site(b["url"], dom) for b in (blocks or []))

        # FU150 (Change 9) — the rescued KEY facts are DYNAMIC, not a fixed price+license: always
        # `price` (near-universal), but only chase `license` when the article actually COMPARES a
        # license/commercial/terms/royalty dimension. So a loans / HR-software blog (no license
        # column) never burns rescue searches — or mis-flags a "missing" fact — for a fact that
        # doesn't exist in its vertical.
        _active_facts = {"price"} if _px else set()   # FU162: pricing OFF ⇒ never chase/rescue a price fact
        if any(_LICENSE_DIM_RE.search(d or "") for d in dims):
            _active_facts.add("license")

        def _missing_facts(blocks):
            t = " ".join(b.get("text", "") for b in (blocks or []))
            return [k for k in _active_facts if not _FACT_SIGNALS[k].search(t)]

        def _fetch_product_price(dd, brand_name, product):
            """FU161: web_search READS a vendor page even when a direct fetch 403s — fetch the OWN site
            for THIS product's price when the search snippet omitted it (the /product/glp1m2m/ case).
            Returns {url, fact} (product-labeled) or None."""
            try:
                fj = self.claude.fetch_site_facts(
                    dd, brand_name, f"{product} price/pricing — the plan/dose and exact monthly cost",
                    max_searches=2) or ""
            except Exception:
                fj = ""
            if fj.strip() and _PRICE_SIGNAL_RE.search(fj):
                return {"url": f"https://{dd}", "fact": f"{product}: " + fj.strip()}
            return None

        def _tier1(tool, dom):
            # web search PINNED to the tool's OWN domain → SPECIFIC pages (pricing/terms) with urls.
            if not dom:
                return []
            _dd = _dom(dom)
            try:
                pin = self.claude.search_sources(
                    # FU161: product-anchored on ALL the article's products so the search returns EACH
                    # product's own pricing page; FU158: the vendor's OWN CURRENT price + unit/tier/dose.
                    (f"{tool} {' and '.join(_products)} CURRENT price/cost on {tool}'s own site (EACH "
                     f"product's price, the unit/tier/dose/supply it applies to); " if (_products and _px) else "")
                    + f"{tool}: " + ("pricing and plans, " if _px else "")   # FU162: no price ask when OFF
                    + f"commercial-use / licensing / royalty-free terms, key capabilities"
                    + (f", availability / coverage / compliance support in {rgeo}" if rgeo else "")
                    + (f", {rqual} terms/options offered" if rqual else "")   # FU93
                    + _subj_brief,   # FU198: the article's subject, not the brand's whole category
                    max_searches=2, allowed_domains=[_dd], first_party=True)   # FU55
            except Exception:
                pin = []
            blocks = _blocks_from(pin, tool, dom)
            # FU161: pick EACH product's own-domain priced page from the SAME results (STRICT token gate —
            # right product's page, and a DIFFERENT product's price still rejected). The PRIMARY product
            # (index 0) gets the full fallback ladder (one targeted own-domain search → fetch-the-page);
            # secondary products are selected free from the general results (cost bound).
            if _px:   # FU162: pricing OFF ⇒ no per-product price selection/fetch for this competitor
                for _i, prod in enumerate(_products or ([_product] if _product else [])):
                    if not prod:
                        continue
                    pp = _best_product_price(pin, prod, _dd)
                    if not pp and _i == 0 and _dd:
                        try:
                            res2 = self.claude.search_sources(
                                f"{tool} {prod} price/pricing — the plan/dose/tier and exact cost, from "
                                f"{tool}'s OWN official site only", max_searches=1,
                                allowed_domains=[_dd], first_party=True)
                        except Exception:
                            res2 = []
                        pp = _best_product_price(res2, prod, _dd) or _fetch_product_price(_dd, tool, prod)
                    if pp and not any((b.get("url") or "") == pp["url"] for b in blocks):
                        _ptxt = (pp.get("fact") or "")[:_EVIDENCE_TEXT_CAP]
                        if prod.lower() not in _ptxt.lower():
                            _ptxt = f"{prod}: {_ptxt}"   # FU161: label the block product-wise (per-product cache)
                        blocks.insert(0, {"label": tool, "url": pp["url"], "text": _ptxt})
            return blocks

        def _tier2(tool, dom):
            # fetch_site_facts — the IP-INDEPENDENT web-search-pinned vendor path.
            if not dom:
                return None
            try:
                fj = self.claude.fetch_site_facts(dom, tool, fetch_brief, max_searches=2) or ""
            except Exception:
                fj = ""
            if fj.strip():
                return {"label": tool, "url": f"https://{_dom(dom)}", "text": fj[:_EVIDENCE_TEXT_CAP]}
            return None

        def _baseline(tool, dom):
            """Cheap first-party baseline: Tier1 pinned + Tier2 fallback. Returns (blocks, t1, t2)."""
            blocks = _tier1(tool, dom)
            t1 = len(blocks)
            t2 = False
            if dom and not _has_vendor(blocks, dom):
                b2 = _tier2(tool, dom)
                if b2:
                    blocks.append(b2)
                    t2 = True
            return blocks, t1, t2

        # ---- FU151 (A): COMPETITOR-FACT CACHE read — reuse a competitor's sourced blocks across the
        # brand's blogs within a TTL, so we don't re-buy the ~90%-of-cost competitor sourcing every blog
        # and a competitor's facts stay consistent cluster-wide. A cache HIT skips domain-resolve +
        # Tier1/2/3 + rescue entirely for that tool; only MISSING/STALE competitors are sourced LIVE.
        try:
            _cfacts = json.loads((brand or {}).get("competitor_facts") or "{}")
        except Exception:
            _cfacts = {}
        if not isinstance(_cfacts, dict):
            _cfacts = {}
        import calendar as _cal
        _now_ts = time.time()

        def _cf_fresh(entry):
            va = (entry or {}).get("verified_at")
            if not va:
                return False
            try:
                return (_now_ts - _cal.timegm(time.strptime(va, "%Y-%m-%dT%H:%M:%SZ"))) \
                    <= _COMPETITOR_FACTS_TTL_DAYS * 86400
            except Exception:
                return False
        _cached_tools = {}   # tool -> its cached blocks (fresh + non-empty)
        # FU160: PER-SLUG bypass — refresh_competitor_facts=True (all) OR a competitor's slug in the
        # selected `refresh_competitor_slugs` set → skip the cache for THAT competitor (re-source live);
        # the rest still take their cached blocks. The UI sends the exact cache-key slugs.
        _refresh_slugs = {str(s).strip().lower() for s in (refresh_competitor_slugs or []) if str(s).strip()}
        for tool in tools:
            if refresh_competitor_facts or _kf_slug(tool) in _refresh_slugs:
                continue
            _e = _cfacts.get(_kf_slug(tool))
            if isinstance(_e, dict) and _cf_fresh(_e):
                _blks = [b for b in (_e.get("blocks") or [])
                         if isinstance(b, dict) and (b.get("text") or "").strip() and b.get("label")]
                if _blks:
                    _cached_tools[tool] = _blks
        if _cached_tools:
            print(f"[blog_gen] competitor-cache: HIT {len(_cached_tools)}/{len(tools)} — "
                  f"{', '.join(_cached_tools)} (no re-sourcing)", flush=True)
        _live_tools = [t for t in tools if t not in _cached_tools]
        _cf_dirty = False

        # ---- Pass 0: batched domain pre-resolve (ONE cheap non-search LLM call; ~free) ----
        # Seed from the brand's FU54 web-verified competitor_domains cache first (neutralizes the
        # same-name-domain risk without a web search), then resolve the rest in one batched call.
        # Only the LIVE (uncached) tools need domain resolution.
        try:
            _cached = json.loads((brand or {}).get("competitor_domains") or "{}")
        except Exception:
            _cached = {}
        _cached_lc = {}
        if isinstance(_cached, dict):
            for k, v in _cached.items():
                dk = _dom(v)
                if str(k).strip() and dk:
                    _cached_lc[str(k).strip().lower()] = dk
        dom_map = {t: _cached_lc[t.lower()] for t in _live_tools if t.lower() in _cached_lc}
        # FU189: a generic OPTION has no website, so there is nothing to resolve for it. Asking anyway
        # is the first step of a ~13-search hunt that cannot succeed, and whose only "success" mode is
        # binding an unrelated site and labelling its pages as the entity's own.
        _need_dom = [t for t in _live_tools
                     if t not in dom_map and t.lower() not in _options]
        if _need_dom:
            try:
                _resolved = self._resolve_brand_domains(_need_dom, seed=seed, subject=name,
                                                        subject_category=cat)
            except Exception:
                _resolved = {}
            _res_lc = {str(k).strip().lower(): _dom(v) for k, v in (_resolved or {}).items() if _dom(v)}
            for t in _need_dom:
                if t.lower() in _res_lc:
                    dom_map[t] = _res_lc[t.lower()]
        def _is_option(t):
            """FU189 — PROVIDER (fetch its site) or generic OPTION (reference material)? The model's
            own GENERIC_OPTIONS list decides; the products BACKSTOP only fires for an entity that ALSO
            failed to resolve a domain, so a real provider whose name merely shares a token with a
            product can never be misrouted."""
            if t.lower() in _options:
                return True
            if not dom_map.get(t) and _matches_a_product(t, products):
                _options.add(t.lower())
                print(f"[blog_gen] entity-kind: {t} -> generic OPTION (matches a PRODUCT and has no "
                      f"resolvable domain)", flush=True)
                return True
            return False

        def _option_keep(t, blob):
            """Keep test for an option's reference results. Tier 3 / the FU78 rescue demand the entity's
            LITERAL full string, which no real page contains — that is WHY the vendor hunt yields zero.
            Use the SHORTEST reading's distinctive tokens instead ("TRT" from "TRT (Testosterone
            Replacement Therapy)", both of "term loan"), and require all of them."""
            best = None
            for f in _opt_forms(t):
                toks = [x for x in _product_tokens(f) if len(x) >= 2]
                if toks and (best is None or len(toks) < len(best)):
                    best = toks
            return bool(best) and all(x in blob for x in best)

        print(f"[blog_gen] verify+complete: pre-resolved {len(dom_map)}/{len(tools)} competitor "
              f"domain(s) (cache+batch)", flush=True)

        # ---- Pass A: cheap first-party BASELINE for EVERY (LIVE) tool (Tier1+Tier2, no rescue) ----
        # Runs before any competitor consumes the expensive rescue budget, so no tool is starved.
        # A CACHED tool takes its blocks from the cache and skips sourcing entirely. FU151 (B): the
        # LIVE baselines are sourced CONCURRENTLY — each is independent and writes a distinct
        # tool_state key (no shared-list race); the finalize loop still emits `fresh` in ORIGINAL order.
        tool_state = {}
        for tool in tools:
            if tool in _cached_tools:
                tool_state[tool] = {"dom": "", "blocks": list(_cached_tools[tool]),
                                    "t1": 0, "t2": 0, "t3": 0, "rescue": 0, "cached": True}

        def _do_baseline(tool):
            dom = dom_map.get(tool, "")
            blocks, t1, t2 = _baseline(tool, dom)
            return tool, {"dom": dom, "blocks": blocks, "t1": t1, "t2": t2, "t3": 0, "rescue": 0}
        _live_baseline = [t for t in tools if t not in _cached_tools]
        if _live_baseline:
            with ThreadPoolExecutor(max_workers=min(_BLOG_FETCH_WORKERS, len(_live_baseline))) as _ex:
                for _tool, _st in _ex.map(_do_baseline, _live_baseline):
                    tool_state[_tool] = _st

        # ---- Pass B: rescue what still needs it, ZERO-BLOCK tools FIRST, then missing-key-fact ----
        def _rescue_prio(t):
            st = tool_state[t]
            if not st["blocks"]:
                return 0                                   # zero-block — highest priority
            return 1 if _missing_facts(st["blocks"]) else 2   # 2 = fully sourced, skip
        def _do_rescue(tool):
            st = tool_state[tool]
            if st.get("cached"):
                return                                     # FU151: cached facts are trusted as-is
            if _rescue_prio(tool) == 2:
                return                                     # nothing missing — no rescue spend
            dom = st["dom"]
            blocks = list(st["blocks"])
            # FU189 — a GENERIC OPTION is not a company. The vendor hunt below (domain resolve → Tier 3
            # → key-fact rescue) costs ~13 searches that CANNOT yield for it, spends them FIRST because
            # a zero-block entity is priority 0, and its only "success" mode is binding an unrelated
            # domain and labelling that site's pages as the entity's own. ONE reference search instead.
            if _is_option(tool):
                _want = "; ".join([d for d in dims if d.strip()][:4]) or "how it works and what it costs"
                try:
                    rs = self.claude.search_sources(
                        f"{tool} in the context of {cat or 'this category'}: what it is, how it works, "
                        f"and the specific current values for {_want} — from AUTHORITATIVE or REFERENCE "
                        f"sources (a regulator, a standards body, manufacturer or product documentation, "
                        f"professional or industry guidance, or a reputable independent publication), "
                        f"NOT a vendor sales page", max_searches=1)
                except Exception:
                    rs = []
                for _s in (rs or []):
                    u, fct = (_s.get("url") or "").strip(), (_s.get("fact") or "").strip()
                    blob = ((_s.get("title") or "") + " " + fct + " " + u).lower()
                    if (u and fct and _option_keep(tool, blob) and not _is_non_evidence(_s)
                            and _dom(u) not in _STALE_AGGREGATORS):
                        blocks.append({"label": f"reference · {(_s.get('title') or _dom(u))[:70]}",
                                       "url": u, "text": fct[:_EVIDENCE_TEXT_CAP]})
                st["blocks"] = blocks
                st["t3"] = len(blocks)
                st["option"] = True
                print(f"[blog_gen] entity-kind: {tool} = generic OPTION -> 1 reference search, "
                      f"{len(blocks)} block(s) kept (vendor hunt skipped)", flush=True)
                return
            # (i) still no domain → resolve via web search, then retry the baseline
            if not dom:
                try:
                    dom = self.claude.find_official_domain(tool, ctx) or ""
                except Exception:
                    dom = ""
                if not dom:
                    try:
                        cand = self.claude.search_sources(
                            f"the OFFICIAL website (its own product homepage) of {tool} "
                            f"({cat or 'the tool'}) — NOT a review site, app store, or directory",
                            max_searches=2)
                    except Exception:
                        cand = []
                    for s in (cand or []):
                        d = _norm_domain(s.get("url"))
                        if d and d not in _THIRD_PARTY_DOMAINS and d not in _STALE_AGGREGATORS:
                            dom = d
                            break
                st["dom"] = dom
                if dom:
                    nb, nt1, nt2 = _baseline(tool, dom)
                    _seen = {b["url"].lower().split("?")[0].rstrip("/") for b in blocks}
                    for b in nb:
                        if b["url"].lower().split("?")[0].rstrip("/") not in _seen:
                            blocks.append(b)
                    st["t1"] = max(st["t1"], nt1)
                    st["t2"] = st["t2"] or nt2
            # (ii) Tier 3 broad — ONLY when no VENDOR block exists (prefer own site, then reputable
            # review; a stale aggregator/random blog is last, honestly labeled 'third-party ·').
            if not _has_vendor(blocks, dom):
                try:
                    br = self.claude.search_sources(
                        (f"{tool} {_product} price/cost; " if (_product and _px) else "")   # FU156/162: product-anchored, only when pricing ON
                        + f"{tool} ({cat}) official " + ("pricing and plans, " if _px else "")
                        + f"commercial-use / licensing / "
                        f"royalty-free terms, key capabilities — prefer its OWN site or a reputable review "
                        f"(G2 / Capterra / Trustpilot / TechCrunch / The Verge)"
                        + _subj_brief, max_searches=2)   # FU198: subject-scoped
                except Exception:
                    br = []
                own = [s for s in (br or []) if _same_site(s.get("url"), dom)]
                reputable = [s for s in (br or []) if _dom(s.get("url")) in _THIRD_PARTY_DOMAINS]
                named = [s for s in (br or [])
                         if tool.lower() in ((s.get("title") or "") + " " + (s.get("fact") or "")).lower()
                         and _dom(s.get("url")) not in _STALE_AGGREGATORS]   # FU54: downrank aggregators
                added = _blocks_from(own or reputable or named, tool, dom)
                st["t3"] = len(added)
                blocks = blocks + added
            # (iii) FU78 key-fact rescue — while a KEY fact (price/license) is still missing, escalate
            # with up to _FACT_RESCUE_TRIES BROAD searches TARGETING it, keeping only fact-carrying hits.
            rescue_tries = 0
            while rescue_tries < _FACT_RESCUE_TRIES and _missing_facts(blocks):
                miss = _missing_facts(blocks)
                wants = []
                if "price" in miss:
                    wants.append("pricing — plan names and the exact monthly cost (e.g. $X/month), any free tier")
                if "license" in miss:
                    wants.append("commercial-use / license / key terms — is commercial use allowed, "
                                 "what's included or restricted, and the contract/usage terms")
                # FU198: this was the loosest brief in the file — tool name and nothing else.
                brief = (f"{tool}: {'; '.join(wants) or 'pricing and key terms'} — the exact, current "
                         f"facts" + _subj_brief)
                rescue_tries += 1
                try:
                    rsc = self.claude.search_sources(brief, max_searches=2)
                except Exception:
                    rsc = []
                cand = [s for s in (rsc or [])
                        if tool.lower() in ((s.get("title") or "") + " " + (s.get("fact") or "")).lower()
                        and _dom(s.get("url")) not in _STALE_AGGREGATORS
                        and any(_FACT_SIGNALS[k].search(s.get("fact") or "") for k in miss)]
                add = _blocks_from(cand, tool, dom)
                if add:
                    blocks = blocks + add
            st["rescue"] = rescue_tries
            # (iv) FU90 geo rescue — a geo blog needs per-tool LOCAL coverage facts.
            if rgeo and blocks:
                _geo_key = re.sub(r"^the\s+", "", rgeo, flags=re.I)
                _geo_rx = re.compile(r"\b" + re.escape(_geo_key) + r"\b", re.I)
                if not any(_geo_rx.search(b.get("text") or "") for b in blocks):
                    try:
                        gsc = self.claude.search_sources(
                            f"{tool}: availability, operations, local coverage and compliance support "
                            f"in {rgeo}", max_searches=2)
                    except Exception:
                        gsc = []
                    gcand = [s for s in (gsc or [])
                             if tool.lower() in ((s.get("title") or "") + " "
                                                 + (s.get("fact") or "")).lower()
                             and _geo_rx.search((s.get("fact") or "") + " " + (s.get("title") or ""))
                             and _dom(s.get("url")) not in _STALE_AGGREGATORS]
                    gadd = _blocks_from(gcand, tool, dom)
                    if gadd:
                        blocks = blocks + gadd
            st["blocks"] = blocks

        # FU159: rescue each tool CONCURRENTLY — each _do_rescue writes only its OWN tool_state[tool]
        # (no shared-list race), so this adds ZERO searches (same set, in parallel) and the finalize loop
        # below still emits `fresh` in ORIGINAL `tools` order → [S#] numbering byte-identical to serial.
        # Zero-block tools are submitted first so a tight budget still favors them.
        _rescue_live = [t for t in sorted(tools, key=_rescue_prio)
                        if not tool_state[t].get("cached") and _rescue_prio(t) != 2]
        if _rescue_live:
            with ThreadPoolExecutor(max_workers=min(_BLOG_FETCH_WORKERS, len(_rescue_live))) as _ex:
                list(_ex.map(_do_rescue, _rescue_live))

        # ---- Finalize: emit in ORIGINAL order (keeps each tool's [S#] blocks contiguous) ----
        for tool in tools:
            st = tool_state[tool]
            blocks = st["blocks"]
            if blocks:
                fresh.extend(blocks)
                # FU151 (A): write LIVE-sourced competitors back to the per-brand cache (skip cache hits).
                if not st.get("cached"):
                    _cfacts[_kf_slug(tool)] = {
                        "domain": st.get("dom") or "",
                        "blocks": [{"label": b["label"], "url": b["url"], "text": b["text"]}
                                   for b in blocks[:_MAX_TOOL_PAGES * 2]],
                        "verified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
                    _cf_dirty = True
                print(f"[blog_gen] verify+complete: {tool} dom={st['dom'] or '∅'} "
                      f"{'(cache) ' if st.get('cached') else ''}"
                      f"t1={st['t1']} t2={int(st['t2'])} t3={st['t3']} rescue={st['rescue']} "
                      f"missing={','.join(_missing_facts(blocks)) or 'none'} -> "
                      f"{'vendor' if _has_vendor(blocks, st['dom']) else 'third-party'} <- "
                      f"{', '.join(b['url'] for b in blocks)}", flush=True)
                # FU161: when the article compares PRICING and this competitor has NO price from its OWN
                # site OR a reputable source (only affiliate-dropped / nothing), FLAG-AND-ASK the operator
                # (the FU79 pause) rather than ship an honest/blank cell — user decision.
                # FU204 (6b): the `not st.get("cached")` gate is GONE. A competitor whose facts came
                # from the FU151 45-day cache could never trigger the ask, so on the SECOND blog for a
                # brand the question silently stopped being asked. The check is pure inspection of
                # blocks already in hand (the FU157 rationale) — it costs nothing to run on them too.
                if _px and any(_PRICE_DIM_RE.search(d or "") for d in dims):
                    if not self._has_confirmed_price(blocks, st.get("dom")):
                        unsourced.append({"tool": tool, "dom": st.get("dom") or "",
                                          "facts": ["current price"], "price_only": True})
                        print(f"[blog_gen] price-check: {tool} has no confirmed own-site/reputable price "
                              f"— FU79 will ask the operator", flush=True)
            else:
                # FU150: the pause names the actual comparison COLUMNS the operator must supply — a
                # zero-block tool is missing EVERY dimension — not the old static "price + license".
                _mf = [d for d in dims if d.strip()] or _missing_facts([])
                if st.get("option"):
                    # FU189: a generic OPTION has no page, so demanding "paste a link to its page" is
                    # asking for something that cannot exist. The PAUSE stays (it is the ONLY way to
                    # rescue the row — `finish_pending_blog` turns a typed fact into the TOOL-LABELED
                    # block the reconcile needs), but the ask changes to typed facts only.
                    unsourced.append({"tool": tool, "facts": _mf, "generic_option": True})
                else:
                    unsourced.append({"tool": tool, "dom": st["dom"] or "", "facts": _mf})
                print(f"[blog_gen] verify+complete: {tool} dom={st['dom'] or '∅'} t1=0 t2=0 t3=0 -> none "
                      f"(could NOT source — FU79 will PAUSE & ask for a manual link/fact)", flush=True)

        # FU151 (A): persist the refreshed competitor-fact cache (best-effort; prune to the cap,
        # keeping the most-recently-verified entries).
        if _cf_dirty and (brand or {}).get("id") is not None and getattr(self, "db", None):
            try:
                if len(_cfacts) > _COMPETITOR_FACTS_CAP:
                    _cfacts = dict(sorted(_cfacts.items(),
                                          key=lambda kv: (kv[1] or {}).get("verified_at") or "",
                                          reverse=True)[:_COMPETITOR_FACTS_CAP])
                self.db.update_brand(brand["id"], competitor_facts=json.dumps(_cfacts))
            except Exception as e:
                print(f"[blog_gen] competitor-cache: persist failed: {e}", flush=True)

        # FU139 — DIMENSION RESCUE ("try harder", user directive): the FU78 rescue covers only
        # price/license; every OTHER extracted comparison dimension (eligibility, delivery,
        # audience focus, …) had no targeted retry, so its cells starved and FU138's resolver
        # dropped the column. Before accepting any gap: for each (tool × dimension) pair with NO
        # signal in the gathered facts, run ONE targeted search — bounded by _DIM_RESCUE_BUDGET,
        # prioritized by the dimensions closest to a column-drop (most tools missing), every
        # attempt logged. Only results that actually NAME the tool are kept (anti-drift).
        def _dim_words(d):
            return [w for w in re.findall(r"[a-z0-9]{3,}", (d or "").lower())
                    if w not in ("the", "and", "for", "with", "per", "key")]

        # FU142 — the SUBJECT joins the rescue: its cells previously had NO rescue path at all
        # (the FU78/FU139 rescues iterate `tools`, which excludes {name} by design), so the
        # publisher's own pricing shipped as "—" beside competitors' real numbers — the worst
        # kind of blank. Subject pairs sort FIRST and its searches prefer its own site.
        own_dom_s = _dom(brand.get("domain_url") or "")
        tool_texts = {}
        subj_txt = ""
        for blk in (getattr(self, "_evidence_blocks", None) or []):
            lbl0 = str(blk.get("label") or "")
            bd0 = _dom(blk.get("url"))
            if lbl0.strip().lower() == name.strip().lower() or (
                    own_dom_s and bd0 and (bd0 == own_dom_s or bd0.endswith("." + own_dom_s))):
                subj_txt += " " + str(blk.get("text") or "").lower()
        tool_texts[name] = subj_txt
        for f in fresh:
            lbl = str(f.get("label") or "")
            for t in [name] + tools:
                if t.lower() in lbl.lower():
                    tool_texts[t] = tool_texts.get(t, "") + " " + str(f.get("text") or "").lower()

        # FU158/FU161 — SUBJECT pricing is OPERATOR-SET-FIRST, honest fallback (the operator's canonical
        # values are authoritative; auto-fetch is unreliable). (1) inject EVERY operator-set item
        # (MULTI-PRODUCT — tirzepatide AND semaglutide both appear when both are set); (2) for an article
        # product WITHOUT an operator value, do a PRODUCT-ANCHORED own-domain search + fetch-the-page;
        # (3) if still unconfirmed → honest cell + a `_price_warn` (never a shaky affiliate number). The
        # subject is ALWAYS excluded from the generic price dim-rescue below (it grabbed the wrong page).
        try:
            _kf = json.loads(brand.get("key_facts") or "{}")
        except Exception:
            _kf = {}
        _op_items = ([it for it in _kf_pricing_items(_kf)
                      if it.get("operator_set") and str(it.get("value") or "").strip()]
                     if _px else [])   # FU162: pricing OFF ⇒ inject no operator price
        for _it in _op_items:
            _cv = str(_it.get("value")).strip()
            _cp = str(_it.get("product") or "").strip()
            _curl = (_it.get("source_url") or "").strip() or (f"https://{own_dom_s}" if own_dom_s else "")
            _ctext = (f"{_cp} pricing: {_cv} (per {name}'s own site)" if _cp
                      else f"pricing: {_cv} (per {name}'s own site)")
            if not any(str(b.get("label") or "").strip().lower() == name.strip().lower()
                       and _cv[:12].lower() in str(b.get("text") or "").lower() for b in fresh):
                fresh.append({"label": name, "url": _curl, "text": _ctext[:_EVIDENCE_TEXT_CAP]})
            tool_texts[name] = tool_texts.get(name, "") + " pricing price cost fee plan " + _cv.lower()
        # FU177/FU178 — the operator's CANONICAL BRAND FACTS. A fact typed into a box is an ASSERTION,
        # not a source, so it earns a [S#] ONLY when a real page can be shown to state it. Three tiers:
        #   1 VERIFIED         — its anchors appear in a page we already fetched → cite THAT page.
        #   2 OPERATOR-SOURCED — the operator gave a URL → fetch it once and verify → cite it.
        #   3 UNVERIFIED       — neither → NO evidence block. The fact still reaches the writer via
        #                        _canonical_facts_block and is stated as {name}'s own POSITIONING
        #                        (attributed, uncited) — the escape verify_claims already allows for
        #                        the subject. Minting `[S4] Acme — https://acme.com` for a sentence
        #                        that may appear nowhere on acme.com is the failure this replaces.
        # tool_texts is fed in ALL three tiers: it drives the dimension-rescue coverage test, which is
        # about what the subject has already said, not about citability.
        _fact_tiers = {"verified": 0, "operator": 0, "unverified": 0}
        _unverified_facts, _fact_fetches = [], 0
        for _fi in _kf_fact_items(_kf):
            _fv = _fi["value"]
            _fl = _fi.get("label") or ""
            _ftext = (f"{_fl}: {_fv} (per {name}'s own site)" if _fl
                      else f"{_fv} (per {name}'s own site)")
            tool_texts[name] = tool_texts.get(name, "") + " " + (_fl + " " + _fv).lower()
            # tier 1 — already-fetched first-party page that states it
            _src = ""
            for _blk in (getattr(self, "_evidence_blocks", None) or []):
                _bd = _dom(_blk.get("url"))
                if not (str(_blk.get("label") or "").strip().lower() == name.strip().lower()
                        or (own_dom_s and _bd and (_bd == own_dom_s or _bd.endswith("." + own_dom_s)))):
                    continue
                if _blk.get("url") and _fact_stated_in(_fv, _blk.get("text")):
                    _src = _blk["url"]
                    break
            _tier = "verified" if _src else ""
            # tier 2 — the operator's own URL, FETCHED and checked (never cited unread)
            _ou = (_fi.get("source_url") or "").strip()
            if not _src and _ou and _fact_fetches < _FACT_VERIFY_FETCHES:
                _fact_fetches += 1
                try:
                    if _fact_stated_in(_fv, _extract_visible_text(_fetch_homepage(_ou))):
                        _src, _tier = _ou, "operator"
                except Exception as _e:
                    print(f"[blog_gen] brand-facts: could not read {_ou} ({_e})", flush=True)
            if _src:
                _fact_tiers[_tier] += 1
                if not any(str(b.get("label") or "").strip().lower() == name.strip().lower()
                           and _fv[:24].lower() in str(b.get("text") or "").lower() for b in fresh):
                    fresh.append({"label": name, "url": _src, "text": _ftext[:_EVIDENCE_TEXT_CAP]})
            else:
                _fact_tiers["unverified"] += 1
                _unverified_facts.append(f"{_fl}: {_fv}" if _fl else _fv)
        self._facts_note = ""
        if any(_fact_tiers.values()):
            print(f"[blog_gen] brand-facts: {_fact_tiers['verified']} verified, "
                  f"{_fact_tiers['operator']} operator-sourced, {_fact_tiers['unverified']} unverified",
                  flush=True)
            if _fact_tiers["unverified"]:
                self._facts_note = (
                    f"brand-facts: {_fact_tiers['unverified']} of "
                    f"{sum(_fact_tiers.values())} canonical fact(s) could not be found on {name}'s own "
                    f"site — stated as {name}'s positioning, NOT cited; add the page URL in Edit Brand "
                    f"to have them cited")

        _op_prod_slugs = {_kf_slug(it.get("product")) for it in _op_items}
        _subj_unpriced = []
        for prod in (_products if (own_dom_s and _px) else []):   # FU162: no subject price search when OFF
            if not prod or _kf_slug(prod) in _op_prod_slugs:
                continue   # operator value already injected — authoritative, don't auto-fetch
            _ptoks = _product_tokens(prod)
            _stxt = tool_texts.get(name, "")
            if _ptoks and any(t in _stxt for t in _ptoks) and _PRICE_SIGNAL_RE.search(_stxt):
                continue   # already in the subject's gathered evidence
            try:
                rs = self.claude.search_sources(
                    f"{name} {prod} price/pricing — the plan/dose and exact cost, from {name}'s OWN "
                    f"official site only", max_searches=1, allowed_domains=[own_dom_s], first_party=True)
            except Exception:
                rs = []
            pp = _best_product_price(rs, prod, own_dom_s) or _fetch_product_price(own_dom_s, name, prod)
            if pp:
                fresh.append({"label": name, "url": pp["url"],
                              "text": (f"{prod}: " + (pp.get("fact") or ""))[:_EVIDENCE_TEXT_CAP]})
                tool_texts[name] = tool_texts.get(name, "") + " pricing price cost " + (pp.get("fact") or "").lower()
            else:
                _subj_unpriced.append(prod)
        if _subj_unpriced:
            self._price_warn = (f"{name}'s price for {', '.join(_subj_unpriced)} could not be confirmed "
                                f"from its own site — set it in Edit Brand → Canonical pricing, else the "
                                f"cell stays honest (\"see {name}'s site\").")
            print(f"[blog_gen] subject-price: unconfirmed for {', '.join(_subj_unpriced)}", flush=True)

        _pairs = []
        for d in dims:
            ws = _dim_words(d)
            if not ws:
                continue
            # FU161: the SUBJECT is always handled by the per-product price fetch above — never by the
            # generic own-domain price dim-rescue (which grabbed the first "{name}" page, e.g. /how-it-works/).
            if _PRICE_DIM_RE.search(d or ""):
                if not _px:   # FU162: pricing OFF ⇒ no price-dimension rescue at all
                    continue
                miss = [t for t in tools
                        if not any(w in tool_texts.get(t, "") for w in ws)]
            else:
                miss = [t for t in [name] + tools
                        if not any(w in tool_texts.get(t, "") for w in ws)]
            for t in miss:
                _pairs.append((1 if t == name else 0, len(miss), d, t))
        _pairs.sort(key=lambda x: (-x[0], -x[1]))
        # FU159: SELECT the within-budget pairs FIRST (same priority order + same counts as serial), then
        # run their searches CONCURRENTLY and MERGE results in ORIGINAL pair order → [S#] byte-identical.
        # (No pair reads another's result — `_pairs` is fixed upfront and the keep filter ignores
        # tool_texts — so parallelizing adds ZERO searches and cannot change the output.)
        _budget = _DIM_RESCUE_BUDGET
        _sbudget = _SUBJ_RESCUE_BUDGET
        _skipped = 0
        _selected = []
        for _pair in _pairs:
            if _pair[0]:   # _is_subj
                if _sbudget <= 0:
                    _skipped += 1
                    continue
                _sbudget -= 1
            else:
                if _budget <= 0:
                    _skipped += 1
                    continue
                _budget -= 1
            _selected.append(_pair)

        def _do_pair(pair):
            _is_subj, _mcount, d, t = pair
            rs = []
            if _is_subj and own_dom_s:
                # first-party-preferred: the subject's own site is the authoritative place for its facts.
                try:
                    rs = self.claude.search_sources(
                        f"{t}: {d} — the specific, current value/details from {t}'s own site",
                        max_searches=1, allowed_domains=[own_dom_s], first_party=True)
                except Exception:
                    rs = []
            # FU150 (#3): the broad "or a reputable source" fallback is for COMPETITORS ONLY — a
            # SUBJECT fact must stay FIRST-PARTY (never a third-party source about {name}).
            if not rs and not _is_subj:
                try:
                    rs = self.claude.search_sources(
                        f"{t}: {d} — the specific, current value/details, from {t}'s own site or a "
                        f"reputable source (not a hit-piece or 'brands to avoid' roundup)"
                        + _subj_brief,   # FU198: subject-scoped
                        max_searches=1)
                except Exception:
                    rs = []
            kb = []
            # FU184: the tool's OWN domain, so this rescue can label honestly and exempt its own pages
            # from the affiliate filter — the same two things `_blocks_from` already does.
            _tdom = own_dom_s if _is_subj else _dom(dom_map.get(t, ""))
            for s in (rs or []):
                u = (s.get("url") or "").strip()
                fct = (s.get("fact") or s.get("title") or "").strip()
                blob = (fct + " " + str(s.get("title") or "") + " " + u).lower()
                if (u and fct and t.lower().split()[0] in blob and not _is_non_evidence(s)
                        and not _is_negative_about(s, name)      # FU150 (#2)
                        # FU198: this branch bypasses _blocks_from entirely, so it needs the same gate
                        and not _is_non_capability_source(s)
                        and not _is_offtopic_credential(s, _subj_toks)
                        # FU161/FU184: no affiliate review for a competitor — and now with the tool's
                        # own_domain, so the competitor's OWN pages are never mistaken for affiliates.
                        and not (t != name and _is_affiliate_review(s, _tdom))):
                    # FU184: label by the SOURCE, not the tool (the rule `_blocks_from` uses). A
                    # third-party page wearing the vendor's name reads as the vendor's own statement —
                    # that is how an affiliate review became a competitor's sole price citation.
                    _lbl = t if (_tdom and _same_site(u, _tdom)) else \
                        f"{_source_class(u) or 'third-party'} · {(s.get('title') or _dom(u) or 'source')[:70]}"
                    kb.append({"label": _lbl, "url": u, "text": fct[:_EVIDENCE_TEXT_CAP]})
            return t, d, kb

        if _selected:
            with ThreadPoolExecutor(max_workers=min(_BLOG_FETCH_WORKERS, len(_selected))) as _ex:
                _dim_results = list(_ex.map(_do_pair, _selected))   # ex.map preserves _selected order
            for _t, _d, _kb in _dim_results:
                for b in _kb:
                    fresh.append(b)
                    tool_texts[_t] = tool_texts.get(_t, "") + " " + b["text"].lower()
                print(f"[blog_gen] dim-rescue: {_t} × '{_d}' → {len(_kb)} kept", flush=True)
        if _skipped:
            print(f"[blog_gen] dim-rescue: budget exhausted with {_skipped} pair(s) left",
                  flush=True)

        # ── FU206 — ONE thin competitor should not cost the whole field its dimensions ──────────
        # FU200 locked "a thin row is never dropped": a badly-sourced competitor costs DIMENSIONS,
        # never its seat, because shrinking the compared field is the self-crowning risk FU98 and the
        # FU105 floor exist to prevent. Measured against the real Osbornes table that trade is
        # sometimes clearly wrong: Withers has 7 of 10 cells empty and is the ONLY firm missing two
        # dimensions the other four can all answer, so keeping it ships 3 dimensions × 5 rows where
        # dropping it would ship 5 × 4 — 20 cells of real data instead of 15.
        #
        # It is NOT always right, which is why this asks instead of deciding. On the real Thyseed
        # table the gaps are SCATTERED (three different bottles, all missing price), so removing rows
        # would cost three competitors to save one column — the floor blocks it and the column drop
        # is correct there. The operator makes the call, per blog, at the pause they already see.
        #
        # Detected HERE, not at table time: `tool_texts` is final, so we know exactly which
        # (tool × dimension) cells are unfilled — the same coverage map the dim-rescue just used.
        # The alternative (deciding on the literal rendered table) would need a SECOND checkpoint
        # after the reconcile, and the resume path is already the riskiest part of the system.
        _sole = {}
        if len(tools) > _MIN_COMPARISON_BRANDS:
            for d in dims:
                ws = _dim_words(d)
                if not ws:
                    continue
                _miss = [t for t in tools if not any(w in tool_texts.get(t, "") for w in ws)]
                if len(_miss) == 1:   # exactly ONE tool is blocking this dimension for everyone
                    _sole.setdefault(_miss[0], []).append(d)
        for _t, _blocked in sorted(_sole.items(), key=lambda kv: -len(kv[1])):
            # Never offer a removal that would breach the FU105 floor — the reason FU200 locked this
            # in the first place. Offered only while enough competitors remain WITHOUT it.
            if len(tools) - 1 < _MIN_COMPARISON_BRANDS:
                break
            _covered = [d for d in dims if any(w in tool_texts.get(_t, "") for w in _dim_words(d) or [""])]
            unsourced.append({
                "tool": _t, "thin_coverage": True,
                "dom": (tool_state.get(_t) or {}).get("dom") or "",
                "facts": [d for d in dims if d not in _covered],
                "blocks_dims": _blocked,
                "covered": len(_covered), "total": len(dims),
                "remaining_if_removed": len(tools) - 1,
            })
            print(f"[blog_gen] thin-coverage: {_t} covers {len(_covered)}/{len(dims)} dimension(s) and "
                  f"is the ONLY tool missing {', '.join(_blocked)} — asking the operator whether to "
                  f"keep it (and drop those dimension(s)) or remove it from the blog", flush=True)

        # (c) INDEPENDENT corroboration for the SUBJECT's self-claims + risk narrative:
        # block ONLY the subject's own domain + already-used third-party URLs (NOT competitors).
        blocked = set()
        if brand.get("domain_url"):
            blocked.add(_dom(brand["domain_url"]))
        for blk in (getattr(self, "_evidence_blocks", None) or []):
            if str(blk.get("label") or "").lower().startswith("third-party"):
                d = _dom(blk.get("url"))
                if d:
                    blocked.add(d)
        blocked = sorted(x for x in blocked if x)
        # FU150 (#2/#3): corroboration NO LONGER "confirms/refutes claims about {name}" — the subject
        # is first-party only and a "refute" would be a negative-about-brand source. Corroborate the
        # COMPARED TOOLS + the TOPIC/category space only; skip the search entirely when nothing but
        # subject claims remain (the common first-party case → frees ~3 searches for competitor sourcing).
        _corr_claims = [c for c in claims[:20]
                        if (c.get("brand") or "").strip().lower() != name.lower()]
        corr = []
        if _corr_claims or tools:
            claim_lines = "; ".join(
                f'{(c.get("brand") or "?")}: {(c.get("dimension") or "")} = '
                f'{(c.get("value") or c.get("claim") or "")}'.strip() for c in _corr_claims)
            subj_cat = f" ({cat})" if cat else ""
            corr_brief = (f"Find reputable INDEPENDENT sources (reviews, documentation, news, analyst "
                          f"pages) about the compared tools ({', '.join(tools[:6]) or 'the options'}) and "
                          f"the {cat or 'topic'} space{subj_cat}, returning a factual value + a source URL "
                          f"for each. Do NOT return negative-review roundups or 'products/brands to avoid' "
                          f"listicles, and do NOT return pages that criticize {name} — seek factual "
                          f"coverage (features, pricing, terms, scale). Claims: {claim_lines or seed}"
                          + (f" Focus on {rgeo}-specific coverage where available." if rgeo else "")
                          + (f" Focus also on {rqual}-related terms, options and mechanics." if rqual else ""))
            try:
                corr = self.claude.search_sources(
                    corr_brief, max_searches=(_VERIFY_MAX_SEARCHES if deep else 3),   # FU56: was 4
                    blocked_domains=(blocked or None))
            except Exception:
                corr = []
        # FU93 (P1) — ONE bounded topic-level search for the QUALIFIER'S MECHANISM itself (not
        # per-tool): the page's reason to exist needs sourced substance (options/structures, terms,
        # what's evaluated, the governing tax/regulatory provision), or the claims-gate strips it.
        if rqual:
            try:
                qsc = self.claude.search_sources(
                    f"how {rqual} works for {cat or seed}: the main options/structures, typical "
                    f"terms, what providers evaluate, and governing tax/regulatory provisions"
                    + (f" in {rgeo}" if rgeo else "")
                    + " — official/authoritative sources preferred", max_searches=2)
            except Exception:
                qsc = []
            corr = (corr or []) + (qsc or [])
        # FU141 — a review OF the subject entering here is tagged `review ·` (machine-readable,
        # so the honesty/YMYL rules can key on it) and capped at 2 across the WHOLE generation
        # (write-time evidence included) — review pileup is the recurring reviewer complaint.
        _rev_kept = sum(1 for _f in (list(fresh) + list(getattr(self, "_evidence_blocks", None) or []))
                        if str(_f.get("label") or "").lower().startswith("review ·"))
        for c in (corr or []):
            if _is_non_evidence(c):   # FU93/FU140: opposition research + job ads never become evidence
                print(f"[blog_gen] source-hygiene: dropped hit-piece "
                      f"'{(c.get('title') or '')[:70]}'", flush=True)
                continue
            if _is_negative_about(c, name):   # FU150 (#2): never cite anything negative about {name}
                print(f"[blog_gen] source-hygiene: dropped negative-about-brand "
                      f"'{(c.get('title') or '')[:70]}'", flush=True)
                continue
            if _is_affiliate_review(c):   # FU161: drop low-quality affiliate/SEO reviews
                print(f"[blog_gen] source-hygiene: dropped affiliate review "
                      f"'{(c.get('title') or '')[:70]}'", flush=True)
                continue
            # FU198: corroboration is the THIRD competitor-evidence path (after _blocks_from and the
            # dim-rescue), so it needs the same two gates or an off-subject credential walks in here.
            if _is_non_capability_source(c):
                print(f"[blog_gen] source-hygiene: dropped non-capability source "
                      f"'{(c.get('title') or '')[:70]}'", flush=True)
                continue
            if _is_offtopic_credential(c, _subj_toks):
                print(f"[blog_gen] source-hygiene: dropped off-subject credential "
                      f"'{(c.get('title') or '')[:70]}'", flush=True)
                continue
            u = (c.get("url") or "").strip()
            ttl = (c.get("title") or "").strip()
            fct = (c.get("fact") or ttl or "").strip()
            if not (u and fct):
                continue
            if _is_subject_review(ttl, name):
                if _rev_kept >= 2:
                    print(f"[blog_gen] review-cap: dropped excess subject review "
                          f"'{ttl[:60]}'", flush=True)
                    continue
                _rev_kept += 1
                fresh.append({"label": f"review · {ttl or u}", "url": u,
                              "text": fct[:_EVIDENCE_TEXT_CAP]})
                continue
            fresh.append({"label": f"{_source_class(u) or 'third-party'} · {ttl or u}", "url": u,
                          "text": fct[:_EVIDENCE_TEXT_CAP]})

        # FU189 — the FU142 dim-rescue runs AFTER the finalize loop and keeps blocks on a LOOSER
        # first-token filter, so it can source an entity every earlier tier missed. Until now the pause
        # was already queued for an entity that got sourced moments later. Re-check each pause item
        # against the FINAL evidence and drop any that now has a block naming it. General: this spares
        # ordinary providers a spurious pause too.
        if unsourced:
            _blob = " ".join(((f.get("label") or "") + " " + (f.get("text") or "") + " "
                              + (f.get("url") or "")) for f in fresh).lower()
            _kept = []
            for _u in unsourced:
                _t = str(_u.get("tool") or "").strip()
                # FU204 (6a): a FACT-specific pause must be re-tested on that FACT, not on whether the
                # entity is sourced at all. The entity-level test below is always true for a price-only
                # item (we found the brand; we are missing its price), so it deleted every price ask
                # ever queued. Re-run the SAME predicate that created the item instead.
                if _u.get("price_only"):
                    if self._has_confirmed_price(self._blocks_naming(_t, fresh), _u.get("dom")):
                        print(f"[blog_gen] price-check: {_t} picked up a confirmed price after all "
                              f"— dropping it from the pause list", flush=True)
                        continue
                    _kept.append(_u)
                    continue
                # FU206: a THIN-COVERAGE item is the FU204 Change 6 bug in a new shape. The
                # entity-level test below asks "is this tool sourced at all?", which is ALWAYS true
                # here — the tool IS sourced, it just cannot answer some dimensions — so the test
                # would delete every thin-coverage ask ever queued, exactly as it once deleted every
                # price ask. It also cannot be stale: the item is created AFTER the dim-rescue, so
                # nothing between here and there can have filled the gap.
                if _u.get("thin_coverage"):
                    _kept.append(_u)
                    continue
                _toks = [x for x in _product_tokens(_t) if len(x) >= 3]
                if _t and _toks and all(x in _blob for x in _toks) and not _u.get("ymyl_official"):
                    print(f"[blog_gen] verify+complete: {_t} was sourced by the dim-rescue after all "
                          f"— dropping it from the pause list", flush=True)
                    continue
                _kept.append(_u)
            unsourced = _kept

        # FU204: the compared brand names, stashed for the citation-attribution check in
        # `_finalize_article` (which runs later and has no access to the extraction). Mirrors how
        # `_peer_note` / `_invented_note` / `_price_warn` already ride the instance.
        self._article_tools = [str(t).strip() for t in (tools or []) if str(t).strip()]
        return {"name": name, "cat": cat, "tools": tools, "dims": dims, "claims": claims,
                "core_topic": core_topic, "fresh": fresh, "unsourced": unsourced,
                "options": sorted(_options),   # FU189: the non-vendor entities, for the reconcile
                "peers": peers,     # FU105: same-type competitors — the reconcile's protected set
                "geo": rgeo,        # FU90: rides the checkpoint too, so the FU79 resume stays geo-aware
                "qualifier": rqual,  # FU93: same for the qualifier
                # FU178: canonical brand facts we could NOT find on the brand's own site — the reconcile
                # must state these as the brand's positioning (attributed), never as a cited fact.
                "unverified_facts": _unverified_facts,
                # FU184: competitors the model named itself (no curated entry, no evidence) — surfaced
                # to the operator; rides the checkpoint so a FU79 resume keeps the flag.
                "invented_tools": _invented,
                "ymyl": ymyl or ""}  # FU133: vertical — reconcile rules + FU79 resume stay YMYL-aware

    def _reconcile_and_finish(self, brand, seed, article, sourcing):
        """FU79 — phase (d) of verify+complete: reconcile the draft against the sourced FRESH FACTS,
        appending them to self._evidence_blocks in [S#] order. `sourcing` is `_source_for_completion`'s
        dict (optionally augmented on resume with manual, tool-labeled blocks). Returns
        {body_markdown, flagged} or None. Never raises."""
        name = sourcing.get("name") or self._brand_block(brand)[0]
        cat = sourcing.get("cat") or ""
        tools = sourcing.get("tools") or []
        peers = sourcing.get("peers") or []   # FU105: protected same-type competitors
        _opts = sourcing.get("options") or []  # FU189: entities that are approaches, not vendors
        _optnames = [t for t in tools if t.lower() in {str(o).lower() for o in _opts}]
        # FU189 — three EXISTING rules below would delete a generic option's row, all keyed on the
        # absence of a VENDOR-labelled block: the no-tool-specific-fresh-fact drop, EVERY KEPT ROW
        # FULLY FILLED, and the competitor-price rule that demands the vendor's own site. An option has
        # no vendor page and never will, so those rules have to be told it is exempt — otherwise fixing
        # the pause would simply trade it for a silently missing row. Emitted ONLY when the comparison
        # actually contains one, so an all-provider blog's prompt is byte-identical to before.
        # FU206 — brands the OPERATOR removed at the pause. The reconcile is the writer that has to
        # take them out of the PROSE the draft already contains; without this its PRESERVE SUBSTANCE
        # rule would actively protect the sentences naming them, and "removed from the blog" would
        # mean "removed from the table only". Empty (and byte-identical) when nothing was removed.
        _rm = [str(x).strip() for x in (getattr(self, "_removed_brands", None) or []) if str(x).strip()]
        _rm_line, _rm_rules = "", ""
        if _rm:
            _rm_line = ("\nREMOVED BY THE OPERATOR (see the REMOVED BRANDS rule): "
                        + json.dumps(_rm, ensure_ascii=False))
            _rm_rules = (
                "\n  - REMOVED BRANDS (hard rule, OVERRIDES PRESERVE SUBSTANCE): the operator has "
                "removed the brands listed under REMOVED BY THE OPERATOR from this article. Delete "
                "EVERY trace of them — their comparison row, every sentence and clause naming them, "
                "any FAQ entry about them, and any citation that exists only to support a claim about "
                "them. Do NOT replace them with a substitute, do NOT say they were removed, and do "
                "NOT leave a dangling comparison that still implies them. Rewrite the surrounding "
                "sentence so it reads naturally without the name. Every OTHER brand stays.")
        _opt_line, _opt_rules = "", ""
        if _optnames:
            _opt_line = ("\nGENERIC OPTIONS (approaches/categories, NOT companies — see the GENERIC "
                         f"OPTIONS rule): {json.dumps(_optnames, ensure_ascii=False)}")
            _opt_rules = (
                "\n  - GENERIC OPTIONS (hard rule): the entities listed under GENERIC OPTIONS are "
                "approaches, methods, materials, plan types or product classes — NOT companies. They "
                "have NO vendor website and none is expected, so the rules above about a tool's OWN "
                "site do NOT apply to them. Fill their cells from the FRESH FACTS labelled "
                "'reference ·', 'official ·' or 'third-party ·' that NAME them, citing the [S#]. "
                "NEVER remove a generic option's row for lacking a vendor page or a tool-labelled "
                "block, and never demand its 'pricing page'. If a specific cell genuinely has no "
                "sourced value, follow the normal punt rules for that CELL — never drop the row.")

        dims = sourcing.get("dims") or []
        claims = sourcing.get("claims") or []
        fresh = sourcing.get("fresh") or []
        body = (article or {}).get("body_markdown") or ""
        if not fresh or not body.strip():
            return None
        # FU90 — geo variant protection: the reconcile is the LAST writer; without these rules it can
        # dilute the geo sections back to generic or hedge missing geo facts.
        rgeo = (sourcing.get("geo") or "").strip() or _seed_geo(seed)
        geo_rules = ""
        if rgeo:
            geo_rules = f"""
  - GEO VARIANT: this article targets {rgeo}. PRESERVE and STRENGTHEN the geography-specific sections —
    never dilute them into generic text. A claim about an option's local coverage/compliance in {rgeo}
    may ONLY be filled from a FRESH FACT that is about {rgeo}. When NO {rgeo}-specific fact exists for a
    tool, keep its GENERAL sourced facts (cited as usual) and keep the WRITING geo-focused — apply the
    {rgeo} evaluation criteria and framing to them. NEVER write hedge language about missing geo sourcing
    ("unverified", "coverage unknown", "not confirmed for {rgeo}") and NEVER invent a {rgeo} coverage
    claim. Each geography-specific section must KEEP multiple concrete, NAMED local provisions/facts
    with their stated implications — compressing a geo section to a one-line nod is the doorway
    pattern. Geography-specific FAQ entries and evaluation criteria MUST survive this rewrite."""
        # FU93 — qualifier variant protection: the qualifier is the page's reason to exist; the
        # reconcile (the LAST writer) must never dilute its sections into a restatement of the
        # subject's own terms.
        rqual = (sourcing.get("qualifier") or "").strip() or _seed_qualifier(seed)
        qual_rules = ""
        if rqual:
            qual_rules = f"""
  - QUALIFIER VARIANT: this article targets "{rqual}" — that is the page's reason to exist. PRESERVE
    and STRENGTHEN the sections explaining the {rqual} MECHANISM itself (options/structures, terms,
    what is evaluated, the governing tax/regulatory angle); never dilute them into a restatement of
    {name}'s own terms — each must keep multiple concrete named facts with their implications, never a
    one-line nod. {rqual}-specific FAQ entries MUST survive this rewrite."""

        # FU178 — canonical brand facts the operator supplied that we could NOT find stated on {name}'s
        # own site. They are usable (the operator vouches for them) but they are ASSERTIONS, not sources:
        # state them as {name}'s own positioning, attributed, and never attach an [S#] to them.
        _unv = [str(x).strip() for x in (sourcing.get("unverified_facts") or []) if str(x).strip()]
        unverified_rules = ""
        if _unv:
            unverified_rules = (
                "\n  - UNVERIFIED CANONICAL LINES (operator-supplied, NOT found on " + name +
                "'s own site): use them where relevant, but state each as " + name + "'s OWN "
                "POSITIONING and ATTRIBUTE it (\"" + name + " says it …\", \"According to " + name +
                " …\") — do NOT attach an [S#] to them and do NOT present them as independently "
                "established. Keep every value in them EXACT.\n"
                + "".join(f"      • {x}\n" for x in _unv[:12]))

        # FU135 — source honesty + internal consistency (all blogs).
        honesty_rules = f"""
  - SOURCE HONESTY: never describe a "third-party ·" source as an "independent audit / analysis /
    report" or "independently verified" — attribute reviews/affiliate write-ups plainly by name, or
    re-cite the figure to the vendor's own page. Only "official ·" sources may be framed as
    authoritative. Sources labeled "review ·" are third-party reviews OF {name} — never
    authoritative, never clinical/safety support; at most color for program facts, attributed
    plainly by name.
  - INTERNAL CONSISTENCY: when the article states BOTH a cited restriction/regulation AND that a
    covered provider still offers the restricted thing, it MUST spell out the specific legal
    pathway/exception that reconciles the two (from the EVIDENCE); if the evidence does not contain
    the reconciliation, state the tension plainly and attribute both sides — never leave the two
    facts silently side-by-side.
  - CLUSTER CONSISTENCY: never introduce a claim that contradicts the brand's other published pages
    listed in the article's context (positioning, pricing, program facts stay consistent)."""

        # FU133 — YMYL reconcile rules (inert for non-YMYL blogs).
        rymyl = (sourcing.get("ymyl") or "").strip()
        ymyl_rules = ""
        if rymyl:
            ymyl_rules = f"""
  - YMYL AUTHORITY ({rymyl} page): every CLINICAL/REGULATORY claim (contraindications, boxed
    warnings, diagnostic thresholds, dosing/monitoring standards, eligibility rules) must cite an
    "official ·" source [S#] — keep those citations, and RE-CITE any such claim currently resting
    on a vendor/affiliate/review source to the official one (or generalize + attribute it).
  - An affiliate review OF {name} duplicates {name}'s own site — it is NEVER independent support
    for a clinical claim; a "best of" listicle may support the comparison's PROGRAM facts only
    (pricing, what's bundled). Neither may back a clinical fact.
  - EXTRACT ZONE: move logistics perks (free/discreet shipping, discounts) OUT of the Quick answer,
    opening paragraph and meta description — clinical/eligibility substance belongs there.
  - QUICK-ANSWER CITATIONS: every [S#] the Quick answer cites must be an "official ·" source or
    {name}'s own page — never a "review ·" / "third-party ·" source for the lead claim.
  - PRODUCT MATCH: a clinical claim about a specific product/drug must cite a source documenting
    THAT product — never a different molecule's/product's label, however closely related. A
    comparison sentence cites each side's own source; re-cite or generalize any claim currently
    resting on the wrong product's document."""

        # (d) reconcile: FILL the comparison from the fetched facts; no "—"; keep/expand dimensions
        start_idx = len(getattr(self, "_evidence_blocks", None) or []) + 1
        fresh_lines = "\n".join(
            f"[S{start_idx + i}] {f['label']} — {f['url']}\n{f['text'][:700]}"
            for i, f in enumerate(fresh))
        recon_prompt = f"""You are the VERIFY + COMPLETE agent for a first-party article about {name}.
Below are the article, the claims to verify, and FRESH SOURCED FACTS — each tool's OWN public facts
(pricing / license / capability) plus independent corroboration. Rewrite the article so its comparison is
COMPLETE and every stated fact is sourced:

  - ONLY tools that perform the article's CORE function ({cat or "the subject's product type"}) belong in
    the comparison. REMOVE any row for a tool that does NOT (e.g. a video-only generator in a music-generator
    comparison). You MAY note it in AT MOST ONE neutral clause (e.g. "Runway is video-only, not a music
    generator") — NEVER a dedicated "Note on <tool>" blockquote or an FAQ entry about it. Do NOT use an
    off-category tool as a foil to disparage-and-pivot to {name}.
  - RELEVANCE BEFORE COMPLETENESS (hard rule): a comparison cell may ONLY carry a fact about the
    SUBJECT of this article. A credential, ranking or capability from a DIFFERENT practice area,
    product line or service line of that option is NOT a substitute and must never be used to fill a
    cell — it misdescribes the option and hides whether it does the subject at all. When an option has
    no fact about the subject for a cell, LEAVE THE CELL EMPTY and let the dimension rules below
    resolve it STRUCTURALLY. Do NOT write a sentence explaining the absence — "not found in public
    sources", "no ranking identified", "not disclosed" and every relative of theirs are punts in ANY
    wording and are banned (see THE PUNT BAN IS ON MEANING below). A column the field cannot answer is
    removed, not annotated.
  - FILL EVERY comparison-table cell with a specific, verified value drawn from the FRESH FACTS, and cite
    it with that source's [S#]. Do this for EVERY tool and EVERY dimension. Cite the SPECIFIC page for each
    claim: a pricing claim → the pricing page's [S#], a license claim → the terms/license page's [S#] —
    NOT a generic homepage when a specific page is in the FRESH FACTS.
  - EVERY COMPARISON COLUMN ANSWERS FOR EVERY OPTION (FU204, hard rule): keep only dimensions every
    compared option can answer from the FRESH FACTS. If one option cannot answer a column, DROP that
    column (or replace it with one they all can) — never leave a blank/"—" cell and never add a note
    saying a value could not be confirmed. A column with a gap is deleted deterministically after you,
    so leaving one only costs the reader a whole dimension.
  - BRAND-SPECIFIC CLAIMS CITE THAT BRAND (FU204, hard rule): a specific factual claim about a NAMED
    brand must cite a source that is that brand's OWN page, or one that explicitly names that brand and
    states that fact about it. NEVER re-point a claim at another brand's page, a listing for a DIFFERENT
    product, or a general authority page. If the FRESH FACTS do not support it FOR THAT BRAND, drop the
    specific rather than borrow a neighbouring citation.
  - WHAT A SOURCE IS GOOD FOR (FU204): a "retail · …" listing or a "review · …" rating page may support
    a PRICE or AVAILABILITY only. A material, safety, certification, temperature or efficacy claim must
    cite the brand's own page or an official/authority source — never a rating page.
  - SOURCE HIERARCHY for a PRICING or LICENSE claim: PREFER the tool's OWN pricing/terms page (a first-party
    block labeled with the tool's name). Use a "third-party ·" review ONLY when no vendor page exists — and
    then ATTRIBUTE it in-text ("per <review>"). Keep the BILLING BASIS exactly as the source states (monthly
    vs annual) — NEVER present an annual price as a monthly one. NEVER cite a stale SaaS aggregator (e.g.
    SaaSworthy, SoftwareFinder) for a price when a vendor page or reputable review is available; if only an
    aggregator has it, attribute it and keep the billing basis, or drop the exact figure.
  - PRICING PRODUCT-MATCH (hard rule): a price in a cell/sentence about the ARTICLE'S product must be the
    price for THAT product's access at that clinic/tool — NEVER the brand's DIFFERENT product (e.g. a TRT
    price in a tirzepatide article) or another product's page. Distinguish a PROGRAM / MEMBERSHIP /
    SUBSCRIPTION fee from the MEDICATION / product cost and LABEL which one a number is; never present a
    membership/program fee AS the medication price. If only a different product's price or a bare program
    fee is available, drop the exact figure and state the pricing model — never a misleading number.
  - CANONICAL PRICE IS AUTHORITATIVE (FU158, hard rule): when the CANONICAL FACTS block lists a price for
    THIS article's product, {name}'s pricing cell AND any {name} price sentence MUST show that exact value
    (an [operator-set] line is locked and overrides any priced page). NEVER fill {name}'s pricing cell with
    a general plan / consult / membership / base fee when a canonical product price exists — replace such a
    cell with the canonical value, cited to {name}'s own site. FU161: carry the FULL structure (intro +
    ongoing + billing cadence/dose) VERBATIM — INCLUDING the meta_description and Quick answer; never
    truncate to just the intro figure (e.g. keep "then $X/month billed quarterly").
  - COMPETITOR PRICE = the vendor's OWN site (FU158, hard rule): a COMPETITOR's price must come from that
    competitor's OWN first-party block — NEVER a "third-party ·" / review / aggregator / listicle block
    (those go stale, e.g. an outdated membership fee). If a competitor's own current price is not in the
    FRESH FACTS, state its pricing honestly (its pricing model, or leave the cell "—") rather than copy a
    review-site number; a "—" plus honesty beats a wrong number.
  - PRICE BASIS (FU158, only when the source states one): keep the unit / quantity / tier / term a price
    applies to (per seat, per supply, a dose/size, annual-vs-monthly, an intro-vs-ongoing tier) VERBATIM;
    label an entry/intro-tier price as such and don't present it as the ongoing cost; don't equate cells on
    DIFFERENT bases. A plain flat price with no such qualifier stays plain — never invent a basis.
  - {name}'s OWN facts are FIRST-PARTY ONLY (this is a hard rule): a fact about {name} (its price, plans,
    features, terms, policies) may ONLY cite a first-party block labeled "{name}" (its own site/published
    content) — NEVER a "third-party ·" / review / analyst block, even for {name}'s pricing. If no
    first-party {name} block supports a {name} fact, state it as {name}'s own positioning or drop it —
    never move it onto a third-party source. And NEVER cite, quote, or keep any source that says anything
    negative/critical about {name} — remove it from the article and from ## Sources.
  - CORE CLAIM: for the article's central factual/policy claim, cite the "official ·" primary source (the
    platform/regulator's own policy page) when one is provided above, and resolve any two contradictory
    versions to that authoritative source. The central claim (and the section that carries it) MUST REMAIN
    in the article — never drop it to avoid a contradiction; reconcile it instead.
  - A tool's cells may ONLY be filled from a FRESH FACT that is ABOUT that SPECIFIC tool (a block labeled
    with the tool's name / its own site, or a page that names it). NEVER fill a tool's cell from a general
    TikTok-policy or industry article (those are for the narrative, not the table). Do NOT copy identical
    cell text across multiple tools — each cell must reflect THAT tool's OWN sourced facts, with its OWN
    specifics (plan names, prices, terms). If a tool has NO tool-specific FRESH FACT, REMOVE its entire row
    from the table — do NOT generalize a policy article or another tool's values to fill it.
  - CHOOSE THE DIMENSIONS THE FIELD CAN ANSWER, and keep the table READABLE: at most {_DIM_CAP}
    comparison columns besides the first (name) column. Keep only dimensions MOST of the compared
    options actually have a sourced value for — a column that two thirds of the field cannot answer is
    not a comparison, it is a column of absence, and it reads as a verdict on the options that are
    blank. Prefer fewer, better-evidenced dimensions over a wide matrix; the selection criteria in the
    PROSE are the page's substance and do NOT each need to become a column.
  - CORRECT any value the fresh facts contradict; CONFIRM supported ones (add the [S#]).
  - THE PUNT BAN IS ON MEANING, NOT WORDING: any phrasing meaning "this data is not available /
    specified / disclosed" ("Not specified in sourced facts", "data not present", …) is a punt.
    Resolve STRUCTURALLY — sourced value, or drop the column/row — never with a filler phrase.
  - NEVER leave "—", "N/A", blank, or ANY punt in a cell OR in prose — no "verify / confirm / check … before
    publishing", no "depends on the plan / varies by plan" hedge, no "verify on their site" pointer. State
    the ACTUAL value(s), including plan-by-plan where they differ (from the FRESH FACTS). If a specific
    dimension genuinely cannot be sourced for MOST tools, remove that WHOLE column. If ONE tool cannot be
    sourced at all, remove that tool's row from the table — never show a blank cell or a "confirm it
    yourself" note.
  - NEVER invent a value or a source — fill only from the FRESH FACTS (or the article's existing cited
    facts). Preserve the structure (Quick answer, question H2s, FAQ) and the byline/disclosure lines at top;
    keep {name}'s existing [S#] citations intact.
  - PRESERVE SUBSTANCE: you may FILL, SOURCE, FIX and REORDER, but you may NOT DELETE any ## / ### SECTION,
    any concrete STATISTIC / number, or any LIST (e.g. a checklist) that is in the draft. If a section's
    factual/policy claim lacks a source, CITE the "official ·" primary source above (or attribute it) —
    NEVER delete a whole section to avoid sourcing it. Deletion is allowed ONLY for (i) an off-category tool
    ROW and (ii) a genuinely unsourceable SPECIFIC value in a table CELL — never a section, a stat, or a list.
  - APPLY EDITS SILENTLY: NEVER write meta-commentary about your sourcing/editing decisions INTO the article.
    Do NOT add a table row, cell, sentence, or note stating that a tool was "deduplicated above", "removed
    per sourcing rules", "has no tool-specific fresh fact", or is "addressed elsewhere / not a direct
    comparison row". When you drop a tool's row, just delete it — never leave a placeholder row or note that
    explains the removal. The reader must never see your rationale.
  - EVERY KEPT ROW FULLY FILLED: a tool row you keep must have EVERY cell filled from that tool's OWN sourced
    facts (including the commercial-license cell). If even ONE required cell can't be sourced for a tool, DROP
    that tool's whole row — never ship a kept row with a blank or "—" cell.
  - STAY NEUTRAL (a vendor page earns AI citations by being the FAIREST answer in the pool, not the
    loudest): the Quick answer must be EVEN-HANDED — name the POOL of qualifying tools and present {name} as
    ONE strong option, NOT as a pitch/headline. Keep the "who might prefer an alternative" balance and any
    honest trade-off. Do NOT stack praise or superlatives ("the only / the best / #1") on {name}.
  - SUBSTANCE BEFORE CREDENTIALS (FU198): PRESERVE and STRENGTHEN the sections that explain the
    subject's MECHANICS (the rules, procedures and constraints that determine the outcome). Never
    trade a mechanic for more rankings, awards or accreditations — those are commodity content and
    must not grow at the expense of substance.
  - NO COMPETITOR-JAB: do NOT add or keep any FAQ entry or "Note on <competitor>" blockquote whose function
    is to disparage a competitor and pivot to {name}. Comparisons must be factual, not a takedown. NEVER
    build a body claim on a competitor's review-site AGGREGATE star score (Trustpilot, Reviews.io, etc.) —
    large-retailer aggregates measure delivery/store complaints, not product quality — unless {name}'s SAME
    metric is cited alongside for an apples-to-apples comparison. Compare on the dimensions that
    legitimately favor {name}, and STATE the competitors' real advantages (store count / pickup, breadth,
    returns infrastructure) plainly — an honest ledger is what earns the citation.
  - NO PHANTOM SELF-REFERENCE: never introduce OR KEEP a sentence that points the reader at another
    {name} page which is not a live URL already present in the draft ("{name} has published a dedicated
    guide on ...", "refer to {name}'s published guidance on ...", "see our guide on ..."). Those pages
    are unpublished drafts — the reader and the answer engine are sent nowhere. DELETE such a sentence;
    this is an explicit exception to PRESERVE SUBSTANCE below. A reference that already carries a real
    Markdown link to a live page is fine and stays.
  - Never STRENGTHEN a conditioned commerce claim by dropping its condition — keep "on qualifying
    purchases" / "in most states" / "up to $N" attached EVERYWHERE the claim is restated, including the
    Quick answer.
  - ENTITY-TYPE (FU98): when the title asks for the best <TYPE> (agencies, platforms, …), the
    comparison's PRIMARY field = entities of that TYPE. Keep different-type options clearly labeled as
    a supplementary category, and NEVER remove same-type competitors so that fewer than 3 same-type
    competitors remain.
  - COMPETITOR FLOOR (FU105): the comparison must KEEP AT LEAST 3 non-{name} competitors. When an
    unsourceable CELL would force a row-drop that leaves fewer than 3 competitors, PREFER dropping the
    offending COLUMN (allowed when a dimension can't be sourced for most tools) or filling the cell
    from the FRESH FACTS — drop the row only when that tool has NO usable facts at all. NEVER invent a
    value to hold the floor.
  - SUBJECT COMPLETENESS (FU142): {name}'s own row must be AT LEAST as complete as the competitors'
    rows — a blank/"—" publisher cell beside filled competitor cells reads evasive and must not
    ship. Fill it from {name}'s sourced facts [S#] (its own-site FRESH FACTS included); never
    invent.{unverified_rules}{honesty_rules}{geo_rules}{qual_rules}{ymyl_rules}{_opt_rules}{_rm_rules}

The FRESH FACTS are numbered starting at [S{start_idx}] — cite them with those EXACT [S#] numbers.

TOOLS: {json.dumps(tools, ensure_ascii=False)}
PEERS (same-type competitors — protected, see COMPETITOR FLOOR): {json.dumps(peers, ensure_ascii=False)}{_opt_line}{_rm_line}
DIMENSIONS (keep all): {json.dumps(dims, ensure_ascii=False)}
CLAIMS TO VERIFY:
{json.dumps(claims[:20], ensure_ascii=False)}

FRESH SOURCED FACTS:
{fresh_lines}

ARTICLE (Markdown):
{body}

Return JSON only:
{{"revised_body_markdown": "the corrected + completed full Markdown body",
  "flagged": [{{"claim": "", "action": "filled|confirmed|corrected|replaced|removed", "reason": ""}}]}}"""
        rres = self.claude.call(recon_prompt, max_tokens=6000, temperature=0.3)
        if (not rres or not isinstance(rres, dict)
                or not (rres.get("revised_body_markdown") or "").strip()):
            return None

        for f in fresh:   # append in [S#] order; _rebuild_sources lists only the cited ones
            self._evidence_blocks.append({"label": f["label"], "url": f["url"], "text": f["text"]})
        flagged = [f for f in (rres.get("flagged") or []) if isinstance(f, dict)]
        changed = sum(1 for f in flagged
                      if f.get("action") in ("filled", "corrected", "replaced", "removed"))
        print(f"[blog_gen] verify+complete: {len(tools)} tool(s) sourced, {len(claims)} claim(s) checked, "
              f"{changed} cell(s)/claim(s) filled-or-changed, {len(fresh)} fresh source(s)", flush=True)
        return {"body_markdown": rres["revised_body_markdown"], "flagged": flagged}

    def generate_linkedin(self, brand, seed, article, geo=""):
        """LinkedIn-native adaptation of the article. Returns the post text or "".

        FU87 — the post is a RETRIEVAL asset, not a teaser: it opens with the target query as a
        question and the NAMED answer lands before the "see more" fold (the FU85 answer-first
        principle on the post surface), carries ONE quotable datum from the blog's verified facts,
        and always includes an against-interest line. A post that answers in-line can be cited;
        a teaser can't. `geo` (FU91): the blog's target geography (explicit wins, else detected
        from the seed) — keeps the post geo-focused instead of genericizing on rewrite."""
        name, _url, _block = self._brand_block(brand)
        title = (article or {}).get("title") or seed
        body = (article or {}).get("body_markdown") or ""
        rgeo = (geo or "").strip() or _seed_geo(seed)   # FU91: explicit geo wins, lexicon fallback
        geo_rule = ""
        if rgeo:
            geo_rule = (
                f"\n  - GEOGRAPHIC FOCUS: this post targets {rgeo}. The LINE-1 question must "
                f"naturally name {rgeo} (as a close natural variant if the target query doesn't "
                f"already contain it); prefer the article's {rgeo}-specific facts for the nugget "
                f"and the body lines. NEVER write hedge language ('unverified', 'coverage unknown', "
                f"'not confirmed for this region').")
        prompt = f"""Adapt this article into a LinkedIn-native post for {name} (first-party company voice).
The post's job is RETRIEVAL for the target query below — a self-contained citation candidate —
as well as being useful and shareable to humans.

TARGET QUERY: {seed}
ARTICLE TITLE: {title}
ARTICLE (the ONLY admissible facts — carry facts over, do NOT copy its wording):
{body[:6000]}

Write the post:
  - OPENING = THE LIFTABLE CHUNK (engines and LinkedIn's "see more" fold both cut here):
    LINE 1: the target query as a natural QUESTION (verbatim or a close natural variant).
    LINE 2: the direct ANSWER with the specific tools/platforms/options NAMED — a complete,
    self-contained answer; keep any nuance as a SECOND clause AFTER the named answer.
    NEVER open with a teaser, a stall, or a curiosity hook ("The honest answer is: it depends…",
    "Let me break down…", "I'm excited to share", "Hot take:").
  - ONE CONCRETE NUGGET: include exactly ONE specific, quotable datum in the visible text — a
    number, a price, a license clause, or a specific capability claim — taken ONLY from the
    ARTICLE's facts. Never invent it and never strengthen it beyond what the article states.
  - AGAINST-INTEREST LINE (mandatory): one sentence naming where an ALTERNATIVE beats {name}
    ("If you want X, <alternative> is the better pick — that's not what we optimized for").
    This is what makes a brand post shareable instead of scrollable.{geo_rule}
  - Then 3-6 short, skimmable lines (line breaks, NOT Markdown headings) — same facts as the
    article, DIFFERENT words.
  - First-party company voice ("we"); name {name} once as the natural recommendation. NEVER use
    fake-discovery framing about {name} ("just found this tool…") and no manufactured social proof.
  - A soft CTA with a link placeholder written exactly as {{link}}.
  - 3-5 relevant hashtags at the end.
  - About 1300-1800 characters. Plain text only — no Markdown headings, no tables.

Return JSON only: {{"linkedin_text": "the full post text"}}"""
        res = self.claude.call(prompt, max_tokens=1500, temperature=0.8)
        if not res or not isinstance(res, dict):
            return ""
        # FU185: this surface never passes through `_finalize_article`'s scrub, so strip here.
        return self._sa((res.get("linkedin_text") or "").strip())

    def generate_linkedin_article(self, brand, article, persona_voice="", target_query="",
                                  manual_title="", geo=""):
        """FU59: rewrite a saved blog into a LONG-FORM LinkedIn ARTICLE (distinct from the short
        `generate_linkedin` post) in a chosen persona's VOICE. Returns {"title","body_markdown"} or {}.

        Author's-voice = adopt the persona's PERSPECTIVE/stance/vocabulary on the topic (first person),
        NEVER fabricate the author's personal history/credentials/numbers/anecdotes. (FU92: the article
        carries NO affiliation-disclosure line — removed per user request.)
        `target_query` (FU61) is the blog's seed / exact target prompt — when set, its phrasing is placed in
        ONE high-weight position (a question-form subhead) so the article also anchors on the exact query.
        `manual_title` (FU83): a user-entered headline — when set it is LOCKED: used verbatim (prompted AND
        code-enforced, like the FU44 custom post title) and the article is written to deliver on it.
        `geo` (FU91): the blog's target geography — the rewrite must PRESERVE the geo focus; with a
        manual_title the geo rule applies to the BODY ONLY (the fixed headline is never steered).
        """
        name, _url, _block = self._brand_block(brand)
        title = (article or {}).get("title") or ""
        body = (article or {}).get("body_markdown") or ""
        pv = (persona_voice or "").strip()
        mt = (manual_title or "").strip()

        if pv:
            persona_block = (
                "WRITE AS THIS PERSON (first-person author voice):\n"
                f"{pv}\n"
                "Adopt THIS persona's perspective, stance, and vocabulary on the topic. Write in first "
                "person as a real practitioner sharing a point of view. BUT do NOT invent the author's "
                "personal history, job history, credentials, specific numbers, or anecdotes "
                '("in my 10 years at…", "when I built…") — nothing about the author that is not in the '
                "source material. Voice and perspective only; never a fabricated biography.\n\n"
            )
        else:
            persona_block = (
                "Write in a natural first-person thought-leadership voice for a practitioner at "
                f"{name}. Do NOT invent personal history, credentials, or anecdotes.\n\n"
            )

        # FU61: retrieval anchor — weave the EXACT target query into ONE high-weight position only.
        # FU83: in manual-title mode the "keep the headline distinctive / don't put it in the headline"
        # clause is dropped — the headline is the USER'S, not ours to steer.
        tq = (target_query or "").strip()
        query_rule = ""
        if tq:
            headline_clause = ("" if mt else
                               "do NOT put it in the headline (keep the headline distinctive), and ")
            query_rule = (
                f'\n  - RETRIEVAL ANCHOR: this article must also help retrieval for the EXACT target query: '
                f'"{tq}". Put that query\'s phrasing (verbatim or a close natural variant) in ONE high-weight '
                f'position — a QUESTION-FORM `##` subhead just before the comparison/recommendation section '
                f'(e.g. "So, {tq}?") — and you MAY echo it once in the opening paragraph. AT MOST twice total; '
                f'do NOT keyword-stuff, {headline_clause}do '
                f'NOT mirror the source blog\'s wording elsewhere.'
                f'\n  - ANSWER-FIRST UNDER THE ANCHOR (FU85 — engines lift heading + first sentence as ONE '
                f'chunk): the FIRST sentence DIRECTLY under that question-form subhead must BE the complete, '
                f'self-contained answer — NAME the specific tools/platforms/options (the entities an engine '
                f'should quote) IN that first sentence. NEVER open it with a stall or transition ("Let us go '
                f'through…", "The honest answer is: it depends…", "Let me break down…"). Keep any nuance as a '
                f'SECOND clause AFTER the named answer — e.g. "Four platforms are worth your time — X, Y, Z '
                f'and W — but they are not doing the same job, and the right one depends on where you are." '
                f'The heading + its first sentence must stand alone as a liftable, citable answer. Apply the '
                f'same answer-first principle to every OTHER question-form subhead in the article too.'
            )

        # FU91: preserve the blog's geographic focus in the rewrite. With a manual title the rule is
        # BODY-ONLY — the FU83 fixed headline is never steered (and stays code-enforced below).
        rgeo = (geo or "").strip() or _seed_geo(tq)
        geo_rule = ""
        if rgeo:
            headline_geo = (
                "The FIXED headline is NOT touched by this rule — apply the geographic focus to the "
                "BODY only." if mt else
                f"The headline may naturally reflect {rgeo}.")
            geo_rule = (
                f"\n  - GEOGRAPHIC FOCUS: the source blog targets {rgeo}. PRESERVE that focus in the "
                f"rewrite: keep the geo-specific facts and evaluation criteria (rephrased, never "
                f"dropped), and the retrieval-anchor subhead's first-sentence answer should name "
                f"{rgeo} naturally. NEVER write hedge language ('unverified', 'coverage unknown', "
                f"'not confirmed for this region'). {headline_geo}")

        # FU83: a user-entered headline is LOCKED — used verbatim; the article delivers on it.
        if mt:
            headline_rule = (
                f'THE ARTICLE HEADLINE IS FIXED — the author already chose it. Return `title` EXACTLY as '
                f'given, verbatim, character for character: "{mt}". Do NOT rewrite, shorten, extend, or '
                f'"improve" it in any way. Write the article to DELIVER on that exact headline.')
        else:
            headline_rule = (
                'Return a compelling, DISTINCTIVE, specific ARTICLE HEADLINE as `title` — draw on the source\'s core\n'
                '    framing/contrast; NOT the source title verbatim. AVOID curiosity-gap / clickbait patterns ("The Real\n'
                '    Reason…", "…The AI That Fixes It", "You won\'t believe…") that pre-announce a sales pitch.')

        prompt = f"""{persona_block}Rewrite the source article below into a LONG-FORM LinkedIn ARTICLE (not a short post).

BRAND (the product you can recommend): {name}
ARTICLE TITLE: {title}
SOURCE ARTICLE (facts to carry over — do NOT copy its wording):
{body[:8000]}

Rules:
  - {headline_rule}
  - Open the body with a strong opening hook. BAN these clichéd openers: "I'm excited to share", "Hot take:",
    "Unpopular opinion:", "I've been thinking a lot about…". Commit to a specific, distinctive point of
    view — no generic thought-leadership filler.
  - WRITE IN FIRST PERSON THROUGHOUT — "I/we", a practitioner speaking — NOT a third-person explainer.
    If a sentence reads like neutral documentation ("Most HR leaders spend months…"), recast it as the
    author's own point of view ("I watch HR leaders spend months…"). This is a person's article, not a
    report.
  - PRESERVE the source's distinctive ANGLE / FRAMING (its core positioning or central contrast) and its
    hook ENERGY — rephrased in different words (still NO sentence reuse). Do NOT flatten a sharp hook or
    contrast into a generic explainer.
  - Skimmable structure: short sections with `##`/`###` subheads, bold, and bulleted lists.
  - Do NOT use Markdown TABLES (pipe `|` tables) or horizontal rules (`---`, `***`, `___`) — LinkedIn's
    editor renders them as literal characters. Present any comparison as a bolded list or short labeled
    lines instead.
  - Carry over the SUBSTANTIVE facts/insights from the source, but do NOT fabricate any claim beyond it,
    and do NOT reuse sentences or phrasing from the source — same facts, DIFFERENT words.
  - Do NOT strengthen a claim beyond the source with emphasis words ("explicitly", "guaranteed", "the only",
    "always"). Keep every claim (esp. licensing / pricing) exactly as precise as the source states.
  - Do NOT cite the brand's OWN press releases / PR-wire distribution (e.g. PR Newswire) or sponsored
    coverage as if it were INDEPENDENT third-party validation. Describe how the product works DIRECTLY
    (the concrete mechanism) rather than leaning on marketing or press quotes.
  - Name {name} as the natural recommendation (don't over-repeat it), with a soft CTA and a link
    placeholder written exactly as {{link}}.{query_rule}{geo_rule}
  - End with 3-5 relevant hashtags.
  - About 800-1500 words. Markdown is allowed (subheads, bold, lists) — but NO tables and NO horizontal rules.

Return JSON only: {{"title": "the article headline", "body_markdown": "the full article in Markdown"}}"""
        res = self.claude.call(prompt, max_tokens=4000, temperature=0.75)
        if not res or not isinstance(res, dict):
            return {}
        return {
            # FU83: a manual title is code-enforced verbatim — never trust the model alone with it.
            # FU185 therefore scrubs a GENERATED headline only; an operator-typed one stays untouched,
            # because the verbatim lock outranks the symbol strip.
            "title": mt if mt else self._sa((res.get("title") or "").strip()),
            "body_markdown": self._sa((res.get("body_markdown") or "").strip()),
        }

    # ---------------------------------------------------------------- FU80: YouTube video package
    @staticmethod
    def _assemble_youtube_description(mini_answer, chapters, blog_link, disclosure):
        """Deterministic YouTube description (rules 12/13/15): first the mini-answer (the title's
        answer in the prompt's language, heavily indexed) → a timestamped chapter block phrased as
        the questions each answers → the blog link → the disclosure line. `blog_link` is the resolved
        blog URL or the literal `{link}` placeholder."""
        parts = [(mini_answer or "").strip()]
        chap_lines = []
        for c in (chapters or []):
            if not isinstance(c, dict):
                continue
            q = (c.get("question") or "").strip()
            ts = (c.get("ts") or "00:00").strip()
            if q:
                chap_lines.append(f"{ts} — {q}")
        if chap_lines:
            parts.append("Chapters:\n" + "\n".join(chap_lines))
        parts.append(f"Full comparison with sources: {blog_link}")
        if (disclosure or "").strip():
            parts.append(disclosure.strip())
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def _youtube_scrub(text):
        """Loop-safety backstop (rule 18): a Reddit link is a recorded, timestamped pointer to a placed
        thread — it must NEVER ship on a permanent surface. Strip any reddit.com URL from the script /
        description / captions. (PR-as-independent framing is handled in the prompt, like the blogs.)"""
        if not text:
            return text
        # scheme OPTIONAL — a bare "reddit.com/r/…" reference is just as much a recorded pointer.
        return re.sub(r"(?:https?://)?(?:www\.|old\.|np\.|m\.)?reddit\.com/\S+", "", text)

    @staticmethod
    def _fmt_min(dm):
        """FU203 — render a (now fractional) target duration for humans: 2.0 -> "2", 1.5 -> "1.5".
        Without this a 1.5-minute target printed as "1.5 minutes" in one place and "1" in another."""
        try:
            f = float(dm or 0)
        except (TypeError, ValueError):
            return "0"
        return str(int(round(f))) if abs(f - round(f)) < 1e-9 else f"{f:g}"

    @staticmethod
    def _parse_ts(ts):
        """FU203 — "MM:SS" / "M:SS" / "H:MM:SS" -> seconds; None when it is not a timestamp."""
        m = _YT_TS_RE.match((ts or "").strip())
        if not m:
            return None
        h, mm, ss = m.group(1), m.group(2), m.group(3)
        if int(ss) > 59:
            return None
        return int(mm) * 60 + int(ss) + (int(h) * 3600 if h else 0)

    @staticmethod
    def _fmt_ts(sec):
        """FU203 — seconds -> the MM:SS shape the chapter list and YouTube both expect."""
        sec = max(0, int(round(sec)))
        return f"{sec // 60:02d}:{sec % 60:02d}"

    @classmethod
    def _normalise_chapters(cls, chapters, dm):
        """FU203 — the first validation `chapters[].ts` has ever had. It is read RAW with a "00:00"
        fallback in three independent places (the pasted description, the export doc, the modal), so a
        model that timed the chapters for a different runtime shipped broken chapters to the client.

        Drops an entry with no question (junk the description already skips) and, where the rest of the
        list is sound, an unparseable or non-monotonic one. When the model clearly timed the script for
        a DIFFERENT runtime (any `ts` past the target) or nothing survives, EVERY question-bearing
        chapter is re-spread evenly across the target instead — its in-range estimates are no more
        trustworthy than its out-of-range ones, and re-timing a chapter beats losing it. A well-formed,
        monotonic, in-range list is returned UNCHANGED (idempotent), and `dm == 0` (model-decided
        length) is a no-op because there is no target to check against."""
        items = [c for c in (chapters or [])
                 if isinstance(c, dict) and (c.get("question") or "").strip()]
        try:
            total = int(round(float(dm or 0) * 60))
        except (TypeError, ValueError):
            total = 0
        if total <= 0 or not items:
            return chapters

        def _spread(entries):
            n = max(1, len(entries))
            step = total / float(n)
            out = []
            for i, c in enumerate(entries):
                c = dict(c)
                c["ts"] = cls._fmt_ts(min(i * step, max(0, total - 1)))
                out.append(c)
            return out

        kept, last, over, dropped = [], -1, False, False
        for c in items:
            sec = cls._parse_ts(c.get("ts"))
            if sec is not None and sec > total:
                over = True
            if sec is None or sec <= last or sec > total:
                dropped = True
                continue
            kept.append(c)
            last = sec
        if over or not kept:
            # The model timed the script for a DIFFERENT runtime (or produced nothing usable), so its
            # in-range estimates are no more trustworthy than its out-of-range ones — re-time every
            # chapter rather than drop the ones that happen to fall outside. Nothing is ever lost.
            return _spread(items)
        if not dropped and len(items) == len(chapters or []):
            return chapters          # already correct — hand back the exact list we were given
        return kept

    @classmethod
    def _script_length_note(cls, script, dm):
        """FU203 — `words = dm * 145` is written INTO the prompt and was never checked against the
        output, so "about 2 minutes" was model-trust with no signal. Count the SPOKEN words (stage
        directions, headings and markdown are not spoken) and report when the script is outside
        ~0.6-1.4x its budget. Returns "" when it is fine, or `dm` is 0. WARNING ONLY — never rewrites."""
        try:
            budget = int(round(float(dm or 0) * 145))
        except (TypeError, ValueError):
            budget = 0
        if budget <= 0 or not (script or "").strip():
            return ""
        txt = re.sub(r"\[[^\]]{0,120}\]", " ", script)         # [B-roll: ...] / [S1] — not spoken
        txt = re.sub(r"(?m)^\s{0,3}#{1,6}\s+.*$", " ", txt)     # segment headings — not spoken
        txt = re.sub(r"[*_`>#|]+", " ", txt)
        words = len([w for w in txt.split() if any(ch.isalnum() for ch in w)])
        if 0.6 * budget <= words <= 1.4 * budget:
            return ""
        return (f"the script runs ~{words / 145.0:.1f} min ({words} words) against a "
                f"{cls._fmt_min(dm)} min target (~{budget} words)")

    def generate_youtube_script(self, brand, article, persona_voice="", disclosure="",
                                target_query="", variant="question", geo="", duration_min=0):
        """FU80 — turn a saved blog into a full YouTube video PACKAGE (script + title + description +
        chapters + a corrected caption transcript + a demo shot-list + thumbnail text + end-screen CTA),
        in the same-facts / DIFFERENT-voice model as `generate_linkedin_article`. Every spoken claim is
        constrained to the blog's already-VERIFIED, cited facts (the blog body carries its `## Sources`),
        so prices/license/coverage match the blog by construction — never re-sourced, never contradicted.

        `variant` ∈ {"question","demo"}: "question" → the head-query title verbatim in the prompt's
        vocabulary; "demo" → the "how/demo" angle so the video covers the cluster without cannibalizing
        the blog (rule 4). `persona_voice` = the on-camera presenter voice (voice/perspective only, never
        a fabricated biography). Returns {title, script, description, captions, meta} or {} on failure.
        Never raises."""
        name, _url, _block = self._brand_block(brand)
        title = (article or {}).get("title") or ""
        body = (article or {}).get("body_markdown") or ""      # carries the blog's cited `## Sources`
        pv = (persona_voice or "").strip()
        # FU84: the default must be factually safe — "sells the products discussed" is wrong for a
        # broker/comparison brand. A passed blog/brand disclosure still wins verbatim.
        disc = (disclosure or "").strip() or (
            f"This channel is run by {name}, which is one of the options compared in this video."
            if _brand_in_comparison(body, name) else f"This channel is run by {name}.")
        tq = (target_query or "").strip()
        is_demo = str(variant).strip().lower() == "demo"

        if pv:
            presenter = ("ON-CAMERA PRESENTER VOICE (first person):\n"
                         f"{pv}\n"
                         "Adopt THIS presenter's perspective, stance, and vocabulary. Speak in first "
                         "person as a real practitioner. Do NOT invent the presenter's personal history, "
                         "job history, credentials, specific numbers, or anecdotes — voice only, never a "
                         "fabricated biography.\n\n")
        else:
            presenter = (f"Write in a natural first-person on-camera voice for a practitioner at {name}. "
                         "Do NOT invent personal history, credentials, or anecdotes.\n\n")

        title_rule = (
            'a DEMO/HOW-TO angle title ("How to …") in the prompt\'s own vocabulary — the video covers '
            'the "how/demo" variant of the cluster so it does NOT duplicate the blog\'s head-query title'
            if is_demo else
            "a QUESTION-FORM title in the prompt's OWN vocabulary (semantic match to the target query is "
            "the strongest retrieval signal you control)")
        query_rule = ""
        if tq:
            query_rule = (f'\n  - RETRIEVAL ANCHOR: this video targets the EXACT prompt "{tq}". Put that '
                          f'phrasing (verbatim or a close natural variant) in the TITLE and speak it in '
                          f'the FIRST lines, and again as spoken section transitions.')
        # FU91: preserve the blog's geographic focus on the video surface (explicit geo wins,
        # else detected from the target prompt). Empty → no prompt change.
        rgeo = (geo or "").strip() or _seed_geo(tq)
        geo_rule = ""
        if rgeo:
            geo_rule = (f'\n  - GEOGRAPHIC FOCUS: this video targets {rgeo}. The `title` should '
                        f'naturally reflect {rgeo}; SPEAK {rgeo} in the opening lines; keep the '
                        f"blog's {rgeo}-specific coverage/compliance facts in the script (never "
                        f"genericized, never hedged — no 'unverified' / 'coverage unknown'); "
                        f'include {rgeo} terms in `tags`.')
        # FU97 — operator-set target duration. 0 = today's model-decided length (byte-identical
        # prompt + token cap). Length is a WORD BUDGET only: the package's mandatory structure is
        # explicitly non-negotiable, so a short target compresses segments, never the purpose.
        # FU203 — FLOAT, clamped 0.5-30, so 1.5 minutes is expressible (it used to `int()` to 1).
        try:
            dm = float(duration_min or 0)
        except (TypeError, ValueError):
            dm = 0.0
        dm = 0.0 if dm <= 0 else max(0.5, min(30.0, dm))
        duration_rule = ""
        if dm:
            words = int(round(dm * 145))   # ~conversational YouTube pace
            _dmtxt = self._fmt_min(dm)
            duration_rule = (
                f"\n  - TARGET LENGTH: about {_dmtxt} minute{'' if _dmtxt == '1' else 's'} spoken "
                f"≈ {words} words (±10%) for `script_markdown` — plan the SEGMENT COUNT to fit. "
                f"NON-NEGOTIABLE AT ANY LENGTH (compress by using FEWER/LEANER segments and less "
                f"elaboration, NEVER by dropping these): the answer-first opening, the spoken "
                f"target-prompt language + section-transition questions, the claims discipline, "
                f"and the honest-tradeoffs segment. ")
            if dm <= 3:
                # FU203 — a real SHORT-FORM STRUCTURE, not one sentence. The old clause said only
                # what to DROP, so the model compressed by removing content (a named option, a figure)
                # and shrank the DESCRIPTION-support fields — which is the half an engine indexes.
                _t = int(round(dm * 60))
                _b = [self._fmt_ts(_t * f) for f in (0.0, 0.10, 0.4167, 0.6667, 0.875, 1.0)]
                duration_rule += (
                    f"SHORT-FORM BEAT SHEET for this {_dmtxt}-minute cut — five beats, each opening "
                    f"with a spoken transition question (those transitions ARE the chapters):\n"
                    f"      * {_b[0]}-{_b[1]} THE LIFTABLE CHUNK — speak the exact target query, then "
                    f"the direct answer with the options NAMED, before any intro or branding.\n"
                    f"      * {_b[1]}-{_b[2]} THE COMPARISON — name each compared option and the ONE "
                    f"differentiator that decides it, with the blog's actual figure. No throat-clearing, "
                    f"no restatement.\n"
                    f"      * {_b[2]}-{_b[3]} HONEST TRADEOFFS — where a competitor wins, and one of "
                    f"{name}'s own limits.\n"
                    f"      * {_b[3]}-{_b[4]} THE RECOMMENDATION — and who should choose otherwise.\n"
                    f"      * {_b[4]}-{_b[5]} CTA + the blog link.\n"
                    f"    DENSITY, NOT OMISSION: compress by cutting hedging, restatement and "
                    f"throat-clearing — NEVER by dropping a named option, a figure, a price or a "
                    f"tradeoff. A short script is MORE specific per second than a long one, never vaguer. "
                    f"THE DESCRIPTION SUPPORT IS NOT COMPRESSED: `mini_answer` stays 2-3 full sentences, "
                    f"`tags` stays 10-15, `captions_transcript` stays complete, and `pinned_comment` keeps "
                    f"its source URLs — the runtime shrinks; the indexed text does not. "
                    f"CHAPTER COUNT: 3-5 chapters for a cut this short (8 chapters on a short video is "
                    f"noise), every `ts` inside {_b[5]}. ")
            else:
                duration_rule += (
                    f"For 3 minutes or less use the tightest viable structure: answer → one "
                    f"comparison segment → tradeoffs → CTA. ")
            duration_rule += (
                f"Spread the `chapters` ts estimates realistically across ~{_dmtxt} minutes. "
                f"`captions_transcript` still covers the FULL spoken script.")

        prompt = f"""{presenter}Turn the SOURCE BLOG below into a YouTube video PACKAGE for {name}.
This is a DIFFERENT surface from the blog: SAME FACTS, DIFFERENT WORDS AND STRUCTURE — never narrate the
blog verbatim (a read-aloud is a duplicate; a video that DEMONSTRATES what the blog asserts is corroborating
format diversity).

BRAND (the product you can recommend): {name}
BLOG TITLE: {title}
SOURCE BLOG (the ONLY admissible facts — its `## Sources` are your citations; carry facts over, do NOT copy wording):
{body[:12000]}

TITLE
  - Return {title_rule} as `title`. Be HONEST — the title must be answerable by the video's actual content;
    NO curiosity-gap / clickbait bait ("You Won't Believe…", "The Real Reason…"). A comparison title may
    name competitors FAIRLY ("X vs Y: Which Syncs Music to Video?") but NEVER a dunk-title ("Why X Is
    Terrible"). Also return `demo_title` = the OTHER angle (the how/demo variant of the same cluster).{query_rule}{geo_rule}

SCRIPT (`script_markdown`)
  - ANSWER-FIRST: in the FIRST 15 SECONDS, SPEAK the question and then the direct answer, BEFORE any intro
    or branding (the transcript is retrieved like text; openings are weighted).
  - SAY THE PROMPT LANGUAGE OUT LOUD: the target phrasing + its natural variants occur verbatim in the
    spoken opening AND as spoken SECTION TRANSITIONS (those transitions become the chapters).
  - CLAIMS DISCIPLINE — every factual claim spoken on camera must be literally true and CONSISTENT WITH THE
    BLOG: same prices, same license terms, same platform coverage. Assert ONLY what the blog sources;
    demonstrate what's demonstrable (an uncut screen recording beats an edited montage). Never state a fact
    the blog doesn't support.
  - MANDATORY HONEST-TRADEOFFS segment: name where competitors WIN and name your OWN limits (the on-camera
    equivalent of the blog's "who should use an alternative" section). This is the credibility engine.
  - NO MANUFACTURED SOCIAL PROOF: no staged reactions, no reading self-written "user testimonials", and do
    NOT cite the brand's OWN press releases / PR-wire syndication as if it were INDEPENDENT reporting. A
    vendor-sourced stat is attributed as yours ("our internal numbers show"), never "reports confirm".
  - YMYL: if this is a health/finance topic, name a credentialed presenter/reviewer on screen and in the
    description, and make NO off-label or ahead-of-evidence claims.
  - Structure the script in clear SEGMENTS with a spoken transition question at the top of each.{duration_rule}

DESCRIPTION SUPPORT — also return, so the description + captions can be assembled:
  - `mini_answer`: 2-3 sentences that DIRECTLY answer the title's question in the prompt's language
    (this text is heavily indexed and often quoted by engines).
  - `chapters`: [{{"question": "the question this segment answers", "ts": "MM:SS estimate"}}], one per
    segment, in order (the user adjusts timings after recording).
  - `captions_transcript`: the clean SPOKEN lines of the whole script, with every brand / product / license
    term spelled CORRECTLY (auto-captions mangle these) — one sentence per line, plain text.
  - `shot_list`: ["a demo/B-roll shot to record", …] — favor UNCUT demonstrations of what's demonstrable.
  - `thumbnail_text`: a short, HONEST thumbnail line (no bait, no claim the footage doesn't show).
  - `cta`: one end-screen call-to-action naming {name} with a link placeholder written exactly as {{link}}.
  - `pinned_comment`: 1-2 natural, genuinely useful sentences to post as the video's PINNED comment (the
    most-read text after the description). You MAY include 1-2 KEY source URLs from the blog's ## Sources
    (official / vendor pages only). Do NOT include the blog link or the disclosure — both are appended
    automatically.
  - `tags`: 10-15 YouTube tags (plain phrases, NO '#') — the target prompt's vocabulary, the product /
    platform names compared, and the category terms.
  - `category`: the best-fit YouTube category name (e.g. "Science & Technology", "Education",
    "Howto & Style").

Never put a Reddit link anywhere. START nothing with the disclosure (it is added deterministically) — but
you MAY assume the description will carry: "{disc}".

Return JSON only:
{{"title": "", "demo_title": "", "mini_answer": "", "script_markdown": "",
  "chapters": [{{"question": "", "ts": ""}}], "captions_transcript": "",
  "shot_list": [], "thumbnail_text": "", "cta": "",
  "pinned_comment": "", "tags": [], "category": ""}}"""
        # FU97 — captions duplicate the script, so a long target would overflow a fixed 6000-token
        # cap and truncate the JSON; scale the budget with the duration (floor 6000, cap 16000).
        _max_tok = 6000 if not dm else max(6000, min(16000, int(dm * 145 * 2 * 1.4) + 1500))
        res = self.claude.call(prompt, max_tokens=_max_tok, temperature=0.7)
        if not res or not isinstance(res, dict) or not (res.get("script_markdown") or "").strip():
            return {}
        chapters = [c for c in (res.get("chapters") or []) if isinstance(c, dict)]
        # FU203 — validate the chapter timestamps BEFORE the description is assembled from them:
        # the description, the export doc and the modal all read this one stored list, so fixing it
        # here fixes all three. No-op when `dm` is 0 (no target to check against).
        _chap_in = chapters
        chapters = self._normalise_chapters(chapters, dm)
        if chapters is not _chap_in:
            print(f"[blog_gen] youtube: chapter timestamps corrected for a "
                  f"{self._fmt_min(dm)} min target ({len(_chap_in)} in, {len(chapters)} out)",
                  flush=True)
        mini_answer = (res.get("mini_answer") or "").strip()
        script_txt = self._sa(self._youtube_scrub((res.get("script_markdown") or "").strip()))
        description = self._assemble_youtube_description(mini_answer, chapters, "{link}", disc)
        # FU82 — pinned comment: the LLM's useful line (reddit-scrubbed; vendor source URLs allowed)
        # + the blog link + the disclosure REPEATED — the most-read text after the description.
        pinned_raw = self._youtube_scrub((res.get("pinned_comment") or "").strip())
        pinned = "\n\n".join(p for p in
                             (pinned_raw, "Full comparison with sources: {link}", disc.strip()) if p)
        # FU82 — tags: deduped (case-insensitive), '#' stripped, capped at 15.
        tags, _seen_tags = [], set()
        for t in (res.get("tags") or []):
            t = str(t).strip().lstrip("#").strip()
            if t and t.lower() not in _seen_tags:
                _seen_tags.add(t.lower())
                tags.append(t)
            if len(tags) >= 15:
                break
        meta = {
            "variant": "demo" if is_demo else "question",
            "demo_title": (res.get("demo_title") or "").strip(),
            "chapters": chapters,
            "shot_list": [str(s).strip() for s in (res.get("shot_list") or []) if str(s).strip()],
            "thumbnail_text": (res.get("thumbnail_text") or "").strip(),
            "cta": self._youtube_scrub((res.get("cta") or "").strip()),
            "mini_answer": mini_answer,
            "pinned_comment": pinned,
            "tags": tags,
            "category": (res.get("category") or "").strip(),
        }
        if dm:
            meta["duration_min"] = dm   # FU97: shown in the export checklist + prefills the UI
            # FU203 — did the script actually hit its budget? Warning only; never rewrites.
            _len_note = self._script_length_note(script_txt, dm)
            if _len_note:
                meta["length_warning"] = _len_note
                print(f"[blog_gen] youtube: length-check — {_len_note}", flush=True)
        # FU185: every published YouTube field gets the symbol strip (this surface does not pass
        # through `_finalize_article`). The description is scrubbed AFTER assembly, which also clears
        # the em-dash our own chapter-line format emits.
        for _k in ("cta", "thumbnail_text", "mini_answer", "pinned_comment", "demo_title"):
            if (meta.get(_k) or "").strip():
                meta[_k] = self._sa(meta[_k])
        for _c in meta.get("chapters") or []:
            if isinstance(_c, dict) and (_c.get("question") or "").strip():
                _c["question"] = self._sa(_c["question"])
        return {
            "title": self._sa((res.get("title") or "").strip()),
            "script": script_txt,
            "description": self._sa(self._youtube_scrub(description)),
            "captions": self._sa(self._youtube_scrub((res.get("captions_transcript") or "").strip())),
            "meta": meta,
        }

    def _discover_site_posts(self, domain_url):
        """FU115 — discover the brand site's EXISTING live blog posts as internal-link
        candidates: sitemap.xml (sitemapindex followed, post/blog children preferred) →
        the /blog index page's anchors as the fallback. Uses `_fetch_homepage` (which
        carries the FU111 residential + FU113 bot-wall ladder). Cached 1h per domain.
        Returns [{url, label}] (label = de-dashed slug, or the anchor text on the
        fallback path); cap 10; NEVER raises — [] on any failure."""
        dom = _norm_site_domain(domain_url)
        if not dom:
            return []
        now = time.time()
        hit = _site_posts_cache.get(dom)
        if hit and (now - hit[0]) < _SITE_POSTS_TTL:
            return list(hit[1])
        posts, seen = [], set()

        def _collect(loc):
            loc = (loc or "").strip()
            m = re.match(r"^https?://([^/]+)(/.*)?$", loc)
            if not m:
                return False
            host = m.group(1).lower()
            host = host[4:] if host.startswith("www.") else host
            path = m.group(2) or "/"
            if host != dom or not _looks_like_site_post(path):
                return False
            key = loc.rstrip("/").lower()
            if key in seen:
                return False
            seen.add(key)
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            label = re.sub(r"[-_]+", " ", slug).strip() or "site post"
            posts.append({"url": loc, "label": label})
            return True

        try:
            xml = _fetch_homepage(f"https://{dom}/sitemap.xml")
            if xml and "<" in xml:
                locs = re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", xml)
                if "<sitemapindex" in xml:
                    # follow up to 3 child sitemaps, preferring post/blog/page ones
                    kids = sorted(locs, key=lambda u: (not any(k in u.lower() for k in
                                                              ("post", "blog", "page")), u))[:3]
                    for kid in kids:
                        kx = _fetch_homepage(kid)
                        for loc in re.findall(r"<loc>\s*([^<\s]+)\s*</loc>", kx or ""):
                            _collect(loc)
                            if len(posts) >= 10:
                                break
                        if len(posts) >= 10:
                            break
                else:
                    for loc in locs:
                        _collect(loc)
                        if len(posts) >= 10:
                            break
            if not posts:
                # fallback: the /blog index page's anchors (anchor text = the real title)
                html = _fetch_homepage(f"https://{dom}/blog")
                for am in re.finditer(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>',
                                      html or "", re.S | re.I):
                    href = am.group(1).strip()
                    if href.startswith("/"):
                        href = f"https://{dom}{href}"
                    if _collect(href):
                        t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", am.group(2))).strip()
                        if len(t) > 3:
                            posts[-1]["label"] = t[:80]
                    if len(posts) >= 10:
                        break
            posts = posts[:10]
            if posts:
                print(f"[blog_gen] site-post discovery: {len(posts)} live post(s) from {dom}",
                      flush=True)
            _site_posts_cache[dom] = (now, list(posts))
            return posts
        except Exception as e:
            print(f"[blog_gen] site-post discovery skipped: {e}", flush=True)
            return []

    def _build_link_targets(self, brand, sibling_links=None):
        """FU114/115 — the verified internal-link candidate list, in priority order:
        (1) the subject's OWN fetched evidence pages; (2) tool-published sibling blogs
        (real titles — they win URL dedupes); (3) the site's EXISTING live posts via
        sitemap//blog discovery. Deduped by URL, cap 15. Only real, verified-or-published
        URLs — never invented."""
        _seen_u, targets = set(), []

        def _add(url, label):
            u = (url or "").strip()
            key = u.rstrip("/").lower()
            if not u or key in _seen_u:
                return
            _seen_u.add(key)
            targets.append({"url": u, "label": (label or "").strip() or "site page"})

        _bname = (brand.get("name") or "").strip()
        for blk in (getattr(self, "_evidence_blocks", None) or []):
            if (blk.get("label") or "").strip() == _bname and (blk.get("url") or "").strip():
                _add(blk["url"], "own site page")
        for _t, _u in (sibling_links or []):
            _add(_u, _t or "related article")
        for p in self._discover_site_posts(brand.get("domain_url") or ""):
            _add(p.get("url"), p.get("label"))
        targets = targets[:15]
        print(f"[blog_gen] internal-links: {len(targets)} verified target(s)", flush=True)
        return targets

    def generate_blog(self, brand, seed, extra_keywords=None, source_urls=None,
                      research_notes="", use_web_search=False, reddit_thread=None,
                      deep_verify=False, allow_pause=False, geo="", sibling_titles=None,
                      qualifier="", internal_links=False, sibling_links=None, ymyl=None,
                      refresh_competitor_facts=False, refresh_competitor_slugs=None,
                      include_pricing=True):
        """Full pipeline: gather evidence → article → verify_claims → [deep_verify] → LinkedIn. Returns
        the merged dict (title, meta_description, keywords, body_markdown, claims_flagged,
        linkedin_text, prompt_version) or None if the article couldn't be generated.

        `reddit_thread` (optional) is a pre-fetched live thread {subreddit,title,url,text}
        the article may cite as COMMUNITY social proof (the brand's own live post + comments).
        `deep_verify` (opt-in): deepen the independent CORROBORATION of the brand's own claims (FU48).
        The competitor-sourcing + comparison-table FILL (FU49) runs on EVERY blog regardless.
        `allow_pause` (FU79): when a tool STILL can't be sourced after all retries, RETURN a
        `{"_pending": {...}}` sentinel (checkpoint of the partial generation) instead of dropping its
        row — so the caller can ask the user for a manual link/fact and resume via `finish_pending_blog`.
        `geo` (FU90): the operator's explicit geography — wins over seed auto-detect everywhere (article
        block, sourcing briefs, reconcile protection, geo-check). `sibling_titles` (FU90): the brand's
        other blog titles so geo variants differentiate. Defaults keep all existing callers unchanged."""
        self.claude.reset_usage()   # FU54: cost this generation from real API usage
        # FU133 — resolve the YMYL vertical: explicit string wins; True = detect (default 'medical'
        # when the lexicon is silent, since the operator asked); False/'off' = off; None = auto.
        if ymyl in (False, "off", "none"):
            rymyl = None
        elif isinstance(ymyl, str) and ymyl.strip():
            rymyl = ymyl.strip()
        elif ymyl is True:
            rymyl = _is_ymyl_brand(brand) or "medical"
        else:
            rymyl = _is_ymyl_brand(brand)
        if rymyl:
            print(f"[blog_gen] ymyl: '{rymyl}' vertical resolved — authoritative sourcing ON", flush=True)
        # FU56 PRIORITY BUDGETING: the low-priority independent-source sweep runs in _gather_evidence FIRST,
        # so cap this stage to a fraction of the budget; the rest is reserved for the higher-priority
        # official/vendor sourcing in verify_and_complete. Keeps a full gen under ~$2 WITHOUT a blunt cutoff
        # that would starve the most valuable searches.
        self.claude.set_cost_ceiling(_CEIL_EVIDENCE)
        # FU134: the brand's KNOWN SOURCES (links the operator supplied on past generations —
        # e.g. an FDA label pasted into the pause modal) join every future generation's pasted
        # sources automatically. Deduped; capped so a long memory can't balloon fetch cost.
        try:
            _known = json.loads((brand or {}).get("known_sources") or "[]")
        except Exception:
            _known = []
        if _known:
            source_urls = list(dict.fromkeys((source_urls or []) + [str(u).strip() for u in _known
                                                                    if str(u).strip()][:8]))
            print(f"[blog_gen] known-sources: +{len(_known[:8])} brand link(s) joined the evidence fetch",
                  flush=True)
        evidence = self._gather_evidence(brand, seed, source_urls=source_urls,
                                         research_notes=research_notes,
                                         use_web_search=use_web_search,
                                         reddit_thread=reddit_thread)
        self.claude.set_cost_ceiling(_BLOG_COST_CEILING)   # raise to the full budget for the priority stage
        # FU114/115 — verified internal-link targets (opt-in), built by the shared helper:
        # own fetched pages + tool-published siblings + the site's existing live posts.
        link_targets = self._build_link_targets(brand, sibling_links) if internal_links else None
        # FU150 (#4): resolve + sync the brand's CANONICAL PER-PRODUCT first-party pricing from THIS
        # run's first-party evidence (+ a first-party product-page web-search for the seed's product —
        # Case 2), so every blog states the SAME values; a differing first-party value is adopted +
        # persisted + surfaced as a warning. `extra_blocks` = a product page the web-search found —
        # fold it into the article's evidence so the writer can cite it as a first-party [S#].
        key_facts, kf_warning, seed_products, extra_blocks = \
            self._resolve_and_sync_key_facts(brand, evidence, seed, include_pricing=include_pricing)
        if extra_blocks:
            _start = len(getattr(self, "_evidence_blocks", None) or []) + 1
            _lines = [f"[S{_start + i}] {b['label']}" + (f" — {b['url']}" if b.get('url') else "")
                      + f"\n{b['text']}" for i, b in enumerate(extra_blocks)]
            if (evidence or "").strip():
                evidence = evidence + "\n\n" + "\n\n".join(_lines)
            else:
                evidence = ("EVIDENCE (the ONLY admissible support for factual claims — cite by [S#] "
                            "and URL):\n" + "\n\n".join(_lines))
            self._evidence_blocks = list(getattr(self, "_evidence_blocks", None) or []) + list(extra_blocks)
        article = self.generate_article(brand, seed, extra_keywords=extra_keywords,
                                        evidence=evidence, geo=geo, sibling_titles=sibling_titles,
                                        qualifier=qualifier,   # FU93
                                        internal_links=internal_links, link_targets=link_targets,
                                        ymyl=rymyl,   # FU133
                                        key_facts=key_facts, key_facts_products=seed_products)   # FU150
        if not article:
            return None
        if kf_warning:
            article["key_facts_warning"] = kf_warning
        draft_body = article.get("body_markdown") or ""   # FU54: pre-verify draft, for the substance guard
        v = self.verify_claims(brand, article, evidence=evidence)
        if v:
            article["body_markdown"] = v["body_markdown"]
            article["claims_flagged"] = v["flagged"]
        else:
            article["claims_flagged"] = []
        # FU49/FU79: source every named competitor's OWN public facts + FILL the comparison. FU79 —
        # when `allow_pause` and a tool STILL can't be sourced after all retries, PAUSE: checkpoint the
        # partial generation and return a `_pending` sentinel so the caller can ask the user for a manual
        # link/fact, instead of silently dropping the tool's row. `deep` deepens corroboration (FU48).
        sourcing = self._source_for_completion(brand, seed, article, deep=deep_verify, geo=geo,
                                               qualifier=qualifier,   # FU93
                                               ymyl=rymyl,   # FU133
                                               refresh_competitor_facts=refresh_competitor_facts,   # FU151
                                               refresh_competitor_slugs=refresh_competitor_slugs,   # FU160
                                               include_pricing=include_pricing)   # FU162
        if allow_pause and sourcing and sourcing.get("unsourced"):
            # FU205 (R6): the resume runs on a FRESH client whose skip count is its own (zero), so
            # the starvation has to travel in the checkpoint or the finished blog forgets WHY its
            # competitors were unsourced.
            self._budget_warn = self._budget_note()
            print(f"[blog_gen] verify+complete: PAUSING — {len(sourcing['unsourced'])} tool(s) unsourced "
                  f"after all retries: {', '.join(u['tool'] for u in sourcing['unsourced'])}", flush=True)
            return {"_pending": {
                "missing": sourcing["unsourced"],
                "checkpoint": {
                    "sourcing": sourcing,
                    "evidence_blocks": list(getattr(self, "_evidence_blocks", None) or []),
                    "check_notes": self._check_notes(),   # FU205 (R4): the checks survive the pause
                    "article": {k: article.get(k) for k in
                                ("title", "meta_description", "meta_title", "keywords", "body_markdown", "claims_flagged")},
                    "draft_body": draft_body,
                },
                "gen_cost": round(self.claude.usage_cost(), 4),
                "gen_usage": dict(self.claude._usage),
                # FU205 (R6): a starved run is the one MOST likely to land here, and this exit never
                # reaches `_finalize_article` — so the warning travels with the pause instead.
                "budget_warning": self._budget_note(),
            }}
        if sourcing and sourcing.get("fresh"):
            vc = self._reconcile_and_finish(brand, seed, article, sourcing)
            if vc:
                article["body_markdown"] = vc["body_markdown"]
                article["claims_flagged"] = (article.get("claims_flagged") or []) + vc["flagged"]
        return self._finalize_article(brand, seed, article, draft_body, geo=geo,
                                      qualifier=qualifier,   # FU93
                                      ymyl=rymyl,   # FU133
                                      link_targets=link_targets)   # FU151 (D): internal-link honesty

    # ------------------------------------------------------------------ FU153 writer pass
    @staticmethod
    def _ngram_overlap(a, b, n=8):
        """Fraction of a's distinct word n-grams that also appear in b — a proxy for how much of
        Claude's exact wording survived a rewrite: LOW overlap ⇒ the token sequence was genuinely
        replaced ⇒ a SynthID watermark on `a` is gone (we have no detector, so this is the removal
        signal). Deterministic, no network. Returns 0.0 when `a` has fewer than n words."""
        wa = re.findall(r"\w+", (a or "").lower())
        wb = re.findall(r"\w+", (b or "").lower())
        if len(wa) < n:
            return 0.0
        grams_a = {tuple(wa[i:i + n]) for i in range(len(wa) - n + 1)}
        if not grams_a:
            return 0.0
        grams_b = {tuple(wb[i:i + n]) for i in range(len(wb) - n + 1)}
        return len(grams_a & grams_b) / len(grams_a)

    @staticmethod
    def _prose_for_overlap(body):
        """FU154/170: return only the DISCRETIONARY PROSE for the watermark-overlap metric — strip the
        structural / must-preserve-verbatim parts (headings, Markdown table rows, the whole ## Sources
        section, inline [S#] markers) AND the clinical-DIRECTIVE sentences we deliberately keep verbatim for
        YMYL safety (contraindications, dosing schedules, safety negations — _CLINICAL_DIRECTIVE_RE). All of
        those are low-entropy, intentionally identical, and carry ~no SynthID watermark (which lives in the
        discretionary prose); counting them would inflate the overlap and falsely grade a SAFE strip
        'not-confirmed' over a residual that can only be removed by risking a clinical fact."""
        head = re.split(r"(?im)^\s*#{1,6}\s*sources\s*$", body or "", maxsplit=1)[0]   # drop Sources
        keep = []
        for l in head.split("\n"):
            s = l.lstrip()
            # FU172: a line that is ENTIRELY bold text is a SECTION LABEL (e.g. "**FSA/HSA eligibility**"
            # above its paragraph) — functionally a heading, required verbatim by the rewrite prompt, and
            # therefore low-entropy structure that carries no watermark. Same class as # / | / >.
            if (s.startswith("#") or s.startswith("|") or s.startswith(">")
                    or re.fullmatch(r"\*\*[^*].*\*\*", s.strip() or "x")):
                continue
            keep.append(l)
        prose = re.sub(r"\[S\d+\]", "", "\n".join(keep))   # drop inline citation markers
        prose = re.sub(r"[\"\u201c\u201d][^\"\u201c\u201d]{12,}?[\"\u201c\u201d]", " ", prose)  # drop quoted spans
        sents = re.split(r"(?<=[.!?])\s+", prose)          # drop deliberately-preserved clinical-directive sentences
        # FU172 guard: only a real SENTENCE is exempt. Without a length bound an UNPUNCTUATED block counts
        # as one "sentence", so a single directive phrase could exempt an ENTIRE body from measurement.
        return " ".join(x for x in sents
                        if not (_CRITICAL_DIRECTIVE_RE.search(x) and len(x.split()) <= 60))

    @staticmethod
    def _longest_shared_run(a, b):
        """FU167: the length (in WORDS) of the LONGEST run of consecutive identical words shared by a and b.
        The watermark = key + the ~4 preceding words → the chosen word; so a surviving verbatim run of >4
        words preserves a marked word PLUS its full hashing context → residual signal. This is the WORST-CASE
        guard (a single long intact passage) that the average n-gram overlap can miss. Deterministic."""
        wa = re.findall(r"\w+", (a or "").lower())
        wb = re.findall(r"\w+", (b or "").lower())
        if not wa or not wb:
            return 0
        m = _SequenceMatcher(None, wa, wb, autojunk=False)   # autojunk off → don't skip common words
        return m.find_longest_match(0, len(wa), 0, len(wb)).size

    @staticmethod
    def _residual_spans(a, b, n=5):
        """FU172: the ORDER-INDEPENDENT residual measure. Returns (spans, words_raw, covered_count) where
        `spans` are (start, end) index pairs over b's words that lie inside SOME n-gram also present in a.

        This REPLACES FU171's `_SequenceMatcher.get_matching_blocks()` coverage, which was BUGGED: that
        returns only a MONOTONIC alignment, while the rewrite prompt explicitly instructs "REORDER clauses
        and sentences" — so reordered verbatim chunks fell off the single increasing path and were silently
        uncounted. Measured on the real article it reported 0.036 where the truth was 0.170 (~5x low), which
        wrongly graded a run "strong" AND skipped the FU170 section pass (gated on a weak grade).
        Set-membership over n-grams has no ordering assumption, is O(n), and is consistent with
        `_ngram_overlap` by construction (same n)."""
        wa = re.findall(r"\w+", (a or "").lower())
        wb_raw = re.findall(r"\w+", (b or ""))
        wb = [w.lower() for w in wb_raw]
        if len(wa) < n or len(wb) < n:
            return [], wb_raw, 0
        grams_a = {tuple(wa[i:i + n]) for i in range(len(wa) - n + 1)}
        covered = set()
        for i in range(len(wb) - n + 1):
            if tuple(wb[i:i + n]) in grams_a:
                covered.update(range(i, i + n))
        spans, st = [], None
        for i in range(len(wb) + 1):
            if i in covered and st is None:
                st = i
            elif i not in covered and st is not None:
                spans.append((st, i))
                st = None
        return spans, wb_raw, len(covered)

    @staticmethod
    def _is_protected_word(w, brand_tokens=(), extracted_words=()):
        """FU172: is this WORD one the rewriter had NO FREEDOM over? That is the test — not "is it a fact?"
        (semantic, fuzzy) but "could it be reworded without making something WRONG?" A token the rewrite is
        FORBIDDEN to change is one Claude had no freedom over either, and no freedom is exactly when a
        watermark cannot be embedded (Google: the mark is "less effective on factual responses"). The two
        definitions coincide, so this reuses the SAME rule set the fact gate enforces.
        BIAS: anything ambiguous returns False (= discretionary) → stricter score, never looser."""
        lw = w.lower()
        if lw in extracted_words:                       # Change 0/0b: verified protect-list (intelligent layer)
            return True
        if lw in brand_tokens:                          # brand / competitor / product names (from the record)
            return True
        if _LOADBEARING_NUM_RE.search(w) or re.fullmatch(r"[\d.,]+", w):
            return True                                 # values: 2.5, $149, 15 mg, 3.9%, 5 seats, 14-day
        if re.fullmatch(r"\d+[A-Za-z]", w):
            return True                                 # regulation codes: 503A / 503B
        if re.fullmatch(r"[A-Z]{3,}\d*", w):
            return True                                 # FDA, MEN2, BMI, HIPAA, SOC (>=3 → not US/AI)
        if "®" in w or "™" in w:
            return True
        return False

    @classmethod
    def _discretionary_words(cls, span_words, brand=None, extracted=None, span_class=None):
        """FU172: the words in a surviving span that COULD carry a watermark — i.e. everything that is
        neither a protected atom nor a function word. `span_class` is the Change-0b/2 classifier verdict
        ('fact-bearing' / 'structural' / 'discretionary'); a non-discretionary verdict zeroes the span.
        Returns the list of discretionary words (its LENGTH is what the grade uses)."""
        if span_class in ("fact-bearing", "structural"):
            return []
        bt = cls._brand_tokens(brand)
        ex = {w.lower() for sp in (extracted or []) for w in re.findall(r"\w+", sp)}
        return [w for w in span_words
                if w.lower() not in _FUNCTION_WORDS and not cls._is_protected_word(w, bt, ex)]

    @staticmethod
    def _brand_tokens(brand):
        """Brand / competitor / product name tokens, read from the brand RECORD (never hardcoded)."""
        out = set()
        for nm in ([(brand or {}).get("name") or ""] + list((brand or {}).get("competitors") or [])
                   + list((brand or {}).get("products") or [])):
            for w in re.findall(r"\w+", str(nm)):
                if len(w) >= 3:
                    out.add(w.lower())
        return out

    @classmethod
    def _residual_run_stats(cls, a, b, min_run=5):
        """FU171/172: the WORST-CASE guard measured as RESIDUAL MASS rather than "does any single run
        exceed 4". Detection scores the MEAN g-value over the whole text, so a lone verbatim island cannot
        lift it while a large surviving fraction can. Returns (longest_run, residual_share, sample)."""
        spans, wb_raw, covered = cls._residual_spans(a, b, n=min_run)
        if not wb_raw:
            return 0, 0.0, ""
        longest, sample = 0, ""
        for st, en in spans:
            if en - st > longest:
                longest = en - st
                sample = " ".join(wb_raw[st:en])[:200]
        return longest, round(covered / len(wb_raw), 4), sample

    # FU167: invisible / zero-width / Default_Ignorable / noncharacter / bidi carrier code points — the
    # "invisible character" watermark/steganography class (NOT Claude's statistical mark, which is word
    # choice). Stripped from every final body as belt-and-suspenders (borrowed from watermarks-remover
    # "Layer A"). Built from explicit code-point ranges (no literal invisibles in the source); EXCLUDES
    # real spaces, line separators (U+2028/2029) and emoji variation selectors (U+FE00-FE0F) so the
    # VISIBLE text never changes.
    _INVISIBLE_RANGES = [
        (0x200B, 0x200F),   # ZWSP, ZWNJ, ZWJ, LRM, RLM
        (0x202A, 0x202E),   # bidi embeddings / overrides
        (0x2060, 0x2069),   # word joiner, invisible operators, bidi isolates
        (0xFEFF, 0xFEFF),   # ZWNBSP / BOM
        (0x180E, 0x180F),   # Mongolian vowel / free variation selector
        (0x3164, 0x3164), (0xFFA0, 0xFFA0),   # Hangul / halfwidth-Hangul filler
        (0xFDD0, 0xFDEF),   # noncharacters
        (0xFFF0, 0xFFFB),   # reserved + interlinear annotation
        (0xFFFE, 0xFFFF),   # noncharacters
        (0xE0000, 0xE01EF), # tags block + variation-selectors supplement
    ]
    _INVISIBLE_RE = re.compile(
        "[" + "".join(chr(a) if a == b else f"{chr(a)}-{chr(b)}" for a, b in _INVISIBLE_RANGES) + "]")

    @classmethod
    def _strip_invisible_chars(cls, text):
        """FU167: delete invisible/zero-width/bidi carrier code points. Returns (cleaned, removed_count).
        Never alters VISIBLE text (real spaces / line breaks / emoji VS are left alone)."""
        cleaned, n = cls._INVISIBLE_RE.subn("", text or "")
        return cleaned, n

    # ------------------------------------------------------ FU185: strip the obvious AI SYMBOLS
    # Generation is NOT touched — no prompt rule, no model call. A prompt rule ("never use an
    # em-dash") perturbs the WHOLE generation: it can shift phrasing in sentences that never had one,
    # and the FU167 grade + every fact gate would then be judging a prompt-nudged variant instead of
    # the real generation. So the symbols are removed MECHANICALLY from the FINISHED body, which is
    # also why this works identically for Claude and for Qwen — both just hand back a body.
    # A model call was considered and rejected: the rewrite prompt ALREADY says "meaning, facts,
    # structure and citations stay identical; only the WORDING changes", and FU172/FU176/FU55 exist
    # precisely because models violated that anyway (a dose weekly→daily, a flipped negation, a price
    # re-attached to the wrong brand, a lost price cadence, the reconcile narrating its own edits into
    # the article). For a punctuation-only job, code is strictly better than a model.
    _AI_ARROWS = "→⇒➔➜⟶⇨⮕"        # → ⇒ ➔ ➜ ⟶ ⇨ ⮕
    # NOTE: U+00B7 (·) is deliberately ABSENT — it is the code-written separator in an evidence label
    # ("third-party · <title>"), not decoration.
    _AI_DECOR = ("•‣▪▫●○✓✔✗✘"
                 "★☆▸▶")                         # • ‣ ▪ ▫ ● ○ ✓ ✔ ✗ ✘ ★ ☆ ▸ ▶
    _AI_QUOTE_MAP = {"“": '"', "”": '"', "„": '"', "″": '"',
                     "‘": "'", "’": "'", "‚": "'", "′": "'",
                     "…": "..."}
    # FU186 — a tail earns a FULL STOP only when it actually OPENS AN INDEPENDENT CLAUSE. Length is
    # not a proxy for clause-hood: the commonest long tail in this pipeline is a bolded label followed
    # by an explanatory PHRASE in a bullet ("- **FDA-compliant procedures** — Medications obtained and
    # distributed through approved channels"), and punctuating a phrase as a sentence ships a visible
    # FRAGMENT. The failure directions are NOT symmetric — a comma splice is a mild wart, a wrong full
    # stop is a fragment the reader sees — so the DEFAULT is a comma and the full stop is earned.
    _AI_CONJ = {"but", "and", "or", "so", "yet", "nor", "while", "though", "although", "because"}
    # openers that can only begin a PHRASE: an infinitive ("to establish …"), a preposition, or an
    # adverbial fragment. `to` covers both the infinitive and the preposition, so one entry does both.
    _AI_NONCLAUSE_LEAD = {"to", "for", "with", "without", "from", "in", "into", "on", "at", "by", "of",
                          "after", "before", "during", "including", "such", "based", "plus", "via",
                          "per", "about", "under", "over", "through", "across", "between", "among",
                          "not", "just", "only", "especially", "particularly", "even", "also", "plus"}
    # a subject pronoun opens a clause on its own
    _AI_CLAUSE_PRONOUN = {"it", "they", "he", "she", "we", "you", "i", "there",
                          "this", "that", "these", "those"}
    # a determiner opens a NOUN PHRASE, which is only a clause once a finite verb follows it
    _AI_CLAUSE_DET = {"the", "a", "an", "most", "many", "some", "each", "all", "every", "no",
                      "its", "his", "her", "their", "our", "your", "both", "few", "several"}
    _AI_FINITE_VERB = {"is", "are", "was", "were", "has", "have", "had", "can", "could", "will",
                       "would", "may", "might", "must", "should", "do", "does", "did",
                       "isn't", "aren't", "wasn't", "weren't", "won't", "can't", "cannot",
                       "doesn't", "don't", "didn't", "hasn't", "haven't", "hadn't"}
    _AI_LIST_LINE_RE = re.compile(r"^\s*(?:[-*+]\s|\d+[.)]\s)")

    @classmethod
    def _ai_lowercase_ok(cls, word):
        """A dash-as-sentence-break leaves the tail's first word CAPITALISED, so converting the dash to
        a comma can strand a stray capital (", To establish your metabolic status"). Fixing that is
        tidying our OWN edit, but a blanket lowercase would maul a proper noun or an acronym ("FDA",
        "LillyDirect", "Zepbound"), so only a FUNCTION WORD — which can never be a proper noun — is
        ever lowered. A capitalised common noun keeps its capital: a mild oddity beats a mangled name."""
        core = word.strip("*_`\"'()[].,;:")
        if not core or not core[:1].isupper() or not core[1:].islower():
            return False                       # ALL-CAPS / internal capital / already lower → leave it
        w = core.lower()
        if w == "i":                            # never "i"
            return False
        return w in (cls._AI_CONJ | cls._AI_NONCLAUSE_LEAD | cls._AI_CLAUSE_DET
                     | cls._AI_CLAUSE_PRONOUN | {"if", "when", "since", "whether", "either",
                                                 "neither", "as", "that", "than", "then"})
    _AI_DASH_TIGHT_RE = re.compile(r"(\w+)(?:—|–|--)(\w+)")
    _AI_DASH_RE = re.compile(r"[ \t]*(?:—|–|--)[ \t]*")

    @classmethod
    def _ai_tail_opens_clause(cls, words, head, line):
        """FU186 — is the text AFTER a dash an independent clause (so a full stop is right), or a
        PHRASE (so a comma is)? Conservative by construction: it answers False unless the opener is
        positively clause-shaped, because the wrong answer in the True direction ships a fragment."""
        if not words:
            return False
        w0 = words[0].lower().strip("*_`\"'()[]")
        # FORCED COMMA — a label ending a bold run on a LIST line is followed by its explanation, not
        # by a sentence. This is the exact shape that shipped broken.
        if head.endswith("**") and cls._AI_LIST_LINE_RE.match(line):
            return False
        if w0 in cls._AI_CONJ or w0 in cls._AI_NONCLAUSE_LEAD:
            return False
        if len(w0) > 4 and w0.endswith("ing"):          # an -ing participle opens a phrase
            return False
        # EARNED FULL STOP
        if w0 in cls._AI_CLAUSE_PRONOUN:
            return True
        if w0 in cls._AI_CLAUSE_DET:
            return any(w.lower() in cls._AI_FINITE_VERB for w in words[1:3])
        return False

    @classmethod
    def _ai_fix_dashes(cls, line):
        """Replace an em-dash / en-dash / `--` used as punctuation. Cases, each unit-tested:
        - TIGHT between word characters → a HYPHEN when either side carries a digit or both sides are
          capitalised, because that is a RANGE or a compound ("5–10 business days" must NEVER become
          "5, 10 business days"; "Monday–Friday" must not become "Monday, Friday"); otherwise a comma.
        - a SPACED dash between two number-bearing tokens → a HYPHEN (still a range: "9am – 5pm").
        - a dash left dangling at the end of a sentence/line → dropped.
        - a tail that OPENS AN INDEPENDENT CLAUSE → a FULL STOP + capitalisation, since a comma there
          reads as a run-on ("Coverage is limited — most plans will not reimburse …").
        - EVERYTHING ELSE → a COMMA. This is the FU186 default: a phrase punctuated as a sentence is a
          visible fragment, so the full stop has to be earned (see the constants above).
        Returns (line, n_replaced). A line with no such dash comes back BYTE-IDENTICAL."""
        n = 0

        def _tight(m):
            nonlocal n
            n += 1
            a, b = m.group(1), m.group(2)
            if any(c.isdigit() for c in (a + b)) or (a[:1].isupper() and b[:1].isupper()):
                return f"{a}-{b}"
            return f"{a}, {b}"

        line = cls._AI_DASH_TIGHT_RE.sub(_tight, line)
        i, guard = 0, 0
        while guard < 400:
            guard += 1
            m = cls._AI_DASH_RE.search(line, i)
            if not m:
                break
            st, en = m.start(), m.end()
            tail = line[en:]
            cut = re.search(r"[.!?](?:\s|$)", tail)
            seg = tail[:cut.start()] if cut else tail
            words = re.findall(r"[\w$%]+", seg)
            head = line[:st].rstrip()
            # a SPACED dash between two number-bearing tokens is still a RANGE ("9am – 5pm",
            # "$1,300 — $10,000") — a comma there would read as two separate values.
            _b = re.search(r"(\S+)\s*$", line[:st])
            _a = re.match(r"\s*(\S+)", tail)
            _is_range = bool(_b and _a and any(c.isdigit() for c in _b.group(1))
                             and any(c.isdigit() for c in _a.group(1)))
            if not words:                                   # dangling before ./!/? or end of line
                line = head + tail
                i = len(head)
            elif _is_range:
                line = head + "-" + tail.lstrip()
                i = len(head) + 1
            elif cls._ai_tail_opens_clause(words, head, line):
                t = tail.lstrip()
                line = head + ". " + t[:1].upper() + t[1:]
                i = len(head) + 2
            else:
                t = tail.lstrip()
                if cls._ai_lowercase_ok(words[0]) and t[:1].isupper():
                    t = t[:1].lower() + t[1:]
                line = head + ", " + t
                i = len(head) + 2
            n += 1
        return line, n

    @classmethod
    def _ai_fix_decor(cls, line):
        """Arrows and decorative bullets/checkmarks. A LEADING one is acting as a list marker, so it
        becomes a real Markdown one (the list survives); an INLINE arrow means "leads to", so it is
        said in words rather than dropped; inline ornament is pure decoration and goes."""
        lead = re.match(r"^(\s*)[" + re.escape(cls._AI_ARROWS + cls._AI_DECOR) + r"]+[ \t]+", line)
        if lead:
            return lead.group(1) + "- " + line[lead.end():], 1
        n = 0
        line, k = re.subn(r"[ \t]*[" + re.escape(cls._AI_ARROWS) + r"][ \t]*", " to ", line)
        n += k
        line, k = re.subn(r"[ \t]*[" + re.escape(cls._AI_DECOR) + r"][ \t]*", " ", line)
        n += k
        return line, n

    def _scrub_ai_symbols(self, text):
        """FU185 — remove the OBVIOUS AI SYMBOLS from a finished body. Mechanical: no LLM, no network,
        no prompt change, and it can therefore never alter a fact, a citation or the structure.

        SKIPS, so it can never damage load-bearing text: fenced code blocks; any TABLE row (which is
        what protects the FU138 "—" punt-placeholder cell); and the `## Sources` section (whose
        " — <url>" separator is written by `_rebuild_sources` itself, not by a model).

        Touches punctuation and decoration ONLY — never a digit, a letter, an [S#] marker, a heading
        line or a table cell — so `_facts_preserved`, `_price_cadence_ok` and `_verify_facts_semantic`
        all stay valid over the scrubbed body, and the FU181 comma-in-a-number class cannot re-open
        (a thousands separator sits BETWEEN digits and is never a target). Idempotent: scrubbing twice
        equals scrubbing once. Returns (text, counts)."""
        if not text:
            return text or "", {}
        counts = {"dashes": 0, "decor": 0, "quotes": 0}
        out, fence, in_sources = [], False, False
        for line in text.splitlines():
            st = line.strip()
            if st.startswith("```") or st.startswith("~~~"):
                fence = not fence
                out.append(line)
                continue
            if re.match(r"(?i)^\s*#{1,6}\s*sources\b", line):
                in_sources = True
                out.append(line)
                continue
            if in_sources and re.match(r"^\s*#{1,6}\s+\S", line):
                in_sources = False          # a later heading ends the Sources section
            # FU191 — a line that is ONLY a thematic break ("---") is Markdown STRUCTURE, not a dash
            # used as punctuation. `_AI_DASH_RE` matched the "--" inside it and left a bare "-", which
            # renders as a stray hyphen paragraph instead of a horizontal rule: 11 of them shipped on a
            # live blog, and one leaked into a FAQ answer in the structured data (the `_parse_faq_pairs`
            # cut is keyed on "---", so degrading it also disabled that cut).
            if re.match(r"^\s*(?:-{3,}|\*{3,}|_{3,})\s*$", line):
                out.append(line)
                continue
            # a source-list entry carries the code-written " — <url>" separator, so it is skipped
            # even outside a recognised `## Sources` heading (belt and braces).
            if fence or in_sources or re.match(r"^\s*[-*]\s*\[S\d+\]", line):
                out.append(line)
                continue
            # FU186 — a TABLE row keeps its dash and decoration passes skipped (that skip is what
            # protects the FU138 "—" punt-placeholder cell and the row's pipes), but a curly quote in
            # a CELL carries no structural meaning, so the quote/ellipsis map still runs there.
            _row = st.startswith("|")
            if _row:
                ln, k_d, k_c = line, 0, 0
            else:
                ln, k_d = self._ai_fix_dashes(line)
                ln, k_c = self._ai_fix_decor(ln)
            k_q = 0
            for bad, good in self._AI_QUOTE_MAP.items():
                if bad in ln:
                    k_q += ln.count(bad)
                    ln = ln.replace(bad, good)
            if not (k_d or k_c or k_q):
                out.append(line)            # untouched line stays BYTE-IDENTICAL
                continue
            if _row:
                counts["quotes"] += k_q     # never run the prose normalisers over a row's cells
                out.append(ln)
                continue
            counts["dashes"] += k_d
            counts["decor"] += k_c
            counts["quotes"] += k_q
            # tidy only the fallout of our own edits, and never the leading indent (a nested list
            # item's two leading spaces are structure).
            pre = re.match(r"^[ \t]*", ln).group(0)
            body = ln[len(pre):]
            body = re.sub(r",[ \t]*,", ",", body)
            body = re.sub(r",[ \t]*([.!?])", r"\1", body)
            body = re.sub(r"[ \t]+,", ",", body)
            body = re.sub(r"[ \t]{2,}", " ", body)
            out.append((pre + body).rstrip() if not body.strip() else pre + body.rstrip())
        res = "\n".join(out)
        if text.endswith("\n") and not res.endswith("\n"):
            res += "\n"
        if any(counts.values()):
            print(f"[blog_gen] ai-symbols: {counts['dashes']} dash(es), {counts['decor']} "
                  f"decoration(s), {counts['quotes']} quote/ellipsis char(s) replaced", flush=True)
        return res, counts

    def _sa(self, text):
        """`_scrub_ai_symbols` when only the text is wanted (the derived-surface call sites)."""
        return self._scrub_ai_symbols(text)[0]

    @classmethod
    def _watermark_removal_report(cls, claude_body, out, brand=None, extracted=None, verdicts=None):
        """FU167/171/172: MEASURE the watermark-removal level from the RESEARCHED mechanism.

        Why the grade is based on the longest DISCRETIONARY run rather than raw verbatim-ness:
        - Detection needs LENGTH. SynthID-Text reaches only TPR~0.30 at FPR 1% on 50-token text — and that
          is text which is 100% watermarked and high-entropy; the Nature paper operates at 400 tokens.
        - The mark is weak-to-absent on FACTUAL/low-entropy text (Google: "less effective on factual
          responses, as there is less opportunity to augment generation without decreasing accuracy").
        - Dilution degrades global detection ~O(1/T), and localizing a span is strictly harder still.
        So a run of preserved facts/names/codes is NOT evidence of a surviving watermark, while a long run
        of free-choice prose is. Grading on raw verbatim-ness penalized exactly the text the rewrite is
        REQUIRED to keep, which is why every YMYL article read "not-confirmed" no matter how good the strip.
        Raw numbers stay reported for transparency."""
        pa, pb = cls._prose_for_overlap(claude_body), cls._prose_for_overlap(out)
        overlap = cls._ngram_overlap(pa, pb, n=5)
        run, share, sample = cls._residual_run_stats(pa, pb)
        spans, wb_raw, _cov = cls._residual_spans(pa, pb)
        vmap = verdicts or {}
        disc_total, disc_longest, disc_sample = 0, 0, ""
        for st, en in spans:
            words = wb_raw[st:en]
            key = " ".join(words).lower()
            d = cls._discretionary_words(words, brand, extracted, vmap.get(key))
            disc_total += len(d)
            if len(d) > disc_longest:
                disc_longest, disc_sample = len(d), " ".join(words)[:200]
        disc_share = round(disc_total / len(wb_raw), 4) if wb_raw else 0.0
        inv = len(cls._INVISIBLE_RE.findall(out or ""))
        target = float(os.environ.get("WRITER_OVERLAP_TARGET", "0.05"))
        # ~36 words ≈ 50 tokens = the point at which even fully-watermarked high-entropy text only reaches
        # ~30% TPR; below it a surviving span cannot carry a testable signal.
        floor = int(os.environ.get("WRITER_DETECT_FLOOR_WORDS", "36"))
        cap = int(os.environ.get("WRITER_RESIDUAL_RUN_CAP", "40"))
        # The discretionary run is the PRIMARY axis (detectability), but residual MASS is still a real
        # backstop: dilution is what destroys the signal, so a rewrite that is largely verbatim overall
        # must fail even if no single discretionary run is long (e.g. a short near-verbatim excerpt).
        # Dilution is what destroys the signal — but only DILUTION OF WATERMARK-BEARING TOKENS counts, and
        # facts carry no mark whoever emitted them. So the mass backstop tracks the DISCRETIONARY share; the
        # RAW share is kept as an abuse floor (>=0.50 = a near-verbatim copy) so the guard can't be gamed.
        abuse = float(os.environ.get("WRITER_RAW_SHARE_ABUSE", "0.50"))
        if (disc_longest < floor // 2 and overlap < 0.15 and disc_share < 0.15
                and share < abuse and run < cap and inv == 0):
            grade = "thorough" if overlap < target else "strong"
        elif (disc_longest < floor and overlap < 0.15 and disc_share < 0.30
                and share < abuse and run < cap and inv == 0):
            grade = "strong"
        else:
            grade = "not-confirmed"
        return {"n5_prose_overlap": round(overlap, 3), "longest_shared_run": int(run),
                "residual_share": share, "residual_sample": sample,
                "residual_discretionary_share": disc_share,
                "longest_discretionary_run": int(disc_longest),
                "discretionary_sample": disc_sample,
                "detect_floor_words": floor,
                "invisible_chars": int(inv), "grade": grade}

    @staticmethod
    def _facts_preserved(claude_body, out, brand=None, extra_atoms=None):
        """FU168/169: the deterministic FACT-INTEGRITY gate — how we DETERMINE a factual-sentence rewrite is
        SAFE without trusting the 72B's judgment. Extract the LOAD-BEARING fact TOKENS from Claude's body and
        require EVERY one to still appear in the rewrite; a rewrite missing any FAILS validation (won't ship) →
        a reworded sentence that DROPPED or ALTERED a dose / price / condition / rule can never ship.
        Protected: load-bearing NUMBERS only (_LOADBEARING_NUM_RE — decimals/doses, currency prices,
        unit-qualified amounts; NOT bare integers or rhetorical percents — FU169 fixed those false positives
        that made the rewrite never ship), regulation/clinical CODES (503A/503B; ≥3-letter acronyms FDA/MTC/
        MEN2/HIPAA/BMI — the ≥3 floor drops US/AI/2-letter false positives), ®/™-marked names, and the brand +
        competitor names. Number matching is comma-normalized ($1,000≡$1000, "15 mg"≡"15mg"). Returns
        (ok, missing[]). Excludes the ## Sources section (its URL numbers are rebuilt by _rebuild_sources)."""
        head = re.split(r"(?im)^\s*#{1,6}\s*sources\s*$", claude_body or "", maxsplit=1)[0]
        out_s = out or ""
        out_nc = out_s.replace(",", "")                                   # comma-normalized (for number cores)
        missing = set()
        for m in _LOADBEARING_NUM_RE.finditer(head):                     # load-bearing numbers → compare on the digit CORE
            core = re.sub(r"[^\d.]", "", m.group(0)).strip(".")          # keep digits + decimal point only
            if core and core not in out_nc:
                missing.add(core)
        literal = set()
        literal |= set(re.findall(r"\b\d+[A-Z]\b", head))                # 503A, 503B (digit-then-letter code)
        # FU187: drop an ordinary word the author merely SHOUTED — see `_is_emphasis_caps`.
        literal |= {t for t in re.findall(r"\b[A-Z]{3,}\d*\b", head)       # FDA, MTC, MEN2, HIPAA, BMI, GLP, TRT (≥3 → not US/AI)
                    if not _is_emphasis_caps(t, head)}
        literal |= set(re.findall(r"\b\w+(?=®|™)", head))                # Zepbound®, Mounjaro®
        if brand:
            # FU181 — two bugs here, and they masked each other:
            #  (a) `list(brand["competitors"])` iterated a JSON STRING as CHARACTERS (get_brand returns
            #      dict(row) with no parsing), and every 1-char "name" was then dropped by the len>=3
            #      filter — so competitor-name protection has never actually fired. _as_list is what the
            #      rest of the file uses for these stored fields.
            #  (b) the names were required in the OUTPUT unconditionally. Parsing them correctly WITHOUT
            #      this second fix would fail every rewrite for a brand whose stored competitor list
            #      names anyone this particular article doesn't discuss. The gate's job is to catch a
            #      DROPPED fact, so a name only counts if Claude's body actually carries it.
            for nm in [(brand.get("name") or "")] + _as_list(brand.get("competitors")):
                nm = (nm or "").strip()
                if len(nm) >= 3 and nm in head:
                    literal.add(nm)
        missing |= {t for t in literal if t and t not in out_s}
        # FU172: the intelligent protect-list is enforced too (union with the regex floor above).
        # FU181: a literal miss on a DIGIT-bearing atom is retried on a normalized form (commas
        # dropped, whitespace collapsed) — the same treatment the number path above already gets, so
        # "$1,300" ≡ "$1300" and "2.5 mg" ≡ "2.5mg". A punctuation-only difference is not a dropped
        # fact; a genuinely dropped one still fails, because the digits must still be there.
        def _norm_atom(s):
            return re.sub(r"\s+", "", (s or "").replace(",", "")).lower()

        out_norm = _norm_atom(out_s)
        for sp in (extra_atoms or []):
            sp = (sp or "").strip()
            if not sp or sp.lower() in out_s.lower():
                continue
            if any(ch.isdigit() for ch in sp) and _norm_atom(sp) in out_norm:
                continue
            missing.add(sp)
        return (not missing), sorted(missing)

    @staticmethod
    def _split_heading_segments(body):
        """FU170: split a Markdown body into [(heading_line_or_None, chunk_text)] segments at every
        heading line, keeping the pre-first-heading preamble as a leading (None, text) segment. Used by
        the section-chunked rewrite so each writer call carries ONE section instead of the whole article.
        (Distinct from the FU54 `_split_sections`, which returns (title, block) pairs for the substance guard.)"""
        segs, head, buf = [], None, []
        for line in (body or "").split("\n"):
            if line.lstrip().startswith("#"):
                if head is not None or any(x.strip() for x in buf):
                    segs.append((head, "\n".join(buf)))
                head, buf = line, []
            else:
                buf.append(line)
        if head is not None or any(x.strip() for x in buf):
            segs.append((head, "\n".join(buf)))
        return segs

    def _rewrite_sections(self, claude_body, name, temperature=1.0, timeout=600):
        """FU170: SECTION-CHUNKED rewrite — the structural lever for the residual verbatim runs.
        Rewriting a ~2,400-word article in ONE call forces the 72B to hold every constraint at once
        (23 citations + 15 headings + a table + every number/negation), so it anchors on the original
        wording for dense factual prose — 4 whole-article attempts still left ~57 runs of 5+ words.
        Rewriting ONE section per call leaves few enough competing constraints that the model actually
        recasts each paragraph. Headings are re-emitted from the ORIGINAL (never sent for rewording), and
        a section whose call fails keeps its ORIGINAL text (safe degradation — content is never lost).
        Returns the reassembled body, or None when the writer produced nothing usable at all."""
        segs = self._split_heading_segments(claude_body)
        if not segs:
            return None

        def _one(idx_head_chunk):
            """Rewrite ONE section. Returns (index, text_to_emit, was_rewritten). Pure per-section work —
            no shared state — so it is safe to run these CONCURRENTLY; results are reassembled by index."""
            i, head, chunk = idx_head_chunk
            src = chunk if chunk.strip() else ""
            if not src.strip():
                return i, (head if head is not None else ""), False
            # the '## Sources' section is rebuilt deterministically by _rebuild_sources — never reword it
            if head and re.match(r"(?i)^\s*#{1,6}\s*sources\s*$", head.strip()):
                return i, ((head + "\n" + chunk) if head else chunk), False
            prompt = (
                f"Rewrite ONE SECTION of a {name} article ENTIRELY in your own words — a full rewrite, not "
                "a light edit.\n\n"
                "WHY: the original was written by another AI whose word choices carry a hidden statistical "
                "mark, which lives in the DISCRETIONARY wording, not the facts. So:\n"
                "- REPLACE every discretionary word (connectives, transitions, adjectives, verbs, adverbs, "
                "sentence openers) and RECAST every sentence's structure;\n"
                "- share NO run of MORE THAN 4 consecutive words with the original — the single most "
                "important rule;\n"
                "- REORDER clauses and sentences wherever the meaning allows;\n"
                "- reword right up to each preserved item (never leave the original phrasing touching one).\n\n"
                "PRESERVE EXACTLY: every [S#] citation marker (keep each on the claim it supports); every "
                "number, dose, %, price and date; every product/drug/brand name; every Markdown table "
                "(structure and cell values, verbatim); every heading line and every line that is entirely "
                "bold text (a section label) character-for-character; and every NEGATION or clinical "
                "directive with its exact scope ('not', 'no', 'contraindicated', 'only', 'required', "
                "'not FDA-approved', 'not a controlled substance').\n"
                "Descriptive factual and regulatory sentences (definitions, eligibility ranges, "
                "certifications, pricing prose, timelines, process steps) MUST be recast — do NOT leave one "
                "near-verbatim. Keep a sentence word-for-word ONLY if it is a contraindication, a dosing "
                "schedule, or a safety negation whose scope you cannot preserve while rewording.\n"
                "Do NOT add a heading. Return ONLY the rewritten section text.\n"
                # FU193 — the generic "no preamble, no commentary" half of this rule lost EIGHT times
                # across twelve live rewrites, so it is restated CONCRETELY, naming the exact shapes that
                # shipped. Forbid-only: it cannot make the rewrite do anything new. The deterministic
                # strip stays the guarantee; this is the layer that also covers a NOVEL wording.
                "OUTPUT DISCIPLINE (this is PUBLISHED PROSE, not a chat reply): your FIRST character "
                "must be the first character of the section text itself. NEVER open with an "
                "acknowledgement (\"Certainly\", \"Sure\", \"Of course\", \"Absolutely\", \"Got it\"), NEVER "
                "announce the work (\"Here is the rewritten section\", \"Below is the revised version\"), "
                "NEVER put a separator line before the text, and NEVER close with an offer "
                "(\"Let me know if…\"). A reader sees this sentence on a published page.\n\n"
                f"SECTION TEXT:\n{src}"
            )
            try:
                got = self.writer.call_text(prompt, max_tokens=3000, temperature=temperature,
                                            timeout=timeout)
            except Exception:
                got = None
            got = _strip_model_preamble((got or "").strip())   # FU192
            # The model is told not to emit a heading, but enforce it deterministically: we re-emit the
            # ORIGINAL heading ourselves, so any heading line the model returns would duplicate it (and a
            # reworded one would trip the heading gate). Drop heading lines the source chunk didn't have.
            if got and not any(l.lstrip().startswith("#") for l in src.split("\n")):
                got = "\n".join(l for l in got.split("\n") if not l.lstrip().startswith("#")).strip()
            # a section rewrite must not drop this section's citations or its load-bearing facts
            if got:
                sec_cited = set(re.findall(r"\[S\d+\]", src))
                if not sec_cited <= set(re.findall(r"\[S\d+\]", got)):
                    got = ""
                elif not self._facts_preserved(src, got)[0]:
                    got = ""
                elif not self._price_cadence_ok(src, got)[0]:
                    got = ""                                 # FU176: price cadence drifted → keep original
                elif len(got) < 0.5 * len(src.strip()):     # truncated / stub
                    got = ""
            body_txt = got if got else chunk                 # safe degradation → keep the original section
            return i, ((head + "\n" + body_txt) if head is not None else body_txt), bool(got)

        # FU173: run the sections CONCURRENTLY. This was the dominant cost of a rewrite — 16 sections
        # issued one at a time (~20-25s each) is ~5-6 minutes of pure round-trip latency, while vLLM
        # batches concurrent requests and serves them for roughly the cost of the slowest one. Results are
        # collected by INDEX and reassembled in the ORIGINAL order, so the output is byte-identical to the
        # serial path for the same model replies (the FU151/FU159 collect-then-merge-in-order discipline).
        tasks = [(i, h, c) for i, (h, c) in enumerate(segs)]
        results = {}
        if len(tasks) > 1:
            with ThreadPoolExecutor(max_workers=min(_WRITER_WORKERS, len(tasks))) as _ex:
                for i, txt, done in _ex.map(_one, tasks):
                    results[i] = (txt, done)
        else:
            for t in tasks:
                i, txt, done = _one(t)
                results[i] = (txt, done)
        out_parts = [results[i][0] for i in range(len(tasks))]
        rewritten_n = sum(1 for i in range(len(tasks)) if results[i][1])
        if not rewritten_n:
            return None
        print(f"[writer] section-chunked rewrite: {rewritten_n}/{len(segs)} sections reworded", flush=True)
        return "\n".join(out_parts)

    # ── FU172 Change 4/6: targeted residual polish + semantic fact verification ───────────────────
    @staticmethod
    def _split_sentences_with_pos(body):
        """Sentences of the BODY paired with their exact source text, skipping structure we never touch
        (headings, bolded section labels, table rows, blockquotes and the ## Sources section)."""
        head = re.split(r"(?im)^\s*#{1,6}\s*sources\s*$", body or "", maxsplit=1)[0]
        out = []
        for line in head.split("\n"):
            st = line.strip()
            if not st or st.startswith("#") or st.startswith("|") or st.startswith(">") \
                    or re.fullmatch(r"\*\*[^*].*\*\*", st):
                continue
            for sent in re.split(r"(?<=[.!?])\s+", line):
                sent = sent.strip()
                if len(sent.split()) >= 5:
                    out.append(sent)
        return out

    def _residual_polish(self, claude_body, out, brand=None, extracted=None, verdicts=None, timeout=600):
        """FU172 Change 4 — surgical pass over ONLY the sentences still carrying DISCRETIONARY verbatim
        wording (the sole part that can hold a watermark). One small call instead of another ~700s re-roll.
        Every replacement is fact-gated per sentence; a sentence that drops a fact keeps its ORIGINAL.
        Returns the spliced body, or None when nothing was safely improved."""
        pa, pb = self._prose_for_overlap(claude_body), self._prose_for_overlap(out)
        spans, wb_raw, _ = self._residual_spans(pa, pb)
        vmap = verdicts or {}
        hot = []
        for st, en in spans:
            words = wb_raw[st:en]
            key = " ".join(words).lower()
            if len(self._discretionary_words(words, brand, (extracted or {}).get("atoms"), vmap.get(key))) >= 5:
                hot.append(" ".join(words))
        if not hot:
            return None
        keep_whole = [x.lower() for x in (extracted or {}).get("verbatim_sentences", [])]
        cands = []
        for sent in self._split_sentences_with_pos(out):
            if sent.lower() in keep_whole or _CRITICAL_DIRECTIVE_RE.search(sent):
                continue                                   # never touch a critical directive
            if any(h.lower() in " ".join(re.findall(r"\w+", sent)).lower() for h in hot):
                if out.count(sent) == 1:                    # splice only on a unique match
                    cands.append(sent)
            if len(cands) >= 12:
                break
        if not cands:
            return None
        numbered = "\n".join(f"{i+1}. {c}" for i, c in enumerate(cands))
        try:
            got = self.writer.call_text(
                "Recast each numbered sentence COMPLETELY in different words — different structure, "
                "different connectives, different sentence openers. Share no run of more than 4 consecutive "
                "words with the original.\n"
                "KEEP EXACT: every [S#] marker, every number/price/date with its unit, every product, brand or "
                "company name, every code, and the exact scope of every negation.\n"
                "Return ONLY the rewritten sentences, numbered the same way, same count, one per line.\n\n"
                + numbered, max_tokens=2000, temperature=1.0, timeout=timeout)
        except Exception as e:
            print(f"[writer] residual polish failed ({e})", flush=True)
            return None
        got = _strip_model_preamble(got or "")   # FU192: a lead-in line broke the exact-count match
        lines = [re.sub(r"^\s*\d+[.)]\s*", "", l).strip() for l in (got or "").split("\n") if l.strip()]
        if len(lines) != len(cands):
            print(f"[writer] residual polish: count mismatch ({len(lines)} vs {len(cands)}) — discarded",
                  flush=True)
            return None
        body, applied = out, 0
        for orig, rep in zip(cands, lines):
            if not rep or rep == orig:
                continue
            ok, _missing = self._facts_preserved(orig, rep, brand)
            if not ok or not self._price_cadence_ok(orig, rep)[0]:
                continue                                   # dropped a fact/cadence → keep the original
            body = body.replace(orig, rep, 1)
            applied += 1
        print(f"[writer] residual polish: {applied}/{len(cands)} sentences reworded", flush=True)
        return body if applied else None

    def _verify_facts_semantic(self, claude_body, out, brand=None, timeout=600):
        """FU172 Change 6 — the FINAL check, and the counterweight that makes the extra rewriting freedom
        safe. The deterministic gate only checks a fact TOKEN still APPEARS, so it cannot see 'once weekly'
        → 'once daily', a flipped negation, a price re-attached to the wrong brand, a dropped condition, or
        a hardened hedge. Claude compares original vs rewrite and flags meaning changes; flagged sentences
        are sent back to the writer (max 2 rounds) and any still-flagged sentence is REVERTED to Claude's
        original wording — correct facts beat a cleaner strip. Returns (body, n_reverted, verified_bool)."""
        if os.environ.get("WRITER_FACT_VERIFY", "1") == "0":
            return out, 0, False
        body, reverted = out, 0
        for _round in range(2):
            try:
                res = self.claude.call(
                    "Compare the REWRITE against the ORIGINAL. Report ONLY sentences where a FACT changed "
                    "meaning — a value or its unit, a PRICING structure (which figure is the intro vs the "
                    "ongoing price, the billing cadence, what the price covers, whose price it is), which "
                    "entity a fact belongs to, the scope of a negation, a dropped condition or exception, "
                    "hardened hedging, a date, or an identifier/code. Wording changes that preserve meaning "
                    "are CORRECT and must NOT be reported.\n"
                    'Return JSON ONLY: {"issues": [{"original": "...", "rewritten": "...", '
                    '"problem": "..."}]}\n\n'
                    f"ORIGINAL:\n{claude_body[:12000]}\n\nREWRITE:\n{body[:12000]}",
                    max_tokens=2000, temperature=0)
            except Exception as e:
                print(f"[writer] fact verification failed ({e}) — token-gated body kept", flush=True)
                return body, reverted, False
            if not isinstance(res, dict):
                return body, reverted, False
            issues = [i for i in (res.get("issues") or []) if isinstance(i, dict)
                      and str(i.get("rewritten") or "").strip() and str(i.get("original") or "").strip()]
            # FU176: the LLM comparison is not exhaustive — it MISSED a real "$249/month billed quarterly"
            # → "$249 quarterly" drift in a shipped rewrite. Add the deterministic cadence findings so the
            # same repair-or-revert machinery fixes them, sentence by sentence.
            for _co, _cr in self._cadence_sentence_pairs(claude_body, body):
                if not any(str(i.get("rewritten") or "").strip() == _cr for i in issues):
                    issues.append({"original": _co, "rewritten": _cr,
                                   "problem": "a price lost its billing cadence (e.g. '/month billed "
                                              "quarterly' must not become just 'quarterly')"})
            if not issues:
                print(f"[writer] fact verification: clean ({reverted} reverted)", flush=True)
                return body, reverted, True
            print(f"[writer] fact verification: {len(issues)} issue(s) — repair round {_round + 1}",
                  flush=True)
            # FU173: fetch the repairs CONCURRENTLY (they are independent single-sentence calls), then
            # APPLY them sequentially in the ORIGINAL issue order — `body.replace` mutates, so ordering
            # must stay deterministic. Gates and the revert-to-original fallback are unchanged.
            todo = [it for it in issues[:12] if str(it["rewritten"]).strip() in body]

            def _repair(it):
                bad, orig, why = str(it["rewritten"]).strip(), str(it["original"]).strip(), \
                    str(it.get("problem") or "")
                try:
                    rep = self.writer.call_text(
                        "Rewrite this sentence in your own words, but fix the factual error described.\n"
                        f"PROBLEM: {why}\nMUST MATCH THIS FACT EXACTLY: {orig}\n"
                        "Keep every [S#] marker. Return ONLY the corrected sentence.\n\n"
                        f"SENTENCE: {bad}", max_tokens=600, temperature=0.7, timeout=timeout)
                except Exception:
                    rep = None
                rep = _strip_model_preamble(rep or "") or None   # FU192
                return (rep or "").strip().split("\n")[0].strip()

            if len(todo) > 1:
                with ThreadPoolExecutor(max_workers=min(_WRITER_WORKERS, len(todo))) as _ex:
                    reps = list(_ex.map(_repair, todo))
            else:
                reps = [_repair(it) for it in todo]
            fixed = 0
            for it, rep in zip(todo, reps):
                bad, orig = str(it["rewritten"]).strip(), str(it["original"]).strip()
                if bad not in body:
                    continue
                if rep and self._facts_preserved(orig, rep, brand)[0]:
                    body = body.replace(bad, rep, 1)
                    fixed += 1
                else:
                    body = body.replace(bad, orig, 1)      # revert to Claude's original — facts win
                    reverted += 1
            if not fixed:
                break
        print(f"[writer] fact verification: finished with {reverted} sentence(s) reverted", flush=True)
        return body, reverted, True

    # ── FU172 Change 0/0b: the INTELLIGENT protect-list (extract → classify → verify) ─────────────
    @staticmethod
    def _atom_shape_ok(span):
        """Deterministic PRECISION guard against over-protection. An atom must look like a VALUE, CODE or
        NAME — not category wording. "compounding pharmacies" / "weight loss program" / "project management
        platform" are rejected; "503B", "2.5 mg", "3.9% APR", "SOC 2", "Ryan Haight Online Pharmacy Consumer
        Protection Act" and a whole pricing structure are kept. Vertical-neutral."""
        ws = (span or "").split()
        if not (1 <= len(ws) <= 14):
            return False
        if len(ws) == 1 and _is_emphasis_caps(ws[0]):     # FU187: "AND" is not a code
            return False
        # FU174: "contains a digit" was too loose — it re-admitted the BARE/RHETORICAL numbers FU169
        # deliberately stopped gating ("100", "100%"), so Qwen validly rewording "100% of patients" →
        # "virtually all patients" failed every attempt and fell back. A number only counts when it is
        # genuinely LOAD-BEARING (currency / decimal / unit-qualified / term) or part of a date.
        loadbearing = bool(_LOADBEARING_NUM_RE.search(span))
        dated = bool(re.search(r"(?i)\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+\d",
                               span) or re.search(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b", span))
        code = bool(re.search(r"\b\d+[A-Za-z]\b|\b[A-Z]{2,}\d*\b", span))    # 503A, FDA, MEN2, SOC
        marked = "®" in span or "™" in span
        # A SINGLE capitalised token is ambiguous — "LillyDirect"/"PeterMD" are names, but "Reputable" is
        # just sentence-initial prose. Internal capitalisation (or ALLCAPS) is the discriminator.
        named = any(re.search(r"[a-z][A-Z]", w) or re.fullmatch(r"[A-Z]{2,}\d*", w) for w in ws)
        caps = sum(1 for w in ws if re.match(r"[A-Z]", w))
        if not (loadbearing or dated or code or marked or named or caps >= 2):
            return False
        if len(ws) > 8 and not (loadbearing or dated):   # long spans only as value/pricing/date structures
            return False
        return True

    @staticmethod
    def _price_cadences(text):
        """FU176: map each money token to the normalized CADENCE terms attached to it. The window stops at
        the next price / ';' / '.' so a neighbouring price's terms cannot bleed in. Returns a sorted list
        of (amount, frozenset(cadences)) so two documents can be compared as multisets."""
        t = re.sub(r"\s+", " ", text or "")
        out = []
        for m in _MONEY_RE.finditer(t):
            rest = t[m.end():m.end() + 70]
            cut = len(rest)
            for stop in ("$", ";", ". ", "€", "£"):
                i = rest.find(stop)
                if i != -1:
                    cut = min(cut, i)
            win = m.group(0) + rest[:cut]
            tags = frozenset(tag for rx, tag in _CADENCE_PATTERNS if rx.search(win))
            out.append((m.group(0).replace(" ", ""), tags))
        return sorted(out)

    @classmethod
    def _cadence_sentence_pairs(cls, claude_text, out_text):
        """FU176: locate the SENTENCES behind a cadence drift, so the semantic verifier's repair-or-revert
        loop can fix them individually instead of failing the whole body. Returns [(original, rewritten)]
        for each rewrite sentence whose price lost a cadence its counterpart in the original still has."""
        ok, _ = cls._price_cadence_ok(claude_text, out_text)
        if ok:
            return []
        c_sents = cls._split_sentences_with_pos(claude_text)
        r_sents = cls._split_sentences_with_pos(out_text)
        pairs = []
        for rs in r_sents:
            amounts = {m.group(0).replace(" ", "") for m in _MONEY_RE.finditer(rs)}
            if not amounts:
                continue
            # Find the ORIGINAL sentence this one was rewritten FROM. Matching on shared amounts ALONE
            # mis-pairs (a table row full of prices matched a prose sentence about one of them), and a
            # mis-pair is dangerous: the repair loop reverts a failed fix to the "original", which would
            # then substitute UNRELATED text. So also require real word overlap, and skip when unsure.
            # Discriminator = the EXACT SET of amounts. Lexical similarity is useless here: a good rewrite
            # deliberately shares almost no words with its source, so word-overlap drops the true pair and
            # keeps false ones (a price-dense table row would match a prose sentence about one price).
            # Requiring the amount sets to be EQUAL pairs "…$149…$249…" with its counterpart and rejects
            # the table row outright. Position breaks ties between equally-matching originals.
            ri = r_sents.index(rs) / max(1, len(r_sents) - 1)
            best, best_gap = None, 1e9
            for ci, cs in enumerate(c_sents):
                cam = {m.group(0).replace(" ", "") for m in _MONEY_RE.finditer(cs)}
                if cam != amounts:
                    continue                # not the same set of prices → refuse to guess
                gap = abs(ci / max(1, len(c_sents) - 1) - ri)
                if gap < best_gap:
                    best, best_gap = cs, gap
            if best and not cls._price_cadence_ok(best, rs)[0]:
                pairs.append((best, rs))
        return pairs[:6]

    @classmethod
    def _price_cadence_ok(cls, claude_text, out_text):
        """FU176 HARD GATE: every price in the original must still carry its cadence in the rewrite.
        Compares multisets, so three identical '$249/month billed quarterly' phrases must all survive.
        Flags only a DROPPED cadence — a synonym swap ('/month' → 'per month' → both MONTH) passes.
        Returns (ok, problems[])."""
        from collections import Counter
        want, got = Counter(cls._price_cadences(claude_text)), Counter(cls._price_cadences(out_text))
        problems = []
        for (amt, tags), n in want.items():
            if not tags:
                continue                     # a bare price with no stated cadence — nothing to preserve
            missing = n - got.get((amt, tags), 0)
            if missing > 0:
                have = sorted({",".join(sorted(t)) or "none" for (a, t) in got if a == amt})
                problems.append(f"{amt} lost its cadence ({','.join(sorted(tags))} → {have or ['absent']})")
        return (not problems), problems

    @staticmethod
    def _gate_atoms(atoms):
        """FU174: which extracted atoms may be enforced VERBATIM by the fact gate.

        The gate is a presence check, so a LONG span turns into "this whole phrase must survive
        word-for-word" — which is the opposite of what we want. A 9-word pricing structure
        ("starting at $149/month then $249/month billed quarterly for 60mg") made every rewrite of that
        sentence fail and fall back. The structure still matters, but it is a MEANING question, so it is
        checked by the semantic verifier (_verify_facts_semantic: intro-vs-ongoing, cadence, what it
        covers, whose price) — while the token gate holds only the short, indivisible values.

        FU181: it ALSO skips an atom that is, in whole, a load-bearing NUMBER. Those are already
        enforced by `_facts_preserved`'s number path — which compares the digit CORE against a
        comma-stripped body, so "$1,300" ≡ "$1300" — whereas re-checking them here is a raw literal
        match that can only ADD false failures (a moved comma, a reformatted amount). Protection is
        unchanged; the redundancy that produced the FU169 / FU174 / FU181 fallbacks is what goes. The
        atom itself stays in the PRESERVE list and the metric — only this literal gate stops re-checking
        it. Anything with semantic content ("2.5 mg", "503B", "Zepbound®", a brand) is NOT a full
        number match and is still enforced verbatim."""
        out = []
        for a in atoms or []:
            # Trailing sentence punctuation is never part of a fact. Trim it here too, so an atom that
            # reaches us from a CACHED article["writer_facts"] or straight from the model is still
            # recognised as a plain number below (and so the literal check can't hinge on a comma).
            a = (a or "").strip().rstrip(",.;:")
            if not a or len(a.split()) > 3:
                continue
            if _LOADBEARING_NUM_RE.fullmatch(a):
                continue        # the regex floor already gates this one, normalized
            if _is_emphasis_caps(a):
                continue        # FU187: an emphasis capital is never enforceable verbatim
            out.append(a)
        return out

    def _extract_protected_facts(self, claude_body, brand=None):
        """FU172 Change 0 — run FIRST: ask Claude for the spans that must survive verbatim, then VERIFY them
        deterministically. Strict in BOTH directions: protecting too little risks a wrong fact; protecting
        too much leaves text the rewriter won't touch, which RAISES the residual. Returns
        {"atoms": [...], "verbatim_sentences": [...]}; never raises (→ {} on any failure = regex-only path)."""
        if os.environ.get("WRITER_FACT_EXTRACT", "1") == "0" or not (claude_body or "").strip():
            return {}
        cat = ((brand or {}).get("category") or "").strip()
        ctx = ((brand or {}).get("context") or "").strip()[:400]
        try:
            res = self.claude.call(
                "You are protecting an article before it is REWRITTEN word-for-word by another model.\n"
                "List ONLY the spans that must survive VERBATIM because rewording them would make a FACT "
                "WRONG.\n\n"
                f"THE BRAND'S DOMAIN: {cat or 'unknown'}. {ctx}\n\n"
                "APPLY THIS SINGLE TEST to every candidate: 'if this were reworded, would a fact become "
                "WRONG?' If no, DO NOT list it.\n"
                "INCLUDE (whatever fits this domain): exact values with their units; PRICES AS A WHOLE "
                "STRUCTURE (e.g. an intro price, the ongoing price, the billing cadence and what it covers "
                "belong together in ONE span — the relationship is the fact, not just the figures); dates; "
                "numeric thresholds; product / brand / company names; statute, regulation and standard "
                "names or codes; certifications; identifiers.\n"
                "Cross-domain examples so you do not assume a vertical: a seat count and 'SOC 2' for "
                "software; an APR and its term for lending; a statute name and jurisdiction for legal; a "
                "dose and a regulation code for healthcare; a part number and a tolerance for manufacturing.\n"
                "EXCLUDE category wording that can be freely reworded (e.g. 'compounding pharmacies', "
                "'weight loss program', 'project management platform', 'flexible pricing').\n"
                "EXCLUDE rhetorical or incidental numbers that carry no fact — '100% online', '24/7', "
                "'thousands of patients', a count of list items. Rewording those changes nothing, and "
                "locking them only stops the rewrite doing its job.\n"
                "USE THE SHORTEST SPAN that carries the fact — a long span needlessly locks the prose "
                "around it.\n"
                "Also return up to 8 WHOLE sentences that cannot be safely reworded at all because their "
                "meaning turns on scope: a contraindication, a dosing schedule, a safety negation, a "
                "regulatory obligation, an eligibility rule, a licence restriction, or quoted/reported "
                "regulator wording.\n\n"
                'Return JSON ONLY: {"atoms": ["..."], "verbatim_sentences": ["..."]}\n\n'
                f"ARTICLE:\n{claude_body[:14000]}", max_tokens=2000, temperature=0)
        except Exception as e:
            print(f"[writer] fact extraction failed ({e}) — regex-only path", flush=True)
            return {}
        if not isinstance(res, dict):
            return {}
        low = (claude_body or "").lower()
        atoms, rejected = [], 0
        for a in (res.get("atoms") or [])[:200]:
            a = str(a or "").strip()
            if not a or a.lower() not in low:          # anti-hallucination: must exist in the body
                continue
            if not self._atom_shape_ok(a):             # anti-over-protection: generic wording rejected
                rejected += 1
                continue
            atoms.append(a)
        sents = [str(x or "").strip() for x in (res.get("verbatim_sentences") or [])[:8]]
        sents = [x for x in sents if x and x.lower() in low and _CRITICAL_DIRECTIVE_RE.search(x)]
        # RECALL audit — anything the regex floor catches but the model missed is added back.
        missed = 0
        # sorted(): set iteration order varies with PYTHONHASHSEED, which made the PRESERVE list — and
        # therefore the whole rewrite prompt — differ between runs on identical input. Deterministic now.
        for tok in sorted(set(re.findall(r"\b\d+[A-Za-z]\b|\b[A-Z]{3,}\d*\b", claude_body or "")) |
                          set(m.group(0) for m in _LOADBEARING_NUM_RE.finditer(claude_body or ""))):
            # FU181: also trim TRAILING sentence punctuation. These tokens bypass _atom_shape_ok (that
            # gate only screens model-supplied atoms), so whatever the regex hands back is enforced
            # verbatim — a stray "," or "." riding along turns a preserved fact into a false failure.
            tok = tok.strip().rstrip(",.;:")
            if tok and not any(tok.lower() in a.lower() for a in atoms):
                atoms.append(tok)
                missed += 1
        # MASS cap — the list may never lock more than ~25% of the body (longest dropped first).
        body_words = max(1, len(re.findall(r"\w+", claude_body or "")))
        cap = float(os.environ.get("WRITER_FACT_MASS_CAP", "0.25"))
        atoms.sort(key=lambda a: -len(a.split()))
        kept, used = [], 0
        for a in atoms:
            w = len(a.split())
            if (used + w) / body_words > cap:
                continue
            kept.append(a)
            used += w
        print(f"[writer] facts: {len(kept)} atoms + {len(sents)} verbatim sentences, locking "
              f"{used / body_words:.1%} of the body (model missed {missed} → restored, "
              f"rejected {rejected} generic)", flush=True)
        return {"atoms": kept, "verbatim_sentences": sents}

    def _classify_spans(self, spans, brand=None, extracted=None):
        """FU172 Change 0b/2 — Claude labels each surviving span fact-bearing / discretionary / structural.
        BOUNDED TRUST (the anti-gaming core): a verdict that makes the score STRICTER (→ discretionary) is
        always applied; a verdict that makes it more LENIENT (→ fact-bearing/structural) is accepted ONLY
        when the span is backed by the VERIFIED protect-list. So the classifier can never inflate a grade by
        asserting that ordinary prose is a fact. Returns {span_lower: verdict}; {} on any failure."""
        if os.environ.get("WRITER_FACT_EXTRACT", "1") == "0" or not spans:
            return {}
        ex = [a.lower() for a in (extracted or {}).get("atoms", [])]
        bt = self._brand_tokens(brand)
        try:
            res = self.claude.call(
                "Each line below is a phrase that survived UNCHANGED when an article was rewritten.\n"
                "Label each: 'fact' (rewording it would make something WRONG — a value, name, code, date, "
                "statute, certification or identifier), 'structural' (a heading or section label), or "
                "'generic' (ordinary category wording that could have been freely reworded).\n"
                "Be strict: prefer 'generic' unless rewording would genuinely break a fact.\n"
                'Return JSON ONLY: {"verdicts": [{"span": "...", "class": "fact|generic|structural"}]}\n\n'
                + "\n".join(f"- {sp}" for sp in spans[:60]), max_tokens=2000, temperature=0)
        except Exception as e:
            print(f"[writer] span classification failed ({e}) — mechanical rules only", flush=True)
            return {}
        if not isinstance(res, dict):
            return {}
        out, promoted, unprotected = {}, 0, 0
        for v in (res.get("verdicts") or []):
            sp = str(v.get("span") or "").strip()
            cl = str(v.get("class") or "").strip().lower()
            if not sp:
                continue
            key = " ".join(re.findall(r"\w+", sp)).lower()
            if cl == "generic":                       # STRICTER → always accepted, no evidence needed
                out[key] = "discretionary"
                unprotected += 1
            elif cl in ("fact", "structural"):        # LENIENT → must be backed by verified evidence
                backed = any(a in sp.lower() or sp.lower() in a for a in ex) or \
                    any(w.lower() in bt for w in re.findall(r"\w+", sp))
                if backed:
                    out[key] = "fact-bearing" if cl == "fact" else "structural"
                    promoted += 1
        print(f"[writer] classifier: {unprotected} unprotected, {promoted} promoted (evidence-backed)",
              flush=True)
        return out

    @staticmethod
    def _restore_headings(orig_heads, rewritten_body):
        """FU154: replace the rewrite's heading lines positionally with the ORIGINAL headings — so a
        reworded heading is put back to its exact original text + level while the reworded PROSE (the
        watermark-stripped part) is kept. Returns the restored body when the heading COUNT matches;
        None when it differs (a genuine section add/drop → the caller rejects that rewrite)."""
        lines = (rewritten_body or "").split("\n")   # split (not splitlines) → faithful round-trip
        idxs = [i for i, l in enumerate(lines) if l.lstrip().startswith("#")]
        if len(idxs) != len(orig_heads or []):
            return None
        for k, i in enumerate(idxs):
            lines[i] = orig_heads[k]
        return "\n".join(lines)

    def _writer_evidence_str(self, char_budget=None):
        """FU166: render the FULL post-sourcing evidence (self._evidence_blocks — which now carry the
        FDA/official/pricing sources fetched AFTER generate_article) as "[S#] label — url\\ntext" blocks,
        numbered [S1..Sn] to MATCH self._evidence_blocks order so _rebuild_sources maps the writer's
        citations correctly. When char_budget is set, spread it across ALL blocks (trim each block's TEXT)
        so every source stays represented — never drop whole (later-appended) sources to fit."""
        blocks = getattr(self, "_evidence_blocks", None) or []
        if not blocks:
            return ""
        per = 2500
        if char_budget:
            per = max(400, min(2500, (int(char_budget) // len(blocks)) - 140))
        parts = []
        for i, b in enumerate(blocks, 1):
            lbl = (b.get("label") or "").strip()
            url = (b.get("url") or "").strip()
            txt = (b.get("text") or "").strip()[:per]
            head = f"[S{i}] {lbl}" + (f" — {url}" if url else "")
            parts.append(head + ("\n" + txt if txt else ""))
        return "\n\n".join(parts)

    def _apply_writer_pass(self, article, draft_body, brand, seed, surface="blog"):
        """FU153: re-author the finished blog body on the self-hosted open model so a Claude SynthID
        watermark is replaced by the open model's tokens. Modes (self.writer_mode):
          - 'rewrite' — re-compose Claude's finished body sentence-by-sentence (preserve [S#]/facts).
          - 'compose' — write the article from Claude's gathered evidence + outline.
        Claude still did ALL research/sourcing/verification; this is only the final prose author.
        A QUALITY gate (length / [S#] citations / headings) FALLS BACK to the Claude body on failure;
        a WATERMARK gate (verbatim overlap) retries-then-warns but NEVER falls back (that would
        reinstate the watermark). Returns the final body_markdown. Records writer_* fields on
        `article`. Never raises → returns the Claude body on any trouble.

        FU179: `surface` selects the PRESERVE list + the structural gates (_WRITER_SURFACES) so the two
        derived LinkedIn surfaces get the SAME fact machinery over their own structure. The caller still
        passes the text as `article["body_markdown"]`; 'blog' is byte-identical to pre-FU179."""
        try:
            claude_body = article.get("body_markdown") or ""
            if not self.writer or not claude_body.strip():
                return claude_body
            _sf = _WRITER_SURFACES.get(surface) or _WRITER_SURFACES["blog"]
            _is_blog = surface == "blog"
            name = (brand or {}).get("name") or "the brand"
            cited = set(re.findall(r"\[S\d+\]", claude_body))          # what Claude actually cited
            heads = [l.strip() for l in claude_body.splitlines() if l.lstrip().startswith("#")]
            n_ev = len(self._evidence_blocks or [])
            import time as _t
            # FU173: per-stage wall-clock, so a slow run explains itself instead of being a black box.
            _stage = {"extract": 0.0, "attempts": 0.0, "sections": 0.0, "classify": 0.0,
                      "polish": 0.0, "verify": 0.0, "cold_start": 0.0}
            # FU173: is the model LOADED? probe() returns fast (it never blocks on a full cold start), so
            # this cheaply separates "the GPU was loading" from "generation is slow" — the question the
            # operator could not answer before. The probe ALSO wakes the Modal container, so the load it
            # reports starts here rather than inside the first real call.
            _cold_t0 = _t.time()
            try:
                _pr = self.writer.probe(timeout=8) if hasattr(self.writer, "probe") else {"state": "ok"}
            except Exception:
                _pr = {"state": "unknown"}
            _was_cold = (_pr or {}).get("state") != "ok"
            if _was_cold:
                print(f"[writer] model COLD ({(_pr or {}).get('state')}) — loading before the rewrite; "
                      f"this run pays the GPU load time", flush=True)

            # FU172 Change 0 — the INTELLIGENT protect-list, computed BEFORE any rewriting and cached.
            _x0 = _t.time()
            _extracted = article.get("writer_facts")
            if _extracted is None and self.writer_mode == "rewrite":
                _extracted = self._extract_protected_facts(claude_body, brand)
                article["writer_facts"] = _extracted
            _stage["extract"] = round(_t.time() - _x0, 1)
            _extracted = _extracted or {}
            _atoms = _extracted.get("atoms") or []

            def _build_prompt(aggressive=False):
                harder = ("\n\nIMPORTANT: a previous attempt reused too much of the original wording. "
                          "Rewrite the PROSE FAR more aggressively — share NO run of more than 4 words with the "
                          "original body text; replace EVERY discretionary word, reorder clauses and sentences, "
                          "and reword right up to each preserved atom. In particular FULLY RECAST every "
                          "multi-sentence factual / regulatory paragraph and every FAQ answer (change the sentence "
                          "order and structure, not just a few words) — a surviving 5+-word run almost always comes "
                          "from a factual or FAQ sentence left too close to the original; the ONLY sentences you may "
                          "keep verbatim are the narrow contraindication / dosing-schedule / safety-negation ones. "
                          "But copy every [S#] and every heading "
                          "line CHARACTER-FOR-CHARACTER — only the paragraph text under the headings changes.") if aggressive else ""
                if self.writer_mode == "compose":
                    # FU165: give Qwen the EXACT SAME article-writing prompt Claude used (all the rules +
                    # brand context + evidence, captured on self._article_prompt in generate_article) — so
                    # Qwen writes its OWN complete article from the SAME instructions, matching Claude's
                    # structure/depth/length. NO reference to Claude's OUTPUT (that would be rewriting). Only
                    # the OUTPUT FORMAT is overridden: Markdown body, not the JSON envelope Claude returns.
                    base = getattr(self, "_article_prompt", "") or ""
                    if base:
                        # FU166: swap the EARLY evidence chunk in the reused prompt for the FULL post-sourcing
                        # evidence (FDA/official/pricing sources are appended AFTER generate_article) so Qwen
                        # composes from the SAME material Claude's final body used — not the stale pre-sourcing
                        # set (which left the body citing only S1-S3 and gutted the clinical depth).
                        early_ev = getattr(self, "_article_prompt_ev", "") or ""
                        fixed_len = len(base) - len(early_ev)                 # rules + brand, minus the old evidence
                        ev_budget = max(6000, 52000 - fixed_len)             # keep all rules; fit evidence to context
                        final_ev = self._writer_evidence_str(char_budget=ev_budget)
                        if early_ev and final_ev:
                            base = base.replace(early_ev, "\n" + final_ev + "\n", 1)
                        base = base[:55000]   # hard safety bound (Qwen --max-model-len 24576 ≈ 14k-tok in + 9k out)
                        return (base
                                + "\n\nOUTPUT FORMAT — IGNORE any JSON instruction above. Return ONLY the "
                                "finished Markdown ARTICLE BODY (the value that would go in \"body_markdown\"): "
                                "start with the first heading, cover EVERY section in full, no JSON, no code "
                                "fences, no preamble, no commentary.\n"
                                "STRUCTURE: use ## (H2) for each MAIN section — the question-shaped ## headings "
                                "are the retrieval anchors an engine lifts for a sub-query; use ### ONLY for "
                                "sub-points WITHIN a section, NEVER as the main section level (do not put every "
                                "section at ###). Keep the FULL clinical/technical depth and every specific "
                                "number, dose, threshold and named [S#] source from the EVIDENCE — do NOT "
                                "compress a well-sourced section into one or two sentences." + harder)
                    # Fallback (compose invoked without a preceding generate_article — not the normal path):
                    blocks = [f"[S{i}] {bl.get('label','')} — {bl.get('url','')}\n{(bl.get('text') or '')[:2500]}"
                              for i, bl in enumerate(self._evidence_blocks or [], 1)]
                    ev = "\n\n".join(blocks) if blocks else "(no external evidence)"
                    outline = "\n".join(heads) if heads else "(derive a clear structure)"
                    return (
                        f"You are writing a first-party blog article for {name}. Target query / H1: \"{seed}\".\n\n"
                        "Write a COMPLETE, comprehensive article in Markdown from the EVIDENCE below — a full "
                        "'## Quick answer', question-shaped H2/H3 headings answered in their FIRST sentence, the "
                        "full comparison table and FAQ, every non-obvious fact cited inline as [S#], no fabrication, "
                        "ending with a '## Sources' line. Return ONLY the Markdown article.\n\n"
                        f"OUTLINE (headings to cover):\n{outline}\n\nEVIDENCE (the ONLY source of facts):\n{ev}{harder}"
                    )
                # rewrite mode — FU167: mechanism-derived (the watermark = a secret key + the ~4 preceding
                # words → the chosen word, only among low-stakes/discretionary choices; sparse on facts). So
                # the removal is: replace every discretionary word, break every verbatim run past the ~4-word
                # context window, and reword the context around every preserved atom.
                return (
                    f"Re-compose the following finished blog article for {name} ENTIRELY in your own words — a "
                    "FULL rewrite, not a light edit.\n\n"
                    "WHY (do this precisely): the original was written by another AI whose word-choices carry a "
                    "hidden statistical mark. The mark lives in the DISCRETIONARY word choices and phrasing, NOT "
                    "in the facts. To remove it:\n"
                    "- REPLACE EVERY discretionary word — connectives, transitions, adjectives, verbs, adverbs, "
                    "sentence openers — with a different word or phrasing, and RECAST every sentence's structure.\n"
                    "- Share NO run of MORE THAN 4 consecutive words with the original (a run of 5+ preserves a "
                    "marked word plus its context — this is the single most important rule).\n"
                    "- REORDER clauses and sentences wherever the meaning allows — this changes the words that "
                    "precede each word.\n"
                    "- When you MUST keep an atom verbatim (see PRESERVE below), REWORD the words IMMEDIATELY "
                    "BEFORE and AFTER it — never leave the original phrasing touching a preserved atom.\n\n"
                    "PRESERVE EXACTLY (do not change, drop, move, or renumber — but reword the prose around them):\n"
                    + ("- every inline citation marker like [S1], [S2] … keep each where it supports its claim;\n"
                       if _is_blog else "") +
                    "- EVERY heading line (starting with #, ##, or ###) — copy it CHARACTER-FOR-CHARACTER; "
                    "never reword, rephrase, shorten, translate, or restructure a heading. Rewrite ONLY "
                    "the paragraph text UNDER the headings;\n"
                    "- every number, dose, %, price, date, and product/drug/brand name;\n"
                    + (("- THESE EXACT SPANS, character-for-character (this is the authoritative list — "
                        "everything NOT on it is yours to recast freely):\n"
                        + "".join(f"    • {a}\n" for a in _atoms[:120])) if _atoms else "")
                    + (("- KEEP THESE SENTENCES WORD-FOR-WORD (their meaning turns on exact scope):\n"
                        + "".join(f"    • {x}\n" for x in (_extracted.get("verbatim_sentences") or [])))
                       if _extracted.get("verbatim_sentences") else "") +
                    "- every NEGATION and clinical DIRECTIVE that carries meaning — 'not', 'no', 'contraindicated', "
                    "'may not', 'required', 'only', 'not FDA-approved', 'not a controlled substance' — keep these "
                    "words and their scope EXACT (moving or dropping one flips the meaning);\n"
                    + ("- every Markdown table (structure and cell values);\n"
                       "- the '## Sources' section at the end.\n" if _is_blog
                       else "".join(b + "\n" for b in _sf["preserve"])) +
                    "REWORD the wording of EVERY sentence, INCLUDING factual, regulatory, clinical and FAQ sentences "
                    "— keep the preserved items above EXACT and NEVER change a fact's meaning, a negation, a "
                    "comparison, or a clinical directive. Descriptive factual / regulatory / FAQ sentences "
                    "(definitions, dates, eligibility ranges, pricing prose, logistics, certifications, timelines, "
                    "process steps) MUST be recast — do NOT leave one near-verbatim; and each FAQ answer must be "
                    "phrased differently from the body and from the question (never paste a body sentence into it). "
                    "NARROW SAFETY FALLBACK: keep a WHOLE sentence verbatim ONLY when it states a CONTRAINDICATION, "
                    "a DOSING instruction/escalation schedule, or a safety NEGATION whose scope you cannot preserve "
                    "while rewording (e.g. a medullary-thyroid/MEN2 contraindication, the 2.5 mg dose-escalation "
                    "schedule, 'not a controlled substance', 'not FDA-approved'). That is a LAST resort for a "
                    "genuinely meaning-critical clinical statement — NEVER a default for a sentence that merely "
                    "sounds clinical or regulatory.\n"
                    "Meaning, facts, structure and citations stay identical; only the WORDING changes.\n"
                    # FU193 — this branch carried NO output-discipline rule at all, so a chat preamble
                    # here would land at the very top of the article. Same forbid-only wording as the
                    # section pass.
                    "OUTPUT DISCIPLINE (this is PUBLISHED PROSE, not a chat reply): your FIRST character "
                    "must be the first character of the article itself. NEVER open with an "
                    "acknowledgement (\"Certainly\", \"Sure\", \"Of course\"), NEVER announce the work "
                    "(\"Here is the rewritten article\"), and NEVER close with an offer.\n"
                    # FU185 (Change 6) — the FOUR editorial rules the rewrite branch never carried. Each
                    # only FORBIDS a regression; none asks the rewrite to do anything new, so they cannot
                    # move prose quality in either direction, only narrow the band of allowed outcomes.
                    "DO NOT REGRESS any of these while rewording:\n"
                    "- THE PUNT BAN IS ON MEANING: never recast a stated value into 'not specified', "
                    "'varies', 'unclear' or 'check their site'. If the original states a value, state THAT "
                    "value.\n"
                    "- NO SUPERLATIVE OR PROMOTIONAL ESCALATION: never strengthen a hedged statement into "
                    "a superlative or an unqualified claim; keep the SAME confidence level the original "
                    "had.\n"
                    "- KEEP THE BALANCE: the 'who might prefer an alternative' element and any stated "
                    "limitation or trade-off must SURVIVE the rewrite.\n"
                    "- DESCRIBE SOURCES HONESTLY: never upgrade a third-party or review source into "
                    "'independent audit' / 'independently verified' framing while rewording around its "
                    "citation.\n"
                    + ("Return ONLY the rewritten Markdown article, nothing else.\n\n" if _is_blog else
                       f"Return ONLY the rewritten {_sf['label']}, nothing else.\n\n")
                    + (f"ARTICLE:\n{claude_body}{harder}" if _is_blog
                       else f"{_sf['label'].upper()}:\n{claude_body}{harder}")
                )

            def _valid(out):
                # FU165: COMPOSE must ALWAYS ship (operator: compose is non-negotiable) — so only a
                # genuinely EMPTY / non-article output hard-fails (there'd be nothing real to ship, and
                # the ONLY alternative is Claude's WATERMARKED body). A leaner-than-Claude length or a
                # stray/out-of-range [S#] is NOT a failure here (the length is fixed by the prompt's
                # reference/depth rule, and `_rebuild_sources` renumbers/drops a stray citation safely).
                # REWRITE is unchanged: its output SHOULD mirror Claude's body, so the length band +
                # citation/heading preservation still hard-gate it.
                if not out or not out.strip():
                    return False, "empty"
                if self.writer_mode == "compose":
                    has_head = any(l.lstrip().startswith("#") for l in out.splitlines())
                    if len(out.strip()) < 200 or not has_head:   # reject only a non-article stub — real
                        return False, f"not a usable article (len {len(out.strip())}, headings={has_head})"
                    return True, ""      # FU165: any real article ships (length/citations → soft warnings)
                # rewrite mode — the output must mirror Claude's finished body
                # FU179: the band is SURFACE-driven. A LinkedIn post has a hard fold/length contract, so
                # the blog's 0.6-1.4 would wave through a gutted post; 'blog' keeps 0.6-1.4 exactly.
                _lo, _hi = _sf["band"]
                if not (_lo * len(claude_body) <= len(out) <= _hi * len(claude_body)):
                    return False, f"length {len(out)} outside band"
                # FU179: structural survival for the derived surfaces — nothing else guards these, and
                # each is load-bearing: the CTA URL is the only path back to the site, the hashtags are
                # the post's distribution, and a Markdown heading in a LinkedIn post renders literally.
                if _sf["urls"]:
                    # FU181: trim TRAILING sentence punctuation off each captured URL. `[^\s)>\]]+`
                    # happily eats the "." or "," that ends the sentence, so a rewrite that merely
                    # MOVED the link mid-sentence looked like it had dropped it — the same
                    # punctuation-in-the-token bug as the price gate, in a gate shipped a day earlier.
                    def _urls(s):
                        return {u.rstrip(".,;:!?") for u in re.findall(r"https?://[^\s)>\]]+", s)}
                    _u_miss = sorted(_urls(claude_body) - _urls(out))
                    if _u_miss:
                        return False, f"dropped URL(s) {_u_miss[:2]}"
                if _sf["tags"]:
                    _t_src = re.findall(r"(?<!\w)#\w[\w-]*", claude_body)
                    _t_out = re.findall(r"(?<!\w)#\w[\w-]*", out)
                    if sorted(t.lower() for t in _t_src) != sorted(t.lower() for t in _t_out):
                        return False, (f"hashtags changed ({len(_t_src)}→{len(_t_out)})")
                if _sf["plain"]:
                    # a heading is `#` + space; a hashtag is `#` + word — only the former is illegal here
                    _bad = [l.strip() for l in out.splitlines() if re.match(r"\s*#{1,6}\s+\S", l)]
                    if _bad or re.search(r"(?m)^\s*(?:\|.*\||-{3,}|\*{3,}|_{3,})\s*$", out):
                        return False, f"Markdown structure introduced into a plain-text post {_bad[:1]}"
                out_cited = set(re.findall(r"\[S\d+\]", out))
                if not cited <= out_cited:
                    return False, f"dropped citations {sorted(cited - out_cited)}"
                if heads:
                    out_heads = {l.strip().lower() for l in out.splitlines() if l.lstrip().startswith("#")}
                    miss = [h for h in heads if h.lower() not in out_heads]
                    if miss:
                        return False, f"dropped headings {miss[:3]}"
                # FU168: fact-integrity gate — a rewrite that dropped/altered any load-bearing fact token
                # (number/dose/price, FDA/MTC/MEN2/503A…, ®-name, brand/competitor name) is UNSAFE → reject
                # (so aggressive rewording of factual sentences can't silently change a clinical fact).
                # FU174: only the SHORT atoms are enforced verbatim; long pricing/date structures inform
                # the prompt and are checked for MEANING by the semantic verifier instead.
                ok_f, missing_f = self._facts_preserved(claude_body, out, brand, self._gate_atoms(_atoms))
                if not ok_f:
                    return False, f"dropped facts {missing_f[:5]}"
                # FU176: a price that lost its cadence ("$249/month billed quarterly" → "$249 quarterly")
                # keeps every fact TOKEN, so the gate above passes it — but the meaning changed ~3x.
                ok_c, probs_c = self._price_cadence_ok(claude_body, out)
                if not ok_c:
                    return False, f"price cadence changed: {probs_c[:3]}"
                return True, ""

            # FU167: rewrite runs up to N escalating attempts and ships the LOWEST-(overlap, longest_run)
            # valid one (not the last); compose keeps its 2-attempt / ship-first-valid behavior.
            _attempts = int(os.environ.get("WRITER_REWRITE_ATTEMPTS", "4")) if self.writer_mode == "rewrite" else 2
            # FU170: on a LONG article the section-chunked stage below is what actually clears the residual,
            # and the extra whole-article retries measurably do NOT (a real 4-attempt run took 963s and every
            # attempt landed at the same ~0.29 overlap). So cap the whole-article attempts at 2 there — just
            # enough for a baseline — and spend that time on the section pass instead of a 3rd/4th re-roll.
            _long_article = (self.writer_mode == "rewrite"
                             and len(claude_body) >= int(os.environ.get("WRITER_SECTION_MIN_CHARS", "4000"))
                             and len(self._split_heading_segments(claude_body)) >= 3)
            best, best_rep, last_why, secs = None, None, "", 0.0

            def _run_sections():
                """The section-chunked rewrite as a reusable stage. Returns (body, report) or (None, None).
                Never raises."""
                nonlocal secs
                try:
                    _s0 = _t.time()
                    sec_out = self._rewrite_sections(
                        claude_body, name, temperature=1.1,
                        timeout=int(os.environ.get("WRITER_CALL_TIMEOUT", "600")))
                    _sdt = _t.time() - _s0
                    secs += _sdt
                    _stage["sections"] = round(_sdt, 1)
                    article["writer_secs"] = round(secs, 1)
                    if sec_out and heads:
                        _restored = self._restore_headings(heads, sec_out)
                        if _restored is not None:
                            sec_out = _restored
                    if not sec_out:
                        return None, None
                    ok_s, why_s = _valid(sec_out)
                    if not ok_s:
                        print(f"[writer] section-chunked pass rejected: {why_s}", flush=True)
                        return None, None
                    sec_out, _ = self._strip_invisible_chars(sec_out)
                    rep_s = self._watermark_removal_report(claude_body, sec_out, brand, _atoms)
                    print(f"[writer] section-chunked pass: n5-overlap {rep_s['n5_prose_overlap']:.2f} "
                          f"longest-run {rep_s['longest_shared_run']} grade={rep_s['grade']}", flush=True)
                    return sec_out, rep_s
                except Exception as _e:
                    print(f"[writer] section-chunked pass failed: {_e}", flush=True)
                    return None, None

            # FU175: on a LONG article run the SECTION pass FIRST. It is both faster (16 small calls in
            # parallel ≈ 40s) and better at recasting dense factual prose than one 9k-token whole-article
            # call (~212s, i.e. 78% of a measured 271s run) — whose output was then usually DISCARDED in
            # favour of the section result anyway. So do the cheap-and-better stage first and only pay for
            # the whole-article rewrite when the section pass fails or comes out weak.
            _sections_done = False
            if _long_article:
                _sec, _sec_rep = _run_sections()
                if _sec is not None:
                    best, best_rep, _sections_done = _sec, _sec_rep, True
                    if _sec_rep["grade"] in ("thorough", "strong"):
                        _attempts = 0        # good enough — skip the slow whole-article rewrite entirely
                        print("[writer] section pass is sufficient — skipping the whole-article rewrite",
                              flush=True)
                    else:
                        _attempts = 1        # weak → ONE whole-article attempt as an alternative
                else:
                    _attempts = 1            # section pass failed → fall back to the whole-article path
            for attempt in range(_attempts):
                # FU155/167: ramp the sampling temperature each attempt → more lexical diversity → lower
                # verbatim overlap (the gates + heading-restore still protect facts/structure).
                _temp = min(0.9 + 0.1 * attempt, 1.2)
                _t0 = _t.time()
                # FU164: a warm gen is ~2-5 min — 300s false-timed-out → fell back (wasted time +
                # reinstated the watermark). 600s + the app-level keep-warm (base.WriterClient.warm) fixes both.
                _timeout = int(os.environ.get("WRITER_CALL_TIMEOUT", "600"))
                out = _strip_model_preamble(self.writer.call_text(
                    _build_prompt(aggressive=(attempt > 0)),
                    max_tokens=9000, temperature=_temp, timeout=_timeout) or "") or None   # FU192
                _dt = _t.time() - _t0
                secs += _dt
                _stage["attempts"] += round(_dt, 1)
                if attempt == 0:
                    _stage["first_call"] = round(_dt, 1)   # a COLD model's load time lands in this call
                article["writer_secs"] = round(secs, 1)
                # Rewrite mode: put the ORIGINAL headings back positionally so a reworded heading isn't a
                # failure — only the PROSE is watermark-stripped. Section-COUNT change → restore returns
                # None → _valid's heading check rejects it.
                if self.writer_mode == "rewrite" and out and heads:
                    restored = self._restore_headings(heads, out)
                    if restored is not None:
                        out = restored
                ok, why = _valid(out)
                if not ok:
                    last_why = why
                    print(f"[writer] attempt {attempt+1} quality gate failed: {why}", flush=True)
                    continue
                # FU167: strip invisible-char carriers, then MEASURE the watermark-removal level
                # (n=5 discretionary-prose overlap + longest-shared-run + grade).
                out, _inv = self._strip_invisible_chars(out)
                rep = self._watermark_removal_report(claude_body, out, brand, _atoms)
                # keep the attempt with the LOWEST (overlap, longest_run) — never just the last one.
                if best is None or (rep["n5_prose_overlap"], rep.get("residual_share", 1.0)) < \
                        (best_rep["n5_prose_overlap"], best_rep.get("residual_share", 1.0)):
                    best, best_rep = out, rep
                # FU165: a compose shorter than the reference SHIPS (never fall back) — warn only.
                if self.writer_mode == "compose" and claude_body and len(out) < 0.6 * len(claude_body):
                    print(f"[writer] compose shorter than reference ({len(out)} vs {len(claude_body)}) "
                          f"— shipping anyway (FU165, watermark stripped)", flush=True)
                    article["writer_warning"] = (f"compose article is shorter than the reference "
                                                 f"({len(out)} vs {len(claude_body)} chars)")
                if self.writer_mode == "compose":
                    break   # compose writes fresh from evidence → ship the first valid (FU165/166)
                if best_rep["grade"] == "thorough":
                    break   # rewrite: watermark thoroughly stripped → stop early
                print(f"[writer] attempt {attempt+1}: n5-overlap {rep['n5_prose_overlap']:.2f} "
                      f"longest-run {rep['longest_shared_run']} grade={rep['grade']} — retrying harder", flush=True)

            if best is None:
                article["writer_mode_used"] = "fallback"
                article["writer_grade"] = "fallback"
                # FU173: record the timing/warmth on the FALLBACK path too — this is exactly when the
                # operator most needs to know the GPU was cold (a cold model timing out CAUSES fallback).
                article["writer_stage_secs"] = dict(_stage)
                article["writer_was_cold"] = bool(_was_cold)
                article["writer_warning"] = (f"⚠ watermark NOT stripped — fell back to Claude ({last_why})"
                                             if last_why else
                                             "⚠ watermark NOT stripped — fell back to Claude (rewrite failed validation)")
                print("[writer] stages: " + " · ".join(f"{k} {v:.0f}s" for k, v in _stage.items() if v)
                      + ("  (model was COLD)" if _was_cold else "  (model was warm)"), flush=True)
                print(f"[blog_gen] writer: FALLBACK {secs:.1f}s — {last_why or 'validation failed'} "
                      f"(watermark NOT stripped — "
                      f"{'the GPU was COLD — warm it first and retry' if _was_cold else 'the model was warm; check the timeout'})",
                      flush=True)
                return claude_body

            # FU170 — SECTION-CHUNKED FINAL STAGE (the structural lever). On a LONG, dense article the
            # whole-article retries above just reproduce the same anchored factual prose: the 72B must hold
            # every constraint at once (all citations + headings + tables + numbers + negations), so it
            # recasts the discretionary prose but leaves regulatory/FAQ sentences near-verbatim (4 attempts
            # still left ~57 runs of 5+ words → grade stuck at not-confirmed, and "regenerate" never cleared
            # it). Rewriting ONE section per call leaves few enough competing constraints that the model
            # actually recasts them. Runs only when we already HAVE a valid rewrite whose grade is weak and
            # the article is long enough for the constraint-load to be the problem — so short articles and a
            # broken/failing writer keep their existing behavior exactly.
            # FU175: only reached when the section pass has NOT already run (short article, or the
            # whole-article path was taken first). Keep-only-if-better is unchanged.
            if _long_article and not _sections_done and best_rep \
                    and best_rep["grade"] not in ("thorough", "strong"):
                _sec, _sec_rep = _run_sections()
                if _sec is not None and (_sec_rep["n5_prose_overlap"], _sec_rep.get("residual_share", 1.0)) < \
                        (best_rep["n5_prose_overlap"], best_rep.get("residual_share", 1.0)):
                    best, best_rep = _sec, _sec_rep   # keep it only if it's genuinely cleaner

            # ── FU172: classify the ACTUAL surviving spans, polish the discretionary residual, then
            # verify the facts semantically. Each stage keeps its result ONLY if it is genuinely better,
            # and any failure leaves the previous body untouched.
            _verdicts, _reverted, _verified = {}, 0, False
            if best and self.writer_mode == "rewrite":
                try:
                    _pa = self._prose_for_overlap(claude_body)
                    _pb = self._prose_for_overlap(best)
                    _sp, _wraw, _ = self._residual_spans(_pa, _pb)
                    _texts = [" ".join(_wraw[a:b]) for a, b in _sp if b - a >= 5]
                    _c0 = _t.time()
                    _verdicts = self._classify_spans(_texts, brand, _extracted)
                    _stage["classify"] = round(_t.time() - _c0, 1)
                    best_rep = self._watermark_removal_report(claude_body, best, brand, _atoms, _verdicts)
                except Exception as _e:
                    print(f"[writer] span classification skipped ({_e})", flush=True)
                if best_rep.get("grade") != "thorough":
                    try:
                        _t0 = _t.time()
                        _pol = self._residual_polish(claude_body, best, brand, _extracted, _verdicts,
                                                     timeout=int(os.environ.get("WRITER_CALL_TIMEOUT", "600")))
                        _dt = _t.time() - _t0
                        secs += _dt
                        _stage["polish"] = round(_dt, 1)
                        if _pol:
                            if heads:
                                _r = self._restore_headings(heads, _pol)
                                _pol = _r if _r is not None else _pol
                            ok_p, why_p = _valid(_pol)
                            if ok_p:
                                _pol, _ = self._strip_invisible_chars(_pol)
                                rep_p = self._watermark_removal_report(claude_body, _pol, brand, _atoms,
                                                                       _verdicts)
                                if (rep_p["longest_discretionary_run"],
                                        rep_p["residual_discretionary_share"]) < \
                                        (best_rep["longest_discretionary_run"],
                                         best_rep["residual_discretionary_share"]):
                                    best, best_rep = _pol, rep_p
                            else:
                                print(f"[writer] residual polish rejected: {why_p}", flush=True)
                    except Exception as _e:
                        print(f"[writer] residual polish skipped ({_e})", flush=True)
                try:
                    _t0 = _t.time()
                    _vb, _reverted, _verified = self._verify_facts_semantic(
                        claude_body, best, brand,
                        timeout=int(os.environ.get("WRITER_CALL_TIMEOUT", "600")))
                    _dt = _t.time() - _t0
                    secs += _dt
                    _stage["verify"] = round(_dt, 1)
                    if _vb and _vb != best:
                        ok_v, why_v = _valid(_vb)
                        if ok_v:
                            best = _vb
                            best_rep = self._watermark_removal_report(claude_body, best, brand, _atoms,
                                                                      _verdicts)
                        else:
                            print(f"[writer] fact-verified body rejected: {why_v}", flush=True)
                except Exception as _e:
                    print(f"[writer] fact verification skipped ({_e})", flush=True)
                article["writer_secs"] = round(secs, 1)

            _ov, _run, _grade = best_rep["n5_prose_overlap"], best_rep["longest_shared_run"], best_rep["grade"]
            _share = best_rep.get("residual_share", 0.0)
            _dshare = best_rep.get("residual_discretionary_share", 0.0)
            _drun = best_rep.get("longest_discretionary_run", 0)
            _floor = best_rep.get("detect_floor_words", 36)
            article["writer_mode_used"] = self.writer_mode
            article["writer_overlap"] = _ov
            article["writer_longest_run"] = _run
            article["writer_residual_share"] = _share
            article["writer_residual_discretionary"] = _dshare
            article["writer_longest_discretionary_run"] = _drun
            article["writer_facts_verified"] = bool(_verified)
            article["writer_facts_reverted"] = int(_reverted)
            article["writer_grade"] = _grade
            # FU173: report the model's WARM/COLD state plainly rather than inventing a "load time".
            # The GPU load happens INSIDE the first writer call, so we surface that call's duration and
            # let the operator compare it against the others — an honest signal instead of a derived
            # number that would just be the first call minus a guess.
            _stage.pop("cold_start", None)
            article["writer_stage_secs"] = dict(_stage)
            article["writer_was_cold"] = bool(_was_cold)
            # Report BOTH axes: the raw verbatim figure (transparency) and the free-choice figure the grade
            # actually turns on — the rest is facts/quotes/labels the rewrite is REQUIRED to keep, which
            # carry no watermark (the mark is weak-to-absent on low-entropy factual text).
            _res = (f"{_share * 100:.1f}% still verbatim, of which {_dshare * 100:.1f}% is free-choice prose "
                    f"(longest free-choice run {_drun} words vs a ~{_floor}-word detection floor)")
            _rev = f"; {_reverted} sentence(s) reverted to preserve facts" if _reverted else ""
            if _grade == "thorough":
                _wm = ""
            elif _grade == "strong":
                _wm = f"watermark-strip strong ({_res}) — proxy, not detector-verifiable{_rev}"
            else:
                # Only advise a regenerate when re-rolling can actually help. When the free-choice residual
                # is already far below the detection floor, the remainder is required-verbatim material and
                # regenerating cannot reduce it — saying otherwise sends the operator round a loop that
                # cannot converge (which is exactly what happened for ~19 rounds).
                _structural = _drun < _floor and _ov < 0.15
                _wm = (f"watermark-strip: the residual is the facts/quotes the rewrite must keep verbatim "
                       f"({_res}) — regenerating will NOT reduce it; review before publishing{_rev}"
                       if _structural else
                       f"watermark-strip: not fully confirmed ({_res}) — proxy; regenerate for a cleaner "
                       f"strip{_rev}")
            article["writer_warning"] = "; ".join(x for x in [article.get("writer_warning", ""), _wm] if x)
            # Show EVERY stage, including zeros — "attempts 0s" is itself the signal that the slow
            # whole-article rewrite was skipped because the section pass was already good enough.
            print("[writer] stages: " + " · ".join(f"{k} {v:.0f}s" for k, v in _stage.items())
                  + ("  (model was COLD — the GPU load is inside first_call)" if _was_cold
                     else "  (model was warm)"), flush=True)
            print(f"[blog_gen] writer: {self.writer_mode} {secs:.1f}s grade={_grade} "
                  f"n5_overlap={_ov:.2f} residual_share={_share:.3f} longest_run={_run} "
                  f"discretionary={_dshare:.3f} longest_disc_run={_drun}/{_floor} "
                  f"verified={_verified} reverted={_reverted} "
                  f"residual_sample={(best_rep.get('residual_sample') or '')[:90]!r}", flush=True)
            return best
        except Exception as e:
            print(f"[writer] pass errored ({e}) — keeping the Claude body.", flush=True)
            return article.get("body_markdown") or ""

    # FU197 — the shapes a phantom self-reference takes. Narrow on purpose: each one PROMISES a
    # separate {name} page, which is the thing that can dangle.
    _SELF_REF_RE = re.compile(
        r"(?:\bpublished\s+(?:a\s+|its\s+|their\s+)?(?:dedicated\s+)?"
        r"(?:guide|guidance|article|resource|breakdown|write-?up)"
        r"|\bdedicated\s+guide\b|\baccompanying\s+(?:guide|article|piece)\b"
        r"|\bcompanion\s+(?:guide|article|piece)\b"
        r"|\bour\s+(?:[\w-]+\s+){0,2}(?:guide|article|resource|write-?up)\b"
        r"|\brefer\s+to\s+[^.]{0,40}?(?:guide|guidance)\b)", re.I)

    # ── FU204 Change 2/3 — deterministic citation checks (warning only, never rewrite) ────────────
    # A shipped baby-bottle blog cited "clinically proven to reduce colic" to an Amazon listing,
    # "borosilicate glass, heat and thermal shock-resistant" to two Trustpilot RATING pages, and a
    # Nanobebe price to Tommee Tippee's site. `_rebuild_sources` cannot see any of it — it maps each
    # marker to blocks[old-1] and rewrites the markers AND the Sources list from the same map, so the
    # text and the list can never disagree. The model simply attached the wrong block, and nothing
    # checked that a sentence naming brand X cites a source belonging to X.
    _SPEC_CLAIM_RE = re.compile(
        r"clinically\s+proven|\bBPA[\s-]?free\b|\bphthalate[\s-]?free\b|heat[\s-]?resist|"
        r"thermal\s+shock|shatter(?:proof|[\s-]?resistant)|\bsterilis|\bsteriliz|"
        r"\d{2,3}\s*°?\s*[CF]\b|\bcertified\b|\bcertification\b|\bFDA\b|\bLFGB\b|\bEN\s?14350\b|"
        r"\bdishwasher[\s-]?safe\b|\bmicrowave[\s-]?safe\b|\bmedical[\s-]?grade\b|\btested\s+to\b",
        re.IGNORECASE)
    _BARE_DOMAIN_RE = re.compile(
        r"\b([a-z0-9][a-z0-9-]{1,40}\.(?:com|co\.uk|net|org|io|ai|shop|store))\b", re.IGNORECASE)

    @staticmethod
    def _prose_sentences(body):
        """Sentences from PROSE only — table rows, headings and the Sources list carry citations that
        are structural, not claims."""
        out = []
        in_src = False
        for ln in (body or "").split("\n"):
            st = ln.strip()
            if _VF_SOURCES_RE.match(ln):
                in_src = True
            elif _VF_HEAD_RE.match(ln):
                in_src = False
            if in_src or not st or st.startswith("|") or st.startswith("#") or st.startswith(">"):
                continue
            for sent in re.split(r"(?<=[.!?])\s+", st):
                if sent.strip():
                    out.append(sent.strip())
        return out

    @staticmethod
    def _blocks_from_sources(body, fallback):
        """FU204 — the `[S#]` → source map as the READER sees it. Both checks run AFTER
        `_rebuild_sources`, which renumbers every marker by first appearance, so indexing into the
        un-renumbered `self._evidence_blocks` names the WRONG source (caught by an end-to-end smoke:
        a sentence citing Tommee Tippee was reported as citing amazon.com). The rebuilt `## Sources`
        list is authoritative and self-consistent with the body; fall back to the raw blocks only when
        a body has no Sources section (a mid-pipeline call)."""
        sec = re.split(r"(?im)^[ \t]*#{2,3}[ \t]+Sources\b", body or "", maxsplit=1)
        if len(sec) < 2:
            return list(fallback or [])
        found = {}
        for m in re.finditer(r"^\s*[-*]\s*\[S(\d+)\]\s*(.*?)\s*(?:—|--)\s*<?(\S+?)>?\s*$",
                             sec[1], re.M):
            found[int(m.group(1))] = {"label": m.group(2), "url": m.group(3), "text": ""}
        if not found:
            return list(fallback or [])
        return [found.get(i + 1, {"label": "", "url": "", "text": ""}) for i in range(max(found))]

    def _citation_attribution_check(self, body, blocks, brand, tools):
        """FU204 Change 2 — a sentence that names exactly ONE brand (or one bare domain) but whose
        cited blocks all belong to somebody else. Warning only: a false positive costs one line of
        toast, and rewriting a citation automatically could silently relabel a real source."""
        blocks = self._blocks_from_sources(body, blocks)
        if not body or not blocks:
            return ""
        subj = ((brand or {}).get("name") or "").strip()
        names = [n for n in ([subj] + [str(t).strip() for t in (tools or [])]) if n]
        if len(names) < 2:
            return ""
        # brand -> the domain(s) we resolved for it
        doms = {}
        if subj and (brand or {}).get("domain_url"):
            doms[subj.lower()] = _norm_domain(brand.get("domain_url") or "")
        _cd = (brand or {}).get("competitor_domains")
        if isinstance(_cd, str):
            try:
                _cd = json.loads(_cd or "{}")
            except Exception:
                _cd = {}
        for k, v in (_cd or {}).items():
            doms[str(k).strip().lower()] = _norm_domain(str(v or ""))

        def _belongs(blk, nm):
            """Does this evidence block belong to / explicitly name brand `nm`?"""
            toks = [x for x in _product_tokens(nm) if len(x) >= 3]
            if not toks:
                return True                       # can't tell → never flag
            d = _norm_domain(blk.get("url") or "")
            bd = doms.get(nm.lower()) or ""
            if bd and d and (d == bd or d.endswith("." + bd)):
                return True
            hay = ((blk.get("label") or "") + " " + (blk.get("url") or "") + " "
                   + (blk.get("text") or "")).lower()
            return all(t in hay for t in toks)

        hits = []
        for sent in self._prose_sentences(body):
            idxs = [int(x) for x in re.findall(r"\[S(\d+)\]", sent)]
            if not idxs:
                continue
            low = sent.lower()
            named = {n for n in names if re.search(r"\b" + re.escape(n.lower()) + r"\b", low)}
            # A brand and its OWN domain are one entity, not two — "Tommee Tippee … on
            # tommeetippee.com [S8]" must still be checkable (that sentence cited a paediatric
            # guideline). Only an UNRELATED bare domain counts as a separate entity.
            _ds = set()
            for d in self._BARE_DOMAIN_RE.findall(sent):
                _dl = d.lower().replace("-", "")
                if any(all(t in _dl for t in _product_tokens(n)) for n in named if _product_tokens(n)):
                    continue
                _ds.add(d)
            named |= _ds
            if len(named) != 1:
                continue                          # 0 or 2+ brands → ambiguous, never flag
            nm = next(iter(named))
            cited = [blocks[i - 1] for i in idxs if 1 <= i <= len(blocks)]
            if cited and not any(_belongs(b, nm) for b in cited):
                where = ", ".join(sorted({_norm_domain(b.get("url") or "") or (b.get("label") or "?")
                                          for b in cited}))
                hits.append(f'"{sent[:60].strip()}…" names {nm} but cites {where}')
            if len(hits) >= 4:
                break
        if not hits:
            return ""
        return "citation-check: " + "; ".join(hits) + " — re-cite to that brand's own source or drop the specific"

    def _source_class_check(self, body, blocks):
        """FU204 Change 3 — a material / safety / certification / efficacy claim whose ONLY citations
        are `review ·` (star ratings) or `retail ·` (marketplace listings). Those evidence sentiment
        and price, never what a product is made of or what it does."""
        blocks = self._blocks_from_sources(body, blocks)
        if not body or not blocks:
            return ""
        hits = []
        for sent in self._prose_sentences(body):
            if not self._SPEC_CLAIM_RE.search(sent):
                continue
            idxs = [int(x) for x in re.findall(r"\[S(\d+)\]", sent)]
            cited = [blocks[i - 1] for i in idxs if 1 <= i <= len(blocks)]
            if not cited:
                continue
            klass = {_source_class(b.get("url") or "") for b in cited}
            if klass and klass <= {"review", "retail"}:
                hits.append(f'"{sent[:60].strip()}…" rests only on a {"/".join(sorted(klass))} source')
            if len(hits) >= 4:
                break
        if not hits:
            return ""
        return ("source-class: " + "; ".join(hits)
                + " — a rating or marketplace page evidences sentiment or price, not a spec")

    def _self_reference_note(self, body, brand):
        """FU197 — flag a sentence that promises another page of this brand without linking a LIVE
        one. Returns a note for `geo_warning`, or "" when the body is clean. Never mutates."""
        name = ((brand or {}).get("name") or "").strip()
        if not body or not name:
            return ""
        live = {u for u in (getattr(self, "_sibling_urls", None) or set())}
        bad = []
        for sent in re.split(r"(?<=[.!?])\s+", body):
            if not self._SELF_REF_RE.search(sent):
                continue
            # only our OWN pages dangle — a third party's guide is someone else's problem
            if name.lower() not in sent.lower() and not re.search(r"\bour\b", sent, re.I):
                continue
            linked = any((u.split("?")[0].rstrip("/").lower() in live)
                         for u in re.findall(r"\]\((https?://[^)\s]+)\)", sent))
            if not linked:
                bad.append(" ".join(sent.split())[:90])
        if not bad:
            return ""
        return (f"self-reference: {len(bad)} sentence(s) promise a {name} page that is not a published "
                f"link — remove them or publish and link the page (e.g. \u201c{bad[0]}\u2026\u201d)")

    # ───────────────────────── FU205 (R2): the warning channel, made legible ─────────────────────
    _WARN_KEY_RE = re.compile(r"^[a-z][a-z0-9 -]{0,28}$")

    @classmethod
    def _warn(cls, article, note, fold_string=True, key=None):
        """Record ONE warning as a STRUCTURED item, and on the legacy joined string.

        The audit's second structural finding: 22 deterministic checks fed a single `"; "`-joined
        string, and `_quality_report` then `.split(";")` it back apart to count and score. But four
        of those checks (`citation-check`, `source-class`, `mechanics-check`, `verify-check`) join
        their OWN sub-hits with `"; "` — so three real problems were counted as five, docked -25
        instead of -15, and the operator was shown a context-free fragment ("add competitors in Edit
        Brand or regenerate") as though it were a warning of its own.

        `article["warnings"]` is now the truth: exactly one entry per check that fired, carrying its
        own key. `article["geo_warning"]` stays exactly as it was so every existing reader — the
        toast, the task result, the persisted scorecard — keeps working unchanged.
        """
        note = (note or "").strip()
        if not note:
            return
        if not key:
            key = "check"
            if ":" in note:
                head = note.split(":", 1)[0].strip()
                if cls._WARN_KEY_RE.match(head):
                    key = head
        article.setdefault("warnings", []).append({"check": key, "detail": note})
        if fold_string:   # `fold_string=False` records a warning that already has its OWN toast
            article["geo_warning"] = "; ".join(
                x for x in [article.get("geo_warning", ""), note] if x)

    def _budget_note(self):
        """FU205 (R6) — the starvation signal, as a note or "" when the run was healthy.

        Shared by BOTH exits of a generation, and the second one is the one that matters: a starved
        run is the MOST likely to hit the FU79 pause (every competitor it never got to reaches the
        modal as unsourced), and the pause returns BEFORE `_finalize_article` — so the check that
        lives there would never fire on the exact case it was written for. The operator would be
        asked to paste links for four competitors with nothing saying the tool simply stopped
        looking. That is the misdiagnosis this retires, so the note has to reach the modal too."""
        try:
            n = int(getattr(self.claude, "skipped_searches", lambda: 0)() or 0)
        except Exception:
            n = 0
        if not n:
            # a RESUMED generation re-runs no searches, so its own count is zero — fall back to the
            # note the paused run recorded in the checkpoint.
            return getattr(self, "_budget_warn", "") or ""
        return (f"budget-check: the ${_BLOG_COST_CEILING:.2f} web-search ceiling was reached — "
                f"{n} search(es) were SKIPPED, so thin sourcing here means the tool stopped "
                f"looking, not that nothing exists; raise BLOG_COST_CEILING or regenerate with "
                f"fewer competitors")

    # ───────────────── FU205 (R7): the two properties nothing verified ─────────────────
    # A stall is the failure mode the answer-first rule exists to prevent: heading + first sentence
    # are lifted as ONE chunk, and a chunk that opens "The honest answer is: it depends" carries no
    # entities and is useless as a citation. The rule has been prompt-only for the blog body since
    # FU85 — deterministic ONLY for LinkedIn and YouTube — so the one surface it matters most on was
    # the one surface nothing checked.
    _STALL_RE = re.compile(
        r"^\s*(?:the\s+)?(?:honest\s+)?answer\s+is[:,]?\s*it\s+depends"
        r"|^\s*it\s+depends\b"
        r"|^\s*let(?:'|\u2019)?s\s+(?:go\s+through|dive|break|take\s+a\s+look|explore)"
        r"|^\s*let\s+us\s+(?:go\s+through|dive|break|take\s+a\s+look|explore)"
        r"|^\s*let\s+me\s+break\s+(?:this\s+)?down"
        r"|^\s*(?:there\s+is|there(?:'|\u2019)s)\s+no\s+(?:one|single)\s+(?:right\s+)?answer"
        r"|^\s*(?:that|this)\s+depends\s+on\b"
        r"|^\s*before\s+(?:we|you)\s+(?:dive|get|begin|start)"
        r"|^\s*first[,]?\s+(?:some|a\s+little)\s+(?:context|background)",
        re.IGNORECASE)

    @classmethod
    def _answer_first_check(cls, body):
        """Every question-shaped heading must be ANSWERED in its own first sentence. Returns a note
        for `geo_warning`, or "" when every question heading answers itself. Never mutates."""
        lines = (body or "").split("\n")
        bad = []
        for i, ln in enumerate(lines):
            m = re.match(r"^\s*#{2,3}\s+(.*\?)\s*$", ln)
            if not m:
                continue
            first = ""
            for nxt in lines[i + 1:]:
                t = nxt.strip()
                if not t:
                    continue
                if t.startswith("#") or t.startswith("|") or t.startswith(">"):
                    break   # another heading / a table / a quote — no prose answer to judge
                first = re.split(r"(?<=[.!?])\s", t, maxsplit=1)[0]
                break
            if first and cls._STALL_RE.search(first):
                bad.append(m.group(1).strip()[:60])
        if not bad:
            return ""
        return ("answer-first: " + str(len(bad)) + " question heading(s) open with a stall instead of "
                "the answer (" + "; ".join(f'"{b}"' for b in bad[:3]) + ") — engines lift the heading "
                "and its first sentence as ONE chunk, so a stall there is an uncitable chunk")

    @staticmethod
    def _byline_present_check(body):
        """FU152 prepends a byline placeholder to EVERY article and FU204 stopped it being corrupted
        into a bullet — but nothing ever checked it SURVIVED the two body-rewriting LLM calls. The
        placeholder is what forces a human pass before publishing; losing it silently removes that
        gate. Returns a note, or "" when the byline is there."""
        head = "\n".join((body or "").split("\n")[:12])
        if "[Add author byline before publishing]" in head:
            return ""
        for ln in head.split("\n"):
            t = ln.strip()
            if t.startswith("*") and t.endswith("*") and len(t) > 2 and not t.startswith("**"):
                return ""   # a real byline / reviewer / disclosure italic line
        return ("byline-check: the author byline placeholder is missing from the top of the article — "
                "a rewrite dropped it; regenerate, or add the byline before publishing")

    # FU205 (R4) — the check notes that survive a FU79 pause. Sourcing does not re-run on resume,
    # so without this every check that depends on it silently reports clean on a resumed blog.
    _CHECK_NOTES = ("_peer_note", "_auth_note", "_facts_note", "_price_warn", "_invented_note",
                    "_table_punt_note", "_core_mechanics", "_subject_phrase", "_subject_peers",
                    "_budget_warn")

    def _check_notes(self):
        """JSON-safe snapshot of the deterministic checks' state, for the pause checkpoint."""
        out = {k: getattr(self, k, None) for k in self._CHECK_NOTES}
        out["_sibling_urls"] = sorted(getattr(self, "_sibling_urls", None) or set())
        return {k: v for k, v in out.items() if v}

    def _restore_check_notes(self, notes):
        """Put a `_check_notes()` snapshot back on a fresh instance (the FU79 resume)."""
        for k, v in (notes or {}).items():
            if k == "_sibling_urls":
                self._sibling_urls = set(v or [])
            elif k in self._CHECK_NOTES:
                setattr(self, k, v)

    def _finalize_article(self, brand, seed, article, draft_body, geo="", qualifier="",
                          ymyl=None, link_targets=None, with_linkedin=True):
        """FU79 — the shared TAIL of generate_blog / finish_pending_blog: substance guard → deterministic
        ## Sources rebuild → LinkedIn adaptation → prompt version + real dollar cost. FU90: also runs the
        geo-check — a WARNING (never a block) when a geo page barely mentions its geography. FU151 (D):
        also computes the deterministic quality scorecard. Mutates + returns `article`.

        FU205 (R1): `with_linkedin=False` runs the entire guard set and every check but SKIPS the
        LinkedIn adaptation. That is what lets `regenerate part=article` / `part=verify` — which
        hand-rolled a partial subset and so silently lost the sections their own rewrites dropped —
        reach this one composition without also regenerating a surface the operator did not ask for
        (`part=linkedin` exists for that) and without paying for the extra call."""
        # FU54 substance guard: restore any whole section the verify/reconcile rewrite dropped (source-first
        # — the official primary source is force-kept regardless), and log any concrete stat that went missing.
        article["body_markdown"] = self._restore_dropped_sections(draft_body, article.get("body_markdown") or "")
        # FU88: re-pin the H1 to the seed AFTER the verify/reconcile rewrites (they preserve structure
        # but could still reword the heading) — the visible H1 must stay the exact target prompt.
        article["body_markdown"] = self._force_h1(article["body_markdown"], seed)
        # FU153: OPTIONAL self-hosted open-model FINAL writing pass (watermark strip). Gated —
        # runs ONLY when a WriterClient is injected AND writer_mode is rewrite/compose; otherwise
        # a no-op and everything below is byte-identical to today. Placed AFTER
        # _restore_dropped_sections + _force_h1 so _restore_dropped_sections can't re-inject
        # Claude's watermarked sections under the rewrite; the deterministic guards below
        # (_rebuild_sources, warnings, _quality_report) then re-run on the rewritten body. Re-pin
        # the H1 after, since the open model may have reworded it.
        if self.writer is not None and self.writer_mode in ("rewrite", "compose"):
            article["body_markdown"] = self._apply_writer_pass(article, draft_body, brand, seed)
            article["body_markdown"] = self._force_h1(article["body_markdown"], seed)
        missing_stats = self._dropped_stats(draft_body, article["body_markdown"])
        if missing_stats:
            print(f"[blog_gen] substance-guard: WARNING dropped stat(s) {missing_stats}", flush=True)
        # FU185 — strip the obvious AI SYMBOLS (em/en-dashes, arrows, decorative bullets, curly quotes,
        # the one-char ellipsis) from the FINISHED body. Placed HERE so it covers whichever body is live
        # — Claude's, or Qwen's when the writer pass ran above — and BEFORE `_rebuild_sources`, so the
        # " — <url>" separators and the "—" punt-placeholder cells that `_rebuild_sources` writes
        # AFTERWARDS are never touched. Mechanical: generation itself is completely unchanged.
        article["body_markdown"], _n_sym = self._scrub_ai_symbols(article["body_markdown"])
        for _mf in ("meta_description", "meta_title"):
            if (article.get(_mf) or "").strip():
                article[_mf] = self._sa(article[_mf])   # a meta field is published text too
        if any(_n_sym.values()):
            article["ai_symbols_removed"] = _n_sym
        # Deterministic ## Sources: contiguous [S#] + correct URLs for every cited source.
        article["body_markdown"] = self._rebuild_sources(article["body_markdown"])
        # FU205 (R3): `_resolve_table_punts` RESETS `self._table_punt_note` on every call, and the
        # verification pass below re-runs `_rebuild_sources` after a prose repair. Capture the note
        # from THIS rebuild so a column dropped here is still reported even when the second rebuild
        # finds nothing left to drop and clears it.
        _tpn_first = getattr(self, "_table_punt_note", "")
        # FU167 (Change 6): strip invisible/zero-width/bidi carrier chars from EVERY final body (belt-and-
        # suspenders — the invisible-CHARACTER watermark class + stray chars from any source). NOT Claude's
        # statistical mark (that's word choice), and it never alters visible text.
        article["body_markdown"], _n_inv = self._strip_invisible_chars(article["body_markdown"])
        if _n_inv:
            article["writer_invisible_removed"] = _n_inv
            print(f"[blog_gen] invisible-char sanitizer: removed {_n_inv} zero-width/bidi char(s)", flush=True)
        # FU201/FU202 — FINAL verification: repair formatting + the mechanically safe content
        # defects, flag the rest, keep BOTH versions.
        #
        # FU205 (R3): this used to run AFTER the ~20 read-only checks below, which meant every one of
        # them judged a body this pass was about to change. Concretely: after a prose repair the pass
        # re-runs `_rebuild_sources`, which re-runs `_resolve_table_punts` — so a table could be
        # narrowed, or removed by the FU204 collapse guard, AFTER the competitor-count check had
        # already passed and `_table_punt_note`'s only reader had already run. The note was rewritten
        # and nobody read it again; the Sources-derived checks (FU133/FU141) validated a `## Sources`
        # block that was then rebuilt. Its own slot requirement — "after `_rebuild_sources`, because
        # `_resolve_table_punts` runs inside it and drops columns" — is still met here: the rebuild
        # happens well above. So the checks now read the body that actually ships.
        _vrep = self._verify_final_article(brand, article)
        if _vrep:
            article["verify_report"] = _vrep
        # FU90 — geo-check (soft signal, never blocks): a geo page whose BODY barely mentions its
        # geography is the doorway pattern; warn the operator immediately instead of at review.
        rgeo = (geo or "").strip() or _seed_geo(seed)
        if rgeo:
            _geo_key = re.sub(r"^the\s+", "", rgeo, flags=re.I)
            _body_no_heads = "\n".join(l for l in (article["body_markdown"] or "").splitlines()
                                       if not l.lstrip().startswith("#"))
            _hits = len(re.findall(r"\b" + re.escape(_geo_key) + r"\b", _body_no_heads, re.I))
            if _hits < 3:
                note = (f"geo-check: this page targets '{rgeo}' but the body mentions it only "
                        f"{_hits}× — possible doorway output; review the geo sections")
                print(f"[blog_gen] {note}", flush=True)
                self._warn(article, note)
        # FU93 — qualifier-check (soft signal, same channel as the geo-check so the app/UI wiring is
        # untouched): a variant page that barely mentions its own qualifier is the doorway pattern.
        # (Substance-share isn't reliably measurable in code — this only catches the near-zero case;
        # the real enforcement is the prompt + sourcing + reconcile rules.)
        rqual = (qualifier or "").strip() or _seed_qualifier(seed)
        if rqual:
            _body_no_heads = "\n".join(l for l in (article["body_markdown"] or "").splitlines()
                                       if not l.lstrip().startswith("#"))
            _qhits = len(re.findall(r"\b" + re.escape(rqual) + r"\b", _body_no_heads, re.I))
            _qtail = rqual.split()[-1]
            if _qtail.lower() != rqual.lower():
                _qhits = max(_qhits, len(re.findall(r"\b" + re.escape(_qtail) + r"\b",
                                                    _body_no_heads, re.I)))
            if _qhits < 3:
                qnote = (f"qualifier-check: this page targets '{rqual}' but the body mentions it only "
                         f"{_qhits}× — possible doorway output; review the qualifier sections")
                print(f"[blog_gen] {qnote}", flush=True)
                self._warn(article, qnote)
        # FU93 (P3) — claim-check (warning only, deliberately narrow): an UNQUALIFIED "no sales tax"
        # in the meta description or the page opening is very likely wrong post-Wayfair (nexus states)
        # and creates checkout disputes. Never rewrites — the operator confirms the policy + regens.
        _claim_zone = (article.get("meta_description") or "") + "\n" + \
                      (article.get("body_markdown") or "")[:800]
        if re.search(r"\bno sales tax\b(?![^.;\n]{0,50}(?:qualif|most|some|where|eligible|select|"
                     r"certain))|\btax[- ]free\b(?![^.;\n]{0,50}(?:qualif|most|some|where|eligible|"
                     r"select|certain))", _claim_zone, re.I):
            cnote = ("claim-check: unqualified 'no sales tax' in the meta/opening — confirm the "
                     "client's actual tax policy and qualify before publishing")
            print(f"[blog_gen] {cnote}", flush=True)
            self._warn(article, cnote)
        _pn = getattr(self, "_peer_note", "")
        if _pn:   # FU98: surfaced on the same toast channel as the geo/qualifier/claim checks
            self._warn(article, _pn)
        _in184 = getattr(self, "_invented_note", "")
        if _in184:  # FU184: a compared competitor the model named itself (not curated, not evidenced)
            self._warn(article, _in184)
        # FU138: unsourced-table resolution outcome. FU205 (R3): the union of BOTH rebuilds — the
        # one above and the one the verification pass runs after a prose repair — so a drop can no
        # longer be silently overwritten by a later, quieter pass.
        _tpn_notes = [n for n in dict.fromkeys(
            [_tpn_first, getattr(self, "_table_punt_note", "")]) if n]
        for _tpn in _tpn_notes:
            self._warn(article, _tpn)
        _an142 = getattr(self, "_auth_note", "")
        if _an142:  # FU142: a NON-primary product with no official source naming it
            self._warn(article, _an142)
        # FU197 — a promise of another {name} page that carries no live link. The writer used to be
        # handed UNPUBLISHED sibling titles and told to defer to them, so it advertised guides that
        # exist nowhere. The prompt rules are the fix; this is the visible backstop. Warning only —
        # the phrasings vary far too much to cut a sentence safely.
        # FU204 — the two citation checks. Warning only; they never rewrite a marker, because a
        # deterministic re-cite would silently relabel a real source. Between them they catch the
        # wrong-ENTITY case (a Nanobebe price cited to Tommee Tippee) and the wrong-SOURCE-CLASS case
        # (borosilicate glass cited to a Trustpilot rating). A same-brand WRONG-PRODUCT page (a PPSU
        # claim cited to that brand's GLASS page) is caught by neither — recorded, not claimed.
        _cab = self._citation_attribution_check(article.get("body_markdown") or "",
                                                getattr(self, "_evidence_blocks", None) or [],
                                                brand, getattr(self, "_article_tools", None) or [])
        if _cab:
            print(f"[blog_gen] {_cab}", flush=True)
            self._warn(article, _cab)
        _scc = self._source_class_check(article.get("body_markdown") or "",
                                        getattr(self, "_evidence_blocks", None) or [])
        if _scc:
            print(f"[blog_gen] {_scc}", flush=True)
            self._warn(article, _scc)
        _srn = self._self_reference_note(article.get("body_markdown") or "", brand)
        if _srn:
            print(f"[blog_gen] {_srn}", flush=True)
            self._warn(article, _srn)
        # FU198 — mechanics coverage. The extracted mechanics are derived from the SUBJECT, not from
        # the draft, so an absent one means the page really did skip it. Warning only.
        _mech = [m for m in (getattr(self, "_core_mechanics", None) or []) if str(m).strip()]
        if _mech:
            _blow = (article.get("body_markdown") or "").lower()
            _missing = []
            for _m in _mech:
                _mt = [t for t in _product_tokens(_m) if len(t) >= 4]
                # covered when MOST of the mechanic's distinctive words appear somewhere in the body
                if _mt and sum(1 for t in _mt if t in _blow) < max(1, (len(_mt) + 1) // 2):
                    _missing.append(str(_m).strip())
            if _missing and len(_missing) > len(_mech) / 2:
                _mnote = ("mechanics-check: the page is credential-led — it does not cover "
                          + "; ".join(_missing[:3])
                          + (f" (+{len(_missing) - 3} more)" if len(_missing) > 3 else ""))
                print(f"[blog_gen] {_mnote}", flush=True)
                self._warn(article, _mnote)
        _pw = getattr(self, "_price_warn", "")
        if _pw:  # FU161: the subject's price for a product couldn't be confirmed from its own site
            self._warn(article, _pw)
        _fn = getattr(self, "_facts_note", "")
        if _fn:  # FU178: canonical brand facts that earned no citation (stated as positioning instead)
            self._warn(article, _fn)
        # FU135 — source-authority check (all blogs): "independent audit/analysis" framing beside a
        # third-party (review/affiliate) citation is authority laundering — warn, never rewrite.
        _bf = article.get("body_markdown") or ""
        _lm = re.search(r"independent(?:ly)?\s+(?:pricing\s+)?(?:audit|review|analysis|report|verified)",
                        _bf, re.I)
        if _lm:
            _seg = _bf[max(0, _lm.start() - 200):_lm.end() + 200]
            _cited = re.findall(r"\[S(\d+)\]", _seg)
            _tp = False
            for _n in _cited:
                for _srcline in re.finditer(r"- \[S" + _n + r"\] (third-party ·)", _bf):
                    _tp = True
            if _tp or not _cited:
                _lnote = ("source-authority check: a review/affiliate source may be framed as an "
                          "'independent audit/analysis' — attribute it plainly or re-cite the "
                          "vendor's own page")
                print(f"[blog_gen] {_lnote}", flush=True)
                self._warn(article, _lnote)
        # FU133 — YMYL checks (same toast channel): (a) the body must actually CITE ≥2 official
        # sources; (b) a clinical page without a named human reviewer is skippable to AI engines —
        # loud warning, never an invented person.
        if ymyl:
            _body_final = article.get("body_markdown") or ""
            _sat = _body_final.find("\n## Sources")
            _prose = _body_final[:_sat] if _sat > 0 else _body_final
            _off_cited = 0
            for _m in re.finditer(r"- \[S(\d+)\] official ·", _body_final):
                if f"[S{_m.group(1)}]" in _prose:
                    _off_cited += 1
            if _off_cited < 2:
                _ynote = (f"YMYL ({ymyl}): only {_off_cited} official citation(s) in the body — "
                          f"clinical claims lack authoritative grounding")
                print(f"[blog_gen] {_ynote}", flush=True)
                self._warn(article, _ynote)
            if not ((brand or {}).get("reviewer_name") or "").strip():
                _rnote = (f"YMYL ({ymyl}): no named medical reviewer — set a REAL reviewer in "
                          f"Edit Brand (never invented) so the page carries a professional byline")
                print(f"[blog_gen] {_rnote}", flush=True)
                self._warn(article, _rnote)
            if ymyl == "medical":
                # FU135: perks in the extract zone (quick answer / meta) = gray-market signal.
                _zone = ((article.get("meta_description") or "") + "\n" + _prose[:900])
                if re.search(r"discreet|free\s+(?:shipping|delivery)", _zone, re.I):
                    _pnote = ("YMYL (medical): logistics perk language ('free/discreet delivery') "
                              "sits in the Quick answer/meta — move it to a logistics section")
                    print(f"[blog_gen] {_pnote}", flush=True)
                    self._warn(article, _pnote)
        # FU141 — quick-answer citation guard (YMYL, warning only): the most-extracted position
        # keeps getting the worst citations. Every [S#] the Quick answer cites must map to an
        # "official ·" or first-party source — a review/affiliate citation there means the page
        # answers a clinical question on affiliate authority.
        if ymyl:
            _bqa = article.get("body_markdown") or ""
            _labels141 = {m.group(1): m.group(2).strip()
                          for m in re.finditer(r"- \[S(\d+)\] ([^\u2014\n]+)", _bqa)}
            _qm = re.search(r"(^|\n)[^\n]*quick answer[^\n]*(\n(?!\n)[^\n]*)*", _bqa, re.I)
            _qzone = _qm.group(0) if _qm else _bqa[:600]
            _badqa = sorted({f"S{n}" for n in re.findall(r"\[S(\d+)\]", _qzone)
                             if (_labels141.get(n) or "").lower().startswith(("review ·",
                                                                              "third-party ·"))})
            if _badqa:
                _qanote = (f"YMYL quick-answer check: the Quick answer cites {', '.join(_badqa)} "
                           f"(review/affiliate) for its lead claim — re-cite to an "
                           f"official/first-party source or regenerate")
                print(f"[blog_gen] {_qanote}", flush=True)
                self._warn(article, _qanote)
        # FU141 — sources integrity (all blogs, warning only): every listed ## Sources entry must
        # carry a URL — a citation pointing at nothing (the nameless/URL-less "provided source"
        # class) must never ship silently.
        _bsrc = article.get("body_markdown") or ""
        _si141 = _bsrc.find("\n## Sources")
        if _si141 > 0:
            _broken = []
            for _ln in _bsrc[_si141:].splitlines():
                _mm = re.match(r"- \[S(\d+)\]", _ln.strip())
                if _mm and "http" not in _ln:
                    _broken.append(f"S{_mm.group(1)}")
            if _broken:
                _snote = (f"sources-integrity: {', '.join(_broken)} in ## Sources "
                          f"carr{'ies' if len(_broken) == 1 else 'y'} no URL — fix the source "
                          f"entry or regenerate")
                print(f"[blog_gen] {_snote}", flush=True)
                self._warn(article, _snote)
        # FU140 — stale-year bump: the model dates titles with its training-era year. Any
        # 20XX strictly before the current year in the SEO title/description is bumped to the
        # current year — unless that year appears in the SEED (an operator-chosen retrospective)
        # — silent auto-fix, logged.
        import datetime as _dt140
        _cy = _dt140.datetime.utcnow().year
        def _bump_years(txt):
            def _r(m):
                y = int(m.group(0))
                if 2015 <= y < _cy and m.group(0) not in (seed or ""):
                    return str(_cy)
                return m.group(0)
            return re.sub(r"\b20\d\d\b", _r, txt or "")
        for _k in ("meta_title", "meta_description"):
            _oldv = article.get(_k) or ""
            _newv = _bump_years(_oldv)
            if _newv != _oldv:
                article[_k] = _newv
                print(f"[blog_gen] year-bump: {_k} → '{_newv}'", flush=True)
        article["ymyl"] = ymyl or ""
        # FU105 — competitor-count check (warning only): the FINAL body's comparison table must carry
        # ≥3 non-{name} competitor rows. Reads the final body directly, so it catches reconcile
        # row-drops (the FU98 check reads the pre-reconcile draft) and fires on the FU79 resume path
        # too. No table → silent (a non-comparison blog never warns).
        _tbl_lines, _bname = [], self._brand_block(brand)[0]
        for _ln in (article.get("body_markdown") or "").split("\n"):
            if _ln.lstrip().startswith("|"):
                _tbl_lines.append(_ln.strip())
            elif _tbl_lines:
                break   # first table only
        if _tbl_lines:
            _sep_re = re.compile(r"^\|[\s:|-]+\|?$")   # the |---|:---| separator row
            _rows = [l for l in _tbl_lines if not _sep_re.match(l)]
            _data = _rows[1:] if len(_rows) > 1 else []   # drop the header row
            _nm_re = re.compile(r"\b" + re.escape(_bname) + r"\b", re.I) if _bname else None
            _comp_rows = [r for r in _data if not (_nm_re and _nm_re.search(r))]
            if len(_comp_rows) < 3:
                _ccnote = (f"competitor-check: comparison table has only {len(_comp_rows)} competitor "
                           f"row(s) — minimum 3 expected; add competitors in Edit Brand or regenerate")
                print(f"[blog_gen] {_ccnote}", flush=True)
                self._warn(article, _ccnote)
            # FU142 — publisher-blank check: a SUBJECT-row cell that is empty/"—" while the SAME
            # column carries a real value in ≥1 competitor row is the inverted lopsided column —
            # the publisher looks like the only party not disclosing. Warn with the column name(s).
            _subj_rows = [r for r in _data if _nm_re and _nm_re.search(r)]
            if _subj_rows and _comp_rows:
                def _cells142(r):
                    return [re.sub(r"\*+", "", c).strip() for c in r.strip().strip("|").split("|")]
                _hdr = _cells142(_rows[0])
                _isblank = lambda v: v in ("", "—", "-", "–")
                _bad_cols = []
                for _ci in range(1, len(_hdr)):
                    _s_blank = any(_ci < len(_cells142(_sr)) and _isblank(_cells142(_sr)[_ci])
                                   for _sr in _subj_rows)
                    _c_filled = any(_ci < len(_cells142(_cr)) and not _isblank(_cells142(_cr)[_ci])
                                    for _cr in _comp_rows)
                    if _s_blank and _c_filled:
                        _bad_cols.append(_hdr[_ci] or f"column {_ci + 1}")
                if _bad_cols:
                    _pbnote = (f"publisher-blank: {_bname}'s "
                               + ", ".join(sorted(set(_bad_cols)))
                               + " cell(s) are empty while competitors show values — provide the "
                                 "fact or regenerate (a publisher-only blank reads evasive)")
                    print(f"[blog_gen] {_pbnote}", flush=True)
                    self._warn(article, _pbnote)
            # FU205 (R4) — canonical-price check. `_canonical_price_item` has claimed since FU158 to
            # make an operator-set price "the single source of truth for the SUBJECT's pricing cell",
            # and nothing ever called it. This is that guarantee, enforced where it can be enforced
            # without touching generation: when the operator has set a canonical price for THIS
            # article's product and the subject's own pricing cell shows a DIFFERENT figure, say so.
            # FU163's three-different-prices failure — meta $647, cell $29, Offer $79 — is exactly
            # the shape this catches, and the operator's number is the one that is right.
            try:
                _kf205 = json.loads((brand or {}).get("key_facts") or "{}")
            except Exception:
                _kf205 = {}
            _canon205 = _canonical_price_item(_kf205, f"{seed} {article.get('title') or ''}")
            if _canon205 and _canon205.get("operator_set") and _subj_rows:
                _cval = str(_canon205.get("value") or "")
                _cnum, _ = _price_amount(_cval)
                if _cnum:
                    _pcols = [i for i, h in enumerate(_hdr) if re.search(r"pric|cost|fee", h, re.I)]
                    for _ci in _pcols:
                        for _sr in _subj_rows:
                            _cells = _cells142(_sr)
                            if _ci >= len(_cells):
                                continue
                            _cell_num, _ = _price_amount(_cells[_ci])
                            if _cell_num and _cell_num != _cnum:
                                _cpn = (f"canonical-price: {_bname}'s {_hdr[_ci] or 'pricing'} cell "
                                        f"shows {_cells[_ci].strip()[:40]} but the operator-set "
                                        f"canonical price is {_cval[:60]} — the operator's value is "
                                        f"the authority; fix the cell or update Edit Brand")
                                print(f"[blog_gen] {_cpn}", flush=True)
                                self._warn(article, _cpn)
                                break
        # FU206 — a removal is from the ENTIRE blog, so verify it actually left. The reconcile is an
        # LLM and PRESERVE SUBSTANCE pulls the other way; "removed from the comparison" quietly
        # meaning "removed from the table only" is exactly the silent half-fix this round exists to
        # stop. Warning, never a rewrite: deleting sentences deterministically would gut the prose.
        _rmb = [x for x in (getattr(self, "_removed_brands", None) or []) if str(x).strip()]
        if _rmb:
            _still = [b for b in _rmb
                      if re.search(r"\b" + re.escape(b) + r"\b", article.get("body_markdown") or "", re.I)]
            if _still:
                _rmn = ("removed-brand: " + ", ".join(_still) + " still appear(s) in the article after "
                        "being removed — delete the remaining mention(s) before publishing, or "
                        "regenerate")
                print(f"[blog_gen] {_rmn}", flush=True)
                self._warn(article, _rmn)
            else:
                print(f"[blog_gen] remove-brand: verified — {', '.join(_rmb)} no longer appear(s) "
                      f"anywhere in the article", flush=True)
        _rmr = [x for x in (getattr(self, "_removed_refused", None) or []) if str(x).strip()]
        if _rmr:
            _rrn = ("removed-brand: " + ", ".join(_rmr) + " could NOT be removed — doing so would "
                    f"leave fewer than {_MIN_COMPARISON_BRANDS} competitors, and a comparison that "
                    "thin reads as self-crowning; it was kept and its unanswered dimensions dropped "
                    "instead")
            print(f"[blog_gen] {_rrn}", flush=True)
            self._warn(article, _rrn)
        # FU205 (R7) — the two properties nothing verified: the answer-first guarantee that earns the
        # citation, and that the byline the client must replace actually survived the rewrites.
        _afn = self._answer_first_check(article.get("body_markdown") or "")
        if _afn:
            print(f"[blog_gen] {_afn}", flush=True)
            self._warn(article, _afn)
        _byn = self._byline_present_check(article.get("body_markdown") or "")
        if _byn:
            print(f"[blog_gen] {_byn}", flush=True)
            self._warn(article, _byn)
        # FU205 (R6) — budget starvation, made visible. Past the $3 ceiling `search_sources`,
        # `fetch_site_facts` and `find_official_domain` silently return []/"" and NOTHING told the
        # operator. Every downstream symptom (thin sources, unsourced competitors, blank cells,
        # punts, the mass-pause) then looks like a logic bug, and has been debugged as one for ~15
        # rounds. One boolean retires a whole class of misdiagnosis.
        _bnote = self._budget_note()
        if _bnote:
            print(f"[blog_gen] {_bnote}", flush=True)
            self._warn(article, _bnote)
        if with_linkedin:   # FU205 (R1): off for the partial-regenerate paths — see the docstring
            article["linkedin_text"] = self.generate_linkedin(brand, seed, article, geo=geo)   # FU91
        article["prompt_version"] = PROMPT_VERSION
        # FU205 (R2): the pricing-conflict alert (FU150) was toast-only — never persisted, never folded
        # into the warning list, so `_quality_report` could not see it and it vanished on reload.
        # Recorded on the structured list; NOT folded into the joined string, because it already has
        # its own toast and would otherwise be shown to the operator twice.
        if (article.get("key_facts_warning") or "").strip():
            self._warn(article, article["key_facts_warning"].strip(),
                       fold_string=False, key="key-facts")
        # FU151 (D): deterministic quality scorecard (structure/meta/links + folded warnings), persisted.
        try:
            article["quality_report"] = self._quality_report(article, brand, link_targets=link_targets)
        except Exception as e:
            print(f"[blog_gen] quality-report skipped: {e}", flush=True)
            article["quality_report"] = {}
        # FU54: real dollar cost of this generation (tokens + web searches), surfaced in the UI.
        article["gen_cost"] = round(self.claude.usage_cost(), 4)
        article["gen_usage"] = dict(self.claude._usage)
        return article

    # ──────────────────────── FU201/FU202: final-output verification ────────────────────────
    # Three stages, deliberately different in what each is allowed to do:
    #   _verify_format_fix     — FORMATTING + SPACING. Mechanical, meaning-preserving, AUTO-APPLIED.
    #                            Free on every generation (no LLM). See `scrub_markdown_formatting`.
    #   _verify_consistency    — price / brand / citation / structure / link / duplicate.
    #                            FLAGGED ONLY, also free: each needs a FACT decision, not an edit.
    #   _verify_content        — FU202: ONE Claude call reading the WHOLE article. Auto-fixes the
    #                            mechanically safe classes (`_VF_FIXABLE`) through the six repair
    #                            gates, flags the rest, and returns an editorial assessment.
    # The auto-fix / flag split is decided in CODE, never by the `kind` the model happens to write.
    # Anything a repair touches is kept in BOTH versions (`article["body_pre_verify"]`).
    _VF_LIST_RE, _VF_RULE_RE, _VF_SOURCES_RE = _VF_LIST_RE, _VF_RULE_RE, _VF_SOURCES_RE

    def _verify_format_fix(self, body):
        """Instance delegate for `scrub_markdown_formatting` (module-level so the app's
        Drive-upload and export render paths can reuse it without a generator)."""
        return scrub_markdown_formatting(body)

    _VF_MONEY_RE = re.compile(r"(?:\$|€|£)\s?\d[\d,]*(?:\.\d+)?")

    def _verify_consistency(self, brand, article):
        """Deterministic CONSISTENCY checks over the finished body. FLAG ONLY — never edits, because every
        one of these needs a FACT decision, not a mechanical repair (the operator's "flag the rest").
        Free: no LLM, no network. Returns a list of {kind, problem, detail}."""
        body = article.get("body_markdown") or ""
        name = ((brand or {}).get("name") or "").strip()
        issues = []
        if not body.strip():
            return issues

        cut = self._VF_SOURCES_RE.search(body)
        prose = body[:cut.start()] if cut else body        # Sources is code-generated — never flag it

        # ── price collision (the FU163 failure: meta said $647, the table said $29, the schema $79) ──
        def _money(t):
            return {m.group(0).replace(" ", "") for m in self._VF_MONEY_RE.finditer(t or "")}
        surfaces = {"meta description": _money(article.get("meta_description") or "")}
        _qa = re.search(r"(?is)^#{1,4}[ \t]*(?:quick answer|short answer|tl;?dr)\b(.*?)(?=\n#{1,4}\s|\Z)",
                        prose, re.M)
        if _qa:
            surfaces["quick answer"] = _money(_qa.group(1))
        if name:
            for ln in prose.splitlines():
                if ln.startswith("|") and re.search(re.escape(name), ln, re.I):
                    surfaces["comparison table"] = _money(ln)
                    break
        seen = {k: v for k, v in surfaces.items() if v}
        allp = set().union(*seen.values()) if seen else set()
        if len(seen) > 1 and len(allp) > 1:
            issues.append({"kind": "price", "problem": "the same brand carries different prices on "
                           "different surfaces — one of them is wrong",
                           "detail": "; ".join(f"{k}: {', '.join(sorted(v))}" for k, v in seen.items())})
        # canonical price (Edit Brand → Canonical pricing) is the authority when set — FU158
        try:
            _canon = {str(i.get("value") or "") for i in _kf_pricing_items((brand or {}).get("key_facts"))
                      if i.get("value")}
        except Exception:
            _canon = set()
        _cm = set().union(*[_money(v) for v in _canon]) if _canon else set()
        if _cm and allp and not (allp & _cm):
            issues.append({"kind": "price", "problem": "no price in the article matches the canonical "
                           "price set in Edit Brand",
                           "detail": f"canonical: {', '.join(sorted(_cm))} · article: {', '.join(sorted(allp))}"})

        # ── brand casing (FU84: "Outsail" vs "OutSail") — flagged, never auto-corrected, because a
        #    quoted source title may legitimately spell it differently.
        if name and len(name) > 2:
            bad = {m.group(0) for m in re.finditer(re.escape(name), prose, re.I)
                   if m.group(0) != name and not re.match(r"https?://", prose[max(0, m.start() - 8):m.start()])}
            if bad:
                issues.append({"kind": "brand", "problem": f"the brand name is spelled inconsistently — "
                               f"it should be '{name}' everywhere",
                               "detail": "also found: " + ", ".join(sorted(bad))})

        # ── citations ──
        # FU205 (R4): this check was DEAD BY CONSTRUCTION. It compared each `[S#]` against
        # `len(self._evidence_blocks)`, but it runs AFTER `_rebuild_sources` has renumbered every
        # marker into 1..len(render) where len(render) <= len(blocks) — so the condition could never
        # be true and the check reported clean on every blog, forever. The question it was actually
        # trying to answer is answerable, and now matters more: does every marker in the prose have
        # a matching entry in the `## Sources` list the READER sees? That IS reachable on the paths
        # R1 opened up — a hand edit, an import, the manual verify endpoint — where the body carries
        # its own Sources list and no evidence map exists at all.
        _srcs = self._blocks_from_sources(body, getattr(self, "_evidence_blocks", None) or [])
        if _srcs:
            orphan = sorted({int(m.group(1)) for m in re.finditer(r"\[S(\d+)\]", prose)
                             if int(m.group(1)) > len(_srcs)
                             or not (_srcs[int(m.group(1)) - 1].get("url")
                                     or _srcs[int(m.group(1)) - 1].get("label"))})
            if orphan:
                issues.append({"kind": "citation", "problem": "a citation points at a source that is "
                               "not in the Sources list — the claim will lose its source",
                               "detail": ", ".join(f"[S{i}]" for i in orphan)})
        for ln in prose.splitlines():
            if re.match(r"^\s*#{1,6}\s", ln) and re.search(r"\[S\d+\]", ln):
                issues.append({"kind": "citation", "problem": "a citation marker is inside a heading",
                               "detail": ln.strip()[:90]})
                break

        # ── structure ──
        h1 = len(re.findall(r"(?m)^#\s+\S", prose))
        if h1 != 1:
            issues.append({"kind": "structure", "problem": f"the article has {h1} H1 headings (expected 1)",
                           "detail": ""})
        _seen_h2 = False
        for ln in prose.splitlines():
            if re.match(r"^##\s+\S", ln):
                _seen_h2 = True
            elif re.match(r"^###\s+\S", ln) and not _seen_h2:
                issues.append({"kind": "structure", "problem": "an H3 appears before any H2",
                               "detail": ln.strip()[:90]})
                break
        # An EMPTY SECTION — a heading immediately followed by another at the SAME or SHALLOWER level.
        # Going DEEPER (H1 → H2, H2 → H3) is ordinary nesting and must not be flagged: this pipeline
        # always emits the H1 seed straight into "## Quick answer".
        _prev_head, _prev_lvl, _empty = None, 0, []
        for ln in prose.splitlines():
            m = re.match(r"^(#{1,6})\s*(.*)$", ln)
            if m:
                lvl = len(m.group(1))
                if not m.group(2).strip():
                    _empty.append(ln.strip() or m.group(1))
                if _prev_head is not None and lvl <= _prev_lvl:
                    issues.append({"kind": "structure", "problem": "a section heading with no content "
                                   "under it", "detail": f"{_prev_head} → {ln.strip()[:60]}"})
                _prev_head, _prev_lvl = ln.strip()[:60], lvl
            elif ln.strip():
                _prev_head, _prev_lvl = None, 0
        if _empty:
            issues.append({"kind": "structure", "problem": "an empty heading", "detail": ", ".join(_empty[:3])})
        if prose.count("```") % 2:
            issues.append({"kind": "structure", "problem": "an unclosed code fence", "detail": ""})
        for m in re.finditer(r"\[\s*\]\([^)]*\)|\[[^\]]*\]\(\s*\)", prose):
            issues.append({"kind": "structure", "problem": "an empty markdown link",
                           "detail": m.group(0)[:60]})
            break

        # ── FU202 heading-level JUMP (H2 → H4) — the outline pane in the Google Doc renders it as a
        #    missing level, and an answer engine reads the hierarchy to decide what answers what.
        _lvl = 0
        for ln in prose.splitlines():
            m = re.match(r"^(#{1,6})\s+\S", ln)
            if m:
                n = len(m.group(1))
                if _lvl and n > _lvl + 1:
                    issues.append({"kind": "structure", "problem": f"a heading level is skipped "
                                   f"(H{_lvl} straight to H{n})", "detail": ln.strip()[:90]})
                    break
                _lvl = n

        # ── FU202 LINKS — the operator's "missing links". A promise of a page with nothing to click is
        #    the damaging one; a href that is not a URL/anchor/relative path silently 404s on the CMS.
        for m in re.finditer(r"\]\(\s*([^)\s]+)", prose):
            u = m.group(1)
            if not re.match(r"^(?:https?://|mailto:|#|/|\.{1,2}/)", u):
                issues.append({"kind": "link", "problem": "a link points at something that is not a URL "
                               "— it will break when the article is published",
                               "detail": u[:80]})
                break
        for _sent in re.split(r"(?<=[.!?])\s+", re.sub(r"(?m)^\s*\|.*$", "", prose)):
            if "](" in _sent or "<http" in _sent:
                continue
            if _VF_LINKPROMISE_RE.search(_sent):
                issues.append({"kind": "link", "problem": "the article points the reader at a page but "
                               "gives no link — either link it or drop the promise",
                               "detail": " ".join(_sent.split())[:110]})
                break

        # ── FU202 REPEATED CONTENT — the same sentence written twice reads as padding and costs the
        #    page its credibility. Table rows are excluded (a repeated cell is normal).
        _sent_seen, _dupe = {}, None
        _flat = re.sub(r"(?m)^\s*(?:\||#{1,6}\s).*$", "", prose)
        for _sent in re.split(r"(?<=[.!?])\s+", _flat):
            _k = " ".join(re.sub(r"\[S\d+\]", "", _sent).split()).strip().lower()
            if len(_k) < 45:
                continue
            if _k in _sent_seen:
                _dupe = " ".join(_sent.split())[:110]
                break
            _sent_seen[_k] = True
        if _dupe:
            issues.append({"kind": "duplicate", "problem": "the same sentence appears twice in the "
                           "article", "detail": _dupe})

        # ── ragged table row (a missing CELL needs a fact, so REPORT it, never reshape) ──
        rows, hdr = [], None
        for ln in prose.splitlines():
            if ln.strip().startswith("|"):
                n = ln.count("|")
                if hdr is None:
                    hdr = n
                elif not re.match(r"^\s*\|[\s:|-]+\|\s*$", ln) and n != hdr:
                    rows.append(ln.strip()[:70])
            else:
                hdr = None
        if rows:
            issues.append({"kind": "table", "problem": "a table row has a different number of columns "
                           "than its header — it will render broken",
                           "detail": rows[0]})
        return issues

    def _verify_final_article(self, brand, article):
        """FU201/FU202 — the AUTOMATIC verification layer. Runs on the FINISHED body at the very end of
        `_finalize_article`, in three stages:
          1. FORMATTING — mechanical, meaning-preserving, AUTO-APPLIED. Free, no LLM (see
             `scrub_markdown_formatting`; spacing, tables, list markers/indent, stray whitespace).
          2. CONSISTENCY — price / brand / citation / structure / link / duplicate. FLAGGED ONLY, free.
          3. CONTENT (FU202) — ONE Claude call that reads the WHOLE article, auto-fixes the mechanically
             safe classes (`_VF_FIXABLE`) through the same six repair gates, flags the rest, and returns
             an editorial assessment.

        Slot: AFTER `_rebuild_sources`, because `_resolve_table_punts` runs inside it and DROPS columns —
        a column-count check before that would judge a table about to change. After a PROSE repair the
        method re-runs `_rebuild_sources` itself (exactly as the manual endpoint does), because that one
        call re-applies the punt scrub, the table resolver, the edit-narration scrub and the contiguous
        [S#] renumber to whatever the repair changed.

        BOTH VERSIONS ARE KEPT: when anything was applied, the pre-verification body is stored on
        `article["body_pre_verify"]`, so nothing the pass changed is ever lost.

        Never raises. Returns the report and mutates article["body_markdown"]."""
        if os.environ.get("BLOG_VERIFY_FINAL", "1") == "0":
            return {}
        try:
            pre = article.get("body_markdown") or ""
            body = pre
            fixed, fixes = self._verify_format_fix(body)
            if fixed != body:
                body = fixed
                article["body_markdown"] = body
            issues = self._verify_consistency(brand, article)
            report = {"fixed": fixes, "issues": issues,
                      "n_fixed": len(fixes), "n_flagged": len(issues)}

            # ── FU202: the paid content read. Auto-fixes on EVERY generation (the operator's decision),
            #    still gated so it can be turned off without disabling the free half.
            applied, skipped, assessment, content = [], [], {}, []
            if self.claude is not None and os.environ.get("BLOG_VERIFY_SEMANTIC", "1") != "0":
                content, assessment = self._verify_content(brand, body)
                # The auto-fix / flag split is decided HERE, not by the kind the model wrote.
                fixable = [i for i in content if i.get("kind") in self._VF_FIXABLE and i.get("fix")]
                flagged = [i for i in content if i not in fixable]
                if fixable:
                    body, applied, skipped = self._verify_apply_repairs(body, fixable, brand,
                                                                        repair="claude")
                if applied:
                    # the repair rewrote prose — re-apply the downstream guards, then re-tidy formatting
                    body = self._rebuild_sources(self._sa(body))
                    body, _refix = self._verify_format_fix(body)
                    fixes = fixes + _refix
                    article["body_markdown"] = body
                for f in flagged:
                    issues.append({"kind": f.get("kind") or "content",
                                   "problem": f.get("problem") or "",
                                   "detail": (f.get("quote") or "")[:110]})
                report.update({"applied": applied, "skipped": skipped,
                               "n_applied": len(applied), "n_skipped": len(skipped)})
                if assessment:
                    report["assessment"] = assessment
                report["issues"], report["n_flagged"] = issues, len(issues)
                report["fixed"], report["n_fixed"] = fixes, len(fixes)

            # BOTH VERSIONS — only stored when the pass actually changed something.
            if (article.get("body_markdown") or "") != pre:
                article["body_pre_verify"] = pre

            if fixes:
                _k = {}
                for f in fixes:
                    _k[f["kind"]] = _k.get(f["kind"], 0) + 1
                print("[blog_gen] verify: fixed " + ", ".join(f"{v}× {k}" for k, v in sorted(_k.items())),
                      flush=True)
            if applied or skipped:
                print(f"[blog_gen] verify: content repairs {len(applied)} applied, {len(skipped)} refused"
                      + (f" · editorial {assessment.get('score')}" if assessment.get("score") is not None
                         else ""), flush=True)
            if issues:
                print(f"[blog_gen] verify: {len(issues)} issue(s) flagged — "
                      + "; ".join(sorted({i["kind"] for i in issues})), flush=True)
                # surface on the existing toast channel so it is seen at generation time, not at review
                _vn = ("verify-check: " + "; ".join(i["problem"] for i in issues[:3])
                       + (f" (+{len(issues) - 3} more)" if len(issues) > 3 else ""))
                self._warn(article, _vn)
            return report
        except Exception as e:
            print(f"[blog_gen] verify skipped: {e}", flush=True)
            return {"failed": str(e)}

    _VF_BANNED_CHARS = "\u2014\u2013\u2018\u2019\u201c\u201d\u2026"

    # FU202 — which content defects may be AUTO-FIXED and which are only ever FLAGGED. Enforced in CODE,
    # never by trusting the `kind` the model happens to write. A repair is mechanical only when it can be
    # made by rewriting ONE sentence with what is already on the page; a missing link, a duplicated
    # section, a thin section or a claim its source cannot carry needs a FACT or a restructure, so
    # "repairing" it means inventing something.
    _VF_FIXABLE = frozenset({"contradiction", "price", "brand", "typo", "readability"})
    _VF_FLAG_ONLY = frozenset({"link", "duplicate", "structure", "claim", "thin"})
    _VF_CHUNK = int(os.environ.get("BLOG_VERIFY_CHUNK", "25000"))

    def _verify_chunks(self, body):
        """Split the finished body for the content read so NOTHING is skipped (the FU201 prompt saw only
        the first 14,000 characters). Splits on `##` boundaries and accumulates up to `_VF_CHUNK`, so every
        chunk is a contiguous SUBSTRING of the body and a returned `quote` still matches verbatim."""
        text = body or ""
        if len(text) <= self._VF_CHUNK:
            return [text] if text.strip() else []
        parts = re.split(r"(?m)(?=^#{2,3}[ \t]+\S)", text)
        chunks, cur = [], ""
        for p in parts:
            if cur and len(cur) + len(p) > self._VF_CHUNK:
                chunks.append(cur)
                cur = p
            else:
                cur += p
        if cur.strip():
            chunks.append(cur)
        return chunks

    def _verify_content(self, brand, body):
        """FU202 — the CONTENT read. Goes over the ENTIRE finished article (chunked, so nothing past the
        old 14k cap is skipped) and returns `(issues, assessment)`:
          issues     — [{kind, quote, problem, fix}], `quote` a verbatim span, `fix` the correction.
          assessment — the editorial read {score, verdict, strengths, weaknesses}, REPORTED beside the
                       deterministic `_quality_report` score and never merged into it.
        ONE Claude call per chunk (one call for a normal-length article). Never raises → ([], {}).

        Deliberately NOT asked for (the operator's "skip the sources url check for now"): whether a cited
        [S#] actually supports its claim, and whether the same page is listed twice under two URLs."""
        name = ((brand or {}).get("name") or "").strip()
        try:
            _canon = "; ".join(f"{i.get('product') or 'general'}: {i.get('value')}"
                               for i in _kf_pricing_items((brand or {}).get("key_facts")) if i.get("value"))
        except Exception:
            _canon = ""
        chunks = self._verify_chunks(body)
        if not chunks:
            return [], {}
        issues, assessments = [], []
        for n, chunk in enumerate(chunks):
            part = (f"\nThis is part {n + 1} of {len(chunks)} of the article; judge only what is here.\n"
                    if len(chunks) > 1 else "")
            try:
                res = self.claude.call(
                    "You are proof-checking a FINISHED published article for a brand. Read ALL of it. "
                    "Report ONLY real defects — a stylistic preference is NOT a defect.\n"
                    "LOOK FOR:\n"
                    "- contradiction: the article contradicting itself (a claim one section makes that the "
                    "comparison table, the Quick answer or the FAQ contradicts).\n"
                    "- price: a price or figure stated inconsistently.\n"
                    "- brand: the brand described or named inconsistently.\n"
                    "- typo: a spelling or grammar error, a wrong word, a broken sentence.\n"
                    "- readability: a garbled or unreadable sentence.\n"
                    "- duplicate: the same sentence or the same claim written twice.\n"
                    "- link: the article points the reader at a page ('see our guide', 'linked below', "
                    "'read more') but gives no link.\n"
                    "- thin: a section that is one sentence of filler, or a question heading whose first "
                    "sentence does not answer it.\n"
                    "Do NOT report anything about whether a [S#] source supports its claim, and do NOT "
                    "report duplicate sources — those are out of scope for this pass.\n"
                    "RULES FOR YOUR FIXES — a fix breaking any of these will be discarded:\n"
                    "- NEVER write that something is unavailable, not found, not specified, not disclosed or "
                    "not public. That wording is banned in this publication; if a fact is missing the "
                    "sentence must simply not claim it.\n"
                    "- Keep every [S#] citation marker exactly as it appears in the quote — never add, "
                    "remove or renumber one.\n"
                    "- Never change the H1 title, the italic byline/disclosure lines, or the ## Sources "
                    "section.\n"
                    "- Fix ONLY from what is already in the article or the canonical facts below. Never "
                    "introduce a new fact, figure or name.\n"
                    "- Use plain ASCII punctuation: no em-dash, en-dash, curly quotes or ellipsis character.\n"
                    "- For `duplicate`, `link` and `thin` leave `fix` empty — they are reported, not fixed.\n"
                    "ALSO return an editorial ASSESSMENT of this article as a whole: does it answer the "
                    "question it sets out to, is it specific rather than generic, is it balanced about the "
                    "brand, and does it read as though a person wrote it. Score it 0-100 and be honest — a "
                    "competent but unremarkable article is a 70.\n"
                    f"BRAND: {name}\n" + (f"CANONICAL PRICING (authoritative): {_canon}\n" if _canon else "") +
                    part +
                    '\nReturn JSON ONLY: {"issues": [{"kind": "contradiction|price|brand|typo|readability|'
                    'duplicate|link|thin", "quote": "<the exact sentence from the article>", '
                    '"problem": "<what is wrong>", "fix": "<the corrected sentence, or \\"\\">"}], '
                    '"assessment": {"score": 0-100, "verdict": "<one sentence>", '
                    '"strengths": ["..."], "weaknesses": ["..."]}}\n\nARTICLE:\n' + chunk,
                    max_tokens=3000, temperature=0)
            except Exception as e:
                print(f"[blog_gen] verify-content failed ({e})", flush=True)
                continue
            if not isinstance(res, dict):
                continue
            for i in (res.get("issues") or []):
                if isinstance(i, dict) and str(i.get("quote") or "").strip():
                    issues.append({"kind": str(i.get("kind") or "content").strip().lower(),
                                   "quote": str(i["quote"]).strip(),
                                   "problem": str(i.get("problem") or "").strip(),
                                   "fix": str(i.get("fix") or "").strip()})
            a = res.get("assessment")
            if isinstance(a, dict):
                assessments.append(a)
        return issues, self._merge_assessments(assessments)

    @staticmethod
    def _merge_assessments(assessments):
        """One assessment per chunk → one for the article. Scores averaged, lists merged and deduped."""
        vals = []
        for a in assessments:
            try:
                vals.append(max(0, min(100, int(float(a.get("score"))))))
            except Exception:
                pass
        if not assessments:
            return {}
        def _lst(key):
            seen, out = set(), []
            for a in assessments:
                for x in (a.get(key) or [])[:6]:
                    t = str(x).strip()
                    if t and t.lower() not in seen:
                        seen.add(t.lower())
                        out.append(t)
            return out[:5]
        verdict = next((str(a.get("verdict")).strip() for a in assessments if str(a.get("verdict") or "").strip()), "")
        out = {"verdict": verdict, "strengths": _lst("strengths"), "weaknesses": _lst("weaknesses")}
        if vals:
            out["score"] = round(sum(vals) / len(vals))
        return out
    def _verify_repair_gate(self, body, quote, fix, brand):
        """Every reason a PROSE repair is refused. Returns "" when the repair may be applied, else the
        reason it was skipped. These six exist because they are exactly what the guards that run after a
        repair CANNOT undo (an added em-dash, a reworded H1, a DROPPED citation, a lost fact, an edited
        byline). A punt is caught here too so it is REPORTED rather than silently scrubbed later."""
        if body.count(quote) != 1:
            return "the sentence was not found exactly once in the article"
        if re.findall(r"\[S\d+\]", quote) != re.findall(r"\[S\d+\]", fix):
            return "the fix changed the [S#] citations"
        cut = self._VF_SOURCES_RE.search(body)
        if cut and body.index(quote) >= cut.start():
            return "the sentence is inside the ## Sources section (rebuilt from evidence, never edited)"
        _line = quote.splitlines()[0].strip()
        if _line.startswith("# "):
            return "the sentence is the H1 title (pinned to the seed)"
        if _line.startswith("*") and _line.endswith("*") and len(_line) > 2:
            return "the sentence is the byline / disclosure line"
        # FU205 (R3): screen the fix against EVERY regex the guards downstream delete on, not just
        # the two cell-level ones. The gate used to check `_PUNT_MEANING_RE` / `_PUNT_CELL_RE` only,
        # so a PROSE punt — "Pricing depends on the specific plan you choose." — passed the gate, was
        # reported to the operator as `applied`, and was then silently deleted by `_scrub_punts`,
        # leaving a hole exactly where the defect had been. A repair that cannot survive the scrubs
        # must be REFUSED and reported as skipped, not applied-then-erased.
        if (self._PUNT_MEANING_RE.search(fix) or self._PUNT_CELL_RE.search(fix)
                or self._PUNT_SENT_RE.search(fix) or self._PUNT_URL_SENT_RE.search(fix)
                or self._PUNT_PROSE_RE.search(fix)):
            return "the fix says a fact is unavailable — that wording is banned"
        if self._META_RE.search(fix):
            return "the fix narrates an editing decision — that is scrubbed from the article"
        if any(c in fix for c in self._VF_BANNED_CHARS):
            return "the fix reintroduced an em-dash / curly quote / ellipsis"
        ok, missing = self._facts_preserved(quote, fix, brand)
        if not ok:
            return f"the fix dropped {', '.join(missing[:3])}"
        return ""

    def _verify_apply_repairs(self, body, issues, brand, repair="claude", timeout=600):
        """Apply the gated repairs. repair="claude" trusts the issue's own `fix`; repair="writer" sends the
        sentence to the self-hosted model instead — the operator's "check with claude but correct it using
        qwen" — reusing the `_verify_facts_semantic` repair shape. A refused repair leaves the ORIGINAL
        span untouched and is reported. Returns (body, applied, skipped)."""
        applied, skipped = [], []
        todo = [i for i in issues if body.count(i["quote"]) == 1]
        reps = {}
        if repair == "writer" and self.writer is not None and todo:
            def _one(it):
                try:
                    r = self.writer.call_text(
                        "Rewrite this sentence to fix the problem described. Keep every [S#] marker, "
                        "every number and every name exactly. Never say a fact is unavailable. Use plain "
                        "ASCII punctuation.\n"
                        f"PROBLEM: {it['problem']}\nReturn ONLY the corrected sentence.\n\n"
                        f"SENTENCE: {it['quote']}", max_tokens=600, temperature=0.7, timeout=timeout)
                except Exception:
                    r = None
                return (_strip_model_preamble(r or "") or "").strip().split("\n")[0].strip()
            if len(todo) > 1:
                with ThreadPoolExecutor(max_workers=min(_WRITER_WORKERS, len(todo))) as _ex:
                    for it, r in zip(todo, _ex.map(_one, todo)):
                        reps[id(it)] = r
            else:
                reps[id(todo[0])] = _one(todo[0])
        for it in issues:
            fix = reps.get(id(it)) or it.get("fix") or ""
            rec = {"kind": it.get("kind"), "problem": it.get("problem"), "quote": it["quote"][:160]}
            if not fix:
                skipped.append({**rec, "reason": "the model returned no correction"})
                continue
            why = self._verify_repair_gate(body, it["quote"], fix, brand)
            if why:
                skipped.append({**rec, "reason": why})
                continue
            body = body.replace(it["quote"], fix, 1)
            applied.append({**rec, "fix": fix[:160]})
        return body, applied, skipped

    def _quality_report(self, article, brand, link_targets=None):
        """FU151 (D): deterministic quality scorecard — STRUCTURE (Quick answer / question-headings /
        FAQ / ≥3-competitor table), META lengths, INTERNAL-LINK honesty — plus the folded `geo_warning`
        notes. Returns {score, checks:[{key,label,ok,detail}], warnings:[...]}. Never raises. No network."""
        body = article.get("body_markdown") or ""
        name = ((brand or {}).get("name") or "").strip()
        low = body.lower()
        checks = []

        def add(key, label, ok, detail=""):
            checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})
        # STRUCTURE (the real gap — was prompt-trust only)
        add("quick_answer", "Quick answer present",
            bool(re.search(r"(?im)^#{1,4}\s*(quick answer|short answer|tl;?dr)\b", body))
            or "quick answer" in low)
        _qh = len(re.findall(r"(?m)^#{2,4}\s+.*\?\s*$", body))
        add("question_headings", "Question-shaped headings", _qh >= 1, f"{_qh} found")
        add("faq", "FAQ section present",
            bool(re.search(r"(?im)^#{1,4}\s*(faq|frequently asked)", body)) or bool(_parse_faq_pairs(body)))
        _ents = _first_table_entities(body)
        if _ents:
            _comp = [e for e in _ents if not (name and name.lower() in e.lower())]
            add("comparison", "Comparison names ≥3 competitors", len(_comp) >= 3,
                f"{len(_comp)} competitor row(s)")
        # META lengths (SEO hygiene — WARN, never silently reword)
        _mt = (article.get("meta_title") or "").strip()
        _md = (article.get("meta_description") or "").strip()
        add("meta_title", "Meta title present, ≤60 chars", bool(_mt) and len(_mt) <= 60, f"{len(_mt)} chars")
        add("meta_desc", "Meta description present, ≤160 chars",
            bool(_md) and len(_md) <= 160, f"{len(_md)} chars")
        # FU201: deliberately NOT added as a scored check. The pass already folds a `verify-check:` note
        # into `geo_warning`, which this method re-splits into `warnings` and docks 5 points for — exactly
        # how the other ~11 `*-check:` warnings behave. Adding a check here too would dock the SAME defect
        # twice. The full detail renders in the modal's Verification block from `quality_report.verify`.
        # INTERNAL-LINK HONESTY (only when internal linking was on): every emitted internal link must be
        # one of the FU114 verified targets. No network.
        if link_targets:
            _verified = set()
            for t in link_targets:
                u = ((t.get("url") if isinstance(t, dict) else t) or "").strip().split("?")[0].rstrip("/").lower()
                if u:
                    _verified.add(u)
            _own = _norm_domain((brand or {}).get("domain_url") or "")
            _bad = []
            for u in re.findall(r"\]\((https?://[^)\s]+)\)", body):
                key = u.strip().split("?")[0].rstrip("/").lower()
                d = _norm_domain(u)
                if _own and d and (d == _own or d.endswith("." + _own)) and key not in _verified:
                    _bad.append(u)
            add("internal_links", "Internal links are verified targets", not _bad,
                (f"{len(_bad)} not in the verified list" if _bad else "all verified"))
        # Fold in the finalize warnings (geo/qualifier/YMYL/peer/publisher-blank/source-authority/…).
        # FU205 (R2): read the STRUCTURED list when it is there — one entry per check that fired.
        # The `.split(";")` fallback below is what this method used to do for everything, and it
        # shredded the four checks that join their own sub-hits with "; ": three real problems were
        # counted as five and docked -25 instead of -15, and a context-free fragment was shown to the
        # operator as a warning in its own right. It is kept ONLY for an article dict that predates
        # the structured list (an older stored report, a caller that builds the dict by hand).
        _wl = article.get("warnings")
        if isinstance(_wl, list) and _wl:
            warnings = [str(w.get("detail") or "").strip() if isinstance(w, dict) else str(w).strip()
                        for w in _wl]
            warnings = [w for w in warnings if w]
        else:
            warnings = [w.strip() for w in (article.get("geo_warning") or "").split(";") if w.strip()]
        total = len(checks) or 1
        passed = sum(1 for c in checks if c["ok"])
        score = max(0, round(100 * passed / total) - min(len(warnings) * 5, 25))
        return {"score": score, "checks": checks, "warnings": warnings}

    def finish_pending_blog(self, brand, seed, checkpoint, provided):
        """FU79 resume — complete a blog paused for manual sources WITHOUT re-gathering / re-generating /
        re-sourcing. `checkpoint` is what `generate_blog`'s `_pending` sentinel stored; `provided` is a
        list of {tool, url?, fact?, skip?}. For each non-skipped item that yields text (url fetched
        verbatim, else the pasted fact), build a TOOL-LABELED evidence block so the reconcile KEEPS that
        tool's row; skipped/blank tools stay dropped. Then runs only the reconcile + finalize tail.
        Returns the finished article dict (same shape as generate_blog). Never raises for a bad item."""
        ck = checkpoint or {}
        sourcing = dict(ck.get("sourcing") or {})
        sourcing["fresh"] = list(sourcing.get("fresh") or [])
        sourcing["unsourced"] = list(sourcing.get("unsourced") or [])
        article = dict(ck.get("article") or {})
        draft_body = ck.get("draft_body") or ""
        # restore the base evidence set so [S#] numbering + _rebuild_sources stay correct
        self._evidence_blocks = list(ck.get("evidence_blocks") or [])
        # FU205 (R4): restore the deterministic checks' notes, so a RESUMED blog runs the same checks
        # as an unpaused one instead of silently reporting clean on all of them.
        self._restore_check_notes(ck.get("check_notes") or {})
        # FU184: the invented-competitor flag was computed during sourcing, which does NOT re-run on a
        # FU79 resume — rebuild it from the checkpointed list so the warning survives the pause.
        # FU204: same reason — the citation-attribution check needs the compared brand names, and
        # sourcing does not re-run on a resume, so rebuild them from the checkpoint.
        self._article_tools = [str(x).strip() for x in (sourcing.get("tools") or []) if str(x).strip()]
        _inv_ck = [str(x).strip() for x in ((ck.get("sourcing") or {}).get("invented_tools") or [])
                   if str(x).strip()]
        if _inv_ck:
            _one_ck = len(_inv_ck) == 1
            self._invented_note = (
                "competitor-check: " + ", ".join(_inv_ck) + (" was" if _one_ck else " were") +
                " named by the model, not from this brand's competitor list or the gathered evidence — "
                f"verify {'it belongs' if _one_ck else 'they belong'} in this comparison, or add/remove "
                "in Edit Brand")

        # FU206 — the operator may REMOVE a thin competitor rather than let it cost the whole field
        # its dimensions. The removal is from the ENTIRE blog, not just the table: its row, its
        # evidence, its prose mentions and any FAQ entry naming it. The FU105 floor is re-checked
        # here as well as at ask time, because the checkpoint is operator-editable and a removal that
        # breaches the floor would hand back a self-crowning comparison — the exact failure the floor
        # exists to prevent. A refused removal is REPORTED, never silently ignored.
        _tools_now = [str(t).strip() for t in (sourcing.get("tools") or []) if str(t).strip()]
        removed, refused = [], []
        for item in (provided or []):
            if not isinstance(item, dict) or not item.get("remove"):
                continue
            _t = str(item.get("tool") or "").strip()
            if not _t:
                continue
            if len([x for x in _tools_now if x.lower() != _t.lower()]) < _MIN_COMPARISON_BRANDS:
                refused.append(_t)
                print(f"[blog_gen] remove-brand: REFUSED {_t} — removing it would leave fewer than "
                      f"{_MIN_COMPARISON_BRANDS} competitors; kept in the comparison instead", flush=True)
                continue
            _tools_now = [x for x in _tools_now if x.lower() != _t.lower()]
            removed.append(_t)
        if removed:
            _low = {r.lower() for r in removed}
            sourcing["tools"] = _tools_now
            sourcing["peers"] = [p for p in (sourcing.get("peers") or [])
                                 if str(p).strip().lower() not in _low]
            sourcing["fresh"] = [f for f in sourcing["fresh"]
                                 if str(f.get("label") or "").strip().lower() not in _low]
            sourcing["unsourced"] = [u for u in sourcing["unsourced"]
                                     if str(u.get("tool") or "").strip().lower() not in _low]
            self._evidence_blocks = [b for b in (self._evidence_blocks or [])
                                     if str(b.get("label") or "").strip().lower() not in _low]
            self._removed_brands = removed
            print(f"[blog_gen] remove-brand: {', '.join(removed)} removed from the ENTIRE blog "
                  f"(row, evidence and prose) — {len(_tools_now)} competitor(s) remain", flush=True)
        if refused:
            self._removed_refused = refused

        resolved = set()
        for item in (provided or []):
            if not isinstance(item, dict):
                continue
            tool = str(item.get("tool") or "").strip()
            if not tool or item.get("skip") or item.get("remove"):
                continue
            url = str(item.get("url") or "").strip()
            fact = str(item.get("fact") or "").strip()
            text = ""
            if url:
                try:
                    text = self._fetch_url(url)
                except Exception:
                    text = ""
            if not text and fact:
                text = fact
            if not text:
                continue   # nothing usable for this tool → it stays dropped
            sourcing["fresh"].append({"label": tool, "url": url, "text": text[:_EVIDENCE_TEXT_CAP]})
            resolved.add(tool.lower())
        if resolved:
            sourcing["unsourced"] = [u for u in sourcing["unsourced"]
                                     if str(u.get("tool") or "").lower() not in resolved]

        # FU206: a removal needs the reconcile to run even when nothing new was provided — it is the
        # writer that has to take the brand out of the prose the draft already contains.
        if sourcing.get("fresh") or removed:
            vc = self._reconcile_and_finish(brand, seed, article, sourcing)
            if vc:
                article["body_markdown"] = vc["body_markdown"]
                article["claims_flagged"] = (article.get("claims_flagged") or []) + vc["flagged"]
        # FU90: the checkpoint's sourcing carries the resolved geo — the resume stays geo-aware.
        return self._finalize_article(brand, seed, article, draft_body,
                                      geo=(sourcing.get("geo") or ""),
                                      qualifier=(sourcing.get("qualifier") or ""),   # FU93
                                      ymyl=(sourcing.get("ymyl") or None))   # FU133


# ----------------------------------------------------------------------------- JSON-LD
def _iso_dt(dt):
    """SQLite datetime('now') (UTC, 'YYYY-MM-DD HH:MM:SS') -> ISO 8601. Date-only stays date."""
    s = (dt or "").strip()
    if not s:
        return ""
    s = s.replace(" ", "T")
    if "T" in s and not s.endswith("Z"):
        s += "Z"
    return s


def _parse_faq_pairs(body_md):
    """Extract (question, answer) pairs from the article's FAQ section for FAQPage schema.
    Tolerant: prefers the pinned `### <q>?` H3 convention; falls back to `**Q: …?**` / `**…?**`.
    Returns a list of {"q","a"}."""
    if not body_md:
        return []
    text = body_md
    # Isolate the FAQ section (## FAQ … until the next ## section), else scan the whole body.
    m = re.search(r"(?im)^\s*#{2,3}\s*FAQ\b.*?$", text)
    faq = text[m.end():] if m else text
    if m:
        nxt = re.search(r"(?m)^\s*##\s+(?!#)", faq)
        if nxt:
            faq = faq[:nxt.start()]
    pairs = []
    h3 = list(re.finditer(r"(?m)^\s*###\s+(.+?)\s*$", faq))
    if h3:
        for i, mm in enumerate(h3):
            q = mm.group(1).strip().strip("#").strip()
            end = h3[i + 1].start() if i + 1 < len(h3) else len(faq)
            a_raw = faq[mm.end():end]
            # Stop at a thematic break (---, ***, ___) — it separates the FAQ from the
            # next section (e.g. ## Sources) and must NOT leak into the last answer.
            hr = re.search(r"(?m)^\s*([-*_])\1{2,}\s*$", a_raw)
            if hr:
                a_raw = a_raw[:hr.start()]
            a = re.sub(r"\s+", " ", a_raw).strip()
            # Strip inline [S#] citation markers — they're meaningless in isolated FAQPage
            # schema and read as noise; tidy any double space they leave behind.
            a = re.sub(r"\s*\[S\d+\]", "", a).strip()
            if q and a and q.endswith("?"):
                pairs.append({"q": q, "a": a[:700]})
    if pairs:
        return pairs
    for mm in re.finditer(r"(?m)^\s*\*\*(?:Q:\s*)?(.+?\?)\*\*\s*(.*)$", faq):
        q = mm.group(1).strip()
        a = re.sub(r"^A:\s*", "", (mm.group(2) or "").strip())
        if q and a:
            pairs.append({"q": q, "a": re.sub(r"\s+", " ", a)[:700]})
    return pairs


def _parse_howto_steps(body_md):
    """FU151 (C): the LONGEST run of consecutive Markdown numbered-list items (`1. …`) as ordered
    step texts. [] when fewer than 2 — deterministic, no LLM."""
    best, run = [], []
    for line in (body_md or "").splitlines():
        m = re.match(r"^\s*\d+[.)]\s+(.+)$", line)
        if m:
            run.append(m.group(1).strip())
        else:
            if len(run) > len(best):
                best = run
            run = []
    if len(run) > len(best):
        best = run
    return best if len(best) >= 2 else []


def _first_table_entities(body_md):
    """FU151 (C): the first Markdown pipe-table's data-row FIRST-column values (the compared
    entities), for an ItemList. []-safe."""
    lines = (body_md or "").splitlines()
    hdr = None
    for i, ln in enumerate(lines):
        s = ln.strip()
        if s.startswith("|") and "|" in s[1:]:
            nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
            if "-" in nxt and re.fullmatch(r"\|?[\s:|-]+\|?", nxt):
                hdr = i
                break
    if hdr is None:
        return []
    out = []
    for ln in lines[hdr + 2:]:
        s = ln.strip()
        if not (s.startswith("|") and "|" in s[1:]):
            break
        cells = [c.strip() for c in s.strip("|").split("|")]
        first = re.sub(r"[\*`\[\]]", "", (cells[0] if cells else "")).strip()
        if first and not re.fullmatch(r":?-{2,}:?", first):
            out.append(first)
    return out


def _price_amount(value):
    """FU151 (C): first currency amount in a free-form pricing string → (amount, currency) or
    (None, None)."""
    m = re.search(r"([\$€£])\s?(\d[\d,]*(?:\.\d+)?)", value or "")
    if not m:
        return None, None
    return m.group(2).replace(",", ""), {"$": "USD", "€": "EUR", "£": "GBP"}.get(m.group(1), "USD")


def _validate_jsonld(graph):
    """FU151 (C): deterministic required-field check per schema.org type. Returns a list of problem
    strings ([] = clean) so the operator sees a schema gap instead of shipping invalid markup."""
    problems = []
    for node in (graph or []):
        t = node.get("@type")
        if t == "Article":
            for f in ("headline", "datePublished", "author", "publisher"):
                if not node.get(f):
                    problems.append(f"Article missing {f}")
        elif t == "FAQPage":
            me = node.get("mainEntity") or []
            if not me:
                problems.append("FAQPage has no questions")
            elif any(not q.get("name") or not ((q.get("acceptedAnswer") or {}).get("text")) for q in me):
                problems.append("FAQPage question missing name/answer")
        elif t == "HowTo":
            if len(node.get("step") or []) < 2:
                problems.append("HowTo has <2 steps")
        elif t == "ItemList":
            if len(node.get("itemListElement") or []) < 2:
                problems.append("ItemList has <2 items")
        elif t == "Product":
            if not node.get("name") or not node.get("offers"):
                problems.append("Product missing name/offers")
        elif t == "BreadcrumbList":
            if not node.get("itemListElement"):
                problems.append("BreadcrumbList empty")
    return problems


def build_blog_jsonld(blog, brand=None, page_url=""):
    """Build an Article + FAQPage JSON-LD @graph for a blog (pure parsing, no LLM). Dates from
    blog.created_at/updated_at. Byline is resolved per-field as a PER-BLOG value overriding the
    brand byline (author = a Person when a name resolves, ELSE the brand Organization — never
    fabricated; reviewer/disclosure likewise). publisher = brand Organization (+logo when set);
    Article gets image (blog image_url else brand logo_url), inLanguage, and — when page_url is
    given — url + mainEntityOfPage. FAQPage mainEntity parsed from the body's FAQ section. All
    enrichments are graceful (emitted only when the data exists). Returns a dict for json.dumps."""
    blog = blog or {}
    brand = brand or {}
    title = (blog.get("title") or "").strip()
    desc = (blog.get("meta_description") or "").strip()
    kws = blog.get("keywords")
    if isinstance(kws, str):
        try:
            kws = json.loads(kws)
        except Exception:
            kws = [k.strip() for k in kws.split(",") if k.strip()]
    kws = kws if isinstance(kws, list) else []
    published = _iso_dt(blog.get("created_at"))
    modified = _iso_dt(blog.get("updated_at")) or published
    brand_name = (brand.get("name") or "").strip()
    brand_url = (brand.get("domain_url") or "").strip()
    if brand_url and not brand_url.startswith(("http://", "https://")):
        brand_url = "https://" + brand_url
    logo_url = (brand.get("logo_url") or "").strip()
    # Article image: a per-blog image_url if set, else the brand logo (only emitted when present).
    image_url = (blog.get("image_url") or "").strip() or logo_url
    # Prefer https in structured data (avoid http:// from an og:image).
    if logo_url.startswith("http://"):
        logo_url = "https://" + logo_url[len("http://"):]
    if image_url.startswith("http://"):
        image_url = "https://" + image_url[len("http://"):]

    # Per-field byline: a per-blog value OVERRIDES the brand byline; fall back to the brand's.
    def _pick(field):
        return (blog.get(field) or brand.get(field) or "").strip()

    publisher = {"@type": "Organization", "name": brand_name or "Publisher"}
    if brand_url:
        publisher["url"] = brand_url
    if logo_url:
        publisher["logo"] = {"@type": "ImageObject", "url": logo_url}
    au = _pick("author_name")
    if au:
        author = {"@type": "Person", "name": au}
        at = _pick("author_title")
        if at:
            author["jobTitle"] = at
    else:
        author = dict(publisher)  # Organization author — legitimate (the brand published it)
    article = {"@type": "Article", "headline": title[:110], "description": desc,
               "author": author, "publisher": publisher, "inLanguage": "en"}
    if image_url:
        article["image"] = image_url
    if kws:
        article["keywords"] = ", ".join(str(k) for k in kws)
    if published:
        article["datePublished"] = published
    if modified:
        article["dateModified"] = modified
    rv = _pick("reviewer_name")
    if rv:
        reviewer = {"@type": "Person", "name": rv}
        rt = _pick("reviewer_title")
        if rt:
            reviewer["jobTitle"] = rt
        article["reviewedBy"] = reviewer
    if page_url:
        article["url"] = page_url
        article["mainEntityOfPage"] = {"@type": "WebPage", "@id": page_url}
    graph = [article]
    faqs = _parse_faq_pairs(blog.get("body_markdown") or "")
    # FU54: keep the FAQPage schema TOPIC-focused — drop any FAQ whose QUESTION names the subject brand
    # (brand-promo Q&A baked into FAQPage reads as advertising, not a topic answer). The visible body FAQ
    # is untouched; only the structured data is de-promoted. If every question names the brand, emit no
    # FAQPage rather than a page of ads.
    if faqs and brand_name:
        bn = brand_name.lower()
        faqs = [f for f in faqs if bn not in (f.get("q") or "").lower()]
    if faqs:
        graph.append({
            "@type": "FAQPage",
            "mainEntity": [
                {"@type": "Question", "name": f["q"],
                 "acceptedAnswer": {"@type": "Answer", "text": f["a"]}}
                for f in faqs
            ],
        })
    body_md = blog.get("body_markdown") or ""
    # FU151 (C) — HowTo: only for a how-to intent (seed/title) WITH a real numbered-step sequence.
    if re.search(r"\bhow to\b|\bhow do i\b|\bstep[- ]by[- ]step\b|\bsteps to\b",
                 ((blog.get("seed") or "") + " " + title).lower()):
        steps = _parse_howto_steps(body_md)
        if len(steps) >= 2:
            graph.append({"@type": "HowTo", "name": title or "How-to",
                          "step": [{"@type": "HowToStep",
                                    "name": (re.split(r"[.:]", s)[0] or s)[:80], "text": s}
                                   for s in steps]})
    # FU151 (C) — ItemList: the compared entities when a comparison table exists.
    entities = _first_table_entities(body_md)
    if len(entities) >= 2:
        graph.append({"@type": "ItemList",
                      "itemListElement": [{"@type": "ListItem", "position": i + 1, "name": e}
                                          for i, e in enumerate(entities)]})
    # FU151 (C) / FU158 — Product + Offer for the SUBJECT from canonical key_facts pricing. Honor
    # operator-set items (never guard-skip them) and emit the ONE Offer for THIS article's product
    # (operator-set match preferred) so an operator's canonical price wins over a stale general /
    # other-product item; the verbatim value in `description`, a best-effort numeric `price`.
    try:
        kf_items = _kf_pricing_items(json.loads(brand.get("key_facts") or "{}"))
    except Exception:
        kf_items = []

    def _url_path(u):   # FU158: the real URL path ("" for a bare domain, whether or not it has a scheme)
        u = re.sub(r"^[a-z]+://", "", (u or "").strip(), flags=re.I)
        i = u.find("/")
        return u[i:].strip("/").lower() if i != -1 else ""

    def _offer_ok(it):
        if not (it.get("value") or "").strip():
            return False
        if it.get("operator_set"):
            return True   # FU158: operator items are trusted — never guard-skip
        _p = str(it.get("product") or "").strip()
        if not _p:
            return True
        # FU156: skip a NAMED non-operator item whose product token isn't in its OWN url path (an
        # auto-mislabeled item like {tirzepatide, url:.../mens-trt/}); FU158: keep an empty-path
        # (bare-domain) item — with no path we can't judge it, so don't drop it.
        _toks = _product_tokens(_p)
        _path = _url_path(it.get("source_url"))
        return not (_toks and _path and not any(t in _path for t in _toks))

    def _mk_offer(it):
        val = (it.get("value") or "").strip()
        off = {"@type": "Offer", "description": val[:200]}
        amt, cur = _price_amount(val)
        if amt:
            off["price"] = amt
            off["priceCurrency"] = cur
        if it.get("product"):
            off["name"] = it["product"]
        u = (it.get("source_url") or "").strip()
        if u:   # FU158: normalize a bare-domain / http url to https for the schema
            if u.startswith("http://"):
                u = "https://" + u[len("http://"):]
            elif not u.startswith("https://"):
                u = "https://" + u
            off["url"] = u
        return off

    _ok_items, _ = _drop_nameless_when_named([it for it in kf_items if _offer_ok(it)])   # FU163: no nameless general Offer beside named
    # FU158: prefer the ONE item for THIS article's product (operator-set first), so a stale general /
    # other-product item can never be advertised as the price on a product-specific article.
    #
    # FU205 (R4): that precedence is `_canonical_price_item` — a function whose docstring has always
    # claimed it makes an operator-set price "the single source of truth for the SUBJECT's pricing
    # cell AND the JSON-LD Offer", and which had THREE passing tests and ZERO production callers.
    # A green suite proving a function works while the product hand-rolls the same rule beside it is
    # exactly the failure the audit set out to close, so the rule now lives in one place.
    _art_product = ((blog.get("seed") or "") + " " + title).strip()
    _canon = _canonical_price_item({"pricing": {"items": _ok_items}}, _art_product)
    _art_toks = set(_product_tokens(_art_product))
    _matched = [it for it in _ok_items
                if _art_toks and (_art_toks & set(_product_tokens(it.get("product") or "")))]
    if _matched:
        offers = [_mk_offer(_canon if _canon in _matched else
                            next((it for it in _matched if it.get("operator_set")), _matched[0]))]
    else:
        # FU161: no product-token match (a general-topic seed like "GLP-1 programs") — when the operator
        # has set canonical pricing, emit ONLY operator-set items so a stale auto-synced general item
        # (e.g. the $79 TRT price) can never accompany the operator's price. No operator items → today's.
        _ops = [it for it in _ok_items if it.get("operator_set")]
        offers = [_mk_offer(it) for it in (_ops if _ops else _ok_items)]
    if offers and brand_name:
        prod = {"@type": "Product", "name": brand_name,
                "offers": offers if len(offers) > 1 else offers[0]}
        if brand_url:
            prod["url"] = brand_url
        if image_url:
            prod["image"] = image_url
        graph.append(prod)
    # FU151 (C) — BreadcrumbList: only once the article has a live URL (published).
    if page_url:
        graph.append({"@type": "BreadcrumbList", "itemListElement": [
            {"@type": "ListItem", "position": 1, "name": brand_name or "Home",
             "item": brand_url or page_url},
            {"@type": "ListItem", "position": 2, "name": title or "Article", "item": page_url}]})
    return {"@context": "https://schema.org", "@graph": graph}
