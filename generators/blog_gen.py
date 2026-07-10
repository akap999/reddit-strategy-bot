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

from generators.post_gen import PostGenerator
from generators.brand_enrichment import _fetch_homepage, _extract_visible_text

PROMPT_VERSION = "blog-v2-evidence"

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
    "g2.com", "capterra.com", "trustpilot.com", "getapp.com", "trustradius.com",
    "softwareadvice.com", "producthunt.com", "gartner.com", "forrester.com",
    "techcrunch.com", "theverge.com", "forbes.com", "businessinsider.com",
    "reuters.com", "crunchbase.com", "wikipedia.org",
]
_MAX_WEB_SOURCES = 5             # FU56: cap independent third-party sources folded in (was 8 — cost)
_VERIFY_MAX_SEARCHES = 5         # FU56: cap on deep independent re-check web searches (was 8 — cost)
_VERIFY_MAX_BRANDS = 4           # FU56: cap on competitor tools sourced per article (was 6 — cost)
_MAX_TOOL_PAGES = 2              # FU52: cap on distinct per-tool source PAGES (deep-linked citations)
_FACT_RESCUE_TRIES = 3          # FU78: broad-search retries to fetch a vendor's MISSING key facts (pricing,
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
_LICENSE_SIGNAL_RE = re.compile(
    r"\b(commercial(?:ly|[- ]use)?|licen[sc]e[ds]?|royalty[- ]free|copyright|monetiz\w*|own\s+the\s+(?:output|rights))\b",
    re.IGNORECASE)
_FACT_SIGNALS = {"price": _PRICE_SIGNAL_RE, "license": _LICENSE_SIGNAL_RE}

# FU56/FU78: hard per-generation cost ceiling ($). Once the running cost hits this, further web searches are
# skipped (search result tokens are ~90% of a blog's cost). Bumped 1.5→2.0 (FU78) to leave headroom for the
# price-rescue searches so public pricing gets fetched rather than punted. Env-overridable.
_BLOG_COST_CEILING = float(os.environ.get("BLOG_COST_CEILING", "2.0"))
# FU56: the LOW-priority independent-source sweep runs in _gather_evidence FIRST. Cap that stage to a
# FRACTION of the budget so it can't starve the higher-priority official/vendor searches that come later —
# i.e. plan + prioritize instead of a first-come cutoff. The remainder is reserved for verify_and_complete.
_CEIL_EVIDENCE = round(_BLOG_COST_CEILING * 0.4, 2)

# FU54: stale SaaS-listing / aggregator domains whose pricing lags the vendor. Downranked BELOW the
# vendor's OWN site and reputable reviews for a price/license cell (a competitor sourced only from one of
# these read as stale in v8, e.g. SaaSworthy's Beatoven price).
_STALE_AGGREGATORS = {
    "saasworthy.com", "softwarefinder.com", "eesel.ai", "softwaresuggest.com",
    "goodfirms.co", "sourceforge.net", "slashdot.org", "toolify.ai", "futurepedia.io",
}


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


def _norm_domain(u):
    """Registrable-ish domain from a url/bare domain: strip scheme + path + a leading 'www.' so a
    www / non-www variant compares equal (FU54). Module-level so it's unit-testable."""
    u = re.sub(r"^https?://", "", (u or "").strip()).rstrip("/")
    d = u.split("/")[0].lower()
    return d[4:] if d.startswith("www.") else d


class BlogGenerator:
    def __init__(self, claude, db):
        self.claude = claude
        self.db = db
        self._evidence_blocks = []   # set by _gather_evidence; read by _rebuild_sources
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
        lines = [f"Brand name: {name}"]
        if url:
            lines.append(f"Website: {url}")
        if b.get("category"):
            lines.append(f"Category: {b['category']}")
        if b.get("audience"):
            lines.append(f"Audience: {b['audience']}")
        for label, key in (("Use cases", "use_cases"), ("Pain points", "pain_points"),
                           ("Features", "features"), ("Competitors", "competitors")):
            vals = _as_list(b.get(key))
            if vals:
                lines.append(f"{label}: {', '.join(vals)}")
        if b.get("context"):
            lines.append(f"Context: {b['context']}")
        if b.get("learned_context"):
            lines.append(f"Learned context: {b['learned_context']}")
        return name, url, "\n".join(lines)

    def _byline_md(self, brand):
        """EEAT byline + disclosure block (Markdown), or "" when the brand supplies none.
        NEVER fabricated — rendered only from brand-supplied fields."""
        b = brand or {}
        bits = []
        au = (b.get("author_name") or "").strip()
        if au:
            at = (b.get("author_title") or "").strip()
            bits.append(f"By {au}" + (f", {at}" if at else ""))
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
    def _resolve_brand_domains(self, names, seed=None, subject=None, subject_category=None):
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
        prompt = ("For each brand/product below, give its official homepage domain (bare, "
                  "no https://, no path). Include ONLY ones you are confident about; omit "
                  "the rest.\n" + body + ctx +
                  '\n\nReturn JSON only: {"domains": {"Name": "domain.com"}}')
        res = self.claude.call(prompt, max_tokens=400, temperature=0)
        dm = (res or {}).get("domains") if isinstance(res, dict) else None
        out = {}
        if isinstance(dm, dict):
            for n, d in dm.items():
                d = re.sub(r"^https?://", "", str(d or "").strip().lower()).rstrip("/").split("/")[0]
                if str(n).strip() and d:
                    out[str(n).strip()] = d
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
        brands = [n for n in ([subject] + list(competitors or [])[:1]) if (n or "").strip()]
        angles = [   # FU56: 2 angles (was 3) — fewer searches per brand
            "independent user REVIEWS and ratings (e.g. G2, Capterra, Trustpilot, TrustRadius)",
            "NEWS / funding / analyst coverage OR third-party PRICING & commercial-license / terms references "
            "(e.g. TechCrunch, Reuters, Forbes, Crunchbase, G2)",
        ]

        def _take(srcs):
            for s in (srcs or []):
                url = (s.get("url") or "").strip()
                fact = (s.get("fact") or s.get("title") or "").strip()
                if not url or not fact:
                    continue
                key = url.lower().split("?")[0].rstrip("/")
                if key in seen:
                    continue
                seen.add(key)
                out.append({"label": f"third-party · {s.get('title') or url}",
                            "url": url, "text": fact[:_EVIDENCE_TEXT_CAP]})

        for nm in brands:
            for ang in angles:
                if len(out) >= _MAX_WEB_SOURCES:
                    break
                brief = (f'Find {ang} about "{nm}"' + (f' ({cat})' if cat else "")
                         + f'. Topic: {seed}. Prefer recent (2024-2025) coverage. It MUST be an '
                           "INDEPENDENT third-party page, NOT the brand's own website.")
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
            txt = _extract_visible_text(_fetch_homepage(url))
            return (txt or "").strip()

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
            to_resolve, seed=seed, subject=subject, subject_category=b.get("category"))
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
        for label, dom, validate in targets:
            dom = re.sub(r"^https?://", "", dom).rstrip("/")
            target_dom[label] = dom
            # Subject brand gets the full path set; competitors get the product-heavy set.
            paths = _EVIDENCE_PATHS if not validate else _comp_paths
            kept = 0
            for path in paths:
                txt = _fetch(f"https://{dom}{path}")
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
                blocks.append({"label": "provided source", "url": u,
                               "text": txt[:_EVIDENCE_TEXT_CAP]})

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

        # Stash the structured blocks (in [S#] order) so _rebuild_sources can rebuild the
        # article's ## Sources authoritatively. Always set (even when empty) so a stale value
        # from a prior call on this instance can't leak in.
        self._evidence_blocks = list(blocks)
        if not blocks:
            return ""
        parts = ["EVIDENCE (the ONLY admissible support for factual claims — cite by [S#] and URL):"]
        for i, bl in enumerate(blocks, 1):
            src = f"{bl['label']}" + (f" — {bl['url']}" if bl["url"] else "")
            parts.append(f"[S{i}] {src}\n{bl['text']}")
        return "\n\n".join(parts)

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
    _STAT_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?%?")

    # FU55: the model narrating its OWN sourcing/editing decisions into the article — a broken comparison
    # row or an "addressed elsewhere" note. Never real content; scrubbed from the final body.
    _META_RE = re.compile(
        r"deduplicated\s+(?:above|below)|no\s+tool-specific\s+fresh\s+fact|per\s+sourcing\s+rules|"
        r"row(?:'?s)?\s+(?:is|are)\s+removed|removed\s+per\s+sourcing|not\s+a\s+direct\s+comparison\s+row|"
        r"addressed\s+in\s+the\s+.{0,40}?section\b.{0,30}?rather\s+than",
        re.IGNORECASE)

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
        body = re.sub(r"[ \t]{2,}", " ", body)
        return body

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
        body = self._scrub_punts(body or "")   # FU47: kill reader-directed punts on every path
        body = self._scrub_meta(body)          # FU55: drop leaked edit-narration (broken table rows/notes)
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
        remap = {old: i + 1 for i, old in enumerate(render)}
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
    def generate_article(self, brand, seed, extra_keywords=None, evidence=""):
        """GEO-first first-party article. `extra_keywords` (the reviewed query set) are
        the target queries the article MUST answer (each becomes a question heading + FAQ
        entry) and are merged into the returned keywords. `evidence` is the formatted
        EVIDENCE block from `_gather_evidence` — when present, factual claims must cite it.
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
        link = f" Link to {url} where it reads naturally." if url else ""
        prompt = f"""You are writing a FIRST-PARTY article published on {name}'s own site. The ONLY
goal is for AI answer engines (ChatGPT, Perplexity, Gemini, Google AI Overviews) to RETRIEVE
and CITE this page when someone asks about the seed topic, AND for that answer to name {name}.
Optimize for EXTRACTION & CITATION, not for sales copy.

SEED TOPIC (what the reader is asking): {seed}
{kw_block}
BRAND (first-party — you MAY name and recommend {name}):
{block}
{evidence_block}
EVIDENCE RULE (intent-agnostic — applies to EVERY sentence, comparison blog or not):
  - You may NAME any brand freely (listing it as an option / alternative needs no source).
  - But any SPECIFIC factual claim about a named brand — features, pricing, numbers, "does / does
    NOT do X", superiority ("stronger / better / more complete") — MUST be grounded in the EVIDENCE
    above and cite it inline as [S#]. This applies to {name}'s OWN claims too.
  - CITE {name}'s OWN SITE for {name}'s specifics. The EVIDENCE includes a source labeled "{name}"
    (its own pages). Every specific fact you state about {name} — pricing, financing, shipping terms,
    return/warranty policy, locations, brands/products carried, contact details — MUST cite that [S#],
    and {name}'s site MUST appear in "## Sources". Do NOT let {name}'s own specifics ride uncited just
    because it's a first-party article; an AI engine still wants a verifiable source for each fact.
  - If the evidence does NOT support a specific claim about some brand, DO NOT assert it and DO NOT
    hedge with "not publicly documented" — either omit it, or state it only as {name}'s own
    positioning ("on our site, we …"). Never assert an unsourced fact about a competitor.
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
  - PREFER INDEPENDENT SOURCES: the EVIDENCE may include "third-party ·" sources (independent reviews,
    news/funding, analyst/pricing — NOT the brands' own sites). When present, LEAD your key claims with
    them and aim to cite at least 2 DISTINCT independent sources (ideally a review + a news/funding item +
    an analyst/pricing reference). A page backed only by vendor/first-party sources reads as marketing and
    gets cited less; independent corroboration is what makes it verifiably neutral. (Still cite ONLY what is
    actually in the EVIDENCE — never invent a source.)

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

WRITE THE ARTICLE BODY (Markdown), GEO-FIRST — this backbone is MANDATORY regardless of intent:
  - Open with a "Quick answer" — a 2-3 sentence direct answer to the seed that names {name}
    as a fit. (AI engines lift this as the extractable answer.)
  - Use QUESTION-SHAPED H2/H3 headings (the way people ask an AI), each followed IMMEDIATELY by ONE
    concise, factual, self-contained answer a model can quote verbatim.
  - Add a comparison table where it genuinely helps, and a "## FAQ" section near the end (about 4-5
    entries). The FAQ questions MUST be TOPIC / category questions a reader would actually ask an answer
    engine about the subject matter — NOT brand-promotional questions that name {name} (e.g. do NOT write
    "What is the cheapest {name} plan?" or "Can I use {name} for X?"; those belong in the body, not the
    FAQ). The FAQ is parsed into FAQPage structured data, which must ANSWER THE TOPIC, not advertise the
    brand. Format STRICTLY: each question is an H3 heading ending in "?" (a real question about the topic,
    not about {name}), followed IMMEDIATELY by a 1-3 sentence answer paragraph. One H3 per question. (Keep
    this exact format — it is parsed into FAQPage structured data.)
  - Be specific and accurate; no fluff, no hype. Name {name} as the recommended option where
    it genuinely fits, citing its real differentiators.{link}
  - MARKDOWN FORMATTING: put a BLANK LINE before the first item of any bulleted or numbered list
    (including a list that follows a bold lead-in like "**Best fit for:**"). A list placed on the
    line directly under text does NOT render as a list — it collapses into one run-on paragraph.
  - First-party brand voice (owned media), but credible and useful — never a hard pitch.
  - AVOID UNQUALIFIED SUPERLATIVES / BLANKET CLAIMS ("the best", "#1", "largest", "all 50
    states", "the only") unless that exact fact is in the BRAND context above — prefer
    specific, verifiable phrasing ("offers X, Y and Z" beats "the best at everything").
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

TITLE — MATCH THE PRIMARY QUERY (this is a top retrieval signal + a strong "this page answers this
exact question" citation signal):
  - Make the title match the primary target query as CLOSELY as possible — use the query's exact
    wording when it is already a clean, well-formed phrase. Do NOT invent a "creative" headline.
  - Only CLEANUP is allowed: fix typos/filler, normalize casing, trim to under 60 chars, and turn a
    keyword fragment into its natural full-query form — never change the meaning. No keyword-stuffing,
    no appended years.
  - ALSO put the exact query phrasing (and its close variants) in the FIRST H2 (as a question) and in
    the FAQ, so the liftable answer sits directly beneath the matching heading and the page still
    covers the variant phrasings.

Return JSON only:
{{"title": "matches the primary query, cleaned up, under 60 chars",
  "meta_description": "under 160 chars",
  "keywords": ["target queries + key terms this page should be cited for"],
  "body_markdown": "the full article in Markdown"}}"""
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
        byline = self._byline_md(brand)        # "" unless the brand supplies author/reviewer/disclosure
        if byline and body:
            body = byline + body
        return {
            "title": (res.get("title") or "").strip(),
            "meta_description": (res.get("meta_description") or "").strip(),
            "keywords": merged,
            "body_markdown": body,
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
  - CERTIFICATIONS / accreditations (e.g. "LegitScript certified") — keep ONLY if in context.
  - SUPERLATIVES & BLANKET-COVERAGE claims ("largest", "best", "#1", "the only", "all 50
    states") — drop or qualify unless explicitly supported by the context.
  - COMPETITOR-NEGATIVE claims (asserting a NAMED competitor lacks a feature) — never assert a
    bald negative about a named third party; soften to a {name}-strength framing or
    date/attribute it ("as of publication").
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

    def verify_and_complete(self, brand, seed, article, deep=False):
        """FU49 — the always-on VERIFY + COMPLETE agent. After the draft is written it:
          (a) extracts the comparison TOOLS + DIMENSIONS + high-risk claims;
          (b) SOURCES every named competitor tool by pulling its OWN public facts (pricing / license /
              royalty-free / capability) — `find_official_domain` + `fetch_site_facts` (web-search pinned
              to that tool's domain, so it works on a cloud IP). Vendor sites are ALLOWED: this is the
              publicly-available info that fills the table;
          (c) runs an INDEPENDENT corroboration search for the SUBJECT's own self-claims + the risk/policy
              narrative, blocking ONLY the subject's own domain + already-used third-party URLs (NOT
              competitors);
          (d) reconciles: FILLS every comparison cell with a verified, cited value, KEEPS/EXPANDS the
              dimensions, corrects wrong values, and NEVER leaves "—" (drop a whole column / remove a tool
              rather than show a blank). Never fabricates — a cell is filled only from a fetched/cited fact.
        Fresh sources are appended to self._evidence_blocks in [S#] order for _rebuild_sources. `deep`
        bumps the corroboration search budget. Returns {body_markdown, flagged} or None (draft unchanged).
        Never raises."""
        name, _url, _block = self._brand_block(brand)
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
  - DIMENSIONS: the comparison columns / attributes being compared (e.g. pricing, commercial license,
    royalty-free, imitates real artists, all-in-one).
  - CLAIMS: the HIGH-RISK factual claims (comparison-table cells, competitor claims, any number / price /
    plan / license term, superlatives) — each with the brand it's about, the dimension, and the value.
  - CORE_TOPIC: the article's CENTRAL subject AND the authority that officially documents it — a short
    phrase naming the platform / regulator / standard whose OWN page is the highest-authority source
    (e.g. "TikTok — synthetic-media / AI-content labeling / monetization policy", "FDA guidance on <X>",
    "GDPR data-retention rules"). Empty if the article has no such external authority.

ARTICLE:
{body[:6000]}

Return JSON only: {{"tools": ["..."], "dimensions": ["..."], "core_topic": "",
  "claims": [{{"brand": "", "dimension": "", "claim": "", "value": ""}}]}}"""
        cres = self.claude.call(claim_prompt, max_tokens=1500, temperature=0.2)
        cres = cres if isinstance(cres, dict) else {}
        core_topic = str(cres.get("core_topic") or "").strip()
        tools = [str(t).strip() for t in (cres.get("tools") or [])
                 if str(t).strip() and str(t).strip().lower() != name.lower()]
        dims = [str(d).strip() for d in (cres.get("dimensions") or []) if str(d).strip()]
        claims = [c for c in (cres.get("claims") or []) if isinstance(c, dict)]
        # de-dupe tools (case-insensitive), cap
        seen_t, tools_u = set(), []
        for t in tools:
            if t.lower() not in seen_t:
                seen_t.add(t.lower()); tools_u.append(t)
        tools = tools_u[:_VERIFY_MAX_BRANDS]
        if not tools and not claims:
            return None  # nothing to source or check

        fresh = []   # each: {"label","url","text"} — appended to _evidence_blocks in [S#] order

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
        for c in (prim or [])[:2]:
            u = (c.get("url") or "").strip()
            fct = (c.get("fact") or c.get("title") or "").strip()
            if u and fct:
                fresh.append({"label": f"official · {c.get('title') or u}", "url": u,
                              "text": fct[:_EVIDENCE_TEXT_CAP]})

        # (b) SOURCE each named competitor tool from its OWN public pages (vendor domains ALLOWED)
        ctx = f"{cat} {seed}".strip()
        fetch_brief = ("pricing and plans; commercial-use & license terms (is monetization/commercial use "
                       "allowed, royalty-free or not); whether it imitates or clones real artists' voices; "
                       "video / all-in-one capability; key features")
        for tool in tools:
            try:
                dom = self.claude.find_official_domain(tool, ctx)
            except Exception:
                dom = ""
            # FU54: when the resolver fails, recover the tool's OWN domain from a web search so the
            # vendor tiers can still run. Competitors depend on this — the SUBJECT sources from its known
            # domain_url, but a competitor with no resolved domain skips Tier 1+2 and falls to a review.
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

            def _same_site(u, dom=dom):
                """True when url u is the tool's OWN site (its registrable domain or a subdomain)."""
                dd = _dom(dom)
                du = _dom(u)
                return bool(dd) and du and (du == dd or du.endswith("." + dd))

            def _blocks_from(srcs, cap=_MAX_TOOL_PAGES, tool=tool):
                """Per-page blocks with HONEST labels (FU53): the tool's OWN domain → first-party (tool
                name); any other domain → 'third-party · <title>' — a review is NEVER labeled as the vendor.
                Uses each result's ACTUAL url so citations deep-link. Deduped by url."""
                out_, seen_ = [], set()
                for s in (srcs or []):
                    u = (s.get("url") or "").strip()
                    fc = (s.get("fact") or s.get("title") or "").strip()
                    key = u.lower().split("?")[0].rstrip("/")
                    if not (u and fc) or key in seen_:
                        continue
                    seen_.add(key)
                    label = tool if _same_site(u) else \
                        f"third-party · {(s.get('title') or _dom(u) or 'review')}"
                    out_.append({"label": label, "url": u, "text": fc[:_EVIDENCE_TEXT_CAP]})
                    if len(out_) >= cap:
                        break
                return out_

            def _has_vendor(blocks):
                return any(_same_site(b["url"]) for b in (blocks or []))

            tool_blocks = []
            n1 = n3 = 0            # FU54: per-tier hit counts for the diagnostic log line
            t2_added = False
            # Tier 1 — web search PINNED to the tool's OWN registrable domain → SPECIFIC pages
            # (pricing/terms) WITH their urls → cite the exact vendor page, not a review.
            if dom:
                try:
                    pin = self.claude.search_sources(
                        f"{tool}: pricing and plans, commercial-use / licensing / royalty-free terms, "
                        f"key capabilities", max_searches=2, allowed_domains=[_dom(dom)],
                        first_party=True)   # FU55: vendor's OWN pages — don't tell it to avoid the vendor
                except Exception:
                    pin = []
                tool_blocks = _blocks_from(pin)
                n1 = len(tool_blocks)
            # Tier 2 — fetch_site_facts (the IP-INDEPENDENT web-search-pinned vendor path — the same one
            # the SUBJECT uses reliably) whenever Tier 1 gave NO vendor page. Together they source the
            # vendor reliably even on a cloud IP.
            if dom and not _has_vendor(tool_blocks):
                try:
                    fj = self.claude.fetch_site_facts(dom, tool, fetch_brief, max_searches=2) or ""
                except Exception:
                    fj = ""
                if fj.strip():
                    tool_blocks.append({"label": tool, "url": f"https://{_dom(dom)}",
                                        "text": fj[:_EVIDENCE_TEXT_CAP]})
                    t2_added = True
            # Tier 3 — ONLY when no VENDOR block exists. Prefer the tool's own domain, then a REPUTABLE
            # review (G2/Capterra/… — _THIRD_PARTY_DOMAINS); a stale SaaS aggregator or random blog is the
            # last resort and is honestly labeled 'third-party ·' (never presented as the vendor).
            if not _has_vendor(tool_blocks):
                try:
                    br = self.claude.search_sources(
                        f"{tool} ({cat}) official pricing and plans, commercial-use / licensing / "
                        f"royalty-free terms, key capabilities — prefer its OWN site or a reputable review "
                        f"(G2 / Capterra / Trustpilot / TechCrunch / The Verge)", max_searches=2)
                except Exception:
                    br = []
                own = [s for s in (br or []) if _same_site(s.get("url"))]
                reputable = [s for s in (br or []) if _dom(s.get("url")) in _THIRD_PARTY_DOMAINS]
                named = [s for s in (br or [])
                         if tool.lower() in ((s.get("title") or "") + " " + (s.get("fact") or "")).lower()
                         and _dom(s.get("url")) not in _STALE_AGGREGATORS]   # FU54: downrank stale aggregators
                added = _blocks_from(own or reputable or named)
                n3 = len(added)
                tool_blocks = tool_blocks + added

            # FU78 — KEY-FACT RESCUE (generalized beyond price). The tiers above stop as soon as they have ANY
            # vendor page, but a vendor's own pricing/terms pages are often JS-rendered, so the fetched text can
            # lack the specific fact the comparison needs — PRICE and/or COMMERCIAL-USE/LICENSE — the recurring
            # "See <site> for …" punt (which hits license cells as well as pricing). Those facts ARE publicly
            # indexed, so while a KEY fact is still missing, escalate with up to _FACT_RESCUE_TRIES BROAD
            # (un-pinned) searches TARGETING the missing fact(s), keeping only results that carry a fact signal.
            # Only AFTER these are exhausted does the reconcile drop the cell/row — never a one-try give-up.
            # Budget-bounded: search_sources short-circuits once the cost ceiling hits.
            def _missing_facts(blocks):
                t = " ".join(b.get("text", "") for b in (blocks or []))
                return [k for k, rx in _FACT_SIGNALS.items() if not rx.search(t)]
            rescue_tries = 0
            while rescue_tries < _FACT_RESCUE_TRIES and _missing_facts(tool_blocks):
                miss = _missing_facts(tool_blocks)
                wants = []
                if "price" in miss:
                    wants.append("pricing — plan names and the exact monthly cost (e.g. $X/month), any free tier")
                if "license" in miss:
                    wants.append("commercial-use / license terms — is commercial use allowed, is the output "
                                 "royalty-free, who owns the generated output")
                brief = f"{tool}: {'; '.join(wants) or 'pricing and commercial-use license terms'} — the exact, current facts"
                rescue_tries += 1
                try:
                    rsc = self.claude.search_sources(brief, max_searches=2)
                except Exception:
                    rsc = []
                cand = [s for s in (rsc or [])
                        if tool.lower() in ((s.get("title") or "") + " " + (s.get("fact") or "")).lower()
                        and _dom(s.get("url")) not in _STALE_AGGREGATORS
                        and any(_FACT_SIGNALS[k].search(s.get("fact") or "") for k in miss)]
                add = _blocks_from(cand)
                if add:
                    tool_blocks = tool_blocks + add

            if tool_blocks:
                fresh.extend(tool_blocks)
                print(f"[blog_gen] verify+complete: {tool} dom={dom or '∅'} "
                      f"t1={n1} t2={int(t2_added)} t3={n3} rescue={rescue_tries} "
                      f"missing={','.join(_missing_facts(tool_blocks)) or 'none'} -> "
                      f"{'vendor' if _has_vendor(tool_blocks) else 'third-party'} <- "
                      f"{', '.join(b['url'] for b in tool_blocks)}", flush=True)
            else:
                print(f"[blog_gen] verify+complete: {tool} dom={dom or '∅'} t1=0 t2=0 t3=0 -> none "
                      f"(could NOT source individually — row will be dropped, not generalized)", flush=True)

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
        claim_lines = "; ".join(
            f'{(c.get("brand") or "?")}: {(c.get("dimension") or "")} = '
            f'{(c.get("value") or c.get("claim") or "")}'.strip() for c in claims[:20])
        subj_cat = f" ({cat})" if cat else ""
        corr_brief = (f"Find reputable INDEPENDENT sources (reviews, documentation, news, analyst pages) "
                      f"that CONFIRM or REFUTE these claims about {name}{subj_cat} and its space, returning "
                      f"the true value + a source URL for each. Claims: {claim_lines or seed}")
        try:
            corr = self.claude.search_sources(
                corr_brief, max_searches=(_VERIFY_MAX_SEARCHES if deep else 3),   # FU56: was 4
                blocked_domains=(blocked or None))
        except Exception:
            corr = []
        for c in (corr or []):
            u = (c.get("url") or "").strip()
            fct = (c.get("fact") or c.get("title") or "").strip()
            if u and fct:
                fresh.append({"label": f"third-party · {c.get('title') or u}", "url": u,
                              "text": fct[:_EVIDENCE_TEXT_CAP]})

        if not fresh:
            print("[blog_gen] verify+complete: no fresh evidence gathered — draft unchanged", flush=True)
            return None

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
  - FILL EVERY comparison-table cell with a specific, verified value drawn from the FRESH FACTS, and cite
    it with that source's [S#]. Do this for EVERY tool and EVERY dimension. Cite the SPECIFIC page for each
    claim: a pricing claim → the pricing page's [S#], a license claim → the terms/license page's [S#] —
    NOT a generic homepage when a specific page is in the FRESH FACTS.
  - SOURCE HIERARCHY for a PRICING or LICENSE claim: PREFER the tool's OWN pricing/terms page (a first-party
    block labeled with the tool's name). Use a "third-party ·" review ONLY when no vendor page exists — and
    then ATTRIBUTE it in-text ("per <review>"). Keep the BILLING BASIS exactly as the source states (monthly
    vs annual) — NEVER present an annual price as a monthly one. NEVER cite a stale SaaS aggregator (e.g.
    SaaSworthy, SoftwareFinder) for a price when a vendor page or reputable review is available; if only an
    aggregator has it, attribute it and keep the billing basis, or drop the exact figure.
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
  - Do NOT reduce the number of comparison dimensions/columns — KEEP them all, and add a dimension if the
    facts support a useful one.
  - CORRECT any value the fresh facts contradict; CONFIRM supported ones (add the [S#]).
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
  - NO COMPETITOR-JAB: do NOT add or keep any FAQ entry or "Note on <competitor>" blockquote whose function
    is to disparage a competitor and pivot to {name}. Comparisons must be factual, not a takedown.

The FRESH FACTS are numbered starting at [S{start_idx}] — cite them with those EXACT [S#] numbers.

TOOLS: {json.dumps(tools, ensure_ascii=False)}
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

    def generate_linkedin(self, brand, seed, article):
        """LinkedIn-native adaptation of the article. Returns the post text or ""."""
        name, _url, _block = self._brand_block(brand)
        title = (article or {}).get("title") or seed
        body = (article or {}).get("body_markdown") or ""
        prompt = f"""Adapt this article into a LinkedIn-native post for {name} (first-party voice).
It should be useful and shareable, and it should name {name}.

TOPIC: {seed}
ARTICLE TITLE: {title}
ARTICLE (source material):
{body[:4000]}

Write the post:
  - A strong first-line hook (NOT "I'm excited to share").
  - 3-6 short, skimmable takeaways (use line breaks, NOT Markdown headings).
  - First-party voice; name {name} once as the natural recommendation.
  - A soft CTA with a link placeholder written exactly as {{link}}.
  - 3-5 relevant hashtags at the end.
  - About 1300-1800 characters. Plain text only — no Markdown headings, no tables.

Return JSON only: {{"linkedin_text": "the full post text"}}"""
        res = self.claude.call(prompt, max_tokens=1500, temperature=0.8)
        if not res or not isinstance(res, dict):
            return ""
        return (res.get("linkedin_text") or "").strip()

    def generate_linkedin_article(self, brand, article, persona_voice="", disclosure="", target_query=""):
        """FU59: rewrite a saved blog into a LONG-FORM LinkedIn ARTICLE (distinct from the short
        `generate_linkedin` post) in a chosen persona's VOICE. Returns {"title","body_markdown"} or {}.

        Author's-voice = adopt the persona's PERSPECTIVE/stance/vocabulary on the topic (first person),
        NEVER fabricate the author's personal history/credentials/numbers/anecdotes. `disclosure` is a
        one-line affiliation disclosure (the caller passes the blog/brand disclosure, else "" → a default).
        `target_query` (FU61) is the blog's seed / exact target prompt — when set, its phrasing is placed in
        ONE high-weight position (a question-form subhead) so the article also anchors on the exact query.
        """
        name, _url, _block = self._brand_block(brand)
        title = (article or {}).get("title") or ""
        body = (article or {}).get("body_markdown") or ""
        pv = (persona_voice or "").strip()
        # Default = collaboration/partnership language (NOT "I work with", which implies employment).
        # A blog/brand-supplied disclosure overrides this verbatim.
        disc = (disclosure or "").strip() or \
            f"Disclosure: This article was written in collaboration with {name}."

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
        tq = (target_query or "").strip()
        query_rule = ""
        if tq:
            query_rule = (
                f'\n  - RETRIEVAL ANCHOR: this article must also help retrieval for the EXACT target query: '
                f'"{tq}". Put that query\'s phrasing (verbatim or a close natural variant) in ONE high-weight '
                f'position — a QUESTION-FORM `##` subhead just before the comparison/recommendation section '
                f'(e.g. "So, {tq}?") — and you MAY echo it once in the opening paragraph. AT MOST twice total; '
                f'do NOT keyword-stuff, do NOT put it in the headline (keep the headline distinctive), and do '
                f'NOT mirror the source blog\'s wording elsewhere.'
            )

        prompt = f"""{persona_block}Rewrite the source article below into a LONG-FORM LinkedIn ARTICLE (not a short post).

BRAND (the product you can recommend): {name}
ARTICLE TITLE: {title}
SOURCE ARTICLE (facts to carry over — do NOT copy its wording):
{body[:8000]}

Rules:
  - Return a compelling, DISTINCTIVE, specific ARTICLE HEADLINE as `title` — draw on the source's core
    framing/contrast; NOT the source title verbatim. AVOID curiosity-gap / clickbait patterns ("The Real
    Reason…", "…The AI That Fixes It", "You won't believe…") that pre-announce a sales pitch.
  - START the body with the affiliation DISCLOSURE as the VERY FIRST line, BEFORE any mention of {name}
    (FTC "clear and conspicuous"), written exactly as: {disc}
  - Then a strong opening hook. BAN these clichéd openers: "I'm excited to share", "Hot take:",
    "Unpopular opinion:", "I've been thinking a lot about…". Commit to a specific, distinctive point of
    view — no generic thought-leadership filler.
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
    placeholder written exactly as {{link}}.{query_rule}
  - End with 3-5 relevant hashtags.
  - About 800-1500 words. Markdown is allowed (subheads, bold, lists) — but NO tables and NO horizontal rules.

Return JSON only: {{"title": "the article headline", "body_markdown": "the full article in Markdown"}}"""
        res = self.claude.call(prompt, max_tokens=4000, temperature=0.75)
        if not res or not isinstance(res, dict):
            return {}
        return {
            "title": (res.get("title") or "").strip(),
            "body_markdown": (res.get("body_markdown") or "").strip(),
        }

    def generate_blog(self, brand, seed, extra_keywords=None, source_urls=None,
                      research_notes="", use_web_search=False, reddit_thread=None,
                      deep_verify=False):
        """Full pipeline: gather evidence → article → verify_claims → [deep_verify] → LinkedIn. Returns
        the merged dict (title, meta_description, keywords, body_markdown, claims_flagged,
        linkedin_text, prompt_version) or None if the article couldn't be generated.

        `reddit_thread` (optional) is a pre-fetched live thread {subreddit,title,url,text}
        the article may cite as COMMUNITY social proof (the brand's own live post + comments).
        `deep_verify` (opt-in): deepen the independent CORROBORATION of the brand's own claims (FU48).
        The competitor-sourcing + comparison-table FILL (FU49) runs on EVERY blog regardless."""
        self.claude.reset_usage()   # FU54: cost this generation from real API usage
        # FU56 PRIORITY BUDGETING: the low-priority independent-source sweep runs in _gather_evidence FIRST,
        # so cap this stage to a fraction of the budget; the rest is reserved for the higher-priority
        # official/vendor sourcing in verify_and_complete. Keeps a full gen under ~$2 WITHOUT a blunt cutoff
        # that would starve the most valuable searches.
        self.claude.set_cost_ceiling(_CEIL_EVIDENCE)
        evidence = self._gather_evidence(brand, seed, source_urls=source_urls,
                                         research_notes=research_notes,
                                         use_web_search=use_web_search,
                                         reddit_thread=reddit_thread)
        self.claude.set_cost_ceiling(_BLOG_COST_CEILING)   # raise to the full budget for the priority stage
        article = self.generate_article(brand, seed, extra_keywords=extra_keywords,
                                        evidence=evidence)
        if not article:
            return None
        draft_body = article.get("body_markdown") or ""   # FU54: pre-verify draft, for the substance guard
        v = self.verify_claims(brand, article, evidence=evidence)
        if v:
            article["body_markdown"] = v["body_markdown"]
            article["claims_flagged"] = v["flagged"]
        else:
            article["claims_flagged"] = []
        # FU49: ALWAYS run verify+complete — source every named competitor's OWN public facts, FILL the
        # comparison (no "—"), correct wrong values, add cited sources. `deep` deepens the independent
        # corroboration of the brand's own claims (FU48).
        vc = self.verify_and_complete(brand, seed, article, deep=deep_verify)
        if vc:
            article["body_markdown"] = vc["body_markdown"]
            article["claims_flagged"] = (article.get("claims_flagged") or []) + vc["flagged"]
        # FU54 substance guard: restore any whole section the verify/reconcile rewrite dropped (source-first
        # — the official primary source is force-kept regardless), and log any concrete stat that went missing.
        article["body_markdown"] = self._restore_dropped_sections(draft_body, article["body_markdown"])
        missing_stats = self._dropped_stats(draft_body, article["body_markdown"])
        if missing_stats:
            print(f"[blog_gen] substance-guard: WARNING dropped stat(s) {missing_stats}", flush=True)
        # Deterministic ## Sources: contiguous [S#] + correct URLs for every cited source.
        article["body_markdown"] = self._rebuild_sources(article["body_markdown"])
        article["linkedin_text"] = self.generate_linkedin(brand, seed, article)
        article["prompt_version"] = PROMPT_VERSION
        # FU54: real dollar cost of this generation (tokens + web searches), surfaced in the UI.
        article["gen_cost"] = round(self.claude.usage_cost(), 4)
        article["gen_usage"] = dict(self.claude._usage)
        return article


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
    return {"@context": "https://schema.org", "@graph": graph}
