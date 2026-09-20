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

import json
import re
import unicodedata

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


def is_price_need(need):
    return bool(PRICE_NEED_RE.search(need or ""))


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


def read_page(url, claude=None, cache=None, max_chars=20000):
    """Step B for one URL: our fetch first (direct → residential), Anthropic's web fetch when the
    page is walled or errored (never for a missing page). Returns (text, how) where how is
    "direct", "web fetch", or the failure reason ("not-found", "blocked", ...)."""
    if cache is not None and url in cache:
        return cache[url]
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


def _url_key(u):
    """One comparison key for a page URL (case, trailing slash and #fragment ignored), so a page is
    never fetched twice because two searches spelled its link differently."""
    return (u or "").strip().lower().split("#")[0].rstrip("/")


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


def build_needs(dims=None, products=None, include_pricing=True, geo="", subject="", max_needs=6):
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
        add(f"Availability or coverage in {geo}")
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
                if k and k not in _seen_u and k not in read_urls:
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
                        kept.append(dict(f, need=need, verified_by=how))
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
    basis = _trim_basis((fact.get("basis") or "").strip() or (fact.get("product") or "").strip())
    return {"value": val, "value_max": "", "kind": "from" if _FROM_RE.search(fact["answer"]) else "exact",
            "basis": basis, "per_unit": "", "url": fact["url"], "source": "own",
            "quote": re.sub(r"\s+", " ", fact.get("quote") or "")[:220],
            "checked_at": checked_at, "via": "research"}
