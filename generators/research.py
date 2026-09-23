"""FU221 (Step 0, R0) — research a brand's facts the way claude.ai does, with every quote checked.

Why this exists: the blog pipeline used to ask a search engine one small question at a time (1-2
searches, "one fact per page", a model-written summary back), so it rarely reached the page that
actually states a price, a licence or a policy — and when it did, the condition words ("with
insurance", "first month", "membership required") were lost in the one-line summary. Tested on
Railway (19 Sep) against the three hand audits: this split design answered every question with the
brand's own page and an exact quote.

The four steps, per brand:
  A. FIND   — one search pinned to the brand's own site: which pages state each need (URLs only).
  B. READ   — our fetch (direct → residential), and Anthropic's web fetch when the site walls us.
              The page's product data (JSON-LD offers) is appended, because some stores render the
              price only there (Philips: nothing visible, "price": "8.99" in the schema).
  C. EXTRACT — one tool-free call over the text WE hold: answer, url, exact quote, product line.
  D. VERIFY — the quote must be on that page word for word (markdown / curly quotes / whitespace
              normalised). A price may instead be verified by its figure sitting near the product
              name on the page — store listings split "Sale price $34.99 Regular price $39.99" across
              elements — and a figure shown as a SALE price is never accepted as the price.
Needs still unconfirmed get ONE more round (a fresh find for just those needs). Anything still
unconfirmed is returned as such; callers fall back to the older tiers and, last, the operator pause.

Everything here is vertical-neutral: the needs come from the article's own columns, products and the
brand's own domain. Nothing names a vertical, a site or a product.
"""

import html as _html          # FU267: NCBI's API returns XML entities
import json
import os
import re
import unicodedata
import urllib.request as _urlreq   # FU267: NCBI E-utilities

from generators.brand_enrichment import _extract_visible_text, _fetch_page

# A need is about price when its wording says so — the only need kind with extra checks.
PRICE_NEED_RE = re.compile(r"\b(pric\w*|cost\w*|fees?|plans?\b|rates?|subscription|membership|"
                           r"per month|/mo|apr|premium|charge\w*|tuition)\b", re.I)
_FIG_RE = re.compile(r"(?:[$€£₹]|USD|EUR|GBP|INR|A\$|C\$)\s?\d[\d,]*(?:\.\d{1,2})?")
# Sale wording in the few characters BEFORE a figure marks it as a sale price, not the regular one.
_SALE_BEFORE_RE = re.compile(r"(sale|now|deal|discount\w*|save|today|promo\w*|clearance|"
                             r"special|intro\w*\s+offer)\W{0,3}(price)?\W{0,3}$", re.I)
_REGULAR_BEFORE_RE = re.compile(r"(regular|list|was|reg\.?|msrp|original)\W{0,3}(price)?\W{0,3}$",
                                re.I)
_MIN_QUOTE = 12   # characters — shorter "quotes" ("$29") prove nothing about where they came from
# FU222: a price question is usually answered by a LADDER (an intro figure and the ongoing one,
# or several plans). We keep every figure whose own quote is on the page, capped so one need
# cannot flood a cell, and print at most this many of them.
_MAX_FACTS_PER_NEED = 3
_MAX_TIERS = 3
_MAX_RUNG_LABEL = 30   # a plan name labels a rung; a full SKU title just bloats it


# FU223 — availability is asked PER PRODUCT, and a product named "the Pro plan" would otherwise
# trip PRICE_NEED_RE and send its availability question into the price search.
AVAIL_NEED_RE = re.compile(r"\b(availab\w*|coverage|eligib\w*|offered)\b", re.I)


def is_availability_need(need):
    return bool(AVAIL_NEED_RE.search(need or ""))


def is_price_need(need):
    return bool(PRICE_NEED_RE.search(need or "")) and not is_availability_need(need)


def norm(s):
    """Normalise text for quote matching: NFKC, straight quotes, markdown / link syntax and table
    pipes removed, whitespace collapsed, lower case."""
    s = unicodedata.normalize("NFKC", s or "")
    s = (s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
         .replace("–", "-").replace("—", "-").replace(" ", " "))
    s = re.sub(r"\]\([^)]*\)", " ", s)          # markdown link targets
    s = re.sub(r"[*_`#>|\[\]]", " ", s)           # markdown emphasis / headings / table pipes
    return re.sub(r"\s+", " ", s).strip().lower()


def quote_on_page(quote, page_text):
    """True when the quote appears on the page word for word (after `norm`). A quote with an ellipsis
    passes when every piece appears, in order."""
    q = norm(quote)
    if len(q) < _MIN_QUOTE:
        return False
    page = norm(page_text)
    pieces = [p.strip(" .") for p in re.split(r"\s*(?:\.\.\.|…)\s*", q) if p.strip(" .")]
    if not pieces:
        return False
    pos = 0
    for p in pieces:
        i = page.find(p, pos)
        if i < 0:
            return False
        pos = i + len(p)
    return True


def _figures(s):
    return [re.sub(r"\s", "", m.group(0)) for m in _FIG_RE.finditer(s or "")]


def _tokens(s):
    return [t for t in re.findall(r"[a-z0-9]{3,}", (s or "").lower())
            if t not in {"the", "and", "for", "with", "per", "month", "price", "regular", "from", "plan",
                         "each", "pack", "bottle", "product", "starts", "starting"}]


def price_on_page(answer, product, page_text, window=200):
    """True when the answer's FIRST figure appears on the page as a price (a currency sign or the word
    "price" right before it) within `window` characters of the product's name (or a word of it), and
    is not labelled a sale price. The fallback for store listings whose markup breaks a verbatim
    quote. Only the first figure counts: a figure the answer DERIVED ("≈ $20 per bottle") proves
    nothing."""
    figs = _figures(answer)
    if not figs:
        return False
    num = re.sub(r"[^\d.,]", "", figs[0]).rstrip(".,")
    if not num:
        return False
    page = page_text or ""
    ptoks = _tokens(product)
    for m in re.finditer(r"(?<![\d.,])" + re.escape(num) + r"(?!\d)", page):
        before = page[max(0, m.start() - 30): m.start()]
        if not re.search(r"([$€£₹]|usd|eur|gbp|inr|price)\W{0,3}$", before, re.I):
            continue
        if _SALE_BEFORE_RE.search(before) and not _REGULAR_BEFORE_RE.search(before):
            continue
        win = page[max(0, m.start() - window): m.end() + window].lower()
        if not ptoks or any(t in win for t in ptoks):
            return True
    return False


def figure_is_sale(answer, text, window=30):
    """True when the answer's FIRST figure appears in `text` only as a SALE price — sale wording right
    before every occurrence, with no "regular / list / was" label. A verified QUOTE can still carry a
    sale figure ("Sale price $35.99" is on the page, word for word), so quoted prices go through this
    too. False when the figure is not in the text at all."""
    figs = _figures(answer)
    if not figs:
        return False
    num = re.sub(r"[^\d.,]", "", figs[0]).rstrip(".,")
    hits = [m for m in re.finditer(r"(?<![\d.,])" + re.escape(num) + r"(?!\d)", text or "")]
    if not hits:
        return False
    for m in hits:
        before = (text or "")[max(0, m.start() - window): m.start()]
        if not (_SALE_BEFORE_RE.search(before) and not _REGULAR_BEFORE_RE.search(before)):
            return False   # at least one occurrence is not labelled a sale
    return True


# FU225 — scope words the PAGE puts in front of a figure. Anchored to the few characters directly
# before it (the `figure_is_sale` pattern), so "Save $50 from the list price of $499" cannot match:
# there, "of" is what precedes the figure, not "from".
_FROM_BEFORE_RE = re.compile(r"\b(from|starting at|starts at|start at|as low as|beginning at)"
                             r"\W{0,3}$", re.I)
_UPTO_BEFORE_RE = re.compile(r"\b(up to|as much as|maximum of|max of|no more than)\W{0,3}$", re.I)


def scope_on_page(answer, text, window=40):
    """The scope word the PAGE attaches to this figure — "from", "upto", or "".

    A scope word LIMITS a claim, and dropping one states the claim more broadly than its source
    does: "starting at $149" becoming "$149" turns a floor into a fixed price. `kind` was read off
    the model's own answer only, so a scope word the model left out was simply lost."""
    figs = _figures(answer)
    if not figs:
        return ""
    num = re.sub(r"[^\d.,]", "", figs[0]).rstrip(".,")
    if not num:
        return ""
    for m in re.finditer(r"(?<![\d.,])" + re.escape(num) + r"(?!\d)", text or ""):
        before = (text or "")[max(0, m.start() - window): m.start()]
        if _FROM_BEFORE_RE.search(before):
            return "from"
        if _UPTO_BEFORE_RE.search(before):
            return "upto"
    return ""


def split_clauses(basis):
    """Split a basis into its clauses on commas — but NOT on a thousands separator. "$1,094 for
    120mg" is one clause, not "$1" and "094 for 120mg"; splitting it the naive way made a real
    figure look like an unsupported condition."""
    return [c.strip() for c in re.split(r"(?<!\d),|,(?!\d)", basis or "") if c.strip()]


def unsupported_basis_clauses(basis, text):
    """Basis clauses carrying a NUMBER that is nowhere on the page.

    Deliberately numeric only. The extractor is told to build a basis from the page's own words,
    and checking WORDS against the page is where false rejections live — a condition is routinely
    stated a sentence away from the figure, or as "/mo" where the basis says "per month". A NUMBER
    is exact: "12-month plan" or "3-pack" with no 12 or 3 anywhere on the page was not read off it.
    Returns the clauses to drop; the figure itself is untouched, because it passed the quote gate."""
    out = []
    for clause in split_clauses(basis):
        nums = re.findall(r"\d[\d,]*(?:\.\d+)?", clause)
        if not nums:
            continue                      # words-only clause: never judged here
        for n in nums:
            plain = n.replace(",", "")
            if not any(re.search(r"(?<![\d.,])" + re.escape(v) + r"(?!\d)", text or "")
                       for v in {n, plain}):
                out.append(clause)
                break
    return out


def _jsonld_offers(html):
    """Product data from the page's JSON-LD (and bare `"price":` fields): lines like
    'PRODUCT DATA: Anti-colic bottle 9oz — price 8.99 USD'. Some stores render the price only here."""
    lines, seen = [], set()

    def walk(o):
        if isinstance(o, list):
            for x in o:
                walk(x)
            return
        if not isinstance(o, dict):
            return
        name = str(o.get("name") or "").strip()
        offers = o.get("offers")
        if offers:
            for off in (offers if isinstance(offers, list) else [offers]):
                if isinstance(off, dict):
                    price = off.get("price") or off.get("lowPrice")
                    cur = off.get("priceCurrency") or ""
                    if price not in (None, ""):
                        ln = f"PRODUCT DATA: {name or 'product'} — price {price} {cur}".strip()
                        if ln not in seen:
                            seen.add(ln)
                            lines.append(ln)
        for v in o.values():
            if isinstance(v, (dict, list)):
                walk(v)

    for m in re.finditer(r'<script[^>]+application/ld\+json[^>]*>(.*?)</script>', html or "",
                         re.I | re.S):
        try:
            walk(json.loads(m.group(1).strip()))
        except Exception:
            continue
    if not lines:
        tm = re.search(r"<title[^>]*>(.*?)</title>", html or "", re.I | re.S)
        title = re.sub(r"\s+", " ", tm.group(1)).strip() if tm else "product"
        for pm in re.finditer(r'"price"\s*:\s*"?(\d+(?:\.\d{1,2})?)"?', html or ""):
            ln = f"PRODUCT DATA: {title[:80]} — price {pm.group(1)}"
            if ln not in seen:
                seen.add(ln)
                lines.append(ln)
            if len(lines) >= 4:
                break
    return lines[:8]


def page_text_from_html(html, max_chars=20000):
    """Visible text plus the page's product data (so an unshown price can still be found and
    verified). Menus are included — main-content extraction is Step 2 (F3)."""
    text = _extract_visible_text(html or "", max_chars=max_chars)
    data = _jsonld_offers(html or "")
    return (text + ("\n" + "\n".join(data) if data else "")).strip()


# ── FU267 (step 4b): NCBI is read through NCBI's own API, not by scraping ───────────────────────
# Measured from Railway on 30 authority URLs taken from stored articles: 20% readable with our own
# fetch, 35% once Anthropic's web fetch is added. The single biggest failing family was PubMed/PMC,
# and it fails on EVERY rung — direct, residential (the proxy refuses to tunnel to those hosts at
# all) and web fetch — because all three receive the same reCAPTCHA interstitial:
#
#     title: Checking your browser - reCAPTCHA
#     Checking your browser before accessing pubmed.ncbi.nlm.nih.gov ...
#
# 376 characters of challenge page, every time. Scraping NCBI is closed.
#
# NCBI publishes E-utilities for exactly this, and it is not CAPTCHA-walled. Measured on the same
# host: the 1,055-infant JAMA trial an article had been mis-citing returns 5,292 characters of real
# abstract, and the BMC Research Notes study it had confused that trial WITH returns 74,275.
_NCBI_PMID_RE = re.compile(r"//pubmed\.ncbi\.nlm\.nih\.gov/(\d+)", re.I)
_NCBI_PMC_RE = re.compile(r"//(?:pmc|www)\.ncbi\.nlm\.nih\.gov/(?:pmc/)?articles/PMC(\d+)", re.I)
_EUTILS = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/efetch.fcgi"
_NCBI_TOOL = "&tool=blog_generator&email=" + os.environ.get("NCBI_EMAIL", "sources@example.com")


def _ncbi_api_text(url, max_chars=20000):
    """The abstract or full text from NCBI's own API. Returns (text, how) or ("", "") to fall
    through to the ordinary ladder — this must never become a new way to fail."""
    m = _NCBI_PMID_RE.search(url or "")
    if m:
        q = f"{_EUTILS}?db=pubmed&id={m.group(1)}&rettype=abstract&retmode=text{_NCBI_TOOL}"
        kind = "pubmed"
    else:
        m = _NCBI_PMC_RE.search(url or "")
        if not m:
            return "", ""
        q = f"{_EUTILS}?db=pmc&id={m.group(1)}&rettype=full&retmode=xml{_NCBI_TOOL}"
        kind = "pmc"
    try:
        req = _urlreq.Request(q, headers={"User-Agent": "Mozilla/5.0 (compatible; blog-sources/1.0)"})
        with _urlreq.urlopen(req, timeout=25) as r:
            raw = r.read().decode("utf-8", "replace")
    except Exception as e:
        print(f"[research] ncbi-api: {kind} {url[:70]} failed ({type(e).__name__})", flush=True)
        return "", ""
    if kind == "pmc":
        raw = re.sub(r"<(?:ref-list|back|front/journal-meta)\b.*?</(?:ref-list|back|journal-meta)>",
                     " ", raw, flags=re.S | re.I)
        raw = re.sub(r"</(?:p|title|sec|abstract|caption)>", "\n", raw, flags=re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = _html.unescape(raw)
    raw = re.sub(r"[ \t]{2,}", " ", re.sub(r"\n{3,}", "\n\n", raw)).strip()
    if len(raw) < 200:
        return "", ""
    print(f"[research] ncbi-api: {kind} read {len(raw)} chars for {url[:70]}", flush=True)
    return raw[:max_chars], "ncbi api"


# ── FU270: rank a study by what it IS, not by the domain that served it ─────────────────────────
# `_weak_study_design` reads a source's TITLE and spots only the weakest designs, and its own
# docstring says why: "Nothing in the repo ranked study design; this is the minimum that makes the
# weakest visible." Everything else about source quality is a domain allowlist.
#
# NCBI publishes the real thing. One batched call returns, per paper, its publication type and year:
#
#   41885859  2026  Journal Article, Randomized Controlled Trial   JAMA Network Open
#   26655941  2016  Journal Article, Meta-Analysis, Systematic Review
#   29537947  2018  Journal Article
#
# That is the standard evidence hierarchy, from the source itself. A 2016 systematic review and a
# 2009 single study are not interchangeable, and until now nothing in the pipeline could tell them
# apart — so the writer could not either.
_ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
# Ordered: the first pattern that matches wins, strongest first.
_STUDY_LEVELS = (
    (1, "systematic review", re.compile(r"meta[- ]analysis|systematic review", re.I)),
    (2, "randomised trial", re.compile(r"randomized controlled trial|randomised controlled trial", re.I)),
    (3, "clinical trial", re.compile(r"clinical trial|controlled trial", re.I)),
    (4, "observational study", re.compile(r"observational|cohort|case[- ]control|cross[- ]sectional", re.I)),
    (5, "practice guideline", re.compile(r"practice guideline|guideline", re.I)),
    (8, "editorial or letter", re.compile(r"editorial|comment\b|letter|news|biography|interview", re.I)),
    # A NARRATIVE review is closer to expert opinion than to a primary study — which is the same
    # judgement `_WEAK_DESIGN_RE` already makes by title. It sits BELOW an ordinary paper, and an
    # editorial below that. Getting this wrong is easy: the first version had the default at 8 and
    # editorials at 7, which ranked an editorial ABOVE a research article.
    (7, "review", re.compile(r"\breview\b", re.I)),
)
_STUDY_DEFAULT = (6, "journal article")


def _study_level(pubtypes):
    """(rank, plain name) for a paper's publication types. Lower rank is stronger evidence."""
    blob = ", ".join(str(p) for p in (pubtypes or []))
    for rank, name, rx in _STUDY_LEVELS:
        if rx.search(blob):
            return rank, name
    return _STUDY_DEFAULT


def ncbi_study_meta(pmids):
    """{pmid: {"level", "design", "year", "journal"}} for up to 50 papers, in ONE call.

    Batched on purpose: this is one HTTP request per article, not per source. Returns {} on any
    trouble — a ranking we could not fetch must never stop a generation."""
    ids = [str(p).strip() for p in (pmids or []) if str(p).strip().isdigit()][:50]
    if not ids:
        return {}
    q = f"{_ESUMMARY}?db=pubmed&retmode=json&id={','.join(ids)}{_NCBI_TOOL}"
    try:
        req = _urlreq.Request(q, headers={"User-Agent": "Mozilla/5.0 (compatible; blog-sources/1.0)"})
        with _urlreq.urlopen(req, timeout=25) as r:
            res = json.loads(r.read().decode("utf-8", "replace")).get("result") or {}
    except Exception as e:
        print(f"[research] ncbi-meta: {len(ids)} id(s) failed ({type(e).__name__})", flush=True)
        return {}
    out = {}
    for k in (res.get("uids") or []):
        rec = res.get(k) or {}
        rank, design = _study_level(rec.get("pubtype"))
        out[str(k)] = {"level": rank, "design": design,
                       "year": (str(rec.get("pubdate") or "")[:4] or ""),
                       "journal": str(rec.get("fulljournalname") or "")[:60]}
    return out


def read_page(url, claude=None, cache=None, max_chars=20000):
    """Step B for one URL: our fetch first (direct → residential), Anthropic's web fetch when the
    page is walled or errored (never for a missing page). Returns (text, how) where how is
    "direct", "web fetch", or the failure reason ("not-found", "blocked", ...)."""
    if cache is not None and url in cache:
        return cache[url]
    _nt, _nh = _ncbi_api_text(url, max_chars)      # FU267: NCBI answers its API, never a scraper
    if _nt:
        if cache is not None:
            cache[url] = (_nt, _nh)
        return _nt, _nh
    html, reason = _fetch_page(url, retries=0)
    if html:
        out = (page_text_from_html(html, max_chars=max_chars), "direct")
    elif claude is not None and reason in ("blocked", "error", "thin"):
        txt, code = claude.web_fetch_text(url)
        out = ((txt[:max_chars], "web fetch") if txt else ("", f"{reason}; web fetch {code}"))
    else:
        out = ("", reason)
    if cache is not None:
        cache[url] = out
    return out


# FU224 — page classes that must never be cited as evidence for a reader-facing claim, even on the
# brand's own site. An affiliate-registration or press page is written for recruiters and reporters,
# not buyers: the same fact sits on the product or policy page, and a source list handed to a client
# that cites "become an affiliate" reads as unserious. Matched as WHOLE path segments on purpose —
# a prefix match would take "/careers-in-medical-coding" and "/pressure-washers" with it.
_EXCLUDED_PAGE_RE = re.compile(
    r"/(affiliate|affiliates|affiliate-program|affiliate-programme|become-an-affiliate"
    r"|press|press-kit|press-kits|press-room|pressroom|press-release|press-releases"
    r"|newsroom|news-room|media-kit|media-kits"
    r"|investor|investors|investor-relations"
    r"|career|careers|job|jobs|hiring|work-with-us|join-our-team"
    r"|become-a-partner|partner-program|partner-programme|partner-with-us|reseller|resellers)"
    r"(?:/|$|\?|#)", re.I)


def page_class_excluded(url):
    """The excluded page class this URL belongs to, or "" when it is fine to read and cite.

    Deliberately NOT a quality judgement — it names page TYPES whose facts always live somewhere
    better on the same site. A product, pricing, plan, feature, policy, terms, shipping or support
    page is never matched; those are exactly the pages a claim should rest on."""
    path = re.sub(r"^https?://[^/]*", "", (url or "").strip(), flags=re.I)
    if not path.startswith("/"):
        path = "/" + path
    m = _EXCLUDED_PAGE_RE.search(path)
    return m.group(1).lower() if m else ""


def _url_key(u):
    """One comparison key for a page URL (case, trailing slash and #fragment ignored), so a page is
    never fetched twice because two searches spelled its link differently."""
    return (u or "").strip().lower().split("#")[0].rstrip("/")


def need_product(need, products):
    """FU223 — which compared product a need is about. `build_needs` writes the product name into
    the need text verbatim ("Pricing Structure for tirzepatide - ..."), so this reads it back.
    Longest match wins, so "semaglutide 2.5 mg" beats "semaglutide"."""
    n = norm(need)
    best = ""
    for p in (products or []):
        p = str(p or "").strip()
        if p and norm(p) in n and len(p) > len(best):
            best = p
    return best


# Words that never say WHICH product something is. Everything else a compared set has in common
# is removed per-set below, so this list stays tiny and vertical-neutral.
_PRODUCT_STOP = {"the", "a", "an", "and", "or", "for", "with", "our", "its", "of"}
_URL_MIN_TOKEN = 4   # a URL is noisy ("/2024/08/"), so only a distinctive word counts there


def _ptokens(s):
    """Identity tokens for a product NAME. Unlike `_tokens` (built for price matching) this keeps
    numbers and short words: "5.4 oz" versus "8.1 oz" IS the difference between two products."""
    return {t for t in re.findall(r"[a-z0-9]+", (s or "").lower()) if t not in _PRODUCT_STOP}


def _discriminators(products):
    """What actually tells the compared products APART: each one's tokens minus the ones they all
    share. "the 5.4 oz bottle" / "the 8.1 oz bottle" discriminate on {5,4} and {8,1}, not on
    "oz bottle"; "the Pro plan" / "the Business plan" on {pro} and {business}, not on "plan"."""
    toks = {p: _ptokens(p) for p in (products or []) if str(p or "").strip()}
    if len(toks) < 2:
        return {p: set() for p in toks}
    common = set.intersection(*toks.values())
    return {p: (t - common) for p, t in toks.items()}


def product_conflict(mine, fact, products):
    """FU223 — the OTHER compared product this fact is actually about, or "" when there is none.

    The audit's biggest cluster: one brand, two products, facts crossed between them — a
    tirzepatide plan given the separate subscription's price, its perks and its eight-state
    exclusion list, telling readers in two states they could not buy something they can. The quote
    gate cannot see this: the page IS the brand's own and the quote IS on it. Only the product is
    wrong.

    Deliberately narrow, because a rejection costs a real fact. It fires ONLY when the evidence
    (the product the extractor named, plus the page's own URL) points at a DIFFERENT product the
    article compares AND does not point at this need's product. A fact with no product named, a
    brand name for the same thing ("Zepbound" for tirzepatide), a page that mentions both, or any
    product outside the compared set is left alone — rejecting on ABSENCE would throw away correct
    facts from pages that simply do not repeat the name."""
    mine = (mine or "").strip()
    if not mine:
        return ""
    disc = _discriminators(products)
    my_d = disc.get(mine) or set()
    others = {p: d for p, d in disc.items() if norm(p) != norm(mine) and d}
    if not my_d or not others:
        return ""
    named = _ptokens((fact or {}).get("product") or "")
    # The URL is corroborating, not primary: a date path or an id can collide with a one-character
    # size token, so only a distinctive WORD in it counts.
    from_url = {t for t in _ptokens(re.sub(r"[^A-Za-z0-9]+", " ", (fact or {}).get("url") or ""))
                if len(t) >= _URL_MIN_TOKEN}
    seen = named | from_url
    if not seen or (seen & my_d):
        return ""                       # it names OUR product (or nothing) - never a conflict
    for o, d in others.items():
        if seen & d:
            return o                    # it names another compared product, and not ours
    return ""


def price_queries(brand, products=None, limit=3):
    """FU222 — the searches a BUYER would type for this brand's price, for the pinned page-finding
    call. The need text that drives the rest of the research is a comparison COLUMN HEADING
    ("Pricing Structure for X — the regular price with its unit, term or pack and any condition");
    a search engine matches a page titled "Pricing" to "<brand> pricing", not to that. Still pinned
    to the brand's own domain by the caller, so first-party sourcing is unchanged."""
    out, seen = [], set()
    b = str(brand or "").strip()
    if not b:
        return out
    for p in [str(x).strip() for x in (products or []) if str(x).strip()][:2] + [""]:
        q = f"{b} {p} price" if p else f"{b} pricing"
        k = norm(q)
        if k and k not in seen and len(out) < int(limit):
            seen.add(k)
            out.append(q)
    return out


def build_needs(dims=None, products=None, include_pricing=True, geo="", subject="", max_needs=8):
    """The questions to research for one brand, from what the ARTICLE uses: its comparison columns,
    a price need per compared product when pricing is on, availability for a geo page. No vertical
    words — the column names are the article's own."""
    needs, seen = [], set()

    def add(n):
        k = norm(n)
        if k and k not in seen and len(needs) < max_needs:
            seen.add(k)
            needs.append(n)

    price_col = False
    for d in (dims or []):
        d = str(d or "").strip()
        if not d:
            continue
        if is_price_need(d):
            price_col = True
            if not include_pricing:
                continue
            prods = [p for p in (products or []) if str(p).strip()][:2]
            for p in (prods or [""]):
                add(f"{d}" + (f" for {p}" if p else "")
                    + " — the regular price with its unit, term or pack and any condition")
            continue
        add(d + (f" (for {subject})" if subject else ""))
    if include_pricing and not price_col:
        for p in [p for p in (products or []) if str(p).strip()][:2] or [""]:
            add(("Price" + (f" of {p}" if p else " of the comparable offering"))
                + " — the regular price with its unit, term or pack and any condition")
    if geo:
        # FU223 — PER PRODUCT. Availability is not a brand fact: a brand's two products can have
        # different state, region and eligibility limits, and carrying one across to the other is
        # how a draft told readers in two states they could not buy something they can. Splitting
        # it is only safe because a fact about the other product is now refused (product_conflict).
        for p in [x for x in (products or []) if str(x).strip()][:2] or [""]:
            add(f"Availability or coverage in {geo}" + (f" for {p}" if p else "")
                + " — where it is offered and any eligibility or coverage limit that applies to it")
    return needs


def research_brand(claude, brand, domains, needs, context="", guidance="", page_cache=None,
                   max_pages=4, rounds=2, log=print, products=None, price_pages=3):
    """Research one brand's needs on its own site. Returns
        {"facts": [verified facts], "unconfirmed": [need, ...], "pages": {url: how}}
    where a verified fact is {"need", "answer", "url", "quote", "product", "verified_by"}
    (verified_by = "quote" or "price-on-page"). Never raises."""
    result = {"facts": [], "unconfirmed": list(needs or []), "pages": {}}
    doms = [d for d in (domains or []) if d]
    if claude is None or not doms or not needs:
        return result
    cache = page_cache if page_cache is not None else {}
    pending = list(needs)
    read_urls = set()
    try:
        for rnd in range(max(1, int(rounds))):
            if not pending:
                break
            # FU222: the PRICE question gets its own pinned search. Asked together with the other
            # columns it competed for the same handful of page slots and lost — a product page that
            # happens to show one figure answered it, and the page actually titled "Pricing" was
            # never read. Its own search, phrased the way a buyer types it, reaches that page.
            _price_pend = [n for n in pending if is_price_need(n)]
            _other_pend = [n for n in pending if not is_price_need(n)]
            _searches = 3 if rnd == 0 else 2
            _found = []
            if _price_pend:
                _found += claude.find_pages(brand, doms, _price_pend, context=context,
                                            max_searches=2 if rnd == 0 else 1,
                                            max_pages=price_pages,
                                            queries=price_queries(brand, products))
            if _other_pend:
                _found += claude.find_pages(brand, doms, _other_pend, context=context,
                                            max_searches=_searches, max_pages=max_pages)
            urls, _seen_u = [], set()
            for u in _found:
                k = _url_key(u)
                if not k or k in _seen_u or k in read_urls:
                    continue
                _cls = page_class_excluded(u)
                if _cls:   # FU224: never cite a recruitment / press / investor page to a reader
                    log(f"[research] {brand}: skipped a {_cls} page — {u[:70]}")
                    _seen_u.add(k)
                    continue
                _seen_u.add(k)
                urls.append(u)
            pages = {}
            for u in urls:
                read_urls.add(_url_key(u))
                txt, how = read_page(u, claude=claude, cache=cache)
                result["pages"][u] = how if txt else f"unread ({how})"
                if txt:
                    pages[u] = txt
            if not pages:
                continue
            facts = claude.extract_facts(brand, pending, pages, context=context, guidance=guidance)
            still = []
            for i, need in enumerate(pending, start=1):
                # FU222: EVERY answer offered for this need, not just the first. A price page states
                # a ladder ("$39 for the first month, then $149 a month"); keeping one figure threw
                # the rest away. Each candidate still stands or falls on its OWN quote, so nothing
                # reaches a cell that is not on the page.
                kept, seen_ans = [], set()
                _np = need_product(need, products)
                for f in [x for x in facts if x.get("need") == i]:
                    if len(kept) >= _MAX_FACTS_PER_NEED:
                        break
                    ans = (f or {}).get("answer", "")
                    if not ans or "not found" in ans.lower():
                        continue
                    key = norm(ans)
                    if not key or key in seen_ans:
                        continue
                    page = pages.get(f.get("url")) or ""
                    if not page:   # the model named a url we did not give it — take the page with it
                        for pu, pt in pages.items():
                            if quote_on_page(f.get("quote"), pt):
                                f["url"], page = pu, pt
                                break
                    _other = product_conflict(_np, f, products)
                    if _other:
                        log(f"[research] {brand}: rejected '{ans[:50]}' — it is about {_other}, "
                            f"not {_np}")
                        continue
                    if is_price_need(need) and (figure_is_sale(ans, f.get("quote") or "")
                                                or figure_is_sale(ans, page)):
                        log(f"[research] {brand}: rejected '{ans[:60]}' — a SALE price, not the "
                            f"regular one")
                        continue
                    how = ""
                    if page and quote_on_page(f.get("quote"), page):
                        how = "quote"
                    elif (page and is_price_need(need)
                          and price_on_page(ans, f.get("product") or brand, page)):
                        how = "price-on-page"
                    if how:
                        seen_ans.add(key)
                        _extra = {}
                        if is_price_need(need):
                            # FU225: the page's own scope word, when the answer dropped it
                            _sc = (scope_on_page(ans, f.get("quote") or "")
                                   or scope_on_page(ans, page))
                            if _sc and not _FROM_RE.search(ans):
                                _extra["scope"] = _sc
                                log(f"[research] {brand}: '{ans[:34]}' is a {_sc.upper()} price on "
                                    f"the page — the answer had dropped that")
                        _bad = unsupported_basis_clauses(f.get("basis"), page)
                        if _bad:
                            _extra["basis"] = ", ".join(c for c in split_clauses(f.get("basis"))
                                                        if c not in _bad)
                            log(f"[research] {brand}: dropped unsupported condition(s) "
                                f"{_bad} — no such number on {(f.get('url') or '?')[-44:]}")
                        kept.append(dict(f, need=need, verified_by=how, **_extra))
                    else:
                        log(f"[research] {brand}: unverified '{need[:50]}' — quote not on "
                            f"{(f.get('url') or '?')[:70]}")
                if kept:
                    result["facts"].extend(kept)
                    if len(kept) > 1:
                        log(f"[research] {brand}: '{need[:40]}' — kept {len(kept)} verified figures")
                else:
                    still.append(need)
            pending = still
    except Exception as e:   # research must never break a generation
        log(f"[research] {brand}: error {e}")
    result["unconfirmed"] = pending
    log(f"[research] {brand}: {len(needs) - len(pending)}/{len(needs)} need(s) verified "
        f"({len(result['facts'])} fact(s)) from {len(result['pages'])} page(s)" + (f"; unconfirmed: {', '.join(n[:40] for n in pending)}"
                                               if pending else ""))
    return result


def facts_to_blocks(label, facts):
    """Evidence blocks from verified facts: one block per page, carrying the quoted facts (not the
    page dump), labelled as the brand's own site so the writer cites them first-party."""
    by_url = {}
    for f in facts or []:
        u = f.get("url") or ""
        if not u:
            continue
        line = f"{f.get('need', '').split(' — ')[0]}: {f.get('answer', '')}"
        if f.get("product"):
            line += f" [{f['product']}]"
        if f.get("quote"):
            line += f' — the page says: "{f["quote"]}"'
        by_url.setdefault(u, []).append(line)
    return [{"label": label, "url": u, "text": "\n".join(lines)} for u, lines in by_url.items()]


_FROM_RE = re.compile(r"\b(from|starts? at|starting at|as low as|beginning at)\b", re.I)


def _trim_basis(b, cap=120):
    """Cap a basis at a CLAUSE boundary. Two faults this fixes, both seen live: a hard slice cut a
    cell mid-word ("...with manufacturer offer, cas"), and an 80-char cap dropped a real condition
    ("membership required") off the end of the longest rung — which then made the shared-condition
    line understate what the page said. Ladder rungs carry richer conditions, and the shared ones
    are factored out when the cell is printed, so the extra room costs nothing on screen."""
    b = (b or "").strip()
    if len(b) <= cap:
        return b
    cut = b[:cap]
    for sep in (", ", " "):
        i = cut.rfind(sep)
        if i >= cap // 2:
            return cut[:i].rstrip(" ,;")
    return cut.rstrip(" ,;")


def price_ladder(facts, checked_at=""):
    """FU222 — every verified price fact for ONE brand as a SINGLE ledger entry that carries the
    whole ladder. `value`/`basis`/`url` stay the first figure, so every existing consumer (the
    per-unit price, the cadence gate, the JSON-LD offer, the prose checks) reads exactly what it
    read before; the rest ride in `tiers`, which only `_format_price_value` looks at. Accepts one
    fact or a list. None when nothing carries a figure."""
    if isinstance(facts, dict):
        facts = [facts]
    entries, seen = [], set()
    for f in (facts or []):
        e = price_entry(f, checked_at=checked_at)
        if not e:
            continue
        e["_product"] = ((f or {}).get("product") or "").strip()
        # Live (Mubert): the model returned "$39/mo (billed annually: $32.49/mo)" AND "$39/mo
        # (monthly billing)" for the same plan, so the cell showed $39 twice with contradictory
        # conditions. One figure for one plan is ONE rung; keep the plainest basis, because the
        # longer one is claiming a condition this figure does not actually carry.
        key = (e["value"], norm(e["_product"]))
        if key in seen:
            _prev = next(x for x in entries if (x["value"], norm(x["_product"])) == key)
            if len(e.get("basis") or "") < len(_prev.get("basis") or ""):
                entries[entries.index(_prev)] = e
            continue
        seen.add(key)
        entries.append(e)
    if not entries:
        return None
    # FU251 — a ladder is ONE page's price list. `tiers` used to merge whatever verified facts came
    # back for this brand, from whatever pages, and the cell is written with a single code-written
    # [S#] pointing at the FIRST rung's page. So "Noom Med · $279 (billed as $837 for 12 weeks);
    # $129 (first 4-week supply); $249 (after the first supply, billed quarterly)" cited one page
    # for three figures that came off three. A rung from another page is not a rung — it is a
    # different fact, and it keeps its own entry rather than borrowing someone else's citation.
    _home = (entries[0].get("url") or "").strip()
    _offpage = [e for e in entries[1:] if (e.get("url") or "").strip() != _home]
    if _offpage:
        print(f"[research] price ladder: {len(_offpage)} rung(s) came from a different page than "
              f"{_home[:70]} — not joined into one cell", flush=True)
    entries = [entries[0]] + [e for e in entries[1:] if (e.get("url") or "").strip() == _home]
    # …and prices ONE thing. The same figure for two DIFFERENT plans is two real rungs ($10 Starter,
    # $10 Team), so only a repeat with nothing to tell it apart is dropped — which is what the
    # product-name basis used to hide: "$25 (per month, with commercial insurance)" and a second
    # "$25" whose only distinguishing mark was a product name printed where the unit goes.
    _seen_val, _uniq = set(), []
    for e in entries:
        if e["value"] in _seen_val and not (e.get("basis") or "").strip() \
                and not (e.get("_product") or "").strip():
            continue
        _seen_val.add(e["value"])
        _uniq.append(e)
    entries = _uniq
    rungs = entries[:_MAX_TIERS]
    # Live (Mubert): three rungs came back for three DIFFERENT plans and printed as bare figures,
    # so the cell mixed tiers without saying which was which. When the rungs name different
    # plans/products, that name leads the rung; when they are all one product it adds nothing.
    _prods = {norm(e["_product"]) for e in rungs if e["_product"]}
    # A plan NAME labels a rung usefully ("Pro", "Business", "Compounded Semaglutide"). A retail
    # SKU TITLE does not — live, Pigeon returned "PPSU Wide Neck Baby Bottle for Newborns 2 Packs,
    # 5.4 Oz" as the product, which restated the basis and made the cell unreadable. So a product
    # only labels a rung when it is short and is not already saying what the basis says.
    _named = (len(_prods) > 1 and all(e["_product"] for e in rungs)
              and all(len(e["_product"]) <= _MAX_RUNG_LABEL for e in rungs)
              and not any(norm(e["_product"]) in norm(e.get("basis") or "")
                          or norm(e.get("basis") or "") in norm(e["_product"]) for e in rungs))
    base = dict(entries[0])
    base.pop("_product", None)
    if len(rungs) > 1:
        base["tiers"] = [{"value": e["value"],
                          "basis": _trim_basis(", ".join(x for x in (e["_product"] if _named else "",
                                                                     e.get("basis") or "") if x)),
                          "kind": e.get("kind") or "exact", "url": e.get("url") or "",
                          "quote": e.get("quote") or ""} for e in rungs]
    return base


def price_entry(fact, checked_at=""):
    """A verified research price as an FU213 ledger entry — the shape the comparison cells are written
    from. The value is the answer's first figure; `basis` carries the unit / term / pack and conditions
    the page attached ("per month, membership required"). source "own": research is pinned to the
    brand's own site. None when the fact carries no figure."""
    figs = _figures((fact or {}).get("answer"))
    if not figs or not (fact or {}).get("url"):
        return None
    val = figs[0]
    # FU251: NEVER fall back to the product name. `_format_price_value` prints a basis as the unit
    # parenthetical after the figure, so an empty basis came out as "$25 (Calibrate Metabolic
    # Reset)" — which reads as a price whose unit is a product name. An unknown basis is empty; the
    # product labels its RUNG instead, where it says something.
    basis = _trim_basis((fact.get("basis") or "").strip())
    _scope = str(fact.get("scope") or "").strip().lower()      # FU225: the PAGE's scope word
    _kind = ("from" if (_FROM_RE.search(fact["answer"]) or _scope == "from")
             else ("upto" if _scope == "upto" else "exact"))
    return {"value": val, "value_max": "", "kind": _kind,
            "basis": basis, "per_unit": "", "url": fact["url"], "source": "own",
            "quote": re.sub(r"\s+", " ", fact.get("quote") or "")[:220],
            "checked_at": checked_at, "via": "research"}
