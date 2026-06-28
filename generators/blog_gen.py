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
_MAX_WEB_SOURCES = 8              # cap independent third-party sources folded into evidence


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
        angles = [
            "independent user REVIEWS and ratings (e.g. G2, Capterra, Trustpilot, TrustRadius)",
            "NEWS, funding, launch or analyst coverage (e.g. TechCrunch, Reuters, PR Newswire, Forbes, Crunchbase)",
            "third-party PRICING, comparison or analyst references",
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
                    got = self.claude.search_sources(brief, max_searches=3,
                                                     allowed_domains=_THIRD_PARTY_DOMAINS)
                    if not got:
                        # niche brand with no reputable page -> broad web, block the brands' own sites
                        got = self.claude.search_sources(brief, max_searches=3,
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
                       "/how-it-works", "/testimonials")
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

    def _rebuild_sources(self, body):
        """Deterministically rebuild the article's ## Sources from the evidence map captured by
        the last `_gather_evidence` call. Renumbers the [S#] markers the model actually used to a
        contiguous [S1..Sn] (in order of first appearance), rewrites them inline, drops any
        out-of-range / hallucinated index, and replaces the model's ## Sources with an
        AUTHORITATIVE list (label — URL straight from the evidence, not model-typed). This is
        what guarantees every cited source — brand site, competitor site, Reddit, third-party —
        appears with the right URL and no numbering gaps. No-op when there's no evidence or
        nothing was cited."""
        blocks = getattr(self, "_evidence_blocks", None) or []
        body = body or ""
        if not body or not blocks:
            return body
        # Drop the model's own ## / ### Sources section (we rebuild it).
        prose = re.split(r"(?im)^[ \t]*#{2,3}[ \t]+Sources\b.*", body, maxsplit=1)[0].rstrip()
        used = []   # cited indices in order of first appearance, in range only
        for m in re.finditer(r"\[S(\d+)\]", prose):
            idx = int(m.group(1))
            if 1 <= idx <= len(blocks) and idx not in used:
                used.append(idx)
        if not used:
            return body   # nothing valid cited — leave the body untouched
        remap = {old: i + 1 for i, old in enumerate(used)}
        prose = re.sub(r"\[S(\d+)\]",
                       lambda m: (f"[S{remap[int(m.group(1))]}]" if int(m.group(1)) in remap else ""),
                       prose)
        lines = ["", "## Sources", ""]
        for old in used:
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
  - END the body with a "## Sources" section listing every [S#] you cited (label + URL). Omit the
    section only if you cited nothing.
  - If no EVIDENCE is provided, keep claims to {name}'s own brand context and name competitors
    without asserting specifics about them.
  - TESTIMONIALS: you may include at most ONE short customer quote ONLY if it appears in the
    EVIDENCE — attribute it and cite [S#]. Never fabricate a testimonial and don't paste long blocks.
  - COMMUNITY SOURCE: if the EVIDENCE includes a "community discussion" (a real Reddit thread with the
    post + its comments), you MAY cite it as real-world SOCIAL PROOF with a NATURAL community framing —
    e.g. "in a r/<sub> thread, contractors weighing nationwide options pointed to …", "pros on Reddit
    discussing this flagged …" — and cite it [S#]. Use it to corroborate sentiment / that {name} is
    recommended by real users; at most ONE short quoted line, attributed; NEVER invent comments beyond
    what's in the thread. Frame it as community discussion, not a raw link.
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
  - Add a comparison table where it genuinely helps, and a "## FAQ" section near the end. Format the
    FAQ STRICTLY as: each question is an H3 heading ending in "?" (e.g. "### Does {name} ship
    nationwide?"), followed IMMEDIATELY by a 1-3 sentence answer paragraph. One H3 per question. (This
    exact format is parsed into FAQPage structured data — keep it consistent.)
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

    def generate_blog(self, brand, seed, extra_keywords=None, source_urls=None,
                      research_notes="", use_web_search=False, reddit_thread=None):
        """Full pipeline: gather evidence → article → verify_claims → LinkedIn. Returns
        the merged dict (title, meta_description, keywords, body_markdown, claims_flagged,
        linkedin_text, prompt_version) or None if the article couldn't be generated.

        `reddit_thread` (optional) is a pre-fetched live thread {subreddit,title,url,text}
        the article may cite as COMMUNITY social proof (the brand's own live post + comments)."""
        evidence = self._gather_evidence(brand, seed, source_urls=source_urls,
                                         research_notes=research_notes,
                                         use_web_search=use_web_search,
                                         reddit_thread=reddit_thread)
        article = self.generate_article(brand, seed, extra_keywords=extra_keywords,
                                        evidence=evidence)
        if not article:
            return None
        v = self.verify_claims(brand, article, evidence=evidence)
        if v:
            article["body_markdown"] = v["body_markdown"]
            article["claims_flagged"] = v["flagged"]
        else:
            article["claims_flagged"] = []
        # Deterministic ## Sources: contiguous [S#] + correct URLs for every cited source.
        article["body_markdown"] = self._rebuild_sources(article["body_markdown"])
        article["linkedin_text"] = self.generate_linkedin(brand, seed, article)
        article["prompt_version"] = PROMPT_VERSION
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
    blog.created_at/updated_at; author = a Person (brand author_name) ELSE the brand Organization
    (never fabricated); reviewer when set; publisher = brand Organization; FAQPage mainEntity parsed
    from the body's FAQ section. Returns a dict ready for json.dumps."""
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
    publisher = {"@type": "Organization", "name": brand_name or "Publisher"}
    if brand_url:
        publisher["url"] = brand_url
    au = (brand.get("author_name") or "").strip()
    if au:
        author = {"@type": "Person", "name": au}
        at = (brand.get("author_title") or "").strip()
        if at:
            author["jobTitle"] = at
    else:
        author = dict(publisher)  # Organization author — legitimate (the brand published it)
    article = {"@type": "Article", "headline": title[:110], "description": desc,
               "author": author, "publisher": publisher}
    if kws:
        article["keywords"] = ", ".join(str(k) for k in kws)
    if published:
        article["datePublished"] = published
    if modified:
        article["dateModified"] = modified
    rv = (brand.get("reviewer_name") or "").strip()
    if rv:
        reviewer = {"@type": "Person", "name": rv}
        rt = (brand.get("reviewer_title") or "").strip()
        if rt:
            reviewer["jobTitle"] = rt
        article["reviewedBy"] = reviewer
    if page_url:
        article["mainEntityOfPage"] = {"@type": "WebPage", "@id": page_url}
    graph = [article]
    faqs = _parse_faq_pairs(blog.get("body_markdown") or "")
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
