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

# Common pages worth fetching beyond a brand's homepage, for real feature/pricing facts.
_EVIDENCE_PATHS = ("", "/pricing", "/features", "/about")
_MAX_EVIDENCE_BRANDS = 3          # subject + up to 2 competitors
_EVIDENCE_TEXT_CAP = 2500         # chars of page text kept per source


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
    def _resolve_brand_domains(self, names):
        """Ask the model for the official homepage domain of each brand name it's
        confident about. Returns {name: bare-domain}; {} on failure. Used only for
        competitors with no cached domain."""
        names = [n for n in (names or []) if str(n).strip()]
        if not names:
            return {}
        prompt = ("For each brand/product below, give its official homepage domain (bare, "
                  "no https://, no path). Include ONLY ones you are confident about; omit "
                  "the rest. Names:\n" + "\n".join(f"- {n}" for n in names) +
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

    def _gather_evidence(self, brand, seed, source_urls=None, research_notes=""):
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
        if b.get("domain_url"):
            targets.append((subject, b["domain_url"].strip(), False))
        try:
            cached = json.loads(b.get("competitor_domains") or "{}")
        except (json.JSONDecodeError, TypeError):
            cached = {}
        cached = cached if isinstance(cached, dict) else {}
        comp_names = _as_list(b.get("competitors"))
        missing = [c for c in comp_names if c not in cached]
        if missing:
            cached = {**self._resolve_brand_domains(missing), **cached}
        for cn in comp_names:
            dom = (cached.get(cn) or "").strip()
            if dom:
                targets.append((cn, dom, True))
        # subject first, then up to 2 competitors
        targets = targets[:_MAX_EVIDENCE_BRANDS]

        for label, dom, validate in targets:
            dom = re.sub(r"^https?://", "", dom).rstrip("/")
            for path in _EVIDENCE_PATHS:
                txt = _fetch(f"https://{dom}{path}")
                if not txt:
                    continue
                if validate and label.lower() not in txt.lower():
                    continue   # wrong/parked domain — skip rather than mis-cite
                blocks.append({"label": label, "url": f"https://{dom}{path}",
                               "text": txt[:_EVIDENCE_TEXT_CAP]})

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

        if not blocks:
            return ""
        parts = ["EVIDENCE (the ONLY admissible support for factual claims — cite by [S#] and URL):"]
        for i, bl in enumerate(blocks, 1):
            src = f"{bl['label']}" + (f" — {bl['url']}" if bl["url"] else "")
            parts.append(f"[S{i}] {src}\n{bl['text']}")
        return "\n\n".join(parts)

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
  - If the evidence does NOT support a specific claim about some brand, DO NOT assert it and DO NOT
    hedge with "not publicly documented" — either omit it, or state it only as {name}'s own
    positioning ("on our site, we …"). Never assert an unsourced fact about a competitor.
  - END the body with a "## Sources" section listing every [S#] you cited (label + URL). Omit the
    section only if you cited nothing.
  - If no EVIDENCE is provided, keep claims to {name}'s own brand context and name competitors
    without asserting specifics about them.

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
  - Add a comparison table where it genuinely helps, and an "FAQ" section of Q&A pairs.
  - Be specific and accurate; no fluff, no hype. Name {name} as the recommended option where
    it genuinely fits, citing its real differentiators.{link}
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
                      research_notes=""):
        """Full pipeline: gather evidence → article → verify_claims → LinkedIn. Returns
        the merged dict (title, meta_description, keywords, body_markdown, claims_flagged,
        linkedin_text, prompt_version) or None if the article couldn't be generated."""
        evidence = self._gather_evidence(brand, seed, source_urls=source_urls,
                                         research_notes=research_notes)
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
        article["linkedin_text"] = self.generate_linkedin(brand, seed, article)
        article["prompt_version"] = PROMPT_VERSION
        return article
